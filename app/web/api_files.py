"""分片上传（可续传）、文件下载、批量打包、保留工具结果。"""
from __future__ import annotations

import json
import os
import shutil
import zipfile
from pathlib import Path

from starlette.responses import FileResponse

from .. import accounts, db, quota, storage
from ..config import get_settings
from ..tools import limits
from ..util import UserError, new_id, now, safe_filename
from .common import Req, api, disk_blocked, file_response, page_args

CHUNK = 5 * 1024 * 1024
UPLOAD_TTL_DAYS = 7


def _udir(uid: str) -> Path:
    return get_settings().upload_dir / uid


@api(auth="any")
def upload_init(req: Req):
    if disk_blocked():
        raise UserError("服务器磁盘空间不足，暂停上传，请联系管理员清理", 507, "disk_full")
    name = safe_filename(str(req.b("name", "")))
    size = int(req.b("size", 0))
    if size <= 0:
        raise UserError("文件为空")
    uid = new_id("up_")
    # 检查配额与登记上传在同一个写事务中：未完成的上传计入用量，并发开始的多个上传不会一起越过配额
    with db.tx() as conn:
        quota.check_upload(req.s.token_id, req.s.workspace_id, size)
        db.insert("uploads", {"id": uid, "workspace_id": req.s.workspace_id, "name": name, "size": size, "chunk_size": CHUNK,
                              "received": [], "created_at": now()}, conn=conn)
    _udir(uid).mkdir(parents=True, exist_ok=True)
    return {"upload_id": uid, "chunk_size": CHUNK, "chunks": (size + CHUNK - 1) // CHUNK}


def _get_upload(req: Req, uid: str) -> dict:
    u = db.one("SELECT * FROM uploads WHERE id=?", (uid,))
    if not u or u["workspace_id"] != req.s.workspace_id:
        raise UserError("上传任务不存在或已过期", 404)
    u["received"] = db.jload(u["received"], [])
    return u


@api(auth="any", raw_body=True)
async def upload_chunk(req: Req):
    from starlette.concurrency import run_in_threadpool

    uid, idx = req.path["uid"], int(req.path["index"])
    u = await run_in_threadpool(_get_upload, req, uid)
    n = (u["size"] + u["chunk_size"] - 1) // u["chunk_size"]
    if not 0 <= idx < n:
        raise UserError("分片序号无效")
    expect = min(u["chunk_size"], u["size"] - idx * u["chunk_size"])
    path = _udir(uid) / f"{idx:06d}"
    tmp = path.with_suffix(".part")
    got = 0
    with open(tmp, "wb") as f:
        async for chunk in req.request.stream():
            got += len(chunk)
            if got > expect:
                f.close()
                tmp.unlink(missing_ok=True)
                raise UserError("分片大小不正确")
            f.write(chunk)
    if got != expect:
        tmp.unlink(missing_ok=True)
        raise UserError(f"分片不完整（收到 {got} 字节，应为 {expect} 字节）")
    os.replace(tmp, path)

    def mark():
        with db.tx() as conn:
            r = conn.execute("SELECT received FROM uploads WHERE id=?", (uid,)).fetchone()
            rec = set(db.jload(r["received"], []))
            rec.add(idx)
            conn.execute("UPDATE uploads SET received=? WHERE id=?", (json.dumps(sorted(rec)), uid))
            return len(rec)
    count = await run_in_threadpool(mark)
    return {"received": count, "chunks": n}


@api(auth="any")
def upload_status(req: Req):
    u = _get_upload(req, req.path["uid"])
    return {"received": u["received"], "chunk_size": u["chunk_size"], "size": u["size"]}


@api(auth="any")
def upload_complete(req: Req):
    u = _get_upload(req, req.path["uid"])
    n = (u["size"] + u["chunk_size"] - 1) // u["chunk_size"]
    if len(u["received"]) != n:
        raise UserError(f"还有 {n - len(u['received'])} 个分片未上传")
    # 完成时再检查一次：当前文件用量 + 其他尚未完成的上传 + 本文件（期间工作区可能新增了任务结果等文件）。
    # 并发完成时互相都会被计入（尚未完成时算待完成，完成后算文件；文件记录先写入、上传记录后删除），不会一起越过配额。
    try:
        quota.check_upload(req.s.token_id, req.s.workspace_id, u["size"], pending=True, exclude_upload=u["id"])
    except UserError:
        shutil.rmtree(_udir(u["id"]), ignore_errors=True)
        db.run("DELETE FROM uploads WHERE id=?", (u["id"],))
        raise
    d = _udir(u["id"])
    full = d / "full"
    with open(full, "wb") as out:
        for i in range(n):
            with open(d / f"{i:06d}", "rb") as f:
                shutil.copyfileobj(f, out)
    try:
        if full.stat().st_size != u["size"]:
            raise UserError("文件大小与声明不符")
        info = limits.inspect(full, u["name"], allow_font=req.s.is_owner and req.b("purpose") == "font")
        purpose = str(req.b("purpose", "source"))
        ttl = None if purpose == "keep" else UPLOAD_TTL_DAYS * 86400
        rec = storage.store_path_as_file(req.s.workspace_id, full, u["name"], "upload", meta={"kind": info["kind"], **{k: v for k, v in info.items() if k != "kind"}},
                                         ttl=ttl)
    finally:
        shutil.rmtree(d, ignore_errors=True)
        db.run("DELETE FROM uploads WHERE id=?", (u["id"],))
    accounts.audit(req.s, "upload", rec["id"], {"name": u["name"], "size": u["size"]}, req.ip)
    return _pub(rec)


def _pub(f: dict) -> dict:
    meta = f.get("meta")
    if isinstance(meta, str):
        meta = db.jload(meta, {})
    return {"id": f["id"], "name": f["name"], "size": f["size"], "mime": f["mime"], "kind": f["kind"], "meta": meta or {},
            "expires_at": f.get("expires_at"), "created_at": f.get("created_at"), "work_id": f.get("work_id")}


@api(auth="any")
def file_info(req: Req):
    return _pub(accounts.get_file_scoped(req.s, req.path["fid"]))


@api(auth="any")
def file_download(req: Req):
    f = accounts.get_file_scoped(req.s, req.path["fid"])
    inline = req.q("inline") == "1" and (f["mime"].startswith("image/") or f["mime"] == "application/pdf")
    if not inline:
        accounts.audit(req.s, "download", f["id"], {"name": f["name"]}, req.ip)
    return file_response(f, inline=inline)


@api(auth="any")
def files_list(req: Req):
    kind = req.q("kind", "output")
    if kind not in ("output", "upload", "saved"):
        raise UserError("kind 无效")
    limit, offset = page_args(req)
    rows = db.all_("SELECT * FROM files WHERE workspace_id=? AND kind=? AND deleted_at IS NULL AND (expires_at IS NULL OR expires_at > ?) "
                   "ORDER BY created_at DESC LIMIT ? OFFSET ?", (req.s.workspace_id, kind, now(), limit, offset))
    return {"items": [_pub(r) for r in rows]}


@api(auth="any")
def file_keep(req: Req):
    """把工具结果或上传文件保存下来（不再自动过期）。"""
    f = accounts.get_file_scoped(req.s, req.path["fid"])
    if f["workspace_id"] != req.s.workspace_id and not req.s.is_owner:
        raise UserError("只能保存自己的文件", 403)
    quota.check_upload(req.s.token_id, req.s.workspace_id, 0)
    db.run("UPDATE files SET expires_at=NULL, kind='saved' WHERE id=?", (f["id"],))
    return {"ok": True}


@api(auth="any")
def file_delete(req: Req):
    f = accounts.get_file_scoped(req.s, req.path["fid"])
    if f["workspace_id"] != req.s.workspace_id and not req.s.is_owner:
        raise UserError("只能删除自己的文件", 403)
    if f["kind"] not in ("output", "upload", "saved"):
        raise UserError("作品文件请在作品库中删除")
    db.run("UPDATE files SET deleted_at=? WHERE id=?", (now(), f["id"]))
    return {"ok": True}


@api(auth="any")
def files_zip(req: Req):
    ids = req.b("file_ids") or []
    if not ids or len(ids) > 200:
        raise UserError("请选择 1–200 个文件")
    files = [accounts.get_file_scoped(req.s, fid) for fid in ids]
    tmp = get_settings().tmp_dir / f"zip_{new_id()}.zip"
    used = set()
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        for f in files:
            # ZIP 条目名再清理一次（防止 ../ 之类的路径穿越条目，包括旧数据中的文件名）
            base = safe_filename(f["name"])
            name = base
            k = 2
            while name in used:
                stem, dot, ext = base.rpartition(".")
                name = f"{stem}_{k}.{ext}" if dot else f"{base}_{k}"
                k += 1
            used.add(name)
            z.write(storage.blob_path(f["sha"]), name)
    from starlette.background import BackgroundTask
    return FileResponse(tmp, media_type="application/zip", filename="打包下载.zip",
                        background=BackgroundTask(lambda: tmp.unlink(missing_ok=True)))
