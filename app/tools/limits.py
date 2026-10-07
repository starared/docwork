"""上传文件检查：按文件头识别真实类型，并检查解压后大小、页数、像素等处理上限。"""
from __future__ import annotations

import zipfile
from pathlib import Path

from .. import db
from ..config import get_settings
from ..util import UserError

OOXML_MAIN = {
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml": "docx",
    "application/vnd.ms-word.document.macroEnabled.main+xml": "docm",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.template.main+xml": "docx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml": "pptx",
    "application/vnd.ms-powerpoint.presentation.macroEnabled.main+xml": "pptm",
    "application/vnd.openxmlformats-officedocument.presentationml.slideshow.main+xml": "pptx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml": "xlsx",
    "application/vnd.ms-excel.sheet.macroEnabled.main+xml": "xlsm",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.template.main+xml": "xlsx",
}
ODF = {"application/vnd.oasis.opendocument.text": "odt", "application/vnd.oasis.opendocument.spreadsheet": "ods",
       "application/vnd.oasis.opendocument.presentation": "odp"}
TEXT_EXT = {".txt": "txt", ".md": "md", ".markdown": "md", ".csv": "csv", ".html": "html", ".htm": "html", ".json": "txt"}
IMAGE_KINDS = {"png", "jpg", "gif", "bmp", "tiff", "webp"}
ALLOWED = {"pdf", "docx", "docm", "pptx", "pptm", "xlsx", "xlsm", "odt", "ods", "odp", "doc", "xls", "ppt", "rtf",
           "txt", "md", "csv", "html"} | IMAGE_KINDS
ZIP_METADATA_MAX = 1024 * 1024


def _zip_metadata(z: zipfile.ZipFile, name: str) -> bytes:
    """类型标记只允许小文件，并且不信任 ZIP 目录中声明的大小。"""
    if z.getinfo(name).file_size > ZIP_METADATA_MAX:
        raise UserError("文件类型信息过大，已拒绝处理", 413, "limits")
    with z.open(name) as f:
        data = f.read(ZIP_METADATA_MAX + 1)
    if len(data) > ZIP_METADATA_MAX:
        raise UserError("文件类型信息过大，已拒绝处理", 413, "limits")
    return data


def _limit(key: str) -> int:
    s = get_settings()
    return int(db.get_setting(key, getattr(s, key)))


def sniff(path: Path, name: str = "") -> str:
    """返回真实类型（扩展名风格），无法识别时抛出错误。"""
    with open(path, "rb") as f:
        head = f.read(16)
    ext = Path(name or path.name).suffix.lower()
    if head.startswith(b"%PDF"):
        return "pdf"
    if head.startswith(b"\x89PNG"):
        return "png"
    if head[:3] == b"\xff\xd8\xff":
        return "jpg"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if head[:2] == b"BM":
        return "bmp"
    if head[:4] in (b"II*\x00", b"MM\x00*"):
        return "tiff"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    if head[:4] in (b"\x00\x01\x00\x00", b"OTTO", b"ttcf", b"true") and ext in (".ttf", ".otf", ".ttc"):
        return "font"
    if head.startswith(b"{\\rtf"):
        return "rtf"
    if head.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        # OLE 复合文档：按扩展名区分 doc / xls / ppt；加密的 OOXML 也是 OLE
        if ext in (".doc", ".xls", ".ppt"):
            return ext[1:]
        raise UserError("无法识别的旧版 Office 文件或已加密的文件（请先取消密码保护）", 415, "unsupported")
    if head.startswith(b"PK\x03\x04") or head.startswith(b"PK\x05\x06"):
        try:
            # 必须在读取任何压缩条目之前执行，包括直接调用 sniff 的路径。
            check_zip(path)
            with zipfile.ZipFile(path) as z:
                names = set(z.namelist())
                if "[Content_Types].xml" in names:
                    ct = _zip_metadata(z, "[Content_Types].xml").decode("utf-8", "replace")
                    for k, v in OOXML_MAIN.items():
                        if k in ct:
                            return v
                if "mimetype" in names:
                    mt = _zip_metadata(z, "mimetype").decode("ascii", "replace").strip()
                    if mt in ODF:
                        return ODF[mt]
        except (zipfile.BadZipFile, RuntimeError, NotImplementedError):
            raise UserError("压缩结构损坏的文件", 415, "unsupported")
        raise UserError("不支持的压缩文件类型（只接受 Office / OpenDocument 文件）", 415, "unsupported")
    if ext in TEXT_EXT:
        try:
            with open(path, "rb") as f:
                f.read(1 << 20).decode("utf-8")
        except UnicodeDecodeError:
            try:
                with open(path, "rb") as f:
                    f.read(1 << 20).decode("gb18030")
            except UnicodeDecodeError:
                raise UserError("文本文件编码无法识别（请使用 UTF-8 或 GBK）", 415, "unsupported")
        return TEXT_EXT[ext]
    raise UserError(f"不支持的文件类型：{name or path.name}", 415, "unsupported")


def check_zip(path: Path) -> None:
    """ZIP 炸弹检查：解压后总大小、文件数量、单项压缩比。"""
    max_unzipped = _limit("max_unzipped_mb") * 1024 * 1024
    total = 0
    with zipfile.ZipFile(path) as z:
        infos = z.infolist()
        if len(infos) > 20000:
            raise UserError("压缩包内文件数量过多", 413, "limits")
        for i in infos:
            total += i.file_size
            if i.file_size > 50 * 1024 * 1024 and i.compress_size and i.file_size / i.compress_size > 500:
                raise UserError("文件内部压缩比异常，已拒绝处理", 413, "limits")
        if total > max_unzipped:
            raise UserError(f"文件解压后超过上限（{max_unzipped // 1024 // 1024} MB）", 413, "limits")


def check_image(path: Path) -> tuple[int, int]:
    from PIL import Image

    max_px = _limit("max_pixels")
    Image.MAX_IMAGE_PIXELS = max_px
    try:
        with Image.open(path) as im:
            w, h = im.size
    except Image.DecompressionBombError:
        raise UserError("图片像素超过上限", 413, "limits")
    except Exception:
        raise UserError("图片文件损坏或格式不受支持", 415, "unsupported")
    if w * h > max_px:
        raise UserError(f"图片像素超过上限（{max_px // 1_000_000} 百万像素）", 413, "limits")
    return w, h


def check_pdf(path: Path) -> int:
    import pikepdf

    max_pages = _limit("max_pages")
    try:
        with pikepdf.open(path) as pdf:
            n = len(pdf.pages)
            if len(pdf.objects) > 3_000_000:
                raise UserError("PDF 对象数量异常，已拒绝处理", 413, "limits")
    except pikepdf.PasswordError:
        raise UserError("PDF 已加密，请先取消密码保护", 415, "encrypted")
    except pikepdf.PdfError as e:
        raise UserError(f"PDF 结构损坏：{str(e)[:120]}", 415, "broken")
    if n > max_pages:
        raise UserError(f"PDF 页数超过上限（{max_pages} 页）", 413, "limits")
    return n


def inspect(path: Path, name: str, allow_font: bool = False) -> dict:
    """完整检查，返回 {kind, pages?, size?, macro?}。"""
    kind = sniff(path, name)
    if kind == "font" and allow_font:
        return {"kind": "font"}
    if kind not in ALLOWED:
        raise UserError(f"不支持的文件类型：{kind}", 415, "unsupported")
    info = {"kind": kind}
    if kind in ("docx", "docm", "pptx", "pptm", "xlsx", "xlsm", "odt", "ods", "odp"):
        info["macro"] = kind.endswith("m")
    elif kind in IMAGE_KINDS:
        info["size"] = check_image(path)
    elif kind == "pdf":
        info["pages"] = check_pdf(path)
    return info
