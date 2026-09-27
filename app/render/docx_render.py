"""DOCX 渲染器：规格 → Word 文档。

- 标题使用真实的“标题 1–3”样式；目录为 TOC 域，缓存内容由两轮渲染填入页码。
- 每个块写入书签 dw_<块ID>，导出 PDF 时成为命名目标，用于把预览页映射回块。
- 公式为原生 OMML，不支持的写法降级为图片；图表为图片并附带 XLSX 数据。
"""
from __future__ import annotations

import copy
import io
import re
from dataclasses import dataclass, field
from pathlib import Path

from docx import Document as DocxDocument
from docx.enum.section import WD_ORIENT
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_BREAK, WD_LINE_SPACING, WD_TAB_ALIGNMENT, WD_TAB_LEADER
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Emu, Mm, Pt, RGBColor

from ..spec.common import ChartSpec, Theme
from ..spec.document import Document
from .charts import chart_png
from .fonts import FONT_SCHEMES, NON_EXACT
from .omml import UnsupportedLatex, latex_to_omath, latex_to_omath_para


@dataclass
class DocRenderResult:
    path: Path
    headings: list[dict] = field(default_factory=list)      # [{id, level, text}]
    charts: list[tuple[str, ChartSpec]] = field(default_factory=list)
    equation_images: list[str] = field(default_factory=list)  # 降级为图片的公式块 ID
    fonts: set[str] = field(default_factory=set)
    warnings: list[str] = field(default_factory=list)


# 各预设的版式参数
def preset_config(preset: str, font_mode: str) -> dict:
    sc = FONT_SCHEMES[font_mode]
    sans, serif, latin_sans, latin_serif = sc["sans"], sc["serif"], sc["latin_sans"], sc["latin_serif"]
    base = dict(
        page=(210, 297), margins=(25.4, 25.4, 31.8, 31.8),  # 上 下 左 右（mm）
        body_font=serif, body_latin=latin_serif, body_size=12, line=1.5, first_indent=True,
        h_font=sans, h_latin=latin_sans, h_sizes=(16, 14, 12), h_color="000000", h_bold=True,
        title_font=sans, title_size=22, table_size=10.5, caption_size=10.5, accent="1F4E79",
        page_fmt="{PAGE}", header_line=True,
    )
    if preset == "proposal":
        base.update(body_font=sans, body_latin=latin_sans, body_size=11, line=1.4, h_color="1F4E79", accent="1F4E79", title_size=26)
    elif preset == "paper":
        base.update(body_size=12, line=1.5, h_sizes=(15, 13.5, 12), title_size=18, margins=(25, 25, 30, 25))
    elif preset == "minutes":
        base.update(body_font=sans, body_latin=latin_sans, body_size=11, line=1.3, first_indent=False, title_size=20, h_sizes=(14, 12, 11))
    elif preset == "manual":
        base.update(body_font=sans, body_latin=latin_sans, body_size=11, line=1.35, first_indent=False, h_color="0F4C81", accent="0F4C81", title_size=24)
    elif preset == "official":
        # GB/T 9704 党政机关公文格式（主要参数）
        base.update(
            margins=(37, 35, 28, 26), body_font="仿宋_GB2312" if font_mode == "system" else serif, body_latin=latin_serif,
            body_size=16, line=None, line_exact=28.95, first_indent=True, h_font="黑体" if font_mode == "system" else sans,
            h_sizes=(16, 16, 16), h_bold=False, title_font="方正小标宋简体" if font_mode == "system" else serif, title_size=22,
            table_size=14, caption_size=14, page_fmt="— {PAGE} —", header_line=False, h2_font="楷体_GB2312" if font_mode == "system" else serif,
        )
    return base


def render_document(doc: Document, out: Path, assets: dict[str, Path] | None = None, tmp: Path | None = None,
                    toc_pages: dict[str, int] | None = None, theme: Theme | None = None) -> DocRenderResult:
    assets = {k: Path(v) for k, v in (assets or {}).items()}
    tmp = tmp or out.parent
    tmp.mkdir(parents=True, exist_ok=True)
    cfg = preset_config(doc.preset, doc.font_mode)
    theme = theme or Theme()
    res = DocRenderResult(path=out)
    d = DocxDocument()
    _setup_page(d, cfg)
    _setup_styles(d, cfg, res)
    num = _Numbering(d)
    ctx = {"table": 0, "figure": 0, "equation": 0, "bm": 100}

    _title_block(d, doc, cfg, res)
    headings = [b for b in doc.blocks if b.type == "heading"]
    res.headings = [{"id": b.id, "level": b.level, "text": b.text} for b in headings]

    for b in doc.blocks:
        t = b.type
        start = len(d.element.body)
        if t == "heading":
            p = d.add_paragraph(style=f"Heading {b.level}")
            _inline(p, b.text, cfg, res, heading=True, level=b.level)
            _bookmark(p, f"dw{b.id}", ctx)
        elif t == "paragraph":
            p = d.add_paragraph(style="Normal")
            p.alignment = {"left": WD_ALIGN_PARAGRAPH.LEFT, "center": WD_ALIGN_PARAGRAPH.CENTER,
                           "right": WD_ALIGN_PARAGRAPH.RIGHT, "justify": WD_ALIGN_PARAGRAPH.JUSTIFY}[b.align]
            _inline(p, b.text, cfg, res)
            _bookmark(p, f"dw{b.id}", ctx)
        elif t == "list":
            nid = num.new(b.ordered)
            first = None
            for it in b.items:
                p = d.add_paragraph(style="Normal")
                _list_para(p, nid, 0, cfg)
                _inline(p, it.text, cfg, res)
                first = first or p
                for sub in it.sub:
                    ps = d.add_paragraph(style="Normal")
                    _list_para(ps, nid, 1, cfg)
                    _inline(ps, sub, cfg, res)
            _bookmark(first, f"dw{b.id}", ctx)
        elif t == "table":
            ctx["table"] += 1
            cap = d.add_paragraph(style="Caption")
            cap.alignment = WD_ALIGN_PARAGRAPH.CENTER
            _caption(cap, "表", ctx["table"], b.table.caption, cfg)
            _keep_next(cap)
            _bookmark(cap, f"dw{b.id}", ctx)
            _table(d, b.table, cfg, theme)
        elif t == "image":
            p = d.add_paragraph()
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            _keep_next(p)
            _no_indent(p)
            src = assets.get(b.image.asset) if b.image.asset else None
            width = _text_width(d) * b.width_pct / 100
            if src and src.exists():
                p.add_run().add_picture(str(_fit_image(src, tmp)), width=Emu(int(width)))
            else:
                r = p.add_run(f"［图片：{b.image.alt or b.image.query or '待补充'}］")
                r.font.color.rgb = RGBColor(0x9C, 0xA3, 0xAF)
                res.warnings.append(f"图片“{b.caption or b.image.query}”没有可用素材，已插入占位文字")
            _bookmark(p, f"dw{b.id}", ctx)
            ctx["figure"] += 1
            cap = d.add_paragraph(style="Caption")
            cap.alignment = WD_ALIGN_PARAGRAPH.CENTER
            _caption(cap, "图", ctx["figure"], b.caption, cfg)
        elif t == "chart":
            p = d.add_paragraph()
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            _keep_next(p)
            _no_indent(p)
            cw = int(_text_width(d) * 0.92)
            if b.chart.native:
                # 原生 Word 图表：可在 Word / WPS 中“编辑数据”
                _add_native_chart(d, p, b.chart, theme, cw, int(cw * 3.4 / 6.2), ctx)
            else:
                # 瀑布图、桑基图等原生不支持的类型：图片，数据另附 XLSX
                png = chart_png(b.chart, theme, width_in=6.2, height_in=3.4)
                ip = tmp / f"chart_{b.id}.png"
                ip.write_bytes(png)
                p.add_run().add_picture(str(ip), width=Emu(cw))
            _bookmark(p, f"dw{b.id}", ctx)
            ctx["figure"] += 1
            cap = d.add_paragraph(style="Caption")
            cap.alignment = WD_ALIGN_PARAGRAPH.CENTER
            _caption(cap, "图", ctx["figure"], b.caption or b.chart.title, cfg)
            res.charts.append((f"图{ctx['figure']}", b.chart))
        elif t == "equation":
            p = d.add_paragraph()
            _no_indent(p)
            if b.number:
                ctx["equation"] += 1
            try:
                if b.number:
                    tw = _text_width(d)
                    pf = p.paragraph_format
                    pf.tab_stops.add_tab_stop(Emu(int(tw / 2)), WD_TAB_ALIGNMENT.CENTER)
                    pf.tab_stops.add_tab_stop(Emu(int(tw)), WD_TAB_ALIGNMENT.RIGHT)
                    p.add_run().add_tab()
                    p._p.append(latex_to_omath(b.latex))
                    r = p.add_run()
                    r.add_tab()
                    r.add_text(f"({ctx['equation']})")
                else:
                    p._p.append(latex_to_omath_para(b.latex))
            except UnsupportedLatex:
                p = _equation_image(d, p, b, ctx, tmp, res)
            _bookmark(p, f"dw{b.id}", ctx)
        elif t == "quote":
            p = d.add_paragraph(style="Normal")
            _no_indent(p)
            pf = p.paragraph_format
            pf.left_indent = Cm(1.0)
            pf.right_indent = Cm(1.0)
            _left_border(p, cfg["accent"])
            _inline(p, b.text, cfg, res, italic=True)
            _bookmark(p, f"dw{b.id}", ctx)
            if b.source:
                ps = d.add_paragraph(style="Normal")
                ps.alignment = WD_ALIGN_PARAGRAPH.RIGHT
                _no_indent(ps)
                _inline(ps, "—— " + b.source, cfg, res)
        elif t == "page_break":
            p = d.add_paragraph()
            p.add_run().add_break(WD_BREAK.PAGE)
            _bookmark(p, f"dw{b.id}", ctx)
        elif t == "toc":
            _toc(d, b, headings, toc_pages or {}, cfg, ctx, res)
        elif t == "references":
            hp = d.add_paragraph(style="Heading 1")
            _inline(hp, "参考文献", cfg, res, heading=True, level=1)
            _bookmark(hp, f"dw{b.id}", ctx)
            for i, item in enumerate(b.items, 1):
                p = d.add_paragraph(style="Normal")
                pf = p.paragraph_format
                pf.first_line_indent = Cm(-0.9)
                pf.left_indent = Cm(0.9)
                _inline(p, f"[{i}] {item}", cfg, res)
        _ = start

    _header_footer(d, doc, cfg, assets, tmp)
    _update_fields_on_open(d)
    d.core_properties.title = doc.title
    d.core_properties.author = doc.meta.author or "DocWork"
    d.save(out)
    for f in (cfg["body_font"], cfg["h_font"], cfg["title_font"], cfg.get("h2_font", "")):
        if f in NON_EXACT:
            res.fonts.add(f)
    return res


# ---------- 页面与样式 ----------

def _setup_page(d, cfg):
    s = d.sections[0]
    s.orientation = WD_ORIENT.PORTRAIT
    s.page_width, s.page_height = Mm(cfg["page"][0]), Mm(cfg["page"][1])
    top, bottom, left, right = cfg["margins"]
    s.top_margin, s.bottom_margin, s.left_margin, s.right_margin = Mm(top), Mm(bottom), Mm(left), Mm(right)
    s.header_distance = Mm(15)
    s.footer_distance = Mm(15)


def _set_rfonts(el_rpr, latin: str, ea: str):
    rf = el_rpr.find(qn("w:rFonts"))
    if rf is None:
        rf = OxmlElement("w:rFonts")
        el_rpr.insert(0, rf)
    for k in ("w:ascii", "w:hAnsi", "w:cs"):
        rf.set(qn(k), latin)
    rf.set(qn("w:eastAsia"), ea)
    for k in ("w:asciiTheme", "w:hAnsiTheme", "w:eastAsiaTheme", "w:cstheme"):
        if rf.get(qn(k)) is not None:
            del rf.attrib[qn(k)]


def _style_font(style, latin, ea, size, bold=None, color=None, italic=None):
    f = style.font
    f.name = latin
    f.size = Pt(size)
    if bold is not None:
        f.bold = bold
    if italic is not None:
        f.italic = italic
    if color:
        f.color.rgb = RGBColor.from_string(color)
    _set_rfonts(style.element.get_or_add_rPr(), latin, ea)


def _setup_styles(d, cfg, res):
    st = d.styles
    normal = st["Normal"]
    _style_font(normal, cfg["body_latin"], cfg["body_font"], cfg["body_size"])
    pf = normal.paragraph_format
    if cfg.get("line"):
        pf.line_spacing = cfg["line"]
    else:
        pf.line_spacing_rule = WD_LINE_SPACING.EXACTLY
        pf.line_spacing = Pt(cfg["line_exact"])
    pf.space_after = Pt(0 if cfg.get("line_exact") else 4)
    pf.space_before = Pt(0)
    if cfg["first_indent"]:
        # 首行缩进 2 字符
        ind = normal.element.get_or_add_pPr().get_or_add_ind()
        ind.set(qn("w:firstLineChars"), "200")
        ind.set(qn("w:firstLine"), str(int(cfg["body_size"] * 2 * 20)))
    for lv in (1, 2, 3):
        h = st[f"Heading {lv}"]
        font = cfg.get("h2_font") if (lv == 2 and cfg.get("h2_font")) else cfg["h_font"]
        _style_font(h, cfg["h_latin"], font, cfg["h_sizes"][lv - 1], bold=cfg["h_bold"] or lv == 3 and cfg.get("h2_font") is not None, color=cfg["h_color"], italic=False)
        hpf = h.paragraph_format
        hpf.space_before = Pt(12 if lv == 1 else 8)
        hpf.space_after = Pt(6 if lv == 1 else 4)
        hpf.keep_with_next = True
        if cfg.get("line_exact"):
            hpf.line_spacing_rule = WD_LINE_SPACING.EXACTLY
            hpf.line_spacing = Pt(cfg["line_exact"])
            hpf.space_before = Pt(0)
            hpf.space_after = Pt(0)
        else:
            hpf.line_spacing = 1.3
        ind = h.element.get_or_add_pPr().get_or_add_ind()
        ind.set(qn("w:firstLine"), "0")
    title = st["Title"]
    _style_font(title, cfg["h_latin"], cfg["title_font"], cfg["title_size"], bold=cfg.get("h_bold", True), color="000000" if cfg.get("line_exact") else cfg["h_color"])
    tpf = title.paragraph_format
    tpf.alignment = WD_ALIGN_PARAGRAPH.CENTER
    tpf.space_after = Pt(12)
    # 去掉默认标题样式的下边框
    ppr = title.element.get_or_add_pPr()
    for b in ppr.findall(qn("w:pBdr")):
        ppr.remove(b)
    cap = st["Caption"]
    _style_font(cap, cfg["body_latin"], cfg["h_font"], cfg["caption_size"], bold=False, color="333333", italic=False)
    cpf = cap.paragraph_format
    cpf.space_before = Pt(3)
    cpf.space_after = Pt(8)
    cind = cap.element.get_or_add_pPr().get_or_add_ind()
    cind.set(qn("w:firstLine"), "0")
    cind.set(qn("w:firstLineChars"), "0")


class _Numbering:
    """为每个列表创建独立的编号实例，使有序列表各自从 1 开始。"""

    def __init__(self, d):
        self.part = d.part.numbering_part
        self.root = self.part.element
        self.abs_ids = {}
        for ordered in (False, True):
            self.abs_ids[ordered] = self._abstract(ordered)

    def _abstract(self, ordered: bool) -> int:
        existing = [int(a.get(qn("w:abstractNumId"))) for a in self.root.findall(qn("w:abstractNum"))]
        aid = max(existing + [0]) + 1
        an = OxmlElement("w:abstractNum")
        an.set(qn("w:abstractNumId"), str(aid))
        mlt = OxmlElement("w:multiLevelType")
        mlt.set(qn("w:val"), "hybridMultilevel")
        an.append(mlt)
        for lvl in range(2):
            lv = OxmlElement("w:lvl")
            lv.set(qn("w:ilvl"), str(lvl))
            start = OxmlElement("w:start")
            start.set(qn("w:val"), "1")
            fmt = OxmlElement("w:numFmt")
            txt = OxmlElement("w:lvlText")
            if ordered:
                fmt.set(qn("w:val"), "decimal" if lvl == 0 else "lowerLetter")
                txt.set(qn("w:val"), f"%{lvl + 1}." if lvl == 0 else f"%{lvl + 1})")
            else:
                fmt.set(qn("w:val"), "bullet")
                txt.set(qn("w:val"), "•" if lvl == 0 else "◦")
            jc = OxmlElement("w:lvlJc")
            jc.set(qn("w:val"), "left")
            ppr = OxmlElement("w:pPr")
            ind = OxmlElement("w:ind")
            ind.set(qn("w:left"), str(420 + 420 * lvl))
            ind.set(qn("w:hanging"), "420")
            ppr.append(ind)
            for el in (start, fmt, txt, jc, ppr):
                lv.append(el)
            if not ordered:
                rpr = OxmlElement("w:rPr")
                rf = OxmlElement("w:rFonts")
                rf.set(qn("w:ascii"), "Arial")
                rf.set(qn("w:hAnsi"), "Arial")
                rpr.append(rf)
                lv.append(rpr)
            an.append(lv)
        # abstractNum 必须位于所有 num 之前
        first_num = self.root.find(qn("w:num"))
        if first_num is not None:
            first_num.addprevious(an)
        else:
            self.root.append(an)
        return aid

    def new(self, ordered: bool) -> int:
        existing = [int(n.get(qn("w:numId"))) for n in self.root.findall(qn("w:num"))]
        nid = max(existing + [0]) + 1
        n = OxmlElement("w:num")
        n.set(qn("w:numId"), str(nid))
        a = OxmlElement("w:abstractNumId")
        a.set(qn("w:val"), str(self.abs_ids[ordered]))
        n.append(a)
        if ordered:
            ov = OxmlElement("w:lvlOverride")
            ov.set(qn("w:ilvl"), "0")
            so = OxmlElement("w:startOverride")
            so.set(qn("w:val"), "1")
            ov.append(so)
            n.append(ov)
        self.root.append(n)
        return nid


def _list_para(p, nid: int, level: int, cfg):
    ppr = p._p.get_or_add_pPr()
    numpr = OxmlElement("w:numPr")
    il = OxmlElement("w:ilvl")
    il.set(qn("w:val"), str(level))
    ni = OxmlElement("w:numId")
    ni.set(qn("w:val"), str(nid))
    numpr.append(il)
    numpr.append(ni)
    ppr.insert(0, numpr) if ppr.find(qn("w:pStyle")) is None else ppr.find(qn("w:pStyle")).addnext(numpr)
    ind = ppr.get_or_add_ind()
    ind.set(qn("w:left"), str(420 + 420 * level + (420 if cfg["first_indent"] else 0)))
    ind.set(qn("w:hanging"), "420")
    ind.set(qn("w:firstLineChars"), "0")
    ind.set(qn("w:firstLine"), "0")


# ---------- 行内文字 ----------

_INLINE = re.compile(r"(\*\*.+?\*\*|\*[^*]+?\*|\^[^^]+?\^|~[^~]+?~)")


def _inline(p, text: str, cfg, res, heading=False, level=0, italic=False):
    for seg in _INLINE.split(text or ""):
        if not seg:
            continue
        bold = it = sup = sub = False
        if seg.startswith("**") and seg.endswith("**") and len(seg) > 4:
            seg, bold = seg[2:-2], True
        elif seg.startswith("*") and seg.endswith("*") and len(seg) > 2:
            seg, it = seg[1:-1], True
        elif seg.startswith("^") and seg.endswith("^") and len(seg) > 2:
            seg, sup = seg[1:-1], True
        elif seg.startswith("~") and seg.endswith("~") and len(seg) > 2:
            seg, sub = seg[1:-1], True
        r = p.add_run(seg)
        if bold:
            r.bold = True
        if it or italic:
            r.italic = True
        if sup:
            r.font.superscript = True
        if sub:
            r.font.subscript = True


def _add_native_chart(d, p, spec, theme, cx: int, cy: int, ctx: dict) -> None:
    from docx.opc.constants import CONTENT_TYPE as CT, RELATIONSHIP_TYPE as RT
    from docx.opc.packuri import PackURI
    from docx.opc.part import Part
    from docx.oxml import parse_xml

    from .charts import native_chart_parts

    chart_xml, xlsx_blob, xrid = native_chart_parts(spec, theme, cx, cy, font_size=10)
    n = ctx["chart_seq"] = ctx.get("chart_seq", 0) + 1
    pkg = d.part.package
    xpart = Part(PackURI(f"/word/embeddings/Microsoft_Excel_Worksheet{n}.xlsx"), CT.SML_SHEET, xlsx_blob, pkg)
    cpart = Part(PackURI(f"/word/charts/chart{n}.xml"), CT.DML_CHART, chart_xml, pkg)
    cpart.rels.add_relationship(RT.PACKAGE, xpart, xrid)
    rid = d.part.relate_to(cpart, RT.CHART)
    shape_id = d.part.next_id
    xml = (
        '<w:drawing xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
        'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" '
        'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
        'xmlns:c="http://schemas.openxmlformats.org/drawingml/2006/chart" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f'<wp:inline distT="0" distB="0" distL="0" distR="0"><wp:extent cx="{cx}" cy="{cy}"/>'
        '<wp:effectExtent l="0" t="0" r="0" b="0"/>'
        f'<wp:docPr id="{shape_id}" name="图表 {n}"/><wp:cNvGraphicFramePr/>'
        '<a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/chart">'
        f'<c:chart r:id="{rid}"/></a:graphicData></a:graphic></wp:inline></w:drawing>'
    )
    p.add_run()._r.append(parse_xml(xml))


def _keep_next(p):
    p.paragraph_format.keep_with_next = True


def _no_indent(p):
    ind = p._p.get_or_add_pPr().get_or_add_ind()
    ind.set(qn("w:firstLine"), "0")
    ind.set(qn("w:firstLineChars"), "0")


def _left_border(p, color: str):
    ppr = p._p.get_or_add_pPr()
    bdr = OxmlElement("w:pBdr")
    left = OxmlElement("w:left")
    left.set(qn("w:val"), "single")
    left.set(qn("w:sz"), "18")
    left.set(qn("w:space"), "8")
    left.set(qn("w:color"), color)
    bdr.append(left)
    ppr.append(bdr)


def _bookmark(p, name: str, ctx):
    if p is None:
        return
    ctx["bm"] += 1
    start = OxmlElement("w:bookmarkStart")
    start.set(qn("w:id"), str(ctx["bm"]))
    start.set(qn("w:name"), name[:40])
    end = OxmlElement("w:bookmarkEnd")
    end.set(qn("w:id"), str(ctx["bm"]))
    ppr = p._p.find(qn("w:pPr"))
    if ppr is not None:
        ppr.addnext(start)
    else:
        p._p.insert(0, start)
    p._p.append(end)


def _field_runs(p, instr: str, cached: str = "1", size: float | None = None):
    """插入域：begin / instrText / separate / 缓存结果 / end。"""
    def fld(kind):
        r = OxmlElement("w:r")
        fc = OxmlElement("w:fldChar")
        fc.set(qn("w:fldCharType"), kind)
        r.append(fc)
        return r
    p._p.append(fld("begin"))
    r = OxmlElement("w:r")
    it = OxmlElement("w:instrText")
    it.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
    it.text = f" {instr} "
    r.append(it)
    p._p.append(r)
    p._p.append(fld("separate"))
    run = p.add_run(cached)
    if size:
        run.font.size = Pt(size)
    p._p.append(fld("end"))


def _caption(p, label: str, n: int, text: str, cfg):
    p.add_run(f"{label} ")
    _field_runs(p, f"SEQ {label} \\* ARABIC", str(n))
    if text:
        p.add_run(f"  {text}")


# ---------- 表格 ----------

def _table(d, spec, cfg, theme: Theme):
    ncol = len(spec.columns)
    rows = [spec.columns] + [["" if v is None else _fmt(v) for v in r] for r in spec.rows]
    t = d.add_table(rows=len(rows), cols=ncol)
    t.style = d.styles["Table Grid"]
    t.alignment = WD_TABLE_ALIGNMENT.CENTER
    tw = _text_width(d)
    weights = [max(2, min(20, max(_disp_len(str(r[c])) for r in rows))) for c in range(ncol)]
    total = sum(weights)
    head_fill = theme.primary.lstrip("#") if not cfg.get("line_exact") else "D9D9D9"
    for ri, r in enumerate(rows):
        row = t.rows[ri]
        if ri == 0:
            trpr = row._tr.get_or_add_trPr()
            th = OxmlElement("w:tblHeader")
            th.set(qn("w:val"), "true")
            trpr.append(th)
        for ci, val in enumerate(r):
            cell = row.cells[ci]
            cell.width = Emu(int(tw * weights[ci] / total))
            p = cell.paragraphs[0]
            _no_indent(p)
            pf = p.paragraph_format
            pf.space_after = Pt(0)
            pf.line_spacing = 1.15 if not cfg.get("line_exact") else None
            if cfg.get("line_exact"):
                pf.line_spacing_rule = WD_LINE_SPACING.SINGLE
            num = bool(re.fullmatch(r"[-+]?[\d,]*\.?\d+%?", str(val).strip())) if val != "" else False
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER if ri == 0 else (WD_ALIGN_PARAGRAPH.RIGHT if num else WD_ALIGN_PARAGRAPH.LEFT)
            run = p.add_run(str(val))
            run.font.size = Pt(cfg["table_size"])
            if ri == 0:
                run.bold = True
                if not cfg.get("line_exact"):
                    run.font.color.rgb = RGBColor(0xFF, 0xFF, 0xFF)
                _shade(cell, head_fill)
            elif ri % 2 == 0 and not cfg.get("line_exact"):
                _shade(cell, "F2F5F9")
    # 表格后留一点间距
    sp = d.add_paragraph()
    sp.paragraph_format.space_after = Pt(0)
    sp.paragraph_format.line_spacing = 0.8
    return t


def _shade(cell, fill: str):
    tcpr = cell._tc.get_or_add_tcPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), fill)
    tcpr.append(shd)


def _fmt(v) -> str:
    if isinstance(v, float):
        return f"{v:.4f}".rstrip("0").rstrip(".")
    return str(v)


def _disp_len(s: str) -> int:
    return sum(2 if ord(c) > 0x2E80 else 1 for c in s)


def _text_width(d) -> int:
    s = d.sections[0]
    return int(s.page_width - s.left_margin - s.right_margin)


# ---------- 标题、目录、页眉页脚 ----------

def _title_block(d, doc: Document, cfg, res):
    m = doc.meta
    if doc.preset == "official":
        if m.issuer:
            p = d.add_paragraph()
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            _no_indent(p)
            r = p.add_run(m.issuer)
            r.font.size = Pt(26)
            r.font.color.rgb = RGBColor(0xC0, 0x00, 0x00)
            r.bold = True
        if m.doc_number:
            p = d.add_paragraph()
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            _no_indent(p)
            p.add_run(m.doc_number)
        p = d.add_paragraph(style="Title")
        _inline(p, doc.title, cfg, res)
        if m.recipients:
            p = d.add_paragraph()
            _no_indent(p)
            p.add_run(m.recipients + "：")
        return
    p = d.add_paragraph(style="Title")
    _inline(p, doc.title, cfg, res)
    sub = [x for x in (m.subtitle,) if x]
    info = "　".join(x for x in (m.organization, m.author, m.date) if x)
    for line in sub + ([info] if info else []):
        q = d.add_paragraph()
        q.alignment = WD_ALIGN_PARAGRAPH.CENTER
        _no_indent(q)
        r = q.add_run(line)
        r.font.size = Pt(max(10.5, cfg["body_size"] - 1))
        r.font.color.rgb = RGBColor(0x55, 0x55, 0x55)
    if sub or info:
        d.add_paragraph().paragraph_format.space_after = Pt(6)


def _toc(d, b, headings, toc_pages: dict[str, int], cfg, ctx, res):
    tp = d.add_paragraph()
    tp.alignment = WD_ALIGN_PARAGRAPH.CENTER
    _no_indent(tp)
    r = tp.add_run(b.title or "目录")
    r.bold = True
    r.font.size = Pt(cfg["h_sizes"][0])
    _bookmark(tp, f"dw{b.id}", ctx)
    tw = _text_width(d)
    entries = [h for h in headings if h.level <= 3]
    paras = []
    if not entries:
        entries_text = [("（没有标题）", 1, "")]
    else:
        entries_text = [(h.text, h.level, str(toc_pages.get(h.id, ""))) for h in entries]
    for text, lvl, page in entries_text:
        p = d.add_paragraph()
        _no_indent(p)
        pf = p.paragraph_format
        pf.left_indent = Cm(0.74 * (lvl - 1))
        pf.space_after = Pt(2)
        pf.tab_stops.add_tab_stop(Emu(tw), WD_TAB_ALIGNMENT.RIGHT, WD_TAB_LEADER.DOTS)
        paras.append((p, text, page))
    # TOC 域：begin 放在第一项之前，end 放在最后一项之后
    first, last = paras[0][0], paras[-1][0]

    def fld(kind):
        rr = OxmlElement("w:r")
        fc = OxmlElement("w:fldChar")
        fc.set(qn("w:fldCharType"), kind)
        if kind == "begin":
            fc.set(qn("w:dirty"), "true")
        rr.append(fc)
        return rr
    first._p.append(fld("begin"))
    ir = OxmlElement("w:r")
    it = OxmlElement("w:instrText")
    it.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
    it.text = ' TOC \\o "1-3" \\h \\z \\u '
    ir.append(it)
    first._p.append(ir)
    first._p.append(fld("separate"))
    for p, text, page in paras:
        p.add_run(text)
        rr = p.add_run()
        rr.add_tab()
        p.add_run(page)
    last._p.append(fld("end"))
    pb = d.add_paragraph()
    pb.add_run().add_break(WD_BREAK.PAGE)


def _header_footer(d, doc: Document, cfg, assets, tmp):
    s = d.sections[0]
    if doc.header_text or doc.logo_asset:
        hp = s.header.paragraphs[0]
        hp.alignment = WD_ALIGN_PARAGRAPH.RIGHT
        _no_indent(hp)
        if doc.logo_asset and doc.logo_asset in assets:
            hp.add_run().add_picture(str(_fit_image(assets[doc.logo_asset], tmp)), height=Mm(8))
            hp.add_run("  ")
        if doc.header_text:
            r = hp.add_run(doc.header_text)
            r.font.size = Pt(9)
            r.font.color.rgb = RGBColor(0x66, 0x66, 0x66)
        if cfg["header_line"]:
            ppr = hp._p.get_or_add_pPr()
            bdr = OxmlElement("w:pBdr")
            bot = OxmlElement("w:bottom")
            for k, v in (("w:val", "single"), ("w:sz", "4"), ("w:space", "1"), ("w:color", "999999")):
                bot.set(qn(k), v)
            bdr.append(bot)
            ppr.append(bdr)
    if doc.page_numbers:
        fp = s.footer.paragraphs[0]
        fp.alignment = WD_ALIGN_PARAGRAPH.CENTER
        _no_indent(fp)
        before, _, after = cfg["page_fmt"].partition("{PAGE}")
        size = 14 if doc.preset == "official" else 9
        if before:
            fp.add_run(before).font.size = Pt(size)
        _field_runs(fp, "PAGE", "1", size)
        if after:
            fp.add_run(after).font.size = Pt(size)


def _update_fields_on_open(d):
    settings = d.settings.element
    uf = settings.find(qn("w:updateFields"))
    if uf is None:
        uf = OxmlElement("w:updateFields")
        settings.append(uf)
    uf.set(qn("w:val"), "true")


# ---------- 图片 ----------

def _fit_image(src: Path, tmp: Path) -> Path:
    from PIL import Image

    with Image.open(src) as im:
        if max(im.size) <= 2000 and im.format in ("PNG", "JPEG"):
            return src
        im = im.convert("RGBA" if im.mode in ("RGBA", "LA", "P") else "RGB")
        im.thumbnail((2000, 2000))
        out = tmp / f"fit_{src.stem}.png"
        im.save(out)
        return out


def _equation_image(d, p, b, ctx, tmp, res):
    """公式降级为图片（matplotlib mathtext）；仍失败时以等宽文字保留原式。"""
    from .charts import MPL_LOCK, _mpl

    plt = _mpl()
    res.equation_images.append(b.id)
    MPL_LOCK.acquire()
    try:
        fig = plt.figure(figsize=(0.01, 0.01))
        fig.text(0, 0, f"${b.latex}$", fontsize=16)
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=200, bbox_inches="tight", pad_inches=0.05, transparent=True)
        plt.close(fig)
        ip = tmp / f"eq_{b.id}.png"
        ip.write_bytes(buf.getvalue())
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        p.add_run().add_picture(str(ip), height=Pt(22))
    except Exception:
        r = p.add_run(b.latex)
        r.font.name = "Courier New"
    finally:
        MPL_LOCK.release()
    if b.number:
        p.add_run(f"    ({ctx['equation']})")
    return p
