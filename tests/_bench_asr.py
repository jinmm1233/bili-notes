"""实测 tiny 在真实 17 分钟音频上的 CPU 转写速度与质量。

结论用于确定默认档位：如果 tiny 能在几分钟内跑完且中文可读，
就没必要让用户等 484MB 的 small 模型。
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["HF_HUB_DISABLE_XET"] = "1"

from faster_whisper import WhisperModel  # noqa: E402

from app.services.transcribe import decode_audio  # noqa: E402

AUDIO = Path("C:/Users/86159/AppData/Roaming/BiliNotes/cache/de7e7663c178/audio.m4a")
MODELS = Path("C:/Users/86159/AppData/Roaming/BiliNotes/models")


def run(model_dir: Path, label: str, beam: int = 1) -> None:
    if not (model_dir / "model.bin").is_file():
        print(f"{label}: 模型不存在 {model_dir}")
        return

    t0 = time.time()
    audio = decode_audio(str(AUDIO))
    decode_s = time.time() - t0
    audio_s = len(audio) / 16000.0

    t1 = time.time()
    model = WhisperModel(str(model_dir), device="cpu", compute_type="int8", local_files_only=True)
    load_s = time.time() - t1

    t2 = time.time()
    segments, info = model.transcribe(
        audio,
        language="zh",
        beam_size=beam,
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 500, "speech_pad_ms": 200},
        condition_on_previous_text=False,
    )

    texts = []
    for seg in segments:
        texts.append((float(seg.start), (seg.text or "").strip()))

    elapsed = time.time() - t2
    chars = sum(len(t) for _, t in texts)
    print(f"\n=== {label} (beam={beam}) ===")
    print(f"解码 {decode_s:.1f}s  加载 {load_s:.1f}s  转写 {elapsed:.1f}s")
    print(f"音频 {audio_s:.0f}s  实时倍率 {elapsed / max(audio_s, 1):.2f}x  {len(texts)} 段 / {chars} 字")
    for ts, text in texts[:8]:
        print(f"  [{ts:6.1f}] {text[:44]}")


if __name__ == "__main__":
    print(f"音频 {AUDIO.stat().st_size / 1048576:.1f}MB")
    target = sys.argv[1] if len(sys.argv) > 1 else "fw-tiny"
    label = sys.argv[2] if len(sys.argv) > 2 else "tiny"
    run(MODELS / target, label)
