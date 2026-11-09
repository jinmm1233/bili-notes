"""把一篇笔记导出成 Word（.docx），目标是**打印好看的讲义**，不是像素级还原网页。

设计取舍：
  - 结构要准确：标题层级、列表、引用、表格、代码块、图片都映射成 Word 原生元素，
    这样用户拿到后还能自己改字号、加批注、排版。
  - 公式要真公式：LaTeX 先转 MathML 再转 OMML（Office Math），在 Word 里可编辑、
    可随字号缩放；转换失败就退回等宽文本，绝不吞掉内容。
  - 打印优先：A4、2cm 页边距、1.5 倍行距、页脚页码、图片不跨页、标题不孤立在页底。
  - 知识地图：网页里是 mermaid，Word 里画不了，改读 mindmap.json 输出层级列表。

只依赖 python-docx + latex2mathml + mathml2omml（都在 vendor/ 里随包分发）。
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_LINE_SPACING
from docx.oxml import OxmlElement, parse_xml
from docx.oxml.ns import nsdecls, qn
from docx.shared import Cm, Inches, Pt, RGBColor

log = logging.getLogger(__name__)

# 打印时没意义、或者纯粹是给网页看的区块，直接丢掉
DROP_SECTIONS = ("文本校验",)

_INLINE = re.compile(
    r"(?P<math>\$\$[\s\S]+?\$\$|\$[^$\n]+?\$)"
    r"|(?P<bold>\*\*[^*\n]+?\*\*)"
    r"|(?P<hl>==[^=\n]+?==)"
    r"|(?P<code>`[^`\n]+?`)"
    r"|(?P<link>\[([^\]\n]+)\]\(([^)\s]+)\))"
    r"|(?P<sub><sub>([\s\S]*?)</sub>)"
    r"|(?P<ital>\*[^*\n]+?\*)",
    re.X,
)
_IMG = re.compile(r"^!\[([^\]]*)\]\(([^)\s]+)\)\s*$")
_HEAD = re.compile(r"^(#{1,4})\s+(.*)$")


# ---------------------------------------------------------------- 解析
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _safe(text: str) -> str:
    """去掉 XML 不允许的控制字符（\t\n 由调用方另行处理）。"""
    return _CTRL.sub("", text or "")


def _split_frontmatter(text: str) -> tuple[dict[str, str], str]:
    match = re.match(r"\A---\n([\s\S]*?)\n---\n?", text)
    if not match:
        return {}, text
    meta: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if ":" not in line:
            continue
        key, raw = line.split(":", 1)
        meta[key.strip()] = raw.strip().strip('"').strip("'")
    return meta, text[match.end():]


def _iter_blocks(body: str, include_transcript: bool) -> Iterator[tuple]:
    """把 Markdown 拆成块。只覆盖笔记生成器实际会写出的语法。"""
    lines = body.replace("\r\n", "\n").split("\n")
    i = 0
    in_code = False
    code_lang, code_buf = "", []
    table_buf: list[str] = []
    skipping_section = False

    def flush_table() -> tuple | None:
        nonlocal table_buf
        if not table_buf:
            return None
        rows = []
        for row in table_buf:
            cells = [c.strip() for c in row.strip().strip("|").split("|")]
            if all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
                continue                      # 分隔行 |---|---|
            rows.append(cells)
        table_buf = []
        return ("table", rows) if rows else None

    while i < len(lines):
        raw = lines[i]
        line = raw.rstrip()

        # 代码块
        if line.startswith("```"):
            if in_code:
                yield ("code", code_lang, "\n".join(code_buf))
                in_code, code_buf, code_lang = False, [], ""
            else:
                if table_buf:
                    blk = flush_table()
                    if blk:
                        yield blk
                in_code, code_lang = True, line[3:].strip()
            i += 1
            continue
        if in_code:
            code_buf.append(raw)
            i += 1
            continue

        # 表格
        if line.startswith("|") and line.endswith("|"):
            table_buf.append(line)
            i += 1
            continue
        if table_buf:
            blk = flush_table()
            if blk:
                yield blk

        stripped = line.strip()

        # 折叠块：完整转录。默认丢；勾选时只保留文本
        if stripped.startswith("<details"):
            end_idx = i
            for j in range(i, len(lines)):
                if "</details>" in lines[j]:
                    end_idx = j
                    break
            inner = "\n".join(lines[i + 1:end_idx])
            summary = ""
            m = re.search(r"<summary>([\s\S]*?)</summary>", inner)
            if m:
                summary = re.sub(r"<[^>]+>", "", m.group(1)).strip()
                inner = inner[m.end():]
            inner = re.sub(r"<[^>]+>", "", inner).strip()
            if include_transcript and inner:
                yield ("h", 2, summary or "完整转录")
                for para in re.split(r"\n{2,}", inner):
                    para = para.strip()
                    if para:
                        yield ("p", para)
            i = end_idx + 1
            continue

        # 标题
        m = _HEAD.match(stripped)
        if m:
            level = len(m.group(1))
            title = re.sub(r"\s*<sub>[\s\S]*?</sub>", "", m.group(2)).strip()
            # 网页专用区块：整段跳过，直到下一个同级或更高级标题
            if level <= 2 and any(k in title for k in DROP_SECTIONS):
                skipping_section = True
                i += 1
                continue
            if skipping_section and level <= 2:
                skipping_section = False
            if not skipping_section:
                yield ("h", level, title)
            i += 1
            continue
        if skipping_section:
            i += 1
            continue

        if not stripped:
            i += 1
            continue

        # 分隔线
        if re.fullmatch(r"[-*_]{3,}", stripped):
            yield ("hr",)
            i += 1
            continue

        # 独立公式
        if stripped.startswith("$$"):
            buf = [stripped]
            if not (stripped.endswith("$$") and len(stripped) > 4):
                i += 1
                while i < len(lines) and not lines[i].strip().endswith("$$"):
                    buf.append(lines[i])
                    i += 1
                if i < len(lines):
                    buf.append(lines[i])
            tex = "\n".join(buf).strip().strip("$").strip()
            yield ("math", tex)
            i += 1
            continue

        # 图片
        m = _IMG.match(stripped)
        if m:
            yield ("img", m.group(2), m.group(1))
            i += 1
            continue

        # 引用块（连续 > 行合并）
        if stripped.startswith(">"):
            buf = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                buf.append(re.sub(r"^\s*>\s?", "", lines[i]).rstrip())
                i += 1
            yield ("quote", buf)
            continue

        # 列表
        m = re.match(r"^(\s*)([-*+]|\d+[.)])\s+(.*)$", raw)
        if m:
            indent = len(m.group(1).replace("\t", "  ")) // 2
            ordered = bool(re.match(r"\d", m.group(2)))
            yield ("ol" if ordered else "ul", min(indent, 2), m.group(3).strip())
            i += 1
            continue

        # 普通段落：连续的同一段多行合并
        buf = [stripped]
        i += 1
        while i < len(lines):
            nxt = lines[i].strip()
            if (not nxt or nxt.startswith(("#", ">", "|", "```", "![", "$$"))
                    or re.match(r"^([-*+]|\d+[.)])\s+", nxt)
                    or re.fullmatch(r"[-*_]{3,}", nxt)):
                break
            buf.append(nxt)
            i += 1
        yield ("p", " ".join(buf))


# ---------------------------------------------------------------- 公式
def _omml(tex: str) -> Any | None:
    """LaTeX → OMML 节点；失败返回 None（调用方退回文本）。"""
    try:
        import latex2mathml.converter as l2m
        import mathml2omml

        mathml = l2m.convert(tex)
        omml = mathml2omml.convert(mathml)
        wrapper = f"<root {nsdecls('w', 'm')}>{omml}</root>"
        root = parse_xml(wrapper)
        return list(root)[0] if len(root) else None
    except Exception as exc:                     # 公式千奇百怪，绝不能因此中断导出
        log.debug("公式转换失败，退回文本：%s (%s)", tex[:40], exc)
        return None


# ---------------------------------------------------------------- 样式
def _set_east_asia(obj, font: str) -> None:
    """给 run / style 设置中文字体（rPr 可能还不存在，要现造）。"""
    rPr = obj.get_or_add_rPr()
    rFonts = rPr.find(qn("w:rFonts"))
    if rFonts is None:
        rFonts = OxmlElement("w:rFonts")
        rPr.append(rFonts)
    rFonts.set(qn("w:eastAsia"), font)


def _setup_document(doc: Document) -> None:
    section = doc.sections[0]
    section.page_height = Cm(29.7)
    section.page_width = Cm(21.0)
    section.top_margin = Cm(2.0)
    section.bottom_margin = Cm(2.0)
    section.left_margin = Cm(2.2)
    section.right_margin = Cm(2.2)

    normal = doc.styles["Normal"]
    normal.font.name = "Calibri"
    normal.font.size = Pt(10.5)
    _set_east_asia(normal.element, "宋体")
    pf = normal.paragraph_format
    pf.line_spacing_rule = WD_LINE_SPACING.MULTIPLE
    pf.line_spacing = 1.45
    pf.space_after = Pt(4)

    for level, size in ((1, 17), (2, 14), (3, 12), (4, 11)):
        style = doc.styles[f"Heading {level}"]
        style.font.size = Pt(size)
        style.font.bold = True
        style.font.color.rgb = RGBColor(0x1A, 0x1A, 0x1A)
        style.font.name = "Calibri"
        _set_east_asia(style.element, "黑体")
        style.paragraph_format.space_before = Pt(14 if level <= 2 else 10)
        style.paragraph_format.space_after = Pt(6)
        style.paragraph_format.keep_with_next = True


def _footer_pageno(doc: Document) -> None:
    para = doc.sections[0].footer.paragraphs[0]
    para.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = para.add_run("第 ")
    run.font.size = Pt(9)
    run.font.color.rgb = RGBColor(0x88, 0x88, 0x88)

    fld = OxmlElement("w:fldSimple")
    fld.set(qn("w:instr"), "PAGE")
    inner = OxmlElement("w:r")
    text = OxmlElement("w:t")
    text.text = "1"
    inner.append(text)
    fld.append(inner)
    para._p.append(fld)

    tail = para.add_run(" 页")
    tail.font.size = Pt(9)
    tail.font.color.rgb = RGBColor(0x88, 0x88, 0x88)


def _shade(paragraph, color: str = "F4F4F4") -> None:
    pPr = paragraph._p.get_or_add_pPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:fill"), color)
    pPr.append(shd)


# ---------------------------------------------------------------- 行内
def _add_inline(para, text: str, base_size: float | None = None, dim: bool = False) -> None:
    """把一段行内 Markdown 写进段落：粗体、斜体、行内码、高亮、链接、公式、下标。"""
    text = _safe(text)
    pos = 0
    for m in _INLINE.finditer(text):
        if m.start() > pos:
            run = para.add_run(text[pos:m.start()])
            if dim:
                run.font.size = Pt(base_size or 9)
                run.font.color.rgb = RGBColor(0x77, 0x77, 0x77)
        token = m.group(0)

        if m.group("math") is not None:
            tex = token[2:-2].strip() if token.startswith("$$") else token[1:-1].strip()
            node = _omml(tex)
            if node is None:
                run = para.add_run(token)
                run.font.name = "Consolas"
                run.font.size = Pt((base_size or 10.5) - 0.5)
                run.font.color.rgb = RGBColor(0x66, 0x33, 0x99)
            else:
                para._p.append(node)
        elif m.group("bold") is not None:
            run = para.add_run(token[2:-2])
            run.bold = True
        elif m.group("hl") is not None:
            run = para.add_run(token[2:-2])
            highlight = OxmlElement("w:highlight")
            highlight.set(qn("w:val"), "yellow")
            run._element.get_or_add_rPr().append(highlight)
        elif m.group("code") is not None:
            run = para.add_run(token[1:-1])
            run.font.name = "Consolas"
            run.font.size = Pt((base_size or 10.5) - 0.5)
        elif m.group("link") is not None:
            run = para.add_run(m.group(6))        # 只印文字，URL 打印出来没意义
            run.font.color.rgb = RGBColor(0x1F, 0x4E, 0x9C)
        elif m.group("sub") is not None:
            inner = re.sub(r"<[^>]+>", "", m.group(9)).strip()
            if inner:
                before = len(para.runs)
                _add_inline(para, f" {inner}", base_size)   # 时间戳里可能有链接，继续解析
                for run in para.runs[before:]:
                    run.font.size = Pt((base_size or 10.5) - 2)
                    if not (run.font.color and run.font.color.rgb):
                        run.font.color.rgb = RGBColor(0x88, 0x88, 0x88)
        elif m.group("ital") is not None:
            run = para.add_run(token[1:-1])
            run.italic = True
        pos = m.end()

    if pos < len(text):
        run = para.add_run(text[pos:])
        if dim:
            run.font.size = Pt(base_size or 9)
            run.font.color.rgb = RGBColor(0x77, 0x77, 0x77)


# ---------------------------------------------------------------- 块级
def _write_mindmap(doc: Document, note_dir: Path) -> None:
    path = note_dir / "mindmap.json"
    if not path.is_file():
        return
    try:
        tree = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return

    doc.add_heading("知识地图", level=2)

    def walk(node: dict, depth: int) -> None:
        name = str(node.get("name") or "").strip()
        if name:
            style = doc.styles["List Bullet"] if depth else "List Bullet"
            try:
                para = doc.add_paragraph(name, style=style)
            except KeyError:                       # 模板缺样式时退回普通段落
                para = doc.add_paragraph("• " + name)
            para.paragraph_format.left_indent = Cm(0.6 * depth)
            para.paragraph_format.space_after = Pt(2)
            for run in para.runs:
                run.font.size = Pt(10 if depth else 10.5)
                if not depth:
                    run.bold = True
        for child in node.get("children") or []:
            walk(child, depth + 1)

    walk(tree, 0)


def _write_table(doc: Document, rows: list[list[str]]) -> None:
    cols = max(len(r) for r in rows)
    table = doc.add_table(rows=0, cols=cols)
    table.style = "Table Grid"
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    for index, row in enumerate(rows):
        cells = table.add_row().cells
        for col in range(cols):
            text = row[col] if col < len(row) else ""
            para = cells[col].paragraphs[0]
            para.paragraph_format.space_after = Pt(2)
            _add_inline(para, text, base_size=9.5)
            if index == 0:
                for run in para.runs:
                    run.bold = True
    doc.add_paragraph().paragraph_format.space_after = Pt(2)


def _write_code(doc: Document, lang: str, code: str) -> None:
    if lang.strip().lower() == "mermaid":
        return                                     # 地图另有渲染路径
    for line in code.splitlines() or [""]:
        para = doc.add_paragraph()
        pf = para.paragraph_format
        pf.space_after = Pt(0)
        pf.space_before = Pt(0)
        pf.line_spacing = 1.1
        pf.left_indent = Cm(0.4)
        _shade(para)
        run = para.add_run(_safe(line) if line else " ")
        run.font.name = "Consolas"
        run.font.size = Pt(9)
    doc.add_paragraph().paragraph_format.space_after = Pt(2)


# ---------------------------------------------------------------- 入口
def _is_repeat_image(path: Path, seen: list) -> bool:
    """感知哈希判重：相邻帧差异很小的话，打印出来几乎一样，没必要重复占页面。

    seen 里存已插入图片的 pHash；汉明距离 <= 8 视为同一张。
    imagehash 缺失时退化为按文件大小+尺寸粗判，绝不让导出失败。
    """
    try:
        from PIL import Image
        import imagehash

        with Image.open(path) as img:
            h = imagehash.phash(img.convert("RGB").resize((64, 64)))
        if any(abs(h - old) <= 8 for old in seen):
            return True
        seen.append(h)
        return False
    except Exception:
        key = (path.stat().st_size if path.exists() else 0)
        if key in seen:
            return True
        seen.append(key)
        return False

def export(note_dir: Path, out: Path, include_transcript: bool = False) -> Path:
    src = note_dir / "notes.md"
    text = src.read_text(encoding="utf-8", errors="replace")
    # 生词标注带的三样东西对 Word 没意义：HTML 注释会原样显示成文字、
    # <a id> 锚点不可点、跳转链接在纸上点不动。词表内容（词/释义/例句）保留。
    text = re.sub(r"<!--[\s\S]*?-->", "", text)
    text = re.sub(r'<a id="[^"]+"></a>\n?', "", text)
    text = re.sub(r"\[([^\]]+)\]\(#w-[^)]+\)", r"\1", text)
    meta, body = _split_frontmatter(text)

    title = meta.get("title") or note_dir.name
    doc = Document()
    _setup_document(doc)
    _footer_pageno(doc)

    # 封面区：标题 + 一行元信息，别做整页封面，打印浪费纸
    head = doc.add_paragraph()
    head.paragraph_format.space_after = Pt(2)
    run = head.add_run(_safe(title))
    run.bold = True
    run.font.size = Pt(20)
    _set_east_asia(run._element, "黑体")

    bits = [b for b in (
        meta.get("uploader"),
        f"时长 {meta['duration']}" if meta.get("duration") else "",
        meta.get("source", "").split("?")[0],
    ) if b]
    if bits:
        sub = doc.add_paragraph()
        sub.paragraph_format.space_after = Pt(2)
        r = sub.add_run(" · ".join(bits))
        r.font.size = Pt(9)
        r.font.color.rgb = RGBColor(0x77, 0x77, 0x77)
    stamp = doc.add_paragraph()
    stamp.paragraph_format.space_after = Pt(10)
    r = stamp.add_run(f"由 BiliNotes 于 {datetime.now():%Y-%m-%d %H:%M} 导出"
                      + (f" · 模型 {meta['model']}" if meta.get("model") else ""))
    r.font.size = Pt(9)
    r.font.color.rgb = RGBColor(0x99, 0x99, 0x99)

    first_heading = True
    first_quote = True
    seen_hashes: list = []
    for block in _iter_blocks(body, include_transcript):
        kind = block[0]
        if kind == "h":
            level, text_ = block[1], block[2]
            if not text_:
                continue
            # 文档里已经用大标题展示过视频标题，正文的一级标题不重复
            if level == 1 and (first_heading or text_.strip() == title):
                first_heading = False
                continue
            first_heading = False
            doc.add_heading(_safe(text_), level=min(level, 4))
        elif kind == "p":
            para = doc.add_paragraph()
            _add_inline(para, block[1])
        elif kind in ("ul", "ol"):
            depth, text_ = block[1], block[2]
            style = "List Number" if kind == "ol" else "List Bullet"
            try:
                para = doc.add_paragraph(style=style)
            except KeyError:
                para = doc.add_paragraph(("• " if kind == "ul" else "1. ") + text_)
            para.paragraph_format.left_indent = Cm(0.75 + 0.6 * depth)
            para.paragraph_format.space_after = Pt(2)
            _add_inline(para, text_)
        elif kind == "quote":
            joined = " ".join(block[1])
            # 开头的 "> UP主 … 字幕来源 …" 与封头重复，打印出来是重复信息
            if first_quote and ("字幕来源" in joined or "UP主" in joined):
                first_quote = False
                continue
            first_quote = False
            for line in block[1]:
                text_line = line.strip()
                if not text_line:
                    continue
                is_bullet = bool(re.match(r"^[-*+]\s+", text_line))
                if is_bullet:
                    text_line = re.sub(r"^[-*+]\s+", "• ", text_line)
                para = doc.add_paragraph()
                pf = para.paragraph_format
                pf.left_indent = Cm(0.6)
                pf.space_after = Pt(2)
                _add_inline(para, text_line, base_size=10)
                # 左侧竖线
                pPr = para._p.get_or_add_pPr()
                borders = OxmlElement("w:pBdr")
                left = OxmlElement("w:left")
                left.set(qn("w:val"), "single")
                left.set(qn("w:sz"), "18")
                left.set(qn("w:space"), "8")
                left.set(qn("w:color"), "C8C8C8")
                borders.append(left)
                pPr.append(borders)
        elif kind == "math":
            para = doc.add_paragraph()
            para.alignment = WD_ALIGN_PARAGRAPH.CENTER
            para.paragraph_format.space_before = Pt(6)
            para.paragraph_format.space_after = Pt(6)
            node = _omml(block[1])
            if node is None:
                run = para.add_run(block[1])
                run.font.name = "Consolas"
                run.font.size = Pt(9.5)
            else:
                para._p.append(node)
        elif kind == "img":
            rel, caption = block[1], block[2]
            path = (note_dir / rel).resolve()
            if not path.is_file():
                continue
            if _is_repeat_image(path, seen_hashes):   # 同一张幻灯片反复出现，打印时只留第一次
                continue
            try:
                para = doc.add_paragraph()
                para.alignment = WD_ALIGN_PARAGRAPH.CENTER
                para.paragraph_format.space_before = Pt(6)
                para.paragraph_format.space_after = Pt(2)
                para.add_run().add_picture(str(path), width=min(Cm(14.6), Inches(5.8)))
                if caption:
                    cap = doc.add_paragraph()
                    cap.alignment = WD_ALIGN_PARAGRAPH.CENTER
                    cap.paragraph_format.space_after = Pt(10)
                    r = cap.add_run(caption)
                    r.font.size = Pt(8.5)
                    r.font.color.rgb = RGBColor(0x88, 0x88, 0x88)
            except Exception:
                log.warning("图片写入失败：%s", path.name, exc_info=True)
        elif kind == "table":
            _write_table(doc, block[1])
        elif kind == "code":
            _write_code(doc, block[1], block[2])
        elif kind == "hr":
            para = doc.add_paragraph()
            pPr = para._p.get_or_add_pPr()
            borders = OxmlElement("w:pBdr")
            bottom = OxmlElement("w:bottom")
            bottom.set(qn("w:val"), "single")
            bottom.set(qn("w:sz"), "6")
            bottom.set(qn("w:space"), "1")
            bottom.set(qn("w:color"), "DDDDDD")
            borders.append(bottom)
            pPr.append(borders)

    _write_mindmap(doc, note_dir)

    out.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(out))
    return out
