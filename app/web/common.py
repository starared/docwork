"""Web 层公共部分：鉴权装饰器、CSRF、请求上下文、文件响应。"""
from __future__ import annotations

import json
import mimetypes
import urllib.parse
from dataclasses import dataclass, field
from functools import wraps
from typing import Any, Callable

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response

from .. import accounts, db, storage
from ..config import get_settings
from ..util import UserError

SESSION_COOKIE = "dw_session"
CSRF_COOKIE = "dw_csrf"
MUTATING = {"POST", "PUT", "PATCH", "DELETE"}


@dataclass
class Req:
    request: Request
    scope: accounts.Scope | None
    body: Any
    path: dict
    query: dict
    ip: str
    cookies: dict = field(default_factory=dict)
    response_cookies: list = field(default_factory=list)
    clear_cookies: bool = False

    def q(self, k: str, default=None):
        return self.query.get(k, default)

    def b(self, k: str, default=None):
        return (self.body or {}).get(k, default) if isinstance(self.body, dict) else default

    @property
    def s(self) -> accounts.Scope:
        assert self.scope is not None
        return self.scope


def _trusted_nets():
    import ipaddress
    out = []
    for part in get_settings().trusted_proxies.split(","):
        part = part.strip()
        if part:
            try:
                out.append(ipaddress.ip_network(part, strict=False))
            except ValueError:
                pass
    return out


def client_ip(request: Request) -> str:
    """客户端真实地址（用于登录、兑换限流和审计）。

    - Uvicorn 以 --no-proxy-headers 启动，request.client 始终是真正连到后端的地址（宿主机 Nginx 经 Docker 网关）。
    - 只有这个直连地址属于可信代理（DW_TRUSTED_PROXIES）时才读取转发头。
    - 优先用 X-Real-IP（Nginx 用 $remote_addr 覆盖设置，客户端无法伪造）；没有时取 X-Forwarded-For 的最后一项
      （由最近一层代理追加），绝不取第一项（第一项可以由客户端随意填写）。"""
    import ipaddress
    peer = request.client.host if request.client else ""
    try:
        pip = ipaddress.ip_address(peer)
    except ValueError:
        return peer
    if not any(pip in n for n in _trusted_nets()):
        return peer
    cand = (request.headers.get("x-real-ip") or "").strip()
    if not cand:
        xff = [x.strip() for x in (request.headers.get("x-forwarded-for") or "").split(",") if x.strip()]
        cand = xff[-1] if xff else ""
    try:
        return str(ipaddress.ip_address(cand)) if cand else peer
    except ValueError:
        return peer


def api(auth: str | None = "any", csrf: bool = True, raw_body: bool = False, perm: str | None = None):
    """auth: None（无需登录）| any（管理员或访客）| owner（仅管理员）。处理函数为同步函数，在线程池中执行。"""
    def deco(fn: Callable[[Req], Any]):
        @wraps(fn)
        async def endpoint(request: Request):
            try:
                scope = await run_in_threadpool(accounts.session_scope, request.cookies.get(SESSION_COOKIE))
                if auth and scope is None:
                    raise UserError("请先登录", 401, "unauthenticated")
                if auth == "owner" and not scope.is_owner:
                    raise UserError("仅管理员可用", 403, "forbidden")
                if perm and scope:
                    scope.require(perm)
                if request.method in MUTATING and csrf and scope is not None:
                    tok = request.headers.get("x-csrf-token", "")
                    if not tok or tok != scope.csrf:
                        raise UserError("请求校验失败，请刷新页面后重试", 403, "csrf")
                body = None
                if raw_body:
                    body = request
                elif request.method in MUTATING:
                    ct = request.headers.get("content-type", "")
                    data = await request.body()
                    if data:
                        if "application/json" not in ct:
                            raise UserError("请求格式必须是 JSON", 415, "content_type")
                        try:
                            body = json.loads(data)
                        except ValueError:
                            raise UserError("请求内容不是合法的 JSON")
                req = Req(request, scope, body, dict(request.path_params), dict(request.query_params), client_ip(request),
                          dict(request.cookies))
                if raw_body:
                    result = await fn(req)
                else:
                    result = await run_in_threadpool(_call, fn, req)
                resp = result if isinstance(result, Response) else JSONResponse(result if result is not None else {"ok": True})
                for c in req.response_cookies:
                    resp.set_cookie(**c)
                if req.clear_cookies:
                    resp.delete_cookie(SESSION_COOKIE, path="/")
                    resp.delete_cookie(CSRF_COOKIE, path="/")
                return resp
            except UserError as e:
                return JSONResponse({"error": e.message, "code": e.code}, status_code=e.status)
        endpoint._api = True  # type: ignore[attr-defined]
        return endpoint
    return deco


def _call(fn, req):
    try:
        return fn(req)
    finally:
        pass


def set_session_cookies(req: Req, cookie: str, csrf: str, max_age: int) -> None:
    s = get_settings()
    req.response_cookies.append(dict(key=SESSION_COOKIE, value=cookie, max_age=max_age, httponly=True, secure=s.cookie_secure,
                                     samesite="lax", path="/"))
    req.response_cookies.append(dict(key=CSRF_COOKIE, value=csrf, max_age=max_age, httponly=False, secure=s.cookie_secure,
                                     samesite="lax", path="/"))


def file_response(f: dict, inline: bool = False, name: str | None = None) -> Response:
    """下载一律经过后端鉴权；生产环境由 Nginx 内部转发发送文件（X-Accel-Redirect）。"""
    s = get_settings()
    name = name or f["name"]
    quoted = urllib.parse.quote(name)
    disp = f"{'inline' if inline else 'attachment'}; filename=\"{_ascii(name)}\"; filename*=UTF-8''{quoted}"
    headers = {"Content-Disposition": disp, "Cache-Control": "private, max-age=86400" if inline else "private, no-cache",
               "X-Content-Type-Options": "nosniff"}
    mime = f.get("mime") or storage.guess_mime(name)
    if s.accel_redirect:
        ext = (name.rsplit(".", 1)[-1] if "." in name else "bin").lower()[:8]
        sha = f["sha"]
        headers["X-Accel-Redirect"] = f"{s.accel_prefix}/blobs/{sha[:2]}/{sha[2:4]}/{sha}/f.{ext}"
        return Response(status_code=200, headers=headers, media_type=mime)
    path = storage.blob_path(f["sha"])
    if not path.exists():
        return JSONResponse({"error": "文件内容不存在"}, status_code=404)
    return FileResponse(path, media_type=mime, headers=headers)


def _ascii(name: str) -> str:
    out = "".join(c if 32 <= ord(c) < 127 and c not in '"\\' else "_" for c in name)
    return out or "file"


def disk_blocked() -> bool:
    st = db.get_setting("disk_state", {}) or {}
    return st.get("state") == "block"


def page_args(req: Req, default: int = 50, maximum: int = 200) -> tuple[int, int]:
    try:
        limit = max(1, min(maximum, int(req.q("limit", default))))
        offset = max(0, int(req.q("offset", 0)))
    except ValueError:
        raise UserError("分页参数无效")
    return limit, offset
