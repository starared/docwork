"""LaTeX 常用子集 → Word 原生公式（OMML）。

支持：分式、上下标、根号、求和/求积/积分、极限、括号（\\left \\right）、矩阵与 cases、
希腊字母、常用运算符与关系符、重音（\\bar \\hat \\vec \\dot \\tilde）、\\text、\\mathrm、\\mathbf、常用函数名。
不支持的写法抛出 UnsupportedLatex，由调用方降级为公式图片。
"""
from __future__ import annotations

import re

from lxml import etree

M = "http://schemas.openxmlformats.org/officeDocument/2006/math"
W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


class UnsupportedLatex(ValueError):
    pass


def m(tag: str, attrs: dict | None = None, children: list | None = None):
    el = etree.Element(f"{{{M}}}{tag}", nsmap={"m": M, "w": W})
    for k, v in (attrs or {}).items():
        el.set(f"{{{M}}}{k}", v)
    for c in children or []:
        el.append(c)
    return el


def prop(tag: str, val: str):
    return m(tag, {"val": val})


GREEK = {
    "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ", "epsilon": "ϵ", "varepsilon": "ε", "zeta": "ζ", "eta": "η",
    "theta": "θ", "vartheta": "ϑ", "iota": "ι", "kappa": "κ", "lambda": "λ", "mu": "μ", "nu": "ν", "xi": "ξ", "pi": "π",
    "varpi": "ϖ", "rho": "ρ", "varrho": "ϱ", "sigma": "σ", "varsigma": "ς", "tau": "τ", "upsilon": "υ", "phi": "ϕ",
    "varphi": "φ", "chi": "χ", "psi": "ψ", "omega": "ω", "Gamma": "Γ", "Delta": "Δ", "Theta": "Θ", "Lambda": "Λ",
    "Xi": "Ξ", "Pi": "Π", "Sigma": "Σ", "Upsilon": "Υ", "Phi": "Φ", "Psi": "Ψ", "Omega": "Ω",
}
SYMBOLS = {
    "times": "×", "cdot": "⋅", "pm": "±", "mp": "∓", "div": "÷", "leq": "≤", "le": "≤", "geq": "≥", "ge": "≥",
    "neq": "≠", "ne": "≠", "approx": "≈", "equiv": "≡", "sim": "∼", "simeq": "≃", "propto": "∝", "infty": "∞",
    "partial": "∂", "nabla": "∇", "rightarrow": "→", "to": "→", "leftarrow": "←", "Rightarrow": "⇒", "Leftarrow": "⇐",
    "leftrightarrow": "↔", "Leftrightarrow": "⇔", "in": "∈", "notin": "∉", "subset": "⊂", "subseteq": "⊆",
    "supset": "⊃", "cup": "∪", "cap": "∩", "forall": "∀", "exists": "∃", "ldots": "…", "cdots": "⋯", "vdots": "⋮",
    "ddots": "⋱", "degree": "°", "circ": "∘", "prime": "′", "angle": "∠", "perp": "⊥", "parallel": "∥",
    "therefore": "∴", "because": "∵", "ll": "≪", "gg": "≫", "emptyset": "∅", "neg": "¬", "land": "∧", "lor": "∨",
    "hbar": "ℏ", "ell": "ℓ", "star": "⋆", "ast": "∗", "bullet": "∙", "uparrow": "↑", "downarrow": "↓", "mid": "∣",
    "%": "%", "{": "{", "}": "}", "_": "_", "#": "#", "&": "&", "$": "$", "|": "‖", "lbrace": "{", "rbrace": "}",
    "langle": "⟨", "rangle": "⟩", "lfloor": "⌊", "rfloor": "⌋", "lceil": "⌈", "rceil": "⌉",
}
NARY = {"sum": "∑", "prod": "∏", "coprod": "∐", "int": "∫", "iint": "∬", "iiint": "∭", "oint": "∮", "bigcup": "⋃", "bigcap": "⋂"}
FUNCS = {"sin", "cos", "tan", "cot", "sec", "csc", "arcsin", "arccos", "arctan", "sinh", "cosh", "tanh", "log", "ln",
         "lg", "exp", "max", "min", "sup", "inf", "det", "dim", "ker", "deg", "gcd", "arg", "Pr"}
LIMFUNCS = {"lim", "limsup", "liminf"}
ACCENTS = {"hat": "̂", "widehat": "̂", "tilde": "̃", "widetilde": "̃", "vec": "⃗", "dot": "̇", "ddot": "̈", "check": "̌", "breve": "̆"}
SPACES = {",": " ", ";": " ", ":": " ", "quad": " ", "qquad": "  ", " ": " ", "!": ""}
DELIMS = {"(": "(", ")": ")", "[": "[", "]": "]", "\\{": "{", "\\}": "}", "|": "|", "\\|": "‖", ".": "",
          "\\langle": "⟨", "\\rangle": "⟩", "\\lfloor": "⌊", "\\rfloor": "⌋", "\\lceil": "⌈", "\\rceil": "⌉",
          "\\lbrace": "{", "\\rbrace": "}"}
ENVS = {"matrix": ("", ""), "pmatrix": ("(", ")"), "bmatrix": ("[", "]"), "Bmatrix": ("{", "}"),
        "vmatrix": ("|", "|"), "Vmatrix": ("‖", "‖"), "cases": ("{", ""), "aligned": ("", ""), "array": ("", "")}

_TOK = re.compile(r"\\[a-zA-Z]+|\\.|[{}^_&]|\s+|[0-9]+(?:\.[0-9]+)?|.", re.S)


def tokenize(s: str) -> list[str]:
    return [t for t in _TOK.findall(s)]


class NaryMarker:
    def __init__(self, ch: str):
        self.ch = ch


class LimMarker:
    def __init__(self, el):
        self.el = el


class Parser:
    def __init__(self, src: str):
        self.toks = tokenize(src)
        self.i = 0

    def peek(self, skip_ws=True):
        j = self.i
        while skip_ws and j < len(self.toks) and self.toks[j].isspace():
            j += 1
        return self.toks[j] if j < len(self.toks) else None

    def next(self, skip_ws=True):
        while skip_ws and self.i < len(self.toks) and self.toks[self.i].isspace():
            self.i += 1
        if self.i >= len(self.toks):
            return None
        t = self.toks[self.i]
        self.i += 1
        return t

    def expect(self, t):
        got = self.next()
        if got != t:
            raise UnsupportedLatex(f"期望 {t}，得到 {got}")

    # 表达式：直到遇到终止符
    def parse_expr(self, stop: set[str] | None = None) -> list:
        stop = stop or set()
        out: list = []
        while True:
            t = self.peek()
            if t is None or t in stop:
                break
            if t == "}":
                break
            atom = self.parse_atom()
            if atom is None:
                continue
            out.extend(self.parse_scripts(atom))
        return out

    def parse_group(self) -> list:
        t = self.peek()
        if t == "{":
            self.next()
            e = self.parse_expr({"}"})
            self.expect("}")
            return e
        a = self.parse_atom()
        if a is None:
            return []
        return self.parse_scripts(a) if isinstance(a, (NaryMarker, LimMarker)) else [a]

    def parse_scripts(self, base) -> list:
        sub = sup = None
        while self.peek() in ("^", "_"):
            op = self.next()
            g = self.parse_group()
            if op == "^":
                sup = g
            else:
                sub = g
        if isinstance(base, NaryMarker):
            return [self.build_nary(base, sub, sup)]
        if isinstance(base, LimMarker):
            if sub is not None:
                return [m("limLow", children=[m("e", children=[base.el]), m("lim", children=sub)])]
            return [base.el]
        if sub is None and sup is None:
            return [base]
        e = m("e", children=[base])
        if sub is not None and sup is not None:
            return [m("sSubSup", children=[e, m("sub", children=sub), m("sup", children=sup)])]
        if sup is not None:
            return [m("sSup", children=[e, m("sup", children=sup)])]
        return [m("sSub", children=[e, m("sub", children=sub)])]

    def build_nary(self, marker, sub, sup):
        chr_ = marker.ch
        props = [prop("chr", chr_), prop("limLoc", "subSup" if chr_ in "∫∬∭∮" else "undOvr")]
        if sub is None:
            props.append(prop("subHide", "1"))
        if sup is None:
            props.append(prop("supHide", "1"))
        # 运算对象：后面紧跟的一个原子（及其上下标）
        body: list = []
        t = self.peek()
        if t is not None and t not in ("}", "&", "\\\\", "=", "+", "-") and not (t.startswith("\\") and t[1:] in ("right", "end")):
            a = self.parse_atom()
            if a is not None:
                body = self.parse_scripts(a)
        return m("nary", children=[m("naryPr", children=props), m("sub", children=sub or []), m("sup", children=sup or []), m("e", children=body)])

    def parse_atom(self):
        t = self.next()
        if t is None:
            return None
        if t.isspace():
            return None
        if t == "{":
            e = self.parse_expr({"}"})
            self.expect("}")
            return _box(e)
        if t in ("^", "_"):
            # 没有底的上下标：用空底
            self.i -= 1
            return run("")
        if t.startswith("\\"):
            return self.parse_command(t[1:])
        if t == "&":
            raise UnsupportedLatex("& 只能出现在矩阵环境中")
        if re.fullmatch(r"[0-9]+(\.[0-9]+)?", t):
            return run(t, upright=True)
        if t in "+-=<>,;:!?()[]|/'.*":
            ch = {"-": "−", "*": "∗", "'": "′"}.get(t, t)
            return run(ch, upright=True)
        return run(t)

    def parse_command(self, name: str):
        if name in GREEK:
            return run(GREEK[name], upright=name[0].isupper())
        if name in SYMBOLS:
            return run(SYMBOLS[name], upright=True)
        if name in SPACES:
            return run(SPACES[name], upright=True) if SPACES[name] else None
        if name in ("frac", "dfrac", "tfrac", "cfrac"):
            num = self.parse_group()
            den = self.parse_group()
            return m("f", children=[m("num", children=num), m("den", children=den)])
        if name == "binom":
            a = self.parse_group()
            b = self.parse_group()
            f = m("f", children=[m("fPr", children=[prop("type", "noBar")]), m("num", children=a), m("den", children=b)])
            return _delim("(", ")", [f])
        if name == "sqrt":
            deg = None
            if self.peek() == "[":
                self.next()
                deg = self.parse_expr({"]"})
                self.expect("]")
            e = self.parse_group()
            if deg:
                return m("rad", children=[m("deg", children=deg), m("e", children=e)])
            return m("rad", children=[m("radPr", children=[prop("degHide", "1")]), m("deg"), m("e", children=e)])
        if name in NARY:
            return NaryMarker(NARY[name])
        if name in LIMFUNCS:
            return LimMarker(run({"lim": "lim", "limsup": "lim sup", "liminf": "lim inf"}[name], upright=True))
        if name in FUNCS:
            return run(name, upright=True)
        if name in ("text", "mathrm", "textrm", "operatorname", "mbox"):
            return run(self.raw_group(), upright=True, text=name in ("text", "textrm", "mbox"))
        if name in ("mathbf", "boldsymbol", "bf"):
            e = self.parse_group()
            for r_ in e:
                for rpr in r_.iter(f"{{{M}}}rPr"):
                    rpr.append(prop("sty", "b"))
            return _box(e)
        if name in ("mathit", "mathcal", "mathbb", "mathsf"):
            return _box(self.parse_group())
        if name in ("bar", "overline"):
            e = self.parse_group()
            return m("bar", children=[m("barPr", children=[prop("pos", "top")]), m("e", children=e)])
        if name == "underline":
            e = self.parse_group()
            return m("bar", children=[m("barPr", children=[prop("pos", "bot")]), m("e", children=e)])
        if name in ACCENTS:
            e = self.parse_group()
            return m("acc", children=[m("accPr", children=[prop("chr", ACCENTS[name])]), m("e", children=e)])
        if name == "left":
            open_ = self.delim_token()
            body = self.parse_expr({"\\right"})
            if self.next() != "\\right":
                raise UnsupportedLatex("\\left 缺少对应的 \\right")
            close = self.delim_token()
            return _delim(open_, close, body)
        if name in ("big", "Big", "bigg", "Bigg", "bigl", "bigr", "Bigl", "Bigr"):
            return run(self.delim_token(), upright=True)
        if name == "begin":
            env = self.raw_group()
            if env.endswith("*"):
                env = env[:-1]
            if env not in ENVS:
                raise UnsupportedLatex(f"不支持的环境 {env}")
            if env == "array":
                self.raw_group()
            rows = self.parse_rows(env)
            open_, close = ENVS[env]
            if env in ("cases", "aligned"):
                eq = m("eqArr", children=[m("e", children=sum(r, [])) for r in rows])
                return _delim(open_, close, [eq]) if open_ else eq
            mat = m("m", children=[m("mr", children=[m("e", children=cell) for cell in r]) for r in rows])
            return _delim(open_, close, [mat]) if open_ else mat
        if name == "end":
            raise UnsupportedLatex("多余的 \\end")
        if name in ("displaystyle", "textstyle", "limits", "nolimits"):
            return None
        raise UnsupportedLatex(f"不支持的命令 \\{name}")

    def delim_token(self) -> str:
        t = self.next()
        if t is None:
            raise UnsupportedLatex("缺少括号")
        if t in DELIMS:
            return DELIMS[t]
        raise UnsupportedLatex(f"不支持的括号 {t}")

    def raw_group(self) -> str:
        if self.peek() != "{":
            t = self.next()
            return t or ""
        self.next()
        depth, out = 1, []
        while self.i < len(self.toks):
            t = self.toks[self.i]
            self.i += 1
            if t == "{":
                depth += 1
            elif t == "}":
                depth -= 1
                if depth == 0:
                    break
            out.append(t)
        return "".join(out)

    def parse_rows(self, env: str) -> list[list[list]]:
        rows, row, cell = [], [], []
        while True:
            t = self.peek()
            if t is None:
                raise UnsupportedLatex(f"环境 {env} 未结束")
            if t == "\\end":
                self.next()
                self.raw_group()
                row.append(cell)
                rows.append(row)
                break
            if t == "&":
                self.next()
                row.append(cell)
                cell = []
                continue
            if t == "\\\\":
                self.next()
                row.append(cell)
                rows.append(row)
                row, cell = [], []
                continue
            a = self.parse_atom()
            if a is not None:
                cell.extend(self.parse_scripts(a))
        # 去掉末尾空行
        rows = [r for r in rows if any(c for c in r)]
        n = max(len(r) for r in rows) if rows else 1
        for r in rows:
            while len(r) < n:
                r.append([])
        if env in ("cases", "aligned"):
            # 每行用 & 分隔的内容合并为一个 e
            rows = [[sum(r, [])] for r in rows]
        return rows


def run(txt: str, upright: bool = False, text: bool = False):
    r = m("r")
    rpr = m("rPr")
    if text:
        rpr.append(m("nor"))
    if upright:
        rpr.append(prop("sty", "p"))
    r.append(rpr)
    wr = etree.SubElement(r, f"{{{W}}}rPr")
    f = etree.SubElement(wr, f"{{{W}}}rFonts")
    f.set(f"{{{W}}}ascii", "Cambria Math")
    f.set(f"{{{W}}}hAnsi", "Cambria Math")
    t = m("t")
    t.text = txt
    t.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
    r.append(t)
    return r


def _box(children: list):
    """把多个元素包成一个整体（用作上下标的底等）。"""
    if len(children) == 1:
        return children[0]
    return m("box", children=[m("e", children=children)])


def _delim(open_: str, close: str, body: list):
    props = [prop("begChr", open_), prop("endChr", close)]
    return m("d", children=[m("dPr", children=props), m("e", children=body)])


def latex_to_omath(latex: str) -> etree._Element:
    """返回 m:oMath 元素。"""
    src = latex.strip().strip("$").strip()
    src = re.sub(r"^\\\[|\\\]$", "", src).strip()
    p = Parser(src)
    items = p.parse_expr()
    if p.peek() is not None:
        raise UnsupportedLatex(f"无法解析：{p.peek()}")
    return m("oMath", children=items)


def latex_to_omath_para(latex: str) -> etree._Element:
    om = latex_to_omath(latex)
    return m("oMathPara", children=[m("oMathParaPr", children=[prop("jc", "center")]), om])


def is_supported(latex: str) -> bool:
    try:
        latex_to_omath(latex)
        return True
    except UnsupportedLatex:
        return False
