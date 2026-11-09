"""下载层：元数据探测、字幕清单、音视频轨下载（yt-dlp 封装）。"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable

from ..core.config import Settings
from ..core.errors import DependencyMissingError, VideoUnavailableError
from ..core.models import VideoMeta, parse_video_id, page_from_url

log = logging.getLogger(__name__)

ProgressFn = Callable[[float, str], None]


def _yt_dlp():
    try:
        import yt_dlp  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover
        raise DependencyMissingError("缺少 yt-dlp，无法解析视频链接") from exc
    return yt_dlp


def normalize_url(url: str) -> str:
    """支持纯 BV 号、av 号、完整链接；保留 ?p= 分P 参数。"""
    text = url.strip()
    if text.startswith(("http://", "https://")):
        return text
    vid = parse_video_id(text)
    base = f"https://www.bilibili.com/video/{vid}"
    page = page_from_url(text)
    return f"{base}?p={page}" if page > 1 else base


def _base_opts(settings: Settings) -> dict[str, Any]:
    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "skip_download": True,
        "retries": 3,
        "fragment_retries": 3,
        "socket_timeout": 30,
        "extractor_retries": 2,
    }
    if settings.cookies_file.is_file():
        opts["cookiefile"] = str(settings.cookies_file)
    return opts


def _pick_page(info: dict[str, Any], page: int) -> dict[str, Any]:
    if info.get("_type") != "playlist":
        return info
    entries = [e for e in (info.get("entries") or []) if e]
    if not entries:
        raise VideoUnavailableError("该视频没有可用的分P")
    idx = min(max(page - 1, 0), len(entries) - 1)
    return entries[idx]


def _to_meta(info: dict[str, Any], page: int) -> VideoMeta:
    bvid = info.get("id") or ""
    title = info.get("title") or "未命名视频"
    page_title = ""
    if page > 1 and info.get("title"):
        page_title = title
    return VideoMeta(
        bvid=bvid,
        cid=int(info.get("cid") or 0),
        title=title,
        uploader=info.get("uploader") or info.get("channel") or "",
        duration=float(info.get("duration") or 0),
        pubdate=int(info.get("timestamp") or 0),
        cover=info.get("thumbnail") or "",
        webpage_url=info.get("webpage_url") or "",
        page_index=page,
        page_title=page_title,
        part_count=int(info.get("n_entries") or 1),
        description=(info.get("description") or "")[:2000],
    )


def probe(settings: Settings, url: str) -> tuple[VideoMeta, dict[str, Any]]:
    """只取元数据与字幕清单，不下载任何媒体。"""
    yt_dlp = _yt_dlp()
    page = page_from_url(url)
    target = normalize_url(url)
    try:
        with yt_dlp.YoutubeDL(_base_opts(settings)) as ydl:
            info = ydl.extract_info(target, download=False)
    except Exception as exc:  # yt-dlp 的异常类型较杂，统一转成业务错误
        msg = str(exc)
        if "Private" in msg or "无法" in msg or "404" in msg:
            raise VideoUnavailableError("视频不可访问（可能已删除、仅粉丝可见或需要登录）") from exc
        raise VideoUnavailableError(f"解析视频失败：{msg[:200]}") from exc

    if not info:
        raise VideoUnavailableError("解析结果为空")

    total_parts = 1
    if info.get("_type") == "playlist":
        total_parts = len([e for e in (info.get("entries") or []) if e])
    info = _pick_page(info, page)
    meta = _to_meta(info, page)
    if total_parts > 1:
        meta.part_count = total_parts
    return meta, info


def pick_stream_url(info: dict[str, Any], prefer_height: int = 1080) -> str:
    """从探测结果里挑一路尽量高清的视频直链（不下载）。"""
    candidates = [
        f for f in (info.get("formats") or [])
        if f.get("vcodec") not in (None, "none") and f.get("url")
    ]
    if not candidates:
        return ""

    within = [f for f in candidates if (f.get("height") or 0) <= prefer_height]
    pool = within or candidates
    best = max(pool, key=lambda f: ((f.get("height") or 0), f.get("tbr") or 0))
    return str(best.get("url") or "")


def download_media(
    settings: Settings,
    url: str,
    workdir: Path,
    kind: str = "video",
    max_height: int = 480,
    on_progress: ProgressFn | None = None,
) -> Path:
    """下载视频轨或音频轨。视频轨默认限 480p —— 抽帧够用，体积可控。"""
    yt_dlp = _yt_dlp()
    workdir.mkdir(parents=True, exist_ok=True)
    opts = _base_opts(settings)
    opts.update({
        "skip_download": False,
        "outtmpl": str(workdir / f"{kind}.%(ext)s"),
        "overwrites": False,
        "format": (
            f"bv*[height<={max_height}]/bv*/b[height<={max_height}]/b"
            if kind == "video"
            else "ba/b"
        ),
    })

    def _hook(d: dict[str, Any]) -> None:
        if on_progress is None:
            return
        total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
        got = d.get("downloaded_bytes") or 0
        pct = (got / total * 100) if total else 0.0
        on_progress(round(pct, 1), f"下载{'视频' if kind == 'video' else '音频'} {pct:.0f}%")

    opts["progress_hooks"] = [_hook]

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(normalize_url(url), download=True)
    except Exception as exc:
        raise VideoUnavailableError(f"下载失败：{str(exc)[:200]}") from exc

    info = _pick_page(info, page_from_url(url))
    path = _resolve_downloaded(info, workdir, kind)
    if path is None:
        raise VideoUnavailableError(f"未能定位下载得到的{kind}文件")
    return path


def _resolve_downloaded(info: dict[str, Any], workdir: Path, kind: str) -> Path | None:
    for entry in info.get("requested_downloads") or []:
        candidate = entry.get("filepath") or entry.get("_filename")
        if candidate and Path(candidate).is_file():
            return Path(candidate)
    existing = sorted(workdir.glob(f"{kind}.*"), key=lambda p: p.stat().st_size, reverse=True)
    return existing[0] if existing else None
