"""知识地图：同一份树输出三种形态。

- mermaid flowchart TD：直接写进 md，Obsidian/Typora/GitHub 都能渲染。
  用 flowchart 而不是 mindmap：mindmap 是放射布局，一条递进的步骤会被
  甩成"八爪鱼"，顺序也看不出来；flowchart TD 从上往下排，递进步骤就是
  一列，带箭头，顺序一眼可见。
- 静态 SVG HTML：可缩放、可导出 PNG，且不依赖网络
- 大纲文本：给想要纯文本层级的人
"""
from __future__ import annotations

import html
import json
import re
from typing import Any

from ..core.models import Chapter, VideoMeta
from . import mathfmt

_MAX_LABEL = 34          # SVG 会折行，不必像以前那样截到 22 字
_MERMAID_ESCAPE = re.compile(r"[()\[\]{}<>\"`|]")


def build_tree(meta: VideoMeta, chapters: list[Chapter], with_points: bool = False) -> dict[str, Any]:
    """章节树。默认**只留章节名**。

    以前把每章的要点也挂成节点，结果一张图横着铺十几个 30 字长句——
    在手机上根本看不全，图也就失去了"一眼看结构"的意义。
    要点属于正文，交给「内容结构」列表去承载。
    """
    children = []
    for chapter in chapters:
        node: dict[str, Any] = {"name": _clip(chapter.title)}
        if with_points:
            points = [_clip(b) for b in chapter.bullets[:5] if b.strip()]
            if points:
                node["children"] = [{"name": p} for p in points]
        children.append(node)
    return {"name": _clip(meta.title, 40), "children": children}


def _clip(text: str, limit: int = _MAX_LABEL) -> str:
    # 导图的节点只有一行宽，LaTeX 矩阵塞进去只会糊掉：块级公式换成一句提示
    clean = re.sub(r"\s+", " ", mathfmt.for_mindmap(text)).strip().strip("-·•")
    return clean[:limit] + "…" if len(clean) > limit else clean


# ---- 步骤识别：判断一组要点是"按顺序做的步骤"还是"并列的几个点" ----
_STEP_START = re.compile(r"^\s*(?:首先|先(?!进)|第一步|第1步|第[一二三四五六七八九十]+步|步骤\s*[1一]|Step\s*1\b)")
_STEP_NEXT = re.compile(r"^\s*(?:再(?!次)|然后|接着|随后|其次|之后|进而|并(?:将|把))")
_STEP_END = re.compile(r"^\s*(?:最后|最终|末尾)")
_STEP_AFTER = re.compile(
    r"(?:变换|完成|处理|结束|操作)?后[^，。；、]{0,8}?(?:即为|即得|便是|就是|得到|可得|便得)"
    r"|即为所求|至此"
)
_CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩"


def _is_steps(names: list[str]) -> bool:
    """含两个以上顺序词（先/再/然后/最后…）才当成递进流程。

    只认顺序词、不认"第一章/首先介绍"这类泛用开头，避免把并列要点误排成链。
    """
    hits = sum(
        1 for n in names
        if _STEP_START.match(n) or _STEP_NEXT.match(n) or _STEP_END.match(n)
    )
    return len(names) >= 2 and hits >= 2


def _step_rank(name: str) -> int:
    """排序权重：前置动作(无标记) → 起始 → 接续 → 收尾动作 → 最终结论。"""
    if _STEP_END.match(name):
        return 50
    if _STEP_AFTER.search(name):
        return 40
    if _STEP_NEXT.match(name):
        return 30
    if _STEP_START.match(name):
        return 20
    return 10


def _order_steps(names: list[str]) -> list[str]:
    """稳定排序：没标记的（如"组成增广矩阵"）排在最前，其次起、接续、收尾。"""
    return [n for _, n in sorted(enumerate(names), key=lambda p: (_step_rank(p[1]), p[0]))]


def _mm_label(text: str, limit: int = 30) -> str:
    """mermaid 节点标签：去掉会破坏语法的符号，超长截断。"""
    clean = _MERMAID_ESCAPE.sub("", text).replace(":", "：").strip()
    return clean[:limit] + "…" if len(clean) > limit else clean


def _chainify(tree: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """把"递进步骤"的兄弟节点改写成链（①→②→③），并加序号。

    返回 (新树, 是否有步骤链)。只在渲染时用，不改动落盘的 mindmap.json。
    改写后单链在竖向布局里天然排成一列，在 mermaid TD 里也是竖着一根。
    """
    had = False

    def conv(node: dict[str, Any]) -> dict[str, Any]:
        nonlocal had
        kids = node.get("children") or []
        names = [k.get("name") or "" for k in kids]
        is_chain = (
            len(kids) >= 2
            and not any(k.get("children") for k in kids)
            and _is_steps(names)
        )
        if not is_chain:
            new_kids = [conv(k) for k in kids]
            out: dict[str, Any] = {"name": node.get("name") or ""}
            if new_kids:
                out["children"] = new_kids
            return out

        had = True
        ordered = _order_steps(names)
        total = len(ordered)
        tail: dict[str, Any] | None = None
        for i in range(total - 1, -1, -1):
            mark = f"{_CIRCLED[i]} " if total <= len(_CIRCLED) else f"{i + 1}. "
            item: dict[str, Any] = {"name": f"{mark}{ordered[i]}"}
            if tail:
                item["children"] = [tail]
            tail = item
        out = {"name": node.get("name") or ""}
        if tail:
            out["children"] = [tail]
        return out

    return conv(tree), had


def to_mermaid(tree: dict[str, Any]) -> str:
    """输出 mermaid flowchart：步骤链竖排一列（TD），并列主题横向铺开（LR）。

    节点统一写成 `nX["文字"]`，阅读器与 mermaid 原生渲染都能解析。
    """
    chained, has_chain = _chainify(tree)
    lines = ["flowchart TD" if has_chain else "flowchart LR"]
    seq = [0]

    def nid() -> str:
        seq[0] += 1
        return f"n{seq[0]}"

    root_id = nid()
    lines.append(f'  {root_id}(["{_mm_label(chained["name"], 34)}"])')

    def emit(parent_id: str, node: dict[str, Any]) -> None:
        for kid in node.get("children") or []:
            cid = nid()
            lines.append(f'  {cid}["{_mm_label(kid.get("name") or "")}"]')
            lines.append(f"  {parent_id} --> {cid}")
            emit(cid, kid)

    emit(root_id, chained)
    return "\n".join(lines)


def _escape(text: str) -> str:
    return _MERMAID_ESCAPE.sub("", text).replace(":", "：")


def to_outline_md(tree: dict[str, Any]) -> str:
    lines = [f"# {tree['name']}"]

    def walk(node: dict[str, Any], depth: int) -> None:
        for child in node.get("children") or []:
            lines.append(f"{'  ' * (depth - 1)}- {child['name']}")
            walk(child, depth + 1)

    walk(tree, 1)
    return "\n".join(lines)


# ---- SVG 布局参数（单位 px，与字号绑定）----
_FONT = 13.0
_LINE_H = 18.0
_PAD_X = 12.0
_PAD_Y = 8.0
_ROW_GAP = 34.0        # 上下布局时层级之间的纵向间距
_COL_GAP = 46.0        # 左右布局时层级之间的横向间距
_SIB_GAP = 14.0        # 同层相邻节点之间的空隙
_MAX_TEXT_W = 186.0    # 单个节点文本最大宽度，超出换行
_MAX_LINES = 3
_MARGIN = 26.0

_MAX_LABEL_CHILD = 30  # 换行后能容纳更多字，不必像以前那样狠截
_MAX_LABEL_ROOT = 40


def _char_w(ch: str) -> float:
    """粗略字符宽度：中日韩全角按 1 个字号，西文按 0.56。"""
    return _FONT if ord(ch) > 0x2E80 else _FONT * 0.56


def _wrap_name(name: str, max_w: float = _MAX_TEXT_W) -> list[str]:
    """按估算宽度折行，最多 3 行，溢出补省略号。"""
    lines: list[str] = []
    cur, width = "", 0.0
    for ch in name:
        cw = _char_w(ch)
        if width + cw > max_w and cur:
            lines.append(cur)
            cur, width = ch, cw
        else:
            cur += ch
            width += cw
    if cur:
        lines.append(cur)
    if len(lines) > _MAX_LINES:
        lines = lines[:_MAX_LINES]
        lines[-1] = lines[-1][:-1] + "…"
    return lines or [""]


def _layout(
    tree: dict[str, Any], vertical: bool = True
) -> tuple[list[dict[str, Any]], float, float]:
    """把树排成层级图，返回 (节点列表, 画布宽, 画布高)。

    vertical=True：父子上下（步骤链一列）；False：父子左右（并列主题横向铺开）。
    布局是纯计算：生成的 SVG 是"死"的——没有 JS、没有网络也能看全。
    节点坐标 (x, y) 存的是盒子中心。
    """
    nodes: list[dict[str, Any]] = []

    def collect(node: dict[str, Any], depth: int, parent: int | None) -> int:
        idx = len(nodes)
        limit = _MAX_LABEL_ROOT if depth == 0 else _MAX_LABEL_CHILD
        name = node.get("name") or ""
        lines = _wrap_name(name[:limit] + ("…" if len(name) > limit else ""))
        width = max((sum(_char_w(c) for c in ln) for ln in lines), default=0) + _PAD_X * 2
        nodes.append({
            "idx": idx, "depth": depth, "parent": parent, "lines": lines,
            "w": round(width, 1),
            "h": round(len(lines) * _LINE_H + _PAD_Y * 2, 1),
            "x": 0.0, "y": 0.0, "children": [],
        })
        for child in node.get("children") or []:
            cidx = collect(child, depth + 1, idx)
            nodes[idx]["children"].append(cidx)
        return idx

    collect(tree, 0, None)

    # 主轴（层级方向）：同一层取最大尺寸，依次累加
    layer_key = "h" if vertical else "w"
    layer_gap = _ROW_GAP if vertical else _COL_GAP
    layer_size: dict[int, float] = {}
    for n in nodes:
        layer_size[n["depth"]] = max(layer_size.get(n["depth"], 0.0), n[layer_key])
    layer_pos: dict[int, float] = {}
    cursor = 0.0
    for depth in sorted(layer_size):
        layer_pos[depth] = cursor + layer_size[depth] / 2
        cursor += layer_size[depth] + layer_gap
    for n in nodes:
        along = layer_pos[n["depth"]]
        if vertical:
            n["y"] = round(along, 1)
        else:
            n["x"] = round(along, 1)

    # 次轴（兄弟方向）：先自底向上算子树的横向跨度，再自顶向下分配。
    # 不能按固定宽度排——折行后节点宽度不同，固定宽度会让宽节点压到邻居。
    def cross_size(node: dict[str, Any]) -> float:
        return node["w"] if vertical else node["h"]

    def span(idx: int) -> float:
        node = nodes[idx]
        kids = node["children"]
        if not kids:
            node["span"] = cross_size(node)
            return node["span"]
        total = sum(span(c) for c in kids) + _SIB_GAP * (len(kids) - 1)
        node["span"] = max(cross_size(node), total)
        return node["span"]

    def place(idx: int, center: float) -> None:
        """以中线定位：单链的父子天然对齐成一列，多个子节点则在父节点下居中铺开。"""
        node = nodes[idx]
        if vertical:
            node["x"] = round(center, 1)
        else:
            node["y"] = round(center, 1)
        kids = node["children"]
        if not kids:
            return
        total = sum(nodes[c]["span"] for c in kids) + _SIB_GAP * (len(kids) - 1)
        cursor = center - total / 2
        for c in kids:
            place(c, cursor + nodes[c]["span"] / 2)
            cursor += nodes[c]["span"] + _SIB_GAP

    root_span = span(0)
    place(0, root_span / 2)
    width = max(n["x"] + n["w"] / 2 for n in nodes)
    height = max(n["y"] + n["h"] / 2 for n in nodes)
    return nodes, width, height


def _svg_body(
    nodes: list[dict[str, Any]], width: float, height: float, vertical: bool = True
) -> str:
    parts: list[str] = []
    # 连线：父节点边缘 → 子节点边缘的贝塞尔曲线，从上行往下画
    for n in nodes:
        parent = n["parent"]
        if parent is None:
            continue
        p = nodes[parent]
        if vertical:
            x1, y1 = p["x"], p["y"] + p["h"] / 2
            x2, y2 = n["x"], n["y"] - n["h"] / 2 - 1
            mid = (y1 + y2) / 2
            path = (f"M{x1:.1f},{y1:.1f} C{x1:.1f},{mid:.1f} {x2:.1f},{mid:.1f} {x2:.1f},{y2:.1f}")
        else:
            x1, y1 = p["x"] + p["w"] / 2, p["y"]
            x2, y2 = n["x"] - n["w"] / 2 - 1, n["y"]
            mid = (x1 + x2) / 2
            path = (f"M{x1:.1f},{y1:.1f} C{mid:.1f},{y1:.1f} {mid:.1f},{y2:.1f} {x2:.1f},{y2:.1f}")
        parts.append(f'<path class="lnk d{min(n["depth"], 3)}" d="{path}"/>')
    # 节点
    for n in nodes:
        d = min(n["depth"], 3)
        x, y = n["x"] - n["w"] / 2, n["y"] - n["h"] / 2
        parts.append(f'<g class="nd d{d}"><title>{html.escape("".join(n["lines"]))}</title>')
        parts.append(
            f'<rect class="box d{d}" x="{x:.1f}" y="{y:.1f}" '
            f'width="{n["w"]:.1f}" height="{n["h"]:.1f}" rx="7" ry="7"/>'
        )
        first = n["y"] - (len(n["lines"]) - 1) * _LINE_H / 2 + _FONT * 0.36
        for i, line in enumerate(n["lines"]):
            parts.append(
                f'<text class="tx d{d}" x="{n["x"]:.1f}" '
                f'text-anchor="middle" y="{first + i * _LINE_H:.1f}">{html.escape(line)}</text>'
            )
        parts.append("</g>")
    return "\n".join(parts)


def to_markmap_html(tree: dict[str, Any], title: str) -> str:
    """自包含单文件知识地图。

    以前这里用 markmap 的 CDN 脚本渲染，一旦断网 / CDN 被拦，
    整页就只剩内嵌的 Markdown 原文（"一堆字，什么图都没有"）。
    现在改成在 Python 里算好布局、直接输出 SVG：不联网、不跑 JS
    也能看到完整图；JS 只负责缩放/平移这类锦上添花。
    """
    safe_title = html.escape(title or "思维导图")
    chained, has_chain = _chainify(tree)
    vertical = has_chain          # 步骤链竖排一列；并列主题横向铺开
    nodes, width, height = _layout(chained, vertical=vertical)
    body = _svg_body(nodes, width, height, vertical=vertical)
    vw = width + _MARGIN * 2
    vh = height + _MARGIN * 2
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{safe_title} · 知识地图</title>
<style>
  :root{{--bg:#fbfbfd;--bar:#fff;--line:#e6e8ee;--ink:#111827;--muted:#8a8f9c}}
  @media (prefers-color-scheme: dark){{
    :root{{--bg:#15171c;--bar:#1b1e25;--line:#2b3038;--ink:#e8eaf0;--muted:#8f95a3}}
  }}
  html,body{{margin:0;height:100%;background:var(--bg);color:var(--ink);
    font-family:-apple-system,"Segoe UI","Microsoft YaHei",sans-serif}}
  #bar{{position:fixed;top:0;left:0;right:0;height:46px;display:flex;align-items:center;
       gap:14px;padding:0 16px;background:var(--bar);border-bottom:1px solid var(--line);
       font-size:13px;color:var(--muted);z-index:9}}
  #bar b{{font-weight:600;color:var(--ink);max-width:46vw;overflow:hidden;
         text-overflow:ellipsis;white-space:nowrap}}
  #bar .sp{{flex:1}}
  #bar button{{border:1px solid var(--line);background:transparent;color:var(--ink);
    border-radius:6px;padding:3px 8px;font-size:12px;cursor:pointer;line-height:1.4}}
  #bar button:hover{{background:rgba(125,125,125,.12)}}
  /* 图按自然尺寸铺开、纵向可滚：缩到一屏会把字缩得看不清 */
  #stage{{position:absolute;top:46px;left:0;right:0;bottom:0;overflow:auto;padding:20px 24px}}
  svg#map{{display:block;height:auto}}
  /* 节点配色：亮/暗两套，SVG 里只写 class，不写死颜色 */
  .box.d0{{fill:#1f2937;stroke:#1f2937}}
  .tx.d0{{fill:#ffffff;font-weight:600}}
  .box.d1{{fill:#e8f0ff;stroke:#c3d7ff}}
  .tx.d1{{fill:#12365e;font-weight:600}}
  .box.d2{{fill:#ffffff;stroke:#e3e6ee}}
  .tx.d2{{fill:#2f3542}}
  .box.d3{{fill:#ffffff;stroke:#eceef4}}
  .tx.d3{{fill:#4b5563}}
  .lnk{{fill:none;stroke-linecap:round}}
  .lnk.d1{{stroke:#c3d7ff;stroke-width:1.6}}
  .lnk.d2{{stroke:#dfe3ec;stroke-width:1.4}}
  .lnk.d3{{stroke:#e8ebf2;stroke-width:1.2}}
  @media (prefers-color-scheme: dark){{
    .box.d0{{fill:#e8eaf0;stroke:#e8eaf0}}
    .tx.d0{{fill:#15171c}}
    .box.d1{{fill:#1e2b3f;stroke:#2f4463}}
    .tx.d1{{fill:#bcd5ff}}
    .box.d2{{fill:#1b1e25;stroke:#2b3038}}
    .tx.d2{{fill:#dfe3ea}}
    .box.d3{{fill:#1b1e25;stroke:#242832}}
    .tx.d3{{fill:#b7bdc9}}
    .lnk.d1{{stroke:#2f4463}}
    .lnk.d2{{stroke:#2b3038}}
    .lnk.d3{{stroke:#242832}}
  }}
  text{{font-size:{_FONT}px;font-family:inherit}}
</style>
</head>
<body>
<div id="bar">
  <b>{safe_title}</b>
  <span>{len(nodes)} 个节点</span>
  <span class="sp"></span>
  <button onclick="mmZoom(1.2)">放大</button>
  <button onclick="mmZoom(0.83)">缩小</button>
  <button onclick="mmZoom(0)">适应宽度</button>
</div>
<div id="stage">
<svg id="map" viewBox="0 0 {vw:.0f} {vh:.0f}" preserveAspectRatio="xMinYMin meet"
     xmlns="http://www.w3.org/2000/svg">
  <g transform="translate({_MARGIN},{_MARGIN})">
{body}
  </g>
</svg>
</div>
<script>
(function () {{
  var svg = document.getElementById('map');
  var stage = document.getElementById('stage');
  if (!svg || !stage) return;
  var BASE = {vw:.0f}, k = 1;

  function fit() {{
    var avail = stage.clientWidth - 48;
    return Math.min(1.4, Math.max(0.35, avail / BASE));
  }}
  function apply() {{
    svg.style.width = Math.round(BASE * k) + 'px';
  }}
  window.mmZoom = function (factor) {{
    k = (factor === 0) ? fit() : Math.min(2.5, Math.max(0.3, k * factor));
    apply();
  }};
  k = fit();
  apply();
  window.addEventListener('resize', function () {{ k = fit(); apply(); }});
}})();
</script>
</body>
</html>
"""


def to_json(tree: dict[str, Any]) -> str:
    return json.dumps(tree, ensure_ascii=False, indent=2)
