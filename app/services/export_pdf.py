"""把笔记导出成 PDF：借用户电脑上现成的 Chromium 内核浏览器做"无头打印"。

为什么不用 reportlab / weasyprint：
  - 笔记里真正难排版的是数学公式（矩阵、分式）和关键帧图片。这些在浏览器里
    已经由 KaTeX 渲染好了，PDF 直接复用同一套 HTML/CSS，所见即所得，零解析成本。
  - 那两个库对中文 + 数学都要额外折腾字体和 LaTeX，体积还大（weasyprint 要装 GTK）。

代价：依赖机器上有 Edge 或 Chrome。Windows 10/11 必带 Edge，够用；
找不到浏览器时返回 None，前端退回浏览器自己的"打印 → 另存为 PDF"。

打印用的页面是 `/?print=<笔记id>`：前端会跳过导航、直接打开这篇笔记，
并在渲染完成后打上 `data-print-ready`。这里优先用 DevTools 协议等这个标记
出现再打印；拿不到调试端口时退回 `--print-to-pdf`（老办法）。
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.parse import quote, urlparse

log = logging.getLogger(__name__)

# 常见安装位置，按优先级排。Windows 靠系统自带 Edge，macOS 找 Chrome/Edge。
_WIN_CANDIDATES = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
)
_MAC_CANDIDATES = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    os.path.expanduser("~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
)
_CANDIDATES = _WIN_CANDIDATES + _MAC_CANDIDATES

# 等页面把笔记、公式、图片都画完。笔记长、图多时虚时间要多给一点。
_VTIME_MS = 15000


def find_browser() -> str | None:
    for path in _CANDIDATES:
        if path and Path(path).is_file():
            return path
    for name in ("msedge", "chrome", "chromium", "google-chrome", "google-chrome-stable"):
        found = shutil.which(name)
        if found:
            return found
    return None


def _free_port() -> int:
    """先占一个空闲端口再交给浏览器：`--remote-debugging-port=0` 拿不到端口号。"""
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _cdp_ready(port: int, timeout: float) -> bool:
    """等 DevTools 的 HTTP 服务起来（浏览器启动要一两秒）。"""
    import urllib.request

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.15)
    return False


def _new_target(port: int, url: str) -> dict:
    """开一个标签页指向打印地址，返回它的 webSocketDebuggerUrl。"""
    import urllib.request

    endpoint = f"http://127.0.0.1:{port}/json/new?{quote(url, safe='')}"
    for method in ("PUT", "GET"):        # 新版 Chrome 只认 PUT
        try:
            req = urllib.request.Request(endpoint, method=method)
            with urllib.request.urlopen(req, timeout=10) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception:
            if method == "GET":
                raise
    raise RuntimeError("无法创建调试标签页")


def _cdp_send(ws, counter, method: str, params: dict | None = None) -> dict:
    msg_id = next(counter)
    ws.send(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
    while True:
        raw = ws.recv(timeout=30)
        msg = json.loads(raw)
        if msg.get("id") == msg_id:
            return msg


def _export_via_cdp(browser: str, url: str, out: Path, timeout: float) -> Path | None:
    """用 DevTools 协议：等 data-print-ready 出现再打印。

    为什么必须这样：`--print-to-pdf` 在页面“加载完成”时就截，前端还在等接口
    返回的话会导出白纸（短笔记必现）。CDP 能明确等到渲染完成这个信号。
    """
    try:
        from websockets.sync.client import connect
    except Exception as exc:                       # 打包环境缺依赖就走老路
        log.info("没有 websockets，跳过 DevTools 打印：%s", exc)
        return None

    port = _free_port()
    profile = tempfile.mkdtemp(prefix="bn_pdf_cdp_")
    proc = subprocess.Popen(
        [
            browser,
            "--headless=new",
            "--disable-gpu",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-extensions",
            f"--remote-debugging-port={port}",
            f"--user-data-dir={profile}",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        if not _cdp_ready(port, 15):
            log.warning("DevTools 端口没起来，退回命令行打印")
            return None
        target = _new_target(port, url)
        with connect(target["webSocketDebuggerUrl"], max_size=None, open_timeout=10) as ws:
            counter = iter(range(1, 10_000))
            _cdp_send(ws, counter, "Page.enable")
            _cdp_send(ws, counter, "Runtime.enable")
            # 新开的标签页是后台页，不切到前台可能不参与绘制，打印出来是空白
            _cdp_send(ws, counter, "Page.bringToFront")

            deadline = time.time() + timeout
            while time.time() < deadline:
                msg = _cdp_send(ws, counter, "Runtime.evaluate", {
                    "expression": "document.documentElement.getAttribute('data-print-ready')",
                    "returnByValue": True,
                })
                value = ((msg.get("result") or {}).get("result") or {}).get("value")
                if value == "1":
                    break
                if value == "error":
                    log.warning("打印页渲染失败（前端报错）")
                    return None
                time.sleep(0.2)
            else:
                log.warning("等打印页就绪超时（%.0fs）", timeout)
                return None

            # 就绪标记是 JS 打的，布局/字体还要一两帧才落定，稍等一下再打
            time.sleep(0.5)
            msg = _cdp_send(ws, counter, "Page.printToPDF", {
                "printBackground": True,
                "preferCSSPageSize": True,
            })
            data = ((msg.get("result") or {}).get("data") or "")
            if not data:
                log.warning("DevTools 打印没有返回数据")
                return None
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(base64.b64decode(data))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(profile, ignore_errors=True)

    return out if _valid_pdf(out) else None


def _valid_pdf(out: Path) -> bool:
    try:
        if not out.is_file() or out.stat().st_size < 1024:
            return False
        with out.open("rb") as fh:
            return fh.read(5) == b"%PDF-"
    except OSError:
        return False


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

    # 首选：DevTools 协议，能等到「渲染完成」再打印（否则短笔记会导出白纸）
    try:
        if _export_via_cdp(browser, url, out, min(timeout, 120.0)):
            return out
        log.info("DevTools 打印没成功，改用命令行打印")
    except Exception as exc:                       # 任何异常都不该让导出直接失败
        log.warning("DevTools 打印异常，改用命令行：%s", exc)

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

    if not _valid_pdf(out):
        tail = (proc.stderr or b"")[-400:].decode("utf-8", "replace")
        log.warning("PDF 导出失败：%s", tail)
        return None
    return out


def print_url(base_url: str, note_id: str, include_transcript: bool) -> str:
    """拼打印页地址。t=1 带上完整转录，t=0 只打正文（默认，省纸）。"""
    from urllib.parse import urlencode

    query = urlencode({"print": note_id, "t": "1" if include_transcript else "0"})
    return f"{base_url.rstrip('/')}/?{query}"
