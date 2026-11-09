"""双端访问：让同一 WiFi 下的手机能打开同一套界面。

为什么不是简单绑 0.0.0.0：
  一旦绑了，同一个 WiFi 里的任何人（宿舍、公司、咖啡厅）扫到端口就能翻你的
  笔记、卡片、B 站登录态。所以默认仍然只听 127.0.0.1，只有用户主动打开
  `lan_access` 才对外，并且**必须带口令**——口令首次使用时随机生成，
  存进 config.json，手机上第一次带 ?k= 进来后写 cookie，之后就不用再输了。

三个东西：
  - `install_guard()`：中间件。本机请求一律放行；外部请求没口令就返回一个
    极简登录页（不依赖任何静态资源），API 请求则返回 401。
  - `lan_ip()`：猜本机局域网地址，只用来在启动日志/设置页上打印可用链接。
  - `login_page()`：登录页，内联样式，手机上直接能输。
"""

from __future__ import annotations

import json
import logging
import secrets
import socket
import time
from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse

log = logging.getLogger(__name__)

COOKIE = "bn_lan"
COOKIE_MAX_AGE = 30 * 86400        # 手机上记一个月，别每次复习都要输口令
LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost", "testclient"}

# 本次进程真正绑上的地址。`pick_port` 在端口被占时会顺延到 8766、8767……
# 状态页如果还照 settings.port 报地址，手机上扫到的就是**另一个实例**。
_BOUND: dict[str, Any] = {"host": "", "port": 0}


def note_binding(host: str, port: int) -> None:
    """启动时由 main.run() 调用，把真实监听端口记下来。"""
    _BOUND["host"] = host
    _BOUND["port"] = int(port)


def bound_port(fallback: int) -> int:
    """对外该报哪个端口：真实绑上的那个，没有就用配置值（测试里没走 run()）。"""
    return int(_BOUND["port"] or fallback)


def running_instance(host: str, port: int, timeout: float = 0.45) -> str:
    """这个端口上是不是已经有一个 BiliNotes 在跑？是就返回它的版本号。

    只认 `/healthz` 里的 `app` 字段——端口通不代表是同类程序（8765 也可能是
    别的开发服务器）。用来实现"再点一次启动不会起第二个实例"。
    """
    import urllib.error  # noqa: PLC0415

    from .local import urlopen_local  # noqa: PLC0415  本机请求不能走代理

    url = f"http://{host}:{port}/healthz"
    try:
        with urlopen_local(url, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError, ValueError):
        return ""
    if isinstance(data, dict) and data.get("app") == "BiliNotes":
        return str(data.get("version") or "unknown")
    return ""


def lan_ip() -> str:
    """本机在局域网里的地址。不真的发包，只是让系统挑一张网卡。"""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("223.5.5.5", 80))
            return sock.getsockname()[0]
    except OSError:
        return ""


def reachable(ip: str, port: int, timeout: float = 0.35) -> bool:
    """当前进程是不是真的绑在对外地址上了。

    开了开关但没重启时，127.0.0.1 能连、局域网 IP 连不上——用这个区分，
    免得设置页说"已开启"、手机却打不开。
    """
    if not ip:
        return False
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        try:
            sock.connect((ip, port))
            return True
        except OSError:
            return False


def new_token() -> str:
    """口令：短到能在手机上敲，长到猜不出来（12 位 base64 约 71 bit）。"""
    return secrets.token_urlsafe(9)[:12]


def is_local(request: Request) -> bool:
    client = request.client.host if request.client else ""
    return client in LOCAL_HOSTS


def authed(request: Request) -> bool:
    """本机，或者已经带着正确口令（cookie / X-BN-Key 头）。

    二维码里装的就是口令，所以“该不该给”取决于“对方是不是已经有口令了”。
    只认本机的话，从局域网地址打开设置页（client.host 是 192.168.x.x）
    自己也会拿到 403，页面上就是个破图。
    """
    if is_local(request):
        return True
    from .config import get_settings  # noqa: PLC0415  避免 config <-> lan 循环导入

    token = (get_settings().lan_token or "").strip()
    if not token:
        return False
    given = (
        request.cookies.get(COOKIE) or request.headers.get("x-bn-key") or ""
    ).strip()
    return secrets.compare_digest(given, token)


def qr_svg(text: str, box: int = 8) -> str:
    """把"带口令的地址"画成二维码。

    给别人用的关键：让他扫码，而不是抄 192.168.x.x 和一串口令。
    纯 Python 生成 SVG，不依赖前端库、不需要 Pillow。
    """
    import io  # noqa: PLC0415

    import qrcode  # noqa: PLC0415
    import qrcode.image.svg  # noqa: PLC0415

    qr = qrcode.QRCode(
        version=None, box_size=box, border=2,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        image_factory=qrcode.image.svg.SvgPathImage,
    )
    qr.add_data(text)
    qr.make(fit=True)
    buf = io.BytesIO()
    qr.make_image().save(buf)
    return buf.getvalue().decode("utf-8")


def login_page(detail: str = "", form: bool = True) -> str:
    tip = f'<p class="bad">{detail}</p>' if detail else ""
    body = ("""  <form method="post" action="/api/lan/login">
    <input name="k" autofocus autocomplete="off" inputmode="text"
           placeholder="访问口令" aria-label="访问口令">
    <button type="submit">进入</button>
  </form>""" if form else "")
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>BiliNotes · 输入访问口令</title>
<style>
  *{{box-sizing:border-box}}
  body{{margin:0;min-height:100vh;display:grid;place-items:center;padding:24px;
    font-family:-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;
    background:#0f1115;color:#e8eaef}}
  .box{{width:100%;max-width:360px}}
  h1{{font-size:19px;margin:0 0 6px}}
  p{{font-size:13px;line-height:1.6;color:#9aa3b2;margin:0 0 18px}}
  .bad{{color:#ff8f8f}}
  input{{width:100%;padding:14px 16px;font-size:16px;border-radius:12px;
    border:1px solid #2a2f3a;background:#171a21;color:inherit;letter-spacing:2px}}
  input:focus{{outline:none;border-color:#fb7299}}
  button{{width:100%;margin-top:14px;padding:14px;font-size:16px;border:none;border-radius:12px;
    background:#fb7299;color:#fff;font-weight:600}}
  button:active{{opacity:.85}}
  .hint{{font-size:12px;color:#6d7686;margin-top:16px}}
</style></head>
<body><div class="box">
  <h1>BiliNotes</h1>
  <p>这个地址只对同一 WiFi 开放。请输入电脑上显示的访问口令。</p>
  {tip}
  {body}
  <p class="hint">口令在电脑上：设置 → 手机访问；或启动窗口里那串 <code>?k=</code> 后面的字符。</p>
</div></body></html>"""


def install_guard(app: Any) -> None:
    """挂上口令中间件。lan_access 关着的时候它是透明的。

    每次请求都重新取一次 settings：用户在设置页换了口令，必须立刻生效，
    不能等重启（监听地址变了才需要重启，那是另一回事）。
    """

    @app.middleware("http")
    async def lan_guard(request: Request, call_next):  # noqa: ANN001
        from .config import get_settings  # noqa: PLC0415  避免 config <-> lan 互相导入

        settings = get_settings()
        local = is_local(request)
        if not settings.lan_access:
            if local:
                return await call_next(request)
            # 开关已关、但进程还绑在 0.0.0.0（没重启）：端口仍然对外开着。
            # 这时候绝不能放行，否则等于关掉口令继续裸奔。
            if request.url.path.startswith("/api/"):
                return JSONResponse({"detail": "手机访问已关闭，请重启 BiliNotes"}, status_code=403)
            return HTMLResponse(
                login_page("手机访问已关闭。请在电脑上重启 BiliNotes。", form=False),
                status_code=403,
            )
        if local:
            return await call_next(request)

        key = settings.lan_token or ""
        if not key:                       # 口令还没生成：别把用户锁在门外
            return await call_next(request)

        given = (
            request.query_params.get("k")
            or request.headers.get("x-bn-key")
            or request.cookies.get(COOKIE)
        )
        if given == key:
            response = await call_next(request)
            if request.query_params.get("k"):
                response.set_cookie(COOKIE, key, max_age=COOKIE_MAX_AGE,
                                    httponly=True, samesite="lax")
            return response

        if request.url.path == "/api/lan/login":
            return await call_next(request)     # 登录接口自己校验
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": "需要访问口令"}, status_code=401)
        return HTMLResponse(login_page(), status_code=401)


# ---------------------------------------------------------------- 地址变更记录
# 手机上的链接是"快照"：IP 一变，书签和截图里的二维码就全废了，而用户只会觉得
# "又断了"。所以每次报地址时顺手记一笔，下次换地址就能明确说"是地址变了"，
# 而不是让人去猜防火墙、猜 WiFi。
def _state_file() -> Path:
    from .config import get_settings  # noqa: PLC0415  避免 config <-> lan 循环导入

    return get_settings().data_dir / "lan_state.json"


def last_seen() -> dict:
    path = _state_file()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def remember(ip: str, url: str) -> dict:
    """记下这次报出去的地址，并判断跟上次比换没换。

    返回 `{"prev_ip", "prev_url", "changed", "ts"}`。只在地址真的变了才落盘，
    否则每个刷设置页的请求都写一遍文件，纯属折腾磁盘。
    """
    prev = last_seen()
    changed = bool(prev.get("ip")) and bool(ip) and prev["ip"] != ip
    info = {
        "prev_ip": prev.get("ip", ""),
        "prev_url": prev.get("url", ""),
        "changed": changed,
        "ts": prev.get("ts", 0),
    }
    if ip and (ip != prev.get("ip") or url != prev.get("url")):
        try:
            _state_file().write_text(
                json.dumps({"ip": ip, "url": url, "ts": int(time.time())},
                           ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            info["ts"] = int(time.time())
        except OSError:                       # 记不下来不影响主流程
            log.debug("写 lan_state.json 失败", exc_info=True)
    return info


def wifi_ssid() -> str:
    """当前 WiFi 名字，用来判断"是不是还在同一个网络"。

    Windows 中文系统的 netsh 输出是 GBK；先按 UTF-8 试，解不出 SSID 再换 GBK。
    """
    import subprocess  # noqa: PLC0415

    try:
        done = subprocess.run(
            ["netsh", "wlan", "show", "interfaces"],
            capture_output=True, timeout=4, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    for enc in ("utf-8", "gbk"):
        text = done.stdout.decode(enc, "replace")
        for line in text.splitlines():
            key = line.strip()
            # 只要 SSID 本身，别把 BSSID 那行（MAC 地址）当名字
            if key.startswith("SSID") and "BSSID" not in key and ":" in key:
                name = key.split(":", 1)[1].strip()
                if name:
                    return name
    return ""


# 校园网/连锁咖啡常开「客户端隔离」：同一个 WiFi 下，设备之间互相看不见。
# 这种情况下无论怎么开防火墙都连不上，必须换热点或用离线包——所以要早点认出来。
_ISOLATION_HINTS = (
    "gdufe", "edu", "campus", "university", "school", "cmcc", "chinanet",
    "chinaunicom", "starbucks", "luckin", "mcdonald", "kfc", "公共", "校园",
)


def looks_isolated(ssid: str) -> bool:
    low = (ssid or "").lower()
    return any(hint in low for hint in _ISOLATION_HINTS)


def diagnose() -> dict:
    """手机连不上时，按「服务 → 监听 → 地址 → 网络」的顺序逐项查。

    只回答"卡在哪一环"，不假装能修——AP 隔离、防火墙这类东西程序改不了，
    但能明确告诉用户该换哪条路（热点 / 离线包）。
    """
    from .config import get_settings  # noqa: PLC0415

    settings = get_settings()
    port = bound_port(settings.port)
    ip = lan_ip()
    ssid = wifi_ssid()
    base = f"http://{ip}:{port}" if ip else ""
    url = f"{base}/?k={settings.lan_token}" if (base and settings.lan_token) else ""
    bound = reachable(ip, port) if ip else False
    seen = remember(ip, url)

    checks: list[dict[str, Any]] = [
        {"key": "service", "label": "软件在运行", "ok": True,
         "detail": f"正在监听 {port} 端口"},
        {"key": "enabled", "label": "已允许手机访问", "ok": bool(settings.lan_access),
         "detail": "开关已打开" if settings.lan_access
                   else "开关没开：手机连不进来，只有这台电脑能打开"},
        {"key": "bound", "label": "端口对外监听", "ok": bool(settings.lan_access and bound),
         "detail": "局域网地址能连上" if bound
                   else "局域网地址连不上：可能改过开关还没重启"},
        {"key": "ip", "label": "电脑的局域网地址", "ok": bool(ip),
         "detail": ip or "拿不到本机地址：可能没连 WiFi"},
    ]
    if seen.get("prev_ip") and seen["changed"]:
        checks.append({
            "key": "ip_changed", "label": "地址没有变", "ok": False,
            "detail": f"变了：{seen['prev_ip']} → {ip}。手机上存的旧链接已失效",
        })

    advice: list[str] = []
    if not settings.lan_access:
        advice.append("先打开上面的开关，然后**重启软件**——监听地址要重启才变。")
    elif not bound:
        advice.append("开关开着但端口没绑上：关掉软件重开一次；如果还是不行，"
                      "看看是不是有另一个 BiliNotes 窗口还占着端口。")
    if seen.get("prev_ip") and seen["changed"]:
        advice.append(f"电脑的地址从 {seen['prev_ip']} 换成了 {ip}"
                      "（换 WiFi、重连路由器、或者 DHCP 到期都会这样）。"
                      "手机上的收藏和截图里那张码都指向旧地址，**重新扫一次码**就好。")
    if looks_isolated(ssid):
        advice.append(f"当前 WiFi 是「{ssid}」，这类校园网/公共网常开"
                      "「设备隔离」：同一个 WiFi 下手机也访问不到电脑。"
                      "这种情况换手机热点最省事（电脑连手机热点，再扫码）。")
    advice.append("手机要用**系统浏览器**打开（微信/QQ 内置浏览器会拦局域网地址），"
                  "并且确认手机没在走流量。")
    advice.append("不想折腾网络的话，直接「导出学习包」发到手机："
                  "那是单文件 HTML，电脑关机也能看。")

    return {
        "ok": bool(settings.lan_access and bound and ip),
        "ip": ip,
        "port": port,
        "ssid": ssid,
        "url": url,
        "ip_changed": bool(seen.get("prev_ip") and seen["changed"]),
        "prev_ip": seen.get("prev_ip", ""),
        "checks": checks,
        "advice": advice,
    }
