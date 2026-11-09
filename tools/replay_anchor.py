"""用两份真实笔记的缓存离线重放「音画锚定」，逐条验收要点 ↔ 配图。

跑法：python tools/replay_anchor.py
只读缓存（不下载、不调模型、不写用户笔记），验证：
  1. 每句无字幕口播被归到它发生时画面正停着的那一页；
  2. 三个已知错配（定语后置 / 宾语从句 / 描述销售代表外貌）是否修正；
  3. 配图的画面文字与讲解的知识点是否一致。

旧缓存里的入选帧是「每 6 秒均匀抽」的，新链路会为每个稳定段末抽完整帧。
这里用 uniform 目录里最接近段末的那张、配上同段 OCR 来模拟新抽帧结果。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.core.models import Frame, Segment  # noqa: E402
from app.core.paths import fmt_ts  # noqa: E402
from app.services import anchors as A  # noqa: E402
from app.services import chunking, frames as F  # noqa: E402

CACHE = Path(
    __import__("os").environ.get(
        "BILINOTES_CACHE",
        str(Path.home() / "AppData" / "Roaming" / "BiliNotes" / "cache"),
    )
)

# 任务号 → 名称
TASKS = {
    "006ae688bfee": "TED 英语演讲逐句讲解",
    "c9415a384190": "CSAPP p16 bits/bytes/integers",
}

# 旧笔记里的已知错配：知识点 → (ASR 关键词, 旧配图秒数, 期望的画面关键词, 旧图是否配错)
# 最后一条是把本来就配对的 happen to 卡当对照组，验证新链路没有把它弄坏。
CHECKS = {
    "006ae688bfee": [
        ("定语后置", "定语后制", 36.0, "true story", True),
        ("宾语从句", "并语从具", 102.0, "", True),
        ("描述销售代表外貌", "well-dressed", 156.0, "well-dressed", True),
        ("碰巧 happen to（对照组）", "Happen to是", 150.0, "happen to", False),
    ],
}

FAILED = 0
PASS = 0


def ok(cond: bool, msg: str) -> None:
    global FAILED, PASS
    print(("  [OK] " if cond else "  [FAIL] ") + msg)
    if cond:
        PASS += 1
    else:
        FAILED += 1


def load(task_id: str):
    root = CACHE / task_id
    meta = json.loads((root / "meta.json").read_text(encoding="utf-8"))
    segments = [
        Segment(start=float(s["start"]), end=float(s["end"]),
                text=s["text"], source=s.get("source", "asr"))
        for s in json.loads((root / "transcript.json").read_text(encoding="utf-8"))
    ]
    raw = json.loads((root / "chunks.json").read_text(encoding="utf-8"))
    chosen = [d for c in raw for d in c.get("frames") or []]

    thumbs = [
        (float(i), p)
        for i, p in enumerate(sorted((root / "frames" / "thumbs").glob("thumb_*.jpg")))
    ]
    spans = F.detect_scene_spans(thumbs, distance=14, min_stable=2)

    # 旧链路入选帧（带真实 OCR）
    frames: list[Frame] = []
    for d in chosen:
        f = Frame(
            ts=float(d["ts"]), path=d["path"],
            origin=d.get("origin", "uniform"),
            ocr_text=d.get("ocr_text") or "",
            screen_title=d.get("screen_title") or "",
            screen_points=d.get("screen_points") or [],
            caption=d.get("caption") or "",
        )
        F.attach_span(f, spans)
        frames.append(f)

    # 模拟新链路「稳定段末抽完整帧」：用 uniform 目录里最接近段末的一张，
    # OCR 借用同一段里最近的一张已识别帧（画面停住时内容相同）。
    uni: list[tuple[float, Path]] = []
    for p in sorted((root / "frames" / "uniform").glob("at_*.jpg")):
        m = re.search(r"at_(\d+)\.jpg$", p.name)
        if m:
            uni.append((int(m.group(1)) / 1000.0, p))
    # 只从「旧链路真正识别过的帧」借 OCR；否则模拟帧之间会互相传染文字
    base_frames = list(frames)
    sim = 0
    for s in spans:
        if not s.stable or not uni:
            continue
        ts, path = min(uni, key=lambda x: abs(x[0] - s.end))
        if abs(ts - s.end) > 8.0:
            continue
        # OCR 只能从「这一页停留期间或刚开始前」的已识别帧借，
        # 绝不能从段末之后借——那已经是下一页了（真实链路会重新 OCR）。
        ocr_src = max(
            (f for f in base_frames if f.ocr_text
             and s.start - 8.0 <= f.ts <= s.end + 0.5),
            key=lambda f: f.ts, default=None,
        )
        # 真实链路在 span.end 这一刻截帧；图片的 ts 就是段末，
        # 不能写成 uniform 文件的时间（可能已跨到下一页）。
        frames.append(Frame(
            ts=s.end, path=str(path), origin="scene",
            seg_start=s.start, seg_end=s.end,
            ocr_text=ocr_src.ocr_text if ocr_src else "",
            screen_title=ocr_src.screen_title if ocr_src else "",
            screen_points=list(ocr_src.screen_points) if ocr_src else [],
        ))
        sim += 1
    return meta, segments, spans, frames, sim


def replay(task_id: str, name: str) -> None:
    print(f"\n===== {name}（{task_id}）=====")
    try:
        meta, segments, spans, frames, sim = load(task_id)
    except FileNotFoundError as exc:
        print(f"  跳过：缓存不全（{exc}）")
        return
    duration = float(meta["duration"])
    print(f"  时长 {fmt_ts(duration)}｜ASR {len(segments)} 句｜画面停留段 {len(spans)} "
          f"（稳定 {sum(1 for s in spans if s.stable)}）｜模拟完整帧 {sim} 张")

    chunks = chunking.build_chunks(
        segments, target=800, max_chars=1200, min_chars=120,
        scene_bounds=A.bounds_from_spans(spans),
    )
    all_anchors = A.build_anchors(chunks, segments, frames, duration, spans=spans)
    print(f"  新链路切出 {len(chunks)} 块 / {len(all_anchors)} 个音画对")

    # 通用验收：每个有讲解的音画对，配图时间必须落在它讲的那段画面里
    spoken = [a for a in all_anchors if a.speech]
    inside = [a for a in spoken
              if a.span_start - 0.5 <= a.frame_ts <= a.span_end + 0.5]
    rate = len(inside) / len(spoken) if spoken else 0.0
    ok(rate >= 0.98,
       f"配图落在讲解所属画面段的比例 {rate * 100:.1f}%（{len(inside)}/{len(spoken)}）")

    def ocr_at(ts: float) -> str:
        f = min((x for x in frames if x.ocr_text),
                key=lambda x: abs(x.ts - ts), default=None)
        return f.ocr_text if f else ""
    ok(all(c.anchors for c in chunks), "每个知识块都拿到了成对材料")
    ok(all("【讲解】" in A.anchor_material(c) for c in chunks), "成对材料里保留无字幕口播")

    for label, keyword, old_ts, expect, old_wrong in CHECKS.get(task_id, []):
        hits = [s for s in segments if keyword in s.text]
        if not hits:
            print(f"  [跳过] {label}：缓存转写里没找到「{keyword}」")
            continue
        seg = hits[0]
        mid = (seg.start + seg.end) / 2
        owners = [a for a in all_anchors if a.start <= mid <= a.end]
        anchor = (
            owners[0] if owners
            else min(all_anchors, key=lambda a: abs(a.start - seg.start))
        )
        screen = (anchor.screen or "").replace("\n", " ")
        excerpt = anchor.speech[:34].replace("\n", " ")
        print(f"\n  · {label}｜讲解 {fmt_ts(seg.start)}–{fmt_ts(seg.end)}「{excerpt}」")
        print(f"    旧笔记配图 {fmt_ts(old_ts)}　→　新配图 {fmt_ts(anchor.frame_ts)}"
              f"（画面停留 {fmt_ts(anchor.span_start)}–{fmt_ts(anchor.span_end)}）")
        print(f"    新画面的文字：{screen[:96]}")
        ok(anchor.span_start - 0.5 <= anchor.frame_ts <= anchor.span_end + 0.5,
           f"{label}：配图属于讲解发生时停住的那一页")
        if expect:
            old_ocr = ocr_at(old_ts)
            ok(expect.lower() in screen.lower(),
               f"{label}：画面文字含「{expect}」，不是别的知识点的卡")
            if old_wrong:
                ok(expect.lower() not in old_ocr.lower(),
                   f"{label}：旧笔记 {fmt_ts(old_ts)} 那张确实是另一页（不含「{expect}」）")


def main() -> int:
    print("=== 真实笔记缓存 · 音画锚定离线重放 ===")
    for tid, name in TASKS.items():
        replay(tid, name)
    print(f"\n通过 {PASS} 项，失败 {FAILED} 项")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
