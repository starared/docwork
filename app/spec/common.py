"""三种文档共享的规格组件：图表、表格、图片引用、主题。"""
from __future__ import annotations

import re
from typing import Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

NATIVE_CHART_TYPES = ("column", "bar", "line", "area", "pie", "doughnut", "scatter", "radar", "bubble")
IMAGE_CHART_TYPES = ("waterfall", "sankey", "funnel", "histogram", "boxplot", "heatmap")
CHART_TYPES = NATIVE_CHART_TYPES + IMAGE_CHART_TYPES
HEX = re.compile(r"^#?[0-9a-fA-F]{6}$")


class Model(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)


Num = Optional[float]


class ChartSeries(Model):
    name: str = ""
    values: list[Num] = Field(default_factory=list)


class ChartPoint(Model):
    x: float
    y: float
    size: Optional[float] = None
    label: str = ""


class PointSeries(Model):
    name: str = ""
    points: list[ChartPoint] = Field(default_factory=list)


class Flow(Model):
    source: str
    target: str
    value: float


class ChartSpec(Model):
    type: Literal[CHART_TYPES] = "column"  # type: ignore[valid-type]
    title: str = ""
    categories: list[str] = Field(default_factory=list)
    series: list[ChartSeries] = Field(default_factory=list)
    point_series: list[PointSeries] = Field(default_factory=list)
    flows: list[Flow] = Field(default_factory=list)
    unit: str = ""
    number_format: str = ""
    stacked: bool = False
    show_values: bool = True
    source: str = ""

    @property
    def native(self) -> bool:
        return self.type in NATIVE_CHART_TYPES

    @model_validator(mode="after")
    def _check(self):
        t = self.type
        if t in ("scatter", "bubble"):
            if not self.point_series or not any(ps.points for ps in self.point_series):
                raise ValueError(f"{t} 图需要 point_series（x、y 数据点）")
        elif t == "sankey":
            if not self.flows:
                raise ValueError("桑基图需要 flows（source、target、value）")
        else:
            if not self.series:
                raise ValueError("图表需要至少一个数据系列 series")
            n = len(self.categories)
            if t != "histogram":
                if n == 0:
                    raise ValueError("图表需要 categories（分类标签）")
                for s in self.series:
                    if len(s.values) != n:
                        raise ValueError(f"系列“{s.name}”的数值个数（{len(s.values)}）与分类个数（{n}）不一致")
            if t in ("pie", "doughnut", "funnel", "waterfall") and len(self.series) > 1 and t != "doughnut":
                self.series = self.series[:1]
        return self


class TableSpec(Model):
    columns: list[str] = Field(default_factory=list)
    rows: list[list[Union[str, float, int, None]]] = Field(default_factory=list)
    caption: str = ""
    header: bool = True
    first_col_bold: bool = False

    @model_validator(mode="after")
    def _norm(self):
        if not self.columns and self.rows:
            self.columns = [f"列{i + 1}" for i in range(len(self.rows[0]))]
        n = len(self.columns)
        if n == 0:
            raise ValueError("表格需要 columns")
        fixed = []
        for r in self.rows:
            r = list(r)[:n]
            r += [""] * (n - len(r))
            fixed.append(r)
        self.rows = fixed
        return self


class ImageRef(Model):
    """配图：query 为检索关键词或生成描述；asset 为已解析到的素材 ID。"""
    query: str = ""
    prompt: str = ""
    asset: Optional[str] = None
    source: Literal["auto", "stock", "generate", "upload", "none"] = "auto"
    alt: str = ""


class Theme(Model):
    name: str = "默认"
    primary: str = "#1F4E79"
    secondary: str = "#2E75B6"
    accent: str = "#F4B183"
    background: str = "#FFFFFF"
    surface: str = "#F3F6FA"
    text: str = "#1F2937"
    muted: str = "#6B7280"
    heading_font: str = "微软雅黑"
    body_font: str = "微软雅黑"
    latin_font: str = "Arial"
    decor: Literal["bar", "band", "underline", "minimal", "corner"] = "bar"
    cover: Literal["solid", "split", "image", "frame"] = "solid"
    font_mode: Literal["system", "open"] = "system"

    @field_validator("primary", "secondary", "accent", "background", "surface", "text", "muted")
    @classmethod
    def _hex(cls, v: str) -> str:
        v = (v or "").strip()
        if not HEX.match(v):
            raise ValueError(f"颜色必须是 #RRGGBB 格式：{v}")
        return "#" + v.lstrip("#").upper()


def text_of(v) -> str:
    """把规格中任意嵌套结构里的文字拼接出来，用于检查、搜索和摘要。"""
    out: list[str] = []

    def walk(x):
        if isinstance(x, str):
            out.append(x)
        elif isinstance(x, dict):
            for k, vv in x.items():
                if k in ("id", "layout", "type", "asset", "source", "icon", "image_side", "number_format", "query", "prompt", "alt"):
                    continue
                walk(vv)
        elif isinstance(x, (list, tuple)):
            for vv in x:
                walk(vv)
        elif isinstance(x, (int, float)) and not isinstance(x, bool):
            out.append(str(x))

    walk(v)
    return "\n".join(s for s in out if s)
