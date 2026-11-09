"""行程单（A 档）：从旅游类视频里抽出「去了哪 → 怎么去 → 花了多少」。

刻意不接任何地图服务：地名转坐标是最脏的一步（同名、口语、
海外 POI 覆盖差），而地图合规还卡着可用服务商。所以这一档只
产出**可验证的行程事实**——地点顺序、交通方式、明确提到的花费，
一个坐标都不给，也就不会骗人。

设计红线：
1. 视频没说的，一律留空字符串，绝不补全。
2. 费用只搬运原文，不做汇率换算（换算等于编数字）。
3. 每个地点都挂视频时间戳，"这条信息出自第几分钟"可回查。
"""
from __future__ import annotations

import logging
from typing import Any

from ..core.models import Chunk, VideoMeta
from ..core.paths import fmt_ts, ts_link

log = logging.getLogger(__name__)

# 旅行类判定用的信号词。宁可漏判（用户手动开）也不要误判：
# 给技术视频硬塞一张"行程表"比没有更糟。
_TRAVEL_HINTS = (
    "旅游", "旅行", "游玩", "攻略", "行程", "自驾", "环岛", "徒步",
    "景点", "打卡", "门票", "景区", "古镇", "老街", "夜市", "民宿",
    "酒店", "机场", "高铁", "火车", "打车", "地铁", "公交", "包车",
    "导游", "当地", "美食", "小吃", "住哪", "怎么去", "玩什么",
)

_SYSTEM = (
    "你是把旅行视频整理成行程单的编辑。只做搬运和归纳，不做任何补充或推测。"
    "输出必须是 JSON。"
)

_TMPL = """把下面这条旅行视频的讲解，抽成一份行程单。

【一、地点】
只列视频里**真的去了/真的讲了**的地点，按出场顺序编号。
"我们下次可以去""听说那边不错"这种没去的不算。
name 用视频里的叫法（专有名词保留原文，可括号附当地语言）；
city 填所在城市，视频没说就从上下文判断，判断不出写空字符串。
kind 从 景点 / 餐饮 / 住宿 / 交通枢纽 / 街区 / 购物 里选一个。

【二、时间戳】
ts 是这个地点**第一次被提到**的秒数，从下面的材料里找，必须落在材料给的时间范围内。

【三、交通（legs）】
from_order / to_order 指向地点的编号。
transport 只填视频里明说的方式（打车 / 步行 / 包车 / 火车 / 船 …），没说就留空。
cost 只填视频里明说的金额，**照抄原话里的数字和币种**（如"30 元人民币""50 万越南盾"），
没说就留空。duration 同理，没说留空。

【四、花费小结】
budget 用一两句话说明：视频里明确提到了哪些花费、哪些完全没提（住宿/餐饮/门票等）。
没提的不要估算。

【五、绝不编造】
材料里没有的信息一律空字符串。宁可少写一条，也不要写一条查不到的。

材料（每行前面是时间码）：
{material}

输出 JSON（不要代码块围栏）：
{{
  "is_travel": true,
  "summary": "一句话概括这趟行程（不超过 40 字）",
  "stops": [
    {{"order": 1, "name": "", "city": "", "kind": "", "ts": 0, "what": "这一站干了什么（不超过 25 字）", "cost": ""}}
  ],
  "legs": [
    {{"from_order": 1, "to_order": 2, "transport": "", "cost": "", "duration": "", "note": ""}}
  ],
  "budget": ""
}}"""


def travel_score(meta: VideoMeta, chunks: list[Chunk]) -> int:
    """粗判是不是旅行内容：标题权重 ×3，正文按命中词数累加。

    标题是最强信号（"XX 旅游攻略""跟我玩 XX"），正文里
    "打车/门票/民宿"这类词旅行视频密集出现，技术视频几乎没有。
    """
    text = " ".join(c.text for c in chunks)
    body = sum(1 for w in _TRAVEL_HINTS if w in text)
    title = sum(1 for w in _TRAVEL_HINTS if w in meta.title)
    return body + title * 3


def looks_like_travel(meta: VideoMeta, chunks: list[Chunk], threshold: int = 6) -> bool:
    return travel_score(meta, chunks) >= threshold


def _material(chunks: list[Chunk], budget_chars: int = 7000) -> str:
    """把知识块按时间码拼成材料，控制长度。"""
    rows: list[str] = []
    used = 0
    for c in chunks:
        text = " ".join(c.text.split())
        if not text:
            continue
        line = f"[{fmt_ts(c.start)}-{fmt_ts(c.end)}] {text}"
        if used + len(line) > budget_chars:
            break
        rows.append(line)
        used += len(line)
    return "\n".join(rows)


def build_itinerary(
    client,
    meta: VideoMeta,
    chunks: list[Chunk],
    max_stops: int = 20,
    segments=None,
) -> dict[str, Any] | None:
    """抽取行程单。没有模型或材料为空时返回 None（调用方决定是否降级）。"""
    if client is None or not chunks:
        return None
    material = _material(chunks)
    if not material:
        return None

    try:
        data = client.complete_json(
            _SYSTEM,
            _TMPL.format(material=material),
            max_tokens=2048,
        )
    except Exception as exc:  # 抽取失败不该毁掉整条笔记
        log.warning("行程单抽取失败：%s", exc)
        return None

    if not isinstance(data, dict):
        return None

    stops: list[dict] = []
    seen: set[str] = set()
    for i, raw in enumerate(data.get("stops") or []):
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        stops.append({
            "order": len(stops) + 1,
            "name": name,
            "city": str(raw.get("city") or "").strip(),
            "kind": str(raw.get("kind") or "").strip(),
            "ts": _safe_ts(raw.get("ts"), meta.duration),
            "what": str(raw.get("what") or "").strip(),
            "cost": str(raw.get("cost") or "").strip(),
        })
        if len(stops) >= max_stops:
            break

    if len(stops) < 2:
        return None        # 一个点串不成路线，不如不出

    order_set = {s["order"] for s in stops}
    legs: list[dict] = []
    for raw in data.get("legs") or []:
        if not isinstance(raw, dict):
            continue
        try:
            a = int(raw.get("from_order"))
            b = int(raw.get("to_order"))
        except (TypeError, ValueError):
            continue
        if a not in order_set or b not in order_set:
            continue
        legs.append({
            "from_order": a,
            "to_order": b,
            "transport": str(raw.get("transport") or "").strip(),
            "cost": str(raw.get("cost") or "").strip(),
            "duration": str(raw.get("duration") or "").strip(),
            "note": str(raw.get("note") or "").strip(),
        })

    # 没有路段信息时，按顺序补"相邻两站"的空壳，让路线连起来
    if not legs:
        legs = [
            {"from_order": stops[i]["order"], "to_order": stops[i + 1]["order"],
             "transport": "", "cost": "", "duration": "", "note": ""}
            for i in range(len(stops) - 1)
        ]

    # 模型给的时间戳往往是"块开头"，两站挤到同一秒。用原文把每一站
    # 钉到"名字第一次被念出来的那一句"——这是点回视频唯一可靠的锚点。
    if segments:
        _snap_ts(stops, segments)

    return {
        "is_travel": True,
        "summary": str(data.get("summary") or "").strip(),
        "stops": stops,
        "legs": legs,
        "budget": str(data.get("budget") or "").strip(),
        "cities": _city_chain(stops),
    }


def _safe_ts(value: Any, duration: float) -> float | None:
    """时间戳只有在"像个秒数且落在视频内"时才收；否则当未知。

    0 是合法值（第一站可能就在开头），所以未知必须用 None 表示，
    不能用 0 兜底——否则第一站会被误显示成"没有定位"。
    """
    try:
        ts = float(value)
    except (TypeError, ValueError):
        return None
    if ts < 0:
        return None
    if duration and ts > duration:
        return None
    return ts


def _name_keys(name: str) -> list[str]:
    """一个地名可能的"被念出来"的形式。

    ASR 常把地名念歪（"巴拿山"→"芭芽来"），所以除了全名，
    还退一步用前 3/2 个字当模糊键——短到 2 字还能命中，
    再短就容易误配到别的句子。
    """
    keys = [name]
    for n in (4, 3, 2):
        if len(name) > n:
            keys.append(name[:n])
    return [k for k in dict.fromkeys(keys) if len(k) >= 2]


def _snap_ts(stops: list[dict], segments) -> None:
    """把每站时间戳钉到"名字第一次出现的那一句"。

    模型给的时间戳通常是块的起点，两三站会挤在同一秒，
    点回视频就全指到同一处。用原文句子做锚点才对得上。
    找不到就保留原值——宁可粗，不要错。
    """
    texts = [(float(s.start), s.text or "") for s in segments]
    cursor = 0
    for stop in stops:
        keys = _name_keys(stop["name"])
        hit = None
        for i in range(cursor, len(texts)):
            if any(k in texts[i][1] for k in keys):
                hit = i
                break
        if hit is not None:
            stop["ts"] = texts[hit][0]
            cursor = hit + 1


def _city_chain(stops: list[dict]) -> list[str]:
    chain: list[str] = []
    for s in stops:
        c = s.get("city") or ""
        if c and (not chain or chain[-1] != c):
            chain.append(c)
    return chain


def render_section(it: dict[str, Any], url: str = "") -> list[str]:
    """渲染成 Markdown，插在笔记的「行程路线」一节。"""
    stops = it.get("stops") or []
    legs = it.get("legs") or []
    by_order = {s["order"]: s for s in stops}
    lines: list[str] = ["## 行程路线", ""]

    cities = it.get("cities") or []
    if it.get("summary"):
        lines.append(it["summary"])
        lines.append("")
    if len(cities) >= 2:
        lines.append(f"**城市串联**：{' → '.join(cities)}　（{len(stops)} 个地点）")
        lines.append("")

    lines.append("| # | 地点 | 城市 | 类型 | 这一站 | 花费 | 视频位置 |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- |")
    for s in stops:
        if s["ts"] is None:
            pos = "—"
        elif url:
            pos = f"[{fmt_ts(s['ts'])}]({ts_link(url, s['ts'])})"
        else:
            pos = fmt_ts(s["ts"])
        lines.append(
            f"| {s['order']} | {s['name']} | {s['city'] or '—'} | {s['kind'] or '—'} "
            f"| {s['what'] or '—'} | {s['cost'] or '—'} | {pos} |"
        )
    lines.append("")

    if legs:
        lines.append("### 怎么去")
        lines.append("")
        lines.append("| 路段 | 交通 | 耗时 | 花费 |")
        lines.append("| --- | --- | --- | --- |")
        for leg in legs:
            a = by_order.get(leg["from_order"], {})
            b = by_order.get(leg["to_order"], {})
            seg = f"{a.get('name', '?')} → {b.get('name', '?')}"
            lines.append(
                f"| {seg} | {leg['transport'] or '未提及'} "
                f"| {leg['duration'] or '—'} | {leg['cost'] or '—'} |"
            )
        lines.append("")
        for leg in legs:
            if leg.get("note"):
                a = by_order.get(leg["from_order"], {})
                b = by_order.get(leg["to_order"], {})
                lines.append(f"- {a.get('name', '?')} → {b.get('name', '?')}：{leg['note']}")
        lines.append("")

    if it.get("budget"):
        lines.append(f"> **花费说明**：{it['budget']}")
        lines.append("")

    lines.append("> 以上仅包含视频明确提到的内容；未提到的费用、交通方式留空，不做推测。")
    lines.append("")
    return lines
