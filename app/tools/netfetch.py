"""受控抓取：用户或模型给出的网址只允许访问公网地址。

- 每一跳（包括重定向）都先解析域名并检查全部地址，再直接连接校验过的 IP，
  连接时仍以原域名做 SNI 和证书校验。这样解析结果不会在"检查"与"连接"之间被换成内网地址（DNS 重绑定）。
- 不自动解压、不自动跟随重定向（由本模块逐跳处理），读取大小有上限。
"""
from __future__ import annotations

import http.client
import ipaddress
import socket
import ssl
from dataclasses import dataclass
from typing import Callable
from urllib.parse import urljoin, urlsplit

from ..util import UserError

MAX_REDIRECTS = 5
REDIRECTS = (301, 302, 303, 307, 308)


@dataclass
class Fetched:
    url: str                        # 最终（重定向后）的地址
    status: int
    headers: http.client.HTTPMessage
    body: bytes
    truncated: bool                 # 超过大小上限时为 True，body 为已读到的部分

    @property
    def charset(self) -> str | None:
        return self.headers.get_content_charset()


def _is_public(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    return bool(ip.is_global) and not ip.is_multicast


def resolve_public(host: str, port: int, allow_private: bool = False) -> list[str]:
    """解析域名并检查全部地址都是公网地址，返回可连接的地址列表。"""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError):
        raise UserError("域名无法解析")
    ips: list[str] = []
    for info in infos:
        raw = str(info[4][0]).split("%")[0]
        try:
            ip = ipaddress.ip_address(raw)
        except ValueError:
            raise UserError("域名无法解析")
        if not allow_private and not _is_public(ip):
            raise UserError("不允许访问内网地址")
        if raw not in ips:
            ips.append(raw)
    if not ips:
        raise UserError("域名无法解析")
    return ips


def _connect(scheme: str, host: str, port: int, ips: list[str], connect_timeout: float, read_timeout: float):
    last: Exception | None = None
    for ip in ips:
        try:
            raw = socket.create_connection((ip, port), timeout=connect_timeout)
        except OSError as e:
            last = e
            continue
        raw.settimeout(read_timeout)
        if scheme == "https":
            ctx = ssl.create_default_context()
            try:
                sock = ctx.wrap_socket(raw, server_hostname=host)
            except (OSError, ssl.SSLError):
                raw.close()
                raise
            conn = http.client.HTTPSConnection(host, port, timeout=read_timeout)
        else:
            sock = raw
            conn = http.client.HTTPConnection(host, port, timeout=read_timeout)
        conn.sock = sock  # 已经连到校验过的 IP；Host 头和 SNI 仍使用原域名
        return conn
    raise last or OSError("无法连接")


def fetch(url: str, *, max_bytes: int, headers: dict | None = None, connect_timeout: float = 8, read_timeout: float = 15,
          allow_private: bool = False, max_redirects: int = MAX_REDIRECTS,
          on_headers: Callable[[http.client.HTTPResponse], None] | None = None) -> Fetched:
    """GET 一个公网地址。on_headers 在读取正文前调用，可用于按状态码或类型提前拒绝。

    地址策略问题抛出 UserError；网络错误抛出 OSError 或 http.client.HTTPException。"""
    for _ in range(max_redirects + 1):
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise UserError("网址无效")
        host = parts.hostname
        try:
            port = parts.port or (443 if parts.scheme == "https" else 80)
        except ValueError:
            raise UserError("网址无效")
        ips = resolve_public(host, port, allow_private)
        conn = _connect(parts.scheme, host, port, ips, connect_timeout, read_timeout)
        try:
            path = parts.path or "/"
            if parts.query:
                path += "?" + parts.query
            hdrs = {"Accept-Encoding": "identity", "Connection": "close", **(headers or {})}
            conn.request("GET", path, headers=hdrs)
            resp = conn.getresponse()
            if resp.status in REDIRECTS and resp.headers.get("Location"):
                url = urljoin(url, resp.headers["Location"])
                continue
            if on_headers:
                on_headers(resp)
            try:
                declared = int(resp.headers.get("Content-Length") or 0)
            except ValueError:
                declared = 0
            if declared > max_bytes:
                return Fetched(url, resp.status, resp.headers, b"", True)
            buf = bytearray()
            truncated = False
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                buf.extend(chunk)
                if len(buf) > max_bytes:
                    truncated = True
                    break
            return Fetched(url, resp.status, resp.headers, bytes(buf), truncated)
        finally:
            conn.close()
    raise UserError("重定向次数过多")
