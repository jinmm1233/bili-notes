"""界面渲染验证：逐视图截图并收集浏览器控制台错误。"""
from __future__ import annotations

import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8765"
OUT = Path(__file__).resolve().parents[1] / ".screens"
VIEWS = ["dashboard", "collect", "queue", "library", "settings"]


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

        page = browser.new_page(viewport={"width": 1440, "height": 900}, device_scale_factor=2)
        page.on("console", lambda m: errors.append(f"[{m.type}] {m.text}") if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(f"[pageerror] {e}"))

        page.goto(BASE, wait_until="networkidle")
        page.wait_for_timeout(900)

        for view in VIEWS:
            page.click(f'[data-view="{view}"]')
            page.wait_for_timeout(1100)
            path = OUT / f"{view}.png"
            page.screenshot(path=str(path))
            title = page.inner_text("h1")
            print(f"  {view:10s} 标题「{title}」  → {path.name}")

        # 连接自检是动态渲染的：点一下，确认它真的把检查项画出来了
        page.click('[data-view="settings"]')
        page.wait_for_timeout(600)
        page.click("#btnLanDoctor")
        page.wait_for_function(
            "() => document.querySelectorAll('#lanDoctor .d-row').length >= 4",
            timeout=5000,
        )
        rows = page.eval_on_selector_all(
            "#lanDoctor .d-row .d-label", "els => els.map(e => e.textContent)"
        )
        print(f"  自检面板   {len(rows)} 项：{' / '.join(rows)}")
        page.locator("#lanDoctor").scroll_into_view_if_needed()
        page.wait_for_timeout(300)
        page.screenshot(path=str(OUT / "settings-doctor.png"))

        browser.close()

    print("\n控制台错误：", len(errors))
    for e in errors[:15]:
        print("  ", e[:160])
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
