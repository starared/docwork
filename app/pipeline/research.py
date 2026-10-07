"""联网资料：SearXNG 检索 + 抓取网页正文，作为生成时的参考资料。

- 检索词由快速模型根据题目给出（失败时直接用题目）；用户也可以直接给出网址。
- 抓取网页只允许公网地址：每一跳（包括重定向）都解析域名并拒绝内网、本机、保留地址，防止借此访问内部服务。
- 只读取 HTML 和纯文本，限制大小；读不到正文时退回检索结果的摘要。
- 每条网络资料带编号 [n]，生成时可按编号标注来源；Word 末尾自动附参考文献。
"""
from __future__ import annotations

import ipaddress
import re
import socket
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin, urlsplit

import httpx

from ..config import get_settings
from ..util import UserError
from . import prompts
from .context import Ctx

MAX_URLS = 5            # 用户直接给出的网址数量上限
PAGE_MAX = 3 * 1024 * 1024
PAGE_CHARS = 6000       # 每个网页保留的正文字数
SNIPPET_MIN = 40        # 网页正文短于此时，视为没读到正文
ALLOW_PRIVATE = False   # 仅测试使用：允许抓取本机地址
LANGS = {"": "zh-CN", "zh": "zh-CN", "繁體中文": "zh-TW", "English": "en", "日本語": "ja"}


def enabled() -> bool:
    return bool(get_settings().searxng_url)


def clean_urls(urls) -> list[str]:
    """创建任务时校验用户给出的网址：只接受 http(s)，去重，最多 MAX_URLS 个。"""
    if isinstance(urls, str):
        urls = urls.split()
    out: list[str] = []
    for u in urls or []:
        u = str(u).strip()
        if not u:
            continue
        parts = urlsplit(u)
        if parts.scheme not in ("http", "https") or not parts.hostname or len(u) > 2000:
            raise UserError(f"网址无效：{u[:80]}")
        if u not in out:
            out.append(u)
    if len(out) > MAX_URLS:
        raise UserError(f"参考网页最多 {MAX_URLS} 个")
    return out


# ---------- 检索 ----------

def search(query: str, language: str = "", n: int = 8) -> list[dict]:
    base = get_settings().searxng_url.rstrip("/")
    with httpx.Client(timeout=httpx.Timeout(20, connect=5)) as c:
        r = c.get(base + "/search", params={"q": query, "format": "json", "language": LANGS.get(language, "auto"), "safesearch": 1})
        r.raise_for_status()
        out = []
        for x in r.json().get("results", []):
            if str(x.get("url", "")).startswith(("http://", "https://")):
                out.append({"url": x["url"], "title": str(x.get("title") or "")[:200], "snippet": str(x.get("content") or "")[:500]})
        return out[:n]


def plan_queries(ctx: Ctx) -> list[str]:
    p = ctx.p
    topic = str(p.get("topic", "")).strip()
    user = f"题目：{topic}\n" + (f"其他要求：{p['extra']}\n" if p.get("extra") else "") + f"输出语言：{p.get('language') or '简体中文'}"
    try:
        d = ctx.llm.json("fast", prompts.SEARCH_QUERIES, user, max_tokens=400)
        qs = [str(q).strip()[:120] for q in (d.get("queries") or []) if str(q).strip()] if isinstance(d, dict) else []
    except Exception:
        qs = []
    return qs[:4] or ([topic[:120]] if topic else [])


# ---------- 抓取 ----------

def _check_host(host: str, port: int | None) -> None:
    try:
        infos = socket.getaddrinfo(host, port or 443, type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise UserError("域名无法解析")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if not ip.is_global and not ALLOW_PRIVATE:
            raise UserError("不允许访问内网地址")


def fetch(url: str) -> tuple[str, str]:
    """抓取网页，返回 (标题, 正文)。逐跳检查地址，最多 5 次重定向。"""
    # 按维基百科等网站的爬虫规范，User-Agent 写明程序名和联系地址（伪装成通用浏览器或爬虫标识会被拒绝）
    ua = f"DocWork/1.0 (+{get_settings().public_url})"
    with httpx.Client(timeout=httpx.Timeout(15, connect=8), follow_redirects=False,
                      headers={"User-Agent": ua, "Accept": "text/html,text/plain;q=0.9"}) as c:
        for _ in range(6):
            parts = urlsplit(url)
            if parts.scheme not in ("http", "https") or not parts.hostname:
                raise UserError("网址无效")
            _check_host(parts.hostname, parts.port)
            with c.stream("GET", url) as r:
                if r.is_redirect:
                    url = urljoin(url, r.headers.get("location", ""))
                    continue
                if r.status_code >= 400:
                    raise UserError(f"网页返回 {r.status_code}")
                ctype = r.headers.get("content-type", "").lower()
                if not ctype.startswith(("text/html", "application/xhtml", "text/plain")):
                    raise UserError("不是网页（" + (ctype.split(";")[0] or "未知类型") + "）")
                buf = bytearray()
                for chunk in r.iter_bytes():
                    buf.extend(chunk)
                    if len(buf) > PAGE_MAX:
                        break
                raw, charset = bytes(buf), r.charset_encoding
            if ctype.startswith("text/plain"):
                return "", _squash(raw.decode(charset or "utf-8", "replace"))
            return html_text(raw, charset)
        raise UserError("重定向次数过多")


_DROP = ("script", "style", "noscript", "template", "svg", "iframe", "form", "nav", "header", "footer", "aside", "button", "select")
_BLOCK = ("p", "div", "section", "article", "li", "tr", "br", "h1", "h2", "h3", "h4", "h5", "h6", "pre", "blockquote", "table", "ul", "ol", "dd", "dt")


def html_text(raw: bytes, charset: str | None = None) -> tuple[str, str]:
    import lxml.html
    from lxml import etree
    if not charset and not re.search(rb"<meta[^>]+charset", raw[:4096], re.I):
        # 响应头和网页都没有声明编码：lxml 会按 Latin-1 解码，中文变成乱码
        try:
            raw.decode("utf-8")
            charset = "utf-8"
        except UnicodeDecodeError:
            charset = "gb18030"
    try:
        try:
            doc = lxml.html.fromstring(raw.decode(charset, "replace")) if charset else lxml.html.fromstring(raw)
        except ValueError:  # 带编码声明的 XHTML 不接受已解码的字符串
            doc = lxml.html.fromstring(raw)
    except (etree.ParserError, etree.XMLSyntaxError, ValueError, LookupError):
        return "", ""
    title = (doc.findtext(".//title") or "").strip()
    for el in doc.xpath("//" + " | //".join(_DROP)):
        if el.getparent() is not None:
            el.drop_tree()
    roots = doc.xpath("//article") or doc.xpath("//main") or doc.xpath("//body") or [doc]
    root = max(roots, key=lambda e: len(e.text_content()))
    for el in root.iter(*_BLOCK):
        el.tail = "\n" + (el.tail or "")
    return re.sub(r"\s+", " ", title)[:200], _squash(root.text_content())


def _squash(text: str) -> str:
    lines = (re.sub(r"[ \t 　]+", " ", ln).strip() for ln in text.splitlines())
    return "\n".join(ln for ln in lines if ln)


def _read(item: dict) -> dict:
    try:
        title, text = fetch(item["url"])
        item = dict(item, title=item.get("title") or title or item["url"], text=text[:PAGE_CHARS])
        if len(text) < SNIPPET_MIN:
            item["error"] = "没有读到正文"
    except (UserError, httpx.HTTPError, OSError) as e:
        item = dict(item, error=str(e)[:80] or type(e).__name__)
    return item


# ---------- 入口 ----------

def web_sources(ctx: Ctx, lo: float, hi: float) -> tuple[str, list[dict], list[str]]:
    """返回 (资料文字, 参考列表 [{n, title, url}], 提示信息)。"""
    p = ctx.p
    s = get_settings()
    notes: list[str] = []
    items = [{"url": u, "title": "", "snippet": "", "user": True} for u in p.get("urls") or []]
    if p.get("web_search"):
        if not enabled():
            notes.append("联网检索未配置，已跳过")
        else:
            ctx.progress(lo, "联网检索", "生成检索词")
            seen = {x["url"] for x in items}
            results: list[list[dict]] = []
            for q in plan_queries(ctx):
                ctx.check()
                ctx.progress(lo + (hi - lo) * 0.2, "联网检索", f"检索：{q}")
                try:
                    results.append(search(q, p.get("language", "")))
                except (httpx.HTTPError, ValueError) as e:
                    notes.append(f"检索“{q}”失败：{str(e)[:60]}")
            # 各检索词的结果轮流取，去重
            found: list[dict] = []
            for rank in range(max((len(r) for r in results), default=0)):
                for r in results:
                    if rank < len(r) and r[rank]["url"] not in seen:
                        seen.add(r[rank]["url"])
                        found.append(r[rank])
            items += found[:s.web_pages]
            if not found:
                notes.append("联网检索没有找到结果")
    if not items:
        return "", [], notes
    ctx.check()
    ctx.progress(lo + (hi - lo) * 0.4, "联网检索", f"读取 {len(items)} 个网页")
    with ThreadPoolExecutor(4) as ex:
        read = list(ex.map(_read, items))
    ctx.check()
    texts, refs = [], []
    for it in read:
        body = it.get("text", "") if not it.get("error") else ""
        if not body:
            if it.get("user"):
                notes.append(f"无法读取网页 {it['url'][:80]}：{it.get('error', '')}")
                continue
            if not it.get("snippet"):
                continue
            body = "（只有检索摘要）" + it["snippet"]
        n = len(refs) + 1
        title = it.get("title") or it["url"]
        refs.append({"n": n, "title": title, "url": it["url"]})
        texts.append(f"# 网络资料 [{n}]：{title}\n来源：{it['url']}\n\n{body}")
    ctx.progress(hi, "联网检索", f"读取到 {len(refs)} 份网络资料")
    return "\n\n".join(texts), refs, notes


def ref_items(refs: list[dict], date: str) -> list[str]:
    """按 GB/T 7714 的电子资源格式写参考文献条目。"""
    return [f"{r['title']}[EB/OL]. [{date}]. {r['url']}." for r in refs]


def citation_hint(refs: list[dict], where: str = "在句末") -> str:
    if not refs:
        return ""
    return (f"资料中的“网络资料 [1]–[{len(refs)}]”来自互联网。使用其中的数据或观点时，{where}用 [编号] 标注来源；"
            "不同来源说法不一致时，以更权威、更新的来源为准。\n")
