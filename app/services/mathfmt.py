"""数学写法归一化：把模型爱用的"伪公式"转成 LaTeX，笔记里才能真渲染成公式。

为什么需要这一层：
    看图模型（如 glm-4.6v-flashx）能把 PPT 上的矩阵读得一个不差，
    但它爱写成 [[3,2,1],[3,1,5],[3,4,-3]] 或 A^-1 这种列表/纯文本写法，
    落到 Markdown 里就是一堆字符，读者看到的不是矩阵。
    提示词里已经明令要求 LaTeX，但小模型不一定听话，所以再加一层兜底：

    [[3,2,1],[3,1,5]]            -> $$\\begin{bmatrix} 3 & 2 & 1 \\\\ 3 & 1 & 5 \\end{bmatrix}$$
    [1 2 3; 4 5 6]               -> $$\\begin{bmatrix} 1 & 2 & 3 \\\\ 4 & 5 & 6 \\end{bmatrix}$$
    A^-1 / A⁻¹                   -> $A^{-1}$
    [A:E] / [A|E]                -> $[A \\mid E]$

笔记是 .md：LaTeX 在 BiliNotes 阅读器（内置 KaTeX）和 Obsidian 里都能显示。
"""
from __future__ import annotations

import re

# ---- 矩阵：[[a,b],[c,d]] 这种嵌套列表写法 ----
_ROWS_RE = re.compile(r"\[\s*\[([^\[\]]*)\]\s*(?:,\s*\[([^\[\]]*)\]\s*)*\]")

# ---- 矩阵：[a b c; d e f] 或 (a b; c d) ----
_SEMI_RE = re.compile(r"[\[\(]\s*([^\[\]\(\);|]{1,120};[^\[\]\(\)]{1,120})\s*[\]\)]")

# ---- 裸上标：A^-1、x^2、A^{T}（尾部空白不能吃掉：\s*\}? 会把"与"前的空格吞掉）----
_CARET_RE = re.compile(
    r"(?<![A-Za-z0-9$}\\])([A-Za-z][A-Za-z0-9]*)\s*\^\s*(?:\{([^{}$]*)\}|(-?\d+|[A-Za-z][A-Za-z0-9]*))"
)

# ---- Unicode 上标：A⁻¹、x² ----
_UNI_SUP = {"⁻": "-", "⁰": "0", "¹": "1", "²": "2", "³": "3", "⁴": "4",
             "⁵": "5", "⁶": "6", "⁷": "7", "⁸": "8", "⁹": "9", "⁺": "+"}
_UNI_RE = re.compile(r"([A-Za-z][A-Za-z0-9]*)([" + "".join(_UNI_SUP) + r"]+)")

# ---- 增广矩阵：[A:E]、[A|E]、[E:A^-1]（两侧不许出现中文，避免误伤"[提示:xxx]"）----
_AUG_RE = re.compile(
    r"\[\s*([A-Za-z][A-Za-z0-9]*)\s*[:|]\s*([A-Za-z0-9\^\{\}\\$\-\s]{1,24}?)\s*\]"
)

# 已经有 $ 包裹的段落先保护起来，避免二次加工
_DOLLAR_RE = re.compile(r"\$\$[\s\S]+?\$\$|\$[^$\n]+?\$")

_MATRIX_MIN_CELLS = 4      # 2x2 起步，单个 [1,2] 不转
_MAX_CELL_LEN = 24


def _split_row(text: str) -> list[str]:
    """一行里的元素：优先按逗号，其次按空格/分号。"""
    parts = [p.strip() for p in re.split(r"[,，]", text) if p.strip()]
    if len(parts) < 2:
        parts = [p.strip() for p in re.split(r"[\s;]{1,3}", text) if p.strip()]
    return parts


def _is_numeric_cell(cell: str) -> bool:
    """矩阵元素：数字、分数、小数、负号、单个变量都算；长句子不算。"""
    if len(cell) > _MAX_CELL_LEN:
        return False
    return bool(re.fullmatch(r"[-+±]?[\d\.,/\\\(\)\s]*\d[\d\.,/\\\(\)\s]*|[-+±]?\d*\s*/\s*\d+|[A-Za-z]", cell.strip()))


def _to_bmatrix(rows: list[list[str]]) -> str:
    body = r" \\ ".join(" & ".join(r) for r in rows)
    return r"$$\begin{bmatrix} " + body + r" \end{bmatrix}$$"


def _list_matrix(text: str) -> str:
    def repl(m: re.Match) -> str:
        raw = m.group(0)
        rows = [_split_row(r) for r in re.findall(r"\[([^\[\]]*)\]", raw)]
        rows = [r for r in rows if r]
        if len(rows) < 2 or len(rows) > 12:
            return raw
        widths = {len(r) for r in rows}
        if len(widths) != 1:                      # 每行元素数必须一致
            return raw
        ncol = rows[0].__len__()
        if ncol < 2 or ncol > 10 or ncol * len(rows) < _MATRIX_MIN_CELLS:
            return raw
        if not all(_is_numeric_cell(c) for r in rows for c in r):
            return raw
        return _to_bmatrix(rows)

    return _ROWS_RE.sub(repl, text)


def _semi_matrix(text: str) -> str:
    def repl(m: re.Match) -> str:
        raw = m.group(0)
        rows = [_split_row(r) for r in m.group(1).split(";")]
        rows = [r for r in rows if r]
        if len(rows) < 2 or len({len(r) for r in rows}) != 1:
            return raw
        ncol = len(rows[0])
        if ncol < 2 or ncol * len(rows) < _MATRIX_MIN_CELLS:
            return raw
        if not all(_is_numeric_cell(c) for r in rows for c in r):
            return raw
        return _to_bmatrix(rows)

    return _SEMI_RE.sub(repl, text)


def _caret(text: str) -> str:
    def repl(m: re.Match) -> str:
        exp = m.group(2) if m.group(2) is not None else m.group(3)
        return f"${m.group(1)}^{{{exp}}}$"

    return _CARET_RE.sub(repl, text)


def _unicode_sup(text: str) -> str:
    def repl(m: re.Match) -> str:
        exp = "".join(_UNI_SUP.get(ch, "") for ch in m.group(2))
        if not exp:
            return m.group(0)
        return f"${m.group(1)}^{{{exp}}}$"

    return _UNI_RE.sub(repl, text)


def _augmented(text: str) -> str:
    def repl(m: re.Match) -> str:
        right = m.group(2).strip()
        # 上标在这里就地补花括号（A^-1 -> A^{-1}），不要等 _caret 去包 $，
        # 否则会得到 $[E \mid $A^{-1}$]$ 这种 $ 套 $ 的坏式子。
        right = re.sub(
            r"([A-Za-z][A-Za-z0-9]*)\^\{?(-?\d+|[A-Za-z][A-Za-z0-9]*)\}?",
            r"\1^{\2}", right,
        )
        # 模型可能已经写了 $[E:$A^{-1}$]$：剥掉内层自带的 $，别套成 $$...$$
        if len(right) > 2 and right.startswith("$") and right.endswith("$"):
            right = right[1:-1]
        return f"$[{m.group(1)} \\mid {right}]$"

    return _AUG_RE.sub(repl, text)


def _protect(text: str, hold: list[str]) -> str:
    """把已存在的 $...$ 抽成占位符，后续规则就不会二次加工它们。"""
    def keep(m: re.Match) -> str:
        hold.append(m.group(0))
        return f"\x00{len(hold) - 1}\x00"

    return _DOLLAR_RE.sub(keep, text)


def normalize(text: str) -> str:
    """把一段笔记文字里的伪公式转成 LaTeX。已经是 $...$ 的不动。"""
    if not text:
        return text
    hold: list[str] = []
    # 增广矩阵先跑：模型有时自己就写了 [E:$A^{-1}$]，一旦被抽成占位符就够不着了
    out = _augmented(text)
    out = _protect(out, hold)
    for step in (_list_matrix, _semi_matrix, _caret, _unicode_sup):
        out = step(out)
        out = _protect(out, hold)      # 本步刚生成的公式也要保护起来
    return re.sub(r"\x00(\d+)\x00", lambda m: hold[int(m.group(1))], out)


def for_mindmap(text: str) -> str:
    """导图层：公式在树状图里渲染不了，块级公式换成一句提示，行内的去掉 $。

    树节点只有一行宽，塞一整个 LaTeX 矩阵只会把导图搞乱。
    """
    if not text:
        return text
    out = re.sub(r"\$\$[\s\S]+?\$\$", "（矩阵/公式见正文）", text)
    out = re.sub(r"\$([^$\n]+?)\$", r"\1", out)
    out = re.sub(r"\\(begin|end)\{[a-zA-Z]*\}|\\[,&]|\\mid", " ", out)
    # x^{2} 这类上标在导图上用 Unicode 更短：A^{-1} -> A⁻¹
    _SUP = str.maketrans("0123456789+-", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻")
    out = re.sub(r"\^\{([^}]*)\}", lambda m: m.group(1).translate(_SUP), out)
    return re.sub(r"\s{2,}", " ", out).strip()
