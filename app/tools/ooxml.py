"""OOXML 包级处理：剥离宏、元素清单（写回前后比对，发现元素丢失时告警）、结构自检。"""
from __future__ import annotations

import re
import zipfile
from pathlib import Path

from lxml import etree

MACRO_TO_PLAIN = {
    "application/vnd.ms-word.document.macroEnabled.main+xml": "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml",
    "application/vnd.ms-powerpoint.presentation.macroEnabled.main+xml": "application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml",
    "application/vnd.ms-excel.sheet.macroEnabled.main+xml": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml",
}
PLAIN_EXT = {"docm": "docx", "pptm": "pptx", "xlsm": "xlsx"}


def strip_macros(src: Path, dst: Path) -> Path:
    """删除 vbaProject 等宏部件及其关系，并把主文档类型改为不含宏的类型。"""
    drop_re = re.compile(r"(^|/)(vbaProject\.bin|vbaData\.xml|vbaProjectSignature\.bin|vbaProjectSignatureAgile\.bin)$", re.I)
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zout:
        dropped = {n for n in zin.namelist() if drop_re.search(n)}
        for item in zin.infolist():
            name = item.filename
            if name in dropped:
                continue
            data = zin.read(name)
            if name == "[Content_Types].xml":
                s = data.decode("utf-8")
                for a, b in MACRO_TO_PLAIN.items():
                    s = s.replace(a, b)
                s = re.sub(r'<Override[^>]+PartName="[^"]*(vbaProject\.bin|vbaData\.xml|vbaProjectSignature[^"]*)"[^>]*/>', "", s)
                s = re.sub(r'<Default[^>]+Extension="bin"[^>]+ContentType="application/vnd\.ms-office\.vbaProject"[^>]*/>', "", s)
                data = s.encode("utf-8")
            elif name.endswith(".rels"):
                s = data.decode("utf-8")
                s = re.sub(r'<Relationship[^>]+Target="[^"]*(vbaProject\.bin|vbaData\.xml|vbaProjectSignature[^"]*)"[^>]*/>', "", s)
                data = s.encode("utf-8")
            zout.writestr(item, data)
    return dst


def inventory(path: Path) -> dict:
    """统计文件中的主要元素，用于写回前后比对。"""
    inv: dict[str, int] = {}
    with zipfile.ZipFile(path) as z:
        names = z.namelist()

        def count(prefix: str, suffix: str = ".xml"):
            return sum(1 for n in names if n.startswith(prefix) and n.endswith(suffix) and "/_rels/" not in n)

        inv["charts"] = count("xl/charts/chart") + count("ppt/charts/chart") + count("word/charts/chart")
        inv["media"] = sum(1 for n in names if "/media/" in n)
        inv["pivot_tables"] = count("xl/pivotTables/")
        inv["pivot_caches"] = count("xl/pivotCache/pivotCacheDefinition")
        inv["slicers"] = count("xl/slicers/")
        inv["external_links"] = count("xl/externalLinks/externalLink")
        inv["smartart"] = count("ppt/diagrams/data") + count("word/diagrams/data")
        inv["embeddings"] = sum(1 for n in names if "/embeddings/" in n)
        inv["comments"] = sum(1 for n in names if re.search(r"comments\d*\.xml$", n) or n.endswith("/comments.xml"))
        inv["slides"] = count("ppt/slides/slide")
        inv["sheets"] = count("xl/worksheets/sheet")
        cf = dv = merged = drawings = 0
        for n in names:
            if n.startswith("xl/worksheets/sheet") and n.endswith(".xml"):
                s = z.read(n)
                cf += s.count(b"<conditionalFormatting")
                dv += s.count(b"<dataValidation ")
                merged += s.count(b"<mergeCell ")
            if n.startswith("xl/drawings/drawing") and n.endswith(".xml"):
                drawings += 1
        inv["cond_formats"] = cf
        inv["data_validations"] = dv
        inv["merged_cells"] = merged
        inv["drawings"] = drawings
        if "xl/workbook.xml" in names:
            inv["defined_names"] = z.read("xl/workbook.xml").count(b"<definedName ")
        if "word/document.xml" in names:
            doc = z.read("word/document.xml")
            inv["tables"] = doc.count(b"<w:tbl>") + doc.count(b"<w:tbl ")
            inv["textboxes"] = doc.count(b"<w:txbxContent")
            inv["content_controls"] = doc.count(b"<w:sdt>") + doc.count(b"<w:sdt ")
            inv["fields"] = doc.count(b"<w:fldChar ") // 3 + doc.count(b"<w:fldSimple")
            inv["equations"] = doc.count(b"<m:oMath>") + doc.count(b"<m:oMath ")
        if any(n.startswith("ppt/slides/") for n in names):
            anim = 0
            for n in names:
                if n.startswith("ppt/slides/slide") and n.endswith(".xml"):
                    s = z.read(n)
                    anim += s.count(b"<p:timing")
            inv["animations"] = anim
    return {k: v for k, v in inv.items() if v}


LABELS = {
    "charts": "图表", "media": "图片等媒体", "pivot_tables": "透视表", "pivot_caches": "透视表缓存", "slicers": "切片器",
    "external_links": "外部链接", "smartart": "SmartArt", "embeddings": "嵌入对象", "comments": "批注", "slides": "幻灯片",
    "sheets": "工作表", "cond_formats": "条件格式", "data_validations": "数据验证", "merged_cells": "合并单元格",
    "drawings": "绘图层", "defined_names": "名称定义", "tables": "表格", "textboxes": "文本框", "content_controls": "内容控件",
    "fields": "域", "equations": "公式", "animations": "动画",
}


def compare_inventory(before: dict, after: dict, expected_changes: dict | None = None) -> list[str]:
    """返回丢失元素的告警文字。expected_changes 为用户要求的增删（如删页）。"""
    exp = expected_changes or {}
    warns = []
    for k, v in before.items():
        a = after.get(k, 0)
        allowed = v + exp.get(k, 0)
        if a < allowed:
            warns.append(f"{LABELS.get(k, k)}从 {v} 个变为 {a} 个")
    return warns


def validate_package(path: Path) -> list[str]:
    """结构自检：ZIP 完整、各 XML 部件格式正确、内容类型和关系指向的部件存在。"""
    problems = []
    try:
        with zipfile.ZipFile(path) as z:
            bad = z.testzip()
            if bad:
                problems.append(f"压缩数据损坏：{bad}")
            names = set(z.namelist())
            if "[Content_Types].xml" not in names:
                problems.append("缺少 [Content_Types].xml")
            for n in names:
                if n.endswith(".xml") or n.endswith(".rels"):
                    try:
                        etree.fromstring(z.read(n))
                    except etree.XMLSyntaxError as e:
                        problems.append(f"{n} 不是合法的 XML：{e}")
                if n.endswith(".rels"):
                    base = n.replace("_rels/", "").rsplit(".rels", 1)[0]
                    base_dir = base.rsplit("/", 1)[0] if "/" in base else ""
                    try:
                        root = etree.fromstring(z.read(n))
                    except etree.XMLSyntaxError:
                        continue
                    for rel in root:
                        if rel.get("TargetMode") == "External":
                            continue
                        tgt = rel.get("Target", "")
                        full = _resolve(base_dir, tgt)
                        if full and full not in names:
                            problems.append(f"{n} 指向不存在的部件 {tgt}")
    except zipfile.BadZipFile:
        problems.append("不是有效的 ZIP 包")
    return problems


def _resolve(base_dir: str, target: str) -> str:
    if target.startswith("/"):
        return target.lstrip("/")
    parts = (base_dir.split("/") if base_dir else []) + target.split("/")
    out: list[str] = []
    for p in parts:
        if p == "..":
            if out:
                out.pop()
        elif p and p != ".":
            out.append(p)
    return "/".join(out)
