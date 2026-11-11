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
from typing import Any, Iterable

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
    return set(must_keep_counts(chunks))


def must_keep_counts(chunks: list[Chunk]) -> dict[str, int]:
    """每张「被讲解指着」的帧被指向了几次——讲得最多的那张最有代表性。"""
    counts: dict[str, int] = {}
    for c in chunks:
        for a in c.anchors:
            if a.frame_path:
                counts[a.frame_path] = counts.get(a.frame_path, 0) + 1
    return counts


def reconcile_anchors(chunks: list[Chunk], selected: list[Frame]) -> int:
    """选帧之后，把锚点改指到「最终入选的那一张」，返回改了多少个。

    同一页 PPT 的几帧感知哈希几乎一样，选帧去重时只会留一张——而锚点还指着
    被去重掉的那张。配图按 ±8 秒找就找不到（实测差到 38 秒），要点只好去蹭
    上一条的图，视觉上就成了张冠李戴。

    锚定的语义本来就是「哪一页」而不是「哪一秒」：同一页里讲的每一句，
    对应的画面内容是同一张。这里把锚点挪到同页入选的那张上，配图就一定能
    找到，而且找到的确实是它讲的那一页。
    """
    by_span: dict[tuple[float, float], list[Frame]] = {}
    for f in selected:
        if f.seg_end > f.seg_start:
            by_span.setdefault((round(f.seg_start, 2), round(f.seg_end, 2)), []).append(f)

    moved = 0
    for chunk in chunks:
        for a in chunk.anchors:
            if a.frame_ts <= 0:
                continue
            pool = by_span.get((round(a.span_start, 2), round(a.span_end, 2)), [])
            if not pool:
                continue
            best = min(pool, key=lambda f: abs(f.ts - a.frame_ts))
            if abs(best.ts - a.frame_ts) > 0.01:
                a.frame_ts = best.ts
                a.frame_path = best.path
                moved += 1
    if moved:
        log.info("锚点对齐到入选帧：%d 处", moved)
    return moved


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


def _pair_anchors(anchors: list[Anchor]) -> list[dict[str, Any]]:
    """把「只有画面」的锚点并进它时间范围内的「有讲解」锚点。

    课堂 / 幻灯片类视频抽不出稳定画面段（每一帧都是 0.1 秒的孤立点），
    而口播是长区间（00:00–00:27）。两边各挂各的，模型拿到的材料就变成
    「一堆只有时间码的空行 + 几段孤立的讲解」，于是如实写下
    「材料未提及具体讲解内容，仅标注了画面时间点 00:06」
    （实测 8 分钟 P/NP 那条笔记就是这个毛病）。

    合并后每条材料都是「这段时间讲了 X，画面上是 Y」，模型才有东西可写。
    """
    ordered = sorted(anchors, key=lambda a: (a.start, a.frame_ts))
    speech_ones = [a for a in ordered if a.speech]
    screen_ones = [a for a in ordered if not a.speech and a.screen]
    used: set[int] = set()

    items: list[dict[str, Any]] = []
    for a in speech_ones:
        # 这段讲解期间出现过的画面文字（同一页反复出现时只留首尾两张）
        picked: list[Anchor] = []
        for s in screen_ones:
            if id(s) in used:
                continue
            t = s.frame_ts if s.frame_ts > 0 else s.start
            if a.start - 2.0 <= t <= a.end + 2.0:
                picked.append(s)
        screens: list[str] = []
        for s in picked:
            used.add(id(s))
            text = s.screen.strip()
            if text and text not in screens:
                screens.append(text)
        # 同一页讲了 10 分钟，开头和结尾各留一份，模型能看出画面推进
        screen_text = "\n".join(screens[:2])
        items.append({
            "head": f"[{fmt_ts(a.start)}–{fmt_ts(a.end)}｜画面 {fmt_ts(a.frame_ts)}]",
            "screen": f"【画面】{screen_text}" if screen_text else "",
            "speech": f"【讲解】{a.speech}",
        })

    # 没被吸收的「只有画面」锚点：画面本身是知识点，单独留着
    last_text = ""
    for a in ordered:
        if id(a) in used or not a.screen:
            continue
        text = a.screen.strip()
        if text and text == last_text:
            continue          # 同一屏连续出现，合并成一条
        last_text = text
        items.append({
            "head": f"[{fmt_ts(a.start)}–{fmt_ts(a.end)}｜画面 {fmt_ts(a.frame_ts)}]",
            "screen": f"【画面】{text}",
            "speech": "",
        })

    items.sort(key=lambda it: it["head"])
    return items


def _render_items(items: list[dict[str, Any]], max_chars: int) -> str:
    """按内容需求分配预算：讲解优先，画面其次，每条都有保底。

    以前是「每条平分预算」，锚点一多（课堂视频能有 80 多个）就平分到
    每人 20 字，正文被砍光；再不行就走兜底只留时间码 —— 模型收到的是一片
    空白，只能如实写「材料未提及」。

    现在的规则：
    1. 每条先给保底（画面 8 字、讲解 8 字），保证时间码和标签都还在；
    2. 余量按各条的内容长度按比例分；
    3. 一条之内讲解占大头（画面最多 40%），口播是主体；
    4. 总字数严格不超 max_chars。
    """
    items = [it for it in items if it["screen"] or it["speech"]]
    if not items:
        return ""
    n = len(items)

    def head_cost(it: dict[str, Any]) -> int:
        return len(it["head"]) + 3      # 标题 + 换行 + 分隔

    room = max_chars - sum(head_cost(it) for it in items)
    if room <= 0:
        # 连标题都放不下：只保留内容最实的几条
        keep = sorted(range(n),
                      key=lambda i: -(len(items[i]["speech"]) + len(items[i]["screen"])))[:8]
        alloc = {i: (10 ** 6, 10 ** 6) for i in keep}
        return _render_with(items, alloc)

    # 1) 保底
    base_s: dict[int, int] = {}
    base_p: dict[int, int] = {}
    for i, it in enumerate(items):
        base_s[i] = 8 if it["screen"] else 0
        base_p[i] = 8 if it["speech"] else 0
    base_total = sum(base_s.values()) + sum(base_p.values())
    if base_total > room:
        scale = room / base_total
        base_s = {i: max(1, int(v * scale)) for i, v in base_s.items() if v}
        base_p = {i: max(1, int(v * scale)) for i, v in base_p.items() if v}
        base_total = sum(base_s.values()) + sum(base_p.values())

    # 2) 余量按需求长度按比例分
    need = {i: max(0, len(items[i]["screen"]) - base_s.get(i, 0))
               + max(0, len(items[i]["speech"]) - base_p.get(i, 0))
            for i in range(n)}
    total_need = sum(need.values())
    extra = max(0, room - base_total)
    if extra > 0 and total_need > 0:
        give = min(extra, total_need)
        add = {i: give * need[i] // total_need for i in range(n)}
    else:
        add = {i: 0 for i in range(n)}

    # 3) 拆给画面/讲解：讲解占大头，画面最多 40%
    alloc: dict[int, tuple[int, int]] = {}
    for i, it in enumerate(items):
        total = base_s.get(i, 0) + base_p.get(i, 0) + add.get(i, 0)
        sc, sp = it["screen"], it["speech"]
        if sc and sp:
            sc_b = min(len(sc), int(total * 0.4))
            sp_b = min(len(sp), total - sc_b)
            if sp_b < 8:
                sp_b = min(len(sp), 8)
                sc_b = min(len(sc), max(0, total - sp_b))
        elif sc:
            sc_b, sp_b = min(len(sc), total), 0
        else:
            sc_b, sp_b = 0, min(len(sp), total)
        alloc[i] = (sc_b, sp_b)

    material = _render_with(items, alloc)
    # 4) 仍超预算：按比例削（画面先削），但每条至少留标题和 8 字正文
    for _ in range(10):
        over = len(material) - max_chars
        if over <= 0:
            break
        victims = [i for i, (sc, sp) in alloc.items() if sc > 8]
        if not victims:
            break
        for i in victims:
            cut = min(alloc[i][0] - 8, max(1, over // len(victims)))
            alloc[i] = (alloc[i][0] - cut, alloc[i][1])
            over -= cut
        material = _render_with(items, alloc)
        if len(material) <= max_chars:
            return material
    for _ in range(10):
        over = len(material) - max_chars
        if over <= 0:
            break
        victims = [i for i, (sc, sp) in alloc.items() if sp > 8]
        if not victims:
            break
        for i in victims:
            cut = min(alloc[i][1] - 8, max(1, over // len(victims)))
            alloc[i] = (alloc[i][0], alloc[i][1] - cut)
            over -= cut
        material = _render_with(items, alloc)
        if len(material) <= max_chars:
            return material
    return material[:max_chars]


def _render_with(items: list[dict[str, Any]], alloc: dict[int, tuple[int, int]]) -> str:
    lines: list[str] = []
    for i, it in enumerate(items):
        scr_bud, sp_bud = alloc.get(i, (0, 0))
        screen = it["screen"][:scr_bud] if scr_bud else ""
        speech = it["speech"][:sp_bud] if sp_bud else ""
        body = "\n".join(x for x in (screen, speech) if x)
        if not body:
            continue
        lines.append(f"{it['head']}\n{body}")
    return "\n\n".join(lines)


def anchor_material(chunk: Chunk, max_chars: int = 2600) -> str:
    """把块的锚点渲染成按时间顺序的成对材料。

    每行都带真实时间码，模型写要点时就能把这个时间码抄过去；
    【画面】和【讲解】分开，它才知道哪句是屏幕上本来就有的例句、
    哪句是博主暂停下来补的分析。

    一页 PPT 讲很久时块里可能有十几个锚点。不能直接 `[:max_chars]` 截尾，
    那样后面的知识点会整段消失（用户要求「有用的信息不要漏」）。
    这里先配对（画面 + 同期讲解）、再按内容长度分预算，讲解优先。
    """
    items = _pair_anchors(chunk.anchors)
    if not items:
        return ""
    return _render_items(items, max_chars)
