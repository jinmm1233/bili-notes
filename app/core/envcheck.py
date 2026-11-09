"""运行环境自检：确认重型依赖在目标机器上真的能加载。

为什么需要：ctranslate2（语音转写）和 onnxruntime（画面 OCR）都带原生 DLL，
目标机器缺 VC++ 运行库时 import 会抛 "DLL load failed"——如果只在用户点
「生成笔记」时才暴露，报错和原因隔着十万八千里。这里在启动时后台探一次，
结果缓存在进程里，healthz / 设置页随取随用。

为什么在子进程里探（重要）：这两个扩展是几十 MB 的 C++ 巨物，在 PyInstaller
冻结包里**在主进程 import 有把整个进程带崩的先例**（原生段错误，faulthandler
都留不下栈，用户看到的就是"双击一闪就没了"）。放到子进程探，最坏情况是子进程
死掉、我们拿到非零返回码——主程序和服务完全不受牵连。子进程复用同一个 exe，
靠 BILINOTES_PROBE 环境变量进探测分支（见 entry.py）。

VC++ 运行库的修复方式：安装包里带 redist/vc_redist.x64.exe，双击装完重开。
"""
from __future__ import annotations

import contextlib
import logging
import os
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_lock = threading.Lock()
_result: dict[str, Any] | None = None

_HEAVY = (
    ("ctranslate2", "语音转文字"),
    ("onnxruntime", "画面取字（OCR）"),
)

# Windows 下别让子进程弹出黑框（窗口版尤其难看）
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _probe_cmd(module: str) -> tuple[list[str], dict[str, str]]:
    """组装探测命令。

    冻结后 sys.executable 是我们的 BiliNotes.exe，它不认 -c，只能靠环境变量
    通知它「这次只 import 一个模块就退出」；源码运行时 sys.executable 是
    python.exe，直接用 -c 最省事。
    """
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    env.pop("BILINOTES_WINDOW", None)          # 探测进程绝不开窗口
    if getattr(sys, "frozen", False):
        env["BILINOTES_PROBE"] = module
        env["BILINOTES_CRASHLOG"] = "0"
        return [sys.executable], env
    return [sys.executable, "-c", f"import {module}"], env


def _check_one(module: str, label: str) -> dict[str, Any]:
    cmd, env = _probe_cmd(module)
    with tempfile.TemporaryDirectory(prefix="bn_probe_") as tmp:
        out = Path(tmp) / "error.txt"
        env["BILINOTES_PROBE_OUT"] = str(out)
        try:
            proc = subprocess.run(
                cmd, env=env, capture_output=True, text=True,
                timeout=180, creationflags=_NO_WINDOW,
            )
        except subprocess.TimeoutExpired:
            hint = f"{label}自检超时（3 分钟没加载完），可能是杀毒软件在拦：{module}"
            log.warning("%s", hint)
            return {"key": module, "label": label, "ok": False, "hint": hint}
        except Exception as exc:                   # 起不来子进程（极少见）
            hint = f"{label}自检没能启动：{exc}"
            log.warning("%s", hint)
            return {"key": module, "label": label, "ok": False, "hint": hint}

        # 窗口版 stderr 是 devnull，import 的异常只能靠探针写文件带回来
        detail = ""
        with contextlib.suppress(OSError):
            detail = out.read_text(encoding="utf-8").strip()

    if proc.returncode == 0:
        return {"key": module, "label": label, "ok": True, "hint": ""}

    msg = "\n".join(x for x in (detail, (proc.stderr or "").strip(),
                                (proc.stdout or "").strip()) if x)
    crashed = proc.returncode < 0 or proc.returncode >= 0xC0000000
    if "DLL load failed" in msg or "找不到指定的模块" in msg:
        hint = (
            f"{label}用不了：缺少微软 VC++ 运行库。"
            "双击安装目录里的 redist/vc_redist.x64.exe 安装后重开程序即可；"
            "安装包用户一般已自动装好，遇到这个提示说明装机时跳过了。"
        )
    elif crashed:
        code = f"0x{proc.returncode & 0xFFFFFFFF:08X}" if proc.returncode < 0 else hex(proc.returncode)
        hint = (
            f"{label}加载时进程直接崩溃（{code}）。"
            "多半是杀毒软件把这批 DLL 拦了，或安装不完整——"
            "把安装目录加进杀毒白名单后重装一次试试。"
        )
    else:
        hint = f"{label}加载失败：{msg[-160:] or '返回码 ' + str(proc.returncode)}"
    log.warning("%s", hint)
    return {"key": module, "label": label, "ok": False, "hint": hint}


def probe() -> dict[str, Any]:
    """探测所有重型依赖（结果缓存，进程内只探一次）。"""
    global _result
    with _lock:
        if _result is not None:
            return _result
    checks = [_check_one(mod, label) for mod, label in _HEAVY]
    result = {"ok": all(c["ok"] for c in checks), "checks": checks}
    with _lock:
        _result = result
    return result


def cached() -> dict[str, Any] | None:
    """已探测完就给结果，没完给 None（healthz 用，不阻塞启动）。"""
    return _result


def probe_async() -> None:
    """后台探测：不拖慢启动，也不让自检牵连主进程。

    BILINOTES_SKIP_ENVCHECK=1 可整体关掉（排障时用，别写进用户文档）。
    """
    if os.environ.get("BILINOTES_SKIP_ENVCHECK") == "1":
        log.info("跳过环境自检（BILINOTES_SKIP_ENVCHECK=1）")
        return

    def work() -> None:
        try:
            probe()
        except Exception as exc:                      # 自检自己坏了不能拖垮程序
            log.warning("环境自检失败：%s", exc)

    threading.Thread(target=work, daemon=True).start()
