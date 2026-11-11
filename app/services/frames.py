"""关键帧选择：候选生成 → 四维打分 → 去重择优。

打分刻意偏重"有没有信息"而非"画面好不好看"：
一段纯人脸的讲解帧对笔记毫无价值，一张满是公式的 PPT 才是要的。
"""
from __future__ import annotations

import logging
import math
from pathlib import Path
from typing import Any

import cv2
import imagehash
import numpy as np
from PIL import Image

from ..core.config import Settings
from ..core.ffmpeg import extract_frames_at, extract_frames_from_url, extract_thumbnails
from ..core.models import Chunk, Frame, SceneSpan, Segment
from .chunking import bigrams

log = logging.getLogger(__name__)

# 感知哈希距离小于这个值算「同一张画面」，只留一张。
# 8 太宽了：相邻两页 PPT 只改几个字就会被判成同一张，
# 于是后一页配不到图，讲它的要点只能去蹭前一张（实测 12 处）。
# 4 只把几乎完全相同的画面算重复，模板相近的两页各留一张。
_DUP_DISTANCE = 4

# 「同一页」的第二判据：时间邻近 + 略宽阈值。
# 整帧 pHash 对「站着不动的幻灯片」很好用，但对**真人出镜的板书课**失效：
# 老师一走一动，同一块黑板的前后两帧就能差 6-8 位，宽松不到 4 位阈值，
# 于是笔记里出现三张一模一样的板书（实测 00:06 / 00:12 / 00:24 同一块黑板）。
# 所以再加一条：90 秒内、差 12 位以内，也算同一页，只留一张。
# 12 位仍低于「翻页」的典型差异（相邻两页 PPT 实测 12 位以上），不会误并不同页。
_DUP_NEAR = 12
_DUP_WINDOW = 90.0

_CUE_PATTERNS = ("看这里", "这个图", "如图所示", "这张图", "看这个", "公式", "代码",
                 "这个表", "示意图", "流程图", "架构图", "我们看", "请注意")

# 稳定画面段上限。档位越高允许保留越多：
# 详细档放宽到 400 段，避免「一节课几百次翻页」被均匀截断丢掉后面的页。
_CUT_CAP = {"brief": 160, "standard": 240, "detailed": 400}

# 判定「画面稳定下来」所需的连续静止秒数（缩略图 1fps）。
# **三档一致**：详细不等于「一变就截」，它只是「每段多留几张」。
# 定 2 秒而不是 3 秒：板书写完往往只停两秒就擦掉/翻页，要求 3 秒会把整页漏掉。
# 放宽到 2 秒后，PPT 逐条动画的中间态会进候选，但打分阶段「字多的分高」，
# 同一页的相似帧去重时最终会留下内容最全的那一张——既不漏，也不选半成品。
_STABLE_MIN = 2

# 一页 PPT 讲很久时，只在末尾截一张会漏掉后面补的板书/高亮。
# 稳定段超过 _LONG_SEGMENT 秒，就每 _LONG_STEP 秒往回补一个点。
# 同样三档一致——判定标准不因档位改变。
_LONG_SEGMENT = 90.0
_LONG_STEP = 30.0


def detect_cuts(
    thumbs: list[tuple[float, Path]],
    distance: int = 14,
    min_gap: float = 2.0,
) -> list[float]:
    """用感知哈希帧差找场景切换点。

    判据是"整幅画面结构变了"而非"亮度变了"——所以白底 PPT 翻页、代码滚动、
    画面切换都能命中，而镜头轻微晃动不会。
    """
    if len(thumbs) < 2:
        return []

    hashes: list[Any] = []
    for _ts, path in thumbs:
        try:
            with Image.open(path) as img:
                hashes.append(imagehash.phash(img.convert("RGB"), hash_size=8))
        except Exception:
            hashes.append(None)

    cuts: list[float] = []
    last_kept = -1e9
    for i in range(1, len(hashes)):
        prev, cur = hashes[i - 1], hashes[i]
        if prev is None or cur is None:
            continue
        ts = thumbs[i][0]
        if (prev - cur) >= distance and (ts - last_kept) >= min_gap:
            cuts.append(ts)
            last_kept = ts
    return cuts


def detect_scene_spans(
    thumbs: list[tuple[float, Path]],
    distance: int = 14,
    min_stable: int = _STABLE_MIN,
) -> list[SceneSpan]:
    """把时间轴切成「画面没变」的连续区间，覆盖全片。

    以前只保留每段的末帧时间点，区间本身被丢掉了。可"博主暂停下来讲这一帧"
    恰恰要靠区间才能判断：这句讲解落在这段画面停留期间，讲的就是这一帧。
    所以这里连不稳定的短段也一并输出（stable=False），保证每句话都有归属。
    """
    if len(thumbs) < 2:
        return []

    hashes: list[Any] = []
    for _ts, path in thumbs:
        try:
            with Image.open(path) as img:
                hashes.append(imagehash.phash(img.convert("RGB"), hash_size=8))
        except Exception:
            hashes.append(None)

    spans: list[SceneSpan] = []
    n = len(hashes)
    i = 0
    while i < n:
        j = i
        while (j + 1 < n and hashes[j] is not None and hashes[j + 1] is not None
               and (hashes[j] - hashes[j + 1]) < distance):
            j += 1
        run = j - i + 1
        start_ts, end_ts = thumbs[i][0], thumbs[j][0]
        stable = run >= min_stable
        # 稳定段取末帧（内容放全的那一刻）；一直在动的段取中点
        rep = end_ts if stable else (start_ts + end_ts) / 2
        spans.append(SceneSpan(start_ts, end_ts, rep, stable))
        i = j + 1

    return spans


def stable_points(
    spans: list[SceneSpan],
    long_segment: float = _LONG_SEGMENT,
    long_step: float = _LONG_STEP,
) -> list[float]:
    """稳定段的取帧时间点：段末 + 长段往回补点（沿用原来的判定）。"""
    points: list[float] = []
    for span in spans:
        if not span.stable:
            continue
        points.append(span.end)                # 段末 = 内容最全的一帧
        if long_step > 0 and (span.end - span.start) > long_segment:
            t = span.start + long_step
            while t < span.end - 1.0:
                points.append(t)
                t += long_step
    return sorted(set(points))


def attach_span(frame: Frame, spans: list[SceneSpan]) -> None:
    """给一张帧标上它所属的画面停留区间（后面音画锚定要用）。"""
    if not spans:
        return
    best: SceneSpan | None = None
    best_gap = float("inf")
    for span in spans:
        gap = 0.0 if span.start <= frame.ts <= span.end else min(
            abs(frame.ts - span.start), abs(frame.ts - span.end)
        )
        if gap < best_gap:
            best, best_gap = span, gap
    if best is not None:
        frame.seg_start, frame.seg_end = best.start, best.end


def detect_stable(
    thumbs: list[tuple[float, Path]],
    distance: int = 12,
    min_stable: int = 3,
    long_segment: float = _LONG_SEGMENT,
    long_step: float = _LONG_STEP,
) -> list[float]:
    """兼容旧接口：找「画面稳定段」，返回每段的**最后一帧**时间点。"""
    return stable_points(
        detect_scene_spans(thumbs, distance=distance, min_stable=min_stable),
        long_segment=long_segment, long_step=long_step,
    )


def generate_candidates(
    settings: Settings,
    video_path: Path,
    duration: float,
    workdir: Path,
    segments: list[Segment] | None = None,
    spans_out: list[SceneSpan] | None = None,
) -> list[Frame]:
    """三路候选：稳定画面段 / 讲解峰值 / 均匀兜底。

    spans_out 用来把**全片**画面停留区间交给调用方：音画锚定需要它判断
    「这句讲解发生在哪一页」，而候选帧只覆盖部分区间，反推会漏段。
    """
    frames: list[Frame] = []
    spans: list[SceneSpan] = []

    # 1. 稳定画面段：等画面停下来再截，跳过 PPT 动画的中间态
    try:
        thumbs = extract_thumbnails(str(video_path), str(workdir / "thumbs"))
        spans = detect_scene_spans(
            thumbs,
            distance=settings.scene_phash_distance,
            min_stable=_STABLE_MIN,
        )
        points = stable_points(spans, _LONG_SEGMENT, _LONG_STEP)
        cap = _CUT_CAP.get(settings.note_detail, 240)
        if len(points) > cap:
            step = len(points) / cap
            points = [points[int(i * step)] for i in range(cap)]
        log.info("缩略图 %d 张，画面停留段 %d 个（稳定 %d 个，要求静止 %d 秒）",
                 len(thumbs), len(spans), sum(1 for s in spans if s.stable), _STABLE_MIN)
        if spans_out is not None:
            spans_out.extend(spans)
        if points:
            for ts, path in extract_frames_at(
                str(video_path), str(workdir / "scene"), points,
                max_edge=settings.frame_max_edge,
            ):
                frame = Frame(ts=ts, path=str(path), origin="scene")
                attach_span(frame, spans)
                frames.append(frame)
    except Exception as exc:
        log.warning("稳定画面抽帧失败：%s", exc)

    # 2. 均匀兜底：防止静态长镜头一帧都抽不到
    step = settings.uniform_interval
    if duration > 0 and len(frames) < max(8, int(duration / (step * 2))):
        stamps = [float(t) for t in np.arange(step, duration - 1.0, step)]
        try:
            for ts, path in extract_frames_at(
                str(video_path), str(workdir / "uniform"), stamps,
                max_edge=settings.frame_max_edge,
            ):
                frame = Frame(ts=ts, path=str(path), origin="uniform")
                # 均匀帧也归到它所在的那段画面，锚定时才知道"讲的是哪张卡"
                attach_span(frame, spans)
                frames.append(frame)
        except Exception as exc:
            log.warning("均匀抽帧失败：%s", exc)

    # 3. 讲解峰值：说到"看这个图"时画面才是重点
    if segments:
        frames.extend(extract_cue_frames(settings, video_path, duration, workdir, segments))

    log.info("候选帧 %d 张（scene/uniform/cue）", len(frames))
    return _reject_blank(frames)


def extract_cue_frames(
    settings: Settings,
    video_path: Path,
    duration: float,
    workdir: Path,
    segments: list[Segment],
    limit: int = 40,
) -> list[Frame]:
    """按"看这个图/公式/代码"这类指代词，在讲到的地方补抽几帧。

    依赖字幕，所以只能在拿到文本之后跑 —— 并行阶段二会调用它。
    """
    cues = [
        s.end + 2.0 for s in segments
        if any(p in s.text for p in _CUE_PATTERNS) and s.end + 2.0 < duration
    ][:limit]
    if not cues:
        return []

    out: list[Frame] = []
    try:
        for ts, path in extract_frames_at(
            str(video_path), str(workdir / "cue"), cues, max_edge=settings.frame_max_edge
        ):
            out.append(Frame(ts=ts, path=str(path), origin="cue"))
    except Exception as exc:
        log.warning("讲解峰值抽帧失败：%s", exc)
    return _reject_blank(out)


def _reject_blank(frames: list[Frame]) -> list[Frame]:
    """丢掉纯色/黑场/严重模糊的帧，省掉后面的 OCR 开销。"""
    kept: list[Frame] = []
    for frame in frames:
        try:
            img = cv2.imread(frame.path, cv2.IMREAD_GRAYSCALE)
            if img is None:
                continue
            if float(img.std()) < 8.0:
                continue
            sharp = float(cv2.Laplacian(img, cv2.CV_64F).var())
            if sharp < 15.0:
                continue
            frame.sharpness = sharp
            kept.append(frame)
        except Exception:
            continue
    return kept


def compute_phash(frame: Frame) -> str:
    try:
        with Image.open(frame.path) as img:
            return str(imagehash.phash(img.convert("RGB"), hash_size=8))
    except Exception:
        return ""


def score_and_select(
    settings: Settings,
    frames: list[Frame],
    chunks: list[Chunk],
    must_keep: set[str] | None = None,
) -> list[Frame]:
    """综合打分后贪心择优，返回最终要放进笔记的帧。

    must_keep 是音画锚定选出的「某段讲解正在讲的那一张画面」的路径集合。
    这些帧加分优先入选——否则按信息量排序时，博主讲的那一页可能被同一块里
    字更多的另一页挤掉，笔记就会出现「文字讲 A、配图是 B」。
    加分只改优先级，不绕过档位的每段上限与去重规则。
    """
    if not frames:
        return []

    keep_counts = must_keep if isinstance(must_keep, dict) else {}
    must_keep = set(keep_counts) if keep_counts else set(must_keep or ())
    chunk_of = _chunk_index_map(chunks, frames)
    max_sharp = max((f.sharpness for f in frames), default=1.0) or 1.0

    for frame in frames:
        frame.phash = frame.phash or compute_phash(frame)
        sharp_norm = math.log1p(frame.sharpness) / math.log1p(max_sharp)
        text_norm = min(1.0, len(frame.ocr_text) / 120.0)
        rel = _relevance(frame, chunks[chunk_of.get(id(frame), 0)] if chunks else None)

        # 「有没有用」看画面里有没有**内容**，与画面里有没有人无关：
        # 老师站在写满板书的黑板前，照样要截。
        # OCR 没覆盖到的（手写、反光、小字）用版面结构分兜底：
        # 边缘密度 + 细节方差能区分「有板书的画面」和「纯人像/风景」。
        if frame.text_density <= 0:
            frame.text_density = text_score(frame.path)
        info = max(text_norm, frame.text_density)
        anchor_bonus = 1.0 if frame.path in must_keep else 0.0
        if info < 0.12 and frame.origin != "cue":
            # 画面上没文字也要留一张：博主正讲的就是这一帧，
            # 笔记里要点带时间码，缺图会让读者对不上。
            frame.score = 0.15 + anchor_bonus if anchor_bonus else 0.0
            continue

        frame.score = (0.35 * text_norm + 0.20 * sharp_norm + 0.25 * rel
                       + 0.10 + 0.15 * frame.text_density + anchor_bonus)

    # 同分时的先后顺序决定「留哪一张」：
    # 板书是边讲边写的，同一段里**越靠后越完整**，所以同分优先取靠后那张。
    # 以前按时间正序排，等于永远留「刚写第一个字」的那张。
    ordered = sorted(frames, key=lambda f: (-round(f.score, 4), -f.ts))
    selected: list[Frame] = []
    per_chunk_count: dict[int, int] = {}
    used_bucket: dict[tuple[int, int], float] = {}

    def _chunk_span(idx: int) -> float:
        if not chunks or idx >= len(chunks):
            return 0.0
        return max(0.0, chunks[idx].end - chunks[idx].start)

    def _bucket(idx: int, ts: float, cap: int) -> int:
        """把知识块均分成 cap 个时间格，每格最多出一张。

        这是「配额铺满整段」的关键：一块 10 分钟给了 8 个名额，就按 75 秒一格铺开。
        只按分数取前 8 张的话，同分时永远落在块首，后面写满板书的部分一张都拿不到。
        """
        span = _chunk_span(idx)
        if not span or not cap:
            return -1
        n = int((ts - chunks[idx].start) / (span / cap))
        return max(0, min(cap - 1, n))

    def take(frame: Frame, allow_full_bucket: bool = False) -> bool:
        # 0 = 不限：详细档靠这个把画面全部留下，不再被固定配额砍掉
        if settings.max_frames_total and len(selected) >= settings.max_frames_total:
            return False
        if frame.score < 0.08:
            return False
        idx = chunk_of.get(id(frame), 0)
        cap = settings.max_frames_per_chunk or 0
        if cap and per_chunk_count.get(idx, 0) >= cap:
            return False
        if cap and not allow_full_bucket:
            b = _bucket(idx, frame.ts, cap)
            if b >= 0 and (idx, b) in used_bucket:
                return False
        if any(_same_page(frame, s) for s in selected):
            return False
        selected.append(frame)
        per_chunk_count[idx] = per_chunk_count.get(idx, 0) + 1
        if cap:
            b = _bucket(idx, frame.ts, cap)
            if b >= 0:
                used_bucket[(idx, b)] = frame.ts
        return True

    # 第一轮：讲解指着的那几张先入座。
    # 同一页 PPT 的几帧 phash 几乎一样，谁先入选另一张就被去重挤掉——
    # 结果是「标 03:28 的要点」上面贴着同页 03:44 的图（真跑过一轮才发现）。
    # 讲得最多的那张优先，同页其它帧再靠分数补。
    keep = [f for f in ordered if f.path in must_keep]
    # 讲得最多的那张优先（同一页被讲得越多，这张越有代表性），其次按时间
    keep.sort(key=lambda f: (-keep_counts.get(f.path, 0), f.ts))
    for frame in keep:
        take(frame)

    # 第二轮：其余帧按信息量补
    for frame in ordered:
        if frame.path in must_keep:
            continue
        take(frame)

    # 第三轮：还有空名额（某几格没有候选帧）时，允许同格再补一张
    for frame in ordered:
        if frame in selected:
            continue
        take(frame, allow_full_bucket=True)

    # 新颖度回填：与最终入选集合的相似度越低越新颖
    for frame in selected:
        others = [s for s in selected if s is not frame and s.phash and frame.phash]
        if others:
            novelty = min(_hamming(frame.phash, o.phash) for o in others) / 64.0
            frame.score = round(frame.score * 0.8 + novelty * 0.2, 4)

    # 兜底一：某个知识块一张都没选到（整段都是人像/风景）时，补该块最清晰的一张。
    # 保证「每段都有画面」，又不至于整篇全是脸——vlog 类视频就靠这个。
    filled = {chunk_of.get(id(f), 0) for f in selected}
    per_chunk_cap = settings.max_frames_per_chunk or 0
    for idx in range(len(chunks)):
        if idx in filled:
            continue
        pool = [f for f in frames if chunk_of.get(id(f), 0) == idx]
        if not pool:
            continue
        if per_chunk_cap and sum(1 for f in selected if chunk_of.get(id(f), 0) == idx) >= per_chunk_cap:
            continue
        cand = max(pool, key=lambda f: f.sharpness)
        # 补位也要守「同一页只留一张」，否则补进来的往往就是刚被去重掉的那张
        if any(_same_page(cand, s) for s in selected):
            continue
        selected.append(cand)

    # 兜底二：整片一张都过不了阈值（纯风景），按清晰度取前 3 张
    if not selected and frames:
        fallback = sorted(frames, key=lambda f: f.sharpness, reverse=True)[:3]
        log.info("无高分帧，按清晰度兜底选取 %d 张", len(fallback))
        selected = fallback

    selected.sort(key=lambda f: f.ts)
    log.info("入选关键帧 %d / 候选 %d", len(selected), len(frames))
    return selected


def text_score(path: str, max_edge: int = 400) -> float:
    """0-1 的"这帧像不像有文字/图表"分数，用于在 OCR 前挡掉纯画面帧。

    两个信号相乘：
    - 边缘密度：文字行和表格线都会产生密集横向边缘，纯色/渐变接近 0
    - 拉普拉斯方差：有文字的版面细节极多，人物和风景低一个数量级

    实测（400px 缩略图）：
        类型        边缘密度   拉普拉斯   得分
        PPT 幻灯片    2.40%     3039     1.00
        表格          1.66%     2133     1.00
        人物画面      0.67%       54     0.03
        渐变背景      0.00%      0.8     0.00

    不用轮廓计数法：表格的横竖线在闭运算后仍是细线段，
    按长宽比过滤会被全部丢掉（整张表只能数出 1 块）。
    """
    img = cv2.imread(path)
    if img is None:
        return 0.0

    h, w = img.shape[:2]
    if max(h, w) > max_edge:
        scale = max_edge / max(h, w)
        img = cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)

    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    sharpness = float(cv2.Laplacian(gray, cv2.CV_64F).var())

    grad = cv2.Sobel(gray, cv2.CV_8U, 1, 0, ksize=3)
    try:
        _, binary = cv2.threshold(grad, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)
    except cv2.error:
        return 0.0
    density = float((binary > 0).mean())

    edge_term = min(1.0, density / 0.012)
    detail_term = min(1.0, sharpness / 800.0)
    return round(edge_term * detail_term, 3)


def filter_text_frames(
    frames: list[Frame],
    min_score: float = 0.35,
    on_progress=None,
) -> tuple[list[Frame], int]:
    """挑出大概率含文字或图表的帧，返回 (保留的帧, 被跳过的数量)。

    跳过的帧仍留在候选池里（只是没有 OCR 文本、打分自然低），
    不会因为过滤而彻底丢失画面。
    """
    if not frames:
        return [], 0

    kept: list[Frame] = []
    total = len(frames)
    for i, frame in enumerate(frames, 1):
        score = text_score(frame.path)
        # 存下来：打分阶段还要用它兜底。手写板书、反光屏 OCR 常认不出，
        # 但版面结构（边缘密度 + 细节）能说明"这画面里有内容"，不能因为
        # OCR 没读到就把整帧丢掉。
        frame.text_density = score
        if score >= min_score:
            kept.append(frame)
        if on_progress and total:
            on_progress(i / total * 100.0, f"预筛画面 {i}/{total}")
    return kept, total - len(kept)


def upgrade_selected(
    frames: list[Frame],
    hi_url: str,
    workdir: Path,
    max_edge: int = 1920,
) -> int:
    """把选中的关键帧换成高清版本，返回替换张数。

    候选阶段为了跑得快用的是低清；真正进笔记的这几十张必须是原画质，
    否则 OCR 认不准 PPT 上的小字，用户看到的图也糊。
    """
    if not frames or not hi_url:
        return 0

    hd = extract_frames_from_url(hi_url, str(workdir / "hd"), [f.ts for f in frames], max_edge=max_edge)
    by_ts = {round(ts, 2): path for ts, path in hd}

    upgraded = 0
    for frame in frames:
        new_path = by_ts.get(round(frame.ts, 2))
        if not new_path:
            continue
        try:
            with Image.open(new_path) as im:
                new_w = im.size[0]
            with Image.open(frame.path) as im:
                old_w = im.size[0]
        except Exception:
            continue
        if new_w > old_w:
            frame.path = str(new_path)
            frame.ocr_text = ""      # 高清帧要重新识别
            frame.ocr_blocks = []
            upgraded += 1

    log.info("高清重取 %d / %d 张", upgraded, len(frames))
    return upgraded


def _chunk_index_map(chunks: list[Chunk], frames: list[Frame]) -> dict[int, int]:
    mapping: dict[int, int] = {}
    if not chunks:
        return mapping
    for frame in frames:
        owner = 0
        for i, chunk in enumerate(chunks):
            if chunk.start <= frame.ts < chunk.end:
                owner = i
                break
        else:
            owner = min(
                range(len(chunks)),
                key=lambda i: min(abs(chunks[i].start - frame.ts), abs(chunks[i].end - frame.ts)),
            )
        mapping[id(frame)] = owner
    return mapping


def _relevance(frame: Frame, chunk: Chunk | None) -> float:
    if chunk is None or not frame.ocr_text:
        return 0.0
    a, b = bigrams(frame.ocr_text), bigrams(chunk.text)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a)


def _same_page(a: Frame, b: Frame) -> bool:
    """两张帧是不是「同一页画面」（去重用）。

    两条判据任一成立即可：
      1. 整帧几乎完全一致（< _DUP_DISTANCE）——静止幻灯片；
      2. 时间邻近且相似（< _DUP_NEAR 且在 _DUP_WINDOW 内）——真人出镜的板书课，
         人在动导致整帧差异被放大，但内容其实是同一块黑板。
    """
    d = _hamming(a.phash, b.phash)
    if d >= 64:          # 没有可区分特征（纯色/黑屏），不当作重复
        return False
    if d < _DUP_DISTANCE:
        return True
    return d < _DUP_NEAR and abs(a.ts - b.ts) <= _DUP_WINDOW


def _hamming(a: str, b: str) -> int:
    if not a or not b or len(a) != len(b):
        return 64
    try:
        va, vb = int(a, 16), int(b, 16)
    except ValueError:
        return 64
    # 感知哈希几乎没有有效位 = 画面里没有可区分特征（纯色、黑屏、纯白，
    # 它们的 phash 都是 8000000000000000）。这种帧两两距离都是 0，
    # 判成「重复」会把不同画面全丢掉，所以按「完全不同」处理，
    # 让它们走正常的分数排序与配额淘汰。
    if bin(va).count("1") < 4 or bin(vb).count("1") < 4:
        return 64
    return bin(va ^ vb).count("1")
