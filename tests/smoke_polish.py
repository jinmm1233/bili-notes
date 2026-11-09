"""转录润色冒烟：智能标点 + 外语译中双语（不依赖真实 LLM Key）。"""
import sys

sys.path.insert(0, ".")

from app.core.config import Settings
from app.core.models import Segment, VideoMeta, VideoNote
from app.services import transcript_polish as tp
from app.services.exporter import render_markdown
from app.services.subtitle import to_srt


class FakeClient:
    """按编号把每行补个句号、并给一个伪译文，验证回写逻辑。"""

    def __init__(self):
        self.calls = 0

    def complete_json(self, system, user, max_tokens=4096):
        self.calls += 1
        lines_in = [ln for ln in user.splitlines() if ln and ln[0].isdigit()]
        out = []
        for ln in lines_in:
            orig = ln.split(". ", 1)[1] if ". " in ln else ln
            out.append({"src": orig.rstrip() + "。", "dst": "译：" + orig.rstrip()})
        return {"lines": out}


def test_foreign_punct_and_translate():
    settings = Settings()
    settings.polish_punctuation = True
    settings.bilingual_transcript = True
    # is_foreign 要求拉丁字母 >= 200 才判定外语，这里给足长度
    eng = ("hello world this is a test of the transcription pipeline and "
           "we want to verify that punctuation is added correctly and a "
           "chinese translation is produced for each foreign sentence. ")
    segs = [
        Segment(0.0, 2.0, eng.strip()),
        Segment(3.0, 5.0, "another english sentence that continues the demonstration."),
    ]
    client = FakeClient()
    out = tp.polish_segments(segs, settings, client)
    assert out[0].text.endswith("。"), out[0].text
    assert out[0].translation.startswith("译："), out[0].translation
    assert client.calls == 1
    print("[OK] 外语：补标点 + 加译文")


def test_chinese_only_no_translate():
    settings = Settings()
    settings.polish_punctuation = True
    settings.bilingual_transcript = True
    segs = [
        Segment(0.0, 2.0, "这是中文测试没有标点"),
        Segment(3.0, 5.0, "另一句中文内容"),
    ]
    client = FakeClient()
    out = tp.polish_segments(segs, settings, client)
    assert out[0].translation == "", out[0].translation
    assert out[0].text.endswith("。")
    print("[OK] 中文：只补标点、不译中")


def test_no_client_returns_same():
    settings = Settings()
    segs = [Segment(0.0, 2.0, "hello world")]
    out = tp.polish_segments(segs, settings, None)
    assert out is segs
    print("[OK] 无 Key：原样返回")


def test_exporter_bilingual_and_srt():
    segs = [
        Segment(0.0, 2.0, "hello。", translation="你好。"),
        Segment(3.0, 5.0, "world。", translation="世界。"),
    ]
    meta = VideoMeta(
        bvid="BV1", cid=1, title="t", uploader="u", duration=10,
        pubdate=0, cover="", webpage_url="http://x",
    )
    note = VideoNote(meta=meta, segments=segs)
    md = render_markdown(note, {}, {})
    assert "完整转录" in md
    assert "译：" not in md  # 译文直接写在 `中` 行，不是"译："
    assert "`中` 你好。" in md, "译文行未渲染"
    srt = to_srt(segs)
    assert "你好。" in srt and "世界。" in srt
    print("[OK] 导出：转录双语 + SRT 双语")


if __name__ == "__main__":
    test_foreign_punct_and_translate()
    test_chinese_only_no_translate()
    test_no_client_returns_same()
    test_exporter_bilingual_and_srt()
    print("\nALL PASS")
