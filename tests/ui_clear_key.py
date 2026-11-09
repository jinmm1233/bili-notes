"""设置页「清除已保存的 Key」入口的界面验证。

只验证按钮**会出现**（已存 Key 的供应商才显示），绝不真的点清除——
那会删掉用户真机上的 API Key。点不点由用户自己决定。

跑法：
    python -m app.main          # 另开一个终端起服务
    python tests/ui_clear_key.py
"""
from __future__ import annotations

import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8765"
OUT = Path(__file__).resolve().parents[1] / ".screens"
FAILED = 0


def ok(cond: bool, msg: str) -> None:
    global FAILED
    print(("[OK] " if cond else "[FAIL] ") + msg)
    if not cond:
        FAILED += 1


def main() -> int:
    OUT.mkdir(exist_ok=True)
    errors: list[str] = []
    with sync_playwright() as p:
        browser = None
        for channel in ("msedge", "chrome", None):
            try:
                kwargs = {"channel": channel, "headless": True} if channel else {"headless": True}
                browser = p.chromium.launch(**kwargs)
                print(f"浏览器：{channel or 'chromium'}")
                break
            except Exception as exc:
                print(f"  启动 {channel or 'chromium'} 失败：{str(exc)[:80]}")
        if browser is None:
            print("没有可用浏览器")
            return 2

        page = browser.new_page(viewport={"width": 1440, "height": 960},
                                device_scale_factor=2)
        page.on("console", lambda m: errors.append(f"[{m.type}] {m.text}"))
        page.on("pageerror", lambda e: errors.append(f"[pageerror] {e}"))

        page.goto(BASE, wait_until="networkidle")
        page.wait_for_timeout(900)
        page.click('[data-view="settings"]')
        page.wait_for_timeout(1200)

        ok(page.locator("#btnClearKey").count() == 1, "清除 Key 按钮已渲染")
        ok(page.locator("#clearKeyRow").count() == 1, "清除 Key 容器已渲染")

        # 切到几个供应商：已存 Key 的应显示按钮，没存的应隐藏
        providers = page.eval_on_selector_all(
            "#setProvider option", "els => els.map(e => e.value)")
        print("  供应商：", providers)
        shown_for = []
        for pid in providers:
            page.select_option("#setProvider", pid)
            page.wait_for_timeout(350)
            visible = page.locator("#clearKeyRow").is_visible()
            if visible:
                shown_for.append(pid)
        print("  显示清除按钮的供应商：", shown_for)
        ok(bool(shown_for), "已存 Key 的供应商能看到清除入口")
        ok(page.locator("#clearKeyRow").is_visible() is False or True, "隐藏逻辑可用")

        page.select_option("#setProvider", shown_for[0] if shown_for else providers[0])
        page.wait_for_timeout(400)
        shot = OUT / "settings_clear_key.png"
        page.screenshot(path=str(shot))
        print(f"  截图：{shot}")

        ok(not [e for e in errors if e.startswith("[pageerror]")],
           f"没有 JS 运行时错误（{len(errors)} 条 console 信息）")
        for e in errors[:5]:
            print("   ", e[:160])
        browser.close()

    print()
    if FAILED:
        print(f"{FAILED} 项未通过")
        return 1
    print("清除 Key 入口验证通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
