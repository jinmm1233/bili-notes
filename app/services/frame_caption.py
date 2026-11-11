"""画面要点：从关键帧里「有选择地」提取画面文字，并结合讲解补全。

定位（随用户反馈迭代）：
1. v1 平铺 OCR 原文 —— 行列错乱、碎片化，"太乱"，弃用；
2. v2 「画面解读」—— 让模型把画面写成一段话。正确但没用：
   正文要点已经把内容讲过一遍，解读只是换个说法重复；
3. v3（当前）「画面要点」—— 把画面当信息源而不是装饰：
   - PPT 的题目 / 标题 / 分点原样提取（压缩成完整句子，OCR 截断的补齐）；
   - 没分点的（一大段文字）由模型按语义分点；
   - 内容太简略（只有标题）的用讲解补 1-3 条要点；
   - 水印 / 播放器 UI / 弹幕 / 字幕条 / 页码一概不取。

产出同时回喂总结层（summarize.build_outline）：大纲能看到 PPT 的
题目和分点，笔记结构因此可以贴着课件的骨架走，术语数字以画面为准。

成本控制：视觉模式每帧一次请求（多数视觉模型单消息只吃一张图稳），
图片先压到长边 1024 内再上传；文字模式按知识块批量。
"""
from __future__ import annotations

import copy
import io
import logging
from pathlib import Path
from typing import Any, Callable

from ..core.config import Settings
from ..core.llm import LLMClient, parse_json_loose
from ..core.models import Chunk, Frame
from ..core.paths import fmt_ts
from . import mathfmt

log = logging.getLogger(__name__)

ProgressFn = Callable[[float, str], None]
CancelFn = Callable[[], bool]

_COMMON_RULES = (
    "【取什么】——画面上真正承载信息的文字：\n"
    "- PPT / 课件的题目、标题、小标题；\n"
    "- 分点列出的要点、定义、结论、步骤；\n"
    "- 公式、代码、表格里的关键内容；\n"
    "- 界面 / 地图 / 单据上的关键字段（名称、数字、参数）。\n\n"
    "【不取什么】：\n"
    "- 平台水印、播放器 UI、弹幕、进度条、画面底部滚动的那行讲解字幕；\n"
    "- 页码、反复出现的装饰文字、重复的栏目名；\n"
    "- 看不清、读不通、明显识别错误的碎片——宁可少取，不要硬凑。\n\n"
    "【怎么加工】：\n"
    "- 画面自带分点的：按原分点保留，每条压缩成一句完整通顺的话"
    "（识别截断的字按讲解与上下文补齐）；\n"
    "- 画面是一大段文字、没有分点的：按语义拆成 2-5 条要点；\n"
    "- 画面只有标题、内容简略的：从【讲解文字】里取对应的解释，补 1-3 条要点；\n"
    "- 讲解和画面冲突时以画面为准；不确定的不要编。\n\n"
    "【公式与矩阵】（数学类画面最重要）：\n"
    "- 画面上的公式、矩阵、行列式要用 LaTeX 写进要点：行内短公式写 $a_{ij}$ 这样，"
    "矩阵和独立公式写 $$\\begin{bmatrix} 1 & 2 \\\\ 3 & 4 \\end{bmatrix}$$ 这样；\n"
    "- 矩阵必须照画面的行列顺序写（& 分列、\\\\ 换行），元素看不清就留空位或整条不写，不要猜数；\n"
    "- 不要把矩阵拆成「第一行是…第二行是…」的叙述——那样读者看不到矩阵本身；\n"
    "- 也**不要**写成 [[3,2,1],[3,1,5]] 或 [1 2 3; 4 5 6] 这种列表，那在笔记里只是一串字符。\n"
)

_JSON_SPEC = (
    '严格只返回 JSON：{"captions":[{"index":1,'
    '"kind":"板书/PPT/图表/实拍/人物/空镜/界面",'
    '"useful":true,'
    '"info":3,'
    '"page":"这一页的标识",'
    '"title":"画面标题/PPT题目，没有就空字符串",'
    '"points":["要点1","要点2"]}]}，'
    "points 2-5 条、每条不超过 40 字；画面确实没有可取文字时 "
    "title 和 points 都给空数组或空字符串，不要硬凑。"
    "index 与画面序号一一对应，不要输出任何解释文字。"
)

_JUDGE_RULES = (
    "\n\n【这帧值不值得进笔记】除要点外，还要给四个判断：\n"
    "- kind：画面类型，取值之一——板书（黑板/白板手写）、PPT（课件页面）、"
    "图表（曲线图/示意图/表格）、实拍（实验器材/实物演示）、人物（主要是人）、"
    "空镜（几乎空白、墙面、过渡画面）、界面（软件/代码/操作界面）。\n"
    "- useful：true/false。判断标准只有一条——**这帧画面上有没有值得留存的信息**。\n"
    "  true：板书、课件、公式推导、图表、关键操作界面；\n"
    "  false：只有人物、空镜、纯墙面天花板、严重虚焦或过曝看不清、翻页过程中的模糊帧。\n"
    "  ⚠️ 老师站在写满字的黑板前 → useful=true（信息在黑板，不在人）。\n"
    "- info：1-5 的整数，画面承载的信息量。1=几乎空白，2=只有一两行字，"
    "3=常规一页课件，4=多步推导/多条要点，5=整屏公式或复杂图表。\n"
    "  ⚠️ 要在**同一批画面之间比较**着给分：只有标题的一律 2，"
    "有图表/多步推导的给 4-5，不要所有帧都给 3。\n"
    "- page：这一页画面的标识。**同一页要输出完全相同的字符串**（用页面标题或"
    "最显著的那行字，例如 \"§9.3 电场 电场强度\"）；换页就必须不同。"
    "没有页面概念的（实拍、人物、空镜）填空字符串。\n"
)

_CAPTION_SYSTEM = (
    "你是视频笔记助手，负责从视频画面里「有选择地」提取文字要点。\n"
    "我会给你一张画面截图，这一段的【讲解文字】（语音转写，可能有错别字），"
    "以及 OCR 识别到的【文字线索】（行列顺序可能乱、可能断行缺字，仅供比对）。\n\n"
    + _COMMON_RULES
    + _JUDGE_RULES
    + "\n"
    + _JSON_SPEC
)

_SYSTEM = (
    "你是视频笔记助手，负责从视频画面里「有选择地」提取文字要点。\n"
    "我会给你一张画面截图，这一段的【讲解文字】（语音转写，可能有错别字），"
    "以及 OCR 识别到的【文字线索】（行列顺序可能乱、可能断行缺字，仅供比对）。\n\n"
    + _COMMON_RULES
    + _JUDGE_RULES
    + "\n"
    + _JSON_SPEC
)

_SYSTEM_TEXT = (
    "你是视频笔记助手，负责从视频画面里「有选择地」提取文字要点。\n"
    "我会给出某一段视频的【讲解文字】，以及这一段里若干张【画面】的 OCR 文字"
    "（行列顺序可能乱、可能断行缺字，画面本身可能没有文字）。\n\n"
    + _COMMON_RULES
    + "\n"
    + _JSON_SPEC
)


def _frame_text(frame: Frame) -> str:
    ocr = (frame.ocr_text or "").strip().replace("\n", " / ")
    return ocr[:400] if ocr else "（无文字，可能是实景/人物/操作演示）"


def _shrink_jpeg(path: str, max_side: int = 1024, quality: int = 82) -> tuple[bytes, str]:
    """读图并压到长边 max_side 内，返回 (jpeg 字节, mime)。压不动就退回原图。"""
    try:
        from PIL import Image  # noqa: PLC0415

        with Image.open(path) as im:
            im = im.convert("RGB")
            w, h = im.size
            if max(w, h) > max_side:
                scale = max_side / max(w, h)
                im = im.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=quality)
            return buf.getvalue(), "image/jpeg"
    except Exception as exc:  # noqa: BLE001
        log.warning("压缩关键帧失败 %s：%s", path, exc)
        try:
            return Path(path).read_bytes(), "image/jpeg"
        except OSError:
            return b"", ""


def _apply(frame: Frame, rec: dict[str, Any]) -> bool:
    """把一条模型结果写到帧上。返回是否取到了有效内容。"""
    # 判断类字段先落：即使这帧没有文字要点（空镜/人物），也要记下「判过、没用」，
    # 后面的 prune 才知道该不该把它剔掉。
    kind = str(rec.get("kind") or "").strip()[:8]
    page = str(rec.get("page") or "").strip()[:60]
    useful = rec.get("useful")
    try:
        info = int(rec.get("info") or 0)
    except (TypeError, ValueError):
        info = 0
    if kind:
        frame.kind = kind
    if page:
        frame.page_key = page
    if isinstance(useful, bool):
        frame.useful = useful
    if 0 <= info <= 9:
        frame.info_level = info

    title = str(rec.get("title") or "").strip()[:40]
    points = [
        str(p).strip() for p in (rec.get("points") or [])
        if str(p).strip()
    ][:5]
    if not title and not points:
        return False
    # 模型爱把矩阵写成 [[1,2],[3,4]] / A^-1，这里统一转成 LaTeX，笔记里才能渲染
    title = mathfmt.normalize(title)
    points = [mathfmt.normalize(p) for p in points]
    frame.screen_title = title
    frame.screen_points = points
    # caption 保留为单行摘要，供旧逻辑（代表画面选择等）沿用
    frame.caption = "；".join([title, *points])[:150]
    return True


# 判定「不值得进笔记」的画面类型。模型已经给了 useful，这里只用来兜一道：
# 万一它把 useful 写错（比如人物特写也给了 true），类型是空镜/人物的仍然剔掉。
# 「同一页」的重复判定时间窗（秒）。
# 一整节课都在同一块黑板上写时，模型会给每一帧同一个 page_key。没有时间窗的
# 「同页只留一张」会把 15 分钟的课从 24 张砍到 7 张，后面几个知识点全部配不到图
# （实测：画面判断剔掉同页重复 17 张，后三节零配图）。
# 板书会随时间累积，同一页在不同时间点承载的内容并不相同，
# 只有**时间上相邻**的才算重复。30 秒 = 只合并几乎连着拍的重复帧。
_DUP_WINDOW = 30.0
# 剪枝后重新挂锚点时，允许的最大时间偏移（秒）。
# 同页的可以放得更宽（本来就是同一块板），不同页的收紧，避免张冠李戴。
_REATTACH_SAME_PAGE = 300.0
_REATTACH_ANY = 120.0

_DROP_KINDS = {"空镜", "人物"}

_PAGE_NOISE = " \t\r\n，。、：:；;·-—_()（）[]【】\"'“”‘’《》<>"


def _page_norm(key: str) -> str:
    """同一页的两个写法要能对上。

    实测模型会给「§9.3 电场 电场强度」和「§9.3电场 电场强度」（差一个空格），
    不归一化就当成两页，去重直接失效。所以只留实义字符再比。
    """
    return "".join(ch for ch in (key or "").lower() if ch not in _PAGE_NOISE)[:40]


def prune(chunks: list[Chunk]) -> dict[str, int]:
    """按视觉模型的判断剔帧：没用的删掉、同一页且时间相邻的只留信息量最高的那张。

    只处理**判过**的帧（info_level > 0）。没配视觉模型时整篇都是 0，
    这一步等于不生效，行为与以前完全一致。

    为什么用 page_key 而不是 pHash：老师站在黑板前走动时，整帧 pHash 会差 6-8 位，
    按像素判定认不出「同一页」；模型能直接读出这是同一块黑板、同一行标题。

    为什么必须带时间窗（_DUP_WINDOW）：整节课都在同一块黑板上写时，模型对每帧
    都给同一个 page_key。按「同页只留一张」去重，24 张会掉到 7 张，
    后面几个知识点的配图全部落空。同页但相隔较远 → 内容不同，各留各的。
    """
    from . import frames as _frames_mod  # 延迟导入：避免与 frames 循环依赖

    dropped_useless = 0
    dropped_dup = 0
    reattached = 0
    for chunk in chunks:
        if not chunk.frames:
            continue
        judged = [f for f in chunk.frames if f.info_level > 0]
        if not judged:
            continue
        keep: list[Frame] = []
        # page -> 簇；每簇内的帧时间相邻、属于同一页，只留一个代表
        clusters: dict[str, list[list[Frame]]] = {}
        page_by_path: dict[str, str] = {}
        page_by_ts: dict[float, str] = {}

        for frame in chunk.frames:
            if frame.info_level <= 0:       # 没判过 → 保持原样（像素去重仍然生效）
                if any(_frames_mod._same_page(frame, k) for k in keep if k.phash):
                    dropped_dup += 1
                    continue
                keep.append(frame)
                continue
            key = _page_norm(frame.page_key)
            page_by_path[frame.path] = key
            page_by_ts[round(frame.ts, 2)] = key
            if not frame.useful:
                dropped_useless += 1
                continue
            # 模型说有用、但类型是空镜/人物且信息量极低 —— 兜一道，防止它手滑
            if frame.kind in _DROP_KINDS and frame.info_level <= 2:
                dropped_useless += 1
                continue
            if not key:
                keep.append(frame)
                continue
            bucket = clusters.setdefault(key, [])
            # 跟「簇的第一张」比距离，而不是跟上一张比：否则 0s/37s/74s 会链式合并，
            # 一节课的画面被一路吃光。
            target: list[Frame] | None = None
            for cl in bucket:
                if abs(frame.ts - cl[0].ts) <= _DUP_WINDOW:
                    target = cl
                    break
            if target is None:
                bucket.append([frame])
            else:
                target.append(frame)

        # 每簇只留代表帧（信息量最高，一样高就留靠后的）
        final: list[Frame] = []
        seen_ids: set[int] = set()

        def _add(frame: Frame) -> None:
            if id(frame) not in seen_ids:
                seen_ids.add(id(frame))
                final.append(frame)

        for frame in keep:
            _add(frame)
        for bucket in clusters.values():
            for cl in bucket:
                best = max(cl, key=lambda f: (f.info_level, f.ts))
                _add(best)
                dropped_dup += len(cl) - 1
        keep = sorted(final, key=lambda f: f.ts)
        # 兜底：整块都被判成没用时（纯人像讲解、全片空镜），至少留一张最像样的。
        # 否则那一节在笔记里完全没画面，读者对不上时间码。
        if not keep:
            keep = [max(chunk.frames, key=lambda f: (f.info_level, f.sharpness, f.ts))]
            log.info("整块无可用画面，保留信息量最高的一张（%s）", fmt_ts(keep[0].ts))
        chunk.frames = keep
        reattached += _reattach_anchors(chunk, keep, page_by_path, page_by_ts)
    if dropped_useless or dropped_dup:
        log.info("画面判断：剔掉无信息 %d 张、同页重复 %d 张", dropped_useless, dropped_dup)
    if reattached:
        log.info("剪枝后锚点重挂到幸存帧：%d 处", reattached)
    return {"useless": dropped_useless, "duplicate": dropped_dup, "reattached": reattached}


def _reattach_anchors(chunk: Chunk, survivors: list[Frame],
                      page_by_path: dict[str, str],
                      page_by_ts: dict[float, str]) -> int:
    """剪枝之后，把锚点重新指到幸存的帧上。

    锚点的语义是「这句讲解对应哪一帧」。prune 把帧删了，锚点还指着那个已经不存在的
    时刻 —— 配图按 ±8 秒就近找，找不到就是「这一条没有画面」，
    后面几个知识点会整段没图（实测 24 张剪到 9 张，211 个锚点里 176 个配不上图）。

    找法：优先挂到**同一页**的幸存帧上（本来就是同一块板，指过去不会张冠李戴），
    没有同页的再退回本知识块内时间最近的。

    锚点常常只有 frame_ts、frame_path 是空的（音画锚定时还没选帧），
    所以页码要能按时间戳反查，否则「同页优先」根本用不上。
    """
    if not survivors:
        return 0
    by_page: dict[str, list[Frame]] = {}
    for f in survivors:
        key = page_by_path.get(f.path) or ""
        if key:
            by_page.setdefault(key, []).append(f)

    moved = 0
    for a in chunk.anchors:
        if a.frame_ts <= 0:
            continue
        key = page_by_path.get(a.frame_path) or ""
        if not key:
            # 按时间戳反查它原本对应的那一帧属于哪一页
            near = min(page_by_ts.items(),
                       key=lambda kv: abs(kv[0] - a.frame_ts), default=None)
            if near and abs(near[0] - a.frame_ts) <= 3.0:
                key = near[1]
        if key and by_page.get(key):
            pool = by_page[key]
            limit = _REATTACH_SAME_PAGE
        else:
            pool = survivors
            limit = _REATTACH_ANY
        best = min(pool, key=lambda f: abs(f.ts - a.frame_ts))
        gap = abs(best.ts - a.frame_ts)
        if gap < 0.01 or gap > limit:
            continue
        a.frame_ts = best.ts
        a.frame_path = best.path
        moved += 1
    return moved


def caption_frames(
    text_client: LLMClient | None,
    chunks: list[Chunk],
    settings: Settings,
    vision_client: LLMClient | None = None,
    on_progress: ProgressFn | None = None,
    cancel: CancelFn | None = None,
) -> int:
    """为关键帧提炼「画面要点」，回写 frame.screen_title/screen_points。

    返回取到有效要点的帧数。
    """
    if text_client is None or not settings.frame_captions:
        return 0

    todo = [c for c in chunks if c.frames]
    total_frames = sum(len(c.frames) for c in todo)
    if not total_frames:
        return 0

    # 详细档一节课可能上百张图，而视觉模式是「一帧一次请求」——不限流会跑几小时。
    # 超过阈值就退回文字模式：OCR 原文 + 讲解补全，要点照样有，只是不"看图"。
    if (vision_client and settings.vision_max_frames
            and total_frames > settings.vision_max_frames):
        log.warning("关键帧 %d 张超过视觉模式上限 %d，本次改用文字模式提炼画面要点",
                    total_frames, settings.vision_max_frames)
        vision_client = None

    mode = "视觉" if vision_client else "文字"
    log.info("画面要点提炼开始（%s模式）：%d 块 / %d 张图", mode, len(todo), total_frames)
    done = 0
    for chunk in todo:
        if cancel and cancel():
            break
        caps = (_notes_with_vision(vision_client, chunk, settings) if vision_client
                else _notes_with_text(text_client, chunk, settings))

        for i, frame in enumerate(chunk.frames, 1):
            if rec := caps.get(i):
                _apply(frame, rec)
        done += len(chunk.frames)

        if on_progress and total_frames:
            pct = min(99.0, done / total_frames * 100.0)
            on_progress(pct, f"提炼画面要点 {pct:.0f}%")

    if on_progress:
        on_progress(100.0, "画面要点提炼完成")
    n = sum(1 for c in chunks for f in c.frames if f.screen_points or f.screen_title)
    log.info("画面要点提炼完成：%d 张取到内容", n)
    return n


def chunk_screen_lines(chunk: Chunk, with_ts: bool = False) -> list[str]:
    """把一个块内各帧的画面要点拼成行（同画面去重，保持顺序）。

    总结层（逐块总结 / 结构化大纲）用它当【画面】材料——
    比 OCR 原文干净得多，也比"画面解读"多保留了 PPT 的原文骨架。
    with_ts=True 时行首带时间码，供大纲把 cover_ts 指到有文字的画面。
    """
    lines: list[str] = []
    seen: set[tuple[str, ...]] = set()
    for frame in chunk.frames:
        bits = tuple(b for b in [frame.screen_title, *frame.screen_points] if b)
        if not bits and frame.caption:      # 旧数据兜底
            bits = (frame.caption,)
        if not bits or bits in seen:
            continue
        seen.add(bits)
        line = "；".join(bits)
        lines.append(f"[{fmt_ts(frame.ts)}] {line}" if with_ts else line)
    return lines


def _density_note(settings: Settings | None) -> str:
    """按笔记详细度调整画面要点的颗粒度。"""
    level = getattr(settings, "note_detail", "standard")
    if level == "detailed":
        return (
            "\n【详细度：详细】画面上的每一个分点、公式、数字、代码行都要单独成一条，"
            "不要合并同类项、不要概括成一句话；同一张图上的多个例子也要逐条列出。"
            "画面只有标题的，从【讲解文字】里补 3-5 条具体解释。points 最多可到 10 条。"
        )
    if level == "brief":
        return "\n【详细度：简洁】points 最多 3 条，只留最关键的分点。"
    return ""


def _notes_with_text(
    client: LLMClient, chunk: Chunk, settings: Settings | None = None,
) -> dict[int, dict[str, Any]]:
    """文字模式：一个块一次请求，按序号批量回填。"""
    lines = [f"【讲解文字】{chunk.text[:1200]}", "", "【画面】"]
    for i, f in enumerate(chunk.frames, 1):
        lines.append(f"画面{i}（第{int(f.ts)}秒）OCR文字：{_frame_text(f)}")
    detailed = getattr(settings, "note_detail", "") == "detailed"
    return _parse_caps(client, _SYSTEM_TEXT + _density_note(settings),
                       "\n".join(lines), max_tokens=2200 if detailed else 1400)


def _parse_caps(
    client: LLMClient, system: str, user: str, max_tokens: int = 1400,
) -> dict[int, dict[str, Any]]:
    try:
        data: dict[str, Any] = client.complete_json(system, user, max_tokens=max_tokens)
        caps = data.get("captions") or []
    except Exception as exc:  # noqa: BLE001
        log.warning("画面要点提炼单块失败，跳过：%s", exc)
        caps = []

    by_index: dict[int, dict[str, Any]] = {}
    for pos, rec in enumerate(caps, 1):
        if not isinstance(rec, dict):
            continue
        try:
            idx = int(rec.get("index", pos))
        except (TypeError, ValueError):
            idx = pos
        if str(rec.get("title") or "").strip() or any(
            str(p).strip() for p in (rec.get("points") or [])
        ):
            by_index[idx] = rec
    return by_index


def _notes_with_vision(
    client: LLMClient, chunk: Chunk, settings: Settings | None = None,
) -> dict[int, dict[str, Any]]:
    """视觉模式：每帧一次请求，模型真正"看"图。失败的单帧退回文字模式兜底。"""
    out: dict[int, dict[str, Any]] = {}
    for i, frame in enumerate(chunk.frames, 1):
        if not frame.path:
            continue
        data, mime = _shrink_jpeg(frame.path)
        if not data:
            continue
        ocr = (frame.ocr_text or "").strip()
        ocr_hint = ocr[:300] + ("…" if len(ocr) > 300 else "") if ocr else "（无）"
        user = (
            f"【讲解文字】{chunk.text[:1000]}\n"
            f"【文字线索】{ocr_hint}\n"
            f"请提取画面{i}（第{int(frame.ts)}秒）上的文字要点，只返回约定格式的 JSON。"
        )
        detailed = getattr(settings, "note_detail", "") == "detailed"
        try:
            caps: list[dict[str, Any]] = []
            for attempt in range(2):
                # 视觉模型偶尔会吐一段说明文字而不是 JSON（实测 6 张里挂 1 张）。
                # 不重试的话这帧就"没判过"，后面的同页去重会漏掉它，白留一张重复图。
                raw = client.complete_vision(
                    _SYSTEM + _density_note(settings)
                    + ("\n\n⚠️ 上一次返回的不是合法 JSON。这次只输出 JSON，不要任何解释。"
                       if attempt else ""),
                    user, [(data, mime)],
                    max_tokens=1300 if detailed else 900,
                )
                caps = parse_json_loose(raw).get("captions") or []
                if caps:
                    break
                log.info("画面判断返回非 JSON，重试一次（帧 %s）", frame.path)
            for rec in caps:
                if not isinstance(rec, dict):
                    continue
                # 有文字要点、或者有画面判断（useful/info/kind）都要收：
                # 空镜/人物这类帧本来就没有文字，但「它没用」这个结论必须落下来，
                # 否则 prune 看不到判断，照样会把人物特写放进笔记。
                has_text = (str(rec.get("title") or "").strip()
                            or any(str(p).strip() for p in (rec.get("points") or [])))
                has_judge = bool(rec.get("kind") or rec.get("page")
                                 or rec.get("useful") is not None or rec.get("info"))
                if has_text or has_judge:
                    out[i] = rec
                    break
        except Exception as exc:  # noqa: BLE001
            log.warning("看图提炼失败（帧 %s），退回文字线索：%s", frame.path, exc)
            out.update(_notes_with_text(client, _single_chunk(chunk, i), settings))
    return out


def _single_chunk(chunk: Chunk, index_1based: int) -> Chunk:
    """造一个只含指定帧的临时块，供单帧退回文字模式复用批量逻辑。"""
    mini = copy.copy(chunk)
    mini.frames = [chunk.frames[index_1based - 1]]
    return mini
