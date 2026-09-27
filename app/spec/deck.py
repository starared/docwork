"""PPT 规格：Deck → Slide（布局 + 内容）。

每个布局有独立的内容模型和数量、字数上限。模型只能从布局库中选择布局并填写内容，
坐标由渲染器按网格计算。
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import Field, ValidationError, field_validator, model_validator

from ..util import new_id
from .common import ChartSpec, ImageRef, Model, TableSpec, Theme, text_of


def _lim(n: int):
    def v(cls, s):
        s = (s or "").strip()
        if len(s) > n:
            raise ValueError(f"文字过长（{len(s)} 字，上限 {n} 字）：{s[:20]}…")
        return s
    return v


class Bullet(Model):
    text: str
    sub: list[str] = Field(default_factory=list, max_length=4)
    chk_t = field_validator("text")(_lim(90))


class Column(Model):
    heading: str = ""
    bullets: list[str] = Field(default_factory=list, max_length=6)
    chk_h = field_validator("heading")(_lim(24))


class Card(Model):
    heading: str
    body: str = ""
    icon: str = ""
    chk_h = field_validator("heading")(_lim(16))
    chk_b = field_validator("body")(_lim(80))


class Step(Model):
    label: str
    detail: str = ""
    chk_l = field_validator("label")(_lim(14))
    chk_d = field_validator("detail")(_lim(50))


class Event(Model):
    date: str
    label: str
    detail: str = ""
    chk_l = field_validator("label")(_lim(16))
    chk_d = field_validator("detail")(_lim(40))


class Quadrant(Model):
    heading: str
    body: str = ""
    chk_h = field_validator("heading")(_lim(14))
    chk_b = field_validator("body")(_lim(50))


class Branch(Model):
    label: str
    items: list[str] = Field(default_factory=list, max_length=4)
    chk_l = field_validator("label")(_lim(14))


class Metric(Model):
    value: str
    label: str
    detail: str = ""
    chk_v = field_validator("value")(_lim(12))
    chk_l = field_validator("label")(_lim(16))
    chk_d = field_validator("detail")(_lim(40))


class Person(Model):
    name: str
    role: str = ""
    bio: str = ""
    image: Optional[ImageRef] = None
    chk_b = field_validator("bio")(_lim(60))


class GridImage(Model):
    image: ImageRef
    caption: str = ""


# ---------- 各布局的内容 ----------

class CoverC(Model):
    subtitle: str = ""
    author: str = ""
    date: str = ""
    image: Optional[ImageRef] = None


class TocC(Model):
    items: list[str] = Field(min_length=2, max_length=8)


class SectionC(Model):
    number: str = ""
    subtitle: str = ""


class EndingC(Model):
    subtitle: str = ""
    contact: str = ""


class BulletsC(Model):
    bullets: list[Bullet] = Field(min_length=1, max_length=7)


class TwoColumnC(Model):
    left: Column
    right: Column


class CardsC(Model):
    cards: list[Card] = Field(min_length=2, max_length=4)


class QuoteC(Model):
    quote: str
    source: str = ""
    chk_q = field_validator("quote")(_lim(120))


class ImageTextC(Model):
    image: ImageRef
    bullets: list[Bullet] = Field(default_factory=list, max_length=5)
    image_side: Literal["left", "right"] = "right"
    caption: str = ""


class FullImageC(Model):
    image: ImageRef
    caption: str = ""


class ImageGridC(Model):
    images: list[GridImage] = Field(min_length=2, max_length=6)


class ProcessC(Model):
    steps: list[Step] = Field(min_length=3, max_length=6)


class TimelineC(Model):
    events: list[Event] = Field(min_length=3, max_length=6)


class MatrixC(Model):
    x_label: str = ""
    y_label: str = ""
    quadrants: list[Quadrant] = Field(min_length=4, max_length=4)


class HierarchyC(Model):
    root: str
    children: list[Branch] = Field(min_length=2, max_length=5)


class ComparisonC(Model):
    left: Column
    right: Column
    verdict: str = ""


class ChartC(Model):
    chart: ChartSpec
    bullets: list[str] = Field(default_factory=list, max_length=4)


class BigNumberC(Model):
    metrics: list[Metric] = Field(min_length=1, max_length=4)


class TableC(Model):
    table: TableSpec

    @model_validator(mode="after")
    def _size(self):
        if len(self.table.columns) > 8:
            raise ValueError("表格最多 8 列，请拆分或精简")
        if len(self.table.rows) > 12:
            raise ValueError("表格最多 12 行，请拆分到多页")
        return self


class ChartTableC(Model):
    chart: ChartSpec
    table: TableSpec

    @model_validator(mode="after")
    def _size(self):
        if len(self.table.columns) > 5 or len(self.table.rows) > 8:
            raise ValueError("图表加表格布局的表格最多 5 列 8 行")
        return self


class TeamC(Model):
    people: list[Person] = Field(min_length=2, max_length=6)


LAYOUT_MODELS: dict[str, type[Model]] = {
    "cover": CoverC, "toc": TocC, "section": SectionC, "ending": EndingC,
    "bullets": BulletsC, "two_column": TwoColumnC, "cards": CardsC, "quote": QuoteC,
    "image_text": ImageTextC, "full_image": FullImageC, "image_grid": ImageGridC,
    "process": ProcessC, "timeline": TimelineC, "matrix": MatrixC, "hierarchy": HierarchyC,
    "comparison": ComparisonC, "chart": ChartC, "big_number": BigNumberC, "table": TableC,
    "chart_table": ChartTableC, "team": TeamC,
}
LAYOUT_LABELS = {
    "cover": "封面", "toc": "目录", "section": "章节过渡页", "ending": "结束页",
    "bullets": "标题加要点", "two_column": "两栏", "cards": "卡片", "quote": "引用",
    "image_text": "图文", "full_image": "整页大图", "image_grid": "图片网格",
    "process": "流程", "timeline": "时间线", "matrix": "2x2 矩阵", "hierarchy": "层级结构",
    "comparison": "对比", "chart": "图表加要点", "big_number": "大数字指标", "table": "表格",
    "chart_table": "图表加表格", "team": "团队介绍",
}
LayoutName = Literal[tuple(LAYOUT_MODELS.keys())]  # type: ignore[valid-type]


class Slide(Model):
    id: str = Field(default_factory=lambda: new_id("s"))
    layout: LayoutName  # type: ignore[valid-type]
    title: str = ""
    content: dict = Field(default_factory=dict)
    notes: str = ""

    @field_validator("title")
    @classmethod
    def _title(cls, v):
        v = (v or "").strip()
        if len(v) > 40:
            raise ValueError(f"标题过长（{len(v)} 字，上限 40 字）")
        return v

    @model_validator(mode="after")
    def _content(self):
        m = LAYOUT_MODELS[self.layout]
        try:
            self.content = m.model_validate(self.content).model_dump(mode="json")
        except ValidationError as e:
            raise ValueError(f"第“{self.title or self.layout}”页（{self.layout}）内容不合格：{_brief(e)}")
        return self

    def c(self):
        return LAYOUT_MODELS[self.layout].model_validate(self.content)


class Deck(Model):
    spec_version: int = 1
    title: str = ""
    aspect: Literal["16:9", "4:3"] = "16:9"
    theme: Theme = Field(default_factory=Theme)
    slides: list[Slide] = Field(min_length=1, max_length=80)
    footer: str = ""
    logo_asset: Optional[str] = None
    language: str = "zh"

    @model_validator(mode="after")
    def _ids(self):
        seen = set()
        for s in self.slides:
            if s.id in seen:
                s.id = new_id("s")
            seen.add(s.id)
        return self

    def slide(self, sid: str) -> Slide:
        for s in self.slides:
            if s.id == sid:
                return s
        raise KeyError(sid)


def _brief(e: ValidationError) -> str:
    parts = []
    for err in e.errors()[:4]:
        loc = ".".join(str(x) for x in err.get("loc", ()))
        msg = err.get("msg", "")
        parts.append(f"{loc}: {msg}" if loc else msg)
    return "；".join(parts)


def slide_text(s: Slide | dict) -> str:
    d = s.model_dump() if isinstance(s, Slide) else s
    return text_of({"title": d.get("title"), "content": d.get("content")})


def deck_text(deck: Deck) -> str:
    return "\n".join([deck.title] + [slide_text(s) for s in deck.slides])


LAYOUT_GUIDE = """可用布局（layout）与 content 字段：
- cover 封面：subtitle, author, date, image{query}（可选）
- toc 目录：items[2-8 个章节名]
- section 章节过渡页：number（如"01"）, subtitle
- ending 结束页：subtitle, contact
- bullets 标题加要点：bullets[1-7]{text≤90字, sub[≤4 条子要点]}
- two_column 两栏：left{heading≤24字, bullets[≤6]}, right{同左}
- cards 卡片：cards[2-4]{heading≤16字, body≤80字, icon}
- quote 引用：quote≤120字, source
- image_text 图文：image{query}, bullets[≤5]{text}, image_side(left|right), caption
- full_image 整页大图：image{query}, caption
- image_grid 图片网格：images[2-6]{image{query}, caption}
- process 流程：steps[3-6]{label≤14字, detail≤50字}
- timeline 时间线：events[3-6]{date, label≤16字, detail≤40字}
- matrix 2x2 矩阵：x_label, y_label, quadrants[正好4个]{heading≤14字, body≤50字}（顺序：左上、右上、左下、右下）
- hierarchy 层级结构：root, children[2-5]{label≤14字, items[≤4]}
- comparison 对比：left{heading, bullets[≤6]}, right{heading, bullets[≤6]}, verdict（结论，可选）
- chart 图表加要点：chart{...}, bullets[≤4]
- big_number 大数字指标：metrics[1-4]{value≤12字（如"38%"）, label≤16字, detail≤40字}
- table 表格：table{columns[≤8], rows[≤12 行]}
- chart_table 图表加表格：chart{...}, table{columns[≤5], rows[≤8]}
- team 团队介绍：people[2-6]{name, role, bio≤60字}

chart 字段：type(column|bar|line|area|pie|doughnut|scatter|radar|bubble|waterfall|sankey|funnel|histogram),
  title, categories[分类], series[{name, values[与分类等长的数字]}], unit, number_format（如 "0.0%"）, stacked,
  散点/气泡用 point_series[{name, points[{x,y,size}]}]，桑基图用 flows[{source,target,value}]。
  数字必须来自用户资料或题目；没有真实数据时不要编造图表，改用其他布局。
icon 可选值：star, heart, lightning, gear, cloud, sun, document, database, cube, target, growth, decline,
  check, cross, clock, people, chart, lock, globe, mail, search, home, flag, idea, shield, money, plus, diamond。
"""
