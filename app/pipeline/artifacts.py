"""渲染任务（render 队列）：规格 → 导出文件、PDF、预览图、排版检查。

结果只包含文件库中的内容哈希，由发起任务创建版本和文件记录。
"""
from __future__ import annotations

import shutil
from pathlib import Path

from .. import storage
from ..config import RENDERER_VERSION
from ..render.charts import chart_data_xlsx
from ..spec.common import Theme
from ..spec.deck import Deck
from ..spec.document import Document, document_text
from ..spec.workbook import Workbook
from ..tools import office, ooxml
from ..tools import pdf as pdftools
from ..util import safe_filename, stable_hash
from .check import check_deck_pdf, check_doc_pdf
from .context import Ctx


def _assets(ctx: Ctx, assets: dict) -> dict[str, Path]:
    out = {}
    for aid, info in (assets or {}).items():
        sha = info["sha"] if isinstance(info, dict) else info
        try:
            out[aid] = storage.materialize(sha, ctx.tmp / "assets", f"{aid}{_ext(info)}")
        except FileNotFoundError:
            continue
    return out


def _ext(info) -> str:
    if isinstance(info, dict) and info.get("ext"):
        return info["ext"]
    return ".img"


def _put(p: Path, name: str | None = None) -> dict:
    sha, size = storage.put_file(p)
    return {"sha": sha, "size": size, "name": name or p.name}


def slide_hash(deck: Deck, i: int) -> str:
    s = deck.slides[i]
    return stable_hash({"s": s.model_dump(mode="json"), "t": deck.theme.model_dump(), "f": deck.footer, "l": deck.logo_asset,
                        "a": deck.aspect, "i": i, "n": len(deck.slides), "r": RENDERER_VERSION})


def handle_render(ctx: Ctx) -> dict:
    p = ctx.p
    kind = p["kind"]
    title = safe_filename(p.get("title") or "document")
    assets = _assets(ctx, p.get("assets") or {})
    out = ctx.tmp / "out"
    out.mkdir(parents=True, exist_ok=True)
    ctx.progress(0.05, "渲染")
    if kind == "ppt":
        return _render_ppt(ctx, Deck.model_validate(p["spec"]), assets, out, title, p)
    if kind == "doc":
        return _render_doc(ctx, Document.model_validate(p["spec"]), assets, out, title, p)
    if kind == "xls":
        return _render_xls(ctx, Workbook.model_validate(p["spec"]), out, title, p)
    raise ValueError(f"未知渲染类型 {kind}")


def _render_ppt(ctx: Ctx, deck: Deck, assets, out: Path, title: str, p: dict) -> dict:
    from ..render.pptx_render import render_deck

    pptx = out / f"{title}.pptx"
    res = render_deck(deck, pptx, assets, ctx.tmp / "r")
    ctx.check()
    problems = ooxml.validate_package(pptx)
    ctx.progress(0.3, "导出 PDF")
    pdf = office.to_pdf(pptx, out / "pdf", cancel=ctx.cancelled)
    ctx.check()
    issues = list(res.issues)
    if p.get("check", True):
        ctx.progress(0.6, "排版检查")
        issues += check_deck_pdf(pdf, res.slide_ids, res.elements)
    # 预览：按单页内容哈希缓存，只渲染变化的页
    cache = p.get("page_cache") or {}
    pages = []
    todo = []
    for i, sid in enumerate(res.slide_ids):
        h = slide_hash(deck, i)
        if h in cache:
            pages.append({"id": sid, "hash": h, "preview": cache[h]})
        else:
            pages.append({"id": sid, "hash": h, "preview": None})
            todo.append(i)
    if todo:
        ctx.progress(0.7, f"生成 {len(todo)} 页预览")
        imgs = pdftools.render_pages(pdf, out / "prev", dpi=110, pages=todo, max_side=1600, cancel=ctx.cancelled)
        for i, img in zip(todo, imgs):
            info = _put(img)
            pages[i]["preview"] = info["sha"]
            pages[i]["size"] = info["size"]
    exports = {"pptx": _put(pptx, f"{title}.pptx"), "pdf": _put(pdf, f"{title}.pdf")}
    if res.all_charts:
        x = chart_data_xlsx(res.all_charts, out / f"{title}_图表数据.xlsx")
        exports["data_xlsx"] = _put(x)
    return {"exports": exports, "pages": pages, "elements": res.elements, "issues": issues,
            "image_charts": [lbl for lbl, _ in res.image_charts], "package_problems": problems,
            "size": {"w": 13.333 if deck.aspect == "16:9" else 10.0, "h": 7.5}}


def _render_doc(ctx: Ctx, doc: Document, assets, out: Path, title: str, p: dict) -> dict:
    from ..render.docx_render import render_document

    theme = Theme.model_validate(p["theme"]) if p.get("theme") else None
    docx = out / f"{title}.docx"
    res = render_document(doc, docx, assets, ctx.tmp / "r", theme=theme)
    ctx.progress(0.3, "导出 PDF")
    pdf = office.to_pdf(docx, out / "pdf1", cancel=ctx.cancelled)
    dests = pdftools.named_dest_pages(pdf)
    has_toc = any(b.type == "toc" for b in doc.blocks)
    if has_toc:
        # 第二轮：把标题页码写入目录缓存，再导出一次
        ctx.progress(0.45, "更新目录页码")
        toc_pages = {h["id"]: dests[f"dw{h['id']}"]["page"] + 1 for h in res.headings if f"dw{h['id']}" in dests}
        res = render_document(doc, docx, assets, ctx.tmp / "r", toc_pages=toc_pages, theme=theme)
        pdf = office.to_pdf(docx, out / "pdf2", cancel=ctx.cancelled)
        dests = pdftools.named_dest_pages(pdf)
    ctx.check()
    issues = []
    if p.get("check", True):
        ctx.progress(0.6, "检查")
        # 公式、图表内文字不计入核对
        texts = []
        for b in doc.blocks:
            if b.type in ("heading", "paragraph", "quote"):
                texts.append(b.text)
            elif b.type == "list":
                texts += [i.text for i in b.items] + [s for i in b.items for s in i.sub]
            elif b.type == "table":
                texts += [str(c) for c in b.table.columns] + [str(v) for r in b.table.rows for v in r if v is not None]
        import re
        clean = re.sub(r"\*\*|\*|\^|~", "", "\n".join(texts))
        issues += check_doc_pdf(pdf, clean)
    ctx.progress(0.7, "生成预览")
    n = pdftools.page_count(pdf)
    imgs = pdftools.render_pages(pdf, out / "prev", dpi=100, max_side=1400, cancel=ctx.cancelled)
    pages = []
    for i, img in enumerate(imgs):
        info = _put(img)
        pages.append({"id": str(i + 1), "hash": info["sha"][:24], "preview": info["sha"], "size": info["size"]})
    block_pages = {k[2:]: v for k, v in dests.items() if k.startswith("dw")}
    exports = {"docx": _put(docx, f"{title}.docx"), "pdf": _put(pdf, f"{title}.pdf")}
    if res.charts:
        x = chart_data_xlsx(res.charts, out / f"{title}_图表数据.xlsx")
        exports["data_xlsx"] = _put(x)
    warnings = list(res.warnings)
    if res.fonts:
        warnings.append(f"预览使用替代字体显示 {'、'.join(sorted(res.fonts))}，版式以装有对应字体的电脑为准")
    if res.equation_images:
        warnings.append(f"{len(res.equation_images)} 个公式超出支持范围，已以图片插入")
    return {"exports": exports, "pages": pages, "block_pages": block_pages, "issues": issues, "warnings": warnings,
            "page_count": n, "package_problems": ooxml.validate_package(docx)}


def _render_xls(ctx: Ctx, wb: Workbook, out: Path, title: str, p: dict) -> dict:
    from ..render.xlsx_render import render_workbook
    from ..tools.xlsx_verify import verify

    theme = Theme.model_validate(p["theme"]) if p.get("theme") else None
    xlsx = out / f"{title}.xlsx"
    res = render_workbook(wb, xlsx, theme)
    ctx.progress(0.3, "重算与验证")
    ver = verify(xlsx, wb, ctx.tmp / "verify", res.counts, cancel=ctx.cancelled)
    ctx.progress(0.7, "生成打印预览")
    exports = {"xlsx": _put(xlsx, f"{title}.xlsx")}
    try:
        pdf = office.to_pdf(xlsx, out / "pdf", cancel=ctx.cancelled)
        exports["pdf"] = _put(pdf, f"{title}.pdf")
    except Exception as e:  # 打印预览失败不影响交付
        ver.setdefault("notes", []).append(f"打印预览生成失败：{str(e)[:100]}")
    return {"exports": exports, "pages": [], "sheets": res.sheets, "preview": ver["preview"],
            "issues": [dict(pr, kind=pr.get("kind", "problem")) for pr in ver["problems"]],
            "package_problems": ooxml.validate_package(xlsx)}
