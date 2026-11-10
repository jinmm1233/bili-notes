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
import re
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

    # **先**记住用户真实目录：下面 env 是给子进程用的隔离环境，
    # 一旦改了 os.environ["APPDATA"]，就再也找不到真实位置了。
    real_appdata = Path(os.environ.get("APPDATA", "")) / "BiliNotes"
    real_secrets = real_appdata / "secrets.dat"

    env = dict(os.environ)
    env["APPDATA"] = str(appdata)
    env["USERPROFILE"] = str(tmp)
    env["HOME"] = str(tmp)
    env["BILINOTES_WINDOW"] = "0"
    env["BILINOTES_OPEN_BROWSER"] = "false"
    env.pop("BILINOTES_SKIP_ENVCHECK", None)

    # 跑之前先拍用户真实目录的快照，结束后比对有没有被写
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

        # ---- 7. 每个功能页的接口都要能返回（不依赖任何本机状态）----
        # 这些接口就是界面上的各个页：总览、采集、队列、笔记库、知识卡、
        # 概念、自测、复习、设置。任一报 500 就说明那页在真机上是坏的。
        pages = [
            ("/api/overview", "总览"),
            ("/api/library", "笔记库"),
            ("/api/cards/stats", "知识卡统计"),
            ("/api/cards", "卡片列表"),
            ("/api/concepts", "概念索引"),
            ("/api/concepts/detail?name=%E5%8D%95%E4%BD%8D%E7%9F%A9%E9%98%B5", "概念详情"),
            ("/api/quiz", "自测卷"),
            ("/api/wordbooks", "词库"),
            ("/api/settings", "设置"),
            ("/api/env", "环境自检"),
            ("/api/update/status", "更新状态"),
        ]
        for path, label in pages:
            try:
                with LOCAL.open(base + path, timeout=30) as r:
                    body = r.read()
                    record(r.status == 200, f"功能页可用：{label}",
                           f"status={r.status} {len(body)}B")
            except urllib.error.HTTPError as exc:
                record(exc.code in (400, 404), f"功能页可用：{label}",
                       f"status={exc.code}")
            except Exception as exc:
                record(False, f"功能页可用：{label}", str(exc))

        # ---- 8. 概念页的新能力在真机上也在（没配模型也不该报错）----
        try:
            with LOCAL.open(base + "/api/concepts", timeout=30) as r:
                data = json.loads(r.read().decode("utf-8"))
            record("items" in data, "概念索引返回条目",
                   f"{data.get('total')} 条")
            record("unjudged" in data, "概念索引带未判释义计数",
                   str(data.get("unjudged")))
        except Exception as exc:
            record(False, "概念索引返回条目", str(exc))

        # ---- 8b. 笔记库要带出收藏夹分组（收藏夹折叠功能靠它）----
        try:
            with LOCAL.open(base + "/api/library", timeout=30) as r:
                data = json.loads(r.read().decode("utf-8"))
            record("collections" in data, "笔记库返回收藏夹分组",
                   f"{len(data.get('collections') or [])} 个")
            record("items" in data, "笔记库返回笔记列表")
        except Exception as exc:
            record(False, "笔记库返回收藏夹分组", str(exc))

        # ---- 9. 没配 Key 时，模型相关接口要给出明确提示而不是崩 ----
        try:
            req = urllib.request.Request(
                base + "/api/concepts/explain", data=b'{"names":[]}',
                headers={"Content-Type": "application/json"}, method="POST")
            with LOCAL.open(req, timeout=60) as r:
                record(r.status == 200, "无 Key 时补全释义接口不崩",
                       f"status={r.status}")
        except urllib.error.HTTPError as exc:
            # 400（没配 Key 的提示）也算活着
            record(exc.code in (400, 404), "无 Key 时补全释义接口不崩",
                   f"status={exc.code}")
        except Exception as exc:
            record(False, "无 Key 时补全释义接口不崩", str(exc))

        # ---- 10. 产物里不含任何本机数据（API Key、cookie、笔记、缓存）----
        # 这一条最要紧：安装包发给谁都不能带着别人的 Key 走。
        # cacert.pem 是 CA 根证书（certifi 自带，所有 HTTPS 都要），不算本机数据。
        leaks: list[str] = []
        for pattern in ("secrets.dat", "cookies.txt", "cards.json", "config.json",
                        "concept_explain.json", "concept_tags.json", "quizzes.json",
                        "*.key", "*.pem"):
            for hit in dist.rglob(pattern):
                if hit.name == "cacert.pem":
                    continue
                leaks.append(str(hit.relative_to(dist)))
        record(not leaks, "安装产物里没有任何本机数据文件",
               f"{len(leaks)} 个" + (f"：{leaks[:3]}" if leaks else ""))

        # ---- 11. 产物里搜不到你的 API Key 原文 ----
        # 拿真实密钥库里的值做特征串，在整个 dist 里扫一遍。
        secret_values: list[str] = []
        if real_secrets.is_file():
            try:
                # 必须把项目根加进 sys.path 才能 import app.*
                # （脚本是直接 python tools/xxx.py 跑的，根目录不在路径上；
                #  之前 import 失败被 except 吞掉，这项检查一直是空跑——）
                if str(ROOT) not in sys.path:
                    sys.path.insert(0, str(ROOT))
                from app.core.secretstore import SecretStore  # noqa: PLC0415
                st = SecretStore(real_secrets)
                for provider in ("deepseek", "zhipu", "qwen", "custom", "openai",
                                 "siliconflow", "moonshot", "ollama"):
                    val = st.get(provider)
                    if val and len(val) >= 12:
                        secret_values.append(val)
            except Exception as exc:
                print(f"  [warn] 读取密钥库失败：{type(exc).__name__}: {exc}")
        found = []
        if secret_values:
            for path in dist.rglob("*"):
                if not path.is_file() or path.suffix.lower() in (
                        ".exe", ".dll", ".pyd", ".png", ".jpg", ".ico", ".mp3", ".mp4"):
                    continue
                try:
                    text = path.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    continue
                for val in secret_values:
                    if val in text:
                        found.append(f"{path.relative_to(dist)}")
                        break
        record(not found, "安装产物里搜不到你的 API Key 原文",
               f"扫了 {len(secret_values)} 个密钥" +
               (f"，命中 {found[:3]}" if found else "，0 命中"))
        # 扫不到密钥就不能算通过——那说明这项检查根本没跑起来，
        # 等于白检查。密钥库不存在（没用过这台机器）才允许跳过。
        if real_secrets.is_file():
            record(bool(secret_values),
                   "密钥扫描确实跑起来了（不是空检查）",
                   f"读到 {len(secret_values)} 个密钥")

        # ---- 11b. 源码里没有指向本机的绝对路径（真机必须能跑）----
        # 写死 C:\Users\86159\... 的路径，换台电脑就全废了。
        hardcode = []
        for path in (dist / "_internal" / "app").rglob("*.py"):
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for m in re.finditer(r"[A-Za-z]:\\\\?Users\\\\?[^\\\\\"'\s]{1,40}", text):
                hardcode.append(f"{path.name}: {m.group(0)[:40]}")
        record(not hardcode, "包内代码没有写死本机用户目录",
               f"{len(hardcode)} 处" + (f"：{hardcode[:2]}" if hardcode else ""))

        # ---- 12. 没有污染用户真实目录（跑之前先拍快照）----
        # edge-profile/ 是 Edge 浏览器自己的缓存目录，不是 BiliNotes 的数据；
        # 而且 Edge 是独立进程，可能在服务退出后才落盘。这里不管它。
        # 注意：real_appdata 用的是第 80 行捕获的那个（真实路径），
        # 不要在这里重新从 os.environ 取——那时 APPDATA 已经被换成临时目录了。
        changed = []
        if real_appdata.is_dir():
            for path in real_appdata.rglob("*"):
                if not path.is_file():
                    continue
                rel = str(path.relative_to(real_appdata))
                if rel.startswith("edge-profile"):
                    continue
                before = real_snapshot.get(str(path))
                now = path.stat().st_mtime
                if before is None or now > before + 0.001:
                    changed.append(rel)
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
