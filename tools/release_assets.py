"""发版辅助：给安装包算 SHA256，生成 SHA256SUMS.txt 和 Release 说明。

用法（在项目根目录）：
    python tools/release_assets.py                       # 自动挑 installer/dist 里最新的包
    python tools/release_assets.py installer/dist/xxx.exe

产出都放在 installer/dist/：
    SHA256SUMS.txt            → 和 exe 一起拖进 GitHub Release 的资产区
    release-notes-<版本>.md   → 整篇复制进 Release 说明框（里面也带了哈希，双保险）

不联网、不改动安装包，随时可以重跑。
"""
from __future__ import annotations

import argparse
import hashlib
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "installer" / "dist"


def current_version() -> str:
    text = (ROOT / "app" / "__init__.py").read_text(encoding="utf-8")
    m = re.search(r'__version__\s*=\s*["\']([^"\']+)["\']', text)
    if not m:
        raise SystemExit("读不到 app/__init__.py 里的 __version__")
    return m.group(1)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def pick_exe() -> Path:
    candidates = sorted(DIST.glob("BiliNotes-setup-*.exe"),
                        key=lambda p: p.stat().st_mtime, reverse=True)
    if not candidates:
        raise SystemExit(f"{DIST} 下没有 BiliNotes-setup-*.exe，先编译安装包")
    return candidates[0]


def main() -> int:
    ap = argparse.ArgumentParser(description="生成 Release 校验文件与说明")
    ap.add_argument("exe", nargs="?", help="安装包路径，默认取最新编译的那个")
    args = ap.parse_args()

    exe = Path(args.exe).resolve() if args.exe else pick_exe()
    if not exe.is_file():
        raise SystemExit(f"找不到安装包：{exe}")

    ver = current_version()
    size = exe.stat().st_size
    digest = sha256(exe)

    if ver not in exe.name:
        print(f"[警告] 安装包名 {exe.name} 里没有版本号 {ver}，"
              f"确认一下是不是发错包了（iss 里的 MyAppVersion 也要一致）")

    sums = DIST / "SHA256SUMS.txt"
    sums.write_text(f"{digest}  {exe.name}\n", encoding="utf-8")

    # 上一轮已经写好的「本次更新」要保留：重新生成校验文件时不该把说明清空
    prev_updates = ""
    prev_path = DIST / f"release-notes-{ver}.md"
    if prev_path.is_file():
        old_text = prev_path.read_text(encoding="utf-8")
        m = re.search(r"### 本次更新\n\n(.*?)\n\n### 安装", old_text, re.S)
        if m and "（把这次改了什么" not in m.group(1):
            prev_updates = m.group(1).strip()

    notes = f"""## BiliNotes v{ver}

Windows 一键安装包，内含主程序、ffmpeg 与 VC++ 运行库。已装过的直接覆盖即可，
笔记、设置、单词本都会保留。

- 文件：`{exe.name}`
- 大小：{size / 1024 / 1024:.1f} MB（{size} 字节）
- SHA256：`{digest}`

### 本次更新

{prev_updates or "（把这次改了什么写成短句，一行一条；删掉这行括号）"}

### 安装

1. 下载上面的 `.exe`；
2. 双击运行，系统权限确认点「是」；
3. 装完从桌面快捷方式打开，或在软件「设置 → 软件更新」里检查新版本。

### 校验

`SHA256SUMS.txt` 会作为资产一起上传，程序下载完会自动比对；
上面的 SHA256 也写了同一份值，两种来源任取其一都能通过。
"""
    notes_path = DIST / f"release-notes-{ver}.md"
    notes_path.write_text(notes, encoding="utf-8")

    print(f"版本      {ver}")
    print(f"安装包    {exe}")
    print(f"大小      {size / 1024 / 1024:.1f} MB")
    print(f"SHA256    {digest}")
    print(f"已生成    {sums}")
    print(f"已生成    {notes_path}")
    print("\n建 Release 时：tag 填 v%s，把这两个文件拖进资产区：" % ver)
    print(f"  1. {exe.name}")
    print(f"  2. {sums.name}")
    print(f"再把 {notes_path.name} 的内容整篇贴进说明框，最后点 Publish release。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
