"""令牌配额：任务开始前原子预占，结束后按实际用量结算。

配额字段（均可省略，省略表示不限制）：
  gen_count     生成次数（生成、对话修改、重建各计 1 次）
  tokens        模型 token 总量
  upload_mb     单文件上传大小
  storage_mb    工作区总存储
  concurrent    同时运行的任务数
"""
from __future__ import annotations

from . import db
from .util import UserError, now

COUNTED_KINDS = {"gen_ppt", "gen_doc", "gen_xls", "edit", "import_edit", "rebuild"}


def token_quota(token_id: str | None) -> dict:
    if not token_id:
        return {}
    r = db.one("SELECT quota FROM tokens WHERE id=?", (token_id,))
    return db.jload(r["quota"], {}) if r else {}


def usage_summary(token_id: str, conn=None) -> dict:
    conn = conn or db.connect()
    r = conn.execute(
        """
        SELECT
          COALESCE(SUM(CASE WHEN status='settled' THEN COALESCE(actual_gen,0) ELSE gen_count END),0) AS gen,
          COALESCE(SUM(CASE WHEN status='settled' THEN COALESCE(actual_tokens,0) ELSE tokens END),0) AS tokens
        FROM reservations WHERE token_id=?
        """,
        (token_id,),
    ).fetchone()
    running = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE token_id=? AND parent_id IS NULL AND status IN ('queued','running','cancelling','awaiting_input')",
        (token_id,),
    ).fetchone()[0]
    return {"gen": int(r["gen"]), "tokens": int(r["tokens"]), "running": int(running)}


def reserve(job_id: str, token_id: str | None, kind: str, est_tokens: int, conn=None) -> None:
    """在调用方的事务内执行（调用方应使用 db.tx 包裹预占和任务创建）。"""
    if not token_id:
        return
    q = token_quota(token_id)
    conn = conn or db.connect()
    u = usage_summary(token_id, conn)
    gen = 1 if kind in COUNTED_KINDS else 0
    if q.get("concurrent") and u["running"] >= int(q["concurrent"]):
        raise UserError(f"同时运行的任务已达上限（{q['concurrent']} 个），请等待其他任务完成", 429, "quota")
    if gen and q.get("gen_count") is not None and u["gen"] + gen > int(q["gen_count"]):
        raise UserError(f"生成次数已用完（上限 {q['gen_count']} 次）", 429, "quota")
    if est_tokens and q.get("tokens") is not None and u["tokens"] + est_tokens > int(q["tokens"]):
        left = max(0, int(q["tokens"]) - u["tokens"])
        raise UserError(f"模型用量不足：本任务预计需要约 {est_tokens} token，剩余 {left} token", 429, "quota")
    conn.execute(
        "INSERT OR IGNORE INTO reservations(job_id, token_id, gen_count, tokens, status, created_at) VALUES(?,?,?,?, 'reserved', ?)",
        (job_id, token_id, gen, int(est_tokens), now()),
    )


def check_during(root_job_id: str, token_id: str | None, next_est: int) -> None:
    """任务运行中、每次调用模型前检查：本任务已用 + 其他任务占用 + 本次预估 不超过上限。"""
    if not token_id:
        return
    q = token_quota(token_id)
    if q.get("tokens") is None:
        return
    conn = db.connect()
    others = conn.execute(
        """SELECT COALESCE(SUM(CASE WHEN status='settled' THEN COALESCE(actual_tokens,0) ELSE tokens END),0)
           FROM reservations WHERE token_id=? AND job_id != ?""",
        (token_id, root_job_id),
    ).fetchone()[0]
    mine = conn.execute(
        "SELECT COALESCE(SUM(prompt_tokens+completion_tokens),0) FROM usage WHERE root_job_id=?", (root_job_id,)
    ).fetchone()[0]
    if others + mine + next_est > int(q["tokens"]):
        raise UserError("模型用量已达到令牌上限，任务已停止", 429, "quota")


def settle(job_id: str) -> None:
    """按实际用量结算，释放多占的额度。幂等：已结算的不再处理。"""
    conn = db.connect()
    with db.tx(conn):
        r = conn.execute("SELECT * FROM reservations WHERE job_id=? AND status='reserved'", (job_id,)).fetchone()
        if not r:
            return
        j = conn.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not j or j["status"] not in ("done", "failed", "cancelled"):
            return
        actual = conn.execute(
            "SELECT COALESCE(SUM(prompt_tokens+completion_tokens),0) FROM usage WHERE root_job_id=?", (job_id,)
        ).fetchone()[0]
        gen = r["gen_count"] if (j and j["status"] == "done") else 0
        conn.execute(
            "UPDATE reservations SET status='settled', actual_tokens=?, actual_gen=?, settled_at=? WHERE job_id=?",
            (int(actual), gen, now(), job_id),
        )


def settle_finished() -> int:
    """维护进程补偿 worker 崩溃、取消或结算失败留下的预占记录。"""
    rows = db.all_("SELECT r.job_id FROM reservations r JOIN jobs j ON j.id=r.job_id "
                   "WHERE r.status='reserved' AND j.status IN ('done','failed','cancelled')")
    for r in rows:
        settle(r["job_id"])
    db.run("UPDATE works SET busy_job_id=NULL WHERE busy_job_id IN "
           "(SELECT id FROM jobs WHERE status IN ('done','failed','cancelled'))")
    return len(rows)


PENDING_UPLOAD_HOURS = 24  # 未完成的上传在这段时间内占用配额；超时的由定时清理删除


def pending_upload_bytes(workspace_id: str, exclude: str | None = None) -> int:
    r = db.one("SELECT COALESCE(SUM(size),0) AS s FROM uploads WHERE workspace_id=? AND created_at>? AND id != ?",
               (workspace_id, now() - PENDING_UPLOAD_HOURS * 3600, exclude or ""))
    return int(r["s"]) if r else 0


def check_upload(token_id: str | None, workspace_id: str, size: int, *, pending: bool = True, exclude_upload: str | None = None) -> None:
    """单文件大小上限和工作区存储配额。pending=True 时把尚未完成的上传也计入已用量，
    否则同时开始的多个上传各自都能通过检查（调用方需在同一个写事务中检查并登记上传）。"""
    from .config import get_settings
    from .storage import workspace_usage_bytes

    s = get_settings()
    q = token_quota(token_id)
    # 后台调整的上限对所有人生效；令牌自己的上限只能在此基础上再收紧
    max_mb = int(db.get_setting("max_upload_mb", s.max_upload_mb))
    limit_mb = min(int(q.get("upload_mb") or max_mb), max_mb) if token_id else max_mb
    if size > limit_mb * 1024 * 1024:
        raise UserError(f"文件超过大小上限（{limit_mb} MB）", 413, "too_large")
    if q.get("storage_mb") is not None:
        used = workspace_usage_bytes(workspace_id)
        if pending:
            used += pending_upload_bytes(workspace_id, exclude_upload)
        if used + size > int(q["storage_mb"]) * 1024 * 1024:
            raise UserError(f"工作区存储已满（上限 {q['storage_mb']} MB，含正在上传的文件）", 413, "quota")


def cancel_job(job_id: str, reason: str) -> None:
    """取消任务。排队中、等待确认的任务不会再运行，立即结算释放预占额度；
    运行中的任务在检查点停止，由 worker 结束时结算（保证计入已经发生的用量）。"""
    from . import jobs

    j = db.one("SELECT status FROM jobs WHERE id=?", (job_id,))
    if not j or j["status"] in jobs.FINAL:
        return
    jobs.request_cancel(job_id, reason)
    if j["status"] in ("queued", "awaiting_input"):
        settle(job_id)
