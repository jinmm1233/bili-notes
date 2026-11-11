"""取帧选择回归：同一页去重 + 配额铺满整个知识块。

对应两个真实故障（电场 电场强度试讲 · BV1yqVU6EERP，2026-11-11）：
  1. 00:06 / 00:12 / 00:24 三张是同一块黑板：老师走动把整帧 pHash 拉开 6-8 位，
     严格的 4 位阈值挡不住。
  2. 全片 147 张候选（每 6 秒一张），最后只留 24 张，且每块都取块首 48 秒，
     后面写满板书的四分钟一张没进 —— 01:54–03:56 整章没有配图。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "vendor"))

from app.core.config import Settings
from app.core.models import Chunk, Frame
from app.services import frames as F

FAILED = 0


def ok(cond: bool, msg: str) -> None:
    global FAILED
    if cond:
        print(f"  OK   {msg}")
    else:
        FAILED += 1
        print(f"  FAIL {msg}")


def _bits(hexstr: str) -> int:
    return int(hexstr, 16)


def phash_page(seed: int, drift: int = 0) -> str:
    """构造一个 64 位感知哈希：seed 决定「哪一页」，drift 模拟人物走动带来的位漂移。"""
    base = (seed * 0x9E3779B97F4A7C15) & 0xFFFFFFFFFFFFFFFF
    base |= 0xF000000000000000          # 保证有效位够多（_hamming 会挡掉近纯色）
    out = base ^ ((1 << drift) - 1 if drift else 0)
    return f"{out:016x}"


def test_same_page() -> None:
    print("\n--- 同一页判据 ---")
    a = Frame(ts=6, path="a.jpg", phash=phash_page(1))
    b = Frame(ts=12, path="b.jpg", phash=phash_page(1, drift=4))   # 人走动，差 4 位
    c = Frame(ts=24, path="c.jpg", phash=phash_page(1, drift=8))   # 差 8 位
    ok(F._same_page(a, b), "90 秒内差 4 位 → 同一页")
    ok(F._same_page(a, c), "90 秒内差 8 位 → 仍是同一页（旧逻辑会漏）")

    far = Frame(ts=400, path="d.jpg", phash=phash_page(1, drift=8))
    ok(not F._same_page(a, far), "同样差 8 位但隔了 6 分钟 → 不判重复（长视频补板书要留）")

    other = Frame(ts=10, path="e.jpg", phash=phash_page(2))
    ok(not F._same_page(a, other), "不同页 → 不判重复")

    blank1 = Frame(ts=1, path="f.jpg", phash="8000000000000000")
    blank2 = Frame(ts=2, path="g.jpg", phash="8000000000000000")
    ok(not F._same_page(blank1, blank2), "近纯色哈希不当重复（避免把不同画面全丢掉）")


def test_board_not_repeated() -> None:
    """前半段同一块黑板 8 张，只应留 1 张。"""
    print("\n--- 同一块板书不重复 ---")
    chunk = Chunk(index=0, start=0, end=600, text="")
    frames = [Frame(ts=6 + 6 * i, path=f"a{i}.jpg", text_density=0.5, ocr_text="板书" * 40,
                    phash=phash_page(1, drift=min(8, i * 2)))
              for i in range(8)]
    # 后半段翻到新的一页
    frames += [Frame(ts=360 + 6 * i, path=f"b{i}.jpg", text_density=0.5, ocr_text="板书" * 40,
                     phash=phash_page(2, drift=i % 4))
               for i in range(8)]
    st = Settings()
    got = F.score_and_select(st, frames, [chunk])
    early = [f for f in got if f.ts < 300]
    ok(len(early) == 1, f"同一块黑板只留 1 张（实际 {len(early)} 张：{[round(f.ts) for f in early]}）")


def test_spread_over_chunk() -> None:
    """8 个名额要铺满 10 分钟的知识块，不能全给开头。"""
    print("\n--- 配额铺满整个知识块 ---")
    chunk = Chunk(index=0, start=0, end=600, text="")
    frames = []
    for i, ts in enumerate(range(6, 600, 6)):
        page = ts // 60                       # 每分钟换一次内容（模拟边讲边写）
        frames.append(Frame(ts=float(ts), path=f"f{i}.jpg", text_density=0.5,
                            ocr_text="板书" * 40,
                            phash=phash_page(page + 10, drift=i % 3)))
    st = Settings()
    got = F.score_and_select(st, frames, [chunk])
    ts_list = sorted(f.ts for f in got)
    ok(len(got) == st.max_frames_per_chunk,
       f"选满配额 {st.max_frames_per_chunk} 张（实际 {len(got)}）")
    ok(ts_list[-1] > 480, f"最后一张落在块尾部（实际 {ts_list[-1]:.0f}s）")
    ok(ts_list[0] < 120, f"第一张仍从块首开始（实际 {ts_list[0]:.0f}s）")
    gaps = [b - a for a, b in zip(ts_list, ts_list[1:])]
    ok(min(gaps) >= 40, f"最小间隔 ≥ 40s（实际 {min(gaps):.0f}s）")


if __name__ == "__main__":
    test_same_page()
    test_board_not_repeated()
    test_spread_over_chunk()
    print()
    if FAILED:
        print(f"失败 {FAILED} 项")
        sys.exit(1)
    print("取帧选择：全部通过")
