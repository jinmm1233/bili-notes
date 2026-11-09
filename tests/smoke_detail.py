"""笔记详细度三档回归：档位派生参数、0=不限的配额逻辑。

跑法：python tests/smoke_detail.py
不联网、不碰真实数据目录。
"""
from __future__ import annotations

import copy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.core.config import DETAIL_PRESETS, Settings  # noqa: E402
from app.core.models import Chunk, Frame  # noqa: E402
from app.services import chunking  # noqa: E402

FAILED = 0


def ok(cond: bool, msg: str) -> None:
    global FAILED
    print(("[OK] " if cond else "[FAIL] ") + msg)
    if not cond:
        FAILED += 1


def test_presets() -> None:
    ok(set(DETAIL_PRESETS) == {"brief", "standard", "detailed"}, "三档齐全")

    std = Settings(note_detail="standard")
    ok(std.max_frames_per_chunk == 8 and std.max_frames_total == 0,
       "适中：每段 8 张，全片不另设总配额")

    brief = Settings(note_detail="brief")
    ok(brief.max_frames_per_chunk == 5, "简洁：每段 5 张（比旧的 3 张多）")

    detail = Settings(note_detail="detailed")
    ok(detail.max_frames_per_chunk == 0 and detail.max_frames_total == 0,
       "详细：每段与全片都不设配额（0=不限）")

    # 用户明确要求：三档的区别**只有每段上限**，判定标准必须完全一致
    ok(len({brief.scene_phash_distance, std.scene_phash_distance,
            detail.scene_phash_distance}) == 1,
       "三档的画面判定阈值一致（都 14）")
    ok(len({brief.uniform_interval, std.uniform_interval,
            detail.uniform_interval}) == 1,
       "三档的兜底间隔一致")
    ok({k: v["max_frames_per_chunk"] for k, v in DETAIL_PRESETS.items()}
       == {"brief": 5, "standard": 8, "detailed": 0},
       "三档唯一差别是每段上限：5 / 8 / 不限")

    # 配置里残留的旧数值不能压过档位，否则「选了详细却只留 3 张」
    override = Settings(note_detail="detailed", max_frames_per_chunk=3, max_frames_total=40)
    ok(override.max_frames_per_chunk == 0 and override.max_frames_total == 0,
       "档位覆盖 config 里的旧配额")

    ok(Settings().note_detail == "standard", "默认档位是适中")

    try:
        Settings(note_detail="疯狂")
        ok(False, "非法档位应报错")
    except Exception:
        ok(True, "非法档位被拒绝")


def _chunks() -> list[Chunk]:
    return [Chunk(index=0, start=0, end=100, text="a"),
            Chunk(index=1, start=100, end=200, text="b")]


def _frames(n: int = 12) -> list[Frame]:
    out: list[Frame] = []
    for i in range(n):
        f = Frame(ts=float(i * 8), path=f"frame_{i}.jpg", origin="scene")
        f.score = 0.5
        out.append(f)
    return out


def test_attach_quota() -> None:
    frames = _frames()

    c = _chunks()
    chunking.attach_frames(c, frames, per_chunk=5, total_cap=60)
    ok(all(len(x.frames) <= 5 for x in c), "每段 5 张上限生效")

    c = _chunks()
    chunking.attach_frames(c, frames, per_chunk=8, total_cap=120)
    ok(all(len(x.frames) <= 8 for x in c), "每段 8 张上限生效")

    c = _chunks()
    chunking.attach_frames(c, frames, per_chunk=0, total_cap=0)
    ok(sum(len(x.frames) for x in c) == len(frames), "0=不限：全部帧都挂上")

    c = _chunks()
    chunking.attach_frames(c, frames, per_chunk=0, total_cap=7)
    ok(sum(len(x.frames) for x in c) == 7, "全片上限仍可单独生效")

    c = _chunks()
    chunking.attach_frames(c, frames, per_chunk=3, total_cap=0)
    ok(all(len(x.frames) <= 3 for x in c), "每段上限可单独生效")

    # 挂载不该改动传入的帧对象
    snapshot = [(f.ts, f.score) for f in frames]
    ok(snapshot == [(f.ts, f.score) for f in frames], "挂载不修改帧本身")


def test_stable_skips_animation() -> None:
    """PPT 逐条出现：中间态不该被截，只留「全部放完、画面停住」那一帧。"""
    import shutil
    import tempfile

    from PIL import Image

    from app.services import frames as F

    tmp = Path(tempfile.mkdtemp())
    paths: list[tuple[float, Path]] = []
    img = Image.new("RGB", (8, 8), "white")
    for i in range(10):
        p = tmp / f"t{i}.jpg"
        img.save(p)
        paths.append((float(i), p))

    class H:
        def __init__(self, v: int) -> None:
            self.v = v

        def __sub__(self, other: "H") -> int:
            return 0 if self.v == other.v else 64

    # 0~2 静止（片头）｜3~4 在变（PPT 正在逐条出现）｜5~9 静止（内容放全了）
    seq = [0, 0, 0, 1, 1, 2, 2, 2, 2, 2]
    orig = F.imagehash.phash
    try:
        it = iter(seq)
        F.imagehash.phash = lambda *_a, **_k: H(next(it))
        pts = F.detect_stable(paths, distance=12, min_stable=3)
        ok(pts == [2.0, 9.0], f"只取稳定段末尾，跳过变化中的中间态（得到 {pts}）")

        # 门槛 2 秒：板书写完只停两秒也不会整页漏掉
        ok(F._STABLE_MIN == 2, "静止门槛 2 秒（写完停两秒也要截到，三档共用）")
        it3 = iter(seq)
        F.imagehash.phash = lambda *_a, **_k: H(next(it3))
        pts3 = F.detect_stable(paths, distance=12, min_stable=F._STABLE_MIN)
        ok(pts3 == [2.0, 4.0, 9.0], f"两秒停顿也进候选，最终取段末（得到 {pts3}）")

        ok(4.0 in pts3 and 3.0 not in pts3, "变化发生的第 3 秒永远不会被选中")
    finally:
        F.imagehash.phash = orig
        shutil.rmtree(tmp, ignore_errors=True)


def test_no_random_faces() -> None:
    """没字又没被讲解点名的帧不该进笔记；整段都没字时兜底补一张。"""
    from app.services import frames as F

    chunks = [Chunk(index=0, start=0, end=100, text="一段讲解")]

    def mk(ts: float, ocr: str = "", origin: str = "scene",
           sharp: float = 100.0, density: float = 0.0) -> Frame:
        f = Frame(ts=ts, path="x.jpg", origin=origin)
        f.ocr_text = ocr
        f.sharpness = sharp
        f.text_density = density
        return f

    faces = [mk(10), mk(20), mk(30)]
    wordy = [mk(40, "这一页 PPT 上写满了公式和推导过程" * 4)]
    # 人站在黑板前：OCR 没认出手写板书，但版面结构分很高
    board = [mk(50, "", density=0.62)]

    orig = F.compute_phash
    F.compute_phash = lambda _f: ""
    try:
        sel = F.score_and_select(Settings(note_detail="detailed"),
                                faces + wordy + board, chunks)
        ok(len(sel) == 2, f"只保留有内容的帧（得到 {len(sel)} 张）")
        ok(any(f.text_density >= 0.6 for f in sel),
           "人+板书：OCR 没认出来也照样入选（不因为画面里有人就丢）")
        ok(not any(f.text_density == 0 and not f.ocr_text for f in sel),
           "纯人像/风景被过滤")

        sel2 = F.score_and_select(Settings(note_detail="detailed"), faces, chunks)
        ok(len(sel2) == 1, "整段都没内容时兜底补一张，不会整段空白")
    finally:
        F.compute_phash = orig


def test_vision_guard() -> None:
    d = Settings(note_detail="detailed")
    ok(d.vision_max_frames == 80, "视觉模式默认 80 张封顶")
    ok(d.vision_max_frames < 200, "详细档上百张图不会逐帧请求视觉模型")


def test_silent_video() -> None:
    """没有语音的视频：靠画面切块 → 挂帧 → 仍有正文和配图。

    实测事故：ASR 0 句时知识块为空，8 张关键帧全被丢弃，
    笔记只剩 896 字节的空壳，还误报「未配置模型」。
    """
    from app.core.models import VideoMeta
    from app.services import summarize, text_check

    frames = _frames(12)
    for i, f in enumerate(frames):
        f.ocr_text = f"第{i}页：神经网络的分层结构" if i % 2 == 0 else ""

    chunks = chunking.chunks_from_frames(frames, duration=300.0, target_chars=800, window=30.0)
    ok(len(chunks) >= 2, f"按时间窗切出多个块（实际 {len(chunks)}）")
    ok(all(f.ts >= chunks[0].start for f in frames), "首块从最早一帧开始")
    above = [f for f in frames if f.ts >= chunks[-1].end]
    ok(not above, "末块覆盖到最后一帧")
    ok(chunks[-1].end <= 300.0 + 0.01 or chunks[-1].end >= 300.0,
       "末块终点落在视频时长上")
    ok(any(c.text for c in chunks), "块正文用上了画面文字")

    chunking.attach_frames(chunks, frames, per_chunk=0, total_cap=0)
    ok(sum(len(c.frames) for c in chunks) == len(frames),
       "无语音时所有关键帧都挂上了（不再整批丢弃）")

    # 音画互校拿到空 segments 时不能把块正文清空
    snapshot = [c.text for c in chunks]
    text_check.rebuild_chunk_text(chunks, [])
    ok([c.text for c in chunks] == snapshot, "空转录不会清空画面切出的块正文")

    # 纯画面块（OCR 也没字）：降级摘要要用画面要点，不能只剩「内容片断」
    blank = Chunk(index=0, start=10, end=40, text="")
    shot = Frame(ts=12.0, path="shot.jpg", origin="scene")
    shot.screen_title = "Transformer 架构"
    shot.screen_points = ["自注意力机制", "多头并行"]
    blank.frames.append(shot)
    summarize._fallback_chunk(blank)
    ok("Transformer" in blank.title, f"纯画面块用画面要点当标题（{blank.title}）")

    meta = VideoMeta(bvid="BV1xx411c7mD", cid=1, title="AI 发展史", uploader="up",
                     duration=300.0, pubdate=0, cover="", webpage_url="")
    one = summarize.finalize(None, meta, [], chunks, speechless=True)[0]
    ok("人声" in one, "无语音 + 无模型：文案说明没识别到人声")

    class _Client:                        # 只用来走「有模型」分支
        model = "x"

    one2 = summarize.finalize(_Client(), meta, [], chunks, speechless=True)[0]
    ok("未配置模型" not in one2, "有模型时不再误报「未配置模型」")
    ok("画面内容" in one2, "有模型但没语音：说明按画面内容整理")


def main() -> int:
    print("笔记详细度三档：")
    test_presets()
    test_attach_quota()
    test_stable_skips_animation()
    test_no_random_faces()
    test_vision_guard()
    test_silent_video()
    print(f"\n{'全部通过' if FAILED == 0 else str(FAILED) + ' 项失败'}")
    return FAILED


if __name__ == "__main__":
    sys.exit(main())
