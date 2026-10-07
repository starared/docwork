"""作品与版本。版本不可变；恢复旧版本会复制出一个新的最新版本。"""
from __future__ import annotations

from . import db, storage
from .util import UserError, new_id, now

KIND_LABELS = {"ppt": "PPT", "doc": "Word", "xls": "Excel", "import_pptx": "导入的 PPT", "import_docx": "导入的 Word",
               "import_xlsx": "导入的 Excel"}
EXPORT_NAMES = {"pptx": "pptx", "docx": "docx", "xlsx": "xlsx", "pdf": "pdf", "data_xlsx": "xlsx"}


def create_work(workspace_id: str, kind: str, title: str, source: str = "generate", origin_file_id: str | None = None,
                folder: str = "", shared_from: str | None = None, conn=None) -> str:
    wid = new_id("wk_")
    t = now()
    db.insert("works", {"id": wid, "workspace_id": workspace_id, "kind": kind, "title": (title or "未命名")[:120], "source": source,
                        "origin_file_id": origin_file_id, "folder": folder, "shared_from": shared_from, "created_at": t, "updated_at": t}, conn=conn)
    return wid


def get_version(version_id: str) -> dict | None:
    v = db.one("SELECT * FROM versions WHERE id=?", (version_id,))
    if v:
        v["spec"] = db.jload(v["spec"], None)
        v["manifest"] = db.jload(v["manifest"], {})
        v["changed"] = db.jload(v["changed"], [])
    return v


def current_version(work_id: str) -> dict | None:
    w = db.one("SELECT current_version_id FROM works WHERE id=?", (work_id,))
    return get_version(w["current_version_id"]) if w and w["current_version_id"] else None


def list_versions(work_id: str) -> list[dict]:
    rows = db.all_("SELECT id, number, source, message, starred, created_at, changed, base_version_id, job_id, manifest "
                   "FROM versions WHERE work_id=? ORDER BY number DESC", (work_id,))
    for r in rows:
        r["changed"] = db.jload(r["changed"], [])
        # 对话修改时模型的答复保存在清单里，编辑器重新打开时据此恢复对话记录
        r["reply"] = str((db.jload(r.pop("manifest"), {}) or {}).get("reply") or "")
    return rows


def version_summaries(version_ids: list[str]) -> dict[str, dict]:
    """作品列表需要的版本摘要：版本号、缩略图、页数、未解决的排版问题数、导出文件。只读清单，不读规格。"""
    ids = [v for v in dict.fromkeys(version_ids) if v]
    out: dict[str, dict] = {}
    for i in range(0, len(ids), 500):
        part = ids[i:i + 500]
        rows = db.all_(f"SELECT id, number, manifest FROM versions WHERE id IN ({','.join('?' for _ in part)})", part)
        for r in rows:
            m = db.jload(r["manifest"], {}) or {}
            pages = m.get("pages") or []
            exports = {k: {"file_id": e["file_id"], "name": e.get("name", "")}
                       for k, e in (m.get("exports") or {}).items() if isinstance(e, dict) and e.get("file_id")}
            out[r["id"]] = {"version": r["number"], "thumb": pages[0].get("file_id") if pages else None, "pages": len(pages),
                            "issues": sum(1 for x in m.get("issues", []) if x.get("status") not in ("fixed", "visual_ok")),
                            "exports": exports}
    return out


def add_version(work_id: str, *, job_id: str | None, source: str, message: str = "", spec: dict | None = None,
                spec_version: int | None = None, renderer_version: str | None = None, file_sha: str | None = None,
                manifest: dict | None = None, changed: list | None = None, base_version_id: str | None = None,
                title: str | None = None, search_text: str = "") -> dict:
    """新增版本并设为当前版本。按 job_id 幂等：同一任务重试不会产生第二个版本。"""
    conn = db.connect()
    with db.tx(conn):
        if job_id:
            ex = conn.execute("SELECT id FROM versions WHERE job_id=?", (job_id,)).fetchone()
            if ex:
                return get_version(ex["id"])
        w = conn.execute("SELECT * FROM works WHERE id=?", (work_id,)).fetchone()
        if not w:
            raise UserError("作品不存在", 404)
        if source in ("chat", "manual", "inplace") and base_version_id and w["current_version_id"] != base_version_id:
            raise UserError("作品已经有了新版本，本次修改未保存，请刷新后重新操作", 409, "conflict")
        n = conn.execute("SELECT COALESCE(MAX(number),0)+1 FROM versions WHERE work_id=?", (work_id,)).fetchone()[0]
        vid = new_id("v_")
        db.insert("versions", {
            "id": vid, "work_id": work_id, "number": n, "job_id": job_id, "source": source, "message": message[:500],
            "spec": spec, "spec_version": spec_version, "renderer_version": renderer_version, "file_sha": file_sha,
            "manifest": manifest or {}, "changed": changed or [], "base_version_id": base_version_id, "created_at": now(),
            "search_text": (search_text or "")[:200000],
        }, conn=conn)
        upd = {"current_version_id": vid, "updated_at": now()}
        if title:
            upd["title"] = title[:120]
        db.update("works", {"id": work_id}, upd, conn=conn)
        _register_files(w["workspace_id"], work_id, vid, manifest or {}, conn)
        index(work_id, title or w["title"], search_text, conn=conn)
    return get_version(vid)


def _register_files(workspace_id: str, work_id: str, version_id: str, manifest: dict, conn) -> None:
    """把版本引用的导出文件、预览图登记为文件记录（下载时按作品归属检查权限）。"""
    base = (manifest.get("title") or "document")
    for key, info in (manifest.get("exports") or {}).items():
        if not info or "sha" not in info:
            continue
        name = info.get("name") or f"{base}.{EXPORT_NAMES.get(key, key)}"
        rec = storage.create_file_record(workspace_id, info["sha"], info.get("size", 0), name, "export",
                                         work_id=work_id, version_id=version_id, meta={"export": key}, conn=conn)
        info["file_id"] = rec["id"]
    for page in manifest.get("pages") or []:
        if page.get("preview"):
            rec = storage.create_file_record(workspace_id, page["preview"], page.get("size", 0), f"page_{page.get('id')}.png",
                                             "preview", mime="image/png", work_id=work_id, version_id=version_id, conn=conn)
            page["file_id"] = rec["id"]
    # 回写 file_id 到清单
    db.update("versions", {"id": version_id}, {"manifest": manifest}, conn=conn)


def drop_previews(version_id: str) -> bool:
    """去掉版本清单中的页面预览引用并删除预览文件记录（页面 ID 和内容哈希保留，用于对比）。"""
    conn = db.connect()
    with db.tx(conn):
        v = conn.execute("SELECT manifest FROM versions WHERE id=?", (version_id,)).fetchone()
        if not v:
            return False
        man = db.jload(v["manifest"], {})
        pages = man.get("pages") or []
        if not any(p.get("preview") for p in pages):
            return False
        for p in pages:
            for k in ("preview", "size", "file_id"):
                p.pop(k, None)
        man["previews_pruned"] = True
        db.update("versions", {"id": version_id}, {"manifest": man}, conn=conn)
        conn.execute("DELETE FROM files WHERE version_id=? AND kind='preview'", (version_id,))
    return True


def set_previews(version_id: str, pages: list[dict]) -> None:
    """重新生成的预览写回版本清单并登记文件记录。"""
    conn = db.connect()
    with db.tx(conn):
        v = conn.execute("SELECT * FROM versions WHERE id=?", (version_id,)).fetchone()
        if not v:
            return
        w = conn.execute("SELECT workspace_id FROM works WHERE id=?", (v["work_id"],)).fetchone()
        man = db.jload(v["manifest"], {})
        man["pages"] = pages
        man.pop("previews_pruned", None)
        conn.execute("DELETE FROM files WHERE version_id=? AND kind='preview'", (version_id,))
        for page in pages:
            if page.get("preview"):
                rec = storage.create_file_record(w["workspace_id"], page["preview"], page.get("size", 0), f"page_{page.get('id')}.png",
                                                 "preview", mime="image/png", work_id=v["work_id"], version_id=version_id, conn=conn)
                page["file_id"] = rec["id"]
        db.update("versions", {"id": version_id}, {"manifest": man}, conn=conn)


def preview_state(version: dict) -> dict:
    """版本预览是否可用；被清理过的版本在第一次查看时排队重新生成（每个版本同时只有一个任务）。"""
    from . import jobs

    man = version.get("manifest") or {}
    if not man.get("previews_pruned"):
        return {"state": "ok"}
    active = db.one(
        "SELECT id FROM jobs WHERE kind='version_preview' AND work_id=? AND json_extract(params,'$.version_id')=? "
        "AND status IN ('queued','running','cancelling') ORDER BY created_at DESC LIMIT 1",
        (version["work_id"], version["id"]),
    )
    if active:
        return {"state": "pending", "job_id": active["id"]}
    failed = db.one(
        "SELECT id, error FROM jobs WHERE kind='version_preview' AND work_id=? AND json_extract(params,'$.version_id')=? "
        "AND status='failed' AND finished_at > ? ORDER BY created_at DESC LIMIT 1",
        (version["work_id"], version["id"], now() - 600),
    )
    if failed:
        return {"state": "failed", "error": failed["error"]}
    w = db.one("SELECT workspace_id FROM works WHERE id=?", (version["work_id"],))
    j = jobs.enqueue("version_preview", w["workspace_id"], {"version_id": version["id"]}, work_id=version["work_id"],
                     title="重新生成历史版本预览")
    return {"state": "pending", "job_id": j["id"]}


def index(work_id: str, title: str, body: str, conn=None) -> None:
    conn = conn or db.connect()
    conn.execute("DELETE FROM works_fts WHERE work_id=?", (work_id,))
    conn.execute("INSERT INTO works_fts(work_id, title, body) VALUES(?,?,?)", (work_id, title or "", (body or "")[:200000]))


def version_search_text(v: dict) -> str:
    """旧数据库中的版本按需重建正文，新版本直接使用持久化的索引文本。"""
    if v.get("search_text") is not None:
        return v["search_text"]
    w = db.one("SELECT kind FROM works WHERE id=?", (v["work_id"],))
    kind = w["kind"] if w else ""
    if v.get("spec") is not None:
        from .spec.deck import Deck, deck_text
        from .spec.document import Document, document_text
        from .spec.workbook import Workbook, workbook_text
        readers = {"ppt": (Deck, deck_text), "doc": (Document, document_text), "xls": (Workbook, workbook_text)}
        if kind in readers:
            model, read = readers[kind]
            return read(model.model_validate(v["spec"]))[:200000]
    if v.get("file_sha") and kind.startswith("import_"):
        from .tools.inplace import units_for
        # blob 没有扩展名，openpyxl 读取路径时会拒绝它，使用文件流读取。
        with storage.blob_path(v["file_sha"]).open("rb") as src:
            units = units_for(kind.removeprefix("import_"), src)
        return "\n".join(u.get("text", "") for u in units)[:200000]
    return ""


def restore(work_id: str, version_id: str, actor_job: str | None = None) -> dict:
    v = get_version(version_id)
    if not v or v["work_id"] != work_id:
        raise UserError("版本不存在", 404)
    man = dict(v["manifest"])
    man.pop("issues_fixed", None)
    return add_version(work_id, job_id=actor_job, source="restore", message=f"恢复到第 {v['number']} 版", spec=v["spec"],
                       spec_version=v["spec_version"], renderer_version=v["renderer_version"], file_sha=v["file_sha"],
                       manifest=_strip_file_ids(man), changed=[], base_version_id=version_id, search_text=version_search_text(v))


def _strip_file_ids(man: dict) -> dict:
    import copy

    m = copy.deepcopy(man)
    for info in (m.get("exports") or {}).values():
        if isinstance(info, dict):
            info.pop("file_id", None)
    for p in m.get("pages") or []:
        p.pop("file_id", None)
    return m


def duplicate_to(work_id: str, workspace_id: str) -> str:
    """复制作品到另一个工作区（分享为可编辑副本）。"""
    w = db.one("SELECT * FROM works WHERE id=?", (work_id,))
    v = current_version(work_id)
    if not w or not v:
        raise UserError("作品不存在", 404)
    nid = create_work(workspace_id, w["kind"], w["title"], source="share", origin_file_id=None, shared_from=work_id)
    add_version(nid, job_id=None, source="import", message="管理员分享的副本", spec=v["spec"], spec_version=v["spec_version"],
                renderer_version=v["renderer_version"], file_sha=v["file_sha"], manifest=_strip_file_ids(v["manifest"]),
                title=w["title"], search_text=version_search_text(v))
    if v["file_sha"]:
        # 导入作品：原件也复制一份记录（大小取内容库中的真实值，否则工作区用量会把它算成另一份内容）
        blob = db.one("SELECT size FROM blobs WHERE sha=?", (v["file_sha"],))
        storage.create_file_record(workspace_id, v["file_sha"], int(blob["size"]) if blob else 0, w["title"], "original", work_id=nid)
    return nid


def public_work(w: dict) -> dict:
    out = {k: w.get(k) for k in ("id", "workspace_id", "kind", "title", "folder", "starred", "source", "current_version_id",
                                 "created_at", "updated_at", "deleted_at", "shared_from", "busy_job_id")}
    out["tags"] = db.jload(w.get("tags"), [])
    out["kind_label"] = KIND_LABELS.get(w["kind"], w["kind"])
    out["readonly"] = bool(w.get("_readonly"))
    return out
