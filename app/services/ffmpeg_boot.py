"""ffmpeg 按需获取。

打包时不带 ffmpeg：一个完整构建的 ffmpeg.exe 就有 220MB，加上 ffprobe 450MB，
塞进安装包会让体积翻几倍，而很多机器（装过剪映、OBS、录屏工具）本来就有。
所以改成"要用到时再下"，下到 %APPDATA%\\BiliNotes\\bin 后一直复用。

下载源用 gyan.dev 的 essentials 构建（92MB zip），GPL 协议、静态链接、
解压即用，不需要额外 DLL。
"""
from __future__ import annotations

import logging
import os
import subprocess
import time
import zipfile
from pathlib import Path
from typing import Callable

from ..core.errors import DependencyMissingError
from ..core.http import stream_download
from ..core.paths import bundle_root, user_data_dir

log = logging.getLogger(__name__)

FFMPEG_URL = os.environ.get(
    "BILINOTES_FFMPEG_URL",
    "https://github.com/GyanD/codexffmpeg/releases/download/7.1/ffmpeg-7.1-essentials_build.zip",
)
# zip 里两个 exe 加起来约 180MB，用于进度估算
_EXPECTED_MB = 92.0
_WANTED = ("ffmpeg.exe", "ffprobe.exe")

ProgressFn = Callable[[float, str], None]
CancelFn = Callable[[], bool]


def bin_dir() -> Path:
    path = user_data_dir() / "bin"
    path.mkdir(parents=True, exist_ok=True)
    return path


def status() -> dict:
    """报告 ffmpeg / ffprobe 是否可用，以及来源。"""
    from ..core.ffmpeg import find_binary  # noqa: PLC0415

    out: dict = {"ready": False, "ffmpeg": None, "ffprobe": None, "source": None}
    try:
        out["ffmpeg"] = find_binary("ffmpeg")
        out["ffprobe"] = find_binary("ffprobe")
    except DependencyMissingError:
        return out
    out["ready"] = True
    # 用真实目录判断来源：只看路径里有没有 AppData 会把 winget
    # 装的 ffmpeg（也在 AppData 下）误判成"我们下载的"
    parent = Path(out["ffmpeg"]).resolve().parent
    if parent == bin_dir().resolve():
        out["source"] = "downloaded"
    elif parent == (bundle_root() / "bin").resolve():
        out["source"] = "bundled"
    else:
        out["source"] = "system"
    return out


def install(
    on_progress: ProgressFn | None = None,
    cancel: CancelFn | None = None,
) -> dict:
    """下载 ffmpeg/ffprobe 到用户目录，返回安装后的状态。"""
    if status()["ready"]:
        if on_progress:
            on_progress(100.0, "ffmpeg 已就绪")
        return status()

    target = bin_dir()
    archive = user_data_dir() / "cache" / "ffmpeg.zip"
    started = time.time()

    def emit(pct: float, msg: str) -> None:
        if on_progress:
            on_progress(pct, msg)

    if not archive.is_file() or archive.stat().st_size < 1024:
        emit(0.0, f"正在下载 ffmpeg（约 {_EXPECTED_MB:.0f}MB）…")
        last = [0.0]

        def report(chunk: int) -> None:
            now = time.time()
            if now - last[0] < 0.4:
                return
            last[0] = now
            mb = chunk / 1048576
            emit(min(90.0, mb / _EXPECTED_MB * 90.0), f"下载 ffmpeg {mb:.0f}/{_EXPECTED_MB:.0f}MB")

        try:
            stream_download(FFMPEG_URL, archive, on_chunk=report, cancel=cancel, timeout=180.0)
        except Exception as exc:
            raise DependencyMissingError(
                f"ffmpeg 下载失败：{str(exc)[:120]}。"
                "可重试（支持断点续传），或手动安装 ffmpeg 后重启程序。",
                detail={"url": FFMPEG_URL, "cache": str(archive)},
            ) from exc

    emit(92.0, "解压 ffmpeg…")
    extracted = _extract(archive, target)
    if not extracted:
        archive.unlink(missing_ok=True)   # 包坏了，下次重下
        raise DependencyMissingError(
            "ffmpeg 压缩包内容异常，已删除缓存，请重试",
            detail={"archive": str(archive)},
        )

    emit(98.0, "校验 ffmpeg…")
    _verify(target)
    log.info("ffmpeg 安装完成，用时 %.1fs -> %s", time.time() - started, target)
    emit(100.0, "ffmpeg 就绪")
    return status()


def _extract(archive: Path, target: Path) -> list[str]:
    """从 zip 里只取两个 exe —— 包里还有文档和 LICENSE，不必落盘。"""
    found: list[str] = []
    with zipfile.ZipFile(archive) as zf:
        for name in zf.namelist():
            base = name.rsplit("/", 1)[-1].lower()
            if base not in _WANTED:
                continue
            dest = target / base
            with zf.open(name) as src, dest.open("wb") as out:
                while chunk := src.read(1 << 20):
                    out.write(chunk)
            found.append(base)
    return found


def _verify(target: Path) -> None:
    """解压完跑一次 -version：zip 损坏 / 缺 DLL 会在这里暴露。"""
    for name in _WANTED:
        exe = target / name
        if not exe.is_file():
            raise DependencyMissingError(f"解压后缺少 {name}")
        proc = subprocess.run(
            [str(exe), "-version"],
            capture_output=True,
            timeout=60,
            creationflags=0x08000000 if os.name == "nt" else 0,
        )
        if proc.returncode != 0:
            raise DependencyMissingError(
                f"{name} 无法运行（可能缺少运行库）："
                f"{proc.stderr.decode('utf-8', 'replace')[-120:]}"
            )
