"""LibreOffice 无界面转换。

- 每个 worker 进程使用独立的用户配置目录（-env:UserInstallation），并行运行互不干扰。
- 配置禁止执行宏，打开表格时总是重新计算公式。
- 可用时优先交给常驻实例（lo_resident），失败再冷启动一个 soffice。
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Callable

from ..config import get_settings
from . import sandbox

REGISTRY = """<?xml version="1.0" encoding="UTF-8"?>
<oor:items xmlns:oor="http://openoffice.org/2001/registry" xmlns:xs="http://www.w3.org/2001/XMLSchema" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
<item oor:path="/org.openoffice.Office.Common/Security/Scripting"><prop oor:name="MacroSecurityLevel" oor:op="fuse"><value>3</value></prop></item>
<item oor:path="/org.openoffice.Office.Common/Security/Scripting"><prop oor:name="DisableMacrosExecution" oor:op="fuse"><value>true</value></prop></item>
<item oor:path="/org.openoffice.Office.Common/Security/Scripting"><prop oor:name="BlockUntrustedRefererLinks" oor:op="fuse"><value>true</value></prop></item>
<item oor:path="/org.openoffice.Office.Calc/Formula/Load"><prop oor:name="OOXMLRecalcMode" oor:op="fuse"><value>0</value></prop></item>
<item oor:path="/org.openoffice.Office.Calc/Formula/Load"><prop oor:name="ODFRecalcMode" oor:op="fuse"><value>0</value></prop></item>
<item oor:path="/org.openoffice.Office.Common/Misc"><prop oor:name="FirstRun" oor:op="fuse"><value>false</value></prop></item>
<item oor:path="/org.openoffice.Office.Common/Misc"><prop oor:name="UseOpenCL" oor:op="fuse"><value>false</value></prop></item>
<item oor:path="/org.openoffice.Office.Writer/Content/Update"><prop oor:name="Link" oor:op="fuse"><value>0</value></prop></item>
<item oor:path="/org.openoffice.Office.Calc/Content/Update"><prop oor:name="Link" oor:op="fuse"><value>0</value></prop></item>
</oor:items>
"""

PDF_WRITER = 'pdf:writer_pdf_Export:{"ExportBookmarksToPDFDestination":{"type":"boolean","value":"true"},"ExportBookmarks":{"type":"boolean","value":"true"},"UseTaggedPDF":{"type":"boolean","value":"false"}}'
PDF_IMPRESS = 'pdf:impress_pdf_Export:{"ExportNotesPages":{"type":"boolean","value":"false"}}'
PDF_CALC = 'pdf:calc_pdf_Export'

TARGET_FILTERS = {
    "docx": "docx:MS Word 2007 XML",
    "pptx": "pptx:Impress MS PowerPoint 2007 XML",
    "xlsx": "xlsx:Calc MS Excel 2007 XML",
    "odt": "odt", "ods": "ods", "odp": "odp",
    "doc": "doc:MS Word 97", "xls": "xls:MS Excel 97", "ppt": "ppt:MS PowerPoint 97",
    "html": "html", "txt": "txt:Text (encoded):UTF8", "csv": "csv:Text - txt - csv (StarCalc):44,34,76,1",
    "rtf": "rtf",
}
WRITER_EXT = {".docx", ".doc", ".odt", ".rtf", ".txt", ".html", ".htm", ".md"}
IMPRESS_EXT = {".pptx", ".ppt", ".odp", ".pps", ".ppsx"}
CALC_EXT = {".xlsx", ".xls", ".ods", ".csv"}


def _profile() -> Path:
    """每个进程的每个线程一个独立配置目录：同一配置目录上的两个 soffice 实例会互相干扰。"""
    import threading

    s = get_settings()
    p = s.tmp_dir / f"lo_profile_{os.getpid()}_{threading.get_ident()}"
    reg = p / "user" / "registrymodifications.xcu"
    if not reg.exists():
        reg.parent.mkdir(parents=True, exist_ok=True)
        reg.write_text(REGISTRY, encoding="utf-8")
    return p


def convert(src: Path, target: str, outdir: Path, *, cancel: Callable[[], bool] | None = None,
            timeout: int | None = None) -> Path:
    """target 可以是扩展名（pdf/docx/...）或完整的 LibreOffice 过滤器字符串。"""
    src = Path(src)
    outdir.mkdir(parents=True, exist_ok=True)
    ext = src.suffix.lower()
    if target == "pdf":
        filt = PDF_IMPRESS if ext in IMPRESS_EXT else PDF_CALC if ext in CALC_EXT else PDF_WRITER
        out_ext = "pdf"
    elif ":" in target:
        filt = target
        out_ext = target.split(":")[0]
    else:
        filt = TARGET_FILTERS.get(target, target)
        out_ext = target
    from . import lo_resident

    if lo_resident.enabled():
        out = lo_resident.convert(src, filt, out_ext, outdir, cancel=cancel, timeout=timeout)
        if out is not None:
            return out
    prof = _profile()
    cmd = ["soffice", f"-env:UserInstallation=file://{prof}", "--headless", "--invisible", "--nologo", "--norestore",
           "--nodefault", "--nolockcheck", "--nofirststartwizard", "--convert-to", filt, "--outdir", str(outdir), str(src)]
    # 同一文件名转换为同一扩展名时，输出会覆盖输入：先移到独立目录
    work = src.parent
    sandbox.run(cmd, work, timeout=timeout, cancel=cancel, writable=[outdir, prof], check=False)
    out = outdir / f"{src.stem}.{out_ext}"
    if not out.exists() or out.stat().st_size == 0:
        raise sandbox.ToolError(f"LibreOffice 未能把 {src.name} 转换为 {out_ext.upper()}（文件可能损坏、加密或格式不受支持）")
    return out


def to_pdf(src: Path, outdir: Path, **kw) -> Path:
    return convert(src, "pdf", outdir, **kw)


def recalc_copy(xlsx: Path, outdir: Path, **kw) -> Path:
    """在副本上重算公式（LibreOffice 打开时总是重算，再另存为 xlsx）。原件不变。"""
    tmp_in = outdir / "recalc_src"
    tmp_in.mkdir(parents=True, exist_ok=True)
    cp = tmp_in / xlsx.name
    shutil.copyfile(xlsx, cp)
    return convert(cp, "xlsx", outdir / "recalc_out", **kw)


def normalize_to_ooxml(src: Path, outdir: Path, **kw) -> Path:
    """旧格式（doc/ppt/xls/odf/rtf）转换为对应的 OOXML。"""
    ext = src.suffix.lower()
    if ext in (".doc", ".odt", ".rtf"):
        return convert(src, "docx", outdir, **kw)
    if ext in (".ppt", ".odp", ".pps"):
        return convert(src, "pptx", outdir, **kw)
    if ext in (".xls", ".ods"):
        return convert(src, "xlsx", outdir, **kw)
    raise sandbox.ToolError(f"不支持的格式：{ext}")
