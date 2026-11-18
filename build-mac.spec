# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置（macOS，onedir + .app bundle）。

和 build.spec（Windows）的差异：
  * 不打包 bin/ —— ffmpeg 走运行时下载（osxexperts，见 ffmpeg_boot.py）
  * 图标用 app.icns，不碰 .ico
  * 不经 Windows 的 VC++ 运行库后处理
  * 收 pywebview + pyobjc —— macOS 用它开原生窗口（WKWebView），
    不像 Windows 那样有 CLR 段错误；必须让 PyInstaller 把这些动态 import 收进来

产物：
    dist/BiliNotes.app
打包命令（在 macOS runner 上）：
    pyinstaller build-mac.spec --noconfirm
"""
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules

ROOT = Path(SPECPATH).resolve()  # noqa: F821

# 版本号唯一来源：app/__init__.py，避免打包后 Info.plist 和程序版本对不上
import re as _re
_ver_src = (ROOT / "app" / "__init__.py").read_text(encoding="utf-8")
_m = _re.search(r'__version__\s*=\s*["\']([^"\']+)["\']', _ver_src)
VERSION = _m.group(1) if _m else "0.0.0"
print(f"[version] {VERSION}")

datas = []
binaries = []
hiddenimports = ["qrcode", "qrcode.image", "qrcode.image.svg", "qrcode.image.base"]

# 前端静态资源
web_dir = ROOT / "web"
if web_dir.is_dir():
    datas.append((str(web_dir), "web"))

# 导出 Word 的第三方库（vendor/），复制干净副本剔除 __pycache__（内含构建机路径）
vendor_dir = ROOT / "vendor"
if vendor_dir.is_dir():
    import shutil as _shutil
    import tempfile as _tempfile

    _clean = Path(_tempfile.mkdtemp(prefix="bn_vendor_")) / "vendor"
    _shutil.copytree(
        str(vendor_dir), str(_clean),
        ignore=_shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    print(f"[vendor] 干净副本已备好（残留 .pyc {sum(1 for _ in _clean.rglob('*.pyc'))} 个）")
    datas.append((str(_clean), "vendor"))

# 原生/数据依赖
for pkg in ("rapidocr_onnxruntime", "onnxruntime", "ctranslate2", "faster_whisper", "av", "yt_dlp"):
    try:
        pkg_datas, pkg_bins, pkg_hidden = collect_all(pkg)
        datas += pkg_datas
        binaries += pkg_bins
        hiddenimports += pkg_hidden
    except Exception as exc:
        print(f"[warn] collect_all({pkg}) 失败: {exc}")

datas += collect_data_files("certifi")
datas += collect_data_files("zhconv")

# macOS 原生窗口：pywebview + 它动态加载的 pyobjc 框架
for pkg in ("webview",):
    try:
        pkg_datas, pkg_bins, pkg_hidden = collect_all(pkg)
        datas += pkg_datas
        binaries += pkg_bins
        hiddenimports += pkg_hidden
    except Exception as exc:
        print(f"[warn] collect_all({pkg}) 失败: {exc}")
try:
    hiddenimports += collect_submodules("webview")
except Exception:
    pass

hiddenimports += [
    "docx", "docx.oxml", "docx.shared", "lxml", "lxml.etree",
    "latex2mathml.converter", "mathml2omml",
    "zhconv", "zhconv.zhconv",
    "app.services.export_docx", "app.services.export_pdf", "app.services.quiz_export",
    "app.api.routes_bili", "app.api.routes_env", "app.api.routes_library", "app.api.routes_update",
    "app.core.http", "app.core.envcheck", "app.core.updater", "app.core.local",
    "app.services.ffmpeg_boot", "pypinyin",
    "uvicorn.logging", "uvicorn.loops.auto", "uvicorn.loops.asyncio",
    "uvicorn.protocols.http.auto", "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets.auto", "uvicorn.lifespan.on",
    "app.api.routes", "app.services.pipeline",
    "app.services.anchors", "app.core.selftest",
]

a = Analysis(  # noqa: F821
    [str(ROOT / "entry.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "scipy", "pandas", "PyQt5", "PySide6", "pytest"],
    noarchive=False,
)

pyz = PYZ(a.pure)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="BiliNotes",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon=str(ROOT / "app.icns") if (ROOT / "app.icns").is_file() else None,
)

# 控制台版：排错用（Mac 上从终端运行 .app/Contents/MacOS/BiliNotes-console）
exe_console = EXE(  # noqa: F821
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="BiliNotes-console",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    icon=str(ROOT / "app.icns") if (ROOT / "app.icns").is_file() else None,
)

coll = COLLECT(  # noqa: F821
    exe, exe_console, a.binaries, a.datas,
    strip=False,
    upx=False,
    name="BiliNotes",
)

# 用 BUNDLE 把 onedir 产物包成 .app（macOS 标准应用包）
app = BUNDLE(  # noqa: F821
    coll,
    name="BiliNotes.app",
    icon=str(ROOT / "app.icns") if (ROOT / "app.icns").is_file() else None,
    bundle_identifier="com.bilinotes.app",
    info_plist={
        "CFBundleName": "BiliNotes",
        "CFBundleDisplayName": "BiliNotes",
        "CFBundleShortVersionString": VERSION,
        "CFBundleVersion": VERSION,
        "NSHighResolutionCapable": True,
        "LSMinimumSystemVersion": "10.15.0",
    },
)
