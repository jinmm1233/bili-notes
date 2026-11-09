"""路径解析：源码运行与 PyInstaller 打包后行为一致。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "BiliNotes"


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def app_root() -> Path:
    """可写资源根目录：源码模式下是项目根，打包后是 exe 所在目录。"""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def bundle_root() -> Path:
    """打包进 exe 的只读资源目录（--add-data 解压位置）。"""
    meipass = getattr(sys, "_MEIPASS", None)
    return Path(meipass) if meipass else app_root()


def user_data_dir() -> Path:
    """%APPDATA%\\BiliNotes，存配置、密钥、模型、缓存。"""
    base = os.environ.get("APPDATA") or str(Path.home() / ".config")
    path = Path(base) / APP_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def default_output_dir() -> Path:
    docs = Path.home() / "Documents"
    if not docs.exists():
        docs = Path.home()
    path = docs / APP_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def fmt_ts(seconds: float) -> str:
    """123.4 -> 02:03；3700 -> 01:01:40"""
    total = max(0, int(seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def safe_dirname(name: str, limit: int = 60) -> str:
    """把视频标题转成合法的 Windows 目录名。"""
    bad = '<>:"/\\|?*'
    cleaned = "".join("_" if c in bad else c for c in name)
    cleaned = "".join(c for c in cleaned if ord(c) >= 32).strip(" .")
    return (cleaned or "untitled")[:limit]


def ts_link(url: str, seconds: float) -> str:
    """跳到视频第 seconds 秒的链接（B站支持 ?t=）。"""
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}t={int(seconds)}"
