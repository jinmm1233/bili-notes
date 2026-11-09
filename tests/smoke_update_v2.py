"""内置更新补强回归：校验文件、资产选择、断点续传、404 提示。

跑法：python tests/smoke_update_v2.py
全程不联网（本地 127.0.0.1 起一个临时 HTTP 服务），用户目录也用临时目录。
"""
from __future__ import annotations

import hashlib
import io
import os
import sys
import tempfile
import threading
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.core import updater as U  # noqa: E402

FAILED = 0
DATA: Path


def ok(cond: bool, msg: str) -> None:
    global FAILED
    print(("[OK] " if cond else "[FAIL] ") + msg)
    if not cond:
        FAILED += 1


def reset() -> Path:
    global DATA
    DATA = Path(tempfile.mkdtemp(prefix="bn_updv2_"))
    U.get_settings = lambda: SimpleNamespace(data_dir=DATA)
    os.environ.pop("BILINOTES_GITHUB_REPO", None)
    with U._LOCK:
        U._DOWNLOAD.clear()
        U._DOWNLOAD.update({"state": "idle", "progress": 0, "received": 0,
                            "total": 0, "message": "", "path": "", "verified": False})
    return DATA


class _Resp(io.BytesIO):
    def __init__(self, data: bytes, status: int = 200, headers: dict | None = None):
        super().__init__(data)
        self.status = status
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _patch_urlopen(fn):
    """替换 urlopen，返回一个恢复函数；只在本进程内生效。"""
    orig = U.urllib.request.urlopen
    U.urllib.request.urlopen = fn
    return lambda: setattr(U.urllib.request, "urlopen", orig)


EXE = "BiliNotes-setup-0.2.3.exe"


def test_asset_choice() -> None:
    payload = {
        "tag_name": "v0.2.3",
        "body": "release notes",
        "assets": [
            {"name": "vc_redist.x64.exe", "size": 100},
            {"name": EXE, "size": 220_000_000},
            {"name": "SHA256SUMS.txt", "size": 130,
             "browser_download_url": "https://github.com/a/b/releases/download/v0.2.3/SHA256SUMS.txt"},
        ],
    }
    info = U.parse_release(payload, "a/b")
    ok(info["asset"]["name"] == EXE, "多个 exe 时优先选主安装包，不是运行库")
    ok(info["asset"]["url"].endswith(EXE), "下载直链指向主安装包")
    ok(info["asset"]["sum_url"].endswith("SHA256SUMS.txt"), "解析出校验文件地址")
    ok(info["latest"] == "0.2.3", "tag 剥掉 v 前缀")


def test_sums_parsing() -> None:
    reset()
    sha = "a" * 64
    text = (f"{sha}  {EXE}\n"
            f"{'b' * 64} *other.exe\n"
            "bad line without hash\n"
            f"{'c' * 64}  dist/{EXE}\n")
    restore = _patch_urlopen(lambda req, timeout=None: _Resp(text.encode()))
    try:
        url = "https://github.com/a/b/releases/download/v0.2.3/SHA256SUMS.txt"
        ok(U._sha_from_sums(url, EXE, {"mirror": ""}) == sha, "从 SHA256SUMS 取到对应文件哈希")
        ok(U._sha_from_sums(url, "missing.exe", {"mirror": ""}) == "", "文件不在清单里返回空")
        ok(U._sha_from_sums("https://evil.example/SHA256SUMS.txt", EXE, {"mirror": ""}) == "",
           "非白名单域名不请求")
    finally:
        restore()

    star = f"{'d' * 64} *{EXE}\n"
    restore = _patch_urlopen(lambda req, timeout=None: _Resp(star.encode()))
    try:
        ok(U._sha_from_sums("https://github.com/a/b/x/SHA256SUMS.txt", EXE,
                            {"mirror": ""}) == "d" * 64, "认 sha256sum -b 的星号格式")
    finally:
        restore()


class _Handler(BaseHTTPRequestHandler):
    data = b""
    sums = b""
    ignore_range = False
    seen_ranges: list[str] = []

    def do_GET(self):  # noqa: N802
        if self.path.endswith("SHA256SUMS.txt"):
            self.send_response(200)
            self.send_header("Content-Length", str(len(self.sums)))
            self.end_headers()
            self.wfile.write(self.sums)
            return
        rng = self.headers.get("Range") or ""
        self.seen_ranges.append(rng)
        start = 0
        if rng and not self.ignore_range:
            start = int(rng.split("=", 1)[1].split("-", 1)[0])
        chunk = self.data[start:]
        if rng and not self.ignore_range:
            self.send_response(206)
            self.send_header("Content-Range",
                             f"bytes {start}-{len(self.data) - 1}/{len(self.data)}")
        else:
            self.send_response(200)
        self.send_header("Content-Length", str(len(chunk)))
        self.end_headers()
        self.wfile.write(chunk)

    def log_message(self, *args):
        pass


def _serve(data: bytes, sums: bytes = b"", ignore_range: bool = False):
    _Handler.data = data
    _Handler.sums = sums
    _Handler.ignore_range = ignore_range
    _Handler.seen_ranges = []
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}/{EXE}"


def _asset(data: bytes, **extra) -> dict:
    a = {"name": EXE, "size": len(data), "sha256": "", "sum_url": ""}
    a.update(extra)
    return a


def test_resume_and_verify() -> None:
    reset()
    data = os.urandom(180_000) + b"tail-of-installer"
    sha = hashlib.sha256(data).hexdigest()
    sums = f"{sha}  {EXE}\n".encode()
    srv, url = _serve(data, sums)
    try:
        folder = DATA / "updates"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / (EXE + ".part")).write_bytes(data[:40_000])
        # 说明里没贴哈希，只上传了 SHA256SUMS.txt：这条路径以前是漏的。
        # 本地服务是 127.0.0.1，放行检查在这里替身成"已确认是 GitHub 域名"。
        sums_url = url.replace(EXE, "SHA256SUMS.txt")
        with patch.object(U, "_url_allowed", return_value=True):
            U._download_worker(url, _asset(data, sum_url=sums_url), {"mirror": ""})
        final = folder / EXE
        ok(final.is_file() and final.read_bytes() == data, "断点续传后文件内容完整")
        ok(_Handler.seen_ranges and _Handler.seen_ranges[0].startswith("bytes=40000-"),
           "确实带着 Range 从 40000 字节续传")
        ok(U._DOWNLOAD["state"] == "done" and U._DOWNLOAD["verified"] is True,
           "从 SHA256SUMS.txt 取到哈希并校验通过")
        ok(U._DOWNLOAD["progress"] == 100, "进度到 100")
        ok(not (folder / (EXE + ".part")).exists(), ".part 已转成正式文件")
    finally:
        srv.shutdown()
        srv.server_close()


def test_server_ignores_range() -> None:
    reset()
    data = os.urandom(90_000)
    sha = hashlib.sha256(data).hexdigest()
    srv, url = _serve(data, ignore_range=True)
    try:
        folder = DATA / "updates"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / (EXE + ".part")).write_bytes(b"corrupt-prefix")
        U._download_worker(url, _asset(data, sha256=sha), {"mirror": ""})
        ok((folder / EXE).read_bytes() == data, "服务端忽略 Range 时从头下载，不拼接坏前缀")
        ok(U._DOWNLOAD["verified"] is True, "整文件重算哈希通过")
    finally:
        srv.shutdown()
        srv.server_close()


def test_bad_hash_rejected() -> None:
    reset()
    data = os.urandom(20_000)
    srv, url = _serve(data)
    try:
        U._download_worker(url, _asset(data, sha256="0" * 64), {"mirror": ""})
        folder = DATA / "updates"
        ok(U._DOWNLOAD["state"] == "error", "哈希不符时进入 error 状态")
        ok("校验不通过" in U._DOWNLOAD["message"], "给出人话提示")
        ok(not (folder / EXE).exists() and not (folder / (EXE + ".part")).exists(),
           "坏包和半截文件都被删掉")
    finally:
        srv.shutdown()
        srv.server_close()


def test_not_found_hint() -> None:
    reset()
    U.save_config({"repo": "someone/BiliNotes"})

    def boom(req, timeout=None):
        raise urllib.error.HTTPError(str(req.full_url), 404, "Not Found", {}, None)

    restore = _patch_urlopen(boom)
    try:
        r = U.check(force=True)
        ok(r.get("reason") == "not_found", "404 单独归类，不再笼统说网络不通")
        ok("用户名/仓库名" in r.get("message", ""), "提示能指导用户改配置")
    finally:
        restore()


def main() -> int:
    test_asset_choice()
    test_sums_parsing()
    test_resume_and_verify()
    test_server_ignores_range()
    test_bad_hash_rejected()
    test_not_found_hint()
    print(f"\n{'全部通过' if FAILED == 0 else str(FAILED) + ' 项失败'}")
    return FAILED


if __name__ == "__main__":
    sys.exit(main())
