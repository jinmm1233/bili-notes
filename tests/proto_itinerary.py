"""A 档原型：拿真实缓存跑一次行程单抽取，看准不准。

只读缓存里的 transcript.json + meta.json，写文件到 tests/_proto_itinerary/，
不碰用户的输出目录，也不改任何设置。

用法：
    python tests/proto_itinerary.py <cache任务目录名>
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import Settings  # noqa: E402
from app.core.models import Segment, VideoMeta  # noqa: E402
from app.core.secretstore import SecretStore  # noqa: E402
from app.core.llm import LLMClient  # noqa: E402
from app.services import chunking, itinerary  # noqa: E402

CACHE = Path("C:/Users/86159/AppData/Roaming/BiliNotes/cache")


def load(task_id: str):
    work = CACHE / task_id
    raw_meta = json.loads((work / "meta.json").read_text(encoding="utf-8"))
    raw_segs = json.loads((work / "transcript.json").read_text(encoding="utf-8"))
    meta = VideoMeta(**raw_meta)
    segs = [Segment(**s) for s in raw_segs]
    return meta, segs


def main() -> int:
    task_id = sys.argv[1] if len(sys.argv) > 1 else "26f0f33a2f90"
    settings = Settings()
    store = SecretStore(settings.data_dir / "secrets.dat")
    client = LLMClient.from_settings(settings, store)
    if client is None:
        print("没有可用的模型 Key，无法跑抽取")
        return 1

    meta, segs = load(task_id)
    chunks = chunking.build_chunks(segs, target=settings.chunk_target_chars,
                                  max_chars=settings.chunk_max_chars)
    score = itinerary.travel_score(meta, chunks)
    print(f"标题：{meta.title}")
    print(f"时长：{meta.duration:.0f}s / 转录 {len(segs)} 句 {sum(len(s.text) for s in segs)} 字")
    print(f"旅行信号词命中：{score}（阈值 6）→ {'判定为旅行类' if score >= 6 else '未判定为旅行类'}")
    print(f"材料长度：{len(itinerary._material(chunks))} 字")
    print("抽取中…")

    it = itinerary.build_itinerary(client, meta, chunks, segments=segs)
    if not it:
        print("抽取失败或地点不足 2 个")
        return 1

    out = Path(__file__).resolve().parent / "_proto_itinerary"
    out.mkdir(exist_ok=True)
    (out / "itinerary.json").write_text(
        json.dumps(it, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    md_lines = itinerary.render_section(it, url=meta.webpage_url)
    (out / "itinerary.md").write_text("\n".join(md_lines), encoding="utf-8")
    # 整段落直接作为笔记的一节，预览用
    (out / "section.md").write_text("\n".join(md_lines), encoding="utf-8")

    print("\n===== 抽取结果 =====")
    print(f"一句话：{it['summary']}")
    print(f"城市串联：{' → '.join(it['cities'])}")
    print(f"地点 {len(it['stops'])} 个 / 路段 {len(it['legs'])} 段\n")
    for s in it["stops"]:
        stamp = f"@{s['ts']:.0f}s" if s["ts"] is not None else "@?"
        print(f"  {s['order']:>2}. {s['name']}（{s['city'] or '?'}·{s['kind'] or '?'}）"
              f" {stamp}  {s['what']}  {('花费:' + s['cost']) if s['cost'] else ''}")
    print()
    for leg in it["legs"]:
        a = next((x["name"] for x in it["stops"] if x["order"] == leg["from_order"]), "?")
        b = next((x["name"] for x in it["stops"] if x["order"] == leg["to_order"]), "?")
        print(f"  {a} → {b} | 交通:{leg['transport'] or '-'} | 耗时:{leg['duration'] or '-'} "
              f"| 花费:{leg['cost'] or '-'}")
    print(f"\n花费说明：{it['budget']}")
    print(f"\n已写入 {out/'itinerary.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
