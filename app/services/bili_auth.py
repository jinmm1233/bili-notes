"""B站扫码登录：走 passport 官方接口，不需要浏览器自动化。

对比 Playwright 方案的优势：
- 不打包 Chromium（省 150MB+）、不起额外进程、不受浏览器版本影响
- 拿到的是标准 cookie，直接写成 Netscape 格式即可喂给 yt-dlp，
  顺带解决了"AI 字幕需要登录态"这个前置条件
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from ..core.config import Settings
from ..core.errors import UpstreamError

log = logging.getLogger(__name__)

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)
HEADERS = {"User-Agent": UA, "Referer": "https://www.bilibili.com/"}
QR_GENERATE = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
QR_POLL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"
NAV_API = "https://api.bilibili.com/x/web-interface/nav"

# 86101 未扫码 / 86090 已扫码待确认 / 86038 已过期 / 0 成功
STATUS_TEXT = {
    0: "登录成功",
    86101: "等待扫码",
    86090: "已扫码，请在手机上点确认",
    86038: "二维码已过期，请刷新",
}

_NETSCAPE_HEADER = (
    "# Netscape HTTP Cookie File\n"
    "# 由 BiliNotes 自动生成，包含你的 B站登录态，请勿分享\n\n"
)
_COOKIE_DOMAIN = ".bilibili.com"


@dataclass
class LoginState:
    logged_in: bool
    mid: int = 0
    uname: str = ""
    face: str = ""
    vip: bool = False
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "logged_in": self.logged_in,
            "mid": self.mid,
            "uname": self.uname,
            "face": self.face,
            "vip": self.vip,
            "message": self.message,
        }


def generate_qrcode(timeout: float = 20.0) -> dict[str, Any]:
    """申请一张登录二维码，返回 {qrcode_key, url, image}。

    image 是 data URI —— B站只给 H5 链接，得自己在后端画成二维码，
    这样不依赖任何在线二维码服务，也不会把登录链接外泄给第三方。
    """
    try:
        with httpx.Client(headers=HEADERS, timeout=timeout, follow_redirects=True) as client:
            resp = client.get(QR_GENERATE)
            resp.raise_for_status()
            payload = resp.json()
    except Exception as exc:
        raise UpstreamError(f"获取登录二维码失败：{str(exc)[:160]}") from exc

    if payload.get("code") != 0:
        raise UpstreamError(f"获取登录二维码失败：{payload.get('message')}")

    data = payload.get("data") or {}
    url = data.get("url", "")
    return {
        "qrcode_key": data.get("qrcode_key", ""),
        "url": url,
        "image": render_qr_data_uri(url),
        "expires_in": 180,
        "generated_at": time.time(),
    }


def render_qr_data_uri(text: str) -> str:
    if not text:
        return ""
    try:
        import base64
        from io import BytesIO

        import qrcode  # noqa: PLC0415

        qr = qrcode.QRCode(box_size=8, border=2, error_correction=qrcode.constants.ERROR_CORRECT_M)
        qr.add_data(text)
        qr.make(fit=True)
        img = qr.make_image(fill_color="#1b1f24", back_color="white")
        buf = BytesIO()
        img.save(buf, format="PNG")
        return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception as exc:
        log.warning("二维码渲染失败：%s", exc)
        return ""


def poll_qrcode(settings: Settings, qrcode_key: str, timeout: float = 20.0) -> dict[str, Any]:
    """轮询扫码结果；成功时把 cookie 落盘并返回用户信息。"""
    if not qrcode_key:
        raise UpstreamError("缺少 qrcode_key")

    try:
        with httpx.Client(headers=HEADERS, timeout=timeout, follow_redirects=True) as client:
            resp = client.get(QR_POLL, params={"qrcode_key": qrcode_key})
            resp.raise_for_status()
            payload = resp.json()
            jar = dict(client.cookies)
    except Exception as exc:
        raise UpstreamError(f"查询扫码状态失败：{str(exc)[:160]}") from exc

    if payload.get("code") != 0:
        raise UpstreamError(f"查询扫码状态失败：{payload.get('message')}")

    data = payload.get("data") or {}
    code = int(data.get("code", -1))
    message = data.get("message") or STATUS_TEXT.get(code, f"未知状态 {code}")

    if code != 0:
        return {
            "status": code,
            "message": message,
            "logged_in": False,
            "done": code == 86038,
        }

    cookies = _extract_cookies(jar, data.get("url", ""))
    if not cookies.get("SESSDATA"):
        raise UpstreamError("登录成功但未取到 SESSDATA，请重试")

    _write_netscape(settings.cookies_file, cookies)
    state = check_login(settings)
    log.info("B站登录成功 mid=%s uname=%s", state.mid, state.uname)
    return {
        "status": 0,
        "message": "登录成功",
        "logged_in": state.logged_in,
        "done": True,
        "user": state.to_dict(),
    }


def _extract_cookies(jar: dict[str, str], redirect_url: str) -> dict[str, str]:
    """cookie 可能在 Set-Cookie 里，也可能挂在返回的 url 查询串上，两边都取。"""
    cookies = {k: v for k, v in jar.items() if v}
    if redirect_url and "?" in redirect_url:
        query = redirect_url.split("?", 1)[1]
        for pair in query.split("&"):
            if "=" not in pair:
                continue
            key, _, value = pair.partition("=")
            if key in {"SESSDATA", "bili_jct", "DedeUserID", "DedeUserID__ckMd5", "sid"}:
                cookies.setdefault(key, value)
    return cookies


def _write_netscape(path: Path, cookies: dict[str, str]) -> None:
    lines = [_NETSCAPE_HEADER]
    expiry = int(time.time()) + 180 * 24 * 3600
    for name, value in cookies.items():
        if not value:
            continue
        lines.append(f"{_COOKIE_DOMAIN}\tTRUE\t/\tFALSE\t{expiry}\t{name}\t{value}\n")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(lines), encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass


def load_cookies(path: Path) -> dict[str, str]:
    """解析 Netscape cookie 文件为字典。"""
    if not path.is_file():
        return {}
    cookies: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) >= 7:
            cookies[parts[5]] = parts[6]
    return cookies


def check_login(settings: Settings, timeout: float = 15.0) -> LoginState:
    """用已保存的 cookie 请求 nav 接口确认登录态。"""
    cookies = load_cookies(settings.cookies_file)
    if not cookies.get("SESSDATA"):
        return LoginState(logged_in=False, message="未登录")

    try:
        with httpx.Client(headers=HEADERS, timeout=timeout, cookies=cookies) as client:
            resp = client.get(NAV_API)
            payload = resp.json()
    except Exception as exc:
        return LoginState(logged_in=False, message=f"校验失败：{str(exc)[:80]}")

    data = payload.get("data") or {}
    if payload.get("code") != 0 or not data.get("isLogin"):
        return LoginState(logged_in=False, message="登录已过期，请重新扫码")

    return LoginState(
        logged_in=True,
        mid=int(data.get("mid") or 0),
        uname=data.get("uname") or "",
        face=data.get("face") or "",
        vip=bool((data.get("vipStatus") or 0)),
        message="已登录",
    )


def logout(settings: Settings) -> None:
    for path in (settings.cookies_file, settings.cookies_file.with_suffix(".bak")):
        path.unlink(missing_ok=True)
    log.info("已退出 B站登录")
