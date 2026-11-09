"""画面要点冒烟：验证关键帧提炼要点 + 导出渲染（不依赖真实 LLM Key）。

背景（用户反馈迭代）：
- v1 平铺 OCR → 太乱，弃用；
- v2 画面解读（一段话复述）→ 正文已讲过，没用，弃用；
- v3 画面要点（题目 + 分点，讲解补全）→ 同时回喂总结层。
"""
import re
import sys

sys.path.insert(0, ".")

from app.core.config import Settings
from app.core.models import Chapter, Chunk, Frame, Segment, VideoMeta, VideoNote
from app.services import frame_caption as fc
from app.services.exporter import _render_chapter, _render_shot


class FakeClient:
    def __init__(self):
        self.calls = 0

    def complete_json(self, system, user, max_tokens=1024):
        self.calls += 1
        n = len(re.findall(r"画面\d+（", user))
        caps = [
            {"index": i + 1, "title": f"PPT题目{i + 1}",
             "points": [f"要点A{i + 1}", f"要点B{i + 1}"]}
            for i in range(n)
        ]
        return {"captions": caps}


def build():
    settings = Settings()
    settings.frame_captions = True
    f1 = Frame(ts=12.0, path="a.jpg", ocr_text="|策略|命中率|\n|---|---|\n|LRU|80%|")
    f2 = Frame(ts=30.0, path="b.jpg", ocr_text="")  # 无文字画面
    chunk = Chunk(
        index=0, start=10.0, end=40.0,
        text="这一段讲解了缓存失效的三种策略，分别是 LRU、LFU 和 FIFO。",
        frames=[f1, f2],
    )
    return settings, chunk, f1, f2


def test_screen_notes_generation():
    settings, chunk, f1, f2 = build()
    client = FakeClient()
    n = fc.caption_frames(client, [chunk], settings)
    assert n == 2, n
    assert f1.screen_title == "PPT题目1"
    assert f1.screen_points == ["要点A1", "要点B1"]
    assert f1.caption, "caption 摘要应回填（旧逻辑兼容）"
    assert client.calls == 1, "应按块批量调用，而不是每张图一次"
    print("[OK] 按块提炼画面要点（title + points）")


def test_screen_lines_for_outline():
    settings, chunk, f1, f2 = build()
    fc.caption_frames(FakeClient(), [chunk], settings)
    lines = fc.chunk_screen_lines(chunk, with_ts=True)
    assert lines == ["[00:12] PPT题目1；要点A1；要点B1", "[00:30] PPT题目2；要点A2；要点B2"], lines
    flat = fc.chunk_screen_lines(chunk)
    assert flat and not flat[0].startswith("["), "不带时间码时应去掉行首 [ts]"
    print("[OK] chunk_screen_lines 供大纲材料使用（去重 + 可带时间码）")


def test_export_screen_block():
    settings, chunk, f1, f2 = build()
    fc.caption_frames(FakeClient(), [chunk], settings)
    meta = VideoMeta(
        bvid="BV1", cid=1, title="t", uploader="u", duration=10,
        pubdate=0, cover="", webpage_url="http://x",
    )
    note = VideoNote(meta=meta, segments=[Segment(0, 2, "x")], chunks=[chunk])
    chapter = Chapter(index=0, title="章", start=10, end=40, chunk_indexes=[0])
    image_map = {id(f1): "assets/a.jpg", id(f2): "assets/b.jpg"}
    md = "\n".join(_render_chapter(chapter, note, image_map, with_bullets=True))

    assert "画面要点" in md
    assert "**PPT题目1**" in md
    assert "- 要点A1" in md
    # 旧版"画面解读"不再出现；OCR 原文也不出现在正文
    assert "画面解读" not in md
    assert "LRU" not in md, "OCR 原文不应出现在笔记正文"
    print("[OK] 导出：图片 + 画面要点块（标题加粗 + 分点），无 OCR 平铺")


def test_empty_points_renders_nothing():
    """画面确实没文字时模型返回空 → 不渲染要点块，也不编造。"""
    settings = Settings()
    settings.frame_captions = True

    class EmptyClient:
        def complete_json(self, system, user, *, max_tokens=1024):
            return {"captions": [{"index": 1, "title": "", "points": []}]}

    f = Frame(ts=5.0, path="a.jpg", ocr_text="")
    chunk = Chunk(index=0, start=0, end=10, text="纯口播", frames=[f])
    fc.caption_frames(EmptyClient(), [chunk], settings)
    assert not f.screen_title and not f.screen_points
    meta = VideoMeta(bvid="BV1", cid=1, title="t", uploader="u", duration=10,
                     pubdate=0, cover="", webpage_url="http://x")
    note = VideoNote(meta=meta, chunks=[chunk])
    lines = _render_shot(note, (f, "assets/a.jpg"), with_caption=True)
    assert _screen_block_empty(lines), f"不应渲染要点块：{lines}"
    print("[OK] 无可取文字的画面不硬凑要点块")


def _screen_block_empty(lines: list[str]) -> bool:
    return not any("画面要点" in ln for ln in lines)


def test_no_frames_returns_zero():
    settings = Settings()
    settings.frame_captions = True
    assert fc.caption_frames(FakeClient(), [Chunk(index=1, start=0, end=1, text="x")], settings) == 0
    print("[OK] 无帧返回 0")


def test_disabled_skips():
    settings = Settings()
    settings.frame_captions = False
    _, chunk, f1, f2 = build()
    assert fc.caption_frames(FakeClient(), [chunk], settings) == 0
    assert not f1.caption and not f1.screen_points
    print("[OK] 关闭开关时跳过")


def test_vision_mode_and_fallback():
    """视觉模式：图片真的发给模型；失败的单帧退回文字模式；没配看图模型走文字模式。"""
    from app.core.config import Settings
    from app.core.models import Chunk, Frame

    settings = Settings()
    settings.frame_captions = True

    class FakeVision:
        model = "fake-vl"
        def __init__(self):
            self.calls = []
            self.fail_once = True
        def complete_vision(self, system, user, images, *, max_tokens=1024):
            self.calls.append(images)
            if self.fail_once:
                self.fail_once = False
                raise RuntimeError("网络抖动")
            import base64
            assert images and images[0][0] and images[0][1] == "image/jpeg"
            return ('{"captions":[{"index":1,"title":"矩阵连乘",'
                    '"points":["给定n个矩阵","求最优加括号次序"]}]}')
        def complete_json(self, system, user, *, max_tokens=2048):
            return {"captions": [{"index": 1, "title": "兜底题目", "points": ["（文字兜底）要点"]}]}

    class FakeText:
        def complete_json(self, system, user, *, max_tokens=2048):
            return {"captions": [{"index": 1, "title": "文字模式题目", "points": ["文字模式要点"]}]}

    # 造一张真图，供压缩函数读取
    from PIL import Image
    import tempfile, pathlib
    tmp = pathlib.Path(tempfile.mkdtemp())
    img_path = tmp / "f.jpg"
    Image.new("RGB", (640, 360), (200, 60, 60)).save(img_path, "JPEG")

    chunk = Chunk(index=0, start=0, end=30, text="这节课讲逆矩阵的求法",
                  frames=[Frame(ts=5.0, path=str(img_path), origin="scene")])

    # 1) 视觉模式：第一帧失败自动兜底
    fake = FakeVision()
    n = fc.caption_frames(FakeText(), [chunk], settings, vision_client=fake)
    assert len(fake.calls) == 1, "应发起一次看图请求"
    assert chunk.frames[0].screen_title == "兜底题目", "失败的帧要退回文字模式"
    assert n == 1

    # 2) 没配看图模型：走文字模式
    chunk2 = Chunk(index=1, start=30, end=60, text="继续讲例题",
                   frames=[Frame(ts=35.0, path=str(img_path), origin="scene")])
    fc.caption_frames(FakeText(), [chunk2], settings, vision_client=None)
    assert chunk2.frames[0].screen_points == ["文字模式要点"]

    # 3) 开关关闭：直接跳过
    settings.frame_captions = False
    chunk3 = Chunk(index=2, start=60, end=90, text="x",
                   frames=[Frame(ts=65.0, path=str(img_path), origin="scene")])
    assert fc.caption_frames(FakeText(), [chunk3], settings) == 0
    assert not chunk3.frames[0].screen_points
    print("  视觉模式：图片编码上传 / 失败兜底 / 文字模式 / 开关关闭 均正确")


if __name__ == "__main__":
    test_screen_notes_generation()
    test_screen_lines_for_outline()
    test_export_screen_block()
    test_empty_points_renders_nothing()
    test_no_frames_returns_zero()
    test_disabled_skips()
    test_vision_mode_and_fallback()
    print("\nALL PASS")
