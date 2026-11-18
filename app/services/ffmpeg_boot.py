"""ffmpeg 按需获取。

打包时不带 ffmpeg：一个完整构建的 ffmpeg.exe 就有 220MB，加上 ffprobe 450MB，
塞进安装包会让体积翻几倍，而很多机器（装过剪映、OBS、录屏工具）本来就有。
所以改成"要用到时再下"，下到用户数据目录的 bin/ 后一直复用。

分平台：
  Windows —— gyan.dev essentials 的单个 zip，里面是 ffmpeg.exe + ffprobe.exe（GPL）。
  macOS   —— osxexperts.net 的静态构建，ffmpeg 和 ffprobe 是两个独立 zip，
             里面各是一个无后缀可执行文件（需 chmod +x）。按 CPU 架构选包：
             Apple Silicon 用 arm 版，Intel 用 intel 版。
两个源都是 GPL 静态构建，解压即用，不需要额外 DLL/dylib。
"""
from __future__ import annotations

import logging
import os
import platform
import subprocess
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ..core.errors import DependencyMissingError
from ..core.http import stream_download
from ..core.paths import bundle_root, user_data_dir

log = logging.getLogger(__name__)

IS_WINDOWS = platform.system() == "Windows"
IS_MAC = platform.system() == "Darwin"

# Windows：单 zip，两个 .exe
_WIN_URL = os.environ.get(
    "BILINOTES_FFMPEG_URL",
    "https://github.com/GyanD/codexffmpeg/releases/download/7.1/ffmpeg-7.1-essentials_build.zip",
)
_WIN_EXPECTED_MB = 92.0

# macOS：按架构选包。osxexperts 的 ffmpeg / ffprobe 是两个独立 zip。
# 文件名带版本号（902 = 9.0.2，80 = 8.0），上游更新时要同步这里。
_MAC_ARM = {
    "ffmpeg": "https://www.osxexperts.net/ffmpeg902arm.zip",
    "ffprobe": "https://www.osxexperts.net/ffprobe902arm.zip",
}
_MAC_INTEL = {
    "ffmpeg": "https://www.osxexperts.net/ffmpeg80intel.zip",
    "ffprobe": "https://www.osxexperts.net/ffprobe80intel.zip",
}
_MAC_EXPECTED_MB = 28.0        # 每个 zip 约 26~28MB，用于进度估算

ProgressFn = Callable[[float, str], None]
CancelFn = Callable[[], bool]


@dataclass(frozen=True)
class _Fetch:
    """一个要下载的归档：url、缓存文件名、期望体积、要提取的条目名集合。"""

    url: str
    cache: str
    expected_mb: float
    names: tuple[str, ...]     # 要从 zip 提取的文件名（Windows 一个包含两个 exe）


def _mac_arch() -> str:
    """arm64 -> arm，其余（x86_64）-> intel。环境变量可强制覆盖，便于测试。"""
    override = os.environ.get("BILINOTES_FFMPEG_ARCH", "").strip().lower()
    if override in ("arm", "arm64", "aarch64"):
        return "arm"
    if override in ("intel", "x86_64", "x64"):
        return "intel"
    return "arm" if platform.machine().lower() in ("arm64", "aarch64") else "intel"


def _plan() -> list[_Fetch]:
    """当前平台要下载的归档列表。Windows 一个，macOS 两个。"""
    if IS_MAC:
        table = _MAC_ARM if _mac_arch() == "arm" else _MAC_INTEL
        return [
            _Fetch(table["ffmpeg"], f"ffmpeg-mac-{_mac_arch()}.zip", _MAC_EXPECTED_MB, ("ffmpeg",)),
            _Fetch(table["ffprobe"], f"ffprobe-mac-{_mac_arch()}.zip", _MAC_EXPECTED_MB, ("ffprobe",)),
        ]
    # 非 Windows / 非 macOS（Linux 等）也走 Windows 那套 zip 逻辑，
    # 至少让显式配置 ffmpeg_path 或系统里已有 ffmpeg 的用户正常工作。
    return [_Fetch(_WIN_URL, "ffmpeg.zip", _WIN_EXPECTED_MB, ("ffmpeg.exe", "ffprobe.exe"))]


def _wanted_names() -> tuple[str, ...]:
    return ("ffmpeg.exe", "ffprobe.exe") if IS_WINDOWS else ("ffmpeg", "ffprobe")


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
    plan = _plan()
    started = time.time()

    def emit(pct: float, msg: str) -> None:
        if on_progress:
            on_progress(pct, msg)

    # 总进度按归档数均分：Windows 1 个占满，macOS 两个各占一半。
    slices = 90.0 / len(plan)
    for idx, fetch in enumerate(plan):
        base = idx * slices
        archive = user_data_dir() / "cache" / fetch.cache
        label = "+".join(fetch.names)
        _download_one(fetch, archive, base, slices, emit, cancel)
        emit(base + slices, f"解压 {label}…")
        _extract(archive, target, fetch.names)
        # 坏包删掉，下次重下
        if not all((target / n).is_file() for n in fetch.names):
            archive.unlink(missing_ok=True)
            raise DependencyMissingError(
                f"ffmpeg 压缩包内容异常（缺 {label}），已删除缓存，请重试",
                detail={"archive": str(archive)},
            )

    emit(92.0, "校验 ffmpeg…")
    _verify(target)
    log.info("ffmpeg 安装完成，用时 %.1fs -> %s", time.time() - started, target)
    emit(100.0, "ffmpeg 就绪")
    return status()


def _download_one(
    fetch: _Fetch,
    archive: Path,
    base: float,
    span: float,
    emit: ProgressFn | None,
    cancel: CancelFn | None,
) -> None:
    """下载单个归档，进度映射到 [base, base+span)。支持断点续传。"""
    if archive.is_file() and archive.stat().st_size >= 1024:
        return                                   # 已有缓存，跳过下载

    label = "+".join(fetch.names)

    def report(chunk: int) -> None:
        mb = chunk / 1048576
        pct = base + min(1.0, mb / fetch.expected_mb) * span
        if emit:
            emit(pct, f"下载 {label} {mb:.0f}/{fetch.expected_mb:.0f}MB")

    if emit:
        emit(base, f"正在下载 {label}（约 {fetch.expected_mb:.0f}MB）…")
    try:
        stream_download(fetch.url, archive, on_chunk=report, cancel=cancel, timeout=180.0)
    except Exception as exc:
        raise DependencyMissingError(
            f"{label} 下载失败：{str(exc)[:120]}。"
            "可重试（支持断点续传），或手动安装 ffmpeg 后重启程序。",
            detail={"url": fetch.url, "cache": str(archive)},
        ) from exc


def _extract(archive: Path, target: Path, names: tuple[str, ...]) -> None:
    """从 zip 里取目标可执行文件。

    Windows 包里有两个 .exe 和一堆文档，只取需要的；macOS 包里就一个无后缀
    文件（外加 __MACOSX/ 元数据），按名字精确匹配。解压后 macOS 要 chmod +x。
    """
    wanted = {n.lower() for n in names}
    with zipfile.ZipFile(archive) as zf:
        for info in zf.infolist():
            base = info.filename.rsplit("/", 1)[-1].lower()
            if base not in wanted:
                continue
            # 用 names 里声明的大小写（Windows 是 .exe，macOS 无后缀）
            real = next(n for n in names if n.lower() == base)
            dest = target / real
            with zf.open(info) as src, dest.open("wb") as out:
                while chunk := src.read(1 << 20):
                    out.write(chunk)
            if not IS_WINDOWS:
                dest.chmod(0o755)            # zip 不保留可执行位，必须显式加
            log.info("已解压 %s -> %s (%d bytes)", info.filename, dest, dest.stat().st_size)


def _verify(target: Path) -> None:
    """解压完跑一次 -version：包损坏 / 缺动态库会在这里暴露。"""
    for name in _wanted_names():
        exe = target / name
        if not exe.is_file():
            raise DependencyMissingError(f"解压后缺少 {name}")
        proc = subprocess.run(
            [str(exe), "-version"],
            capture_output=True,
            timeout=60,
            creationflags=0x08000000 if IS_WINDOWS else 0,
        )
        if proc.returncode != 0:
            raise DependencyMissingError(
                f"{name} 无法运行（可能缺少运行库或架构不匹配）："
                f"{proc.stderr.decode('utf-8', 'replace')[-120:]}"
            )
