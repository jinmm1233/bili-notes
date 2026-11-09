"""内置更新：从 GitHub Releases 检查 / 下载 / 拉起安装器。

设计约束（都是明确要防住的坑）：
  * 网络失败必须静默：国内访问 api.github.com 经常不通，绝不能拖慢启动、
    弹错误框，也不能把 /api/update/status 弄成 500。检查只在用户点按钮
    或超过 12 小时才真的联网。
  * 仓库地址三处来源，优先级：设置页写入的 update.json > 环境变量
    BILINOTES_GITHUB_REPO > 打包时写死的 app.__update_repo__。没配就不联网。
  * 只从 GitHub 官方域名（或用户自己配的镜像）下载，防止配置被改成任意 URL。
  * 安装器要覆盖 Program Files 里的 exe，所以启动前必须让主程序退出，
    否则撞文件占用（iss 里那道「正在运行就拦住」也会弹框）。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from .. import __update_repo__, __version__
from .config import get_settings

log = logging.getLogger(__name__)

API_ROOT = "https://api.github.com"
TIMEOUT = 6                  # 连不上就尽快放弃，别让用户盯着转圈
CHECK_INTERVAL = 12 * 3600   # 自动检查最多每 12 小时一次
MAX_NOTES = 2000
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_SHA_RE = re.compile(r"\b[0-9a-fA-F]{64}\b")
_ALLOWED_HOSTS = {
    "github.com", "api.github.com",
    "objects.githubusercontent.com", "release-assets.githubusercontent.com",
}

_LOCK = threading.Lock()
_DOWNLOAD: dict[str, Any] = {
    "state": "idle",       # idle | running | done | error
    "progress": 0,
    "received": 0,
    "total": 0,
    "message": "",
    "path": "",
    "verified": False,
}


# ---------------------------------------------------------------- 配置 / 状态文件
def _config_file() -> Path:
    return get_settings().data_dir / "update.json"


def _state_file() -> Path:
    return get_settings().data_dir / "update_state.json"


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_json(path: Path, data: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        log.debug("写 %s 失败", path.name, exc_info=True)


def get_config() -> dict:
    cfg = _read_json(_config_file())
    return {
        "repo": str(cfg.get("repo") or "").strip(),
        "mirror": str(cfg.get("mirror") or "").strip().rstrip("/"),
        "auto_check": cfg.get("auto_check") is not False,
    }


def save_config(updates: dict) -> dict:
    """只收白名单字段；repo 格式不合法直接拒绝，别把拼错的地址存进去。"""
    cfg = get_config()
    if "repo" in updates:
        repo = str(updates["repo"] or "").strip()
        if repo and not _REPO_RE.fullmatch(repo):
            raise ValueError("仓库地址要写成 用户名/仓库名，例如 someone/BiliNotes")
        cfg["repo"] = repo
    if "mirror" in updates:
        mirror = str(updates["mirror"] or "").strip().rstrip("/")
        if mirror and not mirror.startswith(("http://", "https://")):
            raise ValueError("镜像地址要以 https:// 开头")
        cfg["mirror"] = mirror
    if "auto_check" in updates:
        cfg["auto_check"] = bool(updates["auto_check"])
    _write_json(_config_file(), cfg)
    return cfg


def effective_repo() -> str:
    """设置页 > 环境变量 > 打包时写死的默认值。"""
    return (get_config()["repo"]
            or os.environ.get("BILINOTES_GITHUB_REPO", "").strip()
            or __update_repo__)


def _mirror_host(cfg: dict) -> str:
    try:
        return (urllib.parse.urlparse(cfg["mirror"]).hostname or "").lower()
    except ValueError:
        return ""


def _with_mirror(url: str, cfg: dict) -> str:
    return f"{cfg['mirror']}/{url}" if cfg.get("mirror") else url


def _url_allowed(url: str, cfg: dict) -> bool:
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    mirror = _mirror_host(cfg)
    return host in _ALLOWED_HOSTS or bool(mirror and host == mirror)


# ---------------------------------------------------------------- 版本号
def parse_version(text: str) -> tuple[int, int, int]:
    """`v0.2.1` / `0.2.1-beta` → (0, 2, 1)；认不出来返回 (0,0,0)。"""
    m = re.match(r"^[vV]?(\d+)\.(\d+)(?:\.(\d+))?", (text or "").strip())
    if not m:
        return (0, 0, 0)
    return (int(m.group(1)), int(m.group(2)), int(m.group(3) or 0))


def is_newer(latest: str, current: str = __version__) -> bool:
    return parse_version(latest) > parse_version(current)


def normalize_tag(tag: str) -> str:
    return (tag or "").strip().lstrip("vV")


# ---------------------------------------------------------------- Release 解析
def parse_release(payload: dict, repo: str) -> dict:
    """把 GitHub 的 release JSON 收成前端要的那一小撮字段。"""
    tag = normalize_tag(str(payload.get("tag_name") or ""))
    assets = [a for a in (payload.get("assets") or []) if isinstance(a, dict)]
    exes = [a for a in assets
            if str(a.get("name") or "").lower().endswith(".exe")]

    def _exe_rank(a: dict) -> tuple[int, str]:
        name = str(a.get("name") or "").lower()
        # Release 里可能同时挂着 vc_redist 这类运行库，别把它当主安装包
        if "bili" in name and "setup" in name:
            return 0, name
        if "setup" in name:
            return 1, name
        if "bili" in name:
            return 2, name
        return 3, name

    exe = min(exes, key=_exe_rank) if exes else None
    sum_asset = next((a for a in assets
                      if re.search(r"(sha256|\.sums?$|checksums)",
                                  str(a.get("name") or ""), re.I)), None)
    body = str(payload.get("body") or "")
    sha = ""
    found = _SHA_RE.search(body)
    if found:
        sha = found.group(0).lower()

    asset: dict[str, Any] = {}
    if exe:
        name = str(exe.get("name") or "")
        # 用 releases/download 直链而不是 browser_download_url：便于套镜像前缀
        asset = {
            "name": name,
            "size": int(exe.get("size") or 0),
            "url": f"https://github.com/{repo}/releases/download/{payload.get('tag_name') or tag}/{urllib.parse.quote(name)}",
            "sha256": sha,
            "sum_url": str(sum_asset.get("browser_download_url") or "") if sum_asset else "",
        }
    return {
        "latest": tag,
        "published_at": str(payload.get("published_at") or ""),
        "release_url": str(payload.get("html_url") or ""),
        "notes": body[:MAX_NOTES],
        "asset": asset,
    }


def check(force: bool = False) -> dict:
    """查最新版。任何失败都返回 ok=False + 人话提示，绝不往外抛异常。"""
    repo, cfg = effective_repo(), get_config()
    if not repo:
        return {"ok": False, "reason": "not_configured",
                "message": "还没配置 GitHub 仓库，先在下面填 用户名/仓库名"}
    state = _read_json(_state_file())
    checked_at = float(state.get("checked_at") or 0)
    if (not force and checked_at
            and time.time() - checked_at < CHECK_INTERVAL
            and state.get("latest")):
        return {"ok": True, "cached": True, "message": "用的是上次检查结果"}

    url = _with_mirror(f"{API_ROOT}/repos/{repo}/releases/latest", cfg)
    req = urllib.request.Request(url, headers={
        "User-Agent": f"BiliNotes/{__version__}",
        "Accept": "application/vnd.github+json",
    })
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        # 填错仓库名 / 还没发 Release 是最常见的两种，给能动手改的提示
        log.info("检查更新返回 %s（不阻断使用）：%s", exc.code, repo)
        if exc.code == 404:
            return {"ok": False, "reason": "not_found",
                    "message": f"没找到 {repo} 的正式 Release：检查用户名/仓库名，或先发布一个 Release"}
        if exc.code in (403, 429):
            return {"ok": False, "reason": "rate_limited",
                    "message": "GitHub 接口暂时限流了，稍后再试；不影响使用"}
        return {"ok": False, "reason": "network",
                "message": f"GitHub 返回 {exc.code}，不影响使用，稍后再试"}
    except Exception as exc:                       # 超时 / DNS 全都不吓人
        log.info("检查更新失败（不阻断使用）：%s", exc)
        return {"ok": False, "reason": "network",
                "message": f"暂时连不上 GitHub（{type(exc).__name__}）——不影响使用，稍后再试"}

    if not isinstance(payload, dict) or not payload.get("tag_name"):
        return {"ok": False, "reason": "empty",
                "message": "仓库里还没有正式发布的 Release"}

    info = parse_release(payload, repo)
    info["checked_at"] = time.time()
    info["has_update"] = is_newer(info["latest"]) and bool(info["asset"])
    # 同一个 Release 重复检查时，别把「已下载好」的缓存记录冲掉
    old = _read_json(_state_file())
    cached = old.get("download_cache") or {}
    if cached.get("name") and cached.get("name") == (info.get("asset") or {}).get("name"):
        info["download_cache"] = cached
    _write_json(_state_file(), info)
    return {"ok": True, "cached": False, **info}


def status() -> dict:
    """前端轮询用：一次给全，不联网。"""
    state = _read_json(_state_file())
    cfg = get_config()
    with _LOCK:
        dl = dict(_DOWNLOAD)
        if dl.get("state") == "idle":
            # 上次下好但没来得及装（关了程序）：包还在磁盘上，别让用户重下 200MB。
            # 只有文件名和当前 Release 的资产对得上才认，避免拿旧包去装新版。
            cache = state.get("download_cache") or {}
            cached = Path(str(cache.get("path") or ""))
            asset_name = str((state.get("asset") or {}).get("name") or "")
            if cache and asset_name and cached.is_file() and cached.name == asset_name:
                dl.update({
                    "state": "done", "progress": 100,
                    "path": str(cached), "verified": bool(cache.get("verified")),
                    "message": "沿用上次下载的安装包",
                })
                _DOWNLOAD.update(dl)      # install() 读的是模块级状态，一并恢复
    return {
        "version": __version__,
        "repo": effective_repo(),
        "configured": bool(effective_repo()),
        "mirror": cfg["mirror"],
        "auto_check": cfg["auto_check"],
        "last_checked": int(state.get("checked_at") or 0),
        "has_update": bool(state.get("has_update")),
        "latest": state.get("latest", ""),
        "notes": state.get("notes", ""),
        "release_url": state.get("release_url", ""),
        "asset": state.get("asset") or {},
        "download": dl,
        "frozen": bool(getattr(sys, "frozen", False)),
    }


# ---------------------------------------------------------------- 下载
def _updates_dir() -> Path:
    return get_settings().data_dir / "updates"


def _cleanup_old(keep: Path, keep_part: Path) -> None:
    """只留当前这一个安装包，别让 updates/ 越攒越大（卸载器不碰这个目录）。

    keep_part 是当前资产的半截下载：断点续传还要接着写，不能当垃圾清掉。
    """
    folder = _updates_dir()
    for p in folder.glob("*"):
        if p in (keep, keep_part):
            continue
        if p.is_file() and p.suffix.lower() in (".exe", ".part", ".tmp"):
            try:
                p.unlink()
            except OSError:
                pass


def start_download() -> dict:
    state = _read_json(_state_file())
    asset = state.get("asset") or {}
    url = str(asset.get("url") or "")
    cfg = get_config()
    if not url or not _url_allowed(url, cfg):
        return {"ok": False, "message": "没有可下载的安装包，或下载地址不在允许的域名"}
    with _LOCK:
        if _DOWNLOAD.get("state") == "running":
            return {"ok": True, "started": False, "message": "已经在下载了"}
        _DOWNLOAD.clear()
        _DOWNLOAD.update({
            "state": "running", "progress": 0, "received": 0,
            "total": int(asset.get("size") or 0), "message": "",
            "path": "", "verified": False,
        })
    threading.Thread(target=_download_worker, args=(url, asset, cfg),
                     daemon=True).start()
    return {"ok": True, "started": True}


def _sha_from_sums(url: str, name: str, cfg: dict) -> str:
    """Release 说明里没贴哈希时，去 SHA256SUMS.txt 里找对应文件的那一行。"""
    if not url or not _url_allowed(url, cfg):
        return ""
    try:
        req = urllib.request.Request(_with_mirror(url, cfg), headers={
            "User-Agent": f"BiliNotes/{__version__}",
        })
        with urllib.request.urlopen(req, timeout=8) as resp:
            text = resp.read(256 * 1024).decode("utf-8", "replace")
    except Exception as exc:                       # 校验文件拿不到也不能挡住更新
        log.info("读取校验文件失败（跳过校验）：%s", exc)
        return ""
    target = name.lower()
    for line in text.splitlines():
        m = re.match(r"^\s*([0-9a-fA-F]{64})\s+\*?(.+?)\s*$", line)
        if not m:
            continue
        filename = m.group(2).strip().lstrip("*").replace("\\", "/").split("/")[-1]
        if filename.lower() == target:
            return m.group(1).lower()
    log.info("校验文件里没有 %s 的记录", name)
    return ""


def _download_worker(url: str, asset: dict, cfg: dict) -> None:
    folder = _updates_dir()
    name = str(asset.get("name") or "BiliNotes-setup.exe")
    final = folder / name
    part = folder / (name + ".part")
    try:
        folder.mkdir(parents=True, exist_ok=True)
        _cleanup_old(final, part)

        # 校验值两个来源：Release 说明里的 64 位哈希 > SHA256SUMS.txt。
        # 以前只认前者，后者虽解析出 sum_url 却从没读过，等于没校验。
        expected = str(asset.get("sha256") or "").lower()
        if not expected:
            expected = _sha_from_sums(str(asset.get("sum_url") or ""), name, cfg)

        total = int(asset.get("size") or 0)
        resume = part.stat().st_size if part.is_file() else 0
        if total and resume >= total:              # 残留的坏文件，重头下
            resume = 0
            part.unlink(missing_ok=True)
        headers = {"User-Agent": f"BiliNotes/{__version__}"}
        if resume:
            headers["Range"] = f"bytes={resume}-"
        req = urllib.request.Request(_with_mirror(url, cfg), headers=headers)
        with urllib.request.urlopen(req, timeout=30) as resp:
            status = int(getattr(resp, "status", 200) or 200)
            content_range = str(resp.headers.get("Content-Range") or "")
            appending = status == 206 and content_range.startswith(f"bytes {resume}-")
            if not appending:
                # 服务端忽略了 Range：必须从头写，否则两份内容会拼在一起
                resume = 0
                part.unlink(missing_ok=True)
            length = int(resp.headers.get("Content-Length") or 0)
            total = total or (resume + length if appending else length)
            with part.open("ab" if appending else "wb") as f:
                received = resume
                while True:
                    chunk = resp.read(256 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
                    received += len(chunk)
                    with _LOCK:
                        _DOWNLOAD.update({
                            "received": received, "total": total,
                            "progress": min(99, int(received * 100 / total)) if total else 0,
                        })
        # 续传时不能只算新增字节：整个文件重读一遍算哈希，代价远小于重下 210MB
        digest = hashlib.sha256()
        with part.open("rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                digest.update(chunk)
        verified = bool(expected) and digest.hexdigest().lower() == expected
        if expected and not verified:
            part.unlink(missing_ok=True)
            raise ValueError("安装包校验不通过（SHA256 不一致），已删除，请重新下载")
        part.replace(final)
        with _LOCK:
            _DOWNLOAD.update({
                "state": "done", "progress": 100, "received": received,
                "path": str(final), "verified": verified,
                "message": ("" if expected else "该 Release 没提供校验值，已跳过 SHA256 校验"),
            })
        # 记到状态文件，重启后 status() 能认出这个包，不必重下
        saved = _read_json(_state_file())
        saved["download_cache"] = {"path": str(final), "name": name,
                                   "verified": verified}
        _write_json(_state_file(), saved)
        log.info("更新包下载完成：%s（SHA256 校验=%s）", final,
                 "通过" if verified else "未提供校验值")
    except Exception as exc:
        log.warning("下载更新包失败：%s", exc)
        try:
            part.unlink(missing_ok=True)
        except OSError:
            pass
        with _LOCK:
            _DOWNLOAD.update({"state": "error", "message": f"下载失败：{exc}"})


# ---------------------------------------------------------------- 安装
def install() -> dict:
    """拉起安装器，然后让本程序退出——不退出会撞文件占用。"""
    with _LOCK:
        path = Path(_DOWNLOAD.get("path") or "")
        state = _DOWNLOAD.get("state")
    if state != "done" or not path.is_file():
        return {"ok": False, "message": "安装包还没下好，先点「下载更新」"}
    try:
        subprocess.Popen([str(path)], cwd=str(path.parent), close_fds=True)
    except OSError as exc:
        return {"ok": False, "message": f"启动安装程序失败：{exc}"}

    def _leave() -> None:
        # 先关掉 Edge 应用窗口（window.py 轮询窗口存活，不用 proc.wait()），
        # 兜底再强退一次，保证安装器不会撞上被占用的 BiliNotes.exe。
        try:
            from .. import window  # noqa: PLC0415  避免 window <-> updater 循环导入
            window.close()
        except Exception:
            pass
        time.sleep(1.2)
        os._exit(0)

    t = threading.Timer(1.5, _leave)   # 留出时间让这个 HTTP 响应发回前端
    t.daemon = True
    t.start()
    return {"ok": True, "message": "安装程序即将启动；会弹出系统权限确认，笔记和配置都会保留"}
