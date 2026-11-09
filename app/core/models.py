"""跨模块共享的数据结构。"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

_BV_RE = re.compile(r"(BV[0-9A-Za-z]{10})")
_AV_RE = re.compile(r"av(\d+)", re.I)


@dataclass
class Segment:
    """一句字幕/转录文本。"""

    start: float
    end: float
    text: str
    source: str = "cc"  # cc | ai | asr
    translation: str = ""  # 非中文转录的中文译文（双语模式）；中文转录为空

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "start": self.start,
            "end": self.end,
            "text": self.text,
            "source": self.source,
        }
        if self.translation:
            d["translation"] = self.translation
        return d


@dataclass
class VideoMeta:
    bvid: str
    cid: int
    title: str
    uploader: str
    duration: float
    pubdate: int
    cover: str
    webpage_url: str
    page_index: int = 1
    page_title: str = ""
    part_count: int = 1
    description: str = ""
    # 来源收藏夹的名字。知识库按它做大类分组；单条添加的为空。
    collection: str = ""
    # 展开「多P / UP 主合集」批量导入时记下的系列身份，知识库据此把同一
    # 合集的多集折叠成一张卡。单条添加为空，由 library 从 URL/标题兜底推断。
    series_id: str = ""
    series_title: str = ""
    series_order: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "bvid": self.bvid,
            "cid": self.cid,
            "title": self.title,
            "uploader": self.uploader,
            "duration": self.duration,
            "pubdate": self.pubdate,
            "cover": self.cover,
            "webpage_url": self.webpage_url,
            "page_index": self.page_index,
            "page_title": self.page_title,
            "part_count": self.part_count,
            "description": self.description,
            "collection": self.collection,
            "series_id": self.series_id,
            "series_title": self.series_title,
            "series_order": self.series_order,
        }


@dataclass
class SceneSpan:
    """一段「画面停住没变」的时间区间。

    博主暂停讲解时画面往往停在某一页 PPT / 某一句双语字幕上，
    这段时间内说的所有话讲的都是这一帧。抽帧只能给出末帧时间点，
    但「哪句讲解属于哪张画面」要靠区间才能算出来。
    """

    start: float
    end: float
    rep_ts: float          # 区间末帧：内容放全的那一刻
    stable: bool = True    # 是否达到「静止 N 秒」的稳定段

    def to_dict(self) -> dict[str, Any]:
        return {
            "start": self.start, "end": self.end,
            "rep_ts": self.rep_ts, "stable": self.stable,
        }


@dataclass
class Anchor:
    """一个「画面 + 同期讲解」的音画对。

    笔记里每条要点都要能回答「它讲的是哪一帧」，靠的就是这个：
    frame_ts 是那张画面的时间点，speech 是这段时间博主说的话，
    screen 是画面上本来就有的文字（例句 / PPT / 板书）。
    """

    start: float
    end: float
    frame_ts: float = 0.0
    frame_path: str = ""
    speech: str = ""
    screen: str = ""
    span_start: float = 0.0
    span_end: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "start": self.start,
            "end": self.end,
            "frame_ts": self.frame_ts,
            "frame_path": self.frame_path,
            "speech": self.speech,
            "screen": self.screen,
            "span_start": self.span_start,
            "span_end": self.span_end,
        }


@dataclass
class Frame:
    """一张候选/关键帧。"""

    ts: float
    path: str
    origin: str = "scene"  # scene | uniform | cue
    # 所属画面稳定段的区间；抽帧阶段填好，音画锚定阶段用
    seg_start: float = 0.0
    seg_end: float = 0.0
    sharpness: float = 0.0
    ocr_text: str = ""
    ocr_blocks: list[dict[str, Any]] = field(default_factory=list)
    text_density: float = 0.0
    phash: str = ""
    score: float = 0.0
    # 画面要点（v3）：从画面里「有选择地」提取的文字信息——
    # PPT 题目 / 分点 / 公式 / 表格关键行，内容太简略时用讲解补全。
    # screen_title 是画面的标题（PPT 题目），screen_points 是分点要点。
    # caption 保留为单行摘要（标题+前几条要点），供旧逻辑与 hero 选择用。
    screen_title: str = ""
    screen_points: list[str] = field(default_factory=list)
    caption: str = ""

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "ts": self.ts,
            "path": self.path,
            "origin": self.origin,
            "seg_start": round(self.seg_start, 2),
            "seg_end": round(self.seg_end, 2),
            "sharpness": round(self.sharpness, 4),
            "ocr_text": self.ocr_text,
            "text_density": round(self.text_density, 4),
            "phash": self.phash,
            "score": round(self.score, 4),
        }
        if self.screen_title:
            d["screen_title"] = self.screen_title
        if self.screen_points:
            d["screen_points"] = self.screen_points
        if self.caption:
            d["caption"] = self.caption
        return d


@dataclass
class Chunk:
    """知识块：一段连续讲解 + 其覆盖的画面/OCR。"""

    index: int
    start: float
    end: float
    text: str
    frames: list[Frame] = field(default_factory=list)
    ocr_text: str = ""
    summary: str = ""
    bullets: list[str] = field(default_factory=list)
    # 与 bullets 一一对应：每条要点讲的是哪一刻（画面时间点）
    bullet_ts: list[float] = field(default_factory=list)
    # 块内按画面段切好的「画面 + 同期讲解」音画对
    anchors: list[Anchor] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    title: str = ""

    @property
    def char_count(self) -> int:
        return len(self.text)


@dataclass
class Chapter:
    index: int
    title: str
    start: float
    end: float
    chunk_indexes: list[int] = field(default_factory=list)
    summary: str = ""
    bullets: list[str] = field(default_factory=list)


@dataclass
class Problem:
    """视频里讲到的一道例题/题目。

    笔记里题目的价值不在"复述题干"，而在"为什么这么做"：
    key 是解题的关键一步（题眼），steps 是可照着做的步骤，pitfall 是最容易错的地方。
    """

    title: str = ""          # 题目标识，如「矩阵连乘」「求逆矩阵」
    statement: str = ""      # 题干（公式用 LaTeX）
    key: str = ""            # 解题关键：突破口 / 为什么想到这一步
    steps: list[str] = field(default_factory=list)
    pitfall: str = ""        # 易错点，可为空
    ts: float = 0.0          # 讲解这道题的时间点

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "title": self.title,
            "statement": self.statement,
            "key": self.key,
            "steps": self.steps,
            "ts": self.ts,
        }
        if self.pitfall:
            d["pitfall"] = self.pitfall
        return d


@dataclass
class VideoNote:
    meta: VideoMeta
    segments: list[Segment] = field(default_factory=list)
    chunks: list[Chunk] = field(default_factory=list)
    chapters: list[Chapter] = field(default_factory=list)
    one_liner: str = ""
    highlights: list[str] = field(default_factory=list)
    glossary: list[dict[str, str]] = field(default_factory=list)
    mindmap: dict[str, Any] = field(default_factory=dict)
    # 结构化大纲：由模型把材料重组成「主题 → 子主题 → 要点」的层级。
    # 与 chapters 的区别：chapters 是时间切片，outline 是按内容逻辑重组，
    # 同一个地点可能横跨多个时间块，也可能一个块里出现两个地点。
    outline: list[dict[str, Any]] = field(default_factory=list)
    takeaways: list[str] = field(default_factory=list)
    # 例题：视频里出现的题目，抽成「题干 + 题眼 + 步骤 + 易错」。
    # 只在材料里确实有题目时才有内容。
    problems: list[Problem] = field(default_factory=list)
    # 行程路线：旅行类视频专有。结构见 itinerary.build_itinerary 的返回值。
    itinerary: dict[str, Any] = field(default_factory=dict)
    transcript_source: str = "cc"
    generated_at: str = ""
    text_fixes: list[dict] = field(default_factory=list)


def parse_video_id(url_or_id: str) -> str:
    """从各种形式的输入中抽出 BV/av 号，抽不出则原样返回（短链交由 yt-dlp 处理）。"""
    text = url_or_id.strip()
    if not text:
        raise ValueError("链接不能为空")
    if m := _BV_RE.search(text):
        return m.group(1)
    if m := _AV_RE.search(text):
        return m.group(0).lower()
    return text


def page_from_url(url: str) -> int:
    """https://www.bilibili.com/video/BVxxx?p=3 -> 3"""
    m = re.search(r"[?&]p=(\d+)", url)
    return int(m.group(1)) if m else 1
