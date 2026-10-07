"""Starlette 应用：路由、安全响应头、静态前端。

启动：uvicorn app.web.app:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
import hashlib

from starlette.responses import FileResponse, HTMLResponse, JSONResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from .. import db
from ..config import STATIC_DIR, get_settings
from . import api_admin as ad
from . import api_auth as au
from . import api_files as fi
from . import api_jobs as jb
from . import api_works as wk
from .common import Req, api

CSP = ("default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; script-src 'self'; "
       "connect-src 'self'; font-src 'self' data:; object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")


class SecurityHeaders(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        resp = await call_next(request)
        h = resp.headers
        h.setdefault("X-Content-Type-Options", "nosniff")
        h.setdefault("X-Frame-Options", "DENY")
        h.setdefault("Referrer-Policy", "no-referrer")
        h.setdefault("Content-Security-Policy", CSP)
        h.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        if request.url.path.startswith("/api/") and "cache-control" not in h:
            h["Cache-Control"] = "no-store"
        elif request.url.path.startswith("/static/") and "cache-control" not in h:
            # 带版本号的路径内容不会变，可以长期缓存；不带版本号的旧路径每次向服务器确认
            versioned = request.url.path.startswith("/static/v/")
            h["Cache-Control"] = "public, max-age=31536000, immutable" if versioned else "no-cache"
        return resp


@api(auth="any")
def meta(req: Req):
    from ..render.icons import ICON_NAMES
    from ..render.theme import PRESET_THEMES
    from ..spec.common import CHART_TYPES, NATIVE_CHART_TYPES
    from ..spec.deck import LAYOUT_LABELS
    from ..spec.document import PRESETS
    from ..tools import convert
    return {"layouts": LAYOUT_LABELS, "doc_presets": PRESETS, "themes": {k: v for k, v in PRESET_THEMES.items()},
            "chart_types": CHART_TYPES, "native_charts": NATIVE_CHART_TYPES, "icons": ICON_NAMES,
            "conversions": convert.matrix_table()}


@api(auth="any")
def convert_targets(req: Req):
    from ..tools import convert
    return {"targets": convert.targets_for(req.q("kind", ""))}


def _static_version() -> str:
    """前端文件内容的摘要。首页引用 /static/v/<摘要>/…，升级后路径随之变化，
    浏览器和 CDN（例如 Cloudflare 会强制缓存脚本数小时）都不会继续使用旧脚本。"""
    h = hashlib.sha256()
    for f in sorted(STATIC_DIR.rglob("*")):
        if f.is_file():
            h.update(str(f.relative_to(STATIC_DIR)).encode())
            h.update(f.read_bytes())
    return h.hexdigest()[:12]


STATIC_VERSION = _static_version()


async def index(request):
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8").replace('"/static/', f'"/static/v/{STATIC_VERSION}/')
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


async def health(request):
    try:
        db.one("SELECT 1 AS x")
        return JSONResponse({"ok": True})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


def build() -> Starlette:
    s = get_settings()
    db.migrate()
    R = Route
    routes = [
        R("/", index), R("/healthz", health),
        R("/api/me", au.me), R("/api/login", au.login, methods=["POST"]), R("/api/logout", au.logout, methods=["POST"]),
        R("/api/redeem", au.redeem, methods=["POST"]),
        R("/api/owner/totp/setup", au.totp_setup, methods=["POST"]), R("/api/owner/totp/enable", au.totp_enable, methods=["POST"]),
        R("/api/owner/totp/disable", au.totp_disable, methods=["POST"]), R("/api/owner/password", au.change_password, methods=["POST"]),
        R("/api/meta", meta), R("/api/convert/targets", convert_targets),
        # 上传与文件
        R("/api/uploads", fi.upload_init, methods=["POST"]), R("/api/uploads/{uid}", fi.upload_status),
        R("/api/uploads/{uid}/complete", fi.upload_complete, methods=["POST"]),
        R("/api/uploads/{uid}/{index:int}", fi.upload_chunk, methods=["PUT"]),
        R("/api/files", fi.files_list), R("/api/files/zip", fi.files_zip, methods=["POST"]),
        R("/api/files/{fid}", fi.file_info), R("/api/files/{fid}", fi.file_delete, methods=["DELETE"]),
        R("/api/files/{fid}/download", fi.file_download), R("/api/files/{fid}/keep", fi.file_keep, methods=["POST"]),
        # 作品与版本
        R("/api/works", wk.works_list), R("/api/works/{wid}", wk.work_get),
        R("/api/works/{wid}", wk.work_update, methods=["PATCH"]), R("/api/works/{wid}", wk.work_trash, methods=["DELETE"]),
        R("/api/works/{wid}/untrash", wk.work_untrash, methods=["POST"]), R("/api/works/{wid}/versions", wk.versions_list),
        R("/api/works/{wid}/restore", wk.work_restore, methods=["POST"]), R("/api/works/{wid}/diff", wk.work_diff),
        R("/api/works/{wid}/share", wk.work_share, methods=["POST"]), R("/api/works/{wid}/share/{ws}", wk.work_unshare, methods=["DELETE"]),
        R("/api/works/{wid}/transfer", wk.work_transfer, methods=["POST"]), R("/api/works/{wid}/copy", wk.work_copy, methods=["POST"]),
        R("/api/versions/{vid}/star", wk.version_star, methods=["POST"]),
        # 任务
        R("/api/jobs", jb.jobs_list), R("/api/jobs", jb.job_create, methods=["POST"]),
        R("/api/jobs/events", jb.jobs_events), R("/api/jobs/{jid}", jb.job_get),
        R("/api/jobs/{jid}/cancel", jb.job_cancel, methods=["POST"]), R("/api/jobs/{jid}/retry", jb.job_retry, methods=["POST"]),
        R("/api/jobs/{jid}/continue", jb.job_continue, methods=["POST"]), R("/api/jobs/{jid}/events", jb.job_events),
        # 后台
        R("/api/admin/tokens", ad.tokens_list), R("/api/admin/tokens", ad.token_create, methods=["POST"]),
        R("/api/admin/tokens/{tid}", ad.token_update, methods=["PATCH"]), R("/api/admin/tokens/{tid}/revoke", ad.token_revoke, methods=["POST"]),
        R("/api/admin/workspaces", ad.workspaces_list), R("/api/admin/workspaces/{ws}", ad.workspace_delete, methods=["DELETE"]),
        R("/api/admin/endpoints", ad.endpoints_list), R("/api/admin/endpoints", ad.endpoint_save, methods=["POST"]),
        R("/api/admin/endpoints/{eid}", ad.endpoint_save, methods=["PATCH"]), R("/api/admin/endpoints/{eid}", ad.endpoint_delete, methods=["DELETE"]),
        R("/api/admin/endpoints/{eid}/test", ad.endpoint_test, methods=["POST"]), R("/api/admin/roles", ad.roles_set, methods=["PUT"]),
        R("/api/admin/endpoints/{eid}/fetch", ad.endpoint_fetch_models, methods=["POST"]),
        R("/api/admin/endpoints/{eid}/models", ad.endpoint_add_models, methods=["POST"]),
        R("/api/admin/models/{mid}", ad.model_update, methods=["PATCH"]), R("/api/admin/models/{mid}", ad.model_delete, methods=["DELETE"]),
        R("/api/admin/models/{mid}/test", ad.model_test, methods=["POST"]),
        R("/api/admin/settings", ad.settings_get), R("/api/admin/settings", ad.settings_put, methods=["PUT"]),
        R("/api/admin/usage", ad.usage), R("/api/admin/system", ad.system), R("/api/admin/audit", ad.audit_list),
        R("/api/admin/maintenance", ad.maintenance_run, methods=["POST"]), R("/api/admin/fonts", ad.fonts_list),
        R("/api/admin/fonts", ad.font_add, methods=["POST"]), R("/api/admin/fonts/{name}", ad.font_delete, methods=["DELETE"]),
        R("/api/admin/compat_pack", ad.compat_pack, methods=["POST"]),
        Mount("/static/v/{ver}", app=StaticFiles(directory=str(STATIC_DIR)), name="static_v"),
        Mount("/static", app=StaticFiles(directory=str(STATIC_DIR)), name="static"),
    ]

    async def not_found(request, exc):
        if request.url.path.startswith("/api/"):
            return JSONResponse({"error": "接口不存在", "code": "not_found"}, status_code=404)
        return FileResponse(STATIC_DIR / "index.html")

    async def server_error(request, exc):
        import logging
        logging.getLogger("docwork.web").exception("请求出错：%s", request.url.path)
        return JSONResponse({"error": "服务器内部错误" + (f"：{exc}" if s.debug else ""), "code": "server_error"}, status_code=500)

    return Starlette(routes=routes, middleware=[Middleware(SecurityHeaders)],
                     exception_handlers={404: not_found, 500: server_error, Exception: server_error})


app = build()
