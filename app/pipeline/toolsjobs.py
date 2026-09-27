"""工具类任务：格式转换、PDF 工具、OCR、模型接口测试、兼容性测试包。

工具结果不进入作品库，默认保留 24 小时（后台可调整），可在界面上保存到作品库。
"""
from __future__ import annotations

from pathlib import Path

from .. import db, llm, storage
from ..tools import convert as convmod
from ..tools import limits, ocr, pdfdocx
from ..tools import pdf as pdftools
from ..util import UserError, safe_filename
from .context import Ctx


def _ttl() -> float:
    return float(db.get_setting("tool_output_hours", 24)) * 3600


def _out(ctx: Ctx, paths: list[Path]) -> list[dict]:
    out = []
    for p in paths:
        rec = storage.store_path_as_file(ctx.workspace_id, p, p.name, "output", job_id=ctx.id, ttl=_ttl())
        out.append({"file_id": rec["id"], "name": rec["name"], "size": rec["size"], "mime": rec["mime"]})
    return out


def _input(ctx: Ctx, fid: str) -> tuple[Path, str, dict]:
    f = storage.get_file(fid)
    if not f:
        raise UserError("输入文件不存在或已过期")
    p = ctx.materialize(f, sub=f"in_{fid}")
    kind = (f.get("meta") or {}).get("kind") or limits.inspect(p, f["name"])["kind"]
    return p, kind, f


def handle_convert(ctx: Ctx) -> dict:
    src, kind, f = _input(ctx, ctx.p["file_id"])
    target = ctx.p["target"]
    ctx.progress(0.1, "转换中")
    outs, report = convmod.convert(src, kind, target, ctx.tmp / "out", cancel=ctx.cancelled,
                                   progress=ctx.sub_progress(0.1, 0.9), options=ctx.p.get("options") or {})
    return {"files": _out(ctx, outs), "report": _clean_report(report)}


def _clean_report(r: dict) -> dict:
    r = dict(r)
    if "low_confidence" in r:
        r["low_confidence"] = r["low_confidence"][:50]
    return r


def handle_pdf_tool(ctx: Ctx) -> dict:
    op = ctx.p["op"]
    opts = ctx.p.get("options") or {}
    ids = ctx.p.get("file_ids") or []
    inputs = []
    for fid in ids:
        p, kind, f = _input(ctx, fid)
        if kind != "pdf":
            raise UserError(f"{f['name']} 不是 PDF")
        inputs.append((p, f))
    if not inputs:
        raise UserError("请选择 PDF 文件")
    out = ctx.tmp / "out"
    out.mkdir(parents=True, exist_ok=True)
    first, f0 = inputs[0]
    stem = safe_filename(Path(f0["name"]).stem)
    report: dict = {}
    ctx.progress(0.1, "处理中")
    if op == "merge":
        if len(inputs) < 2:
            raise UserError("合并至少需要两个文件")
        res = [pdftools.merge([p for p, _ in inputs], out / f"{stem}_合并.pdf", keep_bookmarks=opts.get("bookmarks", True),
                              names=[Path(f["name"]).stem for _, f in inputs])]
    elif op == "split":
        parts = pdftools.split(first, out / "parts", mode=opts.get("mode", "ranges"), ranges=opts.get("ranges", ""), every=opts.get("every", 1))
        res = parts if len(parts) <= 3 else [pdftools.zip_files(parts, out / f"{stem}_拆分.zip")]
        report["parts"] = len(parts)
    elif op == "compress":
        o = out / f"{stem}_压缩.pdf"
        report = pdftools.compress(first, o, opts.get("level", "ebook"), cancel=ctx.cancelled)
        res = [o]
    elif op == "rotate":
        res = [pdftools.rotate(first, out / f"{stem}_旋转.pdf", opts.get("pages", ""), int(opts.get("angle", 90)))]
    elif op == "reorder":
        order = [int(x) for x in opts.get("order") or []]
        res = [pdftools.reorder(first, out / f"{stem}_调整.pdf", order)]
    elif op == "extract_text":
        text, report = pdftools.extract_text(first, fmt=opts.get("format", "md"), cancel=ctx.cancelled, progress=ctx.sub_progress(0.1, 0.9))
        o = out / f"{stem}.{opts.get('format', 'md')}"
        o.write_text(text, encoding="utf-8")
        res = [o]
    elif op == "extract_images":
        imgs = pdftools.extract_images(first, out / "images")
        if not imgs:
            raise UserError("没有找到内嵌图片")
        res = imgs if len(imgs) == 1 else [pdftools.zip_files(imgs, out / f"{stem}_图片.zip")]
        report["images"] = len(imgs)
    else:
        raise UserError(f"未知操作：{op}")
    return {"files": _out(ctx, res), "report": _clean_report(report)}


def handle_ocr(ctx: Ctx) -> dict:
    """扫描件 OCR：可搜索 PDF（添加隐藏文字层）/ TXT / DOCX。图片也可以直接识别。"""
    src, kind, f = _input(ctx, ctx.p["file_id"])
    output = ctx.p.get("output", "pdf")
    stem = safe_filename(Path(f["name"]).stem)
    out = ctx.tmp / "out"
    out.mkdir(parents=True, exist_ok=True)
    if not ocr.engine_name():
        raise UserError("服务器没有可用的 OCR 引擎")
    if kind in ("png", "jpg", "gif", "bmp", "tiff", "webp"):
        from ..tools.convert import images_to_pdf
        src = images_to_pdf([src], out / f"{stem}.pdf")
        kind = "pdf"
    if kind != "pdf":
        raise UserError("OCR 只支持 PDF 和图片")
    ctx.progress(0.05, "识别中")
    if output == "pdf":
        o = out / f"{stem}_可搜索.pdf"
        report = ocr.searchable_pdf(src, o, dpi=int(ctx.p.get("dpi", 300)), cancel=ctx.cancelled, progress=ctx.sub_progress(0.05, 0.95))
    elif output == "txt":
        text, report = pdftools.extract_text(src, fmt="md", cancel=ctx.cancelled, progress=ctx.sub_progress(0.05, 0.95))
        o = out / f"{stem}.txt"
        o.write_text(text, encoding="utf-8")
    elif output == "docx":
        pages, report = pdfdocx.page_blocks(src, cancel=ctx.cancelled, progress=ctx.sub_progress(0.05, 0.9), image_dir=out / "img")
        o = pdfdocx.build_docx(pages, out / f"{stem}.docx", title=stem)
    else:
        raise UserError("输出格式只能是 pdf / txt / docx")
    report["engine"] = ocr.engine_name()
    return {"files": _out(ctx, [o]), "report": _clean_report(report)}


def handle_model_test(ctx: Ctx) -> dict:
    if ctx.p.get("what") == "list":
        return llm.list_remote_models(ctx.p["endpoint_id"])
    if ctx.p.get("model_id"):
        return llm.test_model(ctx.p["model_id"], ctx.p.get("what") or "chat")
    return llm.test_endpoint(ctx.p["endpoint_id"])


def handle_compat_pack(ctx: Ctx) -> dict:
    """兼容性测试包：全部布局、每种图表、每种文档预设、公式与格式的样例，打包下载。"""
    import zipfile

    from ..render.docx_render import render_document
    from ..render.pptx_render import render_deck
    from ..render.theme import PRESET_THEMES
    from ..render.xlsx_render import render_workbook
    from ..spec.common import CHART_TYPES
    from ..spec.deck import Deck
    from ..spec.document import PRESETS, Document
    from ..spec.workbook import Workbook
    from . import samples

    out = ctx.tmp / "pack"
    out.mkdir(parents=True, exist_ok=True)
    files = []
    ctx.progress(0.05, "生成 PPT 样例")
    for i, name in enumerate(list(PRESET_THEMES)[:3]):
        d = samples.sample_deck()
        d["theme"] = dict(PRESET_THEMES[name], name=name)
        p = out / f"PPT_全部布局_{name}.pptx"
        render_deck(Deck.model_validate(d), p, {}, ctx.tmp / "r")
        files.append(p)
    d = samples.chart_deck(CHART_TYPES)
    p = out / "PPT_全部图表类型.pptx"
    render_deck(Deck.model_validate(d), p, {}, ctx.tmp / "r")
    files.append(p)
    d = samples.sample_deck()
    d["aspect"] = "4:3"
    p = out / "PPT_4比3.pptx"
    render_deck(Deck.model_validate(d), p, {}, ctx.tmp / "r")
    files.append(p)
    ctx.progress(0.4, "生成 Word 样例")
    for preset in PRESETS:
        d = samples.sample_document()
        d["preset"] = preset
        for mode in ("system", "open"):
            d["font_mode"] = mode
            p = out / f"Word_{PRESETS[preset]}_{'系统字体' if mode == 'system' else '思源字体'}.docx"
            render_document(Document.model_validate(d), p, {}, ctx.tmp / "r")
            files.append(p)
    ctx.progress(0.8, "生成 Excel 样例")
    p = out / "Excel_公式图表格式.xlsx"
    render_workbook(Workbook.model_validate(samples.sample_workbook()), p)
    files.append(p)
    readme = out / "说明.txt"
    readme.write_text(samples.COMPAT_README, encoding="utf-8")
    files.append(readme)
    z = ctx.tmp / "兼容性测试包.zip"
    with zipfile.ZipFile(z, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in files:
            zf.write(f, f.name)
    return {"files": _out(ctx, [z])}


def handle_file_pages(ctx: Ctx) -> dict:
    """PDF 工具用的页面缩略图（旋转、排序时显示）。"""
    src, kind, f = _input(ctx, ctx.p["file_id"])
    if kind != "pdf":
        from ..tools import office
        src = office.to_pdf(src, ctx.tmp / "pdf", cancel=ctx.cancelled)
    imgs = pdftools.render_pages(src, ctx.tmp / "thumbs", dpi=60, max_side=480, fmt="jpg", cancel=ctx.cancelled)
    return {"pages": _out(ctx, imgs)}
