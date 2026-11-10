# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置（onedir 模式）。

为什么不用 onefile：每次启动要把 300MB 解压到临时目录，冷启动 10 秒以上，
且杀软误报率明显更高。onedir 冷启动 2 秒内，用 Inno Setup 打成安装包后
用户感知是一致的。

打包命令：
    pyinstaller build.spec --noconfirm
产物：
    dist/BiliNotes/BiliNotes.exe
"""
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_data_files

ROOT = Path(SPECPATH).resolve()  # noqa: F821

datas = []
binaries = []
# 二维码是运行时才 import 的子模块，静态分析容易漏
hiddenimports = ["qrcode", "qrcode.image", "qrcode.image.svg", "qrcode.image.base"]

# 前端静态资源
web_dir = ROOT / "web"
if web_dir.is_dir():
    datas.append((str(web_dir), "web"))

# 导出 Word 用的第三方库（python-docx / lxml / latex2mathml / mathml2omml）放在 vendor/，
# 整个目录随包分发；app/__init__.py 会把 <_MEIPASS>/vendor 加进 sys.path。
#
# 必须剔掉 __pycache__：.pyc 里记着**编译时的源码绝对路径**（含打包机的用户名），
# 打进安装包等于把构建者的用户名发给每个用户。实测能扫出 135 处 C:\Users\86159。
# 剔掉不影响运行——冻结环境有源码缓存，不需要带旧的 .pyc。
#
# 做法是**复制一份干净的**再打包，不动源目录（别在打包时改工作区）。
vendor_dir = ROOT / "vendor"
if vendor_dir.is_dir():
    import shutil as _shutil
    import tempfile as _tempfile

    _clean = Path(_tempfile.mkdtemp(prefix="bn_vendor_")) / "vendor"
    _shutil.copytree(
        str(vendor_dir), str(_clean),
        ignore=_shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    _left = sum(1 for _ in _clean.rglob("*.pyc"))
    print(f"[vendor] 干净副本已备好（残留 .pyc { _left } 个），原目录不动")
    datas.append((str(_clean), "vendor"))

# ffmpeg / ffprobe：放 bin/ 下随包分发，目标机无需安装
bin_dir = ROOT / "bin"
if bin_dir.is_dir():
    for exe in bin_dir.glob("*"):
        if exe.is_file():
            datas.append((str(exe), "bin"))

# 这些包带二进制/数据文件，必须显式收集，否则运行时才报缺文件
# 注意：**不要**把 webview/pythonnet/clr_loader 打进来。实测把 pythonnet 带进包后，
# 冻结进程起来几十秒就段错误（哪怕根本没 import webview，只 import 了 app.window）。
# 窗口改用 Edge --app 模式（见 app/window.py），不需要 CLR。开发环境仍可 pip 装 pywebview。
for pkg in ("rapidocr_onnxruntime", "onnxruntime", "ctranslate2", "faster_whisper", "av", "yt_dlp"):
    try:
        pkg_datas, pkg_bins, pkg_hidden = collect_all(pkg)
        datas += pkg_datas
        binaries += pkg_bins
        hiddenimports += pkg_hidden
    except Exception as exc:  # 某个包缺失不应阻断打包
        print(f"[warn] collect_all({pkg}) 失败: {exc}")

datas += collect_data_files("certifi")

hiddenimports += [
    "docx",
    "docx.oxml",
    "docx.shared",
    "lxml",
    "lxml.etree",
    "latex2mathml.converter",
    "mathml2omml",
    "app.services.export_docx",
    "app.services.export_pdf",
    "app.services.quiz_export",
    "app.api.routes_bili",
    "app.api.routes_env",
    "app.api.routes_library",
    "app.api.routes_update",
    "app.core.http",
    "app.core.envcheck",
    "app.core.updater",
    "app.core.local",
    "app.services.ffmpeg_boot",
    "pypinyin",
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan.on",
    "app.api.routes",
    "app.services.pipeline",
    # 运行时按条件 import：音画锚定（音画对）与打包自检（bn_selftest 探针）
    "app.services.anchors",
    "app.core.selftest",
]

a = Analysis(  # noqa: F821
    [str(ROOT / "entry.py")],   # 不能用 app/main.py：会破坏包内相对导入
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
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="BiliNotes",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    # 窗口模式：双击图标开 Edge --app 独立窗口，没有黑框。
    # 出问题时日志在 %APPDATA%\BiliNotes\logs\，下面还有一个控制台版兜底。
    console=False,
    icon=str(ROOT / "app.ico") if (ROOT / "app.ico").is_file() else None,
)

# 控制台版：排错用。报错能直接看见，也方便远程指导别人排查。
exe_console = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="BiliNotes-console",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    icon=str(ROOT / "app.ico") if (ROOT / "app.ico").is_file() else None,
)

coll = COLLECT(  # noqa: F821
    exe,
    exe_console,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="BiliNotes",
)

# ---- 构建后处理：统一 VC++ 运行库版本（别删，这是踩过段错误换来的）----
# collect_all 会把某个依赖 vendor 的 msvcp140.dll 14.26（VS2019 旧版）带进
# _internal，而系统里是 14.51。Windows 优先加载程序目录里的旧版，于是
# onnxruntime 1.31 在 import 时直接 access violation——表现为「程序起来十几秒
# 后静默崩掉」，以及画面取字完全不能用（rapidocr_onnxruntime 依赖它）。
# ctranslate2 / faster_whisper 反而不受影响，很容易误判成语音库的问题。
# 用构建机 System32 的同名文件覆盖，保证同一进程里所有原生库用同一套 CRT；
# 安装包另有 vc_redist.x64.exe 兜底目标机缺运行库的情况。
import os as _os  # noqa: E402
import shutil as _shutil  # noqa: E402

_internal = ROOT / "dist" / "BiliNotes" / "_internal"
_system32 = Path(_os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
_vc_dlls = (
    "msvcp140.dll", "MSVCP140_1.dll", "MSVCP140_2.dll",
    "vcruntime140.dll", "vcruntime140_1.dll", "concrt140.dll",
)
if _internal.is_dir():
    for _name in _vc_dlls:
        _src, _dst = _system32 / _name, _internal / _name
        if _src.is_file() and _dst.is_file():
            _shutil.copy2(_src, _dst)
            print(f"[vc-runtime] 统一 {_name} -> 系统版 {_dst.stat().st_size} bytes")
else:
    print("[vc-runtime] [warn] 没找到 _internal，跳过运行库统一（请检查构建产物）")
