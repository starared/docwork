"""主题：配色约束（文字对比度）、预设主题、字体方案。"""
from __future__ import annotations

from ..spec.common import Theme
from .fonts import FONT_SCHEMES


def hex_rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def rgb_hex(r: float, g: float, b: float) -> str:
    return "#%02X%02X%02X" % tuple(int(max(0, min(255, round(v)))) for v in (r, g, b))


def luminance(h: str) -> float:
    def ch(c):
        c = c / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = hex_rgb(h)
    return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b)


def contrast(a: str, b: str) -> float:
    la, lb = luminance(a), luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def mix(a: str, b: str, t: float) -> str:
    ra, ga, ba = hex_rgb(a)
    rb, gb, bb = hex_rgb(b)
    return rgb_hex(ra + (rb - ra) * t, ga + (gb - ga) * t, ba + (bb - ba) * t)


def ensure_contrast(fg: str, bg: str, minimum: float = 4.5) -> str:
    """调整前景色直到与背景的对比度达到要求（向黑或白逐步混合）。"""
    if contrast(fg, bg) >= minimum:
        return fg
    target = "#000000" if luminance(bg) > 0.4 else "#FFFFFF"
    for i in range(1, 21):
        c = mix(fg, target, i / 20)
        if contrast(c, bg) >= minimum:
            return c
    return target


def on_color(bg: str) -> str:
    """深色背景用白字，浅色背景用深字。"""
    return "#FFFFFF" if contrast("#FFFFFF", bg) >= contrast("#1F2937", bg) else "#1F2937"


def normalize_theme(t: Theme) -> Theme:
    """强制满足文字对比度：正文对背景 ≥ 4.5，次要文字 ≥ 3，主色作为标题色时 ≥ 3。"""
    t = t.model_copy()
    t.text = ensure_contrast(t.text, t.background, 4.5)
    t.muted = ensure_contrast(t.muted, t.background, 3.0)
    t.surface = t.surface if contrast(t.text, t.surface) >= 4.5 else mix(t.background, t.primary, 0.06)
    scheme = FONT_SCHEMES[t.font_mode]
    if t.font_mode == "open":
        if t.heading_font in FONT_SCHEMES["system"].values():
            t.heading_font = scheme["sans"]
        if t.body_font in FONT_SCHEMES["system"].values():
            t.body_font = scheme["sans"]
        if t.latin_font in FONT_SCHEMES["system"].values():
            t.latin_font = scheme["latin_sans"]
    return t


def heading_color(t: Theme) -> str:
    return ensure_contrast(t.primary, t.background, 3.0)


def series_colors(t: Theme, n: int) -> list[str]:
    base = [t.primary, t.secondary, t.accent, mix(t.primary, "#FFFFFF", 0.45), mix(t.secondary, "#000000", 0.3),
            mix(t.accent, "#000000", 0.25), t.muted, mix(t.secondary, "#FFFFFF", 0.5)]
    out = []
    for i in range(n):
        out.append(base[i % len(base)])
    return out


PRESET_THEMES: dict[str, dict] = {
    "商务蓝": dict(primary="#1F4E79", secondary="#2E75B6", accent="#F4B183", background="#FFFFFF", surface="#F2F6FB", text="#1F2937", muted="#6B7280", decor="bar", cover="split"),
    "科技深色": dict(primary="#38BDF8", secondary="#818CF8", accent="#F472B6", background="#0F172A", surface="#1E293B", text="#E2E8F0", muted="#94A3B8", decor="underline", cover="solid"),
    "学术绿": dict(primary="#1B5E20", secondary="#43A047", accent="#FBC02D", background="#FFFFFF", surface="#F1F8E9", text="#212121", muted="#616161", decor="band", cover="frame"),
    "中国红": dict(primary="#B71C1C", secondary="#D84315", accent="#F9A825", background="#FFFDF8", surface="#FBF3E9", text="#2D2A26", muted="#6D6259", decor="corner", cover="solid"),
    "清新青": dict(primary="#00796B", secondary="#26A69A", accent="#FF8A65", background="#FFFFFF", surface="#E8F5F3", text="#263238", muted="#607D8B", decor="underline", cover="split"),
    "简约灰": dict(primary="#374151", secondary="#6B7280", accent="#F59E0B", background="#FFFFFF", surface="#F5F5F4", text="#111827", muted="#6B7280", decor="minimal", cover="frame"),
    "活力橙": dict(primary="#E65100", secondary="#FB8C00", accent="#1E88E5", background="#FFFFFF", surface="#FFF4E8", text="#1F2937", muted="#6B7280", decor="bar", cover="image"),
    "典雅紫": dict(primary="#4A148C", secondary="#7B1FA2", accent="#FFB300", background="#FFFFFF", surface="#F5EEF8", text="#1F1B24", muted="#6A6475", decor="band", cover="solid"),
}


def preset_theme(name: str, font_mode: str = "system") -> Theme:
    d = dict(PRESET_THEMES.get(name) or PRESET_THEMES["商务蓝"])
    d["name"] = name if name in PRESET_THEMES else "商务蓝"
    d["font_mode"] = font_mode
    return normalize_theme(Theme(**d))
