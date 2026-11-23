"""系列展开：识别一个视频属于「多 P 投稿」还是「UP 主合集」，并列出全部分集。

B站有三种容易混淆的结构：
1. 单 P 视频 —— 一个 BV 对应一个视频
2. 多 P 投稿 —— 一个 BV 下分 P1/P2/P3（同一投稿的多段，URL 加 ?p=N）
3. UP 主合集（ugc_season）—— 多个独立 BV 被组织成一个系列，每集有自己的 BV

收藏夹收藏的是单个视频单元，所以默认只处理你收藏的那一个。
想整段处理就得先展开成系列，这个模块负责这件事。

展开优先级：**多P > 合集章节**。
一个 BV 下拆好的分P才是"这门课本身"；合集可能只是把不同 UP 的课按学科
攒成的大杂烩，直接摊平会跑偏。合集内部再按 section 收窄到当前视频所属那一章。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

from ..core.config import Settings
from ..core.errors import VideoUnavailableError
from .bili_auth import HEADERS, load_cookies

log = logging.getLogger(__name__)

VIEW_API = "https://api.bilibili.com/x/web-interface/view"


@dataclass
class SeriesItem:
    bvid: str
    page: int
    title: str
    duration: float

    @property
    def url(self) -> str:
        base = f"https://www.bilibili.com/video/{self.bvid}"
        return f"{base}?p={self.page}" if self.page > 1 else base

    def to_dict(self) -> dict[str, Any]:
        return {
            "bvid": self.bvid,
            "page": self.page,
            "title": self.title,
            "duration": round(self.duration, 1),
            "url": self.url,
        }


@dataclass
class Series:
    kind: str = ""          # "" | "pages" | "season"
    title: str = ""
    items: list[SeriesItem] = field(default_factory=list)
    sid: str = ""           # B站合集 id（跨导入稳定）；多 P 用投稿 BV 兜底
    season_title: str = ""  # 父合集名（分章节时和 title 不同）
    section_title: str = "" # 当前章节名；只有单章节的合集为空
    sections: list[dict[str, Any]] = field(default_factory=list)  # 全部章节，供选集切换
    self_total: int = 0     # 当前 BV 自己的分P数，供前端在"分P / 合集"之间切换

    @property
    def total(self) -> int:
        return len(self.items)

    @property
    def total_duration(self) -> float:
        return sum(i.duration for i in self.items)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "title": self.title,
            "id": self.sid,
            "season_title": self.season_title,
            "section_title": self.section_title,
            "sections": self.sections,
            "self_total": self.self_total,
            "total": self.total,
            "total_duration": round(self.total_duration, 1),
            "items": [i.to_dict() for i in self.items],
        }


def _view(settings: Settings, bvid: str, timeout: float = 15.0) -> dict[str, Any]:
    try:
        with httpx.Client(headers=HEADERS, cookies=load_cookies(settings.cookies_file), timeout=timeout) as client:
            resp = client.get(VIEW_API, params={"bvid": bvid})
            payload = resp.json()
    except httpx.HTTPError as exc:
        raise VideoUnavailableError(f"查询视频信息失败：{str(exc)[:120]}") from exc

    if payload.get("code") != 0:
        raise VideoUnavailableError(payload.get("message") or "视频不可访问")
    return payload.get("data") or {}


def _items_of(episodes: list[dict[str, Any]]) -> list[SeriesItem]:
    items: list[SeriesItem] = []
    for episode in episodes:
        ep_bvid = episode.get("bvid")
        if not ep_bvid:
            continue
        arc = episode.get("arc") or {}
        items.append(SeriesItem(
            bvid=ep_bvid,
            page=1,
            title=(episode.get("title") or arc.get("title") or "")[:80],
            duration=float(arc.get("duration") or 0),
        ))
    return items


def _section_of(sections: list[dict[str, Any]], bvid: str) -> tuple[int, dict[str, Any] | None]:
    """找出当前视频落在哪个章节里。

    合集可以分章节（B站界面上的子合集，例如「期末冲刺上大分」按学科分成
    数学类 / 大学物理类 / 计算机类…）。整季展开会把 78 集全灌进队列，
    所以只能取当前视频所属的那一章。
    """
    for idx, section in enumerate(sections):
        for episode in section.get("episodes") or []:
            if episode.get("bvid") == bvid:
                return idx, section
    return -1, None


def _sections_meta(sections: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "id": str(s.get("id") or ""),
            "title": s.get("title") or "未命名章节",
            "count": len(s.get("episodes") or []),
        }
        for s in sections
    ]


def resolve(settings: Settings, bvid: str, section_id: str = "", mode: str = "") -> Series:
    """查这个视频属于什么系列；不属于任何系列时 kind 为空。

    section_id：多章节合集里指定要展开哪一章（用户在前端切换章节时传），
    留空则自动取当前视频所在的那一章。
    mode："season" 强制按合集展开（即使这个视频本身是多P）；留空按默认优先级。
    """
    data = _view(settings, bvid)
    pages = data.get("pages") or []
    season = data.get("ugc_season")
    season_title = (season or {}).get("title") or ""
    sections_all = (season or {}).get("sections") or []

    # 多P优先：一个 BV 下的分P才是"这门课本身"。
    # 合集（ugc_season）只是把多个独立投稿组织在一起，常常还是按学科分章节的
    # 大杂烩；收藏其中一集时展开整季会跑偏（10 个分P的线代课会被换成 26 门课）。
    if mode != "season" and len(pages) > 1:
        title = data.get("title") or "多P视频"
        items = [
            SeriesItem(
                bvid=bvid,
                page=int(p.get("page") or 1),
                title=(p.get("part") or f"P{p.get('page')}")[:80],
                duration=float(p.get("duration") or 0),
            )
            for p in pages
        ]
        # 合集信息照样带上：前端据此给一个"看所属合集"的入口，但不占默认视图
        return Series(
            "pages", title, items,
            season_title=season_title,
            sections=_sections_meta(sections_all) if season else [],
            self_total=len(pages),
        )

    # 单P视频再看它属于哪个合集：分章节的只展开当前视频所在的那一章
    if season:
        sections = season.get("sections") or []
        season_title = season.get("title") or "未命名合集"
        explicit = next(
            (s for s in sections if section_id and str(s.get("id") or "") == section_id),
            None,
        )
        if explicit is not None:
            section = explicit
        else:
            _idx, section = _section_of(sections, bvid)

        if section is not None:
            # 多章节合集：只展开一章，章节之间互不牵连。
            items = _items_of(section.get("episodes") or [])
            if len(items) > 1 or (explicit is not None and items):
                section_title = section.get("title") or ""
                multi = len(sections) > 1
                sid = str(season.get("id") or "")
                if multi and section.get("id"):
                    # 章节也进 id：不同学科各自折叠成一张卡，不会混成一季
                    sid = f"{sid}-{section.get('id')}"
                return Series(
                    "season",
                    (section_title or season_title) if multi else season_title,
                    items,
                    sid,
                    season_title=season_title,
                    section_title=section_title if multi else "",
                    sections=_sections_meta(sections) if multi else [],
                    self_total=len(pages),
                )
            # 该章节只有这一集：没有可展开的兄弟集，按单集处理
            return Series("", "", [])

        # 找不到归属（B站偶发缺 episodes）：退回整季
        items = _items_of([ep for s in sections for ep in (s.get("episodes") or [])])
        if len(items) > 1:
            return Series("season", season_title, items, str(season.get("id") or ""),
                          season_title=season_title, self_total=len(pages))

    if len(pages) > 1:
        title = data.get("title") or "多P视频"
        items = [
            SeriesItem(
                bvid=bvid,
                page=int(p.get("page") or 1),
                title=(p.get("part") or f"P{p.get('page')}")[:80],
                duration=float(p.get("duration") or 0),
            )
            for p in pages
        ]
        return Series("pages", title, items, self_total=len(pages))

    return Series("", "", [])


def describe(settings: Settings, bvid: str) -> dict[str, Any]:
    """给前端用的轻量描述（不含全部分集）。"""
    series = resolve(settings, bvid)
    if not series.kind:
        return {"kind": "", "title": "", "total": 1, "total_duration": 0}
    return {
        "kind": series.kind,
        "title": series.title,
        "total": series.total,
        "total_duration": round(series.total_duration, 1),
        "preview": [i.to_dict() for i in series.items[:5]],
    }
