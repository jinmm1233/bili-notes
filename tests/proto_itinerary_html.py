"""把 A 档抽取结果渲染成可视预览（只用真实抽取数据，不造样例）。"""
from __future__ import annotations

import html
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE / "_proto_itinerary" / "itinerary.json"
CACHE = Path("C:/Users/86159/AppData/Roaming/BiliNotes/cache/26f0f33a2f90")


def fmt(sec: float) -> str:
    s = int(sec)
    return f"{s // 60:02d}:{s % 60:02d}"


def main() -> None:
    it = json.loads(DATA.read_text(encoding="utf-8"))
    segs = json.loads((CACHE / "transcript.json").read_text(encoding="utf-8"))
    meta = json.loads((CACHE / "meta.json").read_text(encoding="utf-8"))
    url = meta["webpage_url"]

    def quote(ts):
        if ts is None:
            return ""
        seg = min(segs, key=lambda s: abs(s["start"] - ts))
        return html.escape(seg["text"][:64])

    stops = it["stops"]
    legs = it["legs"]
    by_order = {s["order"]: s for s in stops}
    n_cost = sum(1 for s in stops if s["cost"]) + sum(1 for g in legs if g["cost"])
    n_trans = sum(1 for g in legs if g["transport"])

    rows = []
    for s in stops:
        ts = s["ts"]
        rows.append(f"""
        <tr>
          <td class="num">{s['order']}</td>
          <td class="place"><b>{html.escape(s['name'])}</b>
              <span class="quote">{quote(ts)}</span></td>
          <td>{html.escape(s['city']) or '—'}</td>
          <td><span class="tag">{html.escape(s['kind']) or '—'}</span></td>
          <td class="what">{html.escape(s['what']) or '—'}</td>
          <td class="cost">{html.escape(s['cost']) or '<span class="na">未提及</span>'}</td>
          <td>{('<a href="' + url + '?t=' + str(int(ts)) + '" target="_blank">' + fmt(ts) + '</a>') if ts is not None else '<span class="na">—</span>'}</td>
        </tr>""")

    leg_rows = []
    for g in legs:
        a = by_order[g["from_order"]]["name"]
        b = by_order[g["to_order"]]["name"]
        leg_rows.append(f"""
        <tr>
          <td>{html.escape(a)} <span class="arrow">→</span> {html.escape(b)}</td>
          <td>{html.escape(g['transport']) or '<span class="na">视频未提及</span>'}</td>
          <td>{html.escape(g['duration']) or '<span class="na">—</span>'}</td>
          <td>{html.escape(g['cost']) or '<span class="na">—</span>'}</td>
        </tr>""")

    chips = "".join(f'<span class="chip">{html.escape(c)}</span>' for c in it["cities"])

    doc = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>行程路线 · A 档原型</title>
<style>
  :root{{--line:#e6e4df;--ink:#1d1d1b;--muted:#75736d;--bg:#faf9f6;--card:#fff;--brand:#0f6e56;--soft:#e1f5ee}}
  *{{box-sizing:border-box}}
  body{{margin:0;background:var(--bg);color:var(--ink);
    font:15px/1.65 -apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
    padding:34px 22px 60px}}
  .wrap{{max-width:1000px;margin:0 auto}}
  h1{{font-size:19px;font-weight:500;margin:0 0 6px}}
  .sub{{color:var(--muted);font-size:13px;margin-bottom:18px}}
  .card{{background:var(--card);border:1px solid var(--line);border-radius:12px;
    padding:20px 22px;margin-bottom:18px}}
  .lead{{font-size:15px;margin:0 0 14px}}
  .chips{{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:14px}}
  .chip{{background:var(--soft);color:var(--brand);border-radius:999px;
    padding:3px 12px;font-size:12.5px}}
  .chip+ .chip::before{{content:"→";color:var(--muted);margin-right:8px;position:relative;left:-8px}}
  .stats{{display:flex;gap:26px;border-top:1px solid var(--line);padding-top:14px;font-size:13px;color:var(--muted)}}
  .stats b{{color:var(--ink);font-weight:500}}
  h2{{font-size:14px;font-weight:500;margin:0 0 12px;color:var(--muted)}}
  table{{width:100%;border-collapse:collapse;font-size:13.5px}}
  th{{text-align:left;font-weight:400;color:var(--muted);font-size:12.5px;
    padding:0 10px 8px;border-bottom:1px solid var(--line);white-space:nowrap}}
  td{{padding:11px 10px;border-bottom:1px solid #f1efea;vertical-align:top}}
  tr:last-child td{{border-bottom:none}}
  .num{{color:var(--muted);width:26px}}
  .place b{{font-weight:500}}
  .quote{{display:block;color:var(--muted);font-size:12px;margin-top:3px;line-height:1.5}}
  .tag{{background:#f1efea;color:#5f5e5a;border-radius:5px;padding:1px 7px;font-size:11.5px;white-space:nowrap}}
  .cost{{color:var(--brand);white-space:nowrap}}
  .na{{color:#b4b2a9}}
  .what{{color:#3d3c39;font-size:13px}}
  td a{{color:#185fa5;text-decoration:none;font-variant-numeric:tabular-nums}}
  td a:hover{{text-decoration:underline}}
  .arrow{{color:var(--muted)}}
  .note{{background:#faf9f6;border-left:3px solid var(--brand);padding:12px 16px;
    border-radius:0 8px 8px 0;font-size:13px;color:#3d3c39;margin:0}}
  .foot{{color:var(--muted);font-size:12px;margin-top:14px}}
  .warn{{background:#faeeda;border-radius:8px;padding:10px 14px;font-size:12.5px;color:#854f0b;margin-top:14px}}
</style></head>
<body><div class="wrap">
  <h1>行程路线 · A 档原型</h1>
  <div class="sub">{html.escape(meta['title'][:48])}　·　纯抽取，不含任何地图坐标</div>

  <div class="card">
    <p class="lead">{html.escape(it['summary'])}</p>
    <div class="chips">{chips}</div>
    <div class="stats">
      <span><b>{len(stops)}</b> 个地点</span>
      <span><b>{len(it['cities'])}</b> 座城市</span>
      <span><b>{len(legs)}</b> 段路程</span>
      <span>提到花费 <b>{n_cost}</b> 处</span>
      <span>提到交通 <b>{n_trans}/{len(legs)}</b> 段</span>
    </div>
  </div>

  <div class="card">
    <h2>去了哪（按视频出场顺序）</h2>
    <table>
      <thead><tr><th>#</th><th>地点 / 原句依据</th><th>城市</th><th>类型</th>
      <th>这一站</th><th>花费</th><th>视频</th></tr></thead>
      <tbody>{''.join(rows)}</tbody>
    </table>
  </div>

  <div class="card">
    <h2>怎么去</h2>
    <table>
      <thead><tr><th>路段</th><th>交通方式</th><th>耗时</th><th>花费</th></tr></thead>
      <tbody>{''.join(leg_rows)}</tbody>
    </table>
    <div class="warn">灰字是如实留空：这条视频确实没讲跨城怎么走（河内 → 岘港那趟航班没提），
      所以空格比编一个"建议乘机"更可信。</div>
  </div>

  <div class="card">
    <h2>花费说明</h2>
    <p class="note">{html.escape(it['budget'])}</p>
    <p class="foot">以上仅包含视频明确提到的内容；未提及的费用与交通方式留空，不做推测。</p>
  </div>
</div></body></html>"""

    out = HERE / "_proto_itinerary" / "demo.html"
    out.write_text(doc, encoding="utf-8")
    print(f"written {out}")


if __name__ == "__main__":
    main()
