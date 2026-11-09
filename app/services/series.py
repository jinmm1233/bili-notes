"""系列展开：识别一个视频属于「多 P 投稿」还是「UP 主合集」，并列出全部分集。

B站有三种容易混淆的结构：
1. 单 P 视频 —— 一个 BV 对应一个视频
2. 多 P 投稿 —— 一个 BV 下分 P1/P2/P3（同一投稿的多段，URL 加 ?p=N）
3. UP 主合集（ugc_season）—— 多个独立 BV 被组织成一个系列，每集有自己的 BV

收藏夹收藏的是单个视频单元，所以默认只处理你收藏的那一个。
想整段处理就得先展开成系列，这个模块负责这件事。
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


def resolve(settings: Settings, bvid: str) -> Series:
    """查这个视频属于什么系列；不属于任何系列时 kind 为空。"""
    data = _view(settings, bvid)
    pages = data.get("pages") or []
    season = data.get("ugc_season")

    # 合集优先：合集里每集都是独立 BV，比"多P"更符合用户的"整段看完"预期
    if season:
        items: list[SeriesItem] = []
        for section in season.get("sections") or []:
            for episode in section.get("episodes") or []:
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
        if len(items) > 1:
            return Series("season", season.get("title") or "未命名合集", items,
                          str(season.get("id") or ""))

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
        return Series("pages", title, items)

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
