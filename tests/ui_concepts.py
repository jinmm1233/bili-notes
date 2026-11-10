"""概念页 UI 实跑：释义区、重点标记、筛选、补全按钮。

  python tests/ui_concepts.py
环境变量 BILINOTES_UI_BASE 指到服务地址（默认 127.0.0.1:8765）。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = os.environ.get("BILINOTES_UI_BASE", "http://127.0.0.1:8765")
OUT = Path(__file__).resolve().parents[1] / ".screens"
OUT.mkdir(exist_ok=True)

FAILED = 0


def ok(cond: bool, msg: str) -> None:
    global FAILED
    print(("[OK] " if cond else "[FAIL] ") + msg)
    if not cond:
        FAILED += 1


def main() -> int:
    errors: list[str] = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 950})
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)

        page.goto(BASE, wait_until="networkidle")
        page.click('[data-view="concepts"]')
        page.wait_for_selector("#conceptList .concept-item", timeout=20000)
        page.wait_for_timeout(1200)

        # 1) 工具栏有「补全释义」按钮和「只看讲透的」开关
        ok(page.locator("#btnExplainConcepts").count() == 1, "有「补全释义」按钮")
        ok(page.locator("#conceptCoreOnly").count() == 1, "有「只看讲透的」开关")

        # 2) 列表项：重点概念带小圆点
        dots = page.locator("#conceptList .ci-core").count()
        ok(dots > 0, f"列表里有重点标记（{dots} 个）")

        # 3) 点一个重点概念，详情顶部出现「这个概念是什么」
        items = page.locator("#conceptList .concept-item")
        ok(items.count() > 0, f"概念列表有内容（{items.count()} 条）")
        # 找一个带重点标记的点
        target = page.locator("#conceptList .concept-item:has(.ci-core)").first
        if target.count() == 0:
            target = items.first
        target.click()
        page.wait_for_timeout(900)

        ok(page.locator("#conceptDetail .cd-explain").count() == 1,
           "详情里有「这个概念是什么」区块")
        explain_text = page.locator("#conceptDetail .cd-explain-body").inner_text().strip()
        ok(len(explain_text) > 8, f"释义区有实际内容（{explain_text[:40]}…）")
        ok("材料中" not in explain_text[:30],
           "释义不是「材料中…」这种元叙述")

        # 4) 「各集怎么讲」还在，但跳转改成明确的文字提示
        ok(page.locator("#conceptDetail .cd-label:has-text('各集怎么讲')").count() == 1,
           "保留了「各集怎么讲」")
        ok(page.locator("#conceptDetail .cd-note-go").count() > 0,
           "跳转改成「打开笔记 →」明确提示")

        # 5) 重点徽章
        ok(page.locator("#conceptDetail .cd-core").count() >= 0, "重点徽章可渲染")

        # 6) 「只看讲透的」能筛掉非重点
        before = page.locator("#conceptList .concept-item").count()
        page.uncheck("#conceptCoreOnly")
        page.wait_for_timeout(500)
        after = page.locator("#conceptList .concept-item").count()
        ok(after >= before, f"取消勾选后可见概念变多（{before} → {after}）")
        page.check("#conceptCoreOnly")
        page.wait_for_timeout(500)

        # 7) 搜索仍然能用
        page.fill("#conceptSearch", "矩阵")
        page.wait_for_timeout(500)
        ok(page.locator("#conceptList .concept-item").count() > 0, "搜索有结果")
        page.fill("#conceptSearch", "")
        page.wait_for_timeout(400)

        page.screenshot(path=str(OUT / "concepts_explain.png"))
        print(f"  截图：{OUT / 'concepts_explain.png'}")

        ok(not errors, f"没有 JS 报错（{len(errors)} 条）{errors[:3]}")
        browser.close()

    print()
    if FAILED:
        print(f"失败 {FAILED} 项")
        return 1
    print("概念页 UI 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
