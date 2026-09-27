"""按内容哈希存储的文件库。

文件内容存放在 blobs/ab/cd/<sha256>，同一内容只存一份；
files 表记录每个工作区对内容的引用（名称、类型、归属、过期时间）。
"""
from __future__ import annotations

import hashlib
import mimetypes
import os
import shutil
import uuid
from pathlib import Path

from . import db
from .config import get_settings
from .util import new_id, now, safe_filename

mimetypes.add_type("application/vnd.openxmlformats-officedocument.presentationml.presentation", ".pptx")
mimetypes.add_type("application/vnd.openxmlformats-officedocument.wordprocessingml.document", ".docx")
mimetypes.add_type("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", ".xlsx")
mimetypes.add_type("text/markdown", ".md")


def blob_path(sha: str) -> Path:
    return get_settings().blob_dir / sha[:2] / sha[2:4] / sha


def put_file(src: Path | str, move: bool = False) -> tuple[str, int]:
    """把本地文件存入文件库，返回 (sha, size)。"""
    src = Path(src)
    h = hashlib.sha256()
    with open(src, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    sha = h.hexdigest()
    size = src.stat().st_size
    dst = blob_path(sha)
    if not dst.exists():
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_name(dst.name + f".{os.getpid()}.{uuid.uuid4().hex}.tmp")
        try:
            if move:
                shutil.move(str(src), tmp)
            else:
                shutil.copyfile(src, tmp)
            tmp.chmod(0o644)  # 宿主机 Nginx 需要读取内容文件
            os.replace(tmp, dst)
        finally:
            tmp.unlink(missing_ok=True)
    elif move:
        src.unlink(missing_ok=True)
    db.run("INSERT OR IGNORE INTO blobs(sha, size, created_at) VALUES(?,?,?)", (sha, size, now()))
    return sha, size


def put_bytes(data: bytes) -> tuple[str, int]:
    sha = hashlib.sha256(data).hexdigest()
    dst = blob_path(sha)
    if not dst.exists():
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_name(dst.name + f".{os.getpid()}.{uuid.uuid4().hex}.tmp")
        try:
            tmp.write_bytes(data)
            tmp.chmod(0o644)
            os.replace(tmp, dst)
        finally:
            tmp.unlink(missing_ok=True)
    db.run("INSERT OR IGNORE INTO blobs(sha, size, created_at) VALUES(?,?,?)", (sha, len(data), now()))
    return sha, len(data)


def read_bytes(sha: str) -> bytes:
    return blob_path(sha).read_bytes()


def guess_mime(name: str) -> str:
    return mimetypes.guess_type(name)[0] or "application/octet-stream"


def create_file_record(
    workspace_id: str,
    sha: str,
    size: int,
    name: str,
    kind: str,
    *,
    mime: str | None = None,
    work_id: str | None = None,
    version_id: str | None = None,
    job_id: str | None = None,
    meta: dict | None = None,
    ttl: float | None = None,
    conn=None,
) -> dict:
    # 所有文件记录的名字统一清理：不含路径分隔符和控制字符（下载文件名、ZIP 条目名都来自这里）
    name = safe_filename(name)
    row = {
        "id": new_id("f_"),
        "workspace_id": workspace_id,
        "sha": sha,
        "name": name,
        "mime": mime or guess_mime(name),
        "size": size,
        "kind": kind,
        "work_id": work_id,
        "version_id": version_id,
        "job_id": job_id,
        "meta": meta or {},
        "expires_at": (now() + ttl) if ttl else None,
        "created_at": now(),
    }
    db.insert("files", row, conn=conn)
    return row


def store_path_as_file(workspace_id: str, path: Path, name: str, kind: str, **kw) -> dict:
    sha, size = put_file(path)
    return create_file_record(workspace_id, sha, size, name, kind, **kw)


def get_file(file_id: str) -> dict | None:
    r = db.one("SELECT * FROM files WHERE id=? AND deleted_at IS NULL", (file_id,))
    if r:
        r["meta"] = db.jload(r["meta"], {})
    return r


def materialize(file_or_sha: dict | str, dest_dir: Path, name: str | None = None) -> Path:
    """把文件复制到任务目录（外部程序只接触任务目录中的副本）。"""
    if isinstance(file_or_sha, dict):
        sha = file_or_sha["sha"]
        name = name or file_or_sha["name"]
    else:
        sha = file_or_sha
        name = name or sha
    from .util import safe_filename
    dest_dir.mkdir(parents=True, exist_ok=True)
    dst = dest_dir / safe_filename(name)
    shutil.copyfile(blob_path(sha), dst)
    return dst


def workspace_usage_bytes(workspace_id: str) -> int:
    """工作区存储用量：按工作区内引用的不同内容计算。"""
    r = db.one(
        "SELECT COALESCE(SUM(size),0) AS s FROM (SELECT DISTINCT sha, size FROM files "
        "WHERE workspace_id=? AND deleted_at IS NULL AND kind != 'preview')",
        (workspace_id,),
    )
    return int(r["s"]) if r else 0


def disk_usage() -> dict:
    s = get_settings()
    total, used, free = shutil.disk_usage(s.data_dir)
    return {"total": total, "used": used, "free": free, "percent": round(used * 100 / total, 1) if total else 0}
