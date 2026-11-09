"""ffmpeg/ffprobe 子进程封装。

打包后二进制随 app 分发在 bin/ 下，查找顺序：显式配置 > 内置 bin > PATH。
所有调用隐藏控制台窗口（否则 exe 运行时每调一次 ffmpeg 闪一个黑框）。
"""
from __future__ import annotations

import logging
import re
import shutil
import subprocess
import sys
from pathlib import Path

from .errors import DependencyMissingError, FFmpegError
from .paths import bundle_root, user_data_dir

log = logging.getLogger(__name__)

_PTS_RE = re.compile(r"pts_time:\s*([0-9.]+)")
_CREATE_NO_WINDOW = 0x08000000


def _no_window_flags() -> int:
    return _CREATE_NO_WINDOW if sys.platform == "win32" else 0


def find_binary(name: str, configured: str = "") -> str:
    """查找顺序：显式配置 > 随包 bin > 用户目录 bin（按需下载） > PATH。"""
    if configured and Path(configured).is_file():
        return configured
    for root in (bundle_root(), user_data_dir()):
        for candidate in (root / "bin" / f"{name}.exe", root / "bin" / name):
            if candidate.is_file():
                return str(candidate)
    if found := shutil.which(name):
        return found
    raise DependencyMissingError(
        f"未找到 {name}，请安装 ffmpeg 或把可执行文件放到程序目录的 bin/ 下",
        detail={"binary": name},
    )


def run(args: list[str], timeout: float = 900, check: bool = True) -> subprocess.CompletedProcess:
    """执行命令，返回 CompletedProcess；非零退出码按需抛 FFmpegError。"""
    try:
        proc = subprocess.run(
            args,
            capture_output=True,
            timeout=timeout,
            creationflags=_no_window_flags(),
        )
    except subprocess.TimeoutExpired as exc:
        raise FFmpegError(f"命令超时（{timeout:.0f}s）", detail={"cmd": args[0]}) from exc
    except FileNotFoundError as exc:
        raise DependencyMissingError(f"无法执行 {args[0]}") from exc

    if check and proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", "replace")[-800:]
        raise FFmpegError(
            f"{Path(args[0]).name} 执行失败（退出码 {proc.returncode}）",
            detail={"stderr": stderr},
        )
    return proc


def probe_duration(path: str) -> float:
    exe = find_binary("ffprobe")
    proc = run(
        [
            exe, "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", str(path),
        ],
        timeout=60,
    )
    raw = proc.stdout.decode("utf-8", "replace").strip()
    try:
        return float(raw)
    except ValueError:
        return 0.0


def extract_audio(video: str, out_wav: str, sample_rate: int = 16000) -> str:
    """抽 16k 单声道 wav —— whisper 的原生输入格式，避免它内部再转一次。"""
    exe = find_binary("ffmpeg")
    Path(out_wav).parent.mkdir(parents=True, exist_ok=True)
    run([
        exe, "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(video), "-vn", "-ac", "1", "-ar", str(sample_rate),
        "-c:a", "pcm_s16le", str(out_wav),
    ])
    if not Path(out_wav).is_file() or Path(out_wav).stat().st_size == 0:
        raise FFmpegError("音频抽取结果为空")
    return out_wav


def extract_thumbnails(
    video: str,
    out_dir: str,
    fps: float = 1.0,
    width: int = 96,
) -> list[tuple[float, Path]]:
    """抽取低分辨率缩略图序列，返回 [(秒, 路径)]。

    为什么不直接用 ffmpeg 的 scene 分数：select 的 scene 变量与 scdet 的 score
    是两套度量，且 scdet 基于灰度百分比 —— 白底 PPT 换一页文字只有百分之几的变化，
    阈值调低就全是噪声，调高就一个都检不到。自己算感知哈希帧差可控得多。
    """
    exe = find_binary("ffmpeg")
    dir_path = Path(out_dir)
    dir_path.mkdir(parents=True, exist_ok=True)
    for old in dir_path.glob("thumb_*.jpg"):
        old.unlink(missing_ok=True)

    run(
        [
            exe, "-hide_banner", "-y", "-loglevel", "error", "-i", str(video),
            "-an", "-vf", f"fps={fps},scale={width}:-2", "-q:v", "6",
            str(dir_path / "thumb_%06d.jpg"),
        ],
        timeout=3600,
        check=False,
    )
    files = sorted(dir_path.glob("thumb_*.jpg"))
    if not files:
        log.warning("缩略图抽取失败，画面分析将退化为纯均匀抽帧")
    return [(i / fps, f) for i, f in enumerate(files)]


BILI_HEADERS = (
    "Referer: https://www.bilibili.com/\r\n"
    "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36\r\n"
)


def extract_frames_from_url(
    url: str,
    out_dir: str,
    timestamps: list[float],
    max_edge: int = 1920,
    headers: str = BILI_HEADERS,
) -> list[tuple[float, Path]]:
    """对远程视频流做定点 seek 抽帧 —— 只拉目标时间附近的字节。

    这是"高清出图"的关键：不必下载整片，就能拿到原画质的帧。
    实测 1080p 单帧约 0.5-0.8 秒、200-300KB。
    """
    exe = find_binary("ffmpeg")
    dir_path = Path(out_dir)
    dir_path.mkdir(parents=True, exist_ok=True)
    scale = f"scale='if(gt(iw,{max_edge}),{max_edge},iw)':-2"

    results: list[tuple[float, Path]] = []
    for ts in timestamps:
        target = dir_path / f"hd_{int(ts * 1000):010d}.jpg"
        if target.is_file() and target.stat().st_size > 0:
            results.append((ts, target))
            continue
        proc = run(
            [
                exe, "-hide_banner", "-y", "-loglevel", "error",
                "-ss", f"{ts:.3f}", "-headers", headers, "-i", str(url),
                "-frames:v", "1", "-vf", scale, "-q:v", "2", str(target),
            ],
            timeout=60,
            check=False,
        )
        if proc.returncode == 0 and target.is_file() and target.stat().st_size > 0:
            results.append((ts, target))
        else:
            log.debug("高清抽帧失败 ts=%.2f", ts)
    return results


def extract_frames_at(
    video: str,
    out_dir: str,
    timestamps: list[float],
    max_edge: int = 1280,
) -> list[tuple[float, Path]]:
    """按时间点精确抽帧（均匀兜底 / 讲解峰值补帧用）。"""
    exe = find_binary("ffmpeg")
    dir_path = Path(out_dir)
    dir_path.mkdir(parents=True, exist_ok=True)
    scale = f"scale='if(gt(iw,{max_edge}),{max_edge},iw)':-2"

    results: list[tuple[float, Path]] = []
    for ts in timestamps:
        target = dir_path / f"at_{int(ts * 1000):010d}.jpg"
        if target.is_file() and target.stat().st_size > 0:
            results.append((ts, target))
            continue
        proc = run(
            [
                exe, "-hide_banner", "-y", "-loglevel", "error",
                "-ss", f"{ts:.3f}", "-i", str(video),
                "-frames:v", "1", "-vf", scale, "-q:v", "3", str(target),
            ],
            timeout=120,
            check=False,
        )
        if proc.returncode == 0 and target.is_file() and target.stat().st_size > 0:
            results.append((ts, target))
    return results
