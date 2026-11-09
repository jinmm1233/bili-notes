"""原生窗口：优先 Edge --app 模式，开发环境退回 pywebview。

为什么不用 pywebview 做打包后的主窗口：
  - pywebview 在 PyInstaller 冻结环境下，pythonnet/CLR 引导会直接段错误
    （开发和源码运行都正常，一冻结就崩，faulthandler 都抓不到栈）。
  - Edge `--app=<url>` + 独立 user-data-dir 会开一个**干净的无边框独立窗口**
    （没有地址栏、没有标签页，任务栏是自己的图标），视觉效果和 pywebview
    一致，但只依赖系统自带 Edge（导 PDF 本来就要求有它），零额外依赖。

窗口是否还开着，**不能看 Popen 的进程**：Edge 是多进程的，`--app` 启动时
Popen 拿到的那个进程会把窗口交给常驻的浏览器进程、自己先退出（实测 12 秒后
EXIT=0，而窗口还开着）。以前用 `proc.wait()` 判断，结果程序 12 秒就自己收摊、
窗口变成「连不上 127.0.0.1」。现在改为轮询窗口本身是否存在（EnumWindows），
与 Edge 内部进程模型无关。

环境变量 BILINOTES_WINDOW=0 强制走默认浏览器（调试后端时方便看日志）。
"""
from __future__ import annotations

import ctypes
import logging
import os
import subprocess
import sys
import threading
import time
from ctypes import wintypes
from pathlib import Path

from .core.paths import user_data_dir

log = logging.getLogger(__name__)

# 当前 Edge 应用窗口的进程。更新安装前要先关掉它，安装器才能覆盖 exe。
_active: subprocess.Popen | None = None
_active_lock = threading.Lock()

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_WM_CLOSE = 0x0010
_SW_RESTORE = 9


def focus_existing() -> bool:
    """把已经开着的应用窗口叫到前台；没有窗口返回 False。

    第二次双击图标时应该是"把窗口切出来"，而不是再起一个进程干等——
    以前走 open_blocking，每双击一次就多一个 136MB 的 Python 进程挂在后台，
    双击五次就能吃掉半 G 内存，而用户看到的只是同一个界面。
    """
    hwnd = _app_window()
    if not hwnd:
        return False
    try:
        if _user32.IsIconic(hwnd):
            _user32.ShowWindow(hwnd, _SW_RESTORE)
        _user32.SetForegroundWindow(hwnd)
        log.info("已有窗口，切到前台")
        return True
    except Exception as exc:                   # 切换前台被系统拦下也不算失败
        log.debug("窗口切前台失败：%s", exc)
        return False
_ENUM_PROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


def _app_window() -> int:
    """找 Edge 的 `--app` 窗口，返回 hwnd；没有则 0。

    只认类名 `Chrome_WidgetWin_1`（Chromium 的顶层窗口类），避免把资源管理器里
    名字带 BiliNotes 的文件夹窗口误当成应用窗口。标题可能是页面标题
    （`BiliNotes · B站视频知识库`），也可能是加载失败时的 URL（`127.0.0.1`）。
    """
    found = 0

    def _visit(hwnd, _lparam):
        nonlocal found
        if found or not _user32.IsWindowVisible(hwnd):
            return True
        cbuf = ctypes.create_unicode_buffer(64)
        _user32.GetClassNameW(hwnd, cbuf, 64)
        if cbuf.value != "Chrome_WidgetWin_1":
            return True
        n = _user32.GetWindowTextLengthW(hwnd)
        title = ""
        if n:
            tbuf = ctypes.create_unicode_buffer(n + 1)
            _user32.GetWindowTextW(hwnd, tbuf, n + 1)
            title = tbuf.value
        if "BiliNotes" in title or "127.0.0.1" in title:
            found = int(hwnd)
        return True

    try:
        _user32.EnumWindows(_ENUM_PROC(_visit), 0)
    except Exception as exc:                   # 枚举失败不该让程序起不来
        log.debug("枚举窗口失败：%s", exc)
    return found


_EDGE_CANDIDATES = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe"),
)


def _find_edge() -> str | None:
    for path in _EDGE_CANDIDATES:
        if Path(path).is_file():
            return path
    import shutil  # noqa: PLC0415

    return shutil.which("msedge")


def available() -> bool:
    """能不能开独立窗口（Edge --app 或 pywebview，总有一个就算有）。"""
    if os.environ.get("BILINOTES_WINDOW") == "0":
        return False
    if _find_edge():
        return True
    try:
        import webview  # noqa: F401, PLC0415
        return True
    except Exception:
        return False


def open_blocking(url: str, title: str = "BiliNotes") -> None:
    """开一个独立应用窗口并阻塞到窗口关闭。全都起不来时抛异常由调用方兜底。"""
    edge = _find_edge()
    if edge:
        _open_edge_app(edge, url)
        return
    _open_pywebview(url, title)


def _open_edge_app(edge: str, url: str) -> None:
    """Edge --app 模式：独立 profile 保证它是独立进程，proc.wait() 等窗口关闭。"""
    profile = user_data_dir() / "edge-profile"
    profile.mkdir(parents=True, exist_ok=True)
    log.info("用 Edge 应用模式开窗口：%s", url)
    # 不加 CREATE_NO_WINDOW：msedge 本身是 GUI 程序，不会闪黑框
    proc = subprocess.Popen(
        [
            edge,
            f"--app={url}",
            f"--user-data-dir={profile}",
            "--window-size=1280,880",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-features=msSmartScreenPromptForNativeWindows",  # 别对自家地址瞎拦
        ],
        cwd=str(Path(edge).parent),
    )
    global _active  # noqa: PLW0603  模块级单例，整个进程只有一个窗口
    with _active_lock:
        _active = proc
    try:
        # 1) 等窗口出现（Edge 拉起 + 页面加载，最多约 20 秒）
        for _ in range(25):
            time.sleep(0.8)
            if _app_window():
                break
        else:
            raise RuntimeError("Edge 应用窗口没有出现")

        # 2) 等窗口消失。**不能**用 proc.wait()：Edge 多进程会把窗口交给
        #    常驻的浏览器进程，Popen 的启动进程提前退出（实测 12 秒），
        #    用它判定会误以为「窗口关了」，于是服务跟着收摊、窗口变成连不上。
        log.info("应用窗口已就绪，等待关闭")
        misses = 0
        while misses < 3:              # 连续约 4.5 秒看不到窗口才算真的关了
            time.sleep(1.5)
            if _app_window():
                misses = 0
            else:
                misses += 1
        log.info("应用窗口已关闭")
    finally:
        with _active_lock:
            if _active is proc:
                _active = None
        if proc.poll() is None:
            proc.terminate()


def close() -> bool:
    """关掉当前应用窗口。返回是否真的关了；没窗口时返回 False。

    由 updater.install() 调用：发 WM_CLOSE 后，上面的轮询发现窗口消失就返回，
    run() 随即退出进程，安装器才能覆盖 exe。
    """
    hwnd = _app_window()
    if hwnd:
        try:
            _user32.PostMessageW(hwnd, _WM_CLOSE, 0, 0)
            log.info("已请求关闭应用窗口")
            return True
        except Exception as exc:
            log.debug("发送关闭消息失败：%s", exc)
    with _active_lock:
        proc = _active
    if proc is not None and proc.poll() is None:
        try:
            proc.terminate()
            return True
        except Exception as exc:
            log.debug("终止应用进程失败：%s", exc)
    return False


def _open_pywebview(url: str, title: str) -> None:
    """pywebview 兜底：只在源码运行时用（冻结环境会段错误，见模块注释）。"""
    if getattr(sys, "frozen", False):
        raise RuntimeError("打包环境不开 pywebview（已知段错误），走浏览器兜底")
    import webview  # noqa: PLC0415

    window = webview.create_window(
        title, url,
        width=1280, height=880,
        min_size=(920, 600),
        background_color="#f4f5f8",
    )
    window.events.closed += lambda: log.info("窗口已关闭")
    webview.start(private_mode=False)
