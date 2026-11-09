"""双端访问回归：口令中间件、登录页、换口令。

不联网、不真监听端口——只用 TestClient 换来源地址来模拟"手机"。
  python tests/smoke_lan.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "vendor"))

from fastapi import FastAPI  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

from app.api.routes import router  # noqa: E402
from app.core import config as cfg  # noqa: E402
from app.core import lan  # noqa: E402
from app.core.config import Settings  # noqa: E402

FAILED = 0
PHONE = ("192.168.1.23", 51000)
ME = ("127.0.0.1", 51001)


def ok(cond: bool, msg: str) -> None:
    global FAILED
    print(("[OK] " if cond else "[FAIL] ") + msg)
    if not cond:
        FAILED += 1


def build_app() -> FastAPI:
    app = FastAPI()
    lan.install_guard(app)
    app.include_router(router)

    @app.get("/")
    def root() -> dict:
        return {"ok": True}

    @app.get("/api/ping")
    def ping() -> dict:
        return {"ok": True}

    return app


def use(tmp: Path, **kwargs) -> Settings:
    settings = Settings(data_dir=tmp, output_dir=tmp / "out", **kwargs)
    cfg._settings = settings          # 让 get_settings() 返回这份（测试专用）
    return settings


def test_token_guard(tmp: Path) -> None:
    use(tmp, lan_access=True, lan_token="abc123")
    app = build_app()

    phone = TestClient(app, client=PHONE)
    mine = TestClient(app, client=ME)

    r = mine.get("/api/ping")
    ok(r.status_code == 200, "本机请求不需要口令")

    r = phone.get("/api/ping")
    ok(r.status_code == 401, f"手机没带口令 → 401（实际 {r.status_code}）")

    r = phone.get("/")
    ok(r.status_code == 401 and "访问口令" in r.text, "手机打开首页 → 登录页")

    r = phone.get("/?k=wrong")
    ok(r.status_code == 401, "口令不对还是登录页")

    r = phone.get("/?k=abc123", follow_redirects=False)
    ok(r.status_code == 200 and r.json() == {"ok": True}, "带对口令可以进")
    ok(r.cookies.get(lan.COOKIE) == "abc123", "带口令访问后种了 cookie，手机不用每次输")

    # cookie 之后：不带 ?k= 也能进
    phone2 = TestClient(app, client=PHONE, cookies={lan.COOKIE: "abc123"})
    ok(phone2.get("/api/ping").status_code == 200, "有 cookie 之后免口令")

    r = phone.get("/api/ping", headers={"x-bn-key": "abc123"})
    ok(r.status_code == 200, "也认 X-BN-Key 头（给脚本/快捷指令用）")

    # 换口令：旧链接立刻失效（不用重启）
    use(tmp, lan_access=True, lan_token="new999")
    ok(phone.get("/?k=abc123").status_code == 401, "换口令后旧链接立即失效")
    ok(phone.get("/?k=new999").status_code == 200, "新口令可用")


def test_qr(tmp: Path) -> None:
    """二维码：内联成 data-uri 下发，且只给"本机或已带口令"的人。

    设置页原来用 <img src="/api/lan/qr">，那是第二次请求；从局域网地址打开
    设置页时会被 403，页面上只剩破图。现在跟着状态一起下发。
    """
    use(tmp, lan_access=True, lan_token="abc123")
    app = build_app()
    mine = TestClient(app, client=ME)
    phone = TestClient(app, client=PHONE)

    r = mine.get("/api/lan")
    ok(r.status_code == 200 and "qr" not in r.json(), "不传 qr=1 就不生成二维码（省流量）")
    ok("port_open" in r.json(), "回报端口是否还在对外监听（关掉后是否要重启看它）")

    r = mine.get("/api/lan", params={"qr": 1})
    body = r.json()
    ok(r.status_code == 200, "本机能取状态")
    ok(str(body.get("qr", "")).startswith("data:image/svg+xml;base64,"),
       "二维码内联成 data-uri（不再发第二次图片请求）")

    r = mine.get("/api/lan/qr")
    ok(r.status_code == 200 and r.headers["content-type"].startswith("image/svg+xml"),
       "独立接口在本机仍可直接取 SVG")
    ok("<svg" in r.text, "返回的确实是 SVG")

    ok(phone.get("/api/lan/qr").status_code == 401, "外部没口令 → 拿不到码（401）")
    phone.cookies.set(lan.COOKIE, "abc123")
    ok(phone.get("/api/lan/qr").status_code == 200, "外部已带正确口令 → 可以取码")
    phone.cookies.set(lan.COOKIE, "wrong")
    ok(phone.get("/api/lan/qr").status_code == 401, "口令不对 → 还是拿不到")


def test_login_route(tmp: Path) -> None:
    use(tmp, lan_access=True, lan_token="abc123")
    app = build_app()
    phone = TestClient(app, client=PHONE)

    r = phone.post("/api/lan/login", data={"k": "abc123"}, follow_redirects=False)
    ok(r.status_code == 303 and r.cookies.get(lan.COOKIE) == "abc123",
       "登录表单：口令对 → 种 cookie 并跳回首页")
    ok(r.headers.get("location") == "/", "跳回首页")

    r = phone.post("/api/lan/login", data={"k": "nope"})
    ok(r.status_code == 401 and "口令不对" in r.text, "登录表单：口令错 → 提示重试")


def test_closed(tmp: Path) -> None:
    """开关关掉但进程还绑在 0.0.0.0 时，端口仍开着，必须继续挡。"""
    use(tmp, lan_access=False, lan_token="abc123")
    app = build_app()
    phone = TestClient(app, client=PHONE)
    mine = TestClient(app, client=ME)

    ok(mine.get("/api/ping").status_code == 200, "本机照常可用")
    r = phone.get("/api/ping")
    ok(r.status_code == 403, f"外部请求被拒（实际 {r.status_code}）")
    r = phone.get("/")
    ok(r.status_code == 403 and "<form" not in r.text, "关掉时给提示页，且不再显示口令输入框")


def test_page() -> None:
    page = lan.login_page()
    ok("<form" in page and "/api/lan/login" in page, "登录页带表单")
    ok("width=device-width" in page, "登录页是移动端视口")
    ok("<form" not in lan.login_page("已关闭", form=False), "关闭提示页不出现表单")
    token = lan.new_token()
    ok(len(token) == 12 and token.isascii(), f"口令 12 位且好输（{token}）")


def test_bound_port(tmp: Path) -> None:
    """端口被占时 pick_port 会顺延，状态页必须报真实那个。

    报错的后果很隐蔽：手机上扫到的码指向另一个实例，口令/开关状态都不一样，
    表现就是"网址又断了"。
    """
    use(tmp, lan_access=True, lan_token="abc123")
    app = build_app()
    mine = TestClient(app, client=ME)
    try:
        lan.note_binding("0.0.0.0", 8899)
        body = mine.get("/api/lan").json()
        ok(body["port"] == 8899, f"状态页报真实绑定端口（实际 {body['port']}）")
        if body["ip"]:
            ok(":8899/" in body["url"], f"链接跟着换端口（{body['url']}）")
    finally:
        lan.note_binding("", 0)          # 复位，别影响后面的用例
    ok(mine.get("/api/lan").json()["port"] == 8765, "没有绑定记录时退回配置端口")


def test_ip_change(tmp: Path) -> None:
    """地址变更要能记住旧值：这是"换 WiFi 后手机打不开"的唯一线索。"""
    use(tmp, lan_access=True, lan_token="abc123")
    lan.remember("10.0.0.5", "http://10.0.0.5:8765/?k=x")
    r = lan.remember("10.0.0.5", "http://10.0.0.5:8765/?k=x")
    ok(r["changed"] is False, "地址没变 → 不报变更")
    r = lan.remember("10.0.0.9", "http://10.0.0.9:8765/?k=x")
    ok(r["changed"] is True and r["prev_ip"] == "10.0.0.5",
       f"地址变了 → 报变更并记住旧值（{r['prev_ip']}）")
    ok(lan.looks_isolated("GDUFE") and not lan.looks_isolated("TP-LINK_5G"),
       "认得出校园网，也不把家用路由器当隔离网络")


def test_doctor(tmp: Path) -> None:
    """体检接口：开关没开时必须明确指出"卡在哪"，而不是让人去猜防火墙。"""
    use(tmp, lan_access=False, lan_token="abc123")
    app = build_app()
    mine = TestClient(app, client=ME)

    r = mine.get("/api/lan/doctor")
    d = r.json()
    ok(r.status_code == 200 and r.headers.get("cache-control") == "no-store",
       "体检结果不缓存")
    keys = {c["key"] for c in d.get("checks", [])}
    ok({"service", "enabled", "bound", "ip"} <= keys, f"四项基础检查都在（{sorted(keys)}）")
    ok(d["ok"] is False, "开关没开 → 结论是还不通")
    ok(any("开关" in a for a in d["advice"]), "给出「先打开开关」这个具体动作")
    ok(all(c["detail"] for c in d["checks"]), "每项都有可读的说明")


def test_local_bypasses_proxy():
    """系统代理指向死端口时，本机 healthz 仍要能读到。

    这条守着一个真实事故：HTTP_PROXY 有效但 NO_PROXY 为空时，
    find_running() 会拿不到 /healthz → 双击图标又起一个实例 → 端口顺延 →
    手机书签全指向旧地址，用户只看到「网址又断了」。
    """
    import http.server
    import json
    import os
    import threading
    import urllib.request

    from app.core import local as L

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"app":"BiliNotes","version":"x"}')

        def log_message(self, *args):
            pass

    # ProxyHandler({}) 不注册任何 *_open 方法，OpenerDirector 会直接跳过它，
    # 于是这个 opener 里一个代理 handler 都没有（这正是我们要的效果）。
    proxy_handlers = [h for h in L._opener.handlers
                      if isinstance(h, urllib.request.ProxyHandler)]
    ok(not proxy_handlers, "本机 opener 不含任何代理 handler")

    srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    url = f"http://127.0.0.1:{port}/healthz"
    keys = ("HTTP_PROXY", "http_proxy", "NO_PROXY", "no_proxy")
    saved = {k: os.environ.get(k) for k in keys}
    saved_opener = urllib.request._opener
    try:
        os.environ["HTTP_PROXY"] = "http://127.0.0.1:9"
        os.environ["http_proxy"] = "http://127.0.0.1:9"
        os.environ.pop("NO_PROXY", None)
        os.environ.pop("no_proxy", None)
        urllib.request._opener = None       # 让对照组重新读环境里的代理

        with L.urlopen_local(url, timeout=2) as r:
            data = json.loads(r.read().decode("utf-8"))
        ok(data.get("app") == "BiliNotes", "假代理下仍能直连本机 healthz")
        ok(lan.running_instance("127.0.0.1", port) == "x",
           "已运行实例仍能被认出，不会误起第二个进程")

        direct_ok = False
        try:
            with urllib.request.urlopen(url, timeout=1) as r:
                direct_ok = r.status == 200
        except Exception:
            pass
        ok(not direct_ok, "对照组：普通 urlopen 确实被假代理拦住")
    finally:
        urllib.request._opener = saved_opener
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        srv.shutdown()
        srv.server_close()


def main() -> int:
    print("=== 口令中间件 ===")
    with tempfile.TemporaryDirectory() as raw:
        test_token_guard(Path(raw))
    print("\n=== 登录表单 ===")
    with tempfile.TemporaryDirectory() as raw:
        test_login_route(Path(raw))
    print("\n=== 关闭后仍要挡住 ===")
    with tempfile.TemporaryDirectory() as raw:
        test_closed(Path(raw))
    print("\n=== 二维码 ===")
    with tempfile.TemporaryDirectory() as raw:
        test_qr(Path(raw))
    print("\n=== 端口与地址 ===")
    with tempfile.TemporaryDirectory() as raw:
        test_bound_port(Path(raw))
        test_ip_change(Path(raw))
    print("\n=== 连接自检 ===")
    with tempfile.TemporaryDirectory() as raw:
        test_doctor(Path(raw))
    print("\n=== 本机请求绕开系统代理 ===")
    test_local_bypasses_proxy()
    print("\n=== 登录页 ===")
    test_page()
    cfg._settings = None
    print()
    if FAILED:
        print(f"{FAILED} 项未通过")
        return 1
    print("双端访问全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
