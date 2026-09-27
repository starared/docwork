"""字体：文件中写入的字体名称与服务器上用于测量、预览的实际字体。

服务器不安装商业字体。fontconfig 把微软雅黑等映射到思源（Noto CJK）字体（见 deploy/fonts.conf），
测量时同样使用映射后的字体，并预留余量抵消两者的宽度差异。
"""
from __future__ import annotations

import subprocess
from functools import lru_cache
from pathlib import Path

from PIL import ImageFont

# 字体方案：system = Windows 自带字体；open = 思源字体（需要打开文件的电脑安装）
FONT_SCHEMES = {
    "system": {"sans": "微软雅黑", "serif": "宋体", "latin_sans": "Arial", "latin_serif": "Times New Roman"},
    "open": {"sans": "Source Han Sans SC", "serif": "Source Han Serif SC", "latin_sans": "Source Sans 3", "latin_serif": "Source Serif 4"},
}
# 文件中的字体名 → 服务器上的替代字体族（fontconfig 名称）
SUBSTITUTES = {
    "微软雅黑": "Noto Sans CJK SC", "Microsoft YaHei": "Noto Sans CJK SC", "黑体": "Noto Sans CJK SC",
    "SimHei": "Noto Sans CJK SC", "Source Han Sans SC": "Noto Sans CJK SC", "思源黑体": "Noto Sans CJK SC",
    "宋体": "Noto Serif CJK SC", "SimSun": "Noto Serif CJK SC", "Source Han Serif SC": "Noto Serif CJK SC",
    "思源宋体": "Noto Serif CJK SC", "仿宋": "Noto Serif CJK SC", "仿宋_GB2312": "Noto Serif CJK SC",
    "FangSong": "Noto Serif CJK SC", "楷体": "Noto Serif CJK SC", "楷体_GB2312": "Noto Serif CJK SC",
    "方正小标宋简体": "Noto Serif CJK SC", "方正小标宋_GBK": "Noto Serif CJK SC",
    "Arial": "Liberation Sans", "Calibri": "Carlito", "Times New Roman": "Liberation Serif", "Cambria": "Caladea",
    "Source Sans 3": "Liberation Sans", "Source Serif 4": "Liberation Serif",
}
# 这些字体在服务器上没有同款字库，预览时需要提示
NON_EXACT = {"仿宋", "仿宋_GB2312", "FangSong", "楷体", "楷体_GB2312", "方正小标宋简体", "方正小标宋_GBK"}
# 测量余量：替代字体与实际字体的宽度差异
MEASURE_MARGIN = 1.07


@lru_cache(maxsize=64)
def font_file(family: str, bold: bool = False) -> str | None:
    """用 fc-match 查找字体文件（先查已上传的授权字体，再查替代字体）。"""
    from ..config import get_settings

    custom = get_settings().font_dir
    for p in custom.glob("*"):
        if p.suffix.lower() in (".ttf", ".otf", ".ttc") and family.lower().replace(" ", "") in p.stem.lower().replace(" ", ""):
            return str(p)
    target = SUBSTITUTES.get(family, family)
    pattern = f"{target}:style={'Bold' if bold else 'Regular'}"
    try:
        out = subprocess.run(["fc-match", "-f", "%{file}", pattern], capture_output=True, text=True, timeout=10).stdout.strip()
        if out and Path(out).exists():
            return out
    except (OSError, subprocess.SubprocessError):
        pass
    return None


@lru_cache(maxsize=64)
def _pil_font(path: str | None, bold: bool):
    if path:
        try:
            return ImageFont.truetype(path, 100)
        except OSError:
            pass
    return None


class Measurer:
    """按字符宽度测量文本（单位：磅），带缓存。"""

    def __init__(self, family: str, bold: bool = False):
        self.font = _pil_font(font_file(family, bold), bold)
        self.cache: dict[str, float] = {}
        # 字体自然行高（ascent + descent，相对字号）。PowerPoint/LibreOffice 的“倍数行距”以此为基准。
        # 取服务器预览字体与微软雅黑（1.32）中较大者，保证预览与实际打开都放得下。
        try:
            a, d = self.font.getmetrics() if self.font else (132, 0)
            self.line_factor = max(1.32, (a + d) / 100.0)
        except Exception:
            self.line_factor = 1.45

    def char_w(self, ch: str) -> float:
        """字号为 1pt 时的字符宽度（pt）。"""
        w = self.cache.get(ch)
        if w is None:
            if self.font is not None:
                try:
                    w = self.font.getlength(ch) / 100.0
                except Exception:
                    w = 1.0
            else:
                w = 1.0 if ord(ch) > 0x2E80 else 0.55
            if ord(ch) > 0x2E80 and w < 0.9:
                w = 1.0
            self.cache[ch] = w
        return w

    def width(self, text: str, size: float) -> float:
        return sum(self.char_w(c) for c in text) * size * MEASURE_MARGIN

    def wrap(self, text: str, max_w: float, size: float) -> list[str]:
        """按宽度折行：中日韩字符逐字折行，拉丁单词整体折行。"""
        lines: list[str] = []
        for para in (text or "").split("\n"):
            tokens = _tokenize(para)
            cur, cur_w = "", 0.0
            for tok in tokens:
                tw = self.width(tok, size)
                if cur and cur_w + tw > max_w:
                    lines.append(cur.rstrip())
                    cur, cur_w = tok.lstrip(), self.width(tok.lstrip(), size)
                    # 单个超长词强制断开
                    while cur_w > max_w and len(cur) > 1:
                        cut = _cut_to(self, cur, max_w, size)
                        lines.append(cur[:cut])
                        cur = cur[cut:]
                        cur_w = self.width(cur, size)
                else:
                    cur += tok
                    cur_w += tw
            lines.append(cur.rstrip())
        return lines


def _cut_to(m: Measurer, s: str, max_w: float, size: float) -> int:
    w = 0.0
    for i, ch in enumerate(s):
        w += m.char_w(ch) * size * MEASURE_MARGIN
        if w > max_w:
            return max(1, i)
    return len(s)


def _tokenize(s: str) -> list[str]:
    toks, buf = [], ""
    for ch in s:
        if ord(ch) > 0x2E80:
            if buf:
                toks.append(buf)
                buf = ""
            toks.append(ch)
        elif ch == " ":
            buf += ch
            toks.append(buf)
            buf = ""
        else:
            buf += ch
    if buf:
        toks.append(buf)
    return toks


@lru_cache(maxsize=16)
def measurer(family: str, bold: bool = False) -> Measurer:
    return Measurer(family, bold)


def fit_text(
    paragraphs: list[tuple[str, float]],
    box_w: float,
    box_h: float,
    family: str,
    max_size: float,
    min_size: float,
    line_spacing: float = 1.2,
    para_space: float = 0.35,
    bold: bool = False,
) -> tuple[float, bool, int]:
    """找到能放下全部段落的最大字号。

    paragraphs：[(文字, 相对字号系数)]，例如子要点 0.88。
    返回 (字号, 是否仍然溢出, 总行数)。
    """
    m = measurer(family, bold)
    size = max_size
    while True:
        total_h, nlines = 0.0, 0
        for i, (txt, k) in enumerate(paragraphs):
            s = size * k
            lines = m.wrap(txt, box_w, s)
            nlines += len(lines)
            total_h += len(lines) * s * line_spacing * m.line_factor
            if i:
                total_h += s * para_space
        if total_h <= box_h or size <= min_size:
            return size, total_h > box_h + 0.5, nlines
        size = max(min_size, size - 1)
