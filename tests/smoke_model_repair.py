"""模型文件残缺的回归测试（`python tests/smoke_model_repair.py`）。

背景：0.2.10 只要看到 model.bin + config.json 就认为装好了。下载中途退出会
留下「有 model.bin、缺 vocabulary.txt」的半成品，之后每次转写都报
`Cannot load the vocabulary from the model directory`（新机器实测）。

这里守三件事：
  1. 缺文件的目录不算装好，只补缺的那几个；
  2. 齐全的目录不联网、不重复下载；
  3. 内容坏掉（下成网页 / 体积太小）能被识别出来；
  4. 加载失败时能把旧目录挪走重建。
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


def body_for(name: str) -> bytes:
    if name in ("config.json", "tokenizer.json"):
        return b'{"x":"' + b"y" * transcribe._MIN_BYTES[name] + b'"}'
    if name == "model.bin":
        return b"\0" * transcribe._MIN_BYTES["model.bin"]
    return b"token\n" * (transcribe._MIN_BYTES["vocabulary.txt"] // 6 + 1)


def write_file(path: Path, name: str) -> None:
    path.write_bytes(body_for(name))


def install_fake(record: list[str]):
    """假下载：记录被请求的文件名，按标准体积写盘。"""

    def fake(url, dest, on_chunk=None, cancel=None, timeout=180.0):
        name = url.rsplit("/", 1)[-1]
        record.append(name)
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        Path(dest).write_bytes(body_for(name))
        return Path(dest).stat().st_size

    return fake


real_download = transcribe._http_download
try:
    # ---- 1. 半成品目录：只补缺的文件 ----
    with tempfile.TemporaryDirectory() as tmp:
        s = Settings(data_dir=Path(tmp), asr_model="tiny")
        target = transcribe._model_dir(s, "tiny")
        target.mkdir(parents=True)
        write_file(target / "config.json", "config.json")
        write_file(target / "model.bin", "model.bin")   # 上次下到一半，词表没下

        problems = transcribe._model_problems(target)
        ok(set(problems) == {"tokenizer.json", "vocabulary.txt"},
           f"半成品目录被识别为缺词表（{sorted(problems)}）")

        calls: list[str] = []
        transcribe._http_download = install_fake(calls)
        got = transcribe.ensure_model(s)
        transcribe._http_download = real_download
        ok(got == target, "返回同一模型目录")
        ok(calls == ["tokenizer.json", "vocabulary.txt"],
           f"只补下缺失文件（实际 {calls}）")
        ok(not transcribe._model_problems(target), "补齐后文件检查通过")
        ok((target / "model.bin").stat().st_size == transcribe._MIN_BYTES["model.bin"],
           "已完整的 model.bin 没被重下覆盖")

    # ---- 2. 齐全目录：完全不联网 ----
    with tempfile.TemporaryDirectory() as tmp:
        s = Settings(data_dir=Path(tmp), asr_model="tiny")
        target = transcribe._model_dir(s, "tiny")
        target.mkdir(parents=True)
        for name in transcribe._REQUIRED_FILES:
            write_file(target / name, name)

        calls = []
        transcribe._http_download = install_fake(calls)
        got = transcribe.ensure_model(s, on_progress=lambda *a: None)
        transcribe._http_download = real_download
        ok(got == target and calls == [], "文件齐全时直接返回，不发起任何下载")

    # ---- 3. 坏文件识别 ----
    with tempfile.TemporaryDirectory() as tmp:
        s = Settings(data_dir=Path(tmp), asr_model="tiny")
        target = transcribe._model_dir(s, "tiny")
        target.mkdir(parents=True)
        for name in transcribe._REQUIRED_FILES:
            write_file(target / name, name)
        (target / "vocabulary.txt").write_bytes(b"<!DOCTYPE html><html>login</html>")
        (target / "config.json").write_bytes(b"not json")
        (target / "tokenizer.json").write_bytes(b"{broken" + b" " * 10)
        problems = transcribe._model_problems(target)
        ok("内容像网页" in problems.get("vocabulary.txt", ""), "识别出下成网页的词表")
        ok("内容不是 JSON" in problems.get("config.json", ""), "识别出损坏的 config.json")
        ok("没下完" in problems.get("tokenizer.json", ""), "识别出体积不足的 tokenizer.json")

    # ---- 4. 加载失败时重建目录 ----
    with tempfile.TemporaryDirectory() as tmp:
        s = Settings(data_dir=Path(tmp), asr_model="tiny")
        target = transcribe._model_dir(s, "tiny")
        target.mkdir(parents=True)
        (target / "model.bin").write_bytes(b"garbage")

        real_ensure = transcribe.ensure_model

        def fake_ensure(settings, on_progress=None, cancel=None):
            fresh = transcribe._model_dir(settings, settings.asr_model)
            fresh.mkdir(parents=True, exist_ok=True)
            for name in transcribe._REQUIRED_FILES:
                write_file(fresh / name, name)
            return fresh

        transcribe.ensure_model = fake_ensure
        try:
            fresh = transcribe._rebuild_model(s, target, None, None)
        finally:
            transcribe.ensure_model = real_ensure
        ok(fresh is not None and fresh == target, "重建后返回原路径")
        ok((target / "vocabulary.txt").is_file(), "重建目录里词表齐了")
        ok(not list(target.parent.glob("fw-tiny.bad-*")), "旧目录已清理")
finally:
    transcribe._http_download = real_download

print()
if FAILED:
    print(f"{FAILED} 项未通过：模型残缺目录的修复逻辑有问题。")
    sys.exit(1)
print("全部通过：残缺目录只补缺文件、齐全目录不联网、坏文件可识别、加载失败可重建。")
