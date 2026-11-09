"""音画锚定回归：无字幕讲解能不能对上它讲的那张画面。

跑法：python tests/smoke_anchor.py
不联网、不碰真实数据目录、不调真模型（用假 client 记录 prompt 和返回）。

覆盖链路：
  稳定画面区间 → 讲解句归属 → 成对材料 → 逐块总结 bullet_ts
  → 大纲 items_ts → 导出时按要点真实时间配图 + 时间码跳转
"""
from __future__ import annotations

import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

from app.core.models import Anchor, Chunk, Frame, Segment, VideoMeta, VideoNote  # noqa: E402
from app.core.paths import fmt_ts  # noqa: E402
from app.services import anchors as A  # noqa: E402
from app.services import chunking, exporter, frames as F, summarize  # noqa: E402

FAILED = 0
PASS = 0


def ok(cond: bool, msg: str) -> None:
    global FAILED, PASS
    print(("[OK] " if cond else "[FAIL] ") + msg)
    if cond:
        PASS += 1
    else:
        FAILED += 1


# ---- 素材：4 页 PPT，每页停 30 秒，博主在每页停留期间做无字幕讲解 ----
SLIDES = [
    (0.0, 30.0, "This is a true story 定语后置卡"),
    (30.0, 60.0, "happen to 碰巧 语法卡"),
    (60.0, 90.0, "well-dressed 销售代表 卡"),
    (90.0, 120.0, "总结页 卡"),
]
SPEECH = [
    (2.0, 9.0, "This is a true story 这句里 story 后面是定语后置，字幕里没有，是博主补的分析。"),
    (12.0, 20.0, "这里的 art 指文科不是艺术，也是暂停下来讲的。"),
    (33.0, 45.0, "happen to 后面接动词原形，表示碰巧。"),
    (63.0, 78.0, "well-dressed 是描述销售代表的外貌。"),
    (93.0, 106.0, "最后把这几点串起来复习一遍。"),
]


def make_frames() -> list[Frame]:
    """每页给「段末完整帧 + 段内均匀帧」，都标上所属画面停留区间。"""
    out: list[Frame] = []
    for n, (s, e, text) in enumerate(SLIDES):
        for ts in (s + 6, s + 16, e):
            out.append(Frame(
                ts=ts, path=f"slide{n}_{int(ts)}.jpg", origin="scene",
                seg_start=s, seg_end=e,
                ocr_text=text, text_density=0.6,
            ))
        # 段内还有均匀兜底帧：这些是「动画没放全」的中间态，
        # 锚定必须优先认上面的稳定段完整帧，不能被它们带偏。
        out.append(Frame(
            ts=s + 10, path=f"slide{n}_{int(s + 10)}_uniform.jpg", origin="uniform",
            seg_start=s, seg_end=e, ocr_text=text, text_density=0.6,
        ))
    return out


def make_segments() -> list[Segment]:
    return [Segment(start=s, end=e, text=t, source="asr") for s, e, t in SPEECH]


def make_meta() -> VideoMeta:
    return VideoMeta(
        bvid="BVtest", cid=1, title="TED 英语演讲逐句讲解", uploader="测试UP",
        duration=120.0, pubdate=0, cover="",
        webpage_url="https://www.bilibili.com/video/BVtest",
    )


class FakeClient:
    """只记录 prompt、按调用次数返回预定 JSON，绝不联网。"""

    def __init__(self, replies: list[dict]):
        self.replies = list(replies)
        self.prompts: list[str] = []
        self.model = "fake"

    def complete_json(self, system: str, user: str, *, max_tokens: int = 2048) -> dict:
        self.prompts.append(user)
        return self.replies.pop(0) if self.replies else {}


def test_scene_spans() -> list[Frame]:
    """缩略图 → 画面停留区间：区间必须覆盖全片，稳定段代表帧是段末。"""
    with tempfile.TemporaryDirectory() as tmp:
        thumbs = []
        # 4 段各 5 张「同一张图」= 静止；换成另一张随机纹理 = 换页。
        # 纯色图的 phash 全是 0，区分不出换页，所以用固定 seed 的纹理。
        for i in range(4):
            p = Path(tmp) / f"t{i}.png"
            rng = np.random.default_rng(100 + i)
            Image.fromarray(
                rng.integers(0, 256, (64, 64, 3), dtype=np.uint8)
            ).save(p)
            thumbs.extend((i * 30.0 + k * 6.0, p) for k in range(5))
        spans = F.detect_scene_spans(thumbs, distance=14, min_stable=2)
        points = F.stable_points(spans)
        ok(len(spans) == 4, f"4 个画面停留段（实际 {len(spans)}）")
        ok(all(s.stable for s in spans), "每段都判定为稳定画面")
        ok(spans[0].start == 0.0 and spans[-1].end == 114.0,
           f"区间覆盖全片 00:00–01:54（实际 {spans[0].start}–{spans[-1].end}）")
        ok([round(p) for p in points] == [24, 54, 84, 114],
           f"取帧时间点是各段末帧（实际 {[round(p) for p in points]}）")
        legacy = F.detect_stable(thumbs, distance=14, min_stable=2)
        ok(legacy == points, "detect_stable 旧接口返回不变，老测试不破")
    frames = make_frames()
    ok(len(A.spans_from_frames(frames)) == 4, "从候选帧能还原出 4 个停留段")
    ok(A.scene_bounds(frames) == [30.0, 60.0, 90.0, 120.0],
       "画面切换边界 = 停留满 2 秒的段末")
    return frames


def test_anchors(frames: list[Frame]) -> tuple[list[Chunk], list[Segment]]:
    """每句讲解必须归到它发生时画面正停着的那一页。"""
    segments = make_segments()
    chunks = chunking.build_chunks(
        segments, target=100, max_chars=260, min_chars=20,
        scene_bounds=A.scene_bounds(frames),
    )
    ok(len(chunks) >= 2, f"按画面边界切出多个知识块（实际 {len(chunks)}）")
    bounds = [30.0, 60.0, 90.0, 120.0]
    gaps = [(chunks[i].end, chunks[i + 1].start) for i in range(len(chunks) - 1)]
    ok(any(any(prev <= b <= nxt for b in bounds) for prev, nxt in gaps),
       f"切块切在画面切换处（块间间隙 {gaps} 覆盖 {bounds} 之一）")

    all_anchors = A.build_anchors(chunks, segments, frames, 120.0)
    ok(len(all_anchors) >= 4, f"每页讲解都有音画对（实际 {len(all_anchors)}）")
    ok(all(c.anchors for c in chunks), "每个知识块都挂上了锚点")

    def owner(text: str) -> float:
        hits = [a for a in all_anchors if text in a.speech]
        ok(bool(hits), f"讲解「{text}」有对应音画对")
        return hits[0].frame_ts if hits else -1

    t1 = owner("定语后置")
    t2 = owner("happen to")
    t3 = owner("well-dressed")
    ok(0 <= t1 < 30, f"定语后置 → 第 1 页卡（frame_ts={t1}）")
    ok(30 <= t2 < 60, f"happen to → 第 2 页卡（frame_ts={t2}）")
    ok(60 <= t3 < 90, f"well-dressed → 第 3 页卡（frame_ts={t3}）")
    ok(len({t1, t2, t3}) == 3, "三个知识点指向三张不同的图")
    ok(all("_uniform" not in a.frame_path for a in all_anchors),
       "锚定只认稳定段末的完整帧，不挑动画中间态的均匀帧")
    ok(all(a.screen for a in all_anchors), "每个音画对都带画面文字")
    ok(len(A.must_keep_paths(chunks)) >= 4, "被讲解指着的帧进入优先保留集合")

    material = A.anchor_material(chunks[0])
    ok("【画面】" in material and "【讲解】" in material, "材料把画面文字与口播分开")
    ok(f"画面 {fmt_ts(all_anchors[0].frame_ts)}" in material, "材料带真实画面时间码")
    return chunks, segments


def test_ts_utils(chunks: list[Chunk]) -> None:
    ok(A.parse_ts("00:06") == 6.0 and A.parse_ts("1:02:33") == 3753.0,
       "时间码解析支持 MM:SS / HH:MM:SS")
    ok(A.parse_ts("不是时间") == 0.0, "非时间码返回 0")
    ts, text = A.split_item_ts("[00:28] happen to 后接动词原形")
    ok(ts == 28.0 and text == "happen to 后接动词原形", "剥掉要点开头的时间码")
    ok(A.split_item_ts("普通要点") == (0.0, "普通要点"), "没时间码的要点保持原样")
    # 模型抄成讲解区间起点 00:02，应靠停留区间直接归到该页的画面帧
    snapped = summarize.snap_anchor_ts(2.0, chunks)
    ok(0 <= snapped < 30, f"抄成讲解起点也能吸附到本页画面（{snapped}）")
    ok(summarize.snap_anchor_ts(0.0, chunks) == 0.0, "0 时间码不吸附")


def test_material_budget() -> None:
    """一页讲很久、块里十几个锚点时，不能把后面的知识点整段截掉。"""
    many = [
        Anchor(
            start=i * 10.0, end=i * 10.0 + 8.0, frame_ts=i * 10.0 + 6.0,
            speech=f"第{i}个知识点的无字幕讲解，博主补充了很多分析。" * 8,
            screen=f"第{i}页 PPT 画面上的例句与分点。" * 5,
            span_start=i * 10.0, span_end=i * 10.0 + 9.0,
        )
        for i in range(20)
    ]
    chunk = Chunk(index=99, start=0.0, end=200.0, text="", anchors=many)
    material = A.anchor_material(chunk, max_chars=1200)
    ok(len(material) <= 1200, f"材料不超预算（实际 {len(material)} 字）")
    missing = [i for i in range(20)
               if f"画面 {fmt_ts(i * 10.0 + 6.0)}" not in material]
    ok(not missing, f"20 个知识点的画面时间码全在（缺 {missing}）")
    ok("【讲解】" in material and "【画面】" in material, "压缩后仍区分画面与口播")


def test_summarize_chunks(chunks: list[Chunk]) -> None:
    """逐块总结不能 KeyError（模板换了字段），且要点带真实帧时间。"""
    # 只测第一块，避免假回复数量对不上
    one = [chunks[0]]
    client = FakeClient([{
        "title": "定语后置", "summary": "博主补充了语法点。",
        "bullets": ["story 后面是定语后置", "art 指文科"],
        "bullet_ts": ["00:12", "00:33"],
        "keywords": ["定语后置"],
    }])
    summarize.summarize_chunks(client, one, make_meta(), concurrency=1)
    prompt = client.prompts[0]
    ok("【画面】" in prompt and "【讲解】" in prompt, "逐块总结 prompt 用成对材料")
    ok("画面 00:" in prompt, "逐块总结 prompt 带真实画面时间码")
    ok("{text}" not in prompt and "{ocr}" not in prompt and "{material}" not in prompt,
       "模板占位符全部填好，没有 KeyError 残留")
    ok(len(one[0].bullets) == 2 and len(one[0].bullet_ts) == 2,
       "bullets 与 bullet_ts 一一对应")
    ok(all(t > 0 for t in one[0].bullet_ts), "每条要点都有它讲的画面时间")


def test_build_outline(chunks: list[Chunk]) -> list[dict]:
    client = FakeClient([{
        "structure": [{
            "title": "语法点讲解", "summary": "博主逐句分析", "cover_ts": "00:12",
            "groups": [{
                "title": "定语后置与碰巧",
                "cover_ts": "00:36",
                "items": ["[00:12] story 后面是定语后置", "[00:36] happen to 后接动词原形"],
            }],
        }],
        "takeaways": ["无字幕口播也要进笔记"],
        "problems": [],
    }])
    outline, takeaways, _ = summarize.build_outline(client, make_meta(), chunks)
    ok(bool(outline) and bool(takeaways), "大纲正常产出")
    grp = outline[0]["groups"][0]
    ok(all(not x.startswith("[") for x in grp["items"]), "items 剥掉时间码，导图/内容结构不受影响")
    ok(len(grp["items_ts"]) == 2 and all(t > 0 for t in grp["items_ts"]),
       "items_ts 存了每条要点的真实画面时刻")
    ok(grp["ts"] == grp["items_ts"][0], "二级主题配图优先用要点时间，不用模型猜的 cover_ts")
    ok(outline[0]["ts"] > 0, "一级主题也拿到真实时间")
    ok("画面 00:" in client.prompts[0], "大纲 prompt 用成对带时间的材料")
    return outline


def test_fallback_item_ts(chunks: list[Chunk]) -> None:
    """模型整组漏写 [MM:SS] 时，还能从逐块要点继承真实画面时刻。"""
    client = FakeClient([{
        "structure": [{
            "title": "漏写时间码", "summary": "", "cover_ts": "",
            "groups": [{
                "title": "定语后置", "cover_ts": "",
                "items": ["story 后面是定语后置这个语法点"],
            }],
        }],
        "takeaways": [], "problems": [],
    }])
    outline, _, _ = summarize.build_outline(client, make_meta(), chunks)
    grp = outline[0]["groups"][0]
    ok(any(t > 0 for t in grp["items_ts"]),
       f"漏写时间码也能继承逐块要点的时间（{grp['items_ts']}）")
    ok(summarize._infer_item_times(["完全无关的旅游行程安排"], chunks) == [0.0],
       "字面不吻合的要点不乱指画面")


def test_export(chunks: list[Chunk], outline: list[dict]) -> None:
    """导出：要点带时间码、按要点时间配图、容差收紧、一张图可重复出现。"""
    meta = make_meta()
    frames = make_frames()
    # 把帧挂到块上，导出层只认 chunk.frames
    for i, f in enumerate(frames):
        chunks[i % len(chunks)].frames.append(f)
    image_map = {id(f): f"assets/{f.path}" for f in frames}
    note = VideoNote(meta=meta, chunks=chunks, outline=outline)

    text = "\n".join(exporter._render_outline(note, image_map))
    ok("story 后面是定语后置" in text, "要点文本完整")
    ok("`00:" in text and "t=" in text, "每条要点带时间码跳转链接")
    ok(text.count("![画面 ") >= 2, "按要点时间配了多张图，不是一段只有一张封面")

    # 「定语后置」这条要点前面最近的图，必须是它讲的那一页
    lines = text.splitlines()
    idx = next(i for i, ln in enumerate(lines) if "story 后面是定语后置" in ln)
    def shot_ts(line: str) -> float:
        m = re.search(r"!\[画面 (\d+:\d+(?::\d+)?)\]", line)
        return A.parse_ts(m.group(1)) if m else -1.0

    shots = [ln for ln in lines[:idx] if ln.startswith("![画面 ")]
    first_ts = shot_ts(shots[-1]) if shots else -1.0
    ok(0 <= first_ts < 30,
       f"定语后置配的是第 1 页卡（{shots[-1] if shots else '无'}）")
    idx2 = next(i for i, ln in enumerate(lines) if "happen to 后接动词原形" in ln)
    shots2 = [ln for ln in lines[:idx2] if ln.startswith("![画面 ")]
    second_ts = shot_ts(shots2[-1]) if shots2 else -1.0
    ok(30 <= second_ts < 60 and second_ts != first_ts,
       f"讲到下一页时配图跟着换到 happen to 卡（{shots2[-1] if shots2 else '无'}）")

    # 同一时刻的两个二级主题：图应各出现一次（取消全篇一图一次）
    repeat_outline = [{
        "title": "重复验证", "summary": "", "ts": 16.0, "cover_ts": 16.0,
        "groups": [
            {"title": "A", "items": ["要点甲"], "items_ts": [16.0], "ts": 16.0, "cover_ts": 16.0},
            {"title": "B", "items": ["要点乙"], "items_ts": [16.0], "ts": 16.0, "cover_ts": 16.0},
        ],
    }]
    note2 = VideoNote(meta=meta, chunks=chunks, outline=repeat_outline)
    text2 = "\n".join(exporter._render_outline(note2, image_map))
    # 连续紧挨的重复会被渲染层去掉，但两个不同主题不该共用「全篇一次」的老限制
    ok("要点甲" in text2 and "要点乙" in text2, "两个主题的要点都在")

    # 容差：cover_ts 指向 01:50，附近没有帧时宁可不配图
    far = [{"title": "远", "summary": "", "cover_ts": 110.0,
            "groups": [{"title": "远", "items": ["没有图的要点"], "cover_ts": 110.0}]}]
    note3 = VideoNote(meta=meta, chunks=chunks, outline=far)
    text3 = "\n".join(exporter._render_outline(note3, image_map))
    ok("没有图的要点" in text3, "旧笔记（无 items_ts）走兜底不报错")
    ok(exporter._nearest_frame(frames, 110.0, tol=8.0) is not None,
       "01:50 有帧时容差内可命中")
    ok(exporter._nearest_frame([frames[0]], 110.0, tol=8.0) is None,
       "容差从 75 秒收紧到 ±8 秒：差太远不给错图")


def main() -> int:
    print("=== 音画锚定回归 ===")
    frames = test_scene_spans()
    chunks, _ = test_anchors(frames)
    test_ts_utils(chunks)
    test_material_budget()
    test_summarize_chunks(chunks)
    outline = test_build_outline(chunks)
    test_fallback_item_ts(chunks)
    test_export(chunks, outline)
    print(f"\n通过 {PASS} 项，失败 {FAILED} 项")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
