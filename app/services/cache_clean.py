"""缓存清理。

视频和音频是整条链路上体积最大的中间产物：一条 17 分钟的视频
video.mp4 + audio.m4a 就有 70MB 左右，处理完再也没人读它。
但关键帧在导出时已经复制进输出目录的 assets/，所以删缓存
**不会影响已生成的笔记**——这是这里敢默认清理的前提。

三档策略：
    keep  —— 全部保留（要重跑、要排查时用）
    media —— 只删视频/音频（默认，省掉 ~85% 空间，留 json 可复查）
    all   —— 删掉整个任务目录（最省空间）
"""
from __future__ import annotations

import shutil
import threading
import time
from pathlib import Path
from typing import Iterable

POLICIES = ("keep", "media", "all")

# usage() 要逐文件 stat 整个缓存目录。755MB / 35 个任务实测一次 2.8 秒，
# 冻结环境 4~6 秒，而 /api/settings 每次打开设置页都会调它。这个数字只是
# 概览，60 秒内复用即可；真正删过文件后由 invalidate_usage() 主动失效。
_USAGE_TTL = 60.0
_usage_cache: dict[str, tuple[float, dict]] = {}
_usage_lock = threading.Lock()

# yt-dlp 的产物后缀；.part/.ytdl 是没下完的残片，也一并算媒体
_MEDIA_SUFFIXES = {
    ".mp4", ".m4a", ".m4v", ".flv", ".webm", ".mkv", ".mov", ".ts",
    ".mp3", ".wav", ".aac", ".opus", ".ogg", ".flac",
    ".part", ".ytdl", ".temp",
}


def human(size: float) -> str:
    """字节转人类可读。"""
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit in ("B", "KB") else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def dir_size(path: Path) -> int:
    """目录（或文件）占用字节数；不存在返回 0。"""
    if not path.exists():
        return 0
    if path.is_file():
        try:
            return path.stat().st_size
        except OSError:
            return 0
    total = 0
    for p in path.rglob("*"):
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            continue
    return total


def invalidate_usage() -> None:
    """缓存目录被改动后调用，下次 usage() 重新统计。"""
    with _usage_lock:
        _usage_cache.clear()


def usage(cache_dir: Path, *, force: bool = False) -> dict:
    """统计缓存总占用与任务数（60 秒内复用，见模块头注释）。"""
    key = str(cache_dir)
    now = time.monotonic()
    with _usage_lock:
        hit = _usage_cache.get(key)
        if not force and hit and now - hit[0] < _USAGE_TTL:
            return dict(hit[1])
    if not cache_dir.is_dir():
        result = {"bytes": 0, "human": "0 B", "tasks": 0}
    else:
        tasks = [d for d in cache_dir.iterdir() if d.is_dir()]
        total = sum(dir_size(d) for d in tasks)
        result = {"bytes": total, "human": human(total), "tasks": len(tasks)}
    with _usage_lock:
        _usage_cache[key] = (now, result)
    return dict(result)


def _remove_media(work: Path) -> int:
    freed = 0
    for p in work.rglob("*"):
        if not p.is_file():
            continue
        if p.suffix.lower() in _MEDIA_SUFFIXES:
            try:
                size = p.stat().st_size
                p.unlink()
                freed += size
            except OSError:
                pass
    return freed


def clean_task(work: Path, policy: str = "media") -> int:
    """按策略清理单个任务目录，返回释放的字节数。

    失败不抛异常：清理属于收尾动作，删不掉只是没省到空间，
    不该让一条已经跑完的任务因此变成"失败"。
    """
    if policy == "keep" or not work.is_dir():
        return 0
    if policy == "all":
        freed = dir_size(work)
        shutil.rmtree(work, ignore_errors=True)
        invalidate_usage()
        return freed
    freed = _remove_media(work)
    if freed:
        invalidate_usage()
    return freed


def clean_all(
    cache_dir: Path,
    policy: str = "all",
    keep_ids: Iterable[str] = (),
) -> dict:
    """清理整个缓存目录（跳过正在跑的任务）。"""
    if not cache_dir.is_dir():
        return {"freed": 0, "human": "0 B", "removed": 0}
    skip = set(keep_ids)
    freed = 0
    removed = 0
    for d in sorted(cache_dir.iterdir()):
        if not d.is_dir() or d.name in skip:
            continue
        got = clean_task(d, "all" if policy == "all" else "media")
        if got:
            freed += got
            removed += 1
    return {"freed": freed, "human": human(freed), "removed": removed}
