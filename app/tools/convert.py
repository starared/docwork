"""格式转换矩阵：输入类型 × 目标格式 → 工具与保真等级。

PDF 转 PPT 的“重建”模式需要模型，由 AI 流程处理（任务类型 rebuild），不在这里。
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable

from ..util import UserError
from . import office, pdf as pdftools, pdfdocx, sandbox

OFFICE_WRITER = {"docx", "docm", "doc", "odt", "rtf"}
OFFICE_IMPRESS = {"pptx", "pptm", "ppt", "odp"}
OFFICE_CALC = {"xlsx", "xlsm", "xls", "ods"}
IMAGES = {"png", "jpg", "gif", "bmp", "tiff", "webp"}

# (输入类型集合, 目标) → (工具说明, 保真等级)
MATRIX: list[tuple[set[str], str, str, str]] = [
    (OFFICE_WRITER, "pdf", "LibreOffice", "高，取决于字体是否齐全"),
    (OFFICE_IMPRESS, "pdf", "LibreOffice", "高，动画不保留"),
    (OFFICE_CALC | {"csv"}, "pdf", "LibreOffice", "中，自动缩放到页宽"),
    ({"doc", "odt", "rtf"}, "docx", "LibreOffice", "中高"),
    ({"ppt", "odp"}, "pptx", "LibreOffice", "中高"),
    ({"xls", "ods"}, "xlsx", "LibreOffice", "中高"),
    ({"pdf"}, "docx", "pdf2docx / 内置重建（扫描页先 OCR）", "中：段落、表格、图片可编辑，复杂版式会偏移；扫描件为中低"),
    ({"pdf"}, "pptx_image", "每页渲染为整页图片", "外观与原稿一致，不可编辑"),
    ({"pdf"}, "xlsx", "表格识别（文字型 PDF 用结构提取，扫描件用 OCR 推断）", "中低，适合规则表格"),
    ({"pdf"}, "png", "页面渲染", "无损"),
    ({"pdf"}, "jpg", "页面渲染", "无损（JPEG 压缩）"),
    ({"pdf"}, "txt", "文字提取（扫描页 OCR）", "仅文字"),
    ({"pdf"}, "md", "文字提取（扫描页 OCR）", "仅文字与表格结构"),
    (IMAGES, "pdf", "img2pdf（JPEG 不重新压缩）", "无损"),
    ({"md", "html", "txt"}, "docx", "Pandoc", "高"),
    ({"md", "html", "txt"}, "pdf", "Pandoc + LibreOffice", "高"),
    ({"docx"}, "md", "Pandoc", "高（仅保留结构）"),
    ({"csv"}, "xlsx", "pandas / openpyxl", "无损"),
    (OFFICE_CALC, "csv", "pandas", "无损（每个工作表一个 CSV）"),
]
TARGET_LABELS = {"pdf": "PDF", "docx": "Word", "pptx": "PowerPoint", "xlsx": "Excel", "pptx_image": "PPT（页面图片）",
                 "pptx_rebuild": "PPT（重建，可编辑）", "png": "PNG 图片", "jpg": "JPG 图片", "txt": "纯文本", "md": "Markdown", "csv": "CSV"}


def targets_for(kind: str) -> list[dict]:
    out = []
    for kinds, tgt, tool, fid in MATRIX:
        if kind in kinds:
            out.append({"target": tgt, "label": TARGET_LABELS.get(tgt, tgt), "tool": tool, "fidelity": fid})
    if kind == "pdf":
        out.append({"target": "pptx_rebuild", "label": TARGET_LABELS["pptx_rebuild"], "tool": "抽取内容后由模型按本系统布局重建",
                    "fidelity": "内容完整、可编辑，版式不同于原稿", "ai": True})
    return out


def matrix_table() -> list[dict]:
    return [{"inputs": sorted(k), "target": TARGET_LABELS.get(t, t), "tool": tool, "fidelity": f} for k, t, tool, f in MATRIX]


def convert(src: Path, kind: str, target: str, outdir: Path, *, cancel: Callable[[], bool] | None = None,
            progress: Callable[[float, str], None] | None = None, options: dict | None = None) -> tuple[list[Path], dict]:
    """执行转换，返回 (输出文件列表, 报告)。"""
    options = options or {}
    outdir.mkdir(parents=True, exist_ok=True)
    if not any(t["target"] == target for t in targets_for(kind)):
        raise UserError(f"不支持把 {kind.upper()} 转换为 {TARGET_LABELS.get(target, target)}")
    report: dict = {"tool": next(t["tool"] for t in targets_for(kind) if t["target"] == target),
                    "fidelity": next(t["fidelity"] for t in targets_for(kind) if t["target"] == target), "notes": []}
    stem = src.stem
    if target == "pdf" and kind in OFFICE_WRITER | OFFICE_IMPRESS | OFFICE_CALC | {"csv"}:
        return [office.to_pdf(src, outdir, cancel=cancel)], report
    if target in ("docx", "pptx", "xlsx") and kind in {"doc", "odt", "rtf", "ppt", "odp", "xls", "ods"}:
        return [office.normalize_to_ooxml(src, outdir, cancel=cancel)], report
    if kind == "pdf":
        if target == "docx":
            return _pdf_docx(src, outdir, report, options, cancel, progress)
        if target == "pptx_image":
            return [_pdf_pptx_images(src, outdir / f"{stem}.pptx", cancel)], report
        if target == "xlsx":
            return [_pdf_tables_xlsx(src, outdir / f"{stem}.xlsx", report, cancel, progress)], report
        if target in ("png", "jpg"):
            imgs = pdftools.render_pages(src, outdir / "pages", dpi=int(options.get("dpi", 150)), fmt=target,
                                         max_side=int(options.get("max_side", 4000)), cancel=cancel)
            if len(imgs) == 1:
                return imgs, report
            return [pdftools.zip_files(imgs, outdir / f"{stem}_{target}.zip")], report
        if target in ("txt", "md"):
            text, rep = pdftools.extract_text(src, fmt=target, cancel=cancel, progress=progress)
            report.update(rep)
            p = outdir / f"{stem}.{target}"
            p.write_text(text, encoding="utf-8")
            return [p], report
    if kind in IMAGES and target == "pdf":
        return [images_to_pdf([src], outdir / f"{stem}.pdf")], report
    if kind in ("md", "html", "txt") and target in ("docx", "pdf"):
        docx = _pandoc(src, kind, outdir / f"{stem}.docx", cancel)
        if target == "docx":
            return [docx], report
        return [office.to_pdf(docx, outdir / "pdf", cancel=cancel)], report
    if kind == "docx" and target == "md":
        return [_pandoc(src, "docx", outdir / f"{stem}.md", cancel)], report
    if kind == "csv" and target == "xlsx":
        return [_csv_xlsx(src, outdir / f"{stem}.xlsx")], report
    if kind in OFFICE_CALC and target == "csv":
        return _xlsx_csv(src, kind, outdir, cancel), report
    raise UserError("该转换暂不可用")


def _pdf_docx(src, outdir, report, options, cancel, progress):
    import pdfplumber

    out = outdir / f"{src.stem}.docx"
    with pdfplumber.open(src) as p:
        scanned = [i + 1 for i, pg in enumerate(p.pages) if len(pg.chars) <= 5]
    engine = options.get("engine", "auto")
    use_pdf2docx = engine in ("auto", "pdf2docx") and not scanned
    if use_pdf2docx:
        try:
            from pdf2docx import Converter  # type: ignore

            cv = Converter(str(src))
            try:
                cv.convert(str(out), start=0, end=None)
            finally:
                cv.close()
            report["engine"] = "pdf2docx"
            return [out], report
        except ImportError:
            report["notes"].append("pdf2docx 未安装，已使用内置重建后端")
        except Exception as e:
            report["notes"].append(f"pdf2docx 转换失败（{str(e)[:80]}），已改用内置重建后端")
    pages, rep = pdfdocx.page_blocks(src, cancel=cancel, progress=progress, image_dir=outdir / "images")
    pdfdocx.build_docx(pages, out, title=src.stem)
    report.update(rep)
    report["engine"] = "内置重建"
    if scanned:
        report["fidelity"] = "中低：扫描页经 OCR 识别后重新排版"
    return [out], report


def _pdf_pptx_images(src: Path, out: Path, cancel) -> Path:
    from pptx import Presentation
    from pptx.util import Emu

    imgs = pdftools.render_pages(src, out.parent / "slides", dpi=200, fmt="jpg", max_side=2400, cancel=cancel)
    prs = Presentation()
    from PIL import Image
    with Image.open(imgs[0]) as im:
        w, h = im.size
    prs.slide_width = Emu(12192000)
    prs.slide_height = Emu(int(12192000 * h / w))
    blank = prs.slide_layouts[6]
    for p in imgs:
        s = prs.slides.add_slide(blank)
        s.shapes.add_picture(str(p), 0, 0, prs.slide_width, prs.slide_height)
    prs.save(out)
    return out


def _pdf_tables_xlsx(src: Path, out: Path, report: dict, cancel, progress) -> Path:
    from openpyxl import Workbook
    from openpyxl.styles import Font

    pages, rep = pdfdocx.page_blocks(src, cancel=cancel, progress=progress)
    wb = Workbook()
    wb.remove(wb.active)
    n = 0
    for pi, blocks in enumerate(pages, 1):
        for b in blocks:
            if b["type"] != "table":
                continue
            n += 1
            ws = wb.create_sheet(f"第{pi}页_表{n}"[:31])
            for r in b["rows"]:
                ws.append([_num(c) for c in r])
            for c in ws[1]:
                c.font = Font(bold=True)
    if n == 0:
        ws = wb.create_sheet("说明")
        ws.append(["没有识别到表格"])
        report["notes"].append("没有识别到表格：可能是图片中的不规则表格，或表格没有网格线")
    report["tables"] = n
    report["ocr_pages"] = rep.get("ocr_pages", [])
    wb.save(out)
    return out


def _num(s: str):
    t = (s or "").replace(",", "").strip()
    try:
        if t.endswith("%"):
            return float(t[:-1]) / 100
        return int(t) if t.isdigit() else float(t)
    except ValueError:
        return s


def images_to_pdf(images: list[Path], out: Path) -> Path:
    import img2pdf
    from PIL import Image

    prepared = []
    for p in images:
        with Image.open(p) as im:
            fmt = im.format
            if fmt == "JPEG" or (fmt == "PNG" and im.mode in ("RGB", "L", "P") and "transparency" not in im.info):
                prepared.append(str(p))
                continue
            conv = p.with_suffix(".conv.png")
            bg = Image.new("RGB", im.size, "white")
            rgba = im.convert("RGBA")
            bg.paste(rgba, mask=rgba.split()[3])
            bg.save(conv)
            prepared.append(str(conv))
    out.write_bytes(img2pdf.convert(prepared))
    return out


def _pandoc(src: Path, kind: str, out: Path, cancel) -> Path:
    fmt = {"md": "markdown", "txt": "markdown", "html": "html", "docx": "docx"}[kind]
    args = ["pandoc", "-f", fmt, str(src), "-o", str(out)]
    if out.suffix == ".md":
        args = ["pandoc", "-f", "docx", "-t", "gfm", str(src), "-o", str(out)]
    sandbox.run(args, src.parent, cancel=cancel, writable=[out.parent])
    return out


def _csv_xlsx(src: Path, out: Path) -> Path:
    import pandas as pd

    for enc in ("utf-8-sig", "gb18030"):
        try:
            df = pd.read_csv(src, encoding=enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise UserError("CSV 编码无法识别")
    df.to_excel(out, index=False, sheet_name="Sheet1")
    return out


def _xlsx_csv(src: Path, kind: str, outdir: Path, cancel) -> list[Path]:
    import pandas as pd

    if kind != "xlsx":
        src = office.normalize_to_ooxml(src, outdir / "norm", cancel=cancel)
    sheets = pd.read_excel(src, sheet_name=None)
    outs = []
    for name, df in sheets.items():
        p = outdir / f"{src.stem}_{name}.csv"
        df.to_csv(p, index=False, encoding="utf-8-sig")
        outs.append(p)
    if len(outs) > 1:
        return [pdftools.zip_files(outs, outdir / f"{src.stem}_csv.zip")]
    return outs
