"""PyInstaller 打包入口。

不能直接把 app/main.py 交给 Analysis：PyInstaller 会把它当顶层脚本执行，
而它用的是包内相对导入（from .api.routes import router），
启动即 ImportError: attempted relative import with no known parent package。
这里以「包」的方式导入再调用，相对导入才成立。
"""
import contextlib
import os
import sys

# 控制台默认是 GBK，启动横幅里的中文会变乱码（"BiliNotes 已启动" 全是问号）。
# 打包后没法让用户先 chcp 65001，只能在进程里改。
#
# 窗口版（console=False）里 stdout/stderr 是 None——print() 和任何往 stderr
# 写的日志都会直接把进程打死（表现为双击后一闪就没了）。先接到 devnull。
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115
for _stream in (sys.stdout, sys.stderr):
    with contextlib.suppress(Exception):
        _stream.reconfigure(encoding="utf-8", errors="replace")

# 段错误取证：冻结后的原生崩溃没有 Python 栈，日志也停在崩溃前最后一行，
# 光看 app.log 分不清是「谁把进程打死」。faulthandler 会把 C 层栈落到 crash.log，
# 双击闪退时让用户把这个文件发过来就能定位。BILINOTES_CRASHLOG=0 可关。
if os.environ.get("BILINOTES_CRASHLOG", "1") != "0":
    import faulthandler

    try:
        _crash_dir = os.path.join(
            os.environ.get("APPDATA") or os.path.expanduser("~"), "BiliNotes", "logs"
        )
        os.makedirs(_crash_dir, exist_ok=True)
        _crash = open(  # noqa: SIM115  —— 进程级句柄，要活到退出
            os.path.join(_crash_dir, "crash.log"), "a", encoding="utf-8", buffering=1
        )
        faulthandler.enable(file=_crash, all_threads=True)
    except Exception:
        with contextlib.suppress(Exception):
            faulthandler.enable()

_probe = os.environ.get("BILINOTES_PROBE")
if _probe:
    _out = os.environ.get("BILINOTES_PROBE_OUT")

    def _load_all() -> None:
        for _mod in (m.strip() for m in _probe.split(",")):
            if _mod:
                __import__(_mod)

    if _probe == "bn_selftest":
        # 功能自检：证明冻出来的包里音画锚定这类新逻辑真能跑
        try:
            from app.core.selftest import main as _selftest_main
        except BaseException as exc:                  # noqa: BLE001
            if _out:
                with contextlib.suppress(Exception):
                    with open(_out, "w", encoding="utf-8") as _f:
                        _f.write(f"{type(exc).__name__}: {exc}")
            os._exit(3)
        os._exit(_selftest_main())

    try:
        if os.environ.get("BILINOTES_PROBE_THREAD") == "1":
            import threading

            _box: list[BaseException] = []

            def _worker() -> None:
                try:
                    _load_all()
                except BaseException as exc:      # noqa: BLE001
                    _box.append(exc)

            _t = threading.Thread(target=_worker)
            _t.start()
            _t.join()
            if _box:
                raise _box[0]
        else:
            _load_all()
    except BaseException as exc:                  # noqa: BLE001
        if _out:
            with contextlib.suppress(Exception):
                with open(_out, "w", encoding="utf-8") as _f:
                    _f.write(f"{type(exc).__name__}: {exc}")
        os._exit(3)
    os._exit(0)

from app.main import run  # noqa: E402

if __name__ == "__main__":
    run()
