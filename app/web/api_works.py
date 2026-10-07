"""作品库、版本、对比、分享。"""
from __future__ import annotations

from .. import accounts, db, works
from ..util import UserError, now
from .common import Req, api, page_args


def _scope_where(req: Req) -> tuple[str, list]:
    s = req.s
    if s.is_owner and req.q("all") == "1":
        return "1=1", []
    if s.is_owner and req.q("workspace"):
        return "w.workspace_id=?", [req.q("workspace")]
    return "(w.workspace_id=? OR w.id IN (SELECT work_id FROM shares WHERE workspace_id=?))", [s.workspace_id, s.workspace_id]


@api(auth="any")
def works_list(req: Req):
    where, base_args = _scope_where(req)
    args = list(base_args)
    conds = [where]
    if req.q("trash") == "1":
        conds.append("w.deleted_at IS NOT NULL")
    else:
        conds.append("w.deleted_at IS NULL")
    kind = req.q("kind")
    if kind:
        if kind == "import":
            conds.append("w.kind LIKE 'import_%'")
        else:
            conds.append("w.kind=?")
            args.append(kind)
    if req.q("folder") is not None and req.q("folder") != "":
        conds.append("w.folder=?")
        args.append(req.q("folder"))
    if req.q("starred") == "1":
        conds.append("w.starred=1")
    if req.q("tag"):
        conds.append("EXISTS (SELECT 1 FROM json_each(w.tags) WHERE value=?)")
        args.append(req.q("tag"))
    if req.q("source"):
        conds.append("w.source=?")
        args.append(req.q("source"))
    q = (req.q("q") or "").strip()
    if q:
        if len(q) >= 3:
            conds.append("w.id IN (SELECT work_id FROM works_fts WHERE works_fts MATCH ?)")
            args.append('"' + q.replace('"', '""') + '"')
        else:
            # trigram 需要至少 3 个字符，且对短于 3 个字符的 LIKE 直接返回空结果：短词逐条查找正文
            conds.append("(w.title LIKE ? OR w.id IN (SELECT work_id FROM works_fts WHERE instr(lower(body), lower(?)) > 0))")
            args += [f"%{q}%", q]
    limit, offset = page_args(req)
    order = {"updated": "w.updated_at DESC", "created": "w.created_at DESC", "title": "w.title"}.get(req.q("sort", "updated"), "w.updated_at DESC")
    rows = db.all_(f"SELECT w.* FROM works w WHERE {' AND '.join(conds)} ORDER BY {order} LIMIT ? OFFSET ?", args + [limit, offset])
    total = db.one(f"SELECT COUNT(*) AS n FROM works w WHERE {' AND '.join(conds)}", args)["n"]
    items = []
    for r in rows:
        p = works.public_work(r)
        if r["workspace_id"] != req.s.workspace_id and not req.s.is_owner:
            p["readonly"] = True
        v = works.current_version(r["id"]) if r["current_version_id"] else None
        if v:
            pages = v["manifest"].get("pages") or []
            p["thumb"] = pages[0].get("file_id") if pages else None
            p["pages"] = len(pages)
            p["issues"] = sum(1 for i in v["manifest"].get("issues", []) if i.get("status") not in ("fixed", "visual_ok"))
            p["version"] = v["number"]
        items.append(p)
    folders = [r["folder"] for r in db.all_(f"SELECT DISTINCT w.folder FROM works w WHERE {where} AND w.folder != '' AND w.deleted_at IS NULL", base_args)]
    tags = sorted({t for r in db.all_(f"SELECT w.tags FROM works w WHERE {where} AND w.deleted_at IS NULL", base_args) for t in db.jload(r["tags"], [])})
    return {"items": items, "total": total, "folders": folders, "tags": tags}


@api(auth="any")
def work_get(req: Req):
    w = accounts.get_work_scoped(req.s, req.path["wid"], include_deleted=True)
    out = works.public_work(w)
    vid = req.q("version") or w["current_version_id"]
    v = works.get_version(vid) if vid else None
    if v and v["work_id"] != w["id"]:
        raise UserError("版本不存在", 404)
    out["version"] = _pub_version(v) if v else None
    out["current_version_id"] = w["current_version_id"]
    job = db.one("SELECT id, kind, status, stage, message FROM jobs WHERE work_id=? AND parent_id IS NULL AND status IN ('queued','running','cancelling') ORDER BY created_at DESC LIMIT 1", (w["id"],))
    out["active_job"] = job
    if req.s.is_owner:
        out["shares"] = db.all_("SELECT s.workspace_id, s.mode, ws.name FROM shares s JOIN workspaces ws ON ws.id=s.workspace_id WHERE s.work_id=?", (w["id"],))
    return out


def _pub_version(v: dict) -> dict:
    m = dict(v["manifest"] or {})
    return {"id": v["id"], "number": v["number"], "source": v["source"], "message": v["message"], "starred": bool(v["starred"]),
            "created_at": v["created_at"], "changed": v["changed"], "spec": v["spec"], "manifest": m,
            "base_version_id": v["base_version_id"], "file_based": bool(v["file_sha"]), "previews": works.preview_state(v)}


@api(auth="any")
def work_update(req: Req):
    w = accounts.get_work_scoped(req.s, req.path["wid"], write=True)
    upd = {}
    if req.b("title") is not None:
        t = str(req.b("title")).strip()
        if not t:
            raise UserError("标题不能为空")
        upd["title"] = t[:120]
    if req.b("folder") is not None:
        upd["folder"] = str(req.b("folder")).strip()[:60]
    if req.b("tags") is not None:
        if not isinstance(req.b("tags"), list):
            raise UserError("标签格式不正确")
        tags = [str(x).strip()[:20] for x in req.b("tags") if str(x).strip()][:20]
        upd["tags"] = tags
    if req.b("starred") is not None:
        upd["starred"] = 1 if req.b("starred") else 0
    if upd:
        upd["updated_at"] = now()
        db.update("works", {"id": w["id"]}, upd)
        if "title" in upd:
            db.run("UPDATE works_fts SET title=? WHERE work_id=?", (upd["title"], w["id"]))
    return {"ok": True}


@api(auth="any")
def work_trash(req: Req):
    hard = req.q("hard") == "1"
    # 永久删除针对的是回收站中的作品，查找时必须包含已删除的作品
    w = accounts.get_work_scoped(req.s, req.path["wid"], write=True, include_deleted=hard)
    if hard:
        if not w["deleted_at"]:
            raise UserError("请先移到回收站")
        from ..scheduler import delete_work_hard
        delete_work_hard(w["id"])
        accounts.audit(req.s, "work_deleted", w["id"], {"title": w["title"]}, req.ip)
        return {"ok": True}
    db.run("UPDATE works SET deleted_at=? WHERE id=?", (now(), w["id"]))
    accounts.audit(req.s, "work_trashed", w["id"], {"title": w["title"]}, req.ip)
    return {"ok": True}


@api(auth="any")
def work_untrash(req: Req):
    w = accounts.get_work_scoped(req.s, req.path["wid"], write=True, include_deleted=True)
    db.run("UPDATE works SET deleted_at=NULL, updated_at=? WHERE id=?", (now(), w["id"]))
    return {"ok": True}


@api(auth="any")
def versions_list(req: Req):
    w = accounts.get_work_scoped(req.s, req.path["wid"])
    return {"items": works.list_versions(w["id"]), "current": w["current_version_id"]}


@api(auth="any")
def version_star(req: Req):
    v = works.get_version(req.path["vid"])
    if not v:
        raise UserError("版本不存在", 404)
    accounts.get_work_scoped(req.s, v["work_id"], write=True)
    db.run("UPDATE versions SET starred=? WHERE id=?", (1 if req.b("starred", True) else 0, v["id"]))
    return {"ok": True}


@api(auth="any")
def work_restore(req: Req):
    w = accounts.get_work_scoped(req.s, req.path["wid"], write=True)
    busy = db.one("SELECT id FROM jobs WHERE work_id=? AND status IN ('queued','running','cancelling') AND kind IN ('edit','import_edit','manual_edit')", (w["id"],))
    if busy:
        raise UserError("作品正在修改中，请等待完成后再恢复", 409)
    v = works.restore(w["id"], str(req.b("version_id", "")))
    accounts.audit(req.s, "version_restored", w["id"], {"from": req.b("version_id")}, req.ip)
    return {"version_id": v["id"], "number": v["number"]}


@api(auth="any")
def work_diff(req: Req):
    """两个版本逐页对照：页面 ID 对齐，标出新增、删除、修改。"""
    w = accounts.get_work_scoped(req.s, req.path["wid"])
    a, b = works.get_version(req.q("a", "")), works.get_version(req.q("b", ""))
    if not a or not b or a["work_id"] != w["id"] or b["work_id"] != w["id"]:
        raise UserError("版本不存在", 404)
    pa = {p["id"]: p for p in a["manifest"].get("pages") or []}
    pb = {p["id"]: p for p in b["manifest"].get("pages") or []}
    order = [p["id"] for p in b["manifest"].get("pages") or []] + [k for k in pa if k not in pb]
    pending = [st for st in (works.preview_state(a), works.preview_state(b)) if st["state"] != "ok"]
    rows = []
    for pid in order:
        x, y = pa.get(pid), pb.get(pid)
        status = "added" if not x else "removed" if not y else ("same" if x.get("hash") == y.get("hash") else "modified")
        rows.append({"id": pid, "status": status, "a": x.get("file_id") if x else None, "b": y.get("file_id") if y else None})
    text = None
    if a["spec"] and b["spec"] and w["kind"] in ("ppt", "doc"):
        text = _text_diff(w["kind"], a["spec"], b["spec"])
    return {"a": {"id": a["id"], "number": a["number"]}, "b": {"id": b["id"], "number": b["number"]}, "pages": rows, "text": text,
            "previews_pending": [st["job_id"] for st in pending if st.get("job_id")]}


def _text_diff(kind: str, a: dict, b: dict) -> list[dict]:
    import difflib

    from ..spec.common import text_of
    items_a = {x["id"]: x for x in (a.get("slides") if kind == "ppt" else a.get("blocks")) or []}
    items_b = {x["id"]: x for x in (b.get("slides") if kind == "ppt" else b.get("blocks")) or []}
    out = []
    for iid, xb in items_b.items():
        xa = items_a.get(iid)
        ta = text_of(xa) if xa else ""
        tb = text_of(xb)
        if ta == tb:
            continue
        ops = []
        for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, ta, tb).get_opcodes():
            ops.append({"op": tag, "a": ta[i1:i2], "b": tb[j1:j2]})
        out.append({"id": iid, "ops": ops[:400]})
    for iid in items_a:
        if iid not in items_b:
            out.append({"id": iid, "removed": True, "text": text_of(items_a[iid])[:500]})
    return out


@api(auth="owner")
def work_share(req: Req):
    w = accounts.get_work_scoped(req.s, req.path["wid"])
    ws = str(req.b("workspace_id", ""))
    wsr = db.one("SELECT * FROM workspaces WHERE id=? AND kind='guest' AND deleted_at IS NULL", (ws,))
    if not wsr:
        raise UserError("访客工作区不存在")
    mode = req.b("mode", "readonly")
    if mode == "readonly":
        db.run("INSERT OR REPLACE INTO shares(work_id, workspace_id, mode, created_at) VALUES(?,?,?,?)", (w["id"], ws, "readonly", now()))
        accounts.audit(req.s, "share_readonly", w["id"], {"workspace": ws}, req.ip)
        return {"ok": True}
    if mode == "copy":
        nid = works.duplicate_to(w["id"], ws)
        accounts.audit(req.s, "share_copy", w["id"], {"workspace": ws, "copy": nid}, req.ip)
        return {"ok": True, "copy_id": nid}
    raise UserError("分享方式只能是 readonly 或 copy")


@api(auth="owner")
def work_unshare(req: Req):
    db.run("DELETE FROM shares WHERE work_id=? AND workspace_id=?", (req.path["wid"], req.path["ws"]))
    return {"ok": True}


@api(auth="owner")
def work_transfer(req: Req):
    """把访客作品转入管理员名下（工作区删除前保存）。"""
    w = accounts.get_work_scoped(req.s, req.path["wid"])
    db.run("UPDATE works SET workspace_id=?, updated_at=? WHERE id=?", (req.s.workspace_id, now(), w["id"]))
    db.run("UPDATE files SET workspace_id=? WHERE work_id=?", (req.s.workspace_id, w["id"]))
    return {"ok": True}


@api(auth="any")
def work_copy(req: Req):
    """复制作品（包括只读分享的作品：复制到自己的工作区后即可编辑）。"""
    w = accounts.get_work_scoped(req.s, req.path["wid"])
    nid = works.duplicate_to(w["id"], req.s.workspace_id)
    return {"id": nid}
