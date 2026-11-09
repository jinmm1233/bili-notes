"""OCR：把画面上的文字抠出来（PPT 标题、代码、公式、表格）。

选 RapidOCR 而非 PaddleOCR：Py3.13 没有 paddlepaddle 轮子，
且 RapidOCR 走 onnxruntime，打包体积和启动速度都更适合桌面分发。
"""
from __future__ import annotations

import logging
import re
import threading
from typing import Any

import cv2

from ..core.config import Settings
from ..core.models import Frame

log = logging.getLogger(__name__)

_engine: Any = None
# 并行流水线里可能有两个任务同时要识别，onnxruntime 会话本身可并发，
# 但 RapidOCR 实例内部有状态，串行调用更稳妥（OCR 本就是 CPU 密集型）
_engine_lock = threading.Lock()
_SYMBOL_CHARS = set("=+−-×÷∑∫√≈≠≤≥±πΔαβθλμσ")
_CODE_HINT = re.compile(r"[{}();=<>]|def |import |function |class ")


def get_engine() -> Any:
    global _engine
    if _engine is None:
        from rapidocr_onnxruntime import RapidOCR  # noqa: PLC0415

        _engine = RapidOCR()
        log.info("RapidOCR 引擎已加载")
    return _engine


def ocr_image(
    path: str,
    min_confidence: float = 0.6,
    max_edge: int | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    """返回 (按阅读顺序拼接的文本, 结构化行列表)。

    max_edge 用于"候选帧粗筛"：这类帧只参与打分，不需要按原分辨率识别，
    缩到 640 宽能省掉近一半时间，而且不影响最终出图。
    """
    source: Any = path
    if max_edge:
        try:
            img = cv2.imread(path)
            if img is not None:
                h, w = img.shape[:2]
                if max(h, w) > max_edge:
                    scale = max_edge / max(h, w)
                    source = cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
        except Exception:
            source = path

    try:
        engine = get_engine()
        with _engine_lock:
            result, _elapse = engine(source)
    except Exception as exc:
        log.warning("OCR 失败 %s: %s", path, exc)
        return "", []

    if not result:
        return "", []

    items: list[dict[str, Any]] = []
    for row in result:
        try:
            box, text, score = row[0], str(row[1]), float(row[2])
        except (IndexError, TypeError, ValueError):
            continue
        text = text.strip()
        if not text or score < min_confidence:
            continue
        ys = [pt[1] for pt in box]
        xs = [pt[0] for pt in box]
        items.append({
            "text": text,
            "score": round(score, 3),
            "x": min(xs),
            "x2": max(xs),
            "y": sum(ys) / len(ys),
            "h": max(ys) - min(ys),
        })

    if not items:
        return "", []

    lines = _group_lines(items)
    text = render_lines(lines)
    return text, lines


def _group_lines(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按 y 坐标聚类成行，行内按 x 排序 —— OCR 原始输出顺序常是乱的。"""
    items.sort(key=lambda i: i["y"])
    heights = sorted(i["h"] for i in items)
    median_h = heights[len(heights) // 2] if heights else 12.0
    tolerance = max(6.0, median_h * 0.6)

    lines: list[dict[str, Any]] = []
    for item in items:
        if lines and abs(item["y"] - lines[-1]["_y"]) <= tolerance:
            lines[-1]["parts"].append(item)
            lines[-1]["_y"] = (lines[-1]["_y"] + item["y"]) / 2
        else:
            lines.append({"_y": item["y"], "parts": [item]})

    out: list[dict[str, Any]] = []
    for line in lines:
        parts = sorted(line["parts"], key=lambda i: i["x"])
        text = " ".join(p["text"] for p in parts).strip()
        if not text:
            continue
        out.append({
            "text": text,
            "y": round(line["_y"], 1),
            "kind": _classify(text),
            "score": round(sum(p["score"] for p in parts) / len(parts), 3),
            "h": round(sorted(p["h"] for p in parts)[len(parts) // 2], 1),
            "cells": [
                {"text": p["text"], "x": round(p["x"], 1), "x2": round(p.get("x2", p["x"]), 1)}
                for p in parts
            ],
        })
    return out


def _split_cells(cells: list[dict[str, Any]], gap: float) -> list[list[dict[str, Any]]]:
    """把一行里的 OCR 片段按水平间距切成单元格。

    同一句话里 OCR 片段几乎贴在一起（间隙几个像素），
    表格列之间通常空出一个字以上 —— 用这个差判定列边界。
    """
    if not cells:
        return []
    groups: list[list[dict[str, Any]]] = [[cells[0]]]
    for prev, cur in zip(cells, cells[1:]):
        if cur["x"] - prev["x2"] > gap:
            groups.append([cur])
        else:
            groups[-1].append(cur)
    return groups


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[len(ordered) // 2]


def build_table(lines: list[dict[str, Any]]) -> str | None:
    """把一段 OCR 行还原成 Markdown 表格；不像表格就返回 None。

    没有引入 rapid-table 这类表格识别模型，是因为它们要额外下几十 MB 权重、
    还要联网。PPT 里的表格大多规整，用已有的 OCR 坐标做几何聚类就够了，
    而且离线、零依赖、可解释。

    判定标准（刻意从严，宁可漏也不误伤正文）：
    - 至少 3 行，且多数行的单元格数一致（取众数作为列数）
    - 相邻列之间的水平间距普遍大于阈值，否则只是普通排版
    """
    if len(lines) < 3:
        return None

    heights = [ln.get("h", 0.0) for ln in lines if ln.get("h")]
    med_h = _median(heights) or 12.0
    gap = max(8.0, med_h * 0.9)

    rows: list[list[list[dict[str, Any]]]] = []
    for ln in lines:
        cells = ln.get("cells") or []
        rows.append(_split_cells(cells, gap) if len(cells) > 1 else ([cells] if cells else []))

    counts: dict[int, int] = {}
    for r in rows:
        if len(r) >= 2:
            counts[len(r)] = counts.get(len(r), 0) + 1
    if not counts:
        return None
    ncol = max(counts.items(), key=lambda kv: (kv[1], kv[0]))[0]
    if not 2 <= ncol <= 8:
        return None
    if counts[ncol] < 3:                       # 至少三行是这个列数
        return None

    body = [r for r in rows if len(r) == ncol]
    if len(body) < 3 or len(body) < len(rows) * 0.6:
        return None

    # 列间距校验：列之间必须真的"分开"，否则多半是普通的两栏文字
    for j in range(ncol - 1):
        gaps = [r[j + 1][0]["x"] - r[j][-1]["x2"] for r in body]
        if _median(gaps) < gap * 0.8:
            return None

    header = body[0]
    rest = body[1:]
    if not rest:
        return None

    def cell_text(cells: list[dict[str, Any]]) -> str:
        return " ".join(c["text"] for c in cells).strip().replace("|", "／").replace("\n", " ")

    head = "| " + " | ".join(cell_text(c) for c in header) + " |"
    sep = "|" + "|".join([" --- "] * ncol) + "|"
    out = [head, sep]
    for r in rest:
        out.append("| " + " | ".join(cell_text(c) for c in r) + " |")
    return "\n".join(out)


def render_lines(lines: list[dict[str, Any]]) -> str:
    """把 OCR 行拼成给笔记用的文本：表格段落转成 Markdown 表格。

    只在"连续多行都像表格行"的区间上尝试还原，
    这样标题在上、脚注在下的画面也不会被整块吞掉。
    """
    if not lines:
        return ""

    # 找最长的连续"疑似表格行"区间（该行被切成 >=2 个单元格）
    gap_probe = max(8.0, (_median([ln.get("h", 0.0) for ln in lines if ln.get("h")]) or 12.0) * 0.9)
    marks = []
    for ln in lines:
        cells = ln.get("cells") or []
        marks.append(len(_split_cells(cells, gap_probe)) >= 2 if len(cells) > 1 else False)

    best = (0, 0)   # (长度, 起始)
    i = 0
    while i < len(marks):
        if not marks[i]:
            i += 1
            continue
        j = i
        while j < len(marks) and marks[j]:
            j += 1
        if j - i > best[0]:
            best = (j - i, i)
        i = j

    length, start = best
    md = build_table(lines[start: start + length]) if length >= 3 else None

    out: list[str] = []
    idx = 0
    while idx < len(lines):
        if md and idx == start:
            out.append(md)
            idx = start + length
            continue
        out.append(lines[idx]["text"])
        idx += 1
    return "\n".join(out)


def _classify(text: str) -> str:
    symbols = sum(1 for ch in text if ch in _SYMBOL_CHARS)
    if symbols >= 2 and len(text) < 80:
        return "formula"
    if _CODE_HINT.search(text):
        return "code"
    if text.count("|") >= 2 or text.count("\t") >= 2:
        return "table"
    return "text"


def ocr_frames(
    frames: list[Frame],
    settings: Settings,
    on_progress=None,
    max_edge: int | None = None,
) -> list[Frame]:
    total = len(frames)
    for i, frame in enumerate(frames, 1):
        text, lines = ocr_image(frame.path, settings.ocr_min_confidence, max_edge=max_edge)
        frame.ocr_text = _clean(text)
        frame.ocr_blocks = lines
        frame.text_density = min(1.0, len(frame.ocr_text) / 120.0)
        if on_progress and total:
            on_progress(i / total * 100.0, f"识别画面文字 {i}/{total}")
    return frames


def _clean(text: str) -> str:
    """去重与过滤水印。表格块整块放行——表格里出现重复行是正常的，
    一旦去重就会把表体删掉几行，结构直接错位。"""
    lines: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("|"):
            lines.append(line)
            continue
        if len(line) <= 1:
            continue
        if any(kw in line for kw in ("哔哩哔哩", "bilibili", "未经作者授权")):
            continue
        if line not in lines:
            lines.append(line)
    return "\n".join(lines)


def to_markdown_blocks(blocks: list[dict[str, Any]]) -> str:
    """把 OCR 行还原成 md：代码块 / 公式 / 表格各用合适语法，避免糊成一坨。"""
    if not blocks:
        return ""
    chunks: list[str] = []
    buffer: list[str] = []
    kind = "text"

    def flush() -> None:
        nonlocal buffer, kind
        if not buffer:
            return
        joined = "\n".join(buffer)
        if kind == "code":
            chunks.append(f"```\n{joined}\n```")
        elif kind == "formula":
            chunks.append("\n".join(f"$$ {ln} $$" for ln in buffer))
        else:
            chunks.append(joined)
        buffer = []

    for block in blocks:
        if block["kind"] != kind:
            flush()
            kind = block["kind"]
        buffer.append(block["text"])
    flush()
    return "\n\n".join(chunks)
