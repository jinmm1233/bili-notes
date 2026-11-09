"""FastAPI 装配：中间件、异常映射、静态资源、启动入口。"""
from __future__ import annotations

import contextlib
import logging
import os
import socket
import threading
import time
import uuid
import webbrowser
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .api.routes import router
from .api.routes_bili import router as bili_router
from .api.routes_cards import router as cards_router
from .api.routes_concepts import router as concepts_router
from .api.routes_env import router as env_router
from .api.routes_library import invalidate_library
from .api.routes_library import router as library_router
from .api.routes_offline import api as offline_data_api
from .api.routes_offline import router as offline_router
from .api.routes_quiz import router as quiz_router
from .api.routes_update import router as update_router
from .api.routes_wordbook import router as wordbook_router
from .core import envcheck
from .core import lan
from .core.config import get_settings, save_overrides
from .core.errors import AppError
from .core.logging import request_id_var, setup_logging
from .core.secretstore import SecretStore
from .services.task_manager import TaskManager
from . import __version__, window

log = logging.getLogger("main")


def create_app() -> FastAPI:
    settings = get_settings()
    setup_logging("INFO", to_file=True, log_dir=settings.logs_dir)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.store = SecretStore(settings.data_dir / "secrets.dat")
        app.state.manager = TaskManager(settings, app.state.store, on_finish=invalidate_library)
        envcheck.probe_async()          # 后台探重型依赖，缺 VC++ 运行库早知道
        log.info("BiliNotes 启动，数据目录 %s", settings.data_dir)
        yield
        app.state.manager.shutdown()
        log.info("BiliNotes 已停止")

    app = FastAPI(title="BiliNotes", version=__version__, lifespan=lifespan, docs_url="/docs")

    @app.middleware("http")
    async def _request_id(request: Request, call_next):
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:8]
        token = request_id_var.set(rid)
        try:
            response = await call_next(request)
            response.headers["x-request-id"] = rid
            return response
        finally:
            request_id_var.reset(token)

    # 双端：只有打开 lan_access 才生效，本机请求一律放行
    lan.install_guard(app)

    @app.exception_handler(AppError)
    async def _app_error(request: Request, exc: AppError) -> JSONResponse:
        rid = request_id_var.get()
        log.warning("业务异常 %s: %s", exc.code, exc.message)
        return JSONResponse(exc.to_dict(rid), status_code=exc.http_status)

    app.include_router(router)
    app.include_router(bili_router)
    app.include_router(library_router)
    app.include_router(cards_router)
    app.include_router(concepts_router)
    app.include_router(quiz_router)
    app.include_router(wordbook_router)
    app.include_router(offline_router)
    app.include_router(offline_data_api)
    app.include_router(env_router)
    app.include_router(update_router)

    @app.get("/healthz")
    def healthz() -> dict:
        # app 字段是给 lan.running_instance 认亲用的：端口通不代表是同一个程序，
        # 靠它才能安全地判断"已经在跑了，别再起第二个"。
        return {"ok": True, "app": "BiliNotes", "version": __version__,
                "runtime": envcheck.cached()}

    static_dir: Path = settings.static_dir
    if not static_dir.is_dir():
        log.error("前端目录不存在：%s", static_dir)
    else:
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

        @app.get("/")
        def index() -> FileResponse:
            # 首页不能缓存：它带着 app.js / app.css 的版本号，缓存住旧的
            # 就会一直加载旧前端（改了界面却看不到效果，排查起来很费劲）
            return FileResponse(
                static_dir / "index.html",
                headers={"cache-control": "no-cache, no-store, must-revalidate"},
            )

    return app


def _port_in_use(port: int) -> bool:
    """端口上是不是真有人在监听。

    绝不能用 bind + SO_REUSEADDR 判断：Windows 的 SO_REUSEADDR 语义和 Linux 相反，
    它**允许绑定到已被别的程序监听的端口**。于是探测"成功"、uvicorn 真正 listen 时
    报 10048，服务根本没起来，可进程还活着、窗口照开——表现就是页面连不上、
    点什么都没反应，而且关窗也退不掉。改用连一下看看：有 listener 才连得上。
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.25)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def pick_port(host: str, preferred: int, span: int = 20) -> int:
    for port in range(preferred, preferred + span):
        if not _port_in_use(port):
            return port
    raise RuntimeError(f"端口 {preferred}~{preferred + span} 都被占用")


def find_running(base: int, span: int = 20) -> tuple[int, str]:
    """已有的 BiliNotes 实例（只找 base~base+span 这一段）。

    为什么必须找：端口被占时 uvicorn 会顺延到 8766、8767…… 于是桌面上同时躺着
    好几个进程，而手机上的链接还指着最早那个。两边口令、开关状态、代码版本都
    可能不一样，表现就是"网址又断了"。与其让用户猜，不如第二次启动直接认领
    第一个实例、把浏览器指过去。
    """
    for port in range(base, base + span):
        version = lan.running_instance("127.0.0.1", port)
        if version:
            return port, version
    return 0, ""


# 「谁是主人」的权威凭据：Windows 命名互斥量。进程活着就持有，退出（含崩溃）
# 由系统自动释放。**不能**用端口探测来判断——那有个致命竞态：旧实例正在关窗退出时
# 端口还通，新实例于是以为"已有实例"，只去开窗口、自己不起服务；下一毫秒旧实例
# 退出、端口释放，剩下的窗口就永远连不上，而新进程又卡在"等窗口关闭"里不退出。
# 表现就是用户看到的那样：窗口在、点不动、关也关不掉。
_LOCK_HANDLE = None          # 必须模块级保活，被 GC 回收等于释放锁
_LOCK_NAME = "Local\\BiliNotes-single-instance"


def _lock_name() -> str:
    """互斥量名字。回归测试用 BILINOTES_LOCK_NAME 换个名字，
    否则正开着软件时跑测试，测试进程永远抢不到锁。"""
    return os.environ.get("BILINOTES_LOCK_NAME") or _LOCK_NAME


def _acquire_instance_lock() -> bool:
    """抢到单实例锁返回 True；已有实例持有则 False。非 Windows 一律放行。"""
    global _LOCK_HANDLE  # noqa: PLW0603
    if os.name != "nt":
        return True
    try:
        import ctypes  # noqa: PLC0415
        from ctypes import wintypes  # noqa: PLC0415

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateMutexW.restype = wintypes.HANDLE
        k32.CreateMutexW.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)
        k32.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = k32.CreateMutexW(None, False, _lock_name())
        already = ctypes.get_last_error() == 183      # ERROR_ALREADY_EXISTS
        if already:
            # 关键：CreateMutex 即使说"已存在"也返回了一个有效句柄，等于我自己
            # 也给这把锁加了引用。不还回去的话，对方退出后锁依然"被占用"，
            # 我们就永远等不到——必须立刻 CloseHandle。
            k32.CloseHandle(handle)
            _LOCK_HANDLE = None
            return False
        _LOCK_HANDLE = handle
        return True
    except Exception as exc:                 # 锁拿不到不该让程序起不来
        log.warning("单实例锁不可用，按单实例处理：%s", exc)
        return True


def run() -> None:
    settings = get_settings()

    # 用一个循环替代"探测端口 → 复用"：先确认自己是不是主人，再决定起服务还是开窗口。
    # 中间允许短暂重试，覆盖"上一个实例刚关窗、正在退出"的那几秒。
    deadline = time.time() + 8.0
    port, version = 0, ""
    while True:
        if _acquire_instance_lock():
            break                            # 我是主人，下面照常启动服务
        port, version = find_running(settings.port)
        if port:                             # 真有实例活着 → 开窗口指向它
            break
        if time.time() > deadline:
            # 锁被占着、端口却不通：对方卡死了。宁可明确报错，也别开一个连不上的窗口。
            print("\n  BiliNotes 似乎还在退出中，请稍等几秒再打开一次。")
            print("  若反复出现，打开任务管理器结束所有 BiliNotes.exe 后重试。\n")
            return
        time.sleep(0.25)

    if port:
        url = f"http://{settings.host}:{port}"
        print(f"\n  BiliNotes 已经在运行（{version}）→  {url}")
        # 已经开着窗口就把它切到前台，然后本进程直接退出。
        # 不要再 open_blocking：那是"新开一个窗口并阻塞等待"，双击 5 次就会
        # 挂着 5 个 136MB 的进程，用户看到的却还是同一个界面。
        if window.available() and window.focus_existing():
            print("  已把现有窗口切到前台。\n")
            return
        print("  直接打开这个地址即可；要重启请先关掉那个窗口。\n")
        if window.available():
            try:                              # 没有窗口（被关了/最小化）才新开一个
                window.open_blocking(url)
                return
            except Exception as exc:
                log.warning("原生窗口打开失败，退回浏览器：%s", exc)
        if settings.open_browser:
            webbrowser.open(url)
        return

    # 开了双端才绑 0.0.0.0；否则只听本机，避免在咖啡厅里裸奔
    bind = "0.0.0.0" if settings.lan_access else settings.host
    app = create_app()
    use_window = window.available()

    if use_window:
        # 端口"探路成功"不等于"真能绑上"——探完到 listen 之间还有空隙。
        # 绑不上就绝不能开窗口：那会留下一个永远连不上、点不动的白窗口。
        server, local_url, port = _serve(app, bind, settings.port, settings.host)
        if server is None:
            print("\n  BiliNotes 启动失败：可用端口都被占用了。")
            print("  请先在任务管理器里结束其他 BiliNotes.exe，再重新打开。\n")
            return
    else:
        port = pick_port(bind, settings.port)
        local_url = f"http://{settings.host}:{port}"
        if settings.open_browser:
            threading.Timer(1.2, lambda: webbrowser.open(local_url)).start()

    lan.note_binding(bind, port)          # 状态页/二维码要报真实端口，不是配置里的那个
    print(f"\n  BiliNotes 已启动  →  {local_url}")
    if settings.lan_access:
        token = settings.lan_token or lan.new_token()
        if not settings.lan_token:
            save_overrides({"lan_token": token})
        ip = lan.lan_ip() or "<本机IP>"
        print(f"  手机访问（同一 WiFi）→  http://{ip}:{port}/?k={token}")
        print("  口令相当于密码，别往外发；可在「设置 → 手机访问」里换一个")
    print("  按 Ctrl+C 退出\n")

    # 应用窗口（Edge --app，不是 pywebview）：窗口关了程序就退出。
    # 起不来（没 Edge / 启动失败）就退回"开默认浏览器 + uvicorn 阻塞"，行为和以前一样。
    if use_window:
        try:
            window.open_blocking(local_url)
            print("\n  BiliNotes 已关闭\n")
            # uvicorn 在 daemon 线程里，主线程直接结束即可
            return
        except Exception as exc:
            log.warning("原生窗口打开失败，退回浏览器：%s", exc)
            webbrowser.open(local_url)
            try:
                server.should_exit = True
            except Exception:
                pass
            uvicorn.run(app, host=bind, port=port, log_config=None, access_log=False)
            return

    uvicorn.run(app, host=bind, port=port, log_config=None, access_log=False)


def _serve(app, bind: str, start: int, host_label: str, span: int = 20):
    """起服务并确认**真的**可用；端口在这期间被抢就换下一个。

    返回 (server, url, port)；全都起不来返回 (None, "", 0)。调用方据此决定
    要不要开窗口——绑不上的端口开窗口，等于给用户一个死页面。
    """
    for port in range(start, start + span):
        if _port_in_use(port):
            continue
        config = uvicorn.Config(app, host=bind, port=port, log_config=None, access_log=False)
        server = uvicorn.Server(config)
        threading.Thread(target=server.run, daemon=True).start()
        url = f"http://{host_label}:{port}"
        if _wait_ready(url, timeout=6.0):
            return server, url, port
        log.warning("端口 %d 没能就绪（可能刚被抢占），换下一个", port)
        with contextlib.suppress(Exception):
            server.should_exit = True
        time.sleep(0.5)
    return None, "", 0


def _wait_ready(url: str, timeout: float = 5.0) -> bool:
    """轮询 healthz，等服务真起来了再开窗口，避免首屏白屏。"""
    import time  # noqa: PLC0415

    from .core.local import urlopen_local  # noqa: PLC0415  本机请求不能走代理

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urlopen_local(f"{url}/healthz", timeout=1.0) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            time.sleep(0.15)
    return False


if __name__ == "__main__":
    run()
