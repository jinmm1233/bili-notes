"""调系统自带的文件选择框。

为什么不用网页的 <input type=file>：这个界面跑在本地浏览器里，
浏览器出于安全只给 File 对象、不给真实路径，几个 G 的视频复制一份不现实。
所以让后端弹系统的「打开文件」对话框，只把路径拿回来。

只在用户点按钮时调用；取消返回 None（不是错误）。
"""
from __future__ import annotations

import logging
import shutil
import subprocess
import sys

log = logging.getLogger(__name__)

TITLE = "选择要生成笔记的视频"
FILTER_TEXT = (
    "视频文件\0*.mp4;*.mkv;*.mov;*.flv;*.webm;*.avi;*.ts;*.m4v;*.wmv;*.mpg;*.mpeg\0"
    "所有文件\0*.*\0"
)


class PickerUnavailable(RuntimeError):
    """这台机器上没法弹系统对话框（远程会话、缺少组件等）。"""


def _pick_windows() -> str | None:
    import ctypes
    from ctypes import wintypes

    class OPENFILENAMEW(ctypes.Structure):
        _fields_ = [
            ("lStructSize", wintypes.DWORD),
            ("hwndOwner", wintypes.HWND),
            ("hInstance", wintypes.HINSTANCE),
            ("lpstrFilter", wintypes.LPCWSTR),
            ("lpstrCustomFilter", wintypes.LPWSTR),
            ("nMaxCustFilter", wintypes.DWORD),
            ("nFilterIndex", wintypes.DWORD),
            ("lpstrFile", wintypes.LPWSTR),
            ("nMaxFile", wintypes.DWORD),
            ("lpstrFileTitle", wintypes.LPWSTR),
            ("nMaxFileTitle", wintypes.DWORD),
            ("lpstrInitialDir", wintypes.LPCWSTR),
            ("lpstrTitle", wintypes.LPCWSTR),
            ("Flags", wintypes.DWORD),
            ("nFileOffset", wintypes.WORD),
            ("nFileExtension", wintypes.WORD),
            ("lpstrDefExt", wintypes.LPCWSTR),
            ("lCustData", wintypes.LPARAM),
            ("lpfnHook", ctypes.c_void_p),
            ("lpTemplateName", wintypes.LPCWSTR),
        ]

    OFN_FILEMUSTEXIST = 0x00001000
    OFN_PATHMUSTEXIST = 0x00000800
    OFN_EXPLORER = 0x00080000
    OFN_NOCHANGEDIR = 0x00000008      # 别把进程的工作目录改到用户选的文件夹

    buf = ctypes.create_unicode_buffer(32768)
    # 过滤器里有 \0 分隔符，也用 buffer 传，别让 ctypes 在第一个 \0 处截断
    filter_buf = ctypes.create_unicode_buffer(FILTER_TEXT)
    ofn = OPENFILENAMEW()
    ofn.lStructSize = ctypes.sizeof(OPENFILENAMEW)
    try:
        ofn.hwndOwner = ctypes.windll.user32.GetForegroundWindow()
    except Exception:                 # 拿不到前台窗口也不影响弹框
        ofn.hwndOwner = None
    ofn.lpstrFilter = ctypes.cast(filter_buf, ctypes.c_wchar_p)
    ofn.lpstrFile = ctypes.cast(buf, ctypes.c_wchar_p)
    ofn.nMaxFile = len(buf)
    ofn.lpstrTitle = TITLE
    ofn.Flags = OFN_FILEMUSTEXIST | OFN_PATHMUSTEXIST | OFN_EXPLORER | OFN_NOCHANGEDIR

    func = ctypes.windll.comdlg32.GetOpenFileNameW
    func.restype = ctypes.c_int
    func.argtypes = [ctypes.POINTER(OPENFILENAMEW)]
    ok = func(ctypes.byref(ofn))
    if ok:
        return buf.value or None
    # 0 = 用户取消或出错。ComDlgExtendedError 非 0 才算真错误
    try:
        err = ctypes.windll.comdlg32.CommDlgExtendedError()
    except Exception:
        err = 0
    if err:
        raise PickerUnavailable(f"系统文件对话框打不开（错误码 {err}），可以直接把路径粘贴到输入框")
    return None


def _pick_macos() -> str | None:
    script = (
        'POSIX path of (choose file with prompt "选择要生成笔记的视频" '
        'of type {"public.movie", "public.video", "public.audiovisual-content"})'
    )
    proc = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    if proc.returncode != 0:
        if "User canceled" in (proc.stderr or ""):
            return None
        raise PickerUnavailable(f"系统文件对话框打不开：{(proc.stderr or '').strip()[:120]}")
    path = (proc.stdout or "").strip()
    return path or None


def _pick_linux() -> str | None:
    if not shutil.which("zenity"):
        raise PickerUnavailable("没有找到 zenity，可以直接把路径粘贴到输入框")
    proc = subprocess.run(
        ["zenity", "--file-selection", f"--title={TITLE}",
         "--file-filter=视频文件 | *.mp4 *.mkv *.mov *.flv *.webm *.avi *.ts *.m4v *.wmv"],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        return None
    path = (proc.stdout or "").strip()
    return path or None


def pick_video() -> str | None:
    """返回选中的绝对路径；用户取消返回 None；环境不支持抛 PickerUnavailable。"""
    if sys.platform == "win32":
        return _pick_windows()
    if sys.platform == "darwin":
        return _pick_macos()
    return _pick_linux()
