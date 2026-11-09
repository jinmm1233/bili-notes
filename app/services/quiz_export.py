"""自测卷导出：把一份卷子排成"试卷 + 答案"的纸面。

为什么不复用笔记的导出：
  - 笔记导出是"读"的排版（大标题、图片、折叠转录），卷子是"做"的排版——
    题号连续、选项对齐、留答题空间、答案统一放最后。两套骨架硬套只会互相拖累。
  - 但排版内核可以复用：`export_docx.export` 吃一个笔记目录，所以这里把卷子
    渲染成一份临时 Markdown（公式是 `$...$`，它会转成 Word 原生公式），
    再交给它出 .docx。
  - PDF 走浏览器无头打印（和笔记一致），页面是前端的 `/?printquiz=<id>`，
    公式由 KaTeX 现成渲染，不用在 Python 里再实现一遍。

答案放最后而不是每题下面：打印出来可以先做，做完再对答案。
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path
from typing import Any

from . import export_docx

log = logging.getLogger(__name__)

KIND_CN = {
    "single": "单项选择题",
    "multi": "多项选择题",
    "judge": "判断题",
    "blank": "填空题",
    "short": "简答题",
}
KIND_ORDER = ("single", "multi", "judge", "blank", "short")
_LETTERS = "ABCDEFGH"


def _one_line(text: Any) -> str:
    """把多行题干压成一行：Markdown 段内换行会被当软换行，卷面看着像断句。"""
    parts = [seg.strip() for seg in str(text or "").replace("\r", "").split("\n")]
    return "  ".join(p for p in parts if p)


def to_markdown(quiz: dict[str, Any], with_answers: bool = True) -> str:
    """卷子 → Markdown。标题放 frontmatter，正文从题型分组开始。"""
    questions: list[dict[str, Any]] = list(quiz.get("questions") or [])
    title = str(quiz.get("title") or "自测卷").replace("\n", " ").strip()
    lines: list[str] = ["---", f"title: {title}", "---", ""]

    # 按题型分组，但题号连续（1..n），和真实试卷一致
    groups: list[tuple[str, list[dict[str, Any]]]] = []
    for kind in KIND_ORDER:
        bucket = [q for q in questions if q.get("kind") == kind]
        if bucket:
            groups.append((kind, bucket))
    known = {k for k, _ in groups}
    rest = [q for q in questions if q.get("kind") not in known]
    if rest:
        groups.append(("", rest))

    numbered: list[tuple[int, dict[str, Any]]] = []
    index = 0
    for kind, bucket in groups:
        heading = KIND_CN.get(kind) or "其他题目"
        lines += [f"## {heading}", ""]
        for q in bucket:
            index += 1
            numbered.append((index, q))
            stem = _one_line(q.get("stem"))
            if kind == "judge":
                # 判断题在题干后留括号，纸上才有地方写"对/错"
                lines += [f"**{index}.** {stem}（　　）", ""]
            else:
                lines += [f"**{index}.** {stem}", ""]
            opts = q.get("options") or []
            if kind in ("single", "multi") and opts:
                for i, opt in enumerate(opts):
                    mark = _LETTERS[i] if i < len(_LETTERS) else str(i + 1)
                    lines.append(f"- {mark}. {_one_line(opt)}")
                lines.append("")
            if kind == "short":
                # 简答要留白，否则纸上没处写
                lines += ["答：", "", "　", ""]
            if kind == "blank":
                lines.append("")

    if with_answers:
        lines += ["## 参考答案", ""]
        for number, q in numbered:
            answer = _answer_of(q)
            lines.append(f"**{number}.** {answer}")
            explain = _one_line(q.get("explain"))
            if explain:
                lines.append(f"> {explain}")
            lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _answer_of(q: dict[str, Any]) -> str:
    """答案文字：选择题带字母（A. 选项原文），其它按题型。"""
    kind = q.get("kind")
    opts = q.get("options") or []
    if kind in ("single", "multi"):
        picked = q.get("answer") or []
        if not isinstance(picked, list):
            picked = [picked]
        out = []
        for i in picked:
            try:
                pos = int(i)
            except (TypeError, ValueError):
                continue
            if 0 <= pos < len(opts):
                mark = _LETTERS[pos] if pos < len(_LETTERS) else str(pos + 1)
                out.append(f"{mark}. {_one_line(opts[pos])}")
        return "；".join(out) or "（未给出）"
    if kind == "judge":
        return "对（√）" if q.get("answer") else "错（×）"
    if kind == "blank":
        return "／".join(str(a) for a in (q.get("answer") or [])) or "（未给出）"
    if kind == "short":
        return _one_line(q.get("answer")) or "（未给出）"
    return "（未给出）"


def export_docx_file(quiz: dict[str, Any], out: Path) -> Path:
    """卷子 → .docx。借笔记导出的排版内核，所以先落一份临时 notes.md。"""
    md = to_markdown(quiz)
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="bn_quiz_") as raw:
        folder = Path(raw)
        (folder / "notes.md").write_text(md, encoding="utf-8")
        export_docx.export(folder, out)
    return out


def export_pdf_url(base_url: str, quiz_id: str) -> str:
    """打印页地址：前端渲染成卷面（公式用 KaTeX），后台无头打印成 PDF。"""
    from urllib.parse import quote

    return f"{base_url.rstrip('/')}/?printquiz={quote(str(quiz_id))}"


__all__ = ["KIND_CN", "to_markdown", "export_docx_file", "export_pdf_url"]
