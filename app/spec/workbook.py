"""Excel 规格：Workbook → Sheet（列定义、行数据、公式、格式、图表、校验）。

单元格值可以是数字、文字、布尔或公式字符串（以 = 开头）。公式只允许白名单内的函数。
"""
from __future__ import annotations

import re
from typing import Literal, Optional, Union

from pydantic import Field, field_validator, model_validator

from ..util import new_id
from .common import Model

# Excel 与 WPS 都支持的函数白名单
FUNCTION_WHITELIST = {
    "SUM", "SUMIF", "SUMIFS", "SUMPRODUCT", "AVERAGE", "AVERAGEIF", "AVERAGEIFS", "COUNT", "COUNTA", "COUNTIF",
    "COUNTIFS", "COUNTBLANK", "MIN", "MAX", "MEDIAN", "MODE", "STDEV", "STDEVP", "VAR", "VARP", "LARGE", "SMALL",
    "RANK", "ROUND", "ROUNDUP", "ROUNDDOWN", "INT", "ABS", "MOD", "POWER", "SQRT", "EXP", "LN", "LOG", "LOG10",
    "IF", "IFERROR", "AND", "OR", "NOT", "VLOOKUP", "HLOOKUP", "INDEX", "MATCH", "CHOOSE", "LOOKUP",
    "LEFT", "RIGHT", "MID", "LEN", "TRIM", "UPPER", "LOWER", "CONCATENATE", "TEXT", "VALUE", "SUBSTITUTE", "FIND",
    "SEARCH", "REPT", "TODAY", "NOW", "DATE", "YEAR", "MONTH", "DAY", "WEEKDAY", "DATEDIF", "EDATE", "EOMONTH",
    "NETWORKDAYS", "PMT", "FV", "PV", "NPV", "IRR", "RATE", "PERCENTILE", "QUARTILE", "CORREL", "SLOPE",
    "INTERCEPT", "FORECAST", "TRUNC", "CEILING", "FLOOR", "PI", "ISBLANK", "ISNUMBER", "ISTEXT", "ISERROR",
}
_FUNC_RE = re.compile(r"([A-Z][A-Z0-9\.]*)\s*\(")

Cell = Union[str, float, int, bool, None]


def formula_functions(f: str) -> set[str]:
    # 去掉字符串常量后再找函数名
    stripped = re.sub(r'"[^"]*"', '""', f.upper())
    return set(_FUNC_RE.findall(stripped))


class ColumnDef(Model):
    header: str
    width: Optional[float] = None
    number_format: str = ""   # 如 "0.00"、"0.0%"、"#,##0"、"yyyy-mm-dd"


class CondFormat(Model):
    range: str                                 # 如 "C2:C20"
    kind: Literal["color_scale", "data_bar", "greater_than", "less_than", "between", "equal"] = "color_scale"
    value: Optional[float] = None
    value2: Optional[float] = None
    color: str = "#F8CBAD"


class Validation(Model):
    range: str
    options: list[str] = Field(min_length=1)


class SheetChart(Model):
    type: Literal["column", "bar", "line", "pie", "area", "scatter"] = "column"
    title: str = ""
    categories: str                            # 分类区域，如 "A2:A13"
    values: list[str] = Field(min_length=1)    # 数值区域（含表头行时 titles_from_data），如 ["B1:B13"]
    anchor: str = ""                           # 放置位置，如 "H2"
    y_title: str = ""


class ExpectedValue(Model):
    """由 pandas 独立计算的关键结果，用于验证公式。"""
    cell: str
    value: float
    tolerance: float = 1e-6


class Sheet(Model):
    id: str = Field(default_factory=lambda: new_id("sh"))
    name: str
    columns: list[ColumnDef] = Field(default_factory=list)
    rows: list[list[Cell]] = Field(default_factory=list)
    freeze_header: bool = True
    autofilter: bool = True
    cond_formats: list[CondFormat] = Field(default_factory=list)
    validations: list[Validation] = Field(default_factory=list)
    charts: list[SheetChart] = Field(default_factory=list)
    expected: list[ExpectedValue] = Field(default_factory=list)
    note: str = ""

    @field_validator("name")
    @classmethod
    def _name(cls, v):
        v = re.sub(r"[\[\]\*\?/\\:]", "_", (v or "Sheet").strip())[:31]
        return v or "Sheet"

    @model_validator(mode="after")
    def _check(self):
        bad = set()
        for r in self.rows:
            for c in r:
                if isinstance(c, str) and c.startswith("="):
                    bad |= formula_functions(c) - FUNCTION_WHITELIST
        if bad:
            raise ValueError(f"工作表“{self.name}”使用了不在白名单内的函数：{', '.join(sorted(bad))}")
        return self

    @property
    def data_range(self) -> tuple[int, int]:
        """(行数含表头, 列数)。"""
        ncols = max([len(self.columns)] + [len(r) for r in self.rows] + [0])
        return len(self.rows) + (1 if self.columns else 0), ncols


class Workbook(Model):
    spec_version: int = 1
    title: str
    sheets: list[Sheet] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def _names(self):
        seen = set()
        for s in self.sheets:
            base, i = s.name, 2
            while s.name in seen:
                s.name = f"{base[:28]}_{i}"
                i += 1
            seen.add(s.name)
        return self

    def sheet(self, sid: str) -> Sheet:
        for s in self.sheets:
            if s.id == sid or s.name == sid:
                return s
        raise KeyError(sid)


def workbook_text(wb: Workbook) -> str:
    out = [wb.title]
    for s in wb.sheets:
        out.append(s.name)
        out.extend(c.header for c in s.columns)
        for r in s.rows[:200]:
            out.append(" ".join(str(c) for c in r if c is not None))
    return "\n".join(out)
