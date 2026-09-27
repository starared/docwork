"""OCR：识别文字、恢复阅读顺序、推断表格、生成可搜索 PDF。

引擎优先使用 RapidOCR（PP-OCR 模型，ONNX Runtime 推理，中文效果好）；
未安装时退回 Tesseract。识别文字、表格结构、多栏阅读顺序是三个独立步骤，
报告中分别列出低置信度文字、推断出的表格和可能错乱的顺序。
"""
from __future__ import annotations

import io
import re
import statistics
import subprocess
from functools import lru_cache
from pathlib import Path

from PIL import Image

from . import sandbox


@lru_cache(maxsize=1)
def _rapid():
    try:
        from rapidocr_onnxruntime import RapidOCR  # type: ignore
        return RapidOCR()
    except Exception:
        try:
            from rapidocr import RapidOCR  # type: ignore
            return RapidOCR()
        except Exception:
            return None


@lru_cache(maxsize=1)
def _tesseract_langs() -> str | None:
    if not sandbox.which("tesseract"):
        return None
    try:
        out = subprocess.run(["tesseract", "--list-langs"], capture_output=True, text=True, timeout=20).stdout
    except Exception:
        return None
    langs = [l.strip() for l in out.splitlines()[1:]]
    use = [l for l in ("chi_sim", "eng") if l in langs]
    return "+".join(use) if use else None


def engine_name() -> str:
    if _rapid() is not None:
        return "RapidOCR"
    if _tesseract_langs():
        return f"Tesseract（{_tesseract_langs()}）"
    return ""


def ocr_image(img: Image.Image | Path) -> list[dict]:
    """返回按阅读顺序排列的行：[{box:[x0,y0,x1,y1], text, conf}]，坐标为像素。"""
    if isinstance(img, (str, Path)):
        img = Image.open(img)
    img = img.convert("RGB")
    eng = _rapid()
    items: list[dict] = []
    if eng is not None:
        import numpy as np

        result = eng(np.array(img))
        res = result[0] if isinstance(result, tuple) else getattr(result, "boxes", None)
        if isinstance(result, tuple):
            for box, text, score in (res or []):
                xs = [p[0] for p in box]
                ys = [p[1] for p in box]
                items.append({"box": [min(xs), min(ys), max(xs), max(ys)], "text": text, "conf": float(score)})
        else:  # rapidocr 2.x 返回对象
            for box, text, score in zip(result.boxes or [], result.txts or [], result.scores or []):
                xs = [p[0] for p in box]
                ys = [p[1] for p in box]
                items.append({"box": [min(xs), min(ys), max(xs), max(ys)], "text": text, "conf": float(score)})
    else:
        langs = _tesseract_langs()
        if not langs:
            raise sandbox.ToolError("没有可用的 OCR 引擎（RapidOCR 或 Tesseract）")
        items = _tesseract(img, langs)
    for it in items:
        it["page_w"] = img.size[0]
    return order_lines(items, img.size[0])


def _tesseract(img: Image.Image, langs: str) -> list[dict]:
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "page.png"
        img.save(p)
        r = sandbox.run(["tesseract", str(p), "stdout", "-l", langs, "--psm", "3", "tsv"], Path(td), timeout=300, check=True)
    words: dict[tuple, list] = {}
    for row in r.stdout.splitlines()[1:]:
        f = row.split("\t")
        if len(f) < 12 or not f[11].strip():
            continue
        try:
            conf = float(f[10])
        except ValueError:
            continue
        if conf < 0:
            continue
        key = (f[2], f[3], f[4])
        x, y, w, h = int(f[6]), int(f[7]), int(f[8]), int(f[9])
        words.setdefault(key, []).append((x, y, w, h, f[11], conf / 100))
    out = []
    for ws in words.values():
        ws.sort(key=lambda t: t[0])
        hmed = statistics.median([t[3] for t in ws]) or 10
        # 同一行内水平间距过大时拆分为多个片段（多栏、表格单元格）
        frags, cur = [], [ws[0]]
        for prev, wd in zip(ws, ws[1:]):
            if wd[0] - (prev[0] + prev[2]) > hmed * 1.8:
                frags.append(cur)
                cur = [wd]
            else:
                cur.append(wd)
        frags.append(cur)
        for fr in frags:
            x0 = min(t[0] for t in fr)
            y0 = min(t[1] for t in fr)
            x1 = max(t[0] + t[2] for t in fr)
            y1 = max(t[1] + t[3] for t in fr)
            out.append({"box": [x0, y0, x1, y1], "text": _join_words([t[4] for t in fr]), "conf": sum(t[5] for t in fr) / len(fr)})
    return out


def _join_words(words: list[str]) -> str:
    s = ""
    for w in words:
        if s and not (_cjk(s[-1]) and _cjk(w[0])):
            s += " "
        s += w
    return s


def _cjk(ch: str) -> bool:
    return ord(ch) > 0x2E80


def order_lines(items: list[dict], page_w: float) -> list[dict]:
    """多栏阅读顺序：通栏行把页面分段，段内按栏自左向右、栏内自上而下。"""
    if not items:
        return []
    items = sorted(items, key=lambda r: (r["box"][1], r["box"][0]))
    full = page_w * 0.6
    segments: list[list[dict]] = []
    cur: list[dict] = []
    for it in items:
        w = it["box"][2] - it["box"][0]
        if w >= full or it.get("full"):
            if cur:
                segments.append(cur)
                cur = []
            segments.append([it])
        else:
            cur.append(it)
    if cur:
        segments.append(cur)
    out = []
    for seg in segments:
        if len(seg) < 4:
            out.extend(sorted(seg, key=lambda r: (r["box"][1], r["box"][0])))
            continue
        cols = _columns(seg, page_w)
        for c in cols:
            out.extend(sorted(c, key=lambda r: (r["box"][1], r["box"][0])))
    for i, it in enumerate(out):
        it["order"] = i
    return out


def _columns(seg: list[dict], page_w: float) -> list[list[dict]]:
    # 覆盖直方图找竖向空隙
    bins = 200
    cover = [0] * bins
    for it in seg:
        a = int(it["box"][0] / page_w * bins)
        b = int(it["box"][2] / page_w * bins)
        for k in range(max(0, a), min(bins, b + 1)):
            cover[k] += 1
    lo, hi = int(bins * 0.15), int(bins * 0.85)
    gaps = []
    k = lo
    while k < hi:
        if cover[k] == 0:
            s = k
            while k < hi and cover[k] == 0:
                k += 1
            if k - s >= 3:
                gaps.append((s + k) / 2 / bins * page_w)
        k += 1
    if not gaps:
        return [seg]
    edges = [0] + gaps + [page_w + 1]
    cols = [[] for _ in range(len(edges) - 1)]
    for it in seg:
        cx = (it["box"][0] + it["box"][2]) / 2
        for ci in range(len(edges) - 1):
            if edges[ci] <= cx < edges[ci + 1]:
                cols[ci].append(it)
                break
    return [c for c in cols if c]


def to_text(lines: list[dict]) -> str:
    """把识别行合并为段落：行距明显变大时分段。"""
    if not lines:
        return ""
    hs = [l["box"][3] - l["box"][1] for l in lines]
    lh = statistics.median(hs) if hs else 20
    out, prev = [], None
    for l in lines:
        if prev is not None:
            gap = l["box"][1] - prev["box"][3]
            same_col = abs(l["box"][0] - prev["box"][0]) < lh * 3
            if gap > lh * 0.9 or not same_col or l["box"][1] < prev["box"][1]:
                out.append("\n\n")
            elif not (_cjk(out[-1][-1:] or "a") and _cjk(l["text"][:1] or "a")):
                out.append(" ")
        out.append(l["text"])
        prev = l
    return "".join(out).strip()


_NUM = re.compile(r"^[-+±]?[\d,.]+%?$|^\d{4}[-/.年]\d{1,2}")


def detect_tables(lines: list[dict], min_rows: int = 3, min_cols: int = 2) -> list[dict]:
    """按版面规则推断表格：连续多行被切成数量相同、列起点对齐的单元格。

    为避免把双栏正文误判为表格：两列时要求至少三成单元格是数字或日期，且单元格较短。
    返回 [{rows: [[str]], box, ids}]。
    """
    if not lines:
        return []
    lh = statistics.median([l["box"][3] - l["box"][1] for l in lines]) or 20
    rows: list[list[dict]] = []
    for l in sorted(lines, key=lambda r: (r["box"][1] + r["box"][3]) / 2):
        cy = (l["box"][1] + l["box"][3]) / 2
        if rows and abs(((rows[-1][0]["box"][1] + rows[-1][0]["box"][3]) / 2) - cy) < lh * 0.5:
            rows[-1].append(l)
        else:
            rows.append([l])
    for r in rows:
        r.sort(key=lambda x: x["box"][0])
    tables = []
    i = 0
    while i < len(rows):
        n0 = len(rows[i])
        if n0 < min_cols:
            i += 1
            continue
        anchors = [c["box"][0] for c in rows[i]]
        j = i + 1
        while j < len(rows) and len(rows[j]) == n0 and _aligned(rows[j], anchors, lh):
            j += 1
        block = rows[i:j]
        cells = [c["text"] for r in block for c in r]
        numeric = sum(1 for t in cells if _NUM.match(t.strip())) / max(1, len(cells))
        avg_len = sum(len(t) for t in cells) / max(1, len(cells))
        is_table = j - i >= min_rows and avg_len <= 30 and (numeric >= 0.3 or (n0 >= 3 and numeric >= 0.1))
        if is_table:
            grid = [[c["text"] for c in r] for r in block]
            ids = [id(c) for r in block for c in r]
            box = [min(c["box"][0] for r in block for c in r), block[0][0]["box"][1],
                   max(c["box"][2] for r in block for c in r), max(c["box"][3] for c in block[-1])]
            tables.append({"rows": grid, "box": box, "ids": ids})
            i = j
        else:
            i += 1
    return tables


def _aligned(row: list[dict], anchors: list[float], lh: float) -> bool:
    return all(abs(c["box"][0] - a) < lh * 1.5 or abs(c["box"][2] - a) > 0 and abs(c["box"][0] - a) < lh * 3
               for c, a in zip(row, anchors))


def analyze(lines: list[dict], page_w: float) -> list[dict]:
    """版面分析：表格作为整块，其余文字按多栏顺序合并为段落。返回 [{type, text|rows, box}]。"""
    tables = detect_tables(lines)
    in_table = {i for t in tables for i in t["ids"]}
    items = [dict(l) for l in lines if id(l) not in in_table]
    for t in tables:
        items.append({"box": t["box"], "text": "", "table": t["rows"], "full": True, "conf": 1.0})
    ordered = order_lines(items, page_w)
    blocks: list[dict] = []
    buf: list[dict] = []

    def flush():
        if buf:
            blocks.append({"type": "text", "text": to_text(buf), "box": _union([b["box"] for b in buf]),
                           "size": statistics.median([b["box"][3] - b["box"][1] for b in buf])})
            buf.clear()
    for it in ordered:
        if "table" in it:
            flush()
            blocks.append({"type": "table", "rows": it["table"], "box": it["box"]})
        else:
            buf.append(it)
    flush()
    # 按段落拆分文字块
    out = []
    for b in blocks:
        if b["type"] == "text":
            for para in [p for p in b["text"].split("\n\n") if p.strip()]:
                out.append({"type": "text", "text": para.strip(), "box": b["box"], "size": b["size"]})
        else:
            out.append(b)
    return out


def _union(boxes):
    return [min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes)]


def blocks_to_markdown(blocks: list[dict]) -> str:
    parts = []
    for b in blocks:
        if b["type"] == "table":
            rows = b["rows"]
            n = max(len(r) for r in rows)
            rows = [r + [""] * (n - len(r)) for r in rows]
            parts.append("| " + " | ".join(rows[0]) + " |\n|" + "---|" * n + "\n" +
                         "\n".join("| " + " | ".join(r) + " |" for r in rows[1:]))
        else:
            parts.append(b["text"])
    return "\n\n".join(parts)


def searchable_pdf(src: Path, out: Path, dpi: int = 300, cancel=None, progress=None) -> dict:
    """为扫描页添加不可见文字层，生成可搜索、可复制的 PDF。已有文字层的页面保持不变。"""
    import pdfplumber
    import pikepdf
    from reportlab.lib.utils import ImageReader  # noqa: F401
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.pdfgen import canvas

    from .pdf import render_page

    try:
        pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    except Exception:
        pass
    report = {"pages": 0, "ocr_pages": [], "low_confidence": [], "tables": [], "engine": engine_name()}
    with pdfplumber.open(src) as pl:
        text_pages = [len(p.chars) > 5 for p in pl.pages]
    report["pages"] = len(text_pages)
    pdf = pikepdf.open(src)
    try:
        for i, has_text in enumerate(text_pages):
            if cancel and cancel():
                raise sandbox.ToolCancelled()
            if progress:
                progress(i / max(1, len(text_pages)), f"识别第 {i + 1}/{len(text_pages)} 页")
            if has_text:
                continue
            page = pdf.pages[i]
            mb = [float(v) for v in page.mediabox]
            W, H = mb[2] - mb[0], mb[3] - mb[1]
            rot = int(page.obj.get("/Rotate", 0)) % 360
            img = render_page(src, i, dpi=dpi)
            lines = ocr_image(img)
            report["ocr_pages"].append(i + 1)
            report["low_confidence"] += [{"page": i + 1, "text": l["text"], "conf": round(l["conf"], 2)} for l in lines if l["conf"] < 0.8][:30]
            tb = detect_tables(lines)
            if tb:
                report["tables"].append({"page": i + 1, "count": len(tb)})
            k = 72.0 / dpi
            buf = io.BytesIO()
            c = canvas.Canvas(buf, pagesize=(W, H))
            for l in lines:
                x0, y0, x1, y1 = [v * k for v in l["box"]]
                text = l["text"]
                if not text.strip():
                    continue
                fs = max(4.0, (y1 - y0) * 0.85)
                # 显示坐标（左上原点）→ 页面原生坐标
                u, v = x0, y1
                if rot == 0:
                    nx, ny = u, H - v
                elif rot == 90:
                    nx, ny = v, u
                elif rot == 180:
                    nx, ny = W - u, v
                else:
                    nx, ny = W - v, H - u
                t = c.beginText()
                t.setTextRenderMode(3)
                t.setFont("STSong-Light", fs)
                tw = pdfmetrics.stringWidth(text, "STSong-Light", fs) or 1
                t.setHorizScale(max(10, min(500, 100 * (x1 - x0) / tw)))
                c.saveState()
                c.translate(nx + mb[0], ny + mb[1])
                if rot:
                    c.rotate(-rot)
                t.setTextOrigin(0, fs * 0.15)
                t.textLine(text)
                c.drawText(t)
                c.restoreState()
            c.showPage()
            c.save()
            ov = pikepdf.open(io.BytesIO(buf.getvalue()))
            page.add_overlay(ov.pages[0])
        pdf.save(out)
    finally:
        pdf.close()
    return report
