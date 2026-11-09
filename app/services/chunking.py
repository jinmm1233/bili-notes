"""分块：把连续字幕切成语义完整、长度可控的知识块，并把画面帧挂到对应块上。

不用固定字数硬切——那会把一个概念劈成两半，两半的摘要都不准确。
切点靠打分：句末标点、长停顿、转折词都是强信号。
"""
from __future__ import annotations

import bisect
import logging
import re

from ..core.models import Chunk, Frame, Segment

log = logging.getLogger(__name__)

_SENT_END = "。！？!?…"
_CUE_WORDS = (
    "接下来", "下面", "然后我们", "第二个", "第三", "首先", "其次", "最后",
    "另一方面", "总结一下", "小结", "我们来看", "举个例子", "所以", "因此",
    "好了", "那么",
)


def _cut_score(seg: Segment, gap: float, current_len: int, target: int) -> float:
    score = 0.0
    text = seg.text.strip()
    if text and text[-1] in _SENT_END:
        score += 2.0
    if gap >= 1.2:
        score += 2.0
    head = text[:10]
    if any(w in head for w in _CUE_WORDS):
        score += 3.0
    if current_len >= target:
        score += 2.0
    return score


def build_chunks(
    segments: list[Segment],
    target: int = 800,
    max_chars: int = 1200,
    min_chars: int = 120,
    scene_bounds: list[float] | None = None,
) -> list[Chunk]:
    """按语义切块；接近目标长度时优先在画面切换处断开。

    scene_bounds 是画面停留段的结束时间。只把切点往这些边界上靠，
    不改变三档共用的判定标准，也不额外增加块数（门槛是 target 的八成）。
    """
    if not segments:
        return []

    bounds = sorted(scene_bounds or [])

    def crosses_scene_bound(last_end: float, seg: Segment) -> bool:
        """上一句到这一句之间是否换过画面。

        ASR 一句常只有两三秒，很少「恰好」跨在切换那一秒上；所以看的是
        两句之间的间隙里有没有边界，而不是单句自身跨没跨。
        """
        if not bounds:
            return False
        i = bisect.bisect_left(bounds, last_end)
        return i < len(bounds) and bounds[i] <= seg.end

    chunks: list[Chunk] = []
    buf: list[Segment] = []
    buf_len = 0
    prev_end = segments[0].start

    def flush() -> None:
        nonlocal buf, buf_len
        if not buf:
            return
        text = "".join(s.text for s in buf).strip()
        if not text:
            buf, buf_len = [], 0
            return
        chunks.append(
            Chunk(
                index=len(chunks),
                start=buf[0].start,
                end=buf[-1].end,
                text=text,
            )
        )
        buf, buf_len = [], 0

    for seg in segments:
        last_end = prev_end
        gap = seg.start - last_end
        buf.append(seg)
        buf_len += len(seg.text)
        prev_end = seg.end

        if buf_len >= max_chars:
            flush()
            continue
        if buf_len >= target and _cut_score(seg, gap, buf_len, target) >= 4.0:
            flush()
            continue
        if buf_len >= target * 0.8 and crosses_scene_bound(last_end, seg):
            flush()

    flush()

    # 尾巴过短就并进上一块，避免出现只有一句话的碎块
    if len(chunks) >= 2 and chunks[-1].char_count < min_chars:
        tail = chunks.pop()
        last = chunks[-1]
        last.text = f"{last.text}{tail.text}"
        last.end = tail.end

    for i, chunk in enumerate(chunks):
        chunk.index = i
    return chunks


def chunks_from_frames(
    frames: list[Frame],
    duration: float,
    target_chars: int = 800,
    window: float = 90.0,
) -> list[Chunk]:
    """没有语音时，用画面当骨架切块。

    视频没有音轨、或者音轨里只有配乐没有人声时，本地转写会返回 0 句。
    这时如果直接往下走，知识块为空 → `attach_frames` 整批丢弃 →
    截好的画面一张都挂不上去，笔记只剩下标题（实测就是 896 字节的空壳）。

    这里按时间窗把画面分组，用画面上的文字当块正文，让后面
    「打分 → 挂帧 → 生成画面要点 → 总结 → 导出」照常走完。
    块文本允许为空：纯图像内容靠视觉模型的画面要点补。
    """
    if not frames:
        return []

    ordered = sorted(frames, key=lambda f: f.ts)
    groups: list[list[Frame]] = []
    buf: list[Frame] = []
    buf_chars = 0

    for frame in ordered:
        buf.append(frame)
        buf_chars += len(frame.ocr_text or "")
        spent = frame.ts - buf[0].ts
        if spent >= window or buf_chars >= target_chars:
            groups.append(buf)
            buf, buf_chars = [], 0
    if buf:
        groups.append(buf)

    chunks: list[Chunk] = []
    for i, group in enumerate(groups):
        texts: list[str] = []
        for frame in group:
            text = (frame.ocr_text or "").strip()
            if text and text not in texts:
                texts.append(text)
        body = "\n".join(texts)
        start = group[0].ts
        # 终点取下一组起点：保证 _owner_index 的 [start, end) 不漏帧
        end = groups[i + 1][0].ts if i + 1 < len(groups) else max(duration, start + 1.0)
        chunks.append(
            Chunk(
                index=i,
                start=start,
                end=max(end, start + 0.1),
                text=body,
                ocr_text=body,
            )
        )
    return chunks


def attach_frames(
    chunks: list[Chunk],
    frames: list[Frame],
    per_chunk: int = 3,
    total_cap: int = 40,
) -> list[Chunk]:
    """按时间归属挂载关键帧；超出配额的按分数淘汰。"""
    if not chunks or not frames:
        return chunks

    ranked = sorted(frames, key=lambda f: f.score, reverse=True)
    if total_cap:                      # 0 = 不限（详细档）
        ranked = ranked[:total_cap]
    kept = set(id(f) for f in ranked)

    for frame in frames:
        if id(frame) not in kept:
            continue
        idx = _owner_index(chunks, frame.ts)
        if idx is not None:
            chunks[idx].frames.append(frame)

    for chunk in chunks:
        if per_chunk and len(chunk.frames) > per_chunk:   # 0 = 不限（详细档）
            chunk.frames.sort(key=lambda f: f.score, reverse=True)
            chunk.frames = chunk.frames[:per_chunk]
        chunk.frames.sort(key=lambda f: f.ts)
        if chunk.frames:
            chunk.ocr_text = "\n".join(dict.fromkeys(f.ocr_text for f in chunk.frames if f.ocr_text))
    return chunks


def _owner_index(chunks: list[Chunk], ts: float) -> int | None:
    for i, chunk in enumerate(chunks):
        if chunk.start <= ts < chunk.end:
            return i
    # 落在间隙里就归到最近的一块
    if not chunks:
        return None
    return min(range(len(chunks)), key=lambda i: min(abs(chunks[i].start - ts), abs(chunks[i].end - ts)))


def coverage_ratio(chunks: list[Chunk], duration: float) -> float:
    if duration <= 0:
        return 0.0
    covered = sum(max(0.0, c.end - c.start) for c in chunks)
    return min(1.0, covered / duration)


def bigrams(text: str) -> set[str]:
    clean = re.sub(r"[\s\W_]+", "", text.lower())
    return {clean[i: i + 2] for i in range(len(clean) - 1)}
