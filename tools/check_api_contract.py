"""前后端接口契约检查：前端点了的接口，后端是不是真的都有。

为什么需要：新机上最隐蔽的故障是「界面按钮在、点了报 404」——前端写错路径
或后端漏注册路由，肉眼扫代码很难发现，等到用户点才发现。

怎么查：
  1. 用临时 APPDATA 起一个服务（不碰用户数据、不联网）
  2. 拉 /openapi.json，拿后端**真实**的路由表和方法
  3. 从 web/*.js 里抽出所有 api(...) 调用（路径 + method）
  4. 逐条比对：路径在不在、方法对不对

跑法：python tools/check_api_contract.py
退出码 0 = 契约一致；1 = 有对不上的（附清单）。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
LOCAL = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# api('/api/x', { method: 'POST' })  —— 路径与方法一起抓
CALL_RE = re.compile(
    r"api\(\s*[`'\"](?P<path>[^`'\"]+)[`'\"]\s*(?:,\s*\{(?P<opts>[^}]*)\})?",
    re.S,
)
METHOD_RE = re.compile(r"method:\s*['\"](?P<m>[A-Z]+)['\"]")


def norm(path: str) -> str:
    """把前端路径归一化成后端 openapi 的写法。"""
    path = path.split("?")[0]
    path = re.sub(r"\$\{[^}]*\}", "{x}", path)
    path = re.sub(r"\{[^}]*\}", "{x}", path)
    if not path.startswith("/"):
        path = "/" + path
    return path.rstrip("/") or path


# <a href="/api/offline/export"> 这种直接指向接口的入口
HREF_RE = re.compile(r"""(?:href|action)\s*=\s*["'](?P<path>/api/[^"']+)["']""")
# 裸 fetch / URL 模板里出现的接口路径（不经过 api() 封装的那种）
RAW_RE = re.compile(r"""["'`](?P<path>/api/[^"'`\s]*)["'`]""")


def front_calls() -> list[tuple[str, str]]:
    """扫前端所有指向 /api 的入口：js 里的 api(...) 调用 + html 里的 href。"""
    calls: list[tuple[str, str]] = []
    files = sorted(WEB.glob("*.js")) + sorted(WEB.glob("*.html"))
    for path in files:
        text = path.read_text(encoding="utf-8", errors="replace")
        for m in CALL_RE.finditer(text):
            raw = m.group("path")
            if not raw.startswith("/api"):
                continue
            opts = m.group("opts") or ""
            mm = METHOD_RE.search(opts)
            calls.append((norm(raw), mm.group("m") if mm else "GET"))
        for m in HREF_RE.finditer(text):
            calls.append((norm(m.group("path")), "GET"))
        for m in RAW_RE.finditer(text):
            calls.append((norm(m.group("path")), "?"))
    return calls


def wait_openapi(timeout: float = 40.0) -> tuple[dict | None, str]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        for port in range(8765, 8790):
            try:
                with LOCAL.open(f"http://127.0.0.1:{port}/openapi.json", timeout=1) as r:
                    if r.status == 200:
                        return json.loads(r.read().decode("utf-8")), f"127.0.0.1:{port}"
            except Exception:
                continue
        time.sleep(0.4)
    return None, ""


def main() -> int:
    calls = front_calls()
    print(f"前端接口调用 {len(calls)} 处（去重前）")

    tmp = Path(tempfile.mkdtemp(prefix="bn_contract_"))
    env = dict(os.environ)
    env["APPDATA"] = str(tmp / "AppData" / "Roaming")
    env["USERPROFILE"] = str(tmp)
    env["HOME"] = str(tmp)
    env["BILINOTES_WINDOW"] = "0"
    env["BILINOTES_OPEN_BROWSER"] = "false"

    proc = subprocess.Popen([sys.executable, "-m", "app.main"], env=env, cwd=str(ROOT),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            creationflags=NO_WINDOW)
    try:
        spec, base = wait_openapi()
        if spec is None:
            print("FAIL：服务没起来，取不到 openapi.json")
            return 1
        print(f"后端路由来自 {base}\n")

        routes: dict[str, set[str]] = {}
        for path, item in (spec.get("paths") or {}).items():
            routes.setdefault(norm(path), set()).update(
                m.upper() for m in item if m.lower() in
                {"get", "post", "put", "delete", "patch"}
            )

        missing: list[str] = []
        wrong_method: list[str] = []
        def matches(path: str) -> bool:
            """前端路径可能比后端少一段（/file/xxx、/file 这类前缀）。"""
            if path in routes:
                return True
            return any(r.startswith(path + "/") or path.startswith(r + "/")
                       for r in routes)

        for path, method in sorted(set(calls)):
            if not matches(path):
                missing.append(f"{method} {path}")
            elif path not in routes:
                pass
            elif method not in ("?", "GET") and method not in routes[path]:
                wrong_method.append(
                    f"{method} {path}（后端只有 {sorted(routes[path])}）")

        print(f"后端路由 {len(routes)} 条")
        if missing:
            print(f"\n[FAIL] 前端调用了但后端没有：{len(missing)} 处")
            for x in missing:
                print("   -", x)
        else:
            print("\n[OK] 前端调用的路径后端都有")
        if wrong_method:
            print(f"\n[FAIL] 路径在但方法对不上：{len(wrong_method)} 处")
            for x in wrong_method:
                print("   -", x)
        else:
            print("[OK] 方法全部对得上")

        used_paths = {c for c, _ in calls}
        unused = [p for p in routes if p not in used_paths]
        if unused:
            print(f"\n[INFO] 后端有、前端暂时没调用的路由 {len(unused)} 条：")
            for p in sorted(unused)[:20]:
                print("   -", p, sorted(routes[p]))

        return 1 if (missing or wrong_method) else 0
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
