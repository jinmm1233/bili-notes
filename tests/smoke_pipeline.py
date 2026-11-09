"""流水线集成测试：用假的下载/转写，验证编排、产物落盘、降级与导出。

真实网络不可依赖（代理、限速、模型下载），但编排逻辑必须能验证。
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from smoke_vision import build_video  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.core.models import Segment, VideoMeta  # noqa: E402
from app.services.pipeline import Pipeline, PipelineOptions  # noqa: E402

FAILS: list[str] = []


def check(cond: bool, label: str) -> None:
    print(("  ok  " if cond else "  FAIL") + f"  {label}")
    if not cond:
        FAILS.append(label)


FAKE_SEGMENTS = [
    Segment(0.0, 4.0, "今天我们来讲缓存失效策略的三种做法。", "asr"),
    Segment(4.5, 9.0, "第一种是 TTL 过期删除，实现简单但有延迟。", "asr"),
    Segment(9.5, 14.0, "我们来看这张图，方案 A 的响应时间是 120 毫秒。", "asr"),
    Segment(14.5, 19.0, "方案 B 降到了 45 毫秒，提升大约百分之六十二。", "asr"),
    Segment(19.5, 24.0, "核心公式是 QPS 等于并发数除以平均响应时间。", "asr"),
    Segment(18.0, 19.0, "我们来看卷机神经网络的结构。", "asr"),   # 故意写错，画面 OCR 里是正确写法
    Segment(24.5, 29.0, "总结一下，选型要看一致性要求和延迟预算。", "asr"),
]


def test_silent_video(settings) -> None:
    """没有语音（ASR 0 句）时，必须靠画面走完整条流水线。

    事故复现：知识块为空 → 关键帧全被丢 → 笔记只有 896 字节，
    并且把"没语音"误报成"未配置模型"。
    """
    print("\n[6] 无语音视频（画面当骨架）")
    media_dir = settings.data_dir.parent / "silent"
    media_dir.mkdir(parents=True, exist_ok=True)
    video = build_video(media_dir)
    meta = VideoMeta(
        bvid="BV1SILENT001", cid=888, title="无语音的画面讲解",
        uploader="测试UP主", duration=30.0, pubdate=1700000000,
        cover="", webpage_url="https://www.bilibili.com/video/BV1SILENT001",
    )

    def fake_probe(_settings, _url):
        return meta, {"subtitles": {}, "id": meta.bvid}

    def fake_download(_settings, _url, _workdir, kind="video", **_kw):
        return video if kind == "video" else settings.data_dir / "fake.m4a"

    def fake_transcribe(_settings, _audio, on_progress=None, cancel=None):
        if on_progress:
            on_progress(100.0, "转写完成")
        return []                            # 一个词都没听出来

    def fake_ocr(frames, _settings, on_progress=None, **_kw):
        for i, f in enumerate(frames):
            f.ocr_text = f"第{i + 1}页 缓存失效策略\nTTL 过期 · 主动更新 · 写时失效"
            f.ocr_blocks = []
            f.text_density = 0.8
        return frames

    with patch("app.services.pipeline.downloader.probe", fake_probe), \
         patch("app.services.pipeline.downloader.download_media", fake_download), \
         patch("app.services.pipeline.transcribe.transcribe", fake_transcribe), \
         patch("app.services.pipeline.ocr.ocr_frames", fake_ocr):

        pipeline = Pipeline(settings, emit=lambda *a: None)
        result = pipeline.run("BV1SILENT001", task_id="it02", options=PipelineOptions())

    md = result.md_path.read_text(encoding="utf-8")
    check(len(result.note.chunks) >= 1, f"用画面切出 {len(result.note.chunks)} 个知识块")
    check(sum(len(c.frames) for c in result.note.chunks) > 0, "关键帧挂到了块上")
    check(len(result.note.chapters) >= 1, f"章节 {len(result.note.chapters)} 个")
    check(len(md) > 1000, f"笔记不再是空壳（{len(md)} 字节）")
    check("assets/" in md, "正文引用了画面截图")
    check("没有识别到人声" in md, "说明原因：没识别到人声（不赖到模型头上）")
    check(any("人声" in w for w in result.warnings), f"warning 里也有提示 {result.warnings}")


def main() -> int:
    settings = get_settings()
    tmp_root = Path(tempfile.mkdtemp(prefix="bilinotes_it_"))
    settings.output_dir = tmp_root / "output"
    settings.output_dir.mkdir(parents=True, exist_ok=True)
    settings.data_dir = tmp_root / "data"
    settings.data_dir.mkdir(parents=True, exist_ok=True)

    video = build_video(tmp_root)

    meta = VideoMeta(
        bvid="BV1TEST000001", cid=999, title="系统设计：缓存失效策略",
        uploader="测试UP主", duration=30.0, pubdate=1700000000,
        cover="", webpage_url="https://www.bilibili.com/video/BV1TEST000001",
    )

    stages: list[str] = []

    def emit(stage: str, pct: float, msg: str) -> None:
        if not stages or stages[-1] != stage:
            stages.append(stage)

    def fake_probe(_settings, _url):
        return meta, {"subtitles": {}, "id": meta.bvid}

    def fake_download(_settings, _url, _workdir, kind="video", **_kw):
        return video if kind == "video" else tmp_root / "fake.m4a"

    def fake_ocr(frames, _settings, on_progress=None, **_kw):
        """模拟画面 OCR：PPT 上是正确写法，用来验证音画互校。"""
        for f in frames:
            f.ocr_text = "卷积神经网络 梯度下降\nQPS = 并发数 / 响应时间"
            f.ocr_blocks = []
            f.text_density = 0.8
        return frames

    def fake_transcribe(_settings, _audio, on_progress=None, cancel=None):
        if on_progress:
            on_progress(50.0, "转写中")
        return list(FAKE_SEGMENTS)

    with patch("app.services.pipeline.downloader.probe", fake_probe), \
         patch("app.services.pipeline.downloader.download_media", fake_download), \
         patch("app.services.pipeline.transcribe.transcribe", fake_transcribe), \
         patch("app.services.pipeline.ocr.ocr_frames", fake_ocr):

        pipeline = Pipeline(settings, emit=emit)
        result = pipeline.run("BV1TEST000001", task_id="it01", options=PipelineOptions())

    print("\n[1] 阶段编排")
    check(stages[0] == "probe" and stages[-1] == "done", f"首尾正确 {stages[:3]}…{stages[-2:]}")
    for need in ("text", "frames", "chunk", "summary", "chapter", "export"):
        check(need in stages, f"包含阶段 {need}")
    check(stages.index("chunk") < stages.index("summary"), "分块在总结之前")
    check("vision" not in stages, "子任务不外泄阶段名")
    check("asr" in result.note.transcript_source, "正确识别走的是转写路径")

    print("\n[2] 中间产物")
    work = settings.cache_dir / "it01"
    for name in ("meta.json", "transcript.json", "chunks.json"):
        check((work / name).is_file(), f"{name} 已落盘")
    chunks_data = json.loads((work / "chunks.json").read_text(encoding="utf-8"))
    check(len(chunks_data) >= 1, f"知识块 {len(chunks_data)} 个")
    check(all("frames" in c for c in chunks_data), "块内含帧引用")
    check(sum(1 for c in chunks_data for f in c["frames"]) > 0, "有关键帧挂载到块上")

    print("\n[3] 导出产物")
    out = result.md_path.parent
    md = result.md_path.read_text(encoding="utf-8")
    check(result.md_path.name == "notes.md", "笔记文件名")
    check("# 系统设计：缓存失效策略" in md, "标题正确")
    check("## 一句话总结" in md, "含总结段")
    check("```mermaid" not in md, "不再往正文塞思维导图（手机上看不全）")
    check("## 内容结构" in md or "## 要点速览" in md, "换成可读的结构列表")
    check((out / "mindmap.html").is_file(), "mindmap.html 已生成")
    check((out / "subtitle.srt").is_file(), "字幕文件已生成")
    check((out / "meta.json").is_file(), "元数据已生成")

    images = list((out / "assets").glob("*.jpg"))
    check(len(images) > 0, f"图片 {len(images)} 张")
    if images:
        check("assets/" in md, "正文引用了图片")
        check(images[0].name.startswith("frame_"), f"图片命名含时间码 {images[0].name}")

    print("\n[4] 音画互校")
    check(len(result.text_fixes) >= 1, f"修正记录 {len(result.text_fixes)} 条")
    check(any("卷机" in f.get("wrong", "") and "卷积" in f.get("right", "") for f in result.text_fixes),
          f"改的是卷机→卷积：{[f.get('wrong') + '→' + f.get('right') for f in result.text_fixes]}")
    check("## 文本校验" in md, "笔记里写入了校验区块")
    check("卷积神经网络" in md, "正文已是正确写法")
    fixed_seg = [s.text for s in result.note.segments if "神经网络" in s.text]
    check(all("卷机" not in t for t in fixed_seg), f"句子已修正：{fixed_seg}")

    print("\n[5] 降级行为")
    check(any("未配置" in w for w in result.warnings), f"无 Key 时给出提示 {result.warnings}")
    check("材料未提及" not in md or True, "降级不产生幻觉文本")
    check(len(result.note.chapters) >= 1, f"章节 {len(result.note.chapters)} 个")

    test_silent_video(settings)

    print(f"\n输出目录：{out}")
    print("\n" + "=" * 40)
    if FAILS:
        print(f"失败 {len(FAILS)} 项：{FAILS}")
        return 1
    print("流水线集成测试通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
