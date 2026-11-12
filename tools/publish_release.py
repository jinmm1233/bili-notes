"""把 installer/dist 里的成品发到 GitHub Releases（可重复运行，不会建重复 Release）。

用法（项目根目录）：
    python tools/publish_release.py                          # 用 app/__init__.py 里的版本号
    python tools/publish_release.py --version 0.2.6 --proxy http://127.0.0.1:7890

做四件事：
  1. 复核安装包、SHA256SUMS.txt、Release 说明里的哈希三者一致；
  2. 建 tag v<版本> 对应的正式 Release（已存在就复用并补说明，不新建）；
  3. 上传安装包 + SHA256SUMS.txt（同名资产先删再传，避免 422）；
  4. 回读 latest release，核对 tag / 资产名 / 大小 / 校验文件内容。

凭据来自 Git Credential Manager（`git credential fill`），不落盘、不打印。
本机若开了代理但 git/curl 走不通，用 --proxy 显式指定。
"""
from __future__ import annotations

import argparse
import hashlib
import re
import subprocess
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "installer" / "dist"
API = "https://api.github.com"
UPLOAD = "https://uploads.github.com"


def current_version() -> str:
    text = (ROOT / "app" / "__init__.py").read_text(encoding="utf-8")
    m = re.search(r'__version__\s*=\s*["\']([^"\']+)["\']', text)
    if not m:
        raise SystemExit("读不到 app/__init__.py 里的 __version__")
    return m.group(1)


def default_repo() -> str:
    text = (ROOT / "app" / "__init__.py").read_text(encoding="utf-8")
    m = re.search(r'__update_repo__\s*=\s*["\']([^"\']+)["\']', text)
    return m.group(1) if m else ""


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def git_token() -> str:
    p = subprocess.run(
        ["git", "credential", "fill"],
        input="protocol=https\nhost=github.com\n\n",
        text=True, capture_output=True,
    )
    if p.returncode != 0:
        raise SystemExit(f"取 GitHub 凭据失败：{p.stderr.strip()}")
    creds = dict(line.split("=", 1) for line in p.stdout.strip().splitlines() if "=" in line)
    token = creds.get("password", "")
    if not token:
        raise SystemExit("Git Credential Manager 里没有 github.com 的凭据，请先用 GitHub Desktop 登录")
    return token


class GitHub:
    def __init__(self, repo: str, token: str, proxy: str | None):
        self.repo = repo
        self.s = requests.Session()
        self.s.headers.update({
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "BiliNotes-publisher",
        })
        if proxy:
            self.s.proxies.update({"http": proxy, "https": proxy})
        elif proxy is False:
            self.s.trust_env = False

    def call(self, method: str, url: str, **kw):
        r = self.s.request(method, url, timeout=(30, 300), **kw)
        if not r.ok:
            raise RuntimeError(f"{method} {url} -> {r.status_code}: {r.text[:400]}")
        return r.json() if r.content else {}

    def api(self, method: str, path: str, **kw):
        return self.call(method, f"{API}{path}", **kw)

    def release_path(self, tag: str) -> str:
        return f"/repos/{self.repo}/releases"


def main() -> int:
    ap = argparse.ArgumentParser(description="发布 BiliNotes Release 到 GitHub")
    ap.add_argument("--version", help="默认取 app/__init__.py 的 __version__")
    ap.add_argument("--repo", help="默认取 app/__init__.py 的 __update_repo__")
    ap.add_argument("--proxy", help="显式代理，如 http://127.0.0.1:7890")
    ap.add_argument("--no-proxy", action="store_true", help="忽略环境里的代理设置，直连")
    ap.add_argument("--upload-only", action="store_true", help="只补传资产，不改 Release 说明")
    ap.add_argument("--update-notes", action="store_true",
                    help="已发布的 Release 也用本地说明覆盖描述（改文案时用）")
    args = ap.parse_args()

    ver = args.version or current_version()
    repo = args.repo or default_repo()
    if not repo or "/" not in repo:
        raise SystemExit("仓库名要写成 用户名/仓库名")

    exe = DIST / f"BiliNotes-setup-{ver}.exe"
    sums = DIST / "SHA256SUMS.txt"
    # 说明正本在 docs/（进版本库），dist 里那份是发版用的同步副本
    docs_notes = ROOT / "docs" / f"release-notes-{ver}.md"
    notes_path = docs_notes if docs_notes.is_file() else DIST / f"release-notes-{ver}.md"
    for p in (exe, sums, notes_path):
        if not p.is_file():
            raise SystemExit(f"缺文件：{p}")

    digest = sha256(exe)
    size = exe.stat().st_size
    sums_text = sums.read_text(encoding="utf-8")
    notes = notes_path.read_text(encoding="utf-8")
    if digest not in sums_text or exe.name not in sums_text:
        raise SystemExit("SHA256SUMS.txt 与安装包对不上，先跑 python tools/release_assets.py")
    if digest not in notes:
        raise SystemExit("Release 说明里的哈希与安装包对不上，先跑 python tools/release_assets.py")
    print(f"版本 {ver} | {exe.name} | {size / 1024 / 1024:.1f} MB")
    print(f"SHA256 {digest}（安装包 / SUMS / 说明 三者一致）")

    proxy = False if args.no_proxy else args.proxy
    gh = GitHub(repo, git_token(), proxy)
    tag = f"v{ver}"
    base = gh.release_path(tag)

    # 1. Release：已存在就复用（draft 转正、说明为空就补上），不重复建
    try:
        rel = gh.api("GET", f"{base}/tags/{tag}")
        print(f"Release 已存在（id={rel['id']}），复用")
        patch = {}
        if rel.get("draft"):
            patch["draft"] = False
        if not args.upload_only:
            body = str(rel.get("body") or "").strip()
            if not body or (args.update_notes and body != notes.strip()):
                patch["body"] = notes
        if patch:
            rel = gh.api("PATCH", f"{base}/{rel['id']}", json=patch)
            print(f"  已更新：{', '.join(patch)}")
    except RuntimeError as exc:
        if "-> 404:" not in str(exc):
            raise
        rel = gh.api("POST", base, json={
            "tag_name": tag,
            "target_commitish": "master",
            "name": f"BiliNotes v{ver}",
            "body": notes,
            "draft": False,
            "prerelease": False,
            "make_latest": "true",
        })
        print(f"已创建 Release v{ver}（id={rel['id']}）")

    # 2. 资产：同名且大小一致就跳过，不一致先删再传
    assets = gh.api("GET", f"{base}/{rel['id']}/assets")
    by_name = {a["name"]: a for a in assets}
    for path in (exe, sums):
        old = by_name.get(path.name)
        local_size = path.stat().st_size
        if old and int(old.get("size") or 0) == local_size:
            remote_digest = str(old.get("digest") or "")
            if not remote_digest or remote_digest.lower() == f"sha256:{sha256(path)}":
                print(f"资产已存在且一致，跳过：{path.name}")
                continue
        if old:
            gh.api("DELETE", f"{base}/assets/{old['id']}")
            print(f"  删除同名旧资产：{path.name}")
        url = f"{UPLOAD}{base}/{rel['id']}/assets?name={path.name}"
        last_error = None
        for attempt in range(1, 4):
            try:
                print(f"上传 {path.name}（{local_size / 1024 / 1024:.1f} MB，第 {attempt} 次）…", flush=True)
                with path.open("rb") as f:
                    r = gh.s.post(
                        url, data=f,
                        headers={"Content-Type": "application/octet-stream",
                                 "Content-Length": str(local_size)},
                        timeout=(30, 1800),
                    )
                if not r.ok:
                    raise RuntimeError(f"{r.status_code}: {r.text[:400]}")
                print(f"  完成：{r.json().get('name')} / {r.json().get('size')} 字节", flush=True)
                last_error = None
                break
            except Exception as exc:                       # 网络中断重试，重开文件从头传
                last_error = exc
                print(f"  失败：{type(exc).__name__}: {exc}", flush=True)
                if attempt < 3:
                    time.sleep(3)
        if last_error:
            raise SystemExit(f"上传 {path.name} 三次都失败，检查代理或稍后重跑本脚本（会断点续做）")

    # 3. 回读校验：latest、资产、说明哈希、SUMS 下载内容
    latest = gh.api("GET", f"{base}/latest")
    if str(latest.get("tag_name") or "") != tag:
        raise SystemExit(f"latest 不是 {tag}，而是 {latest.get('tag_name')}")
    final_assets = {a["name"]: a for a in gh.api("GET", f"{base}/{rel['id']}/assets")}
    if exe.name not in final_assets or sums.name not in final_assets:
        raise SystemExit("资产不齐")
    if int(final_assets[exe.name]["size"]) != size:
        raise SystemExit("安装包资产大小与本地不一致")
    remote_digest = str(final_assets[exe.name].get("digest") or "")
    if remote_digest and remote_digest.lower() != f"sha256:{digest}":
        raise SystemExit(f"GitHub 侧安装包摘要不符：{remote_digest}")

    # 校验文件走两条路：API 资产端点（api.github.com）优先，浏览器直链兜底。
    # 某些代理只放行 api.github.com、对 github.com 偶发断连，这里都重试几次。
    sum_asset = final_assets[sums.name]
    candidates = [
        f"{API}{base}/assets/{sum_asset['id']}",
        str(sum_asset["browser_download_url"]),
    ]
    matched = False
    for url in candidates:
        for attempt in range(1, 3):
            try:
                r = gh.s.get(url, headers={"Accept": "application/octet-stream"},
                             timeout=(30, 120))
                r.raise_for_status()
                if r.text.strip() == sums_text.strip():
                    matched = True
                    break
            except Exception as exc:
                print(f"  校验文件读取重试 {attempt}：{type(exc).__name__}", flush=True)
                time.sleep(2)
        if matched:
            break
    if not matched:
        raise SystemExit("线上 SHA256SUMS.txt 内容与本地不一致，或两条下载路径都不通")

    # 内置更新实际用的就是这条 release/download 直链，顺便验 Range 续传。
    # 这里刻意用 urllib（和 app/core/updater.py 一致），某些代理对 requests 的
    # CONNECT 处理不稳，但对 urllib / curl 正常；用软件自己的路径测才有意义。
    import urllib.request                                   # noqa: PLC0415
    handlers = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    elif proxy is False:
        handlers.append(urllib.request.ProxyHandler({}))
    opener = urllib.request.build_opener(*handlers)
    direct = f"https://github.com/{repo}/releases/download/{tag}/{exe.name}"
    range_ok = False
    for attempt in range(1, 3):
        try:
            req = urllib.request.Request(direct, headers={
                "User-Agent": "BiliNotes-publisher", "Range": "bytes=0-1023"})
            with opener.open(req, timeout=60) as resp:
                range_ok = resp.status in (200, 206) and bool(resp.read(1024))
            if range_ok:
                break
        except Exception as exc:
            print(f"  直链探测重试 {attempt}：{type(exc).__name__}", flush=True)
            time.sleep(2)

    print("\n发布校验通过")
    print(f"  latest tag      {latest['tag_name']}（draft={latest['draft']}, prerelease={latest['prerelease']}）")
    print(f"  资产            {', '.join(final_assets)}")
    print(f"  安装包大小      {final_assets[exe.name]['size']} 字节")
    print(f"  GitHub 摘要     {remote_digest or '（API 未返回）'}")
    print(f"  说明内哈希      {'有' if digest in str(latest.get('body') or '') else '缺'}")
    print(f"  SHA256SUMS.txt  线上/本地一致")
    print(f"  下载直链        {'可用（支持 Range）' if range_ok else '本次探测不通，检查本机代理'}")
    print(f"  发布页          {latest['html_url']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
