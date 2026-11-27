"""本地视频：地址解析、元数据探测、抽音频、读同名字幕。

只服务「本机文件 → 笔记」这一条路：B 站链接仍走 downloader.py，
两边的代码互不调用，避免本地分支出问题时影响原有链路。

约定：
- 任务 url 用 `file://` 地址（`Path.as_uri()`），中文/空格都会被百分号编码，
  既能被任务队列当普通字符串去重，也不会因为路径里有空格而解析错。
- 这里只做纯函数/纯子进程调用，不改全局状态；出错抛业务异常，
  由任务状态机照常收尾成「失败」。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

from ..core.errors import FFmpegError, VideoUnavailableError
from ..core.ffmpeg import find_binary, probe_duration, run
from ..core.models import Segment, VideoMeta
from . import subtitle

log = logging.getLogger(__name__)

LOCAL_PREFIX = "file://"

# 只收常见容器。音频文件先不收：没有画面，笔记质量会明显打折，不如明确拒绝。
VIDEO_EXTS = {
    ".mp4", ".mkv", ".mov", ".flv", ".webm", ".avi",
    ".ts", ".m4v", ".wmv", ".mpg", ".mpeg", ".rmvb",
}
SUB_EXTS = (".srt", ".vtt", ".ass", ".ssa")


def is_local_url(url: str) -> bool:
    return str(url or "").strip().lower().startswith(LOCAL_PREFIX)


def to_url(path: Path | str) -> str:
    """绝对路径 → file:// 地址。"""
    return Path(path).expanduser().resolve().as_uri()


def from_url(url: str) -> Path:
    """file:// 地址 → 本地路径（Windows 盘符、UNC 都能还原）。"""
    parsed = urlparse(str(url).strip())
    if parsed.scheme.lower() != "file":
        raise VideoUnavailableError("不是本地文件地址")
    raw = url2pathname(parsed.path)
    if parsed.netloc:                      # file://server/share/a.mp4
        raw = f"//{parsed.netloc}{raw}"
    return Path(unquote(raw))


def validate_video(path_text: str) -> Path:
    """把用户给的路径收紧成可处理的视频文件；不合格直接给人话错误。"""
    text = str(path_text or "").strip().strip('"').strip("'")
    if not text:
        raise VideoUnavailableError("请先选择视频文件")
    p = Path(text).expanduser()
    if not p.is_absolute():
        raise VideoUnavailableError("请填写完整路径（例如 D:\\视频\\第01讲.mp4）")
    try:
        p = p.resolve()
    except OSError as exc:
        raise VideoUnavailableError(f"路径无法解析：{exc}") from exc
    if not p.exists():
        raise VideoUnavailableError("文件不存在，可能被移动或改名了")
    if not p.is_file():
        raise VideoUnavailableError("这是一个文件夹，请选择具体视频文件")
    if p.suffix.lower() not in VIDEO_EXTS:
        raise VideoUnavailableError(
            f"暂不支持 {p.suffix or '该'} 格式，支持 mp4 / mkv / mov / flv / webm 等常见视频")
    try:
        if p.stat().st_size <= 0:
            raise VideoUnavailableError("文件是空的（0 字节）")
    except OSError as exc:
        raise VideoUnavailableError(f"读不到文件信息：{exc}") from exc
    return p


def probe(path: Path) -> tuple[VideoMeta, dict]:
    """读本地视频的基本信息。duration 用现成的 ffprobe 封装。"""
    if not path.is_file():
        raise VideoUnavailableError("文件已不存在，可能被移动或删除了")
    try:
        duration = float(probe_duration(str(path)))
    except FFmpegError as exc:
        raise VideoUnavailableError("读不出视频时长，文件可能损坏或不是视频") from exc
    if duration <= 0:
        raise VideoUnavailableError("这个文件的时长是 0，无法处理")

    meta = VideoMeta(
        bvid="",
        cid=0,
        title=path.stem or "未命名视频",
        uploader="",                        # 本地文件没有 UP 主，别硬塞一个假名字
        duration=duration,
        pubdate=0,
        cover="",
        webpage_url="",                     # 空的来源地址 = 导出时不生成原视频链接
        description="",
    )
    meta.local_path = str(path)
    return meta, {"local_path": str(path)}


def extract_audio(video: Path, workdir: Path) -> Path:
    """从视频里抽一条 16k 单声道 wav 给转写用。

    先解码成 PCM 而不是复制音轨：容器里的音轨可能是任意编码，
    统一成 wav 后 faster-whisper 读起来最稳，也不会因为容器怪异而失败。
    """
    exe = find_binary("ffmpeg")
    workdir.mkdir(parents=True, exist_ok=True)
    out = workdir / "audio.wav"
    proc = run(
        [
            exe, "-hide_banner", "-y", "-loglevel", "error",
            "-i", str(video),
            "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le",
            str(out),
        ],
        timeout=3600,                 # 两三小时的课程也够
        check=False,
    )
    if proc.returncode != 0 or not out.is_file() or out.stat().st_size <= 44:
        stderr = proc.stderr.decode("utf-8", "replace").strip()[-300:]
        raise FFmpegError(
            "从视频里提取音轨失败（文件可能没有音轨）",
            detail={"stderr": stderr},
        )
    return out


# ---------------------------------------------------------------- 同名字幕
def _read_text(path: Path) -> str:
    """字幕文件编码很杂：UTF-8 BOM / GBK / BIG5 都见过，逐个试。"""
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "utf-8", "gb18030", "big5", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def _vtt_to_srt_text(text: str) -> str:
    """VTT 和 SRT 只差一点：头部 WEBVTT、时间戳用点、行尾可能带 cue 设置。"""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: list[str] = []
    for line in lines:
        if line.strip().upper() == "WEBVTT":
            continue
        if "-->" in line:
            head, _, tail = line.partition("-->")
            end = tail.strip().split(" ")[0]        # 去掉 align:start position:0%
            out.append(f"{head.strip().replace('.', ',')} --> {end.replace('.', ',')}")
            continue
        out.append(line)
    return "\n".join(out)


def _ass_to_srt(ass: Path, workdir: Path) -> Path:
    """ass/ssa 交给 ffmpeg 转 srt，省得自己写解析器。"""
    exe = find_binary("ffmpeg")
    workdir.mkdir(parents=True, exist_ok=True)
    out = workdir / f"{ass.stem}.converted.srt"
    proc = run(
        [exe, "-hide_banner", "-y", "-loglevel", "error", "-i", str(ass), str(out)],
        timeout=120, check=False,
    )
    if proc.returncode != 0 or not out.is_file():
        raise FFmpegError("字幕转换失败（ass/ssa → srt）")
    return out


def find_subtitle(video: Path) -> Path | None:
    """找同名/同前缀的字幕文件：第01讲.srt、第01讲.zh.srt、第01讲.chs.vtt 都算。"""
    parent, stem = video.parent, video.stem
    # 1. 完全同名（只换扩展名）
    for ext in SUB_EXTS:
        candidate = parent / f"{stem}{ext}"
        if candidate.is_file():
            return candidate
        candidate = parent / f"{stem}{ext.upper()}"
        if candidate.is_file():
            return candidate
    # 2. 带语言后缀：第01讲.zh.srt / 第01讲.en.vtt
    try:
        matches = sorted(
            p for p in parent.iterdir()
            if p.is_file() and p.stem.startswith(stem + ".") and p.suffix.lower() in SUB_EXTS
        )
    except OSError:
        return None
    return matches[0] if matches else None


def load_subtitle(video: Path, workdir: Path) -> tuple[list[Segment], str] | None:
    """读同名字幕；没有或解析不出内容就返回 None，交给语音转写。

    返回 (segments, 来源标签)。标签会写进笔记的 transcript 字段，
    导出时映射成「本地字幕文件」。
    """
    found = find_subtitle(video)
    if not found:
        return None
    try:
        path = found
        if path.suffix.lower() in (".ass", ".ssa"):
            path = _ass_to_srt(path, workdir)
        text = _read_text(path)
        if path.suffix.lower() == ".vtt":
            text = _vtt_to_srt_text(text)
        segments = subtitle.parse_srt(text)
    except (OSError, FFmpegError) as exc:
        log.warning("读取同名字幕失败 %s：%s", found, exc)
        return None
    if not segments:
        log.warning("同名字幕没有解析出内容：%s", found)
        return None
    for seg in segments:
        seg.source = "srt"
    log.info("使用本地字幕 %s（%d 句）", found.name, len(segments))
    return subtitle.normalize(segments), "srt"
