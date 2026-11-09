"""把笔记导出成 PDF：借用户电脑上现成的 Chromium 内核浏览器做"无头打印"。

为什么不用 reportlab / weasyprint：
  - 笔记里真正难排版的是数学公式（矩阵、分式）和关键帧图片。这些在浏览器里
    已经由 KaTeX 渲染好了，PDF 直接复用同一套 HTML/CSS，所见即所得，零解析成本。
  - 那两个库对中文 + 数学都要额外折腾字体和 LaTeX，体积还大（weasyprint 要装 GTK）。

代价：依赖机器上有 Edge 或 Chrome。Windows 10/11 必带 Edge，够用；
找不到浏览器时返回 None，前端退回浏览器自己的"打印 → 另存为 PDF"。

打印用的页面是 `/?print=<笔记id>`：前端会跳过导航、直接打开这篇笔记，
并在渲染完成后打上 `data-print-ready`，这里靠 --virtual-time-budget 等它画完。
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)

# 常见安装位置，按优先级排。Edge 必带，Chrome 作为补充。
_CANDIDATES = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
)

# 等页面把笔记、公式、图片都画完。笔记长、图多时虚时间要多给一点。
_VTIME_MS = 15000


def find_browser() -> str | None:
    for path in _CANDIDATES:
        if path and Path(path).is_file():
            return path
    for name in ("msedge", "chrome", "chromium"):
        found = shutil.which(name)
        if found:
            return found
    return None


def export(url: str, out: Path, timeout: float = 150.0) -> Path | None:
    """把 url 打成 PDF 写到 out。返回 None 表示环境不支持（没有浏览器）。"""
    browser = find_browser()
    if not browser:
        return None

    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        try:
            out.unlink()
        except OSError:
            pass

    with tempfile.TemporaryDirectory(prefix="bn_pdf_") as profile:
        cmd = [
            browser,
            "--headless=new",
            "--disable-gpu",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-extensions",
            "--disable-background-networking",
            "--run-all-compositor-stages-before-draw",
            f"--user-data-dir={profile}",
            f"--virtual-time-budget={_VTIME_MS}",
            "--no-pdf-header-footer",
            f"--print-to-pdf={out}",
            url,
        ]
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                timeout=timeout,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except subprocess.TimeoutExpired:
            log.warning("PDF 导出超时（%ss）", timeout)
            return None
        except OSError as exc:
            log.warning("启动浏览器失败：%s", exc)
            return None

    if not out.is_file() or out.stat().st_size < 1024:
        tail = (proc.stderr or b"")[-400:].decode("utf-8", "replace")
        log.warning("PDF 导出失败：%s", tail)
        return None
    with out.open("rb") as fh:
        if fh.read(5) != b"%PDF-":
            log.warning("PDF 导出产物不是 PDF")
            return None
    return out


def print_url(base_url: str, note_id: str, include_transcript: bool) -> str:
    """拼打印页地址。t=1 带上完整转录，t=0 只打正文（默认，省纸）。"""
    from urllib.parse import quote, urlencode

    query = urlencode({"print": note_id, "t": "1" if include_transcript else "0"})
    return f"{base_url.rstrip('/')}/?{query}"
