"""收藏夹：拉取用户的收藏夹与夹内视频，支撑批量导入。

这些接口需要登录态 cookie（SESSDATA），未登录直接给明确提示而不是空列表。
"""
from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from ..core.config import Settings
from ..core.errors import AuthRequiredError, UpstreamError
from .bili_auth import HEADERS, load_cookies

log = logging.getLogger(__name__)

FOLDER_ALL_API = "https://api.bilibili.com/x/v3/fav/folder/created/list-all"
FOLDER_ITEMS_API = "https://api.bilibili.com/x/v3/fav/resource/list"
FAVORITE_COUNT_API = "https://api.bilibili.com/x/v3/fav/folder/navnum"

PAGE_SIZE = 20


def _client(settings: Settings, timeout: float = 20.0) -> httpx.Client:
    cookies = load_cookies(settings.cookies_file)
    if not cookies.get("SESSDATA"):
        raise AuthRequiredError("需要先扫码登录 B站才能读取收藏夹")
    return httpx.Client(headers=HEADERS, cookies=cookies, timeout=timeout, follow_redirects=True)


def list_folders(settings: Settings, mid: int) -> list[dict[str, Any]]:
    """返回收藏夹列表（含默认收藏夹）。"""
    if not mid:
        raise AuthRequiredError("缺少用户 ID，请重新登录")

    try:
        with _client(settings) as client:
            resp = client.get(FOLDER_ALL_API, params={"up_mid": mid})
            payload = resp.json()
    except httpx.HTTPError as exc:
        raise UpstreamError(f"读取收藏夹失败：{str(exc)[:160]}") from exc

    if payload.get("code") != 0:
        raise UpstreamError(f"读取收藏夹失败：{payload.get('message')}")

    raw = ((payload.get("data") or {}).get("list")) or []
    folders = []
    for item in raw:
        folders.append({
            "media_id": int(item.get("id") or 0),
            "title": item.get("title") or "未命名收藏夹",
            "count": int(item.get("media_count") or 0),
            "is_default": bool(int(item.get("attr") or 0) & 1),
        })
    folders.sort(key=lambda f: (not f["is_default"], -f["count"]))
    return folders


def list_items(
    settings: Settings,
    media_id: int,
    page: int = 1,
    page_size: int = PAGE_SIZE,
) -> dict[str, Any]:
    """分页拉取收藏夹内的视频。"""
    params = {
        "media_id": media_id,
        "pn": page,
        "ps": page_size,
        "keyword": "",
        "order": "mtime",
        "type": 0,
        "tid": 0,
        "platform": "web",
    }
    try:
        with _client(settings) as client:
            resp = client.get(FOLDER_ITEMS_API, params=params)
            payload = resp.json()
    except httpx.HTTPError as exc:
        raise UpstreamError(f"读取收藏内容失败：{str(exc)[:160]}") from exc

    if payload.get("code") != 0:
        raise UpstreamError(f"读取收藏内容失败：{payload.get('message')}")

    data = payload.get("data") or {}
    medias = data.get("medias") or []
    info = data.get("info") or {}

    items = []
    for media in medias:
        bvid = media.get("bvid") or ""
        if not bvid:
            continue  # 已失效视频没有 bvid
        cover = media.get("cover") or ""
        if cover.startswith("//"):
            cover = "https:" + cover
        upper = media.get("upper") or {}
        items.append({
            "bvid": bvid,
            "title": media.get("title") or "未命名视频",
            "cover": cover,
            "duration": int(media.get("duration") or 0),
            "uploader": upper.get("name") or "",
            "pubtime": int(media.get("pubtime") or 0),
            "intro": (media.get("intro") or "")[:120],
        })

    return {
        "items": items,
        "page": page,
        "page_size": page_size,
        "total": int(info.get("media_count") or 0),
        "has_more": bool(data.get("has_more")),
    }


def fetch_all_bvids(
    settings: Settings,
    media_id: int,
    max_items: int = 500,
    on_page: Any = None,
) -> list[dict[str, Any]]:
    """把整个收藏夹翻完，返回全部条目（带简单去重与限速）。"""
    collected: dict[str, dict[str, Any]] = {}
    page = 1
    seen_pages = 0

    while len(collected) < max_items:
        batch = list_items(settings, media_id, page=page)
        items = batch["items"]
        if not items:
            break
        for item in items:
            collected.setdefault(item["bvid"], item)
        if on_page:
            on_page(len(collected), batch.get("total") or 0)
        if not batch["has_more"]:
            break
        page += 1
        seen_pages += 1
        if seen_pages > 100:  # 防御：避免接口异常导致无限翻页
            log.warning("翻页超过 100 页，提前终止")
            break
        time.sleep(0.4)  # 限速，别把接口打急

    return list(collected.values())[:max_items]
