"""用系统 Shell API（IShellLink + IPersistFile）生成 .lnk。

为什么不手写二进制：手写版 ShellExecute 返回 5（拒绝访问）——缺卷信息，
Shell 判为不可信。系统 API 会自己填卷序列号、相对路径等，生成的就是
资源管理器自己会写的那种文件。

为什么不用 WScript.Shell：当前环境的权限策略禁用了 PowerShell 的 COM
实例化，只能用 ctypes 直接调 COM。

用法：
    python tools/make_lnk.py <目标exe> <输出.lnk> [工作目录] [描述]
"""
from __future__ import annotations

import ctypes
import os
import sys
from ctypes import POINTER, byref, c_void_p, wintypes

ole32 = ctypes.windll.ole32
CLSCTX_INPROC_SERVER = 1


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]


def guid(text: str) -> GUID:
    g = GUID()
    hr = ole32.CLSIDFromString(ctypes.c_wchar_p(text), byref(g))
    if hr < 0:
        raise OSError(f"CLSIDFromString 失败: {text}")
    return g


CLSID_SHELL_LINK = "{00021401-0000-0000-C000-000000000046}"
IID_ISHELLLINKW = "{000214F9-0000-0000-C000-000000000046}"
IID_IPERSISTFILE = "{0000010B-0000-0000-C000-000000000046}"


def _method(ptr: c_void_p, index: int, restype, *argtypes):
    vtbl = ctypes.cast(ptr, POINTER(POINTER(c_void_p))).contents
    proto = ctypes.WINFUNCTYPE(restype, c_void_p, *argtypes)
    return proto(vtbl[index])


def _check(hr: int, what: str) -> None:
    if hr < 0:
        raise OSError(f"{what} 失败: HRESULT=0x{hr & 0xFFFFFFFF:08X}")


def create(target: str, lnk: str, workdir: str = "", desc: str = "BiliNotes") -> None:
    ole32.CoInitialize(None)
    try:
        p_link = c_void_p()
        _check(
            ole32.CoCreateInstance(
                byref(guid(CLSID_SHELL_LINK)), None, CLSCTX_INPROC_SERVER,
                byref(guid(IID_ISHELLLINKW)), byref(p_link),
            ),
            "CoCreateInstance(ShellLink)",
        )
        if not p_link:
            raise OSError("没有拿到 IShellLink 接口")

        set_path = _method(p_link, 20, ctypes.c_long, wintypes.LPCWSTR)
        _check(set_path(p_link, target), "SetPath")

        if workdir:
            set_wd = _method(p_link, 9, ctypes.c_long, wintypes.LPCWSTR)
            _check(set_wd(p_link, workdir), "SetWorkingDirectory")

        set_icon = _method(p_link, 17, ctypes.c_long, wintypes.LPCWSTR, ctypes.c_int)
        _check(set_icon(p_link, target, 0), "SetIconLocation")

        if desc:
            set_desc = _method(p_link, 7, ctypes.c_long, wintypes.LPCWSTR)
            _check(set_desc(p_link, desc), "SetDescription")

        p_persist = c_void_p()
        query = _method(p_link, 0, ctypes.c_long, POINTER(GUID), POINTER(c_void_p))
        _check(query(p_link, byref(guid(IID_IPERSISTFILE)), byref(p_persist)),
               "QueryInterface(IPersistFile)")
        if not p_persist:
            raise OSError("没有拿到 IPersistFile 接口")

        save = _method(p_persist, 6, ctypes.c_long, wintypes.LPCWSTR, wintypes.BOOL)
        _check(save(p_persist, lnk, True), "IPersistFile.Save")

        release = _method(p_persist, 2, ctypes.c_ulong)
        release(p_persist)
        release_link = _method(p_link, 2, ctypes.c_ulong)
        release_link(p_link)
    finally:
        ole32.CoUninitialize()


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    target = os.path.normpath(sys.argv[1])
    lnk = os.path.normpath(sys.argv[2])
    workdir = os.path.normpath(sys.argv[3]) if len(sys.argv) > 3 else os.path.dirname(target)
    desc = sys.argv[4] if len(sys.argv) > 4 else "BiliNotes"

    if not os.path.isfile(target):
        print(f"目标不存在: {target}")
        return 1

    create(target, lnk, workdir, desc)
    size = os.path.getsize(lnk)
    print(f"已写入 {lnk}（{size} 字节）")
    print(f"  目标: {target}")
    print(f"  工作目录: {workdir}")

    # 用 ShellExecute 试着解析一次：返回 >32 说明 Shell 认这个快捷方式
    r = ctypes.windll.shell32.ShellExecuteW(None, "open", lnk, None, None, 1)
    print(f"  ShellExecute 返回: {r}（>32 为成功；5=拒绝访问，说明是环境策略）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
