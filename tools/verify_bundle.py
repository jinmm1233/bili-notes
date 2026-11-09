"""打包产物一键验收。

用法：
    python tools/verify_bundle.py                 # 验 dist/BiliNotes
    python tools/verify_bundle.py --dist <目录>   # 验指定目录
    python tools/verify_bundle.py --quick         # 只查文件和探针，不起服务

为什么要有这个脚本：打包版出过两次「看起来打好了、给别人一跑就崩」的事故，
而且都不是 Python 层报错，是原生段错误，光看界面根本看不出来：
  1) 窗口版 stdout/stderr 为 None，print 就崩；
  2) 包内混进旧版 msvcp140.dll，onnxruntime 加载时 access violation。
所以这里既查文件齐全，也用 exe 自己的探针模式真的去加载重型库，
再起一次服务做接口冒烟。任一项 FAIL，退出码就是 1，别拿去发人。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
# 本机地址绝不走系统代理：开发环境里 HTTP_PROXY 指向 127.0.0.1:xxxxx 且 NO_PROXY
# 为空时，普通 urlopen 会把 /healthz 发给代理并拿到 502，整套冒烟会假失败。
LOCAL_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
VC_DLLS = (
    "msvcp140.dll", "MSVCP140_1.dll", "MSVCP140_2.dll",
    "vcruntime140.dll", "vcruntime140_1.dll", "concrt140.dll",
)
PROBES = ("ctranslate2", "onnxruntime", "ctranslate2,onnxruntime",
          "faster_whisper", "rapidocr_onnxruntime", "app.services.anchors")
REQUIRED = (
    "BiliNotes.exe", "BiliNotes-console.exe",
    "_internal/onnxruntime/capi/onnxruntime.dll",
    "_internal/ctranslate2/ctranslate2.dll",
    # onedir 模式下 datas/binaries 都在 _internal/ 下，不在 exe 同级
    "_internal/web/index.html", "_internal/web/app.js",
    "_internal/web/app.css", "_internal/web/offline.html",
    "_internal/bin/ffmpeg.exe", "_internal/bin/ffprobe.exe",
    "_internal/vendor",
)
MUST_200 = ("/healthz", "/api/env", "/", "/static/app.js", "/api/update/status")
OPTIONAL = ("/api/quiz", "/api/wordbooks/missing", "/api/library")

results: list[tuple[bool, str, str]] = []


def record(ok: bool, name: str, detail: str = "") -> bool:
    results.append((ok, name, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))
    return ok


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check_files(dist: Path) -> None:
    print("\n[1/5] 文件齐全")
    for rel in REQUIRED:
        p = dist / rel
        record(p.exists(), rel, f"{p.stat().st_size} bytes" if p.is_file() else "")

    py_dlls = list((dist / "_internal").glob("python3*.dll"))
    record(bool(py_dlls), "_internal/python3*.dll",
           ", ".join(p.name for p in py_dlls))

    # 导出 Word 的 vendor 目录不能是空的
    vendor = dist / "_internal" / "vendor"
    n = len(list(vendor.rglob("*.py"))) if vendor.is_dir() else 0
    record(n > 0, "vendor/ 含 Python 模块", f"{n} 个 .py")


def check_vc_runtime(dist: Path) -> None:
    print("\n[2/5] VC++ 运行库版本一致（旧版 msvcp140.dll 会让 onnxruntime 段错误）")
    system32 = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
    internal = dist / "_internal"
    for name in VC_DLLS:
        packed = internal / name
        if not packed.is_file():
            continue                      # 包里没有就交给系统/redist 提供
        system = system32 / name
        if not system.is_file():
            record(False, name, "系统里找不到，无法比对")
            continue
        same = sha(packed) == sha(system)
        record(same, name, "与系统版一致" if same else "★ 包内是旧副本，请重跑 build.spec")


def check_probes(dist: Path) -> None:
    print("\n[3/5] 冻结环境加载重型依赖（探针模式，rc 必须为 0）")
    exe = dist / "BiliNotes.exe"
    for mods in PROBES:
        env = dict(os.environ, BILINOTES_PROBE=mods, BILINOTES_CRASHLOG="0")
        env.pop("BILINOTES_WINDOW", None)
        try:
            proc = subprocess.run([str(exe)], env=env, cwd=str(dist), timeout=90,
                                  capture_output=True, creationflags=NO_WINDOW)
            rc = proc.returncode
        except subprocess.TimeoutExpired:
            rc = -999
        record(rc == 0, f"import {mods}", f"rc={rc}" + ("（超时）" if rc == -999 else ""))


def check_selftest(dist: Path) -> None:
    """功能自检：让冻出来的 exe 自己跑一遍音画锚定，证明新逻辑在真机里能用。"""
    print("\n[4/5] 功能自检（冻结环境跑音画锚定全链路）")
    exe = dist / "BiliNotes.exe"
    with tempfile.TemporaryDirectory(prefix="bn_selftest_") as tmp:
        out = Path(tmp) / "selftest.txt"
        env = dict(os.environ, BILINOTES_PROBE="bn_selftest",
                   BILINOTES_PROBE_OUT=str(out), BILINOTES_CRASHLOG="0")
        env.pop("BILINOTES_WINDOW", None)
        try:
            proc = subprocess.run([str(exe)], env=env, cwd=str(dist), timeout=180,
                                  capture_output=True, creationflags=NO_WINDOW)
            rc = proc.returncode
        except subprocess.TimeoutExpired:
            rc = -999
        detail = ""
        if out.is_file():
            detail = out.read_text(encoding="utf-8", errors="replace").strip()
        passed = rc == 0 and "全部通过" in detail
        record(passed, "冻结环境功能自检", f"rc={rc}" + (f"｜{detail.splitlines()[-1]}" if detail else ""))
        if not passed and detail:
            for line in detail.splitlines():
                if line.startswith("[FAIL]"):
                    print(f"      {line}")


def check_service(dist: Path) -> None:
    print("\n[5/5] 启动服务 + 接口冒烟（窗口关掉，避免弹窗干扰）")
    exe = dist / "BiliNotes-console.exe"
    env = dict(os.environ, BILINOTES_WINDOW="0", BILINOTES_OPEN_BROWSER="false")
    proc = subprocess.Popen([str(exe)], env=env, cwd=str(dist),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            creationflags=NO_WINDOW)
    base = "http://127.0.0.1:8765"
    ready = False
    try:
        for _ in range(80):                     # 最多等 20 秒
            try:
                with LOCAL_OPENER.open(base + "/healthz", timeout=1) as r:
                    if r.status == 200:
                        ready = True
                        break
            except Exception:
                time.sleep(0.25)
        record(ready, "服务启动并响应 /healthz")

        for path in MUST_200:
            try:
                with LOCAL_OPENER.open(base + path, timeout=8) as r:
                    body = r.read(4096)
                    record(r.status == 200, f"GET {path}", f"status={r.status} {len(body)}B")
            except Exception as exc:
                record(False, f"GET {path}", str(exc))

        # 版本号三处必须一致：不一致时内置更新会永远误判「有新版」或永远查不到
        versions: dict[str, str] = {}
        for path, key in (("/healthz", "healthz"),
                          ("/api/settings", "settings"),
                          ("/api/update/status", "update")):
            try:
                # /api/settings 首次会扫缓存目录（几百 MB 逐文件 stat，实测 4~6 秒），
                # 别用 8 秒把它误判成挂了；20 秒足够，且缓存命中后是毫秒级
                with LOCAL_OPENER.open(base + path, timeout=20) as r:
                    versions[key] = str(json.loads(r.read().decode("utf-8")).get("version", ""))
            except Exception as exc:
                versions[key] = f"ERR:{exc}"
        same = len(set(versions.values())) == 1 and all(v[:1].isdigit() for v in versions.values())
        record(same, "版本号三处一致", str(versions))

        # /api/env 会同步跑完自检，两个重型依赖必须都 ok
        try:
            with LOCAL_OPENER.open(base + "/api/env", timeout=180) as r:
                data = json.loads(r.read().decode("utf-8"))
            bad = [c["key"] for c in data.get("checks", []) if not c.get("ok")]
            record(data.get("ok") is True and not bad, "环境自检全部通过",
                   f"checks={[c['key'] for c in data.get('checks', [])]} bad={bad}")
        except Exception as exc:
            record(False, "环境自检全部通过", str(exc))

        for path in OPTIONAL:                  # 只记录，不算失败（可能要参数）
            try:
                with LOCAL_OPENER.open(base + path, timeout=8) as r:
                    print(f"  [INFO] GET {path} -> {r.status}")
            except urllib.error.HTTPError as exc:
                print(f"  [INFO] GET {path} -> {exc.code}")
            except Exception as exc:
                print(f"  [INFO] GET {path} -> {exc}")

        # ffmpeg 随包分发且能执行
        ff = dist / "_internal" / "bin" / "ffmpeg.exe"
        try:
            r = subprocess.run([str(ff), "-version"], capture_output=True, timeout=20,
                               creationflags=NO_WINDOW)
            record(r.returncode == 0, "ffmpeg 可执行", r.stdout.decode("utf-8", "replace").splitlines()[0][:80])
        except Exception as exc:
            record(False, "ffmpeg 可执行", str(exc))

        # 再稳 12 秒：历史上段错误都在启动后 15 秒左右发生
        print("  ... 观察 12 秒，确认进程没有静默崩溃")
        time.sleep(12)
        record(proc.poll() is None, "启动 12 秒后进程仍存活",
               f"poll={proc.poll()}")
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()


def main() -> int:
    ap = argparse.ArgumentParser(description="打包产物验收")
    ap.add_argument("--dist", default=str(ROOT / "dist" / "BiliNotes"))
    ap.add_argument("--quick", action="store_true", help="跳过起服务")
    args = ap.parse_args()
    dist = Path(args.dist).resolve()
    print(f"BiliNotes 打包验收  →  {dist}")
    if not (dist / "BiliNotes.exe").is_file():
        print("FAIL：找不到 BiliNotes.exe，先跑 python -m PyInstaller build.spec --noconfirm")
        return 1

    check_files(dist)
    check_vc_runtime(dist)
    check_probes(dist)
    check_selftest(dist)
    if not args.quick:
        check_service(dist)

    failed = [name for ok, name, _ in results if not ok]
    print("\n" + "=" * 60)
    if failed:
        print(f"验收不通过：{len(failed)} 项失败")
        for name in failed:
            print("  -", name)
        return 1
    print(f"验收通过：{len(results)} 项全绿，可以拿去装机。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
