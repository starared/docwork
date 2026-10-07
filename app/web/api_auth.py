"""登录、令牌兑换、当前身份、管理员两步验证与改密码。"""
from __future__ import annotations

from .. import accounts, db, models_cfg, security
from ..config import get_settings
from ..util import UserError, now
from .common import Req, api, set_session_cookies


@api(auth=None)
def me(req: Req):
    if req.scope is None:
        has_owner = db.one("SELECT id FROM owner WHERE id=1") is not None
        return {"authenticated": False, "owner_exists": has_owner}
    s = req.s
    out = {"authenticated": True, "kind": s.kind, "perms": s.perms, "workspace_id": s.workspace_id, "csrf": s.csrf,
           "perm_labels": accounts.PERM_LABELS, "models": models_cfg.status(),
           "disk": (db.get_setting("disk_state", {}) or {}).get("state", "ok")}
    if s.is_owner:
        o = db.one("SELECT username, totp_enabled FROM owner WHERE id=1")
        out["username"] = o["username"]
        out["totp_enabled"] = bool(o["totp_enabled"])
    else:
        tok = db.one("SELECT note, expires_at, quota FROM tokens WHERE id=?", (s.token_id,))
        from .. import quota
        out["note"] = tok["note"]
        out["expires_at"] = tok["expires_at"]
        out["quota"] = db.jload(tok["quota"], {})
        out["usage"] = quota.usage_summary(s.token_id)
        from ..storage import workspace_usage_bytes
        out["usage"]["storage_bytes"] = workspace_usage_bytes(s.workspace_id)
    return out


@api(auth=None, csrf=False)
def login(req: Req):
    s = get_settings()
    accounts.rate_limit(f"login:{req.ip}", s.login_rate_per_10min)
    try:
        cookie = accounts.owner_login(str(req.b("username", "")), str(req.b("password", "")), str(req.b("totp", "") or ""))
    except UserError as e:
        accounts.audit("anonymous", "login_failed", "", {"code": e.code}, req.ip)
        raise
    sc = accounts.session_scope(cookie)
    set_session_cookies(req, cookie, sc.csrf, accounts.SESSION_TTL_OWNER)
    accounts.audit("owner", "login", "", {}, req.ip)
    return {"ok": True}


@api(auth=None, csrf=False)
def redeem(req: Req):
    s = get_settings()
    accounts.rate_limit(f"redeem:{req.ip}", s.redeem_rate_per_10min)
    token = str(req.b("token", "")).strip()
    if not token:
        raise UserError("请输入访问令牌")
    cookie, tok = accounts.redeem(token)
    sc = accounts.session_scope(cookie)
    set_session_cookies(req, cookie, sc.csrf, max(60, int(tok["expires_at"] - now())))
    accounts.audit(f"token:{tok['id']}", "redeem", tok["id"], {}, req.ip)
    return {"ok": True}


@api(auth="any")
def logout(req: Req):
    accounts.logout(req.s)
    req.clear_cookies = True
    return {"ok": True}


@api(auth="owner")
def totp_setup(req: Req):
    o = db.one("SELECT username, totp_secret, totp_enabled FROM owner WHERE id=1")
    # 已开启时重新设置等同于先关闭：与关闭一样必须验证当前验证码，会话被盗用时不能无声地换掉密钥
    if o["totp_enabled"] and not security.verify_totp(o["totp_secret"], str(req.b("code", "") or "")):
        raise UserError("已开启两步验证，重新设置前请输入当前验证码")
    secret = security.new_totp_secret()
    db.run("UPDATE owner SET totp_secret=?, totp_enabled=0 WHERE id=1", (secret,))
    if o["totp_enabled"]:
        accounts.audit(req.s, "totp_disabled", "", {"reason": "reset"}, req.ip)
    return {"secret": secret, "uri": security.totp_uri(secret, o["username"])}


@api(auth="owner")
def totp_enable(req: Req):
    o = db.one("SELECT totp_secret FROM owner WHERE id=1")
    if not o["totp_secret"] or not security.verify_totp(o["totp_secret"], str(req.b("code", ""))):
        raise UserError("验证码不正确")
    db.run("UPDATE owner SET totp_enabled=1 WHERE id=1")
    accounts.audit(req.s, "totp_enabled", "", {}, req.ip)
    return {"ok": True}


@api(auth="owner")
def totp_disable(req: Req):
    o = db.one("SELECT totp_secret, totp_enabled FROM owner WHERE id=1")
    if o["totp_enabled"] and not security.verify_totp(o["totp_secret"], str(req.b("code", ""))):
        raise UserError("验证码不正确")
    db.run("UPDATE owner SET totp_enabled=0, totp_secret=NULL WHERE id=1")
    accounts.audit(req.s, "totp_disabled", "", {}, req.ip)
    return {"ok": True}


@api(auth="owner")
def change_password(req: Req):
    o = db.one("SELECT pw_hash FROM owner WHERE id=1")
    if not security.verify_password(str(req.b("old", "")), o["pw_hash"]):
        raise UserError("原密码不正确")
    new = str(req.b("new", ""))
    if len(new) < 10:
        raise UserError("新密码至少 10 位")
    db.run("UPDATE owner SET pw_hash=? WHERE id=1", (security.hash_password(new),))
    db.run("DELETE FROM sessions WHERE kind='owner' AND id != ?", (req.s.session_id,))
    accounts.audit(req.s, "password_changed", "", {}, req.ip)
    return {"ok": True}
