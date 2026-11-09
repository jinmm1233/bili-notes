"""前端静态一致性检查：JS 引用的 DOM id、视图名、接口路径必须真实存在。

这类问题在浏览器里表现为"点了没反应"，很难靠读代码发现，静态比对最省事。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"

FAILS: list[str] = []


def check(cond: bool, label: str) -> None:
    print(("  ok  " if cond else "  FAIL") + f"  {label}")
    if not cond:
        FAILS.append(label)


def main() -> int:
    html = (WEB / "index.html").read_text(encoding="utf-8")
    css = (WEB / "app.css").read_text(encoding="utf-8")
    js = (WEB / "app.js").read_text(encoding="utf-8")

    html_ids = set(re.findall(r'id="([^"]+)"', html))
    # JS 里用 innerHTML 现造的节点也算存在：只比 index.html 会误报一大片
    js_built_ids = set(re.findall(r'id="([^"]+)"', js))
    js_ids = set(re.findall(r"\$\('([^']+)'\)", js))

    print("[1] DOM id 引用")
    # selCount 会在导入后被重新渲染，属于动态节点，单独放行
    dynamic = {"selCount"}
    missing = sorted(js_ids - html_ids - js_built_ids - dynamic)
    check(not missing, f"JS 引用的 id 都存在{'（缺失：' + str(missing) + '）' if missing else ''}")

    print("\n[2] 视图名称")
    views_html = set(re.findall(r'id="view-([a-z]+)"', html))
    views_nav = set(re.findall(r'data-view="([a-z]+)"', html))
    loader_block = re.search(r"const VIEW_LOADERS = \{(.*?)\n\};", js, re.S)
    views_js = set(re.findall(r"^\s{2}([a-z]+):", loader_block.group(1) if loader_block else "", re.M))
    check(views_html == views_nav, f"导航与视图一一对应 {sorted(views_html)}")
    check(views_js == views_html, f"JS 加载器覆盖全部视图 {sorted(views_js)}")

    print("\n[3] 样式类")
    key_classes = [
        "app", "sidebar", "nav-item", "view", "panel", "stat", "note-grid",
        "note-card", "video-card", "task-row", "reader", "markdown", "toast",
    ]
    missing_css = [c for c in key_classes if f".{c}" not in css]
    check(not missing_css, f"关键类都有样式{'（缺：' + str(missing_css) + '）' if missing_css else ''}")

    print("\n[4] 接口调用与后端路由")
    called = set(re.findall(r"api\(\s*[`'\"](/api/[^`'\"?]*)", js))
    called |= {m for m in re.findall(r"`/api/[^`]*`", js)}
    backend = ""
    for f in (ROOT / "app" / "api").glob("routes*.py"):
        backend += f.read_text(encoding="utf-8")

    unresolved = []
    for path in sorted(called):
        p = path.strip("`").split("?")[0]
        # 模板字符串里的 ${...} 可能被上面的正则截断（在 ? 处断掉），
        # 统一从 ${ 起砍掉，只留能对上的路径前缀
        p = p.split("${")[0]
        p = re.sub(r"\$\{[^}]+\}", "{x}", p)
        token = p.split("/")[2] if len(p.split("/")) > 2 else ""
        if token and token not in backend:
            unresolved.append(p)
    check(not unresolved, f"前端调用的接口在路由中可查{'（未匹配：' + str(unresolved) + '）' if unresolved else ''}")

    print("\n[5] 静态资源引用")
    check("/static/app.css" in html, "样式表引用")
    check("/static/app.js" in html, "脚本引用")
    check("cdn." not in html and "cdn." not in js, "无外链 CDN（离线可用）")

    print("\n[6] 危险写法")
    check("innerHTML = content" not in js, "未直接注入未转义内容")
    check("eval(" not in js, "无 eval")
    check("document.write" not in js, "无 document.write")

    print("\n" + "=" * 40)
    if FAILS:
        print(f"失败 {len(FAILS)} 项：{FAILS}")
        return 1
    print("前端一致性检查通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
