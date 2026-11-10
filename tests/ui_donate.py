"""打赏入口的界面验证：四个金额按钮 → 点开收款码。

跑法：
    python -m app.main          # 另开终端起服务
    python tests/ui_donate.py
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

        page = browser.new_page(viewport={"width": 1280, "height": 900}, device_scale_factor=2)
        page.on("pageerror", lambda e: errors.append(f"[pageerror] {e}"))
        page.goto(BASE, wait_until="networkidle")
        page.wait_for_timeout(900)
        page.click('[data-view="settings"]')
        page.wait_for_timeout(1200)

        opts = page.locator(".donate-opt")
        ok(opts.count() == 4, f"四个金额按钮（实际 {opts.count()}）")
        labels = [opts.nth(i).inner_text().replace("\n", " ") for i in range(opts.count())]
        print("  按钮：", labels)
        for want in ("3.33", "6.66", "8.88", "16.88"):
            ok(any(want in x for x in labels), f"有 {want} 的按钮")
        for want in ("烤肠", "奶茶", "鸡腿", "午饭"):
            ok(any(want in x for x in labels), f"有「{want}」")

        ok(page.locator("#donateBox").is_hidden(), "一开始不显示收款码")
        opts.nth(1).click()                      # 6.66 奶茶
        page.wait_for_timeout(700)
        ok(page.locator("#donateBox").is_visible(), "点按钮后出现收款码")
        src = page.get_attribute("#donateImg", "src") or ""
        ok("donate-6.66.png" in src, f"显示的是对应金额的码（{src}）")
        loaded = page.evaluate(
            "() => { const i = document.getElementById('donateImg');"
            " return i.complete && i.naturalWidth > 0; }")
        ok(loaded, "图片真的加载出来了（不是 404）")
        tip = page.inner_text("#donateTip")
        ok("6.66" in tip, f"提示写明金额（{tip}）")

        opts.nth(3).click()                      # 换 16.88
        page.wait_for_timeout(700)
        src2 = page.get_attribute("#donateImg", "src") or ""
        ok("donate-16.88.png" in src2, f"切换金额后码也跟着换（{src2}）")

        page.locator("#donateBox").scroll_into_view_if_needed()
        page.wait_for_timeout(400)
        page.screenshot(path=str(OUT / "settings_donate.png"))
        print(f"  截图：{OUT / 'settings_donate.png'}")

        opts.nth(3).click()                      # 再点一次收起
        page.wait_for_timeout(500)
        ok(page.locator("#donateBox").is_hidden(), "再点同一个按钮会收起")

        ok(not errors, f"没有 JS 运行时错误（{len(errors)} 条）")
        for e in errors[:3]:
            print("   ", e[:160])
        browser.close()

    print()
    if FAILED:
        print(f"{FAILED} 项未通过")
        return 1
    print("打赏入口验证通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
