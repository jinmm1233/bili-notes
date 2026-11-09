"""内置更新回归：版本比较、更新源、Release 解析、离线兜底、下载域名白名单。

跑法：python tests/smoke_update.py
全程不联网、不碰真实用户数据目录（配置与状态都落在临时目录）。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import __version__, __update_repo__ as DEFAULT_REPO  # noqa: E402
from app.core import updater as U  # noqa: E402

FAILED = 0


def ok(cond: bool, msg: str) -> None:
    global FAILED
    print(("[OK] " if cond else "[FAIL] ") + msg)
    if not cond:
        FAILED += 1


def reset() -> Path:
    """每个用例一份干净的用户目录。"""
    global DATA
    DATA = Path(tempfile.mkdtemp(prefix="bn_update_"))
    U.get_settings = lambda: SimpleNamespace(data_dir=DATA)
    os.environ.pop("BILINOTES_GITHUB_REPO", None)
    # 有些用例会临时清空打包默认值，每个用例都从真实默认状态开始
    U.__update_repo__ = DEFAULT_REPO
    with U._LOCK:
        U._DOWNLOAD.clear()
        U._DOWNLOAD.update({"state": "idle", "progress": 0, "received": 0,
                            "total": 0, "message": "", "path": "", "verified": False})
    return DATA


def test_version() -> None:
    ok(U.parse_version("v0.2.1") == (0, 2, 1), "v 前缀版本号")
    ok(U.parse_version("0.3") == (0, 3, 0), "两段版本号补 0")
    ok(U.parse_version("0.2.1-beta2") == (0, 2, 1), "带后缀版本号")
    ok(U.parse_version("不是版本号") == (0, 0, 0), "无法识别不误判为新版")
    # 别写死版本号：软件升到 0.2.2 后，0.2.1 就不再是"新版"了
    ok(U.is_newer("v0.2.1", "0.2.0"), "新版本识别（相对旧版）")
    ok(not U.is_newer("0.2.0"), "同版本不算更新")
    ok(not U.is_newer("0.1.9", "0.2.0"), "旧版本不算更新")
    ok(U.normalize_tag("V1.4.0") == "1.4.0", "tag 去 v 前缀")


def test_config() -> None:
    reset()
    ok(U.effective_repo() == DEFAULT_REPO and bool(DEFAULT_REPO),
       "打包时写死的仓库地址自动生效")
    # 「没配仓库」这条兜底仍要测：临时把默认值抹掉，模拟开发者没填的情况
    U.__update_repo__ = ""
    ok(U.effective_repo() == "", "清空默认值后确实没有更新源")
    ok(not U.check()["ok"] and U.check()["reason"] == "not_configured",
       "没配仓库时明确回报，不联网")
    U.__update_repo__ = DEFAULT_REPO

    cfg = U.save_config({"repo": "someone/BiliNotes", "auto_check": False})
    ok(cfg["repo"] == "someone/BiliNotes" and cfg["auto_check"] is False,
       "配置写入并回读")
    try:
        U.save_config({"repo": "https://evil.example/x.exe"})
        ok(False, "非法仓库地址应被拒绝")
    except ValueError:
        ok(True, "非法仓库地址被拒绝")
    try:
        U.save_config({"mirror": "ftp://x"})
        ok(False, "非法镜像地址应被拒绝")
    except ValueError:
        ok(True, "非法镜像地址被拒绝")

    os.environ["BILINOTES_GITHUB_REPO"] = "env/Repo"
    U.save_config({"repo": ""})
    ok(U.effective_repo() == "env/Repo", "环境变量可作为更新源")
    U.save_config({"repo": "ui/Repo"})
    ok(U.effective_repo() == "ui/Repo", "设置页优先于环境变量")

    s = U.status()
    ok(s["version"] == __version__ and s["configured"], "status 带当前版本与配置状态")
    ok("download" in s and s["download"]["state"] == "idle", "status 带下载状态")
    ok(isinstance(s["frozen"], bool), "status 区分源码/冻结运行")


def test_parse_release() -> None:
    sha = "a" * 64
    payload = {
        "tag_name": "v0.3.0",
        "published_at": "2026-11-09T00:00:00Z",
        "html_url": "https://github.com/someone/BiliNotes/releases/tag/v0.3.0",
        "body": f"修复若干问题\nSHA256: {sha}",
        "assets": [
            {"name": "BiliNotes-setup-0.3.0.exe", "size": 123456,
             "browser_download_url": "https://github.com/someone/BiliNotes/releases/download/v0.3.0/BiliNotes-setup-0.3.0.exe"},
            {"name": "notes.txt", "size": 10},
        ],
    }
    info = U.parse_release(payload, "someone/BiliNotes")
    ok(info["latest"] == "0.3.0", "解析 tag")
    ok(info["asset"]["name"].endswith(".exe"), "只挑 exe 安装包")
    ok("releases/download/v0.3.0/" in info["asset"]["url"], "生成直链而非 API 地址")
    ok(info["asset"]["sha256"] == sha, "从说明里提取 SHA256")
    ok(U.is_newer(info["latest"]), "解析出来的版本能比对")

    empty = U.parse_release({"tag_name": "v0.3.0", "assets": []}, "a/b")
    ok(empty["asset"] == {}, "没有 exe 时 asset 为空（不会误报可更新）")


def test_check_cached_and_network_silent() -> None:
    d = reset()
    U.save_config({"repo": "someone/BiliNotes"})
    (d / "update_state.json").write_text(json.dumps({
        "checked_at": time.time(), "latest": "9.9.9", "has_update": False,
        "notes": "", "asset": {},
    }), encoding="utf-8")

    called = []

    def _boom(*a, **kw):
        called.append(1)
        raise AssertionError("不该联网")

    old = U.urllib.request.urlopen
    U.urllib.request.urlopen = _boom
    try:
        r = U.check(force=False)
    finally:
        U.urllib.request.urlopen = old
    ok(r.get("cached") is True and not called, "12 小时内复用缓存，不联网")

    old = U.urllib.request.urlopen
    U.urllib.request.urlopen = lambda *a, **kw: (_ for _ in ()).throw(OSError("dns fail"))
    try:
        r = U.check(force=True)
    finally:
        U.urllib.request.urlopen = old
    ok(r["ok"] is False and r["reason"] == "network" and "不影响使用" in r["message"],
       "网络失败静默降级，不抛异常")


def test_download_guard() -> None:
    d = reset()
    U.save_config({"repo": "someone/BiliNotes"})
    (d / "update_state.json").write_text(json.dumps({
        "checked_at": time.time(), "has_update": True, "latest": "0.3.0",
        "asset": {"name": "x.exe", "size": 1, "url": "http://evil.example/x.exe"},
    }), encoding="utf-8")
    r = U.start_download()
    ok(r["ok"] is False, "拒绝非白名单域名的下载地址")
    with U._LOCK:
        ok(U._DOWNLOAD["state"] == "idle", "被拒绝时不启动下载线程")

    ok(U._url_allowed("https://github.com/a/b/releases/download/v1/x.exe", U.get_config()),
       "放行 GitHub 官方域名")
    ok(U._url_allowed("https://objects.githubusercontent.com/x", U.get_config()),
       "放行 GitHub 资源域名")
    ok(not U._url_allowed("https://evil.example/x.exe", U.get_config()),
       "拦截其他域名")
    ok(U.install()["ok"] is False, "没下载好时不启动安装器")


def test_resume_after_restart() -> None:
    d = reset()
    U.save_config({"repo": "someone/BiliNotes"})
    folder = d / "updates"
    folder.mkdir(parents=True, exist_ok=True)
    pkg = folder / "BiliNotes-setup-0.3.0.exe"
    pkg.write_bytes(b"fake installer")
    (d / "update_state.json").write_text(json.dumps({
        "checked_at": time.time(), "has_update": True, "latest": "0.3.0",
        "asset": {"name": pkg.name, "size": 14, "url": "https://github.com/a/b/releases/download/v0.3.0/x.exe"},
        "download_cache": {"path": str(pkg), "name": pkg.name, "verified": True},
    }), encoding="utf-8")
    s = U.status()
    ok(s["download"]["state"] == "done", "重启后认出已下载的安装包")
    ok(s["download"]["path"] == str(pkg) and s["download"]["verified"] is True,
       "恢复路径与校验状态")

    # 换成另一个 Release 的资产后，旧包不能被误认
    (d / "update_state.json").write_text(json.dumps({
        "checked_at": time.time(), "has_update": True, "latest": "0.4.0",
        "asset": {"name": "BiliNotes-setup-0.4.0.exe", "size": 1, "url": "https://github.com/a/b/x.exe"},
        "download_cache": {"path": str(pkg), "name": pkg.name, "verified": True},
    }), encoding="utf-8")
    reset_download_state()
    ok(U.status()["download"]["state"] == "idle", "新版本资产名变化时旧包不复用")


def reset_download_state() -> None:
    with U._LOCK:
        U._DOWNLOAD.clear()
        U._DOWNLOAD.update({"state": "idle", "progress": 0, "received": 0,
                            "total": 0, "message": "", "path": "", "verified": False})


def main() -> int:
    test_version()
    test_config()
    test_parse_release()
    test_check_cached_and_network_silent()
    test_download_guard()
    test_resume_after_restart()
    print(f"\n{'全部通过' if FAILED == 0 else str(FAILED) + ' 项失败'}")
    return FAILED


if __name__ == "__main__":
    sys.exit(main())
