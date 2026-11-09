"""语种处理冒烟：检测逻辑、配置校验、外语成稿提示。

不加载真实模型（几百 MB 太慢），用鸭子类型的假模型验证调度逻辑：
真实模型的差异只在于它返回什么语种，分支本身与模型无关。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np
from types import SimpleNamespace

from app.core.config import Settings
from app.services import summarize, transcribe


class FakeModel:
    """按预设语种回答，并记录收到的参数。"""

    def __init__(self, langs):
        self.langs = list(langs)
        self.calls = []

    def transcribe(self, audio, **kw):
        self.calls.append(kw)
        lang = self.langs[len(self.calls) - 1] if len(self.calls) <= len(self.langs) else self.langs[-1]
        return [], SimpleNamespace(language=lang, language_probability=0.93)


def test_detect_votes() -> None:
    audio = np.zeros(16000 * 600, dtype=np.float32)   # 10 分钟静音
    model = FakeModel(["en", "en", "zh"])
    lang, prob = transcribe.detect_language(model, audio)
    assert lang == "en", lang
    assert prob > 0, prob
    assert len(model.calls) == 3, model.calls
    print(f"  投票：en 胜出，置信度 {prob:.2f}，采样 {len(model.calls)} 段")


def test_detect_skips_no_speech() -> None:
    """VAD 裁不出人声时 faster-whisper 返回 nn，不能算作语种。"""
    audio = np.zeros(16000 * 600, dtype=np.float32)
    model = FakeModel(["nn", "nn", "nn"])
    lang, prob = transcribe.detect_language(model, audio)
    assert lang == "zh", lang      # 兜底中文
    assert prob == 0.0
    print("  无人声：兜底 zh")


def test_detect_short_audio() -> None:
    audio = np.zeros(16000 * 20, dtype=np.float32)
    model = FakeModel(["ja"])
    lang, _ = transcribe.detect_language(model, audio)
    assert lang == "ja", lang       # 短音频只采一段
    assert len(model.calls) == 1
    print("  短音频：单段采样")


def test_resolve_precedence() -> None:
    audio = np.zeros(16000 * 600, dtype=np.float32)
    model = FakeModel(["en", "en", "en"])

    s = Settings(asr_language="ja", data_dir=ROOT / ".tmp_test", output_dir=ROOT / ".tmp_test" / "out")
    assert transcribe.resolve_language(s, model, audio) == "ja"
    print("  显式指定优先：ja")

    s = Settings(asr_language="auto", data_dir=ROOT / ".tmp_test", output_dir=ROOT / ".tmp_test" / "out")
    assert transcribe.resolve_language(s, model, audio) == "en"
    print("  auto：跟随检测结果 en")

    s = Settings(asr_language="auto", asr_model="base",
                 data_dir=ROOT / ".tmp_test", output_dir=ROOT / ".tmp_test" / "out")
    s.asr_model = "base.en"
    assert transcribe.resolve_language(s, model, audio) == "en"
    print("  .en 模型：强制 en（英文词表不接受 zh）")


def test_config_validator() -> None:
    base = dict(data_dir=ROOT / ".tmp_test", output_dir=ROOT / ".tmp_test" / "out")
    assert Settings(asr_language="auto", **base).asr_language == "auto"
    assert Settings(asr_language="EN", **base).asr_language == "en"
    try:
        Settings(asr_language="klingon", **base)
    except Exception as exc:
        assert "asr_language" in str(exc)
        print("  非法语种被拒绝：klingon")
    else:
        raise AssertionError("非法语种应被拒绝")


def test_foreign_and_sentences() -> None:
    en = "The researchers trained a model on 40 million images and found that scaling helps." * 6
    zh = "我们今天来讲一下这个视频里面提到的一些内容，以及它背后的原理是什么。" * 20
    assert summarize.is_foreign(en)
    assert not summarize.is_foreign(zh)
    # 中英混排的技术视频不该被判成外语
    mixed = "这个模型的 loss 曲线在 epoch 30 之后开始 overfit，我们来看一下具体的数据。" * 12
    assert not summarize.is_foreign(mixed)
    print("  语种判定：英文=外语 / 中文=否 / 中英混排=否")

    out = summarize._split_sentences("Hello world. Pi is 3.14 and this is U.S.A. code! Ok? 中文句子。继续")
    assert out[0] == "Hello world"
    assert any("3.14" in s and "U.S.A." in s for s in out), out
    print(f"  英文切句：{out}")


def main() -> int:
    print("== 语种处理冒烟 ==")
    test_detect_votes()
    test_detect_skips_no_speech()
    test_detect_short_audio()
    test_resolve_precedence()
    test_config_validator()
    test_foreign_and_sentences()
    print("== 全部通过 ==")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
