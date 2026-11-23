"""语音模型下载源回归（`python tests/smoke_model_source.py`）。

守两件事：
  1. 默认先走 ModelScope（国内实测 ~5MB/s），hf-mirror 只作兜底；
  2. 第一个源连不上时，会自动换源并把文件下完，而不是直接报"下载失败"。
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.core.config import Settings  # noqa: E402
from app.services import transcribe  # noqa: E402

FAILED = 0


def ok(cond: bool, msg: str) -> None:
    global FAILED
    print(("[OK] " if cond else "[FAIL] ") + msg)
    if not cond:
        FAILED += 1


# ---- 1. 源顺序 ----
with tempfile.TemporaryDirectory() as tmp:
    s = Settings(data_dir=Path(tmp), asr_model="base")
    sources = transcribe._model_sources(s, "base")
    ok(sources[0].startswith("https://modelscope.cn/models/pengzhendong/faster-whisper-base/"),
       f"首选 ModelScope（{sources[0][:60]}…）")
    ok(any("hf-mirror.com/Systran/faster-whisper-base" in u for u in sources[1:]),
       "hf-mirror 作为兜底仍在列表里")

    s2 = Settings(data_dir=Path(tmp), hf_endpoint="https://my-mirror.example.com/")
    src2 = transcribe._model_sources(s2, "tiny")
    ok("https://my-mirror.example.com/Systran/faster-whisper-tiny/resolve/main" in src2,
       "自建镜像（hf_endpoint）仍然生效")

# ---- 2. 第一个源失败时自动换源 ----
def fake_download(url, dest, on_chunk=None, cancel=None, timeout=180.0):
    if "modelscope.cn" in url:
        raise ConnectionError("modelscope 连不上")
    name = url.rsplit("/", 1)[-1]
    if name in ("config.json", "tokenizer.json"):
        body = b'{"x":"' + b"y" * transcribe._MIN_BYTES[name] + b'"}'
    elif name == "model.bin":
        body = b"\0" * transcribe._MIN_BYTES["model.bin"]
    else:                                 # vocabulary.txt
        body = b"token\n" * (transcribe._MIN_BYTES["vocabulary.txt"] // 6 + 1)
    Path(dest).parent.mkdir(parents=True, exist_ok=True)
    Path(dest).write_bytes(body)
    if on_chunk:
        on_chunk(len(body))
    return len(body)


with tempfile.TemporaryDirectory() as tmp:
    s = Settings(data_dir=Path(tmp), asr_model="base")
    real = transcribe._http_download
    transcribe._http_download = fake_download
    try:
        model_dir = transcribe.ensure_model(s)
    finally:
        transcribe._http_download = real

    ok((model_dir / "model.bin").is_file(), "换源后 model.bin 下载完成")
    ok((model_dir / "config.json").is_file(), "换源后 config.json 下载完成")
    ok((model_dir / "tokenizer.json").is_file(), "换源后 tokenizer.json 下载完成")
    ok((model_dir / "vocabulary.txt").is_file(), "换源后 vocabulary.txt 下载完成（CT2 必需）")
    ok(not (model_dir / "preprocessor_config.json").exists(), "不请求推理用不到的文件")
    ok(model_dir.name == "fw-base", f"模型目录名正确（{model_dir.name}）")

# ---- 3. 全部源都失败时，错误信息要说明换过几个源 ----
def all_fail(url, dest, on_chunk=None, cancel=None, timeout=180.0):
    raise ConnectionError("network down")


with tempfile.TemporaryDirectory() as tmp:
    s = Settings(data_dir=Path(tmp), asr_model="tiny")
    real = transcribe._http_download
    transcribe._http_download = all_fail
    try:
        try:
            transcribe.ensure_model(s)
        except Exception as exc:  # noqa: BLE001
            msg = str(exc)
            ok("下载失败" in msg and "已自动换过" in msg, "失败信息说明已自动换源")
            ok("tiny" in msg, "失败信息带上档位名")
        else:
            ok(False, "全部源失败时应当抛错")
    finally:
        transcribe._http_download = real

print()
if FAILED:
    print(f"{FAILED} 项未通过：模型下载源的兜底逻辑有问题。")
    sys.exit(1)
print("全部通过：ModelScope 优先、失败自动换源、必需文件齐全。")
