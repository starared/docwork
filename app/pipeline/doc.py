"""AI 生成 Word：资料 → 结构（可确认）→ 逐节撰写（带前文摘要）→ 配图 → 渲染（两轮目录页码）→ 版本。"""
from __future__ import annotations

import json

from pydantic import TypeAdapter, ValidationError

from .. import jobs
from ..spec.document import PRESETS, Block, Document, document_text
from ..util import estimate_tokens
from . import prompts
from .common import add_source_images, ensure_work, file_asset, gather_sources, render, resolve_images, save_version, source_block
from .context import Ctx
from .ppt import AWAITING, choose_theme

_BLOCKS = TypeAdapter(list[Block])


def handle_gen_doc(ctx: Ctx):
    p = ctx.p
    prev = ctx.job.get("result") or {}
    if prev.get("sources") is not None:
        sources, src_images = prev["sources"], prev.get("src_images", [])
    else:
        sources, src_images = gather_sources(ctx, p.get("file_ids") or [], 0.0, 0.15)
    outline = p.get("outline")
    if not outline:
        ctx.progress(0.17, "规划结构")
        outline = make_outline(ctx, sources)
        if p.get("confirm_outline"):
            jobs.set_awaiting(ctx.id, {"outline": outline, "sources": sources, "src_images": src_images}, "文档结构已生成，请确认或修改后继续")
            return AWAITING
    title = outline.get("title") or p.get("topic", "")[:40] or "文档"
    work_id = ensure_work(ctx, "doc", title)
    assets: dict = {}
    avail = add_source_images(src_images, assets)
    preset = p.get("preset") or outline.get("preset") or "report"
    if preset not in PRESETS:
        preset = "report"
    blocks: list[dict] = []
    if outline.get("toc") and preset != "official":
        blocks.append({"type": "toc", "title": "目录"})
    sections = outline.get("sections") or []
    src = sources if estimate_tokens(sources) <= 10000 else sources[:15000] + "\n……（资料已截断）"
    written: list[str] = []
    for i, sec in enumerate(sections):
        ctx.check()
        ctx.progress(0.2 + 0.55 * i / max(1, len(sections)), "撰写正文", f"第 {i + 1}/{len(sections)} 节：{sec.get('heading', '')}")
        prev_text = "\n".join(written[-2:])[-3000:]
        user = (f"文档标题：{title}\n文档类型：{PRESETS[preset]}\n完整结构：\n"
                + "\n".join(f"{'  ' * (s.get('level', 1) - 1)}- {s.get('heading', '')}" for s in sections)
                + f"\n\n现在撰写：{json.dumps(sec, ensure_ascii=False)}\n"
                + (f"前文结尾（用于衔接，不要重复）：\n{prev_text}\n" if prev_text else "")
                + (f"可用图片素材：{json.dumps(avail, ensure_ascii=False)}\n" if avail else "")
                + (f"用户的其他要求：{p.get('extra')}\n" if p.get("extra") else "")
                + "\n" + source_block(src))

        def validate(d, level=sec.get("level", 1)):
            if not isinstance(d, dict) or not isinstance(d.get("blocks"), list) or not d["blocks"]:
                raise ValueError("需要非空的 blocks 数组")
            bl = _BLOCKS.validate_python(d["blocks"])
            return [b.model_dump(mode="json") for b in bl]
        try:
            got = ctx.llm.json("writer", prompts.DOC_SECTION, user, validate=validate, stream=True, max_tokens=8000)
        except Exception as e:
            got = [{"type": "heading", "level": sec.get("level", 1), "text": sec.get("heading", "")},
                   {"type": "paragraph", "text": "（本节生成失败，请通过对话修改补充。原因：" + str(e)[:80] + "）"}]
        if got and got[0].get("type") != "heading":
            got.insert(0, {"type": "heading", "level": sec.get("level", 1), "text": sec.get("heading", "")})
        blocks.extend(got)
        written.append("\n".join(b.get("text", "") for b in got if isinstance(b.get("text"), str)))
    meta = outline.get("meta") or {}
    doc_d = {"title": title, "preset": preset, "meta": meta, "font_mode": p.get("font_mode", "system"),
             "header_text": p.get("header_text", "") or (title if preset in ("proposal", "manual") else ""), "blocks": blocks}
    if p.get("logo_file_id"):
        doc_d["logo_asset"] = file_asset(p["logo_file_id"], assets)
    doc = Document.model_validate(doc_d)
    ctx.progress(0.78, "配图")
    dd = doc.model_dump(mode="json")
    notes = resolve_images(ctx, dd, assets, p.get("image_mode", "auto"), lo=0.78, hi=0.82)
    doc = Document.model_validate(dd)
    theme = choose_theme(ctx, {"title": title}) if p.get("style") else None
    ctx.progress(0.83, "排版与导出")
    rendered = render(ctx, "doc", doc.model_dump(mode="json"), assets, "render_0", title=title,
                      theme=theme.model_dump() if theme else None)
    v = save_version(ctx, work_id, "doc", doc.model_dump(mode="json"), rendered, assets, source="generate",
                     message=p.get("topic", "")[:200], title=title, search_text=document_text(doc),
                     extra={"warnings": notes + rendered.get("warnings", []), "outline": outline,
                            "theme": theme.model_dump() if theme else None})
    return {"work_id": work_id, "version_id": v["id"], "issues": len(rendered.get("issues", [])), "notes": notes}


def make_outline(ctx: Ctx, sources: str) -> dict:
    p = ctx.p
    req = [f"要求：{p.get('topic', '')}"]
    for k, label in (("preset", "文档类型"), ("pages", "篇幅（页）"), ("audience", "读者"), ("language", "语言"), ("extra", "其他要求")):
        if p.get(k):
            req.append(f"{label}：{p[k]}")
    user = "\n".join(req) + "\n\n" + source_block(sources)

    def validate(d):
        if not isinstance(d, dict) or not d.get("sections"):
            raise ValueError("需要 sections 数组")
        for s in d["sections"]:
            if not s.get("heading"):
                raise ValueError("每个章节都需要 heading")
            s["level"] = max(1, min(3, int(s.get("level", 1))))
        return d
    return ctx.llm.json("planner", prompts.DOC_OUTLINE, user, validate=validate, stream=True, max_tokens=4000)
