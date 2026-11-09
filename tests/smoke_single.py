"""单实例判定：命名互斥量必须能正确接管，不能出现"窗口没人管"。

回归的是 2026-11-09 用户遇到的故障：关窗后立刻重开，新进程看到旧实例端口还通，
于是只开窗口、自己不起服务；旧实例随即退出，端口释放，窗口永远连不上，
新进程又卡在"等窗口关闭"里不退出 —— 窗口在、点不动、关不掉。

根因是靠端口探测猜"有没有实例"存在竞态。现在改用 Windows 命名互斥量：
进程活着就持锁，退出由系统自动释放，锁是唯一权威。
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
fails: list[str] = []

# 软件正开着时真实互斥量被它占着，测试自己得用一把独立的锁
os.environ.setdefault("BILINOTES_LOCK_NAME", f"Local\\BiliNotes-test-{os.getpid()}")


def ok(cond: bool, label: str, extra: str = "") -> None:
    print(("  ok  " if cond else "  FAIL") + f"  {label}" + (f"  {extra}" if extra else ""))
    if not cond:
        fails.append(label)


CHILD = textwrap.dedent(
    """
    import sys, time
    sys.path.insert(0, r"{root}")
    from app.main import _acquire_instance_lock
    print("ACQUIRED" if _acquire_instance_lock() else "DENIED", flush=True)
    time.sleep(float(sys.argv[1]))
    """
).format(root=str(ROOT))


def spawn(seconds: float) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", CHILD, str(seconds)],
        stdout=subprocess.PIPE, text=True,
    )


def test_lock() -> None:
    print("\n=== 单实例锁 ===")
    a = spawn(8)
    ok(a.stdout.readline().strip() == "ACQUIRED", "第一个进程拿到锁")

    b = spawn(1)
    ok(b.stdout.readline().strip() == "DENIED", "第二个进程被拒（不会起第二个服务）")
    b.wait(timeout=15)

    # 核心场景：B 在 A 活着时被拒，随后 A 立刻退出 —— 新进程必须能接管。
    # 修复前这里会因为"我们自己还给这把锁加了引用"而永久拿不到。
    c = spawn(5)
    ok(c.stdout.readline().strip() == "DENIED", "抢锁期间的第三个进程也被拒")

    a.terminate()
    a.wait(timeout=15)

    d = spawn(1)
    got = d.stdout.readline().strip()
    ok(got == "ACQUIRED", "持有者退出后，新进程立刻能接管", f"得到 {got}")
    d.wait(timeout=15)

    c.terminate()
    c.wait(timeout=15)


def test_no_stale_handle() -> None:
    """被拒时不能留下自己的句柄，否则锁永远'被占用'。"""
    print("\n=== 被拒后不残留引用 ===")
    import importlib

    sys.path.insert(0, str(ROOT))
    main = importlib.import_module("app.main")

    ok(main._acquire_instance_lock() is True, "同进程首次拿锁成功")
    # 同进程第二次走的是"已存在"分支，必须还掉句柄并返回 False
    ok(main._acquire_instance_lock() is False, "重复获取返回 False")
    ok(main._LOCK_HANDLE is None, "被拒时不保留句柄（否则锁会被自己占死）")


def test_port_probe() -> None:
    """端口探测：Windows 的 SO_REUSEADDR 会骗人，必须用 connect 判断。

    回归的是 2026-11-09 用户看到的「点都点不动」：旧实现用 bind+SO_REUSEADDR
    探路，Windows 允许绑定到已被监听的端口，于是"探测通过"，uvicorn 真正
    listen 时报 10048，服务没起来、进程却活着，窗口就是死页面。
    """
    print("\n=== 端口探测 ===")
    import importlib
    import socket

    sys.path.insert(0, str(ROOT))
    main_mod = importlib.import_module("app.main")

    free_port = 18899
    ok(main_mod._port_in_use(free_port) is False, "空闲端口判定为未占用")

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", free_port))
    srv.listen(5)
    try:
        ok(main_mod._port_in_use(free_port) is True, "监听中的端口判定为已占用")

        # 对照：旧探测法在 Windows 上会误判成空闲（这正是服务起不来的原因）
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("0.0.0.0", free_port))
            old_free = True
        except OSError:
            old_free = False
        finally:
            probe.close()
        print(f"  [对照] 旧探测法(windows) 认为占用中的端口 {'空闲 → 会误判' if old_free else '已占用'}")

        picked = main_mod.pick_port("0.0.0.0", free_port)
        ok(picked != free_port, "pick_port 跳过被占端口", f"选到 {picked}")
    finally:
        srv.close()


def main() -> int:
    test_lock()
    test_no_stale_handle()
    test_port_probe()
    print("\n" + ("单实例判定全通过" if not fails else f"{len(fails)} 项失败: {fails}"))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
