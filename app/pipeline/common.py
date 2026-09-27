"""各生成流程共用：资料收集与摘要、配图解析、渲染子任务、创建版本。"""
from __future__ import annotations

import io
from pathlib import Path

from .. import db, models_cfg, storage, works
from ..config import RENDERER_VERSION, SPEC_VERSION
from ..tools import extract as extract_mod
from ..tools import limits
from ..util import UserError, estimate_tokens, stable_hash
from . import prompts
from .context import Ctx

MAX_SOURCE_TOKENS = 24000


# ---------- 资料 ----------

def handle_extract(ctx: Ctx) -> dict:
    """convert 队列：抽取单个文件的文字与图片。"""
    f = storage.get_file(ctx.p["file_id"])
    if not f:
        raise UserError("资料文件不存在")
    path = ctx.materialize(f)
    kind = (f.get("meta") or {}).get("kind") or limits.sniff(path, f["name"])
    res = extract_mod.extract(path, kind, ctx.tmp / "x", cancel=ctx.cancelled, progress=ctx.sub_progress(0.05, 0.95),
                              with_images=ctx.p.get("images", True))
    images = []
    for im in res["images"]:
        p = Path(im["path"])
        sha, size = storage.put_file(p)
        images.append({"sha": sha, "caption": im.get("caption", ""), "ext": p.suffix.lower()})
    return {"text": res["text"], "images": images, "report": res.get("report", {}), "name": f["name"]}


def gather_sources(ctx: Ctx, file_ids: list[str], lo: float, hi: float) -> tuple[str, list[dict]]:
    """抽取所有资料；过长时分块摘要（保留数字与页码）。返回 (资料文字, 图片素材列表)。"""
    if not file_ids:
        return "", []
    texts, images = [], []
    for i, fid in enumerate(file_ids):
        ctx.progress(lo + (hi - lo) * 0.6 * i / len(file_ids), "读取资料", f"读取第 {i + 1}/{len(file_ids)} 个文件")
        r = ctx.child("extract", f"extract_{fid}", {"file_id": fid})
        texts.append(f"# 资料：{r.get('name', '')}\n\n{r.get('text', '')}")
        images += r.get("images", [])
    full = "\n\n".join(texts)
    if estimate_tokens(full) <= MAX_SOURCE_TOKENS:
        return full, images
    # 分块摘要
    chunks = _chunks(full, 9000)
    sums = []
    for i, ch in enumerate(chunks):
        ctx.progress(lo + (hi - lo) * (0.6 + 0.4 * i / len(chunks)), "整理资料", f"摘要第 {i + 1}/{len(chunks)} 段")
        d = ctx.llm.json("fast", prompts.SUMMARIZE, f"<资料>\n{ch}\n</资料>", max_tokens=2500)
        sums.append(d.get("summary", "") if isinstance(d, dict) else str(d))
    return "\n\n".join(sums), images


def _chunks(text: str, tokens: int) -> list[str]:
    out, cur, n = [], [], 0
    for para in text.split("\n\n"):
        t = estimate_tokens(para)
        if n + t > tokens and cur:
            out.append("\n\n".join(cur))
            cur, n = [], 0
        cur.append(para[:20000])
        n += t
    if cur:
        out.append("\n\n".join(cur))
    return out


def source_block(text: str) -> str:
    return f"<资料>\n{text}\n</资料>" if text else "（用户没有提供资料，只根据题目撰写；不要编造具体数据。）"


# ---------- 配图 ----------

def image_refs(obj, path=""):
    """遍历规格，找出所有配图引用（dict 中含 query/prompt/asset 的 image 字段）。"""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in ("image",) and isinstance(v, dict):
                yield v
            else:
                yield from image_refs(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from image_refs(v)


def resolve_images(ctx: Ctx, spec: dict, assets: dict, mode: str = "auto", limit: int = 20, lo=0.0, hi=1.0) -> list[str]:
    """为没有素材的配图检索图库或生成图片。返回无法获取配图的说明。"""
    refs = [r for r in image_refs(spec) if not (r.get("asset") and r["asset"] in assets)]
    notes = []
    if mode == "none" or not refs:
        for r in refs:
            r["asset"] = None
        return notes
    st = models_cfg.status()
    use_stock = st["stock"] and mode in ("auto", "stock")
    use_gen = st["image"] and mode in ("auto", "generate")
    if not use_stock and not use_gen:
        notes.append("未配置图库或图像生成接口，配图位置使用占位图形")
        for r in refs:
            r["asset"] = None
        return notes
    cache: dict[str, str] = {}
    for i, r in enumerate(refs[:limit]):
        ctx.check()
        ctx.progress(lo + (hi - lo) * i / max(1, len(refs)), "配图", f"第 {i + 1}/{len(refs)} 张")
        q = (r.get("query") or r.get("alt") or "").strip()
        key = q or r.get("prompt", "")
        if key in cache:
            r["asset"] = cache[key]
            continue
        data, source, credit = None, "", ""
        if use_stock and q and r.get("source") in (None, "auto", "stock"):
            for cand in ctx.llm.stock_search(q, n=3):
                try:
                    data = ctx.llm.download(cand["url"])
                    source, credit = "stock", cand.get("credit", "")
                    break
                except Exception:
                    continue
        if data is None and use_gen and r.get("source") in (None, "auto", "generate"):
            try:
                data = ctx.llm.generate_image(r.get("prompt") or q or "abstract background")
                source = "generate"
            except Exception as e:
                notes.append(f"图像生成失败：{str(e)[:80]}")
        if data is None:
            r["asset"] = None
            continue
        aid = _save_asset(data, assets, source, credit, q)
        r["asset"] = aid
        cache[key] = aid
    for r in refs[limit:]:
        r["asset"] = None
    if len(refs) > limit:
        notes.append(f"配图超过 {limit} 张，其余位置使用占位图形")
    return notes


def _save_asset(data: bytes, assets: dict, source: str, credit: str = "", query: str = "") -> str:
    from PIL import Image

    with Image.open(io.BytesIO(data)) as im:
        im.load()
        fmt = (im.format or "PNG").lower()
        if max(im.size) > 2400:
            im = im.convert("RGB")
            im.thumbnail((2400, 2400))
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=88)
            data, fmt = buf.getvalue(), "jpeg"
    sha, size = storage.put_bytes(data)
    aid = "a" + sha[:15]
    assets[aid] = {"sha": sha, "source": source, "credit": credit, "query": query, "ext": "." + ("jpg" if fmt == "jpeg" else fmt)}
    return aid


def add_source_images(images: list[dict], assets: dict) -> list[dict]:
    """把资料中的图片登记为可用素材，返回给模型看的清单。"""
    out = []
    for im in images[:30]:
        aid = "a" + im["sha"][:15]
        assets.setdefault(aid, {"sha": im["sha"], "source": "upload", "credit": "", "ext": im.get("ext", ".png")})
        out.append({"asset": aid, "caption": im.get("caption", "")})
    return out


def file_asset(file_id: str, assets: dict) -> str | None:
    f = storage.get_file(file_id)
    if not f:
        return None
    aid = "a" + f["sha"][:15]
    assets.setdefault(aid, {"sha": f["sha"], "source": "upload", "credit": "", "ext": Path(f["name"]).suffix.lower() or ".png"})
    return aid


# ---------- 渲染与版本 ----------

def render(ctx: Ctx, kind: str, spec: dict, assets: dict, step: str, *, title: str, theme: dict | None = None,
           page_cache: dict | None = None, check: bool = True) -> dict:
    return ctx.child("render", step, {"kind": kind, "spec": spec, "assets": assets, "title": title, "theme": theme,
                                      "page_cache": page_cache or {}, "check": check})


def page_cache_of(version: dict | None) -> dict:
    if not version:
        return {}
    return {p["hash"]: p["preview"] for p in (version.get("manifest") or {}).get("pages", []) if p.get("hash") and p.get("preview")}


def save_version(ctx: Ctx, work_id: str, kind: str, spec: dict, rendered: dict, assets: dict, *, source: str,
                 message: str = "", changed: list | None = None, base_version_id: str | None = None,
                 extra: dict | None = None, title: str = "", search_text: str = "") -> dict:
    manifest = {
        "title": title,
        "exports": rendered.get("exports", {}),
        "pages": rendered.get("pages", []),
        "elements": rendered.get("elements", {}),
        "issues": rendered.get("issues", []),
        "warnings": rendered.get("warnings", []),
        "block_pages": rendered.get("block_pages", {}),
        "preview": rendered.get("preview"),
        "sheets": rendered.get("sheets"),
        "size": rendered.get("size"),
        "assets": assets,
        "package_problems": rendered.get("package_problems", []),
    }
    manifest.update(extra or {})
    return works.add_version(work_id, job_id=ctx.id, source=source, message=message, spec=spec, spec_version=SPEC_VERSION,
                             renderer_version=RENDERER_VERSION, manifest=manifest, changed=changed or [],
                             base_version_id=base_version_id, title=title, search_text=search_text)


def ensure_work(ctx: Ctx, kind: str, title: str) -> str:
    """生成任务第一次运行时创建作品；重试时复用（作品 ID 记在任务参数里）。"""
    if ctx.work_id:
        # 纵深防御：只沿用本工作区、同类型、还没有版本的作品（即本任务或其重试前创建的空作品）
        w = db.one("SELECT workspace_id, kind, current_version_id FROM works WHERE id=? AND deleted_at IS NULL", (ctx.work_id,))
        if w and w["workspace_id"] == ctx.workspace_id and w["kind"] == kind and (
                not w["current_version_id"] or db.one("SELECT 1 AS x FROM versions WHERE work_id=? AND job_id=?", (ctx.work_id, ctx.id))):
            return ctx.work_id
    wid = works.create_work(ctx.workspace_id, kind, title or "未命名", source="generate")
    db.update("jobs", {"id": ctx.id}, {"work_id": wid})
    ctx.work_id = wid
    return wid
