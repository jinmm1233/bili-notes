"""音画锚定：把「哪句讲解讲的是哪一帧画面」显式算出来。

以前文本和画面各自带时间戳，却在切块时一起被拼成两坨没有时间的正文，
模型只能靠语义猜配图时间（容差 75 秒），于是「讲 A、配图 B」很常见。

这里用画面停留区间（Frame.seg_start/seg_end）当骨架，把每句 ASR 归到它
发生时画面正停着的那一帧上，产出 Anchor =「这一帧 + 这段时间博主说的话」。
后面总结、大纲、配图都吃这份成对材料，位置就有据可依了。
"""
from __future__ import annotations

import logging
import re
from typing import Iterable

from ..core.models import Anchor, Chunk, Frame, SceneSpan, Segment
from ..core.paths import fmt_ts

log = logging.getLogger(__name__)

# 一页 PPT 讲太久时按时间窗再切，避免一个 anchor 塞进上千字
_WINDOW = 45.0
_MAX_SPEECH_CHARS = 420
_MAX_SCREEN_CHARS = 320

_TS_HEAD = re.compile(
    r"^\s*[\[【(]?\s*(\d{1,2}:\d{2}(?::\d{2})?)\s*[\]】)]?\s*[-—:：]?\s*"
)


def parse_ts(text: str) -> float:
    """把 "00:06" / "1:02:33" 解析成秒；不是时间码就返回 0。"""
    parts = str(text or "").strip().split(":")
    if not 1 <= len(parts) <= 3:
        return 0.0
    try:
        nums = [int(p.strip()) for p in parts]
    except ValueError:
        return 0.0
    ts = 0.0
    for n in nums:
        ts = ts * 60 + n
    return max(0.0, ts)


def split_item_ts(text: str) -> tuple[float, str]:
    """拆掉模型写在要点开头的时间码，返回 (秒, 干净要点)。

    大纲模型会写成 "[00:06] happen to 意为碰巧"，时间码是它从材料里抄的
    真实讲解时刻。剥掉后要点仍是纯文本，旧的内容结构 / 导图逻辑都不受影响。
    """
    m = _TS_HEAD.match(str(text or ""))
    if not m:
        return 0.0, str(text or "").strip()
    ts = parse_ts(m.group(1))
    if ts <= 0:
        return 0.0, str(text or "").strip()
    return ts, str(text or "")[m.end():].strip()


def spans_from_frames(frames: Iterable[Frame]) -> list[SceneSpan]:
    """从候选帧上带的区间还原画面停留段（去重 + 按时间排序）。"""
    seen: set[tuple[float, float]] = set()
    spans: list[SceneSpan] = []
    for f in frames:
        if f.seg_end <= 0 or f.seg_end < f.seg_start:
            continue
        key = (round(f.seg_start, 2), round(f.seg_end, 2))
        if key in seen:
            continue
        seen.add(key)
        spans.append(SceneSpan(key[0], key[1], key[1]))
    spans.sort(key=lambda s: s.start)
    return spans


def ensure_spans(frames: list[Frame], spans: list[SceneSpan]) -> None:
    """给补抽的帧（讲解峰值帧等）补上它所属的画面停留区间。"""
    if not spans:
        return
    for f in frames:
        if f.seg_end > 0:
            continue
        best = min(
            spans,
            key=lambda s: 0.0 if s.start <= f.ts <= s.end else min(
                abs(f.ts - s.start), abs(f.ts - s.end)
            ),
        )
        f.seg_start, f.seg_end = best.start, best.end


def _frame_text(frame: Frame) -> str:
    """这一帧画面上的文字：优先用提炼过的画面要点，退回 OCR 原文。"""
    bits = [frame.screen_title, *(frame.screen_points or [])]
    text = "；".join(b for b in bits if b).strip()
    if not text:
        text = frame.caption.strip()
    if not text:
        text = (frame.ocr_text or "").strip()
    text = re.sub(r"\s*\n\s*", " / ", text)
    return text[:_MAX_SCREEN_CHARS]


def _owner_span(seg: Segment, spans: list[SceneSpan]) -> int:
    """一句讲解归到与它重叠时间最长的那段画面。"""
    best_i, best_overlap = -1, 0.0
    mid = (seg.start + seg.end) / 2
    for i, span in enumerate(spans):
        if span.end < seg.start - 0.5:
            continue
        if span.start > seg.end + 0.5:
            break
        overlap = min(seg.end, span.end) - max(seg.start, span.start)
        if overlap > best_overlap:
            best_i, best_overlap = i, overlap
    if best_i < 0:
        # 落在间隙里就归到时间上最近的一段
        best_i = min(
            range(len(spans)),
            key=lambda i: min(abs(spans[i].start - mid), abs(spans[i].end - mid)),
        )
    return best_i


def _owner_chunk(anchor: Anchor, chunks: list[Chunk]) -> int | None:
    for i, chunk in enumerate(chunks):
        if chunk.start <= anchor.start < chunk.end:
            return i
    if not chunks:
        return None
    return min(
        range(len(chunks)),
        key=lambda i: min(abs(chunks[i].start - anchor.start),
                          abs(chunks[i].end - anchor.start)),
    )


def build_anchors(
    chunks: list[Chunk],
    segments: list[Segment],
    frames: list[Frame],
    duration: float = 0.0,
    window: float = _WINDOW,
    spans: list[SceneSpan] | None = None,
) -> list[Anchor]:
    """给每个知识块挂上「画面 + 同期讲解」的音画对，返回全部 anchors。

    spans 是抽帧阶段算出的**全片**画面停留区间。必须传它：候选帧只覆盖
    部分区间，靠帧反推会漏掉大量段，讲解就被硬塞到别页的卡上
    （实测 TED 讲解里 02:41 的 well-dressed 被归到 02:30 的 happen to 卡）。
    抽帧失败 / 纯音频任务没有 spans 时，才退回从候选帧反推。
    """
    for chunk in chunks:
        chunk.anchors = []
    if not chunks:
        return []

    spans = list(spans or []) or spans_from_frames(frames)
    ensure_spans(frames, spans)
    by_span: dict[tuple[float, float], list[Frame]] = {}
    for f in frames:
        by_span.setdefault((round(f.seg_start, 2), round(f.seg_end, 2)), []).append(f)
    frame_groups = {
        (round(s.start, 2), round(s.end, 2)):
            by_span.get((round(s.start, 2), round(s.end, 2)), [])
        for s in spans
    }

    span_segs: dict[int, list[Segment]] = {}
    if spans:
        for seg in segments:
            span_segs.setdefault(_owner_span(seg, spans), []).append(seg)
    elif segments:
        # 没有画面区间（抽帧失败 / 纯音频任务）：按时间窗分组，frame_ts 留空
        span_segs[0] = list(segments)

    all_anchors: list[Anchor] = []
    ordered_spans = spans or [SceneSpan(0.0, duration or (segments[-1].end if segments else 0.0), 0.0)]

    for i, span in enumerate(ordered_spans):
        key = (round(span.start, 2), round(span.end, 2))
        group_frames = frame_groups.get(key, []) if spans else []
        segs = sorted(span_segs.get(i, []), key=lambda s: s.start)

        # 一页讲很久：按时间窗/字数拆成多个 anchor，每个仍指向同一段画面
        groups: list[list[Segment]] = []
        buf: list[Segment] = []
        buf_chars = 0
        for seg in segs:
            if buf and (seg.start - buf[0].start > window or buf_chars >= _MAX_SPEECH_CHARS):
                groups.append(buf)
                buf, buf_chars = [], 0
            buf.append(seg)
            buf_chars += len(seg.text)
        if buf:
            groups.append(buf)
        # 画面上有信息但这段时间没人说话，也要留一条（别漏掉画面知识点）
        if not groups and any(_frame_text(f) for f in group_frames):
            groups.append([])

        for grp in groups:
            if grp:
                start, end = grp[0].start, grp[-1].end
                speech = "".join(s.text for s in grp)[:_MAX_SPEECH_CHARS]
            else:
                start, end = span.start, span.end
                speech = ""
            # 优先认「稳定段末的完整帧」：同一页 PPT 讲的所有话都指向这一页，
            # 别为了贴近讲解中点去挑动画还没放全的中间态（均匀兜底帧）。
            # 这也是 must_keep 会优先保留的那张，两处必须一致。
            scene_frames = [f for f in group_frames if f.origin == "scene"]
            pool = scene_frames or group_frames
            mid = (start + end) / 2
            frame = min(pool, key=lambda f: abs(f.ts - mid)) if pool else None
            all_anchors.append(Anchor(
                start=start,
                end=max(end, start + 0.1),
                # 没有画面区间时（纯音频任务）用讲解中点，时间码仍然可信
                frame_ts=frame.ts if frame else (span.rep_ts if spans else mid),
                frame_path=frame.path if frame else "",
                speech=speech,
                screen=_frame_text(frame) if frame else "",
                span_start=span.start,
                span_end=span.end,
            ))

    for anchor in all_anchors:
        idx = _owner_chunk(anchor, chunks)
        if idx is not None:
            chunks[idx].anchors.append(anchor)
    for chunk in chunks:
        chunk.anchors.sort(key=lambda a: a.start)
        if not chunk.anchors:
            # 极端兜底：块内没有任何可锚定材料时，别让 prompt 空着
            chunk.anchors.append(Anchor(
                start=chunk.start, end=chunk.end,
                frame_ts=chunk.start, speech=chunk.text[:_MAX_SPEECH_CHARS],
                screen=(chunk.ocr_text or "")[:_MAX_SCREEN_CHARS],
            ))

    log.info("音画锚定：%d 个画面段 → %d 个音画对，分到 %d 个知识块",
             len(ordered_spans), len(all_anchors), len(chunks))
    return all_anchors


def bounds_from_spans(spans: list[SceneSpan]) -> list[float]:
    """画面切换边界（停留满 2 秒的段末），供分块优先在这些地方断开。"""
    return sorted({
        round(s.end, 2) for s in spans
        if s.end - s.start >= 2.0
    })


def scene_bounds(frames: list[Frame]) -> list[float]:
    """兼容：只有候选帧、没有全片区间时从这里反推边界。"""
    return bounds_from_spans(spans_from_frames(frames))


def must_keep_paths(chunks: list[Chunk]) -> set[str]:
    """被讲解指着的那些画面帧，打分时优先保留。"""
    return {a.frame_path for c in chunks for a in c.anchors if a.frame_path}


def refresh_screens(chunks: list[Chunk]) -> None:
    """画面要点提炼完之后，把锚点里的 OCR 原文换成干净的 PPT 标题/分点。

    build_anchors 跑在 OCR 之后、画面要点提炼之前，那时只有行列错乱的
    OCR 碎片。caption_frames 会把选中的帧填好 screen_title/screen_points，
    这里按时间点把同一帧的结构化文字回填，喂给总结模型的材料才分得清
    「屏幕上本来就有的例句」和「博主补的分析」。
    """
    frames = [f for c in chunks for f in c.frames]
    if not frames:
        return
    by_ts = sorted(frames, key=lambda f: f.ts)
    for chunk in chunks:
        for a in chunk.anchors:
            if a.frame_ts <= 0:
                continue
            frame = min(by_ts, key=lambda f: abs(f.ts - a.frame_ts))
            if abs(frame.ts - a.frame_ts) > 0.6:
                continue
            text = _frame_text(frame)
            if text:
                a.screen = text
            # 入选帧可能已被高清重取换过路径，锚点跟上，别留着临时低清图
            a.frame_path = frame.path


def anchor_material(chunk: Chunk, max_chars: int = 2600) -> str:
    """把块的锚点渲染成按时间顺序的成对材料。

    每行都带真实时间码，模型写要点时就能把这个时间码抄过去；
    【画面】和【讲解】分开，它才知道哪句是屏幕上本来就有的例句、
    哪句是博主暂停下来补的分析。

    一页 PPT 讲很久时块里可能有十几个锚点。不能直接 `[:max_chars]` 截尾，
    那样后面的知识点会整段消失（用户要求「有用的信息不要漏」）。
    这里先按锚点数平分预算、逐个压缩正文，保证每组的画面时间码都在。
    """
    items: list[tuple[str, str, str]] = []
    for a in chunk.anchors:
        head = f"[{fmt_ts(a.start)}–{fmt_ts(a.end)}｜画面 {fmt_ts(a.frame_ts)}]"
        screen = f"【画面】{a.screen}" if a.screen else ""
        if a.speech:
            speech = f"【讲解】{a.speech}"
        elif not a.screen:
            speech = "【讲解】（这段时间没有可识别的人声）"
        else:
            speech = ""
        items.append((head, screen, speech))

    if not items:
        return ""

    def render(limit: int) -> str:
        lines: list[str] = []
        for head, screen, speech in items:
            body = "\n".join(x for x in (screen, speech) if x)
            if len(body) <= limit:
                lines.append(f"{head}\n{body}")
                continue
            # 画面和口播各留一半：预算再紧也不能把博主的无字幕分析挤没
            half = max(10, (limit - 10) // 2)
            lines.append(f"{head}\n" + "\n".join(
                x[:half] for x in (screen, speech) if x
            ))
        return "\n\n".join(lines)

    full = render(10 ** 9)
    if len(full) <= max_chars:
        return full

    head_cost = sum(len(h) + 3 for h, _, _ in items)
    budget = max(24, (max_chars - head_cost) // len(items))
    material = render(budget)
    while len(material) > max_chars and budget > 12:
        budget = max(12, int(budget * 0.8))
        material = render(budget)
    if len(material) > max_chars:
        # 极端兜底：宁可只留每组的时间码，也不砍掉后面的知识点
        material = "\n\n".join(h for h, _, _ in items)[:max_chars]
    return material
