"""已有文件的原位修改：列出可修改的内容单元，按单元 ID 写回，不动其他部分。

- PPTX：文本框与占位符的段落、表格单元格、原生图表数据、备注；删页、复制页、调整页序。
- DOCX：正文段落（沿用段落样式和第一个文字片段的格式）、表格单元格段落、页眉页脚；可选修订模式。
  文本框、域、内容控件中的段落不列为可修改单元，原样保留。
- XLSX：单元格值与公式（openpyxl 写回，前后比对元素清单）。
"""
from __future__ import annotations

import copy
import datetime as dt
from pathlib import Path

from lxml import etree

from ..util import UserError

A = "http://schemas.openxmlformats.org/drawingml/2006/main"
WNS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
MAX_UNITS = 5000


def _q(ns, tag):
    return f"{{{ns}}}{tag}"


# ======================= PPTX =======================

def pptx_units(path: Path) -> list[dict]:
    from pptx import Presentation
    from pptx.enum.shapes import MSO_SHAPE_TYPE

    prs = Presentation(path)
    units: list[dict] = []

    def walk(shapes, si):
        for sh in shapes:
            if sh.shape_type == MSO_SHAPE_TYPE.GROUP:
                walk(sh.shapes, si)
                continue
            if sh.has_text_frame:
                for pi, p in enumerate(sh.text_frame.paragraphs):
                    t = "".join(r.text for r in p.runs)
                    if t.strip():
                        units.append({"id": f"s{si}/sh{sh.shape_id}/p{pi}", "kind": "text", "slide": si, "text": t,
                                      "shape": sh.name})
            if getattr(sh, "has_table", False) and sh.has_table:
                for r, row in enumerate(sh.table.rows):
                    for c, cell in enumerate(row.cells):
                        for pi, p in enumerate(cell.text_frame.paragraphs):
                            t = "".join(x.text for x in p.runs)
                            if t.strip():
                                units.append({"id": f"s{si}/sh{sh.shape_id}/t{r}.{c}/p{pi}", "kind": "table", "slide": si, "text": t})
            if getattr(sh, "has_chart", False) and sh.has_chart:
                try:
                    plot = sh.chart.plots[0]
                    units.append({"id": f"s{si}/sh{sh.shape_id}/chart", "kind": "chart", "slide": si,
                                  "categories": [str(c) for c in plot.categories],
                                  "series": [{"name": s.name, "values": list(s.values)} for s in plot.series]})
                except Exception:
                    pass

    for si, slide in enumerate(prs.slides, 1):
        walk(slide.shapes, si)
        if slide.has_notes_slide:
            t = slide.notes_slide.notes_text_frame.text
            if t.strip():
                units.append({"id": f"s{si}/notes", "kind": "notes", "slide": si, "text": t})
        if len(units) > MAX_UNITS:
            break
    return units


def _find_shape(shapes, sid: int):
    from pptx.enum.shapes import MSO_SHAPE_TYPE
    for sh in shapes:
        if sh.shape_id == sid:
            return sh
        if sh.shape_type == MSO_SHAPE_TYPE.GROUP:
            f = _find_shape(sh.shapes, sid)
            if f is not None:
                return f
    return None


def _set_para_text(p, text: str) -> None:
    """保留第一个文字片段的格式，替换段落文字。"""
    runs = p.runs
    if not runs:
        r = p.add_run()
        r.text = text
        return
    runs[0].text = text
    for r in runs[1:]:
        r._r.getparent().remove(r._r)
    # 去掉段内换行，避免残留
    for br in p._p.findall(_q(A, "br")):
        p._p.remove(br)


def pptx_apply(src: Path, dst: Path, ops: list[dict]) -> dict:
    from pptx import Presentation
    from pptx.chart.data import CategoryChartData

    prs = Presentation(src)
    slides = list(prs.slides)
    applied, skipped = 0, []
    structural = []
    for op in ops:
        k = op.get("op")
        try:
            if k in ("set_text", "set_chart_data"):
                uid = op["unit"]
                parts = uid.split("/")
                si = int(parts[0][1:])
                slide = slides[si - 1]
                if parts[1] == "notes":
                    slide.notes_slide.notes_text_frame.text = op["text"]
                    applied += 1
                    continue
                sh = _find_shape(slide.shapes, int(parts[1][2:]))
                if sh is None:
                    raise KeyError(uid)
                if k == "set_chart_data":
                    cd = CategoryChartData()
                    cd.categories = op["categories"]
                    for s in op["series"]:
                        cd.add_series(s.get("name", ""), s["values"])
                    sh.chart.replace_data(cd)
                elif parts[2].startswith("t"):
                    r, c = map(int, parts[2][1:].split("."))
                    para = sh.table.cell(r, c).text_frame.paragraphs[int(parts[3][1:])]
                    _set_para_text(para, op["text"])
                else:
                    para = sh.text_frame.paragraphs[int(parts[2][1:])]
                    _set_para_text(para, op["text"])
                applied += 1
            elif k == "set_notes":
                slides[int(op["index"]) - 1].notes_slide.notes_text_frame.text = op["text"]
                applied += 1
            elif k in ("delete_slide", "duplicate_slide", "move_slide"):
                structural.append(op)
            else:
                skipped.append(f"不支持的操作 {k}")
        except (KeyError, IndexError, ValueError, AttributeError) as e:
            skipped.append(f"{op.get('unit') or op.get('index')}：{type(e).__name__}")
    # 结构操作按页码从大到小执行，避免序号错位
    lst = prs.slides._sldIdLst
    ids = list(lst)
    deleted = 0
    for op in sorted(structural, key=lambda o: -int(o.get("index", 0))):
        i = int(op["index"]) - 1
        if not 0 <= i < len(ids):
            skipped.append(f"页码 {op['index']} 超出范围")
            continue
        if op["op"] == "delete_slide":
            el = ids[i]
            rid = el.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id")
            lst.remove(el)
            prs.part.drop_rel(rid)
            ids.pop(i)
            deleted += 1
            applied += 1
        elif op["op"] == "move_slide":
            el = ids.pop(i)
            to = max(0, min(len(ids), int(op["to"]) - 1))
            lst.remove(el)
            ids.insert(to, el)
            lst.insert(to, el)
            applied += 1
        elif op["op"] == "duplicate_slide":
            ok = _duplicate_slide(prs, i)
            if ok:
                applied += 1
                ids = list(lst)
            else:
                skipped.append(f"第 {op['index']} 页含图表或嵌入对象，暂不支持复制")
    prs.save(dst)
    return {"applied": applied, "skipped": skipped, "deleted_slides": deleted}


def _duplicate_slide(prs, i: int) -> bool:
    src = prs.slides[i]
    for rel in src.part.rels.values():
        if "chart" in rel.reltype or "oleObject" in rel.reltype or "package" in rel.reltype or "diagram" in rel.reltype:
            return False
    new = prs.slides.add_slide(src.slide_layout)
    for shp in list(new.shapes):
        shp._element.getparent().remove(shp._element)
    rid_map = {}
    for rid, rel in src.part.rels.items():
        if "slideLayout" in rel.reltype or "notesSlide" in rel.reltype:
            continue
        if rel.is_external:
            nrid = new.part.rels.get_or_add_ext_rel(rel.reltype, rel.target_ref)
        else:
            nrid = new.part.relate_to(rel.target_part, rel.reltype)
        rid_map[rid] = nrid
    xml = etree.tostring(src.shapes._spTree)
    for old, nw in rid_map.items():
        xml = xml.replace(f'"{old}"'.encode(), f'"{nw}"'.encode())
    tree = etree.fromstring(xml)
    new_tree = new.shapes._spTree
    for child in list(tree):
        if child.tag.endswith("}nvGrpSpPr") or child.tag.endswith("}grpSpPr"):
            continue
        new_tree.append(child)
    # 放到原页之后
    lst = prs.slides._sldIdLst
    el = list(lst)[-1]
    lst.remove(el)
    lst.insert(i + 1, el)
    return True


# ======================= DOCX =======================

def _docx_paras(doc):
    """正文与表格中的段落（排除文本框、内容控件、含域的段落）。"""
    body = doc.element.body
    out = []
    for p in body.iter(_q(WNS, "p")):
        anc = p.getparent()
        skip = False
        while anc is not None and anc is not body:
            tag = etree.QName(anc).localname
            if tag in ("txbxContent", "sdtContent"):
                skip = True
                break
            anc = anc.getparent()
        if skip:
            continue
        if p.find(".//" + _q(WNS, "fldChar")) is not None or p.find(".//" + _q(WNS, "fldSimple")) is not None:
            continue
        out.append(p)
    return out


def _p_text(p) -> str:
    return "".join(t.text or "" for t in p.iter(_q(WNS, "t")))


def docx_units(path: Path) -> list[dict]:
    from docx import Document

    d = Document(path)
    units = []
    for n, p in enumerate(_docx_paras(d)):
        t = _p_text(p)
        if t.strip():
            in_tbl = any(etree.QName(a).localname == "tbl" for a in p.iterancestors())
            style = p.find(f"{_q(WNS, 'pPr')}/{_q(WNS, 'pStyle')}")
            units.append({"id": f"p{n}", "kind": "table" if in_tbl else "text", "text": t,
                          "style": style.get(_q(WNS, "val")) if style is not None else ""})
        if len(units) > MAX_UNITS:
            break
    for si, sec in enumerate(d.sections):
        for kind, part in (("h", sec.header), ("f", sec.footer)):
            if part.is_linked_to_previous:
                continue
            for n, p in enumerate(part.paragraphs):
                if p.text.strip() and p._p.find(".//" + _q(WNS, "fldChar")) is None:
                    units.append({"id": f"{kind}{si}.{n}", "kind": "header" if kind == "h" else "footer", "text": p.text})
    return units


def docx_apply(src: Path, dst: Path, ops: list[dict], track: bool = False, author: str = "AI 修改") -> dict:
    from docx import Document

    d = Document(src)
    paras = _docx_paras(d)
    applied, skipped = 0, []
    rev = [1000]
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for op in ops:
        if op.get("op") != "set_text":
            skipped.append(f"Word 不支持的操作 {op.get('op')}")
            continue
        uid = op.get("unit", "")
        try:
            if uid.startswith("p"):
                p = paras[int(uid[1:])]
            else:
                kind, rest = uid[0], uid[1:]
                si, n = map(int, rest.split("."))
                sec = d.sections[si]
                p = (sec.header if kind == "h" else sec.footer).paragraphs[n]._p
            _docx_set(p, op["text"], track, author, now, rev)
            applied += 1
        except (IndexError, ValueError, KeyError) as e:
            skipped.append(f"{uid}：{type(e).__name__}")
    d.save(dst)
    return {"applied": applied, "skipped": skipped}


def _docx_set(p, text: str, track: bool, author: str, date: str, rev: list[int]) -> None:
    runs = [r for r in p if etree.QName(r).localname == "r"]
    rpr = None
    for r in runs:
        x = r.find(_q(WNS, "rPr"))
        if x is not None and r.find(_q(WNS, "t")) is not None:
            rpr = copy.deepcopy(x)
            break
    new_r = etree.SubElement(p, _q(WNS, "r"))
    if rpr is not None:
        new_r.append(rpr)
    t = etree.SubElement(new_r, _q(WNS, "t"))
    t.text = text
    t.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
    if not track:
        for r in runs:
            if r.find(_q(WNS, "drawing")) is None and r.find(_q(WNS, "object")) is None:
                p.remove(r)
        return
    # 修订模式：原文字包进 w:del，新文字包进 w:ins
    for r in runs:
        if r.find(_q(WNS, "drawing")) is not None:
            continue
        rev[0] += 1
        dl = etree.Element(_q(WNS, "del"))
        dl.set(_q(WNS, "id"), str(rev[0]))
        dl.set(_q(WNS, "author"), author)
        dl.set(_q(WNS, "date"), date)
        r.addprevious(dl)
        dl.append(r)
        for tt in r.findall(_q(WNS, "t")):
            tt.tag = _q(WNS, "delText")
    rev[0] += 1
    ins = etree.Element(_q(WNS, "ins"))
    ins.set(_q(WNS, "id"), str(rev[0]))
    ins.set(_q(WNS, "author"), author)
    ins.set(_q(WNS, "date"), date)
    new_r.addprevious(ins)
    ins.append(new_r)


# ======================= XLSX =======================

def xlsx_units(path: Path, max_cells: int = 3000) -> list[dict]:
    from openpyxl import load_workbook

    wb = load_workbook(path)
    units = []
    for ws in wb.worksheets:
        for row in ws.iter_rows():
            for c in row:
                if c.value is None:
                    continue
                units.append({"id": f"{ws.title}!{c.coordinate}", "kind": "cell", "text": str(c.value)})
                if len(units) >= max_cells:
                    return units
    return units


def xlsx_apply(src: Path, dst: Path, ops: list[dict]) -> dict:
    from openpyxl import load_workbook

    wb = load_workbook(src)
    applied, skipped = 0, []
    for op in ops:
        if op.get("op") not in ("set_cell", "set_text"):
            skipped.append(f"Excel 不支持的操作 {op.get('op')}")
            continue
        uid = op.get("unit", "")
        sheet, _, cell = uid.rpartition("!")
        try:
            ws = wb[sheet]
            v = op.get("value", op.get("text"))
            if isinstance(v, str) and v.startswith("="):
                from ..spec.workbook import FUNCTION_WHITELIST, formula_functions
                bad = formula_functions(v) - FUNCTION_WHITELIST
                if bad:
                    skipped.append(f"{uid}：使用了不在白名单内的函数 {', '.join(bad)}")
                    continue
            ws[cell] = v
            applied += 1
        except (KeyError, ValueError) as e:
            skipped.append(f"{uid}：{type(e).__name__}")
    wb.save(dst)
    return {"applied": applied, "skipped": skipped}


def units_for(kind: str, path: Path) -> list[dict]:
    if kind == "pptx":
        return pptx_units(path)
    if kind == "docx":
        return docx_units(path)
    if kind == "xlsx":
        return xlsx_units(path)
    raise UserError("该格式不支持原位修改")


def apply(kind: str, src: Path, dst: Path, ops: list[dict], track: bool = False) -> dict:
    if kind == "pptx":
        return pptx_apply(src, dst, ops)
    if kind == "docx":
        return docx_apply(src, dst, ops, track=track)
    if kind == "xlsx":
        return xlsx_apply(src, dst, ops)
    raise UserError("该格式不支持原位修改")
