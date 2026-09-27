"""管理员后台：令牌、工作区、模型接口、设置、用量、系统状态、审计、字体。"""
from __future__ import annotations

import os
import time
from pathlib import Path

import psutil

from .. import accounts, db, jobs, models_cfg, quota, storage
from ..config import get_settings
from ..util import UserError, now, safe_filename
from .common import Req, api, page_args
from .api_jobs import create_job

SETTING_KEYS = {
    "max_upload_mb": (int, 1, 2048), "max_unzipped_mb": (int, 10, 8192), "max_pages": (int, 1, 5000),
    "max_pixels": (int, 1_000_000, 500_000_000), "tool_output_hours": (float, 1, 24 * 30), "keep_versions": (int, 3, 500),
    "trash_days": (float, 1, 365), "workspace_retention_days": (float, 0, 365), "preview_days": (float, 1, 365),
    "disk_warn_percent": (float, 50, 99), "disk_block_percent": (float, 50, 99),
}


def _tok_pub(t: dict) -> dict:
    st = accounts.token_state(t)
    ws = db.one("SELECT name, delete_after, deleted_at FROM workspaces WHERE id=?", (t["workspace_id"],)) or {}
    u = quota.usage_summary(t["id"])
    return {"id": t["id"], "note": t["note"], "workspace_id": t["workspace_id"], "workspace": ws.get("name"),
            "workspace_delete_after": ws.get("delete_after"), "expires_at": t["expires_at"], "one_time": bool(t["one_time"]),
            "redeemed_at": t["redeemed_at"], "revoked_at": t["revoked_at"], "perms": db.jload(t["perms"], []),
            "quota": db.jload(t["quota"], {}), "created_at": t["created_at"], **st, "usage": u}


@api(auth="owner")
def tokens_list(req: Req):
    rows = db.all_("SELECT * FROM tokens ORDER BY created_at DESC")
    return {"items": [_tok_pub(t) for t in rows]}


QUOTA_KEYS = ("gen_count", "tokens", "upload_mb", "storage_mb", "concurrent")


def _parse_quota(quota_in) -> dict:
    """创建和修改令牌共用的配额校验：只接受已知字段，必须是非负整数。"""
    if quota_in in (None, ""):
        return {}
    if not isinstance(quota_in, dict):
        raise UserError("配额格式不正确")
    unknown = [k for k in quota_in if k not in QUOTA_KEYS]
    if unknown:
        raise UserError(f"未知的配额字段：{'、'.join(map(str, unknown))}")
    q = {}
    for k in QUOTA_KEYS:
        v = quota_in.get(k)
        if v in (None, ""):
            continue
        if isinstance(v, bool) or not (isinstance(v, int) or (isinstance(v, str) and v.strip().isdigit())) or int(v) < 0:
            raise UserError(f"配额 {k} 必须是非负整数")
        q[k] = int(v)
    return q


def _parse_expires(b: dict) -> float:
    """有效期：expires_at（时间戳）或 hours（小时数），必须晚于当前时间，最长 1 年。"""
    try:
        if b.get("expires_at") not in (None, ""):
            t = float(b["expires_at"])
        else:
            t = now() + float(b.get("hours") or 24) * 3600
    except (TypeError, ValueError):
        raise UserError("有效期格式不正确")
    if t != t or t <= now():  # NaN 或已过去
        raise UserError("有效期必须晚于当前时间")
    if t > now() + 366 * 86400:
        raise UserError("有效期最长 1 年")
    return t


@api(auth="owner")
def token_create(req: Req):
    b = req.body or {}
    expires_at = _parse_expires(b)
    q = _parse_quota(b.get("quota"))
    plain, row = accounts.create_token(str(b.get("note", ""))[:60], expires_at, one_time=bool(b.get("one_time")),
                                       perms=b.get("perms"), quota=q, workspace_id=b.get("workspace_id") or None)
    accounts.audit(req.s, "token_created", row["id"], {"note": row["note"]}, req.ip)
    base = get_settings().public_url.rstrip("/")
    return {"token": plain, "link": f"{base}/#redeem={plain}", "item": _tok_pub(db.one("SELECT * FROM tokens WHERE id=?", (row["id"],)))}


@api(auth="owner")
def token_revoke(req: Req):
    accounts.revoke_token(req.path["tid"])
    accounts.audit(req.s, "token_revoked", req.path["tid"], {}, req.ip)
    return {"ok": True}


@api(auth="owner")
def token_update(req: Req):
    t = db.one("SELECT * FROM tokens WHERE id=?", (req.path["tid"],))
    if not t:
        raise UserError("令牌不存在", 404)
    if t["revoked_at"] is not None:
        raise UserError("令牌已撤销，不能修改；请为同一工作区续发新令牌")
    b = req.body or {}
    upd = {}
    if b.get("note") is not None:
        upd["note"] = str(b["note"])[:60]
    if b.get("expires_at") is not None or b.get("hours") is not None:
        upd["expires_at"] = _parse_expires(b)
    if b.get("perms") is not None:
        if not isinstance(b["perms"], list):
            raise UserError("权限格式不正确")
        upd["perms"] = [p for p in b["perms"] if p in accounts.ALL_PERMS]
    if b.get("quota") is not None:
        upd["quota"] = _parse_quota(b["quota"])
    if upd:
        with db.tx() as conn:
            db.update("tokens", {"id": t["id"]}, upd, conn=conn)
            if "expires_at" in upd:
                # 已登录的访客会话跟随令牌有效期（延长或缩短），否则延期后访客仍会在旧时间被踢出
                conn.execute("UPDATE sessions SET expires_at=? WHERE token_id=?", (upd["expires_at"], t["id"]))
            accounts.recompute_retention(t["workspace_id"], conn=conn)
        accounts.audit(req.s, "token_updated", t["id"], {k: v for k, v in upd.items() if k != "note"}, req.ip)
    return _tok_pub(db.one("SELECT * FROM tokens WHERE id=?", (t["id"],)))


@api(auth="owner")
def workspaces_list(req: Req):
    rows = db.all_("SELECT * FROM workspaces WHERE deleted_at IS NULL ORDER BY created_at DESC")
    out = []
    for w in rows:
        n = db.one("SELECT COUNT(*) AS n FROM works WHERE workspace_id=? AND deleted_at IS NULL", (w["id"],))["n"]
        out.append({**w, "works": n, "storage_bytes": storage.workspace_usage_bytes(w["id"]),
                    "tokens": db.one("SELECT COUNT(*) AS n FROM tokens WHERE workspace_id=?", (w["id"],))["n"]})
    return {"items": out}


@api(auth="owner")
def workspace_delete(req: Req):
    """立即删除访客工作区：撤销其全部令牌，删除全部作品和文件。"""
    wid = req.path["ws"]
    ws = db.one("SELECT * FROM workspaces WHERE id=?", (wid,))
    if not ws or ws["kind"] != "guest":
        raise UserError("只能删除访客工作区")
    for t in db.all_("SELECT id FROM tokens WHERE workspace_id=? AND revoked_at IS NULL", (wid,)):
        accounts.revoke_token(t["id"])
    expire_workspaces_now(wid)
    accounts.audit(req.s, "workspace_deleted", wid, {}, req.ip)
    return {"ok": True}


def expire_workspaces_now(wid: str) -> None:
    from ..scheduler import delete_work_hard
    for w in db.all_("SELECT id FROM works WHERE workspace_id=?", (wid,)):
        delete_work_hard(w["id"])
    conn = db.connect()
    with db.tx(conn):
        conn.execute("DELETE FROM files WHERE workspace_id=?", (wid,))
        conn.execute("DELETE FROM shares WHERE workspace_id=?", (wid,))
        conn.execute("DELETE FROM sessions WHERE workspace_id=?", (wid,))
        conn.execute("UPDATE workspaces SET deleted_at=? WHERE id=?", (now(), wid))


# ---------- 模型接口 ----------

@api(auth="owner")
def endpoints_list(req: Req):
    return {"items": models_cfg.list_endpoints(), "models": models_cfg.list_models(), "roles": models_cfg.get_roles(),
            "status": models_cfg.status(), "kinds": models_cfg.KINDS, "role_labels": models_cfg.MODEL_ROLES}


@api(auth="owner")
def endpoint_save(req: Req):
    eid = models_cfg.save_endpoint(req.body or {}, req.path.get("eid"))
    accounts.audit(req.s, "endpoint_saved", eid, {"kind": (req.body or {}).get("kind")}, req.ip)
    return {"id": eid}


@api(auth="owner")
def endpoint_delete(req: Req):
    models_cfg.delete_endpoint(req.path["eid"])
    return {"ok": True}


@api(auth="owner")
def endpoint_test(req: Req):
    if not models_cfg.get_endpoint(req.path["eid"], with_key=False):
        raise UserError("接口不存在", 404)
    return create_job(req.s, "model_test", {"endpoint_id": req.path["eid"]}, None, req.ip)


@api(auth="owner")
def endpoint_fetch_models(req: Req):
    """拉取接口的模型列表。由 worker-ai 执行：只有它接入了模型接口所在的网络。"""
    ep = models_cfg.get_endpoint(req.path["eid"], with_key=False)
    if not ep:
        raise UserError("接口不存在", 404)
    if ep["kind"] != "openai":
        raise UserError("图库接口没有模型列表")
    return create_job(req.s, "model_test", {"endpoint_id": ep["id"], "what": "list"}, None, req.ip)


@api(auth="owner")
def endpoint_add_models(req: Req):
    names = (req.body or {}).get("models") or []
    if not isinstance(names, list) or not names:
        raise UserError("请选择或填写模型")
    ids = models_cfg.add_models(req.path["eid"], names[:200])
    accounts.audit(req.s, "models_added", req.path["eid"], {"models": [str(n)[:80] for n in names[:20]]}, req.ip)
    return {"ids": ids}


@api(auth="owner")
def model_update(req: Req):
    models_cfg.update_model(req.path["mid"], req.body or {})
    return {"ok": True}


@api(auth="owner")
def model_delete(req: Req):
    models_cfg.delete_model(req.path["mid"])
    return {"ok": True}


@api(auth="owner")
def model_test(req: Req):
    if not models_cfg.get_model(req.path["mid"], with_key=False):
        raise UserError("模型不存在", 404)
    what = "image" if (req.body or {}).get("what") == "image" else "chat"
    return create_job(req.s, "model_test", {"model_id": req.path["mid"], "what": what}, None, req.ip)


@api(auth="owner")
def roles_set(req: Req):
    models_cfg.set_roles(req.body or {})
    return {"roles": models_cfg.get_roles()}


# ---------- 设置 ----------

@api(auth="owner")
def settings_get(req: Req):
    from ..scheduler import DEFAULTS
    s = get_settings()
    out = {}
    for k in SETTING_KEYS:
        out[k] = db.get_setting(k, DEFAULTS.get(k, getattr(s, k, None)))
    return {"settings": out, "profile": s.profile, "public_url": s.public_url}


@api(auth="owner")
def settings_put(req: Req):
    for k, v in (req.body or {}).items():
        if k not in SETTING_KEYS:
            continue
        typ, lo, hi = SETTING_KEYS[k]
        try:
            val = typ(v)
        except (TypeError, ValueError):
            raise UserError(f"{k} 的值无效")
        if not lo <= val <= hi:
            raise UserError(f"{k} 应在 {lo} 到 {hi} 之间")
        db.set_setting(k, val)
    return settings_get.__wrapped__(req)


# ---------- 用量 ----------

@api(auth="owner")
def usage(req: Req):
    days = max(1, min(365, int(req.q("days", 30))))
    since = now() - days * 86400
    by_token = db.all_(
        """SELECT u.token_id, t.note, SUM(u.prompt_tokens) AS prompt, SUM(u.completion_tokens) AS completion,
                  SUM(u.images) AS images, SUM(u.cost) AS cost, COUNT(DISTINCT u.root_job_id) AS jobs
           FROM usage u LEFT JOIN tokens t ON t.id=u.token_id WHERE u.created_at >= ? GROUP BY u.token_id ORDER BY cost DESC""", (since,))
    by_day = db.all_(
        """SELECT date(created_at, 'unixepoch', 'localtime') AS day, SUM(prompt_tokens) AS prompt, SUM(completion_tokens) AS completion,
                  SUM(cost) AS cost FROM usage WHERE created_at >= ? GROUP BY day ORDER BY day""", (since,))
    by_kind = db.all_(
        """SELECT j.kind, SUM(u.prompt_tokens + u.completion_tokens) AS tokens, SUM(u.cost) AS cost, COUNT(DISTINCT u.root_job_id) AS jobs
           FROM usage u JOIN jobs j ON j.id=u.root_job_id WHERE u.created_at >= ? GROUP BY j.kind ORDER BY tokens DESC""", (since,))
    by_model = db.all_(
        """SELECT u.model, u.role, SUM(u.prompt_tokens) AS prompt, SUM(u.completion_tokens) AS completion, SUM(u.cost) AS cost,
                  SUM(u.estimated) AS estimated_calls, COUNT(*) AS calls
           FROM usage u WHERE u.created_at >= ? GROUP BY u.model, u.role ORDER BY cost DESC""", (since,))
    for r in by_token:
        if r["token_id"] is None:
            r["note"] = "管理员"
    return {"by_token": by_token, "by_day": by_day, "by_kind": by_kind, "by_model": by_model, "days": days}


# ---------- 系统 ----------

@api(auth="owner")
def system(req: Req):
    s = get_settings()
    vm = psutil.virtual_memory()
    queues = db.all_("SELECT queue, status, COUNT(*) AS n FROM jobs WHERE status IN ('queued','running','cancelling') GROUP BY queue, status")
    failed = db.one("SELECT COUNT(*) AS n FROM jobs WHERE status='failed' AND finished_at > ?", (now() - 86400,))["n"]
    recent_failed = db.all_("SELECT id, kind, title, error, finished_at FROM jobs WHERE status='failed' AND parent_id IS NULL ORDER BY finished_at DESC LIMIT 10")
    return {
        "cpu_percent": psutil.cpu_percent(interval=0.3), "cpu_count": psutil.cpu_count(), "load": os.getloadavg(),
        "memory": {"total": vm.total, "used": vm.total - vm.available, "percent": vm.percent},
        "disk": storage.disk_usage(), "disk_state": db.get_setting("disk_state", {}), "queues": queues,
        "failed_24h": failed, "recent_failed": recent_failed, "profile": s.profile, "heavy_limit": s.heavy_limit,
        "ai_limit": s.ai_limit, "last_backup": db.get_setting("last_db_backup"), "last_restic": db.get_setting("last_restic"),
        "workers": db.all_("SELECT DISTINCT worker FROM jobs WHERE heartbeat_at > ?", (now() - 300,)),
        "ocr_engine": _ocr_engine(), "sandbox": s.sandbox, "sandbox_effective": _sandbox_effective(),
    }


def _sandbox_effective() -> dict:
    """各重负载 worker 启动时报告的实际沙箱方式。"""
    rows = db.all_("SELECT key, value FROM meta WHERE key LIKE 'setting:sandbox_effective:%'")
    return {r["key"].split(":", 2)[2]: db.jload(r["value"], {}) for r in rows}


def _ocr_engine() -> str:
    from ..tools import ocr
    try:
        return ocr.engine_name() or "未安装"
    except Exception:
        return "未知"


@api(auth="owner")
def audit_list(req: Req):
    limit, offset = page_args(req, 100, 500)
    rows = db.all_("SELECT * FROM audit ORDER BY id DESC LIMIT ? OFFSET ?", (limit, offset))
    for r in rows:
        r["detail"] = db.jload(r["detail"], {})
    return {"items": rows}


@api(auth="owner")
def maintenance_run(req: Req):
    from .. import scheduler
    names = req.b("tasks") or ["expire_files", "disk_check", "clean_tmp"]
    allowed = {n for n, _, _ in scheduler.TASKS}
    res = scheduler.run_once([n for n in names if n in allowed])
    return {"result": {k: (v if isinstance(v, (int, str, float, list, dict, type(None))) else str(v)) for k, v in res.items()}}


# ---------- 字体（仅用于服务器预览） ----------

@api(auth="owner")
def fonts_list(req: Req):
    d = get_settings().font_dir
    return {"items": [{"name": p.name, "size": p.stat().st_size} for p in sorted(d.glob("*")) if p.is_file()]}


@api(auth="owner")
def font_add(req: Req):
    """从已上传的文件中登记字体（仅限有合法授权的字体），用于让预览更接近实际效果。"""
    f = accounts.get_file_scoped(req.s, str(req.b("file_id", "")))
    name = safe_filename(f["name"])
    if not name.lower().endswith((".ttf", ".otf", ".ttc")):
        raise UserError("只接受 TTF / OTF / TTC 字体文件")
    dst = get_settings().font_dir / name
    import shutil
    shutil.copyfile(storage.blob_path(f["sha"]), dst)
    from ..render.fonts import font_file
    font_file.cache_clear()
    return {"ok": True}


@api(auth="owner")
def font_delete(req: Req):
    p = get_settings().font_dir / safe_filename(req.path["name"])
    if p.exists():
        p.unlink()
    return {"ok": True}


@api(auth="owner")
def compat_pack(req: Req):
    return create_job(req.s, "compat_pack", {}, None, req.ip)
