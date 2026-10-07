"""定时维护：任务心跳超时处理、临时文件与旧版本清理、工作区到期删除、磁盘告警、备份。

用法：python -m app.scheduler
"""
from __future__ import annotations

import logging
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path

from . import accounts, db, jobs, quota, storage, works
from .config import get_settings
from .util import now

log = logging.getLogger("docwork.scheduler")

DEFAULTS = {
    "tool_output_hours": 24,
    "keep_versions": 30,
    "trash_days": 30,
    "workspace_retention_days": 7,
    "preview_days": 30,
    "orphan_tmp_hours": 24,
    "disk_warn_percent": 80,
    "disk_block_percent": 90,
}


def setting(k):
    return db.get_setting(k, DEFAULTS[k])


def requeue_stale() -> int:
    n = jobs.requeue_stale(120)
    quota.settle_finished()
    return n


def expire_files() -> int:
    """过期的工具结果和上传的临时文件：删除记录（内容由垃圾回收处理）。"""
    return db.run("UPDATE files SET deleted_at=? WHERE expires_at IS NOT NULL AND expires_at < ? AND deleted_at IS NULL", (now(), now()))


def prune_versions() -> int:
    """每个作品只保留最近 N 个未加星标的版本（当前版本和星标版本始终保留）。"""
    keep = int(setting("keep_versions"))
    n = 0
    for w in db.all_("SELECT id, current_version_id FROM works"):
        vs = db.all_("SELECT id, starred FROM versions WHERE work_id=? ORDER BY number DESC", (w["id"],))
        unstarred = [v for v in vs if not v["starred"] and v["id"] != w["current_version_id"]]
        for v in unstarred[max(0, keep - 1):]:
            db.run("DELETE FROM files WHERE version_id=?", (v["id"],))
            db.run("DELETE FROM versions WHERE id=?", (v["id"],))
            n += 1
    return n


def purge_trash() -> int:
    cutoff = now() - float(setting("trash_days")) * 86400
    rows = db.all_("SELECT id FROM works WHERE deleted_at IS NOT NULL AND deleted_at < ?", (cutoff,))
    for r in rows:
        delete_work_hard(r["id"])
    return len(rows)


def delete_work_hard(work_id: str) -> None:
    for j in db.all_("SELECT id FROM jobs WHERE work_id=? AND parent_id IS NULL AND status IN ('queued','running','cancelling','awaiting_input')", (work_id,)):
        quota.cancel_job(j["id"], "作品已删除")
    conn = db.connect()
    with db.tx(conn):
        conn.execute("DELETE FROM files WHERE work_id=?", (work_id,))
        conn.execute("DELETE FROM versions WHERE work_id=?", (work_id,))
        conn.execute("DELETE FROM shares WHERE work_id=?", (work_id,))
        conn.execute("DELETE FROM works_fts WHERE work_id=?", (work_id,))
        conn.execute("DELETE FROM works WHERE id=?", (work_id,))


def expire_token_jobs() -> int:
    """令牌到期或撤销后，停止其排队中、等待确认和运行中的任务（续发了新令牌的工作区除外）。"""
    n = 0
    for r in db.all_("SELECT DISTINCT token_id FROM jobs WHERE token_id IS NOT NULL AND parent_id IS NULL "
                     "AND status IN ('queued','running','awaiting_input')"):
        err = accounts.job_grant_error(r["token_id"])
        if err:
            n += accounts.cancel_token_jobs(r["token_id"], err + "，任务已停止")
    return n


def expire_workspaces() -> int:
    """到期令牌：重新计算其工作区的保留期；超过保留期的访客工作区整体删除。"""
    t = now()
    for ws in db.all_("SELECT DISTINCT workspace_id FROM tokens WHERE expires_at < ? OR revoked_at IS NOT NULL", (t,)):
        with db.tx():
            accounts.recompute_retention(ws["workspace_id"])
    rows = db.all_("SELECT id FROM workspaces WHERE kind='guest' AND delete_after IS NOT NULL AND delete_after < ? AND deleted_at IS NULL", (t,))
    for r in rows:
        wid = r["id"]
        for w in db.all_("SELECT id FROM works WHERE workspace_id=?", (wid,)):
            delete_work_hard(w["id"])
        conn = db.connect()
        with db.tx(conn):
            conn.execute("DELETE FROM files WHERE workspace_id=?", (wid,))
            conn.execute("DELETE FROM shares WHERE workspace_id=?", (wid,))
            conn.execute("DELETE FROM jobs WHERE workspace_id=? AND status IN ('done','failed','cancelled')", (wid,))
            conn.execute("DELETE FROM sessions WHERE workspace_id=?", (wid,))
            conn.execute("UPDATE workspaces SET deleted_at=? WHERE id=?", (t, wid))
        accounts.audit("system", "workspace_deleted", wid)
    return len(rows)


def prune_previews() -> int:
    """旧版本（非当前、未加星标、超过保留天数）的页面预览图：删除文件记录，并从版本清单中去掉引用，
    这样垃圾回收才能真正释放空间。版本本身和导出文件保留；再次查看该版本时自动重新生成预览。"""
    cutoff = now() - float(setting("preview_days")) * 86400
    n = 0
    rows = db.all_(
        "SELECT v.id FROM versions v WHERE v.created_at < ? AND v.starred=0 "
        "AND v.id NOT IN (SELECT current_version_id FROM works WHERE current_version_id IS NOT NULL)",
        (cutoff,),
    )
    for r in rows:
        if works.drop_previews(r["id"]):
            n += 1
    return n


def _blob_refs(conn) -> set[str]:
    """所有仍被引用的内容哈希：文件记录、版本文件、版本清单中的导出、预览与素材、近期任务的参数与结果。"""
    refs: set[str] = set()
    for r in db.all_("SELECT DISTINCT sha FROM files WHERE deleted_at IS NULL", conn=conn):
        refs.add(r["sha"])
    for r in db.all_("SELECT file_sha, manifest FROM versions", conn=conn):
        if r["file_sha"]:
            refs.add(r["file_sha"])
        m = db.jload(r["manifest"], {})
        for info in (m.get("exports") or {}).values():
            if isinstance(info, dict) and info.get("sha"):
                refs.add(info["sha"])
        for p in m.get("pages") or []:
            if p.get("preview"):
                refs.add(p["preview"])
        for a in (m.get("assets") or {}).values():
            if isinstance(a, dict) and a.get("sha"):
                refs.add(a["sha"])
    for r in db.all_("SELECT params, result FROM jobs WHERE status NOT IN ('done','failed','cancelled') OR finished_at > ?",
                     (now() - 86400,), conn=conn):
        for txt in (r["params"] or "", r["result"] or ""):
            for tok in _shas(txt):
                refs.add(tok)
    return refs


def gc_blobs(grace_hours: float = 24) -> tuple[int, int]:
    """删除没有任何引用的内容（文件记录、版本文件、版本清单中的素材与预览缓存）。

    引用集合是一次性读出的快照。删除每个内容前在写事务里再确认一次：文件表中没有新引用，
    并且快照之后没有新建版本或任务（有则重新读取快照）。这样旧内容在快照与删除之间被新上传
    去重复用时不会被误删。"""
    conn = db.connect()
    t0 = now()
    refs = _blob_refs(conn)
    cutoff = now() - grace_hours * 3600
    removed = freed = 0
    for b in db.all_("SELECT sha, size, created_at FROM blobs WHERE created_at < ?", (cutoff,), conn=conn):
        if b["sha"] in refs:
            continue
        with db.tx(conn):
            if conn.execute("SELECT 1 FROM files WHERE sha=? AND deleted_at IS NULL LIMIT 1", (b["sha"],)).fetchone():
                continue
            changed = conn.execute(
                "SELECT 1 FROM versions WHERE created_at > ? UNION ALL SELECT 1 FROM jobs WHERE updated_at > ? LIMIT 1", (t0, t0)
            ).fetchone()
            if changed:
                t0 = now()
                refs = _blob_refs(conn)
                if b["sha"] in refs:
                    continue
            conn.execute("DELETE FROM blobs WHERE sha=?", (b["sha"],))
        p = storage.blob_path(b["sha"])
        try:
            p.unlink(missing_ok=True)
        except OSError:
            continue
        removed += 1
        freed += b["size"]
    return removed, freed


def _shas(text: str):
    import re
    return re.findall(r"[0-9a-f]{64}", text)


def clean_tmp() -> int:
    s = get_settings()
    cutoff = time.time() - float(setting("orphan_tmp_hours")) * 3600
    active = {f"job_{r['id']}" for r in db.all_("SELECT id FROM jobs WHERE status IN ('queued','running','cancelling','awaiting_input')")}
    n = 0
    for d in s.tmp_dir.glob("job_*"):
        if d.name in active:
            continue
        try:
            if d.stat().st_mtime < cutoff:
                shutil.rmtree(d, ignore_errors=True)
                n += 1
        except FileNotFoundError:
            pass
    # 未完成的分片上传：超过保留时间的记录和分片一起删除（否则会一直占用存储配额）
    from .quota import PENDING_UPLOAD_HOURS
    db.run("DELETE FROM uploads WHERE created_at < ?", (time.time() - PENDING_UPLOAD_HOURS * 3600,))
    live = {r["id"] for r in db.all_("SELECT id FROM uploads")}
    for d in s.upload_dir.glob("*"):
        if d.name in live:
            continue
        try:
            if d.stat().st_mtime < cutoff:
                shutil.rmtree(d, ignore_errors=True) if d.is_dir() else d.unlink()
        except FileNotFoundError:
            pass
    return n


def disk_check() -> dict:
    u = storage.disk_usage()
    state = "ok"
    if u["percent"] >= float(setting("disk_block_percent")):
        state = "block"
    elif u["percent"] >= float(setting("disk_warn_percent")):
        state = "warn"
    db.set_setting("disk_state", {"state": state, **u, "checked_at": now()})
    return u


def backup_db() -> Path:
    """SQLite 在线备份，保留最近 7 份。"""
    s = get_settings()
    dst = s.backup_dir / time.strftime("docwork-%Y%m%d-%H%M%S.sqlite3")
    src = sqlite3.connect(str(s.db_path))
    out = sqlite3.connect(str(dst))
    with out:
        src.backup(out)
    src.close()
    out.close()
    files = sorted(s.backup_dir.glob("docwork-*.sqlite3"))
    for f in files[:-7]:
        f.unlink(missing_ok=True)
    db.set_setting("last_db_backup", {"path": str(dst), "at": now()})
    return dst


def restic_backup() -> str:
    s = get_settings()
    if not s.restic_repository or not shutil.which("restic"):
        return "skipped"
    env = {"RESTIC_REPOSITORY": s.restic_repository, "RESTIC_PASSWORD": s.restic_password, "PATH": "/usr/bin:/bin:/usr/local/bin"}
    import os
    env.update({k: v for k, v in os.environ.items() if k.startswith(("AWS_", "B2_", "OS_", "RESTIC_"))})
    chk = subprocess.run(["restic", "cat", "config"], env=env, capture_output=True, timeout=120)
    if chk.returncode != 0:
        subprocess.run(["restic", "init"], env=env, capture_output=True, timeout=300)
    res = subprocess.run(["restic", "backup", str(s.backup_dir), str(s.blob_dir), "--tag", "docwork"], env=env, capture_output=True, text=True, timeout=6 * 3600)
    subprocess.run(["restic", "forget", "--keep-weekly", "8", "--keep-daily", "7", "--prune"], env=env, capture_output=True, timeout=6 * 3600)
    db.set_setting("last_restic", {"at": now(), "ok": res.returncode == 0, "tail": (res.stderr or res.stdout)[-500:]})
    return "ok" if res.returncode == 0 else "failed"


TASKS = [
    # (名称, 间隔秒, 函数)
    ("requeue_stale", 60, requeue_stale),
    ("expire_token_jobs", 60, expire_token_jobs),
    ("expire_files", 600, expire_files),
    ("disk_check", 600, disk_check),
    ("expire_workspaces", 1800, expire_workspaces),
    ("clean_tmp", 3600, clean_tmp),
    ("prune_versions", 6 * 3600, prune_versions),
    ("purge_trash", 6 * 3600, purge_trash),
    ("prune_previews", 12 * 3600, prune_previews),
    ("gc_blobs", 12 * 3600, gc_blobs),
    ("backup_db", 24 * 3600, backup_db),
    ("restic_backup", 7 * 24 * 3600, restic_backup),
]


def run_once(names: list[str] | None = None) -> dict:
    out = {}
    for name, _, fn in TASKS:
        if names and name not in names:
            continue
        try:
            out[name] = fn()
        except Exception as e:
            log.exception("维护任务 %s 失败", name)
            out[name] = f"error: {e}"
    return out


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    db.migrate()
    last = {name: db.get_setting(f"sched:{name}", 0) for name, _, _ in TASKS}
    log.info("维护进程启动")
    while True:
        t = time.time()
        for name, every, fn in TASKS:
            if t - float(last.get(name) or 0) >= every:
                try:
                    r = fn()
                    if r:
                        log.info("%s: %s", name, r)
                except Exception:
                    log.exception("维护任务 %s 失败", name)
                last[name] = t
                db.set_setting(f"sched:{name}", t)
        time.sleep(20)


if __name__ == "__main__":
    main()
