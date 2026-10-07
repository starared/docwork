"""排版检查：规格中的预期文字与导出 PDF 逐项核对，并检查越界、溢出、字号。

- 缺失：元素的文字在整页都找不到（导出时被截掉或丢失）。
- 溢出：元素文字在页面上存在，但有一部分落在元素框之外。
- 越界：有文字超出页面边缘。
- 字号：渲染器已按下限缩字，仍放不下时记录为预估溢出。
背景、装饰形状与文字的叠放是正常设计，不算重叠。
"""
from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

_WS = re.compile(r"[\s•–\-—·…“”\"'‘’()（）,，.。:：;；!！?？、|/]+")


def norm(s: str) -> str:
    return _WS.sub("", s or "")


def _page_chars(pdf, index: int) -> tuple[float, float, list[dict]]:
    """用 pypdfium2 读出一页的全部字符及其框（左上角为原点，与 pdfplumber 的坐标约定一致）。
    pdfplumber 约占 20 MB 内存且逐页解析很慢，这里只需要字符和坐标。"""
    page = pdf[index]
    W, H = page.get_size()
    tp = page.get_textpage()
    chars = []
    try:
        n = tp.count_chars()
        text = tp.get_text_range(0, n) if n else ""
        # get_text_range 返回的字符串与字符索引一一对应（代理对、换行符除外），逐个取框
        for i in range(n):
            ch = text[i] if i < len(text) else ""
            if not ch.strip():
                continue
            l, b, r, t = tp.get_charbox(i)
            if r <= l or t <= b:
                continue
            chars.append({"text": ch, "x0": l, "x1": r, "top": H - t, "bottom": H - b})
    finally:
        tp.close()
    return float(W), float(H), chars


def check_deck_pdf(pdf_path: Path, slide_ids: list[str], elements: dict[str, list[dict]], tol: float = 4.0) -> list[dict]:
    import pypdfium2 as pdfium

    issues: list[dict] = []
    pdf = pdfium.PdfDocument(str(pdf_path))
    try:
        for idx in range(min(len(pdf), len(slide_ids))):
            sid = slide_ids[idx]
            W, H, chars = _page_chars(pdf, idx)
            page_counts = Counter(norm("".join(c["text"] for c in chars)))
            # 越界
            oob = [c for c in chars if c["x1"] > W + 1 or c["bottom"] > H + 1 or c["x0"] < -1 or c["top"] < -1]
            if oob:
                issues.append({"slide": sid, "index": idx, "element": None, "kind": "out_of_page",
                               "message": f"有 {len(oob)} 个字符超出页面边缘"})
            for e in elements.get(sid, []):
                if e["kind"] not in ("text", "table") or e.get("decor") or not norm(e.get("text", "")):
                    continue
                x, y, w, h = e["box"]
                bx0, by0, bx1, by1 = x * W - tol, y * H - tol, (x + w) * W + tol, (y + h) * H + tol
                inside = [c for c in chars if bx0 <= (c["x0"] + c["x1"]) / 2 <= bx1 and by0 <= (c["top"] + c["bottom"]) / 2 <= by1]
                exp = Counter(norm(e["text"]))
                got = Counter(norm("".join(c["text"] for c in inside)))
                total = sum(exp.values())
                missing_in_box = sum(max(0, n - got.get(ch, 0)) for ch, n in exp.items())
                if total == 0 or missing_in_box / total < 0.03:
                    continue
                missing_page = sum(max(0, n - page_counts.get(ch, 0)) for ch, n in exp.items())
                if missing_page / total >= 0.03:
                    issues.append({"slide": sid, "index": idx, "element": e["id"], "kind": "missing_text",
                                   "message": f"约 {missing_page} 个字没有出现在导出结果中（可能被截断）"})
                else:
                    issues.append({"slide": sid, "index": idx, "element": e["id"], "kind": "overflow",
                                   "message": f"约 {missing_in_box} 个字超出了文本框"})
    finally:
        pdf.close()
    return issues


def check_doc_pdf(pdf_path: Path, expected_text: str) -> list[dict]:
    """Word：全文文字覆盖率核对（公式、图表内文字不计入）。"""
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(str(pdf_path))
    try:
        parts = []
        for i in range(len(pdf)):
            tp = pdf[i].get_textpage()
            try:
                parts.append(tp.get_text_range())
            finally:
                tp.close()
    finally:
        pdf.close()
    got = Counter(norm("".join(parts)))
    exp = Counter(norm(expected_text))
    total = sum(exp.values())
    if not total:
        return []
    missing = sum(max(0, n - got.get(ch, 0)) for ch, n in exp.items())
    if missing / total > 0.02:
        return [{"kind": "missing_text", "message": f"导出结果比预期少约 {missing} 个字（{missing * 100 // total}%）"}]
    return []


# ---------- 精简文字时保护关键信息 ----------

_PROTECT = [
    re.compile(r"[-+]?\d[\d,]*(?:\.\d+)?\s*(?:%|‰|万|亿|千|百|元|美元|倍|个百分点|岁|年|月|日|天|小时|分钟|秒|次|人|家|项|μM|mM|nM|mg|kg|g|ml|mL|L|km|m|cm|mm|℃|°C)?"),
    re.compile(r"\[\d+(?:[,，\-–]\d+)*\]"),
    re.compile(r"[「“\"《][^」”\"》]{1,30}[」”\"》]"),
]
QUALIFIERS = ["不", "未", "无", "非", "仅", "只", "至少", "至多", "最多", "最少", "超过", "低于", "高于", "不超过", "不低于", "约", "近", "以上", "以下", "除"]


def protected_tokens(text: str) -> list[str]:
    toks = []
    for rx in _PROTECT:
        toks += [m.group(0).strip() for m in rx.finditer(text or "") if m.group(0).strip()]
    return [t for t in toks if re.search(r"\d|[「“\"《\[]", t)]


def qualifiers(text: str) -> list[str]:
    return [q for q in QUALIFIERS if q in (text or "")]


def condense_ok(before: str, after: str) -> tuple[bool, str]:
    """精简后的文字必须保留全部数字、单位、引用和限定词。"""
    nb = norm(after)
    for t in protected_tokens(before):
        if norm(t) not in nb:
            return False, f"丢失了“{t}”"
    for q in qualifiers(before):
        if q not in after:
            return False, f"丢失了限定词“{q}”"
    if len(after) > len(before):
        return False, "精简后反而变长"
    return True, ""
