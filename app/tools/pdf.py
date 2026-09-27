"""PDF 工具：合并、拆分、压缩、旋转、排序与删页、文字提取、图片提取、页面渲染。

使用 pikepdf（qpdf）处理结构，pdfplumber 提取带坐标的文字和表格，pypdfium2 渲染页面，
Ghostscript 负责压缩中的图片重采样。
"""
from __future__ import annotations

import io
import re
import shutil
import zipfile
from pathlib import Path
from typing import Callable

import pikepdf

from ..util import UserError
from . import sandbox


def page_count(path: Path) -> int:
    with pikepdf.open(path) as pdf:
        return len(pdf.pages)


def parse_ranges(spec: str, n: int) -> list[list[int]]:
    """'1-3,5,7-' → [[0,1,2],[4],[6..n-1]]（每段一组，页码从 1 开始）。"""
    groups = []
    for part in re.split(r"[,，;；\s]+", (spec or "").strip()):
        if not part:
            continue
        m = re.fullmatch(r"(\d*)\s*[-~—]\s*(\d*)", part)
        if m:
            a = int(m.group(1)) if m.group(1) else 1
            b = int(m.group(2)) if m.group(2) else n
        elif part.isdigit():
            a = b = int(part)
        else:
            raise UserError(f"页码范围格式不正确：{part}")
        if a < 1 or b > n or a > b:
            raise UserError(f"页码超出范围：{part}（共 {n} 页）")
        groups.append(list(range(a - 1, b)))
    if not groups:
        raise UserError("请填写页码范围")
    return groups


def merge(files: list[Path], out: Path, keep_bookmarks: bool = True, names: list[str] | None = None) -> Path:
    dst = pikepdf.new()
    offset = 0
    outline_items = []
    for i, f in enumerate(files):
        with pikepdf.open(f) as src:
            n = len(src.pages)
            dst.pages.extend(src.pages)
            if keep_bookmarks:
                title = (names[i] if names else f.stem)
                item = pikepdf.OutlineItem(title, offset)
                try:
                    with src.open_outline() as ol:
                        for child in ol.root:
                            item.children.append(_shift_outline(child, src, offset))
                except Exception:
                    pass
                outline_items.append(item)
            offset += n
    if keep_bookmarks and outline_items:
        with dst.open_outline() as ol:
            ol.root.extend(outline_items)
    dst.save(out)
    return out


def _shift_outline(item, src, offset: int):
    """把源文件书签复制为指向合并后页码的新书签。"""
    page = None
    try:
        dest = item.destination
        if isinstance(dest, pikepdf.Array) and len(dest) > 0:
            pg = dest[0]
            for idx, p in enumerate(src.pages):
                if p.obj.objgen == pg.objgen:
                    page = idx
                    break
        elif item.action is not None and "/D" in item.action:
            d = item.action.D
            if isinstance(d, pikepdf.Array) and len(d):
                for idx, p in enumerate(src.pages):
                    if p.obj.objgen == d[0].objgen:
                        page = idx
                        break
    except Exception:
        page = None
    new = pikepdf.OutlineItem(item.title or "", (page or 0) + offset)
    for c in item.children:
        new.children.append(_shift_outline(c, src, offset))
    return new


def split(path: Path, outdir: Path, mode: str = "ranges", ranges: str = "", every: int = 1) -> list[Path]:
    outdir.mkdir(parents=True, exist_ok=True)
    outs = []
    with pikepdf.open(path) as src:
        n = len(src.pages)
        if mode == "every":
            every = max(1, int(every))
            groups = [list(range(i, min(n, i + every))) for i in range(0, n, every)]
        elif mode == "bookmarks":
            starts = []
            try:
                with src.open_outline() as ol:
                    for item in ol.root:
                        d = item.destination
                        if isinstance(d, pikepdf.Array) and len(d):
                            for idx, p in enumerate(src.pages):
                                if p.obj.objgen == d[0].objgen:
                                    starts.append((idx, item.title))
                                    break
            except Exception:
                pass
            starts = sorted(set(starts))
            if not starts:
                raise UserError("该 PDF 没有可用于拆分的顶级书签")
            if starts[0][0] != 0:
                starts.insert(0, (0, "开头"))
            groups = []
            for k, (s, _) in enumerate(starts):
                e = starts[k + 1][0] if k + 1 < len(starts) else n
                if e > s:
                    groups.append(list(range(s, e)))
        else:
            groups = parse_ranges(ranges, n)
        width = len(str(len(groups)))
        for gi, g in enumerate(groups, 1):
            dst = pikepdf.new()
            for i in g:
                dst.pages.append(src.pages[i])
            o = outdir / f"{path.stem}_{str(gi).zfill(width)}_p{g[0] + 1}-{g[-1] + 1}.pdf"
            dst.save(o)
            outs.append(o)
    return outs


def rotate(path: Path, out: Path, pages: str, angle: int) -> Path:
    if angle % 90 != 0:
        raise UserError("旋转角度必须是 90 的倍数")
    with pikepdf.open(path) as pdf:
        n = len(pdf.pages)
        idx = [i for g in parse_ranges(pages, n) for i in g] if pages else list(range(n))
        for i in idx:
            pdf.pages[i].rotate(angle, relative=True)
        pdf.save(out)
    return out


def reorder(path: Path, out: Path, order: list[int]) -> Path:
    """order 为新的页序（从 1 开始），未列出的页被删除；可重复以复制页面。"""
    with pikepdf.open(path) as src:
        n = len(src.pages)
        if not order:
            raise UserError("至少保留一页")
        for i in order:
            if i < 1 or i > n:
                raise UserError(f"页码 {i} 超出范围（共 {n} 页）")
        dst = pikepdf.new()
        for i in order:
            dst.pages.append(src.pages[i - 1])
        dst.save(out)
    return out


def compress(path: Path, out: Path, level: str = "ebook", cancel: Callable[[], bool] | None = None) -> dict:
    """Ghostscript 重采样图片 + pikepdf 去冗余，取两者中较小的结果。"""
    presets = {"screen": "/screen", "ebook": "/ebook", "printer": "/printer"}
    if level not in presets:
        raise UserError("压缩档位只能是 screen / ebook / printer")
    before = path.stat().st_size
    work = out.parent
    candidates = []
    # 1) pikepdf：对象流 + 去除未使用对象
    p1 = work / f"{out.stem}_qpdf.pdf"
    with pikepdf.open(path) as pdf:
        pdf.remove_unreferenced_resources()
        pdf.save(p1, compress_streams=True, object_stream_mode=pikepdf.ObjectStreamMode.generate, recompress_flate=True)
    candidates.append(p1)
    # 2) Ghostscript
    gs = sandbox.which("gs")
    if gs:
        p2 = work / f"{out.stem}_gs.pdf"
        try:
            sandbox.run([gs, "-sDEVICE=pdfwrite", "-dCompatibilityLevel=1.6", f"-dPDFSETTINGS={presets[level]}", "-dNOPAUSE",
                         "-dBATCH", "-dQUIET", "-dSAFER", "-dDetectDuplicateImages=true", f"-sOutputFile={p2}", str(path)],
                        work, cancel=cancel)
            if p2.exists() and p2.stat().st_size > 0:
                # 再做一次对象流压缩
                p3 = work / f"{out.stem}_gsq.pdf"
                with pikepdf.open(p2) as pdf:
                    pdf.save(p3, compress_streams=True, object_stream_mode=pikepdf.ObjectStreamMode.generate)
                candidates.append(p3)
        except sandbox.ToolError:
            pass
    best = min(candidates + [path], key=lambda p: p.stat().st_size)
    shutil.copyfile(best, out)
    for c in candidates:
        c.unlink(missing_ok=True)
    after = out.stat().st_size
    return {"before": before, "after": after, "ratio": round(after / before, 3) if before else 1,
            "method": "原文件已是最小" if best == path else ("Ghostscript" if "gs" in best.name else "结构优化")}


def page_has_text(page) -> bool:
    return len(page.chars) > 5


def extract_text(path: Path, ocr_scanned: bool = True, fmt: str = "md", cancel=None, progress=None) -> tuple[str, dict]:
    """逐页提取文字；扫描页（无文字层）走 OCR。返回 (文本, 报告)。"""
    import pdfplumber

    from . import ocr as ocrmod

    parts = []
    report = {"pages": 0, "ocr_pages": [], "low_confidence": []}
    with pdfplumber.open(path) as pdf:
        n = len(pdf.pages)
        report["pages"] = n
        for i, page in enumerate(pdf.pages):
            if cancel and cancel():
                raise sandbox.ToolCancelled()
            if progress:
                progress(i / max(1, n), f"第 {i + 1}/{n} 页")
            head = f"\n\n## 第 {i + 1} 页\n\n" if fmt == "md" else f"\n\n—— 第 {i + 1} 页 ——\n\n"
            if page_has_text(page):
                txt = page.extract_text(layout=False) or ""
            elif ocr_scanned:
                img = render_page(path, i, dpi=250)
                res = ocrmod.ocr_image(img)
                txt = ocrmod.blocks_to_markdown(ocrmod.analyze(res, img.size[0]))
                report["ocr_pages"].append(i + 1)
                report["low_confidence"] += [{"page": i + 1, "text": r["text"], "conf": round(r["conf"], 2)} for r in res if r["conf"] < 0.8][:20]
            else:
                txt = ""
            parts.append(head + txt.strip())
    return "".join(parts).strip() + "\n", report


def extract_images(path: Path, outdir: Path) -> list[Path]:
    outdir.mkdir(parents=True, exist_ok=True)
    outs = []
    seen = set()
    with pikepdf.open(path) as pdf:
        for pi, page in enumerate(pdf.pages, 1):
            for name, raw in page.images.items():
                key = raw.objgen
                if key in seen:
                    continue
                seen.add(key)
                try:
                    img = pikepdf.PdfImage(raw)
                    base = outdir / f"p{pi:03d}_{str(name).strip('/')}"
                    p = img.extract_to(fileprefix=str(base))
                    outs.append(Path(p))
                except Exception:
                    try:
                        pil = pikepdf.PdfImage(raw).as_pil_image()
                        p = outdir / f"p{pi:03d}_{str(name).strip('/')}.png"
                        pil.save(p)
                        outs.append(p)
                    except Exception:
                        continue
    return outs


def render_page(path: Path, index: int, dpi: int = 150):
    """渲染单页为 PIL 图片。"""
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(str(path))
    try:
        page = pdf[index]
        return page.render(scale=dpi / 72).to_pil()
    finally:
        pdf.close()


def render_pages(path: Path, outdir: Path, dpi: int = 110, pages: list[int] | None = None, fmt: str = "png",
                 max_side: int = 1600, cancel=None) -> list[Path]:
    import pypdfium2 as pdfium

    outdir.mkdir(parents=True, exist_ok=True)
    pdf = pdfium.PdfDocument(str(path))
    outs = []
    try:
        idx = pages if pages is not None else list(range(len(pdf)))
        for i in idx:
            if cancel and cancel():
                raise sandbox.ToolCancelled()
            page = pdf[i]
            w, h = page.get_size()
            scale = min(dpi / 72, max_side / max(w, h))
            im = page.render(scale=scale).to_pil()
            p = outdir / f"page_{i + 1:04d}.{fmt}"
            if fmt == "jpg":
                im.convert("RGB").save(p, "JPEG", quality=85)
            else:
                im.save(p, "PNG", optimize=False)
            outs.append(p)
    finally:
        pdf.close()
    return outs


def zip_files(files: list[Path], out: Path) -> Path:
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for f in files:
            z.write(f, f.name)
    return out


def named_dest_pages(path: Path, prefix: str = "dw") -> dict[str, dict]:
    """读取 PDF 命名目标：{名称: {page, y}}，用于 Word 预览定位。"""
    out = {}
    with pikepdf.open(path) as pdf:
        pages = {p.obj.objgen: i for i, p in enumerate(pdf.pages)}
        dests = {}
        if "/Dests" in pdf.Root:
            for k, v in pdf.Root.Dests.items():
                dests[str(k).lstrip("/")] = v
        if "/Names" in pdf.Root and "/Dests" in pdf.Root.Names:
            from pikepdf import NameTree
            for k, v in NameTree(pdf.Root.Names.Dests).items():
                dests[str(k)] = v
        for name, v in dests.items():
            if not name.startswith(prefix):
                continue
            arr = v if isinstance(v, pikepdf.Array) else v.get("/D")
            if arr is None or len(arr) == 0:
                continue
            pg = pages.get(arr[0].objgen)
            if pg is None:
                continue
            y = float(arr[3]) if len(arr) > 3 and arr[3] is not None and not isinstance(arr[3], pikepdf.Name) else None
            h = float(pdf.pages[pg].mediabox[3])
            out[name] = {"page": pg, "y": round(1 - (y / h), 4) if y is not None and h else 0}
    return out
