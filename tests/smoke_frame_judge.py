"""画面判断：视觉模型给的有用性/信息量/页码，用来剔图与同页去重。

对应真实故障（电场课 BV1yqVU6EERP）：同一块黑板留了 3 张、纯人物帧也进笔记。
判断与「画面要点」共用同一次视觉调用，所以这一步不额外花钱。
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "vendor"))

from PIL import Image

from app.core.config import Settings
from app.core.models import Chunk, Frame
from app.services import frame_caption as FC

FAILED = 0
_TMP = Path(tempfile.mkdtemp(prefix="bn-judge-"))


def ok(cond: bool, msg: str) -> None:
    global FAILED
    if cond:
        print(f"  OK   {msg}")
    else:
        FAILED += 1
        print(f"  FAIL {msg}")


def make_frame(ts: float, name: str) -> Frame:
    p = _TMP / f"{name}.jpg"
    Image.new("RGB", (64, 36), (30, 30, 30)).save(p, "JPEG")
    return Frame(ts=ts, path=str(p), text_density=1.0)


class FakeVision:
    """按 index 依次返回预设记录。"""

    def __init__(self, records: list[dict]):
        self.records = records
        self.calls = 0

    def complete_vision(self, system, user, images, max_tokens=900):
        rec = self.records[self.calls] if self.calls < len(self.records) else {}
        self.calls += 1
        return json.dumps({"captions": [dict(rec, index=1)]}, ensure_ascii=False)


def run_caption(frames: list[Frame], records: list[dict]) -> Chunk:
    chunk = Chunk(index=0, start=0, end=600, text="这一段讲电场强度", frames=frames)
    st = Settings()
    st.frame_captions = True
    FC.caption_frames(FakeVision(records), [chunk], st, vision_client=FakeVision(records))
    return chunk


def test_judge_stored_without_text() -> None:
    print("\n--- 无文字的帧也要记下判断 ---")
    f = make_frame(10, "a")
    chunk = run_caption([f], [{"kind": "人物", "useful": False, "info": 1,
                              "page": "", "title": "", "points": []}])
    f = chunk.frames[0]
    ok(f.useful is False, "useful=False 落到帧上")
    ok(f.info_level == 1, f"info_level=1（实际 {f.info_level}）")
    ok(f.kind == "人物", "kind=人物")


def test_prune_drops_and_dedupes() -> None:
    print("\n--- 剔无信息 + 同页去重 ---")
    f1 = make_frame(12, "b1")   # 同一页，信息量 2
    f2 = make_frame(24, "b2")   # 同一页，信息量 4（更全）
    f3 = make_frame(30, "c")    # 人物，无用
    f4 = make_frame(48, "d")    # 另一页
    records = [
        {"kind": "板书", "useful": True, "info": 2, "page": "§9.3 电场", "title": "§9.3", "points": ["引入"]},
        {"kind": "板书", "useful": True, "info": 4, "page": "§9.3 电场", "title": "§9.3", "points": ["引入", "定义"]},
        {"kind": "人物", "useful": False, "info": 1, "page": "", "title": "", "points": []},
        {"kind": "板书", "useful": True, "info": 3, "page": "电场强度", "title": "电场强度", "points": ["E=F/q"]},
    ]
    chunk = run_caption([f1, f2, f3, f4], records)
    stats = FC.prune([chunk])
    kept = [round(f.ts) for f in chunk.frames]
    ok(12 not in kept, "同页里信息量低的被剔掉")
    ok(24 in kept, "同页里信息量高的留下")
    ok(30 not in kept, "人物帧被剔掉")
    ok(48 in kept, "另一页保留")
    ok(len(kept) == 2, f"最终剩 2 张（实际 {kept}）")
    ok(stats["useless"] == 1 and stats["duplicate"] == 1,
       f"统计正确：{stats}")


def test_unjudged_untouched() -> None:
    print("\n--- 没判过的帧不动（没配视觉模型时）---")
    frames = [make_frame(10, "u1"), make_frame(20, "u2")]
    chunk = Chunk(index=0, start=0, end=600, text="", frames=frames)
    stats = FC.prune([chunk])
    ok(len(chunk.frames) == 2, "两张都留着")
    ok(stats["useless"] == 0 and stats["duplicate"] == 0, "没有任何剔除")


def test_keep_same_page_when_later_more_complete() -> None:
    print("\n--- 同页同信息量时留靠后的（板书写得更全）---")
    f1 = make_frame(60, "e1")
    f2 = make_frame(120, "e2")
    records = [
        {"kind": "板书", "useful": True, "info": 3, "page": "同一块黑板", "title": "板书", "points": ["a"]},
        {"kind": "板书", "useful": True, "info": 3, "page": "同一块黑板", "title": "板书", "points": ["a", "b"]},
    ]
    chunk = run_caption([f1, f2], records)
    FC.prune([chunk])
    kept = [round(f.ts) for f in chunk.frames]
    ok(kept == [120], f"留下 120s 那张（实际 {kept}）")


def test_all_useless_keeps_one() -> None:
    """整块都判成没用（纯人物讲解）时，至少留一张，别让那一节没画面。"""
    print("\n--- 整块无可用画面时保底 ---")
    f1 = make_frame(30, "g1")
    f2 = make_frame(60, "g2")
    records = [
        {"kind": "人物", "useful": False, "info": 1, "page": "", "title": "", "points": []},
        {"kind": "人物", "useful": False, "info": 2, "page": "", "title": "", "points": []},
    ]
    chunk = run_caption([f1, f2], records)
    FC.prune([chunk])
    ok(len(chunk.frames) == 1, f"仍留 1 张（实际 {len(chunk.frames)}）")
    ok(chunk.frames and round(chunk.frames[0].ts) == 60, "留的是信息量更高的那张")


if __name__ == "__main__":
    test_judge_stored_without_text()
    test_prune_drops_and_dedupes()
    test_unjudged_untouched()
    test_keep_same_page_when_later_more_complete()
    test_all_useless_keeps_one()
    print()
    if FAILED:
        print(f"失败 {FAILED} 项")
        sys.exit(1)
    print("画面判断：全部通过")
