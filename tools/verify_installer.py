"""安装包验收：验证安装包能在一台「干净的机器」上真正跑起来。

为什么要单独验这个：产物验收（verify_bundle.py）只验打包出来的目录，
全新机模拟（verify_fresh_install.py）验的是开发版产物 + 空的用户目录。
但用户拿到手的是一个**安装包 exe**，中间隔着 Inno Setup 这一层：
装到哪、快捷方式建没建、VC++ 运行库装没装、卸干净没有 —— 这些只有真装一次才知道。

怎么模拟「别的机器」：
  - 装到一个全新的目录（不是 D:\\BiliNotes，也不是 Program Files）
  - APPDATA / USERPROFILE / HOME 全部指向临时目录 → 用户数据是全新的
  - 装完把环境变量还原，再启动装的程序，验证服务能起来、接口能用
  - 卸载后确认安装目录被清掉、用户数据目录还在（用户数据不该被卸载删掉）

跑法：python tools/verify_installer.py --setup installer/dist/BiliNotes-setup-0.2.6.exe
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
LOCAL = urllib.request.build_opener(urllib.request.ProxyHandler({}))
results: list[tuple[bool, str, str]] = []


def record(ok: bool, name: str, detail: str = "") -> bool:
    results.append((ok, name, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))
    return ok


def free_port() -> int:
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_health(base: str, timeout: float = 45.0) -> tuple[bool, str]:
    """等服务的 /api/health 起来。"""
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        try:
            with LOCAL.open(base + "/healthz", timeout=4) as r:
                if r.status == 200:
                    body = r.read().decode("utf-8", "replace")
                    return True, body[:120]
        except Exception as exc:  # noqa: BLE001
            last = f"{type(exc).__name__}: {exc}"[:100]
        time.sleep(1.0)
    return False, last


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--setup", required=True, help="安装包路径")
    ap.add_argument("--install-dir", default="", help="装到哪（默认用临时目录）")
    ap.add_argument("--keep", action="store_true", help="保留临时目录")
    args = ap.parse_args()

    setup = Path(args.setup).resolve()
    print(f"安装包: {setup}")
    if not setup.is_file():
        print("安装包不存在")
        return 1

    tmp_root = Path(tempfile.mkdtemp(prefix="bn_installer_"))
    install_dir = Path(args.install_dir) if args.install_dir else tmp_root / "BiliNotes"
    fake_appdata = tmp_root / "AppData" / "Roaming"
    fake_userprofile = tmp_root / "UserProfile"
    fake_home = tmp_root / "Home"
    for d in (fake_appdata, fake_userprofile, fake_home):
        d.mkdir(parents=True, exist_ok=True)

    print(f"\n=== 1. 安装包静态检查 ===")
    size = setup.stat().st_size
    record(size > 50 * 1024 * 1024, "安装包体积合理", f"{size/1024/1024:.1f} MB")

    import hashlib
    h = hashlib.sha256(setup.read_bytes()).hexdigest()
    record(len(h) == 64, "SHA256 可计算", h[:16] + "…")

    # 版本号要在文件名和 PE 版本信息里都对
    m = re.search(r"setup-(\d+\.\d+\.\d+)\.exe$", setup.name)
    record(bool(m), "文件名含版本号", m.group(1) if m else setup.name)

    print(f"\n=== 2. 静默安装（模拟另一台机器）===")
    log = tmp_root / "install.log"
    env = dict(os.environ)
    env["APPDATA"] = str(fake_appdata)
    env["USERPROFILE"] = str(fake_userprofile)
    env["HOME"] = str(fake_home)
    env["LOCALAPPDATA"] = str(tmp_root / "AppData" / "Local")
    Path(env["LOCALAPPDATA"]).mkdir(parents=True, exist_ok=True)

    cmd = [
        str(setup), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART",
        f"/DIR={install_dir}", f"/LOG={log}",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=900, env=env)
    record(proc.returncode == 0, "安装程序退出码为 0", f"returncode={proc.returncode}")
    if proc.returncode != 0:
        tail = ""
        if log.is_file():
            tail = "\n".join(log.read_text(encoding="utf-8", errors="replace").splitlines()[-12:])
        print(f"安装日志尾部:\n{tail}")

    print(f"\n=== 3. 安装内容检查 ===")
    exe = install_dir / "BiliNotes.exe"
    console = install_dir / "BiliNotes-console.exe"
    record(exe.is_file(), "主程序存在", str(exe))
    record(console.is_file(), "控制台版存在")
    record((install_dir / "_internal" / "web" / "index.html").is_file(), "前端资源存在")
    record((install_dir / "_internal" / "zhconv" / "zhcdict.json").is_file(),
           "繁简转换字典已打包")
    record((install_dir / "redist" / "vc_redist.x64.exe").is_file(), "VC++ 运行库已随包")

    # PE 版本信息
    try:
        import win32api  # type: ignore
        info = win32api.GetFileVersionInfo(str(exe), "\\")
        ms = win32api.FileVersionInfoLookup(info, "\\VarFileInfo\\Translation")
        key = f"\\VarFileInfo\\Translation\\{ms[0]:04x}{ms[1]:04x}\\FileVersion"
        ver = win32api.FileVersionInfoLookup(info, key)
        record(m is not None and ver.startswith(m.group(1)),
               "PE 版本信息与文件名一致", ver)
    except ImportError:
        # 没装 pywin32 就跳过这���，不影响其余验收
        record(True, "PE 版本信息检查（跳过：未装 pywin32）")

    # 桌面快捷方式（公共桌面，管理员安装写这里）
    shortcut = Path(r"C:\Users\Public\Desktop\BiliNotes.lnk")
    record(shortcut.is_file(), "公共桌面快捷方式已创建", str(shortcut))
    start_menu = Path(r"C:\ProgramData\Microsoft\Windows\Start Menu\Programs\BiliNotes")
    record(start_menu.is_dir(), "开始菜单快捷方式已创建", str(start_menu))

    # 泄露检查：包里不能有打包机的用户名
    data = setup.read_bytes()
    record(os.environ.get("USERNAME", "").encode() not in data,
           "安装包不含打包机用户名")

    print(f"\n=== 4. 启动安装好的程序（全新用户目录）===")
    env2 = dict(env)
    env2["BILINOTES_WINDOW"] = "0"
    env2["BILINOTES_PROBE"] = ""
    p = subprocess.Popen([str(exe)], env=env2, cwd=str(install_dir),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=NO_WINDOW)
    # 程序端口被占时会自动往后找（8765→8769），挨个试
    ok, detail, base = False, "", ""
    for port in (8765, 8766, 8767, 8768, 8769, free_port()):
        ok, detail = wait_health(f"http://127.0.0.1:{port}")
        if ok:
            base = f"http://127.0.0.1:{port}"
            break
    record(ok, "服务启动并通过健康检查", detail)

    if ok:
        try:
            with LOCAL.open(base + "/api/settings", timeout=8) as r:
                j = json.loads(r.read().decode())
            record(bool(j.get("version")), "接口返回版本号", str(j.get("version")))
            record("vision_frame_judge" in j, "看图开关字段存在")
        except Exception as exc:  # noqa: BLE001
            record(False, "读取设置接口", str(exc)[:80])

        # 全新用户目录：不该写脏真实目录
        real_appdata = Path(os.environ["APPDATA"]) / "BiliNotes"
        record(not real_appdata.exists() or not any(real_appdata.iterdir()),
               "未写入真实用户目录", str(real_appdata))
        # 临时 APPDATA 下应建出数据目录
        record((fake_appdata / "BiliNotes").is_dir(), "在新的 APPDATA 下建出数据目录")

    print(f"\n=== 5. 卸载 ===")
    p.terminate()
    try:
        p.wait(timeout=20)
    except Exception:  # noqa: BLE001
        p.kill()

    uninst = install_dir / "unins000.exe"
    if uninst.is_file():
        u = subprocess.run([str(uninst), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"],
                           capture_output=True, text=True, timeout=300, env=env)
        record(u.returncode == 0, "卸载程序退出码为 0", f"returncode={u.returncode}")
        time.sleep(3)
        leftovers = [p.name for p in install_dir.iterdir()] if install_dir.is_dir() else []
        record(not leftovers, "卸载后安装目录已清空", str(leftovers[:5]))
    else:
        record(False, "找到卸载程序", str(uninst))

    # 用户数据目录不该被卸载删掉
    record((fake_appdata / "BiliNotes").is_dir(), "卸载未删除用户数据目录")

    if not args.keep:
        shutil.rmtree(tmp_root, ignore_errors=True)
    else:
        print(f"\n临时目录保留在: {tmp_root}")

    passed = sum(1 for ok, _, _ in results if ok)
    failed = len(results) - passed
    print(f"\n{'=' * 60}")
    print(f"安装包验收：{passed} 项通过，{failed} 项失败")
    if failed:
        for ok, name, detail in results:
            if not ok:
                print(f"  FAIL {name}  {detail}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
