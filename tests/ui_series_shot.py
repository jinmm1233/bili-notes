"""界面验证：收藏夹 → 合集卡 → 合集内分集，三级浏览真的点得进去。

跑法：先起本地服务（BILINOTES_WINDOW=0），再 `python tests/ui_series_shot.py`。
截图落在 .screens/，同时收集浏览器控制台报错。
"""
from __future__ import annotations

import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8765"
OUT = Path(__file__).resolve().parents[1] / ".screens"
OUT.mkdir(exist_ok=True)
FAILS: list[str] = []


def check(cond: bool, label: str, extra: str = "") -> None:
    print(("  ok  " if cond else "  FAIL") + f"  {label}" + (f"  {extra}" if extra else ""))
    if not cond:
        FAILS.append(label)


def main() -> int:
    errors: list[str] = []
    with sync_playwright() as p:
        browser = None
        for channel in ("msedge", "chrome", None):
            try:
                kwargs = {"channel": channel, "headless": True} if channel else {"headless": True}
                browser = p.chromium.launch(**kwargs)
                break
            except Exception:
                continue
        if browser is None:
            print("FAIL  没有可用浏览器")
            return 1
        page = browser.new_page(viewport={"width": 1400, "height": 950})
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)

        page.goto(BASE + "/#library", wait_until="networkidle")
        page.wait_for_selector("#kbGrid .collection-card", timeout=15000)
        collections = page.locator("#kbGrid .collection-card").count()
        check(collections >= 1, f"第一级：{collections} 个收藏夹")
        page.screenshot(path=str(OUT / "series-1-collections.png"), full_page=True)

        # 进「默认收藏夹」（也可能是别的名字，取含笔记最多的那个）
        cards = page.locator("#kbGrid .collection-card")
        target = None
        for i in range(cards.count()):
            if "默认收藏夹" in cards.nth(i).inner_text():
                target = cards.nth(i)
                break
        target = target or cards.first
        target.click()
        page.wait_for_selector("#kbGrid .series-card", timeout=15000)
        series_cards = page.locator("#kbGrid .series-card").count()
        check(series_cards >= 1, f"第二级：{series_cards} 张合集卡（已折叠）")
        title = page.locator("#kbGrid .series-card .note-title").first.inner_text()
        badge = page.locator("#kbGrid .series-card .series-badge").first.inner_text()
        check("CSAPP" in title, f"合集名取自公共前缀：{title}")
        check("合集" in badge and "集" in badge, f"角标写明集数：{badge}")
        page.screenshot(path=str(OUT / "series-2-folded.png"), full_page=True)

        # 第三级：点进去看全部 7 集，按集数顺序
        page.locator("#kbGrid .series-card").first.click()
        page.wait_for_selector("#kbGrid .note-card[data-note]", timeout=15000)
        parts = page.locator("#kbGrid .note-card[data-note]").count()
        check(parts >= 2, f"第三级：展开出 {parts} 集")
        crumb = page.locator("#libCrumbName").inner_text()
        check("›" in crumb and "CSAPP" in crumb, f"面包屑显示当前合集：{crumb}")
        back_label = page.locator("#btnLibBack").inner_text()
        check("返回收藏夹" in back_label, f"返回按钮指向上一级：{back_label}")
        page.screenshot(path=str(OUT / "series-3-expanded.png"), full_page=True)

        page.locator("#btnLibBack").click()
        page.wait_for_selector("#kbGrid .series-card", timeout=15000)
        check(page.locator("#kbGrid .series-card").count() >= 1, "返回后仍是折叠视图")
        check(not errors, f"无浏览器报错：{errors[:3]}")
        browser.close()

    print("\n截图：" + ", ".join(p.name for p in sorted(OUT.glob("series-*.png"))))
    if FAILS:
        print(f"失败 {len(FAILS)} 项：{FAILS}")
        return 1
    print("三级浏览界面验证通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
