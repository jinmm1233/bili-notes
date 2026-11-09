"""新机首次运行模拟：验证打包产物在「全新用户」环境下能起来。

为什么要有这个：新机上的用户目录里没有任何配置、密钥、模型、缓存，
所有分支都是第一次走。历史上出过的几类事故都在这条路径上——
配置读不到、目录建不出来、端口被占、单实例判定错、依赖没加载。

怎么模拟新机：
  - APPDATA 指向临时目录 → %APPDATA%\\BiliNotes 是全新的
  - USERPROFILE/HOME 指向临时目录 → 「我的文档」是全新的
  - BILINOTES_WINDOW=0 → 不弹 Edge 窗口（CI/无人值守）

跑法：python tools/verify_fresh_install.py [--dist dist/BiliNotes]
不联网、不写用户真实目录，跑完自动清理临时目录。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
LOCAL = urllib.request.build_opener(urllib.request.ProxyHandler({}))
results: list[tuple[bool, str, str]] = []


def record(ok: bool, name: str, detail: str = "") -> bool:
    results.append((ok, name, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))
    return ok


def wait_health(base: str, timeout: float = 40.0) -> tuple[bool, int]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with LOCAL.open(base + "/healthz", timeout=1) as r:
                if r.status == 200:
                    return True, r.status
        except Exception:
            time.sleep(0.3)
    return False, 0


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dist", default=str(ROOT / "dist" / "BiliNotes"))
    args = ap.parse_args()
    dist = Path(args.dist).resolve()
    exe = dist / "BiliNotes-console.exe"
    print(f"新机首次运行模拟  →  {dist}")
    if not exe.is_file():
        print(f"FAIL：找不到 {exe}")
        return 1

    tmp = Path(tempfile.mkdtemp(prefix="bn_fresh_"))
    appdata = tmp / "AppData" / "Roaming"
    docs = tmp / "Documents"
    appdata.mkdir(parents=True, exist_ok=True)
    docs.mkdir(parents=True, exist_ok=True)

    env = dict(os.environ)
    env["APPDATA"] = str(appdata)
    env["USERPROFILE"] = str(tmp)
    env["HOME"] = str(tmp)
    env["BILINOTES_WINDOW"] = "0"
    env["BILINOTES_OPEN_BROWSER"] = "false"
    env.pop("BILINOTES_SKIP_ENVCHECK", None)

    # 跑之前先拍用户真实目录的快照，结束后比对有没有被写
    real_appdata = Path(os.environ.get("APPDATA", "")) / "BiliNotes"
    real_snapshot: dict[str, float] = {}
    if real_appdata.is_dir():
        for path in real_appdata.rglob("*"):
            if path.is_file():
                real_snapshot[str(path)] = path.stat().st_mtime

    proc = None
    try:
        # ---- 1. 全新环境启动 ----
        proc = subprocess.Popen([str(exe)], env=env, cwd=str(dist),
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                creationflags=NO_WINDOW)
        base = "http://127.0.0.1:8765"
        ready, _ = wait_health(base)
        if not ready:
            # 端口被占时会自动换端口，挨个试
            for port in (8765, 8766, 8767, 8768, 8769):
                if proc.poll() is not None:
                    break
                ok, _ = wait_health(f"http://127.0.0.1:{port}", timeout=3)
                if ok:
                    base = f"http://127.0.0.1:{port}"
                    break
        record(ready, "全新环境下服务启动并响应 /healthz", base)

        # ---- 2. 数据目录自动建出来 ----
        user_dir = appdata / "BiliNotes"
        record(user_dir.is_dir(), "%APPDATA%\\BiliNotes 自动创建",
               str(user_dir))
        record((user_dir / "logs").is_dir(), "日志目录创建")
        record((user_dir / "cache").is_dir(), "缓存目录创建")

        # ---- 3. 「我的文档」下的笔记目录 ----
        record((docs / "BiliNotes").is_dir(), "笔记输出目录创建",
               str(docs / "BiliNotes"))

        # ---- 4. 首次访问的接口都要通（不需要任何配置）----
        for path in ("/api/settings", "/api/library", "/api/env",
                     "/api/update/status", "/api/wordbooks"):
            try:
                with LOCAL.open(base + path, timeout=30) as r:
                    body = r.read(2048)
                    record(r.status == 200, f"GET {path}",
                           f"status={r.status} {len(body)}B")
            except urllib.error.HTTPError as exc:
                # 400/404 也算「接口活着」，只是没数据
                record(exc.code in (400, 404), f"GET {path}", f"status={exc.code}")
            except Exception as exc:
                record(False, f"GET {path}", str(exc))

        # ---- 5. 设置页返回的默认值应该是「什么都没配」----
        try:
            with LOCAL.open(base + "/api/settings", timeout=30) as r:
                data = json.loads(r.read().decode("utf-8"))
            record(data.get("note_detail") in ("brief", "standard", "detailed"),
                   "详细度档位有默认值", str(data.get("note_detail")))
            record(data.get("llm_provider") not in (None, ""), "默认模型厂商已选",
                   str(data.get("llm_provider")))
            record(data.get("version") not in (None, ""), "版本号可读",
                   str(data.get("version")))
        except Exception as exc:
            record(False, "设置页默认配置可读", str(exc))
        try:
            with LOCAL.open(base + "/api/providers", timeout=30) as r:
                data = json.loads(r.read().decode("utf-8"))
            providers = data.get("providers") or []
            record(bool(providers), "可选模型列表非空", f"{len(providers)} 个")
        except Exception as exc:
            record(False, "可选模型列表非空", str(exc))

        # ---- 6. 单实例：再起一个应该自己退出，不抢端口 ----
        second = subprocess.Popen([str(exe)], env=env, cwd=str(dist),
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                  creationflags=NO_WINDOW)
        time.sleep(6)
        record(second.poll() is not None, "第二个实例自行退出（单实例生效）",
               f"poll={second.poll()}")
        if second.poll() is None:
            second.terminate()
        record(proc.poll() is None, "第一个实例仍在运行", f"poll={proc.poll()}")

        # ---- 7. 没有污染用户真实目录（跑之前先拍快照）----
        real_appdata = Path(os.environ.get("APPDATA", "")) / "BiliNotes"
        changed = []
        if real_appdata.is_dir():
            for path in real_appdata.rglob("*"):
                if not path.is_file():
                    continue
                before = real_snapshot.get(str(path))
                now = path.stat().st_mtime
                if before is None or now > before + 0.001:
                    changed.append(str(path.relative_to(real_appdata)))
        record(not changed, "没有写进用户真实目录",
               f"新增/改动 {len(changed)} 个" + (f"：{changed[:3]}" if changed else ""))

    finally:
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        shutil.rmtree(tmp, ignore_errors=True)

    failed = [name for ok, name, _ in results if not ok]
    print("\n" + "=" * 60)
    if failed:
        print(f"新机模拟不通过：{len(failed)} 项失败")
        for name in failed:
            print("  -", name)
        return 1
    print(f"新机模拟通过：{len(results)} 项全绿。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
