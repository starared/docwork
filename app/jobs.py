"""基于数据库的任务队列。

- 任务状态持久化在 jobs 表中：queued → running → done / failed / cancelled，
  另有 awaiting_input（等待确认大纲）和 cancelling（已请求取消、正在停止）。
- 认领任务使用 BEGIN IMMEDIATE 事务，并在同一事务内检查全局重负载并发，
  因此不同 worker 容器之间不会超出并发上限。
- 取消使用持久化的 cancel_requested 标记，任务在各检查点读取。
- 子任务按 (parent_id, step) 唯一，父任务重试时复用已完成的子任务结果。
"""
from __future__ import annotations

import time
from typing import Any

from . import db
from .config import get_settings
from .util import UserError, dumps, new_id, now

# 任务类型 → 队列。ai 队列以外都是重负载队列，受全局并发限制。
KIND_QUEUE: dict[str, str] = {
    "gen_ppt": "ai",
    "gen_doc": "ai",
    "gen_xls": "ai",
    "edit": "ai",
    "import_edit": "ai",
    "rebuild": "ai",
    "model_test": "ai",
    "render": "render",
    "compat_pack": "render",
    "manual_edit": "ai",
    "convert": "convert",
    "pdf_tool": "convert",
    "import_file": "convert",
    "extract": "convert",
    "ocr": "ocr",
    "preview": "preview",
    "file_pages": "preview",
    "version_preview": "preview",
}
HEAVY_QUEUES = {"render", "convert", "ocr", "preview"}
# 修改同一作品的任务需串行执行
SERIAL_KINDS = ("edit", "import_edit", "manual_edit", "restore", "rebuild")
ACTIVE = ("queued", "running", "cancelling", "awaiting_input")
FINAL = ("done", "failed", "cancelled")


class Cancelled(Exception):
    pass


def _decode(j: dict | None) -> dict | None:
    if j is None:
        return None
    j["params"] = db.jload(j.get("params"), {})
    j["result"] = db.jload(j.get("result"), None)
    return j


def get(job_id: str) -> dict | None:
    return _decode(db.one("SELECT * FROM jobs WHERE id=?", (job_id,)))


def enqueue(
    kind: str,
    workspace_id: str,
    params: dict | None = None,
    *,
    token_id: str | None = None,
    work_id: str | None = None,
    title: str = "",
    parent_id: str | None = None,
    step: str | None = None,
    queue: str | None = None,
    max_attempts: int = 2,
    job_id: str | None = None,
    conn=None,
) -> dict:
    queue = queue or KIND_QUEUE[kind]
    conn = conn or db.connect()
    with db.tx(conn):
        if parent_id and step:
            ex = db.one("SELECT * FROM jobs WHERE parent_id=? AND step=?", (parent_id, step), conn=conn)
            if ex:
                return _decode(ex)
        t = now()
        row = {
            "id": job_id or new_id("j_"),
            "parent_id": parent_id,
            "step": step,
            "kind": kind,
            "queue": queue,
            "heavy": 1 if queue in HEAVY_QUEUES else 0,
            "workspace_id": workspace_id,
            "token_id": token_id,
            "work_id": work_id,
            "title": title,
            "params": params or {},
            "status": "queued",
            "max_attempts": max_attempts,
            "created_at": t,
            "updated_at": t,
        }
        db.insert("jobs", row, conn=conn)
    return get(row["id"])


def claim(queues: list[str], worker: str, pid: int = 0) -> dict | None:
    """认领一个排队任务。重负载任务受全局并发上限约束。"""
    s = get_settings()
    conn = db.connect()
    qmarks = ",".join("?" for _ in queues)
    serial = ",".join(f"'{k}'" for k in SERIAL_KINDS)
    with db.tx(conn):
        heavy_running = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE heavy=1 AND status IN ('running','cancelling')"
        ).fetchone()[0]
        ai_running = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE heavy=0 AND status IN ('running','cancelling')"
        ).fetchone()[0]
        allow_heavy = heavy_running < s.heavy_limit
        allow_ai = ai_running < s.ai_limit
        allowed = [q for q in queues if (q in HEAVY_QUEUES and allow_heavy) or (q not in HEAVY_QUEUES and allow_ai)]
        if not allowed:
            return None
        qmarks = ",".join("?" for _ in allowed)
        row = conn.execute(
            f"""
            SELECT * FROM jobs j
            WHERE j.status='queued' AND j.queue IN ({qmarks})
              AND (j.work_id IS NULL OR j.kind NOT IN ({serial}) OR NOT EXISTS (
                    SELECT 1 FROM jobs r WHERE r.work_id=j.work_id AND r.id != j.id
                      AND r.status IN ('running','cancelling') AND r.kind IN ({serial})))
            ORDER BY (j.parent_id IS NULL) ASC, j.created_at ASC
            LIMIT 1
            """,
            allowed,
        ).fetchone()
        if not row:
            return None
        t = now()
        conn.execute(
            "UPDATE jobs SET status='running', attempts=attempts+1, worker=?, pid=?, heavy=?, "
            "started_at=COALESCE(started_at, ?), heartbeat_at=?, updated_at=? WHERE id=?",
            (worker, pid, 1 if row["queue"] in HEAVY_QUEUES else 0, t, t, t, row["id"]),
        )
    return get(row["id"])


# heavy 列：0 = 普通任务（计入 AI 并发），1 = 重负载任务（计入全局重负载并发），
# 2 = 重负载任务正在等待子任务，暂时让出名额（两种并发都不计入）。
# 重负载父任务（例如导入）等待自己的预览子任务时如果不让出名额，并发上限为 1 时必然死锁，
# 上限为 2 时两个导入同时运行也会互相卡住。

def release_heavy(job_id: str) -> bool:
    return db.run("UPDATE jobs SET heavy=2 WHERE id=? AND heavy=1", (job_id,)) > 0


def reacquire_heavy(job_id: str, check=None, poll: float = 0.5) -> None:
    """子任务结束后重新取得重负载名额（与认领任务使用同一个事务检查）。"""
    s = get_settings()
    while True:
        conn = db.connect()
        with db.tx(conn):
            n = conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE heavy=1 AND status IN ('running','cancelling') AND id != ?", (job_id,)
            ).fetchone()[0]
            if n < s.heavy_limit:
                conn.execute("UPDATE jobs SET heavy=1 WHERE id=? AND heavy=2", (job_id,))
                return
        heartbeat(job_id)
        if check:
            check()
        time.sleep(poll)


def heartbeat(job_id: str) -> None:
    t = now()
    db.run("UPDATE jobs SET heartbeat_at=? WHERE id=? AND status IN ('running','cancelling')", (t, job_id))


def progress(job_id: str, progress: float | None = None, stage: str | None = None, message: str | None = None) -> None:
    sets, args = ["heartbeat_at=?", "updated_at=?"], [now(), now()]
    if progress is not None:
        sets.append("progress=?")
        args.append(max(0.0, min(1.0, progress)))
    if stage is not None:
        sets.append("stage=?")
        args.append(stage)
    if message is not None:
        sets.append("message=?")
        args.append(message[:500])
    db.run(f"UPDATE jobs SET {', '.join(sets)} WHERE id=?", (*args, job_id))


def set_stream(job_id: str, text: str) -> None:
    db.run("UPDATE jobs SET stream=?, updated_at=?, heartbeat_at=? WHERE id=?", (text[-6000:], now(), now(), job_id))


def cancel_requested(job_id: str) -> bool:
    r = db.one("SELECT cancel_requested FROM jobs WHERE id=?", (job_id,))
    return bool(r and r["cancel_requested"])


def request_cancel(job_id: str, reason: str = "") -> None:
    """持久化取消：排队中的直接取消，运行中的标记为 cancelling，由任务在检查点停止。子任务一并取消。"""
    conn = db.connect()
    with db.tx(conn):
        ids = [job_id]
        i = 0
        while i < len(ids):
            for r in conn.execute("SELECT id FROM jobs WHERE parent_id=?", (ids[i],)).fetchall():
                ids.append(r["id"])
            i += 1
        t = now()
        for jid in ids:
            conn.execute(
                "UPDATE jobs SET cancel_requested=1, updated_at=?, "
                "status=CASE WHEN status IN ('queued','awaiting_input') THEN 'cancelled' "
                "            WHEN status='running' THEN 'cancelling' ELSE status END, "
                "finished_at=CASE WHEN status IN ('queued','awaiting_input') THEN ? ELSE finished_at END, "
                "message=CASE WHEN ?!='' THEN ? ELSE message END "
                "WHERE id=?",
                (t, t, reason, reason, jid),
            )


def finish(job_id: str, result: Any = None) -> None:
    t = now()
    db.run(
        "UPDATE jobs SET status='done', progress=1, result=?, finished_at=?, updated_at=? WHERE id=? AND status IN ('running','cancelling')",
        (dumps(result) if result is not None else None, t, t, job_id),
    )


def fail(job_id: str, error: str, retry: bool = False) -> None:
    t = now()
    j = db.one("SELECT attempts, max_attempts FROM jobs WHERE id=?", (job_id,))
    if retry and j and j["attempts"] < j["max_attempts"]:
        db.run("UPDATE jobs SET status='queued', error=?, updated_at=? WHERE id=? AND status='running'", (error[:4000], t, job_id))
        return
    db.run(
        "UPDATE jobs SET status='failed', error=?, finished_at=?, updated_at=? WHERE id=? AND status IN ('running','cancelling','queued')",
        (error[:4000], t, t, job_id),
    )


def mark_cancelled(job_id: str) -> None:
    t = now()
    db.run(
        "UPDATE jobs SET status='cancelled', finished_at=?, updated_at=? WHERE id=? AND status IN ('running','cancelling','queued')",
        (t, t, job_id),
    )


def set_awaiting(job_id: str, result: Any, message: str) -> None:
    db.run(
        "UPDATE jobs SET status='awaiting_input', result=?, message=?, updated_at=? WHERE id=?",
        (dumps(result), message, now(), job_id),
    )


def resume(job_id: str, params_update: dict) -> dict:
    conn = db.connect()
    with db.tx(conn):
        j = _decode(db.one("SELECT * FROM jobs WHERE id=?", (job_id,), conn=conn))
        if not j or j["status"] != "awaiting_input":
            raise UserError("该任务不在等待确认状态")
        p = j["params"]
        p.update(params_update)
        conn.execute(
            "UPDATE jobs SET status='queued', params=?, updated_at=?, attempts=0 WHERE id=?",
            (dumps(p), now(), job_id),
        )
    return get(job_id)


def retry(job_id: str) -> dict:
    j = get(job_id)
    if not j or j["status"] not in ("failed", "cancelled"):
        raise UserError("只有失败或已取消的任务可以重试")
    # 重试建立新任务，保留原参数；幂等键不同，因此会产生新的结果
    return enqueue(
        j["kind"], j["workspace_id"], j["params"], token_id=j["token_id"], work_id=j["work_id"], title=j["title"]
    )


def requeue_stale(stale_seconds: float = 120) -> int:
    """worker 异常退出后，心跳超时的任务重新排队或判定失败。"""
    t = now()
    n = 0
    conn = db.connect()
    with db.tx(conn):
        rows = conn.execute(
            "SELECT id, attempts, max_attempts, cancel_requested FROM jobs WHERE status IN ('running','cancelling') AND heartbeat_at < ?",
            (t - stale_seconds,),
        ).fetchall()
        for r in rows:
            if r["cancel_requested"]:
                conn.execute("UPDATE jobs SET status='cancelled', finished_at=?, updated_at=? WHERE id=?", (t, t, r["id"]))
            elif r["attempts"] < r["max_attempts"]:
                conn.execute("UPDATE jobs SET status='queued', message='worker 中断，重新排队', updated_at=? WHERE id=?", (t, r["id"]))
            else:
                conn.execute(
                    "UPDATE jobs SET status='failed', error='worker 中断且已达到最大重试次数', finished_at=?, updated_at=? WHERE id=?",
                    (t, t, r["id"]),
                )
            n += 1
    return n


def wait(job_id: str, parent_id: str | None = None, timeout: float = 3600, poll: float = 0.5) -> dict:
    """等待子任务结束。父任务被取消时同时取消子任务。"""
    deadline = time.time() + timeout
    while True:
        j = get(job_id)
        if j is None:
            raise RuntimeError("子任务不存在")
        if j["status"] == "done":
            return j
        if j["status"] == "failed":
            raise RuntimeError(j["error"] or "子任务失败")
        if j["status"] == "cancelled":
            raise Cancelled()
        if parent_id:
            if cancel_requested(parent_id):
                request_cancel(job_id)
                raise Cancelled()
            heartbeat(parent_id)
        if time.time() > deadline:
            request_cancel(job_id, "超时")
            raise RuntimeError("子任务超时")
        time.sleep(poll)


def public(j: dict) -> dict:
    """返回给前端的任务信息。"""
    keys = ("id", "kind", "title", "status", "progress", "stage", "message", "error", "work_id",
            "created_at", "started_at", "finished_at", "updated_at", "parent_id", "attempts", "queue")
    out = {k: j.get(k) for k in keys}
    out["result"] = j.get("result")
    out["stream"] = j.get("stream") or ""
    if out["error"] and len(out["error"]) > 1500:
        out["error"] = out["error"][:1500] + "…"
    return out
