"""Word 规格：Document → 块（标题、段落、列表、表格、图片、公式、图表、引用、分页、目录、参考文献）。

段落文字支持行内标记：**粗体**、*斜体*、^上标^、~下标~。
"""
from __future__ import annotations

from typing import Annotated, Literal, Optional, Union

from pydantic import Field, model_validator

from ..util import new_id
from .common import ChartSpec, ImageRef, Model, TableSpec, text_of

PRESETS = {
    "report": "研究或实验报告",
    "proposal": "项目方案",
    "paper": "论文式文档",
    "minutes": "会议纪要",
    "manual": "说明书",
    "official": "公文",
}


class BlockBase(Model):
    id: str = Field(default_factory=lambda: new_id("b"))


class Heading(BlockBase):
    type: Literal["heading"] = "heading"
    level: int = Field(1, ge=1, le=3)
    text: str


class Paragraph(BlockBase):
    type: Literal["paragraph"] = "paragraph"
    text: str
    align: Literal["left", "center", "right", "justify"] = "justify"


class ListBlock(BlockBase):
    type: Literal["list"] = "list"
    ordered: bool = False
    items: list[Union[str, "ListItem"]] = Field(min_length=1)

    @model_validator(mode="after")
    def _norm(self):
        self.items = [ListItem(text=i) if isinstance(i, str) else i for i in self.items]
        return self


class ListItem(Model):
    text: str
    sub: list[str] = Field(default_factory=list)


ListBlock.model_rebuild()


class TableBlock(BlockBase):
    type: Literal["table"] = "table"
    table: TableSpec


class ImageBlock(BlockBase):
    type: Literal["image"] = "image"
    image: ImageRef
    caption: str = ""
    width_pct: int = Field(80, ge=20, le=100)


class EquationBlock(BlockBase):
    type: Literal["equation"] = "equation"
    latex: str
    number: bool = False


class ChartBlock(BlockBase):
    type: Literal["chart"] = "chart"
    chart: ChartSpec
    caption: str = ""


class QuoteBlock(BlockBase):
    type: Literal["quote"] = "quote"
    text: str
    source: str = ""


class PageBreak(BlockBase):
    type: Literal["page_break"] = "page_break"


class TocBlock(BlockBase):
    type: Literal["toc"] = "toc"
    title: str = "目录"


class References(BlockBase):
    type: Literal["references"] = "references"
    style: Literal["gbt7714", "apa", "plain"] = "gbt7714"
    items: list[str] = Field(default_factory=list)


Block = Annotated[Union[Heading, Paragraph, ListBlock, TableBlock, ImageBlock, EquationBlock, ChartBlock, QuoteBlock, PageBreak, TocBlock, References], Field(discriminator="type")]


class DocMeta(Model):
    author: str = ""
    date: str = ""
    organization: str = ""
    subtitle: str = ""
    # 公文专用
    doc_number: str = ""
    issuer: str = ""
    recipients: str = ""


class Document(Model):
    spec_version: int = 1
    title: str
    preset: Literal[tuple(PRESETS.keys())] = "report"  # type: ignore[valid-type]
    meta: DocMeta = Field(default_factory=DocMeta)
    font_mode: Literal["system", "open"] = "system"
    header_text: str = ""
    page_numbers: bool = True
    logo_asset: Optional[str] = None
    blocks: list[Block] = Field(min_length=1)  # type: ignore[valid-type]

    @model_validator(mode="after")
    def _ids(self):
        seen = set()
        for b in self.blocks:
            if b.id in seen:
                b.id = new_id("b")
            seen.add(b.id)
        return self

    def block(self, bid: str):
        for b in self.blocks:
            if b.id == bid:
                return b
        raise KeyError(bid)

    def outline(self) -> list[dict]:
        out = []
        for b in self.blocks:
            label = getattr(b, "text", "") or getattr(b, "caption", "") or getattr(b, "latex", "") or b.type
            out.append({"id": b.id, "type": b.type, "level": getattr(b, "level", 0), "label": label[:60]})
        return out


def document_text(doc: Document) -> str:
    return "\n".join([doc.title] + [text_of(b.model_dump()) for b in doc.blocks])


BLOCK_GUIDE = """blocks 数组中每个块的格式（type 决定字段）：
- {"type":"heading","level":1-3,"text":"..."}
- {"type":"paragraph","text":"...（可用 **粗体**、*斜体*、^上标^、~下标~）"}
- {"type":"list","ordered":false,"items":["...", {"text":"...","sub":["..."]}]}
- {"type":"table","table":{"caption":"表题","columns":[...],"rows":[[...],...]}}
- {"type":"image","image":{"query":"配图检索词"},"caption":"图题","width_pct":80}
- {"type":"equation","latex":"E = mc^2","number":true}（LaTeX 常用子集：分式、上下标、根号、求和积分、矩阵、希腊字母）
- {"type":"chart","chart":{...同 PPT 的 chart 字段...},"caption":"图题"}
- {"type":"quote","text":"...","source":"..."}
- {"type":"page_break"}
- {"type":"toc","title":"目录"}
- {"type":"references","style":"gbt7714","items":["按格式整理好的一条文献", ...]}（只整理用户资料中出现的文献，不得编造）
"""
