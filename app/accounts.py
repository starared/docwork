"""管理员账号、临时令牌、会话、工作区与访问范围。"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import db, security
from .util import UserError, new_id, now, rand_token, sha256_hex

ALL_PERMS = ["ppt", "doc", "xls", "edit", "import", "convert", "pdf"]
PERM_LABELS = {
    "ppt": "PPT 生成", "doc": "Word 生成", "xls": "Excel 生成", "edit": "修改",
    "import": "导入", "convert": "格式转换", "pdf": "PDF 工具",
}
SESSION_TTL_OWNER = 7 * 86400
DEFAULT_RETENTION_DAYS = 7


@dataclass
class Scope:
    kind: str                      # owner | guest
    workspace_id: str
    session_id: str = ""
    token_id: str | None = None
    perms: list[str] = field(default_factory=lambda: list(ALL_PERMS))
    csrf: str = ""

    @property
    def is_owner(self) -> bool:
        return self.kind == "owner"

    @property
    def actor(self) -> str:
        return "owner" if self.is_owner else f"token:{self.token_id}"

    def require(self, perm: str) -> None:
        if not self.is_owner and perm not in self.perms:
            raise UserError(f"当前令牌没有“{PERM_LABELS.get(perm, perm)}”权限", 403, "forbidden")

    def require_owner(self) -> None:
        if not self.is_owner:
            raise UserError("仅管理员可用", 403, "forbidden")


# ---------- 工作区 ----------

def create_workspace(name: str, kind: str, conn=None) -> str:
    wid = new_id("w_")
    db.insert("workspaces", {"id": wid, "name": name, "kind": kind, "created_at": now()}, conn=conn)
    return wid


def owner_workspace_id() -> str | None:
    r = db.one("SELECT workspace_id FROM owner WHERE id=1")
    return r["workspace_id"] if r else None


# ---------- 管理员 ----------

def create_owner(username: str, password: str) -> None:
    if len(password) < 10:
        raise UserError("密码至少 10 位")
    conn = db.connect()
    with db.tx(conn):
        if db.one("SELECT id FROM owner WHERE id=1", conn=conn):
            raise UserError("管理员账号已存在；如需重置密码请使用 reset-password 命令")
        wid = create_workspace("管理员", "owner", conn=conn)
        db.insert(
            "owner",
            {"id": 1, "username": username, "pw_hash": security.hash_password(password), "workspace_id": wid, "created_at": now()},
            conn=conn,
        )


def reset_owner_password(password: str) -> None:
    if len(password) < 10:
        raise UserError("密码至少 10 位")
    db.run("UPDATE owner SET pw_hash=? WHERE id=1", (security.hash_password(password),))
    db.run("DELETE FROM sessions WHERE kind='owner'")


def owner_login(username: str, password: str, totp: str = "") -> str:
    o = db.one("SELECT * FROM owner WHERE id=1")
    if not o or o["username"] != username or not security.verify_password(password, o["pw_hash"]):
        raise UserError("用户名或密码错误", 401, "auth")
    if o["totp_enabled"]:
        if not totp:
            raise UserError("请输入两步验证码", 401, "totp_required")
        if not security.verify_totp(o["totp_secret"], totp):
            raise UserError("两步验证码错误", 401, "auth")
    return _new_session("owner", o["workspace_id"], None, now() + SESSION_TTL_OWNER)


# ---------- 会话 ----------

def _new_session(kind: str, workspace_id: str, token_id: str | None, expires_at: float, conn=None) -> str:
    cookie = rand_token(32)
    t = now()
    db.insert(
        "sessions",
        {
            "id": sha256_hex(cookie), "kind": kind, "token_id": token_id, "workspace_id": workspace_id,
            "csrf": rand_token(24), "created_at": t, "last_seen": t, "expires_at": expires_at,
        },
        conn=conn,
    )
    return cookie


def grant_valid(tok: dict, t: float | None = None) -> bool:
    t = t or now()
    return tok["revoked_at"] is None and tok["expires_at"] > t


def session_scope(cookie: str | None) -> Scope | None:
    """每次请求调用：会话必须存在且未过期；访客会话还要检查令牌授权状态（撤销、到期）。"""
    if not cookie:
        return None
    sid = sha256_hex(cookie)
    s = db.one("SELECT * FROM sessions WHERE id=?", (sid,))
    t = now()
    if not s or s["expires_at"] <= t:
        return None
    perms = list(ALL_PERMS)
    if s["kind"] == "guest":
        tok = db.one("SELECT * FROM tokens WHERE id=?", (s["token_id"],))
        # 只检查授权状态（撤销、到期），不检查兑换状态
        if not tok or not grant_valid(tok, t):
            return None
        perms = db.jload(tok["perms"], [])
    if t - s["last_seen"] > 60:
        db.run("UPDATE sessions SET last_seen=? WHERE id=?", (t, sid))
    return Scope(kind=s["kind"], workspace_id=s["workspace_id"], session_id=sid, token_id=s["token_id"], perms=perms, csrf=s["csrf"])


def logout(scope: Scope) -> None:
    db.run("DELETE FROM sessions WHERE id=?", (scope.session_id,))


# ---------- 令牌 ----------

def create_token(
    note: str,
    expires_at: float,
    *,
    one_time: bool = False,
    perms: list[str] | None = None,
    quota: dict | None = None,
    workspace_id: str | None = None,
) -> tuple[str, dict]:
    if expires_at <= now():
        raise UserError("有效期必须晚于当前时间")
    perms = [p for p in (perms if perms is not None else ALL_PERMS) if p in ALL_PERMS]
    quota = {k: v for k, v in (quota or {}).items() if v not in (None, "")}
    plain = security.new_access_token()
    conn = db.connect()
    with db.tx(conn):
        if workspace_id:
            ws = db.one("SELECT * FROM workspaces WHERE id=? AND deleted_at IS NULL", (workspace_id,), conn=conn)
            if not ws or ws["kind"] != "guest":
                raise UserError("只能绑定未删除的访客工作区")
        else:
            workspace_id = create_workspace(note or "访客", "guest", conn=conn)
        row = {
            "id": new_id("t_"), "token_hash": security.token_hash(plain), "note": note, "workspace_id": workspace_id,
            "expires_at": expires_at, "one_time": 1 if one_time else 0, "perms": perms, "quota": quota, "created_at": now(),
        }
        db.insert("tokens", row, conn=conn)
        recompute_retention(workspace_id, conn=conn)
    return plain, row


def redeem(plain: str) -> tuple[str, dict]:
    """兑换令牌建立会话。一次性令牌兑换后标记为已兑换，禁止再次兑换；已建立的会话在有效期内照常使用。"""
    h = security.token_hash(plain or "")
    conn = db.connect()
    with db.tx(conn):
        tok = db.one("SELECT * FROM tokens WHERE token_hash=?", (h,), conn=conn)
        if not tok or not grant_valid(tok):
            raise UserError("令牌无效、已过期或已被撤销", 401, "auth")
        if tok["one_time"] and tok["redeemed_at"] is not None:
            raise UserError("该一次性令牌已被使用", 401, "auth")
        if tok["redeemed_at"] is None:
            conn.execute("UPDATE tokens SET redeemed_at=? WHERE id=?", (now(), tok["id"]))
        cookie = _new_session("guest", tok["workspace_id"], tok["id"], tok["expires_at"], conn=conn)
    return cookie, tok


def revoke_token(token_id: str) -> None:

    conn = db.connect()
    with db.tx(conn):
        tok = db.one("SELECT * FROM tokens WHERE id=?", (token_id,), conn=conn)
        if not tok:
            raise UserError("令牌不存在", 404, "not_found")
        if tok["revoked_at"] is None:
            conn.execute("UPDATE tokens SET revoked_at=? WHERE id=?", (now(), token_id))
        conn.execute("DELETE FROM sessions WHERE token_id=?", (token_id,))
        recompute_retention(tok["workspace_id"], conn=conn)
    cancel_token_jobs(token_id, "令牌已撤销")


def job_grant_error(token_id: str | None) -> str | None:
    """后台任务执行前和执行中的授权检查。管理员任务（无令牌）不受限。
    令牌被撤销：立即停止。令牌到期：若同一工作区已续发了仍有效的令牌，则继续执行，否则停止。"""
    if not token_id:
        return None
    tok = db.one("SELECT * FROM tokens WHERE id=?", (token_id,))
    if not tok:
        return "访问令牌不存在"
    if tok["revoked_at"] is not None:
        return "访问令牌已撤销"
    t = now()
    if tok["expires_at"] > t:
        return None
    if db.one("SELECT 1 AS x FROM tokens WHERE workspace_id=? AND revoked_at IS NULL AND expires_at>?", (tok["workspace_id"], t)):
        return None
    return "访问令牌已到期"


def cancel_token_jobs(token_id: str, reason: str) -> int:
    from . import quota

    rows = db.all_(
        "SELECT id FROM jobs WHERE token_id=? AND parent_id IS NULL AND status IN ('queued','running','cancelling','awaiting_input')",
        (token_id,),
    )
    for j in rows:
        quota.cancel_job(j["id"], reason)
    return len(rows)


def token_state(tok: dict) -> dict:
    t = now()
    if tok["revoked_at"] is not None:
        grant = "revoked"
    elif tok["expires_at"] <= t:
        grant = "expired"
    else:
        grant = "active"
    return {"grant": grant, "redeem": "redeemed" if tok["redeemed_at"] else "unredeemed"}


def recompute_retention(workspace_id: str, conn=None) -> None:
    """工作区保留期按最后一个有效授权结束的时间计算；仍有有效授权时取消删除计划。"""
    conn = conn or db.connect()
    ws = conn.execute("SELECT kind FROM workspaces WHERE id=?", (workspace_id,)).fetchone()
    if not ws or ws["kind"] != "guest":
        return
    toks = [dict(r) for r in conn.execute("SELECT * FROM tokens WHERE workspace_id=?", (workspace_id,)).fetchall()]
    t = now()
    if not toks or any(grant_valid(k, t) for k in toks):
        conn.execute("UPDATE workspaces SET delete_after=NULL WHERE id=?", (workspace_id,))
        return
    last_end = max(min(k["expires_at"], k["revoked_at"] or k["expires_at"]) for k in toks)
    days = float(db.get_setting("workspace_retention_days", DEFAULT_RETENTION_DAYS))
    conn.execute("UPDATE workspaces SET delete_after=? WHERE id=?", (last_end + days * 86400, workspace_id))


# ---------- 限流 ----------

def rate_limit(key: str, limit: int, window: float = 600) -> None:
    t = now()
    conn = db.connect()
    with db.tx(conn):
        conn.execute("DELETE FROM rate WHERE ts < ?", (t - window,))
        n = conn.execute("SELECT COUNT(*) FROM rate WHERE key=? AND ts >= ?", (key, t - window)).fetchone()[0]
        if n >= limit:
            raise UserError("尝试次数过多，请稍后再试", 429, "rate_limited")
        conn.execute("INSERT INTO rate(key, ts) VALUES(?,?)", (key, t))


# ---------- 访问范围 ----------

def get_work_scoped(scope: Scope, work_id: str, write: bool = False, include_deleted: bool = False) -> dict:
    """数据访问层统一的作品访问检查：管理员可访问全部；访客只能访问本工作区作品和分享给它的只读作品。"""
    w = db.one("SELECT * FROM works WHERE id=?", (work_id,))
    if not w or (w["deleted_at"] and not include_deleted):
        raise UserError("作品不存在", 404, "not_found")
    if scope.is_owner or w["workspace_id"] == scope.workspace_id:
        return w
    if not write and db.one("SELECT 1 AS x FROM shares WHERE work_id=? AND workspace_id=?", (work_id, scope.workspace_id)):
        w["_readonly"] = True
        return w
    raise UserError("作品不存在", 404, "not_found")


def get_file_scoped(scope: Scope, file_id: str) -> dict:
    from .storage import get_file

    f = get_file(file_id)
    if not f:
        raise UserError("文件不存在", 404, "not_found")
    if f["expires_at"] and f["expires_at"] < now():
        raise UserError("文件已过期", 410, "expired")
    if scope.is_owner or f["workspace_id"] == scope.workspace_id:
        return f
    if f["work_id"] and db.one("SELECT 1 AS x FROM shares WHERE work_id=? AND workspace_id=?", (f["work_id"], scope.workspace_id)):
        return f
    raise UserError("文件不存在", 404, "not_found")


def get_job_scoped(scope: Scope, job_id: str, write: bool = False) -> dict:
    from . import jobs

    j = jobs.get(job_id)
    if not j:
        raise UserError("任务不存在", 404, "not_found")
    if scope.is_owner or j["workspace_id"] == scope.workspace_id:
        return j
    # 系统为作品重新生成历史预览的任务：能查看该作品的人（包括只读分享的访客）都可以查看进度
    if not write and j["kind"] == "version_preview" and j["work_id"] and not j["parent_id"]:
        get_work_scoped(scope, j["work_id"])
        return j
    raise UserError("任务不存在", 404, "not_found")


def audit(scope_or_actor, action: str, target: str = "", detail: dict | None = None, ip: str = "") -> None:
    actor = scope_or_actor.actor if isinstance(scope_or_actor, Scope) else str(scope_or_actor)
    db.insert("audit", {"ts": now(), "actor": actor, "action": action, "target": target, "detail": detail or {}, "ip": ip})
