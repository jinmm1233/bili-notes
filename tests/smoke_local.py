"""本地视频链路：路径校验、元数据、抽音频、同名字幕、导出链接。

不依赖网络和模型：视频用 ffmpeg 现场造一个 3 秒的测试片，
字幕与笔记对象都手工构造，只验证「本地来源」这条新路。
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.ffmpeg import find_binary  # noqa: E402
from app.core.models import Chapter, Chunk, Frame, Segment, VideoMeta, VideoNote  # noqa: E402
from app.services import exporter, local_source  # noqa: E402

FAILS: list[str] = []


def check(cond: bool, label: str) -> None:
    print(("  ok  " if cond else "  FAIL") + f"  {label}")
    if not cond:
        FAILS.append(label)


def make_video(folder: Path) -> Path:
    ff = find_binary("ffmpeg")
    video = folder / "第01讲 测试 视频.mp4"
    subprocess.run(
        [
            ff, "-hide_banner", "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc=size=320x180:rate=10:duration=3",
            "-f", "lavfi", "-i", "anullsrc=channel_layout=mono:sample_rate=16000",
            "-t", "3", "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "32k", "-shortest", str(video),
        ],
        check=True,
    )
    (folder / "第01讲 测试 视频.srt").write_text(
        "1\n00:00:00,100 --> 00:00:02,900\n第一句测试字幕。\n", encoding="utf-8")
    return video


def test_validate(video: Path, tmp: Path) -> None:
    print("\n[1] 路径校验")
    txt = tmp / "说明.txt"
    txt.write_text("not a video", encoding="utf-8")
    for bad, label in [
        ("", "空路径"),
        ("relative.mp4", "相对路径"),
        (str(tmp / "nope.mp4"), "不存在的文件"),
        (str(tmp), "文件夹"),
        (str(txt), "不支持的格式"),
    ]:
        try:
            local_source.validate_video(bad)
            check(False, f"{label} 被错误接受")
        except Exception as exc:
            check(True, f"{label} 被拒绝：{getattr(exc, 'message', str(exc))[:24]}")

    ok = local_source.validate_video(str(video))
    check(ok == video.resolve(), "合法视频通过校验")


def test_url(video: Path) -> None:
    print("\n[2] file:// 地址")
    url = local_source.to_url(video)
    check(url.startswith("file:///") and "%20" in url, "中文与空格被正确编码")
    check(local_source.is_local_url(url), "识别为本地地址")
    check(local_source.from_url(url) == video.resolve(), "地址能还原成原路径")
    check(not local_source.is_local_url("https://www.bilibili.com/video/BV1xx"), "B站链接不误判")


def test_probe_audio_sub(video: Path, tmp: Path) -> None:
    print("\n[3] 元数据 / 抽音频 / 同名字幕")
    meta, info = local_source.probe(video)
    check(meta.title == video.stem, f"标题取文件名：{meta.title}")
    check(abs(meta.duration - 3.0) < 0.6, f"时长读到了：{meta.duration:.2f}s")
    check(meta.webpage_url == "" and meta.uploader == "", "没有硬塞假的 UP 主 / 链接")
    check(meta.local_path == str(video.resolve()), "内存里带着本地路径")
    check("local_path" not in meta.to_dict(), "meta.json 不含本地路径（不泄漏盘符）")

    audio = local_source.extract_audio(video, tmp / "work")
    check(audio.is_file() and audio.stat().st_size > 1000, f"抽音频成功（{audio.stat().st_size} 字节）")

    found = local_source.load_subtitle(video, tmp / "work")
    check(bool(found), "找到同名字幕")
    if found:
        segs, source = found
        check(source == "srt", "来源标记为 srt")
        check(len(segs) == 1 and "第一句" in segs[0].text, "字幕内容解析正确")


def _note(meta: VideoMeta, frame_path: Path) -> VideoNote:
    frame = Frame(ts=2.0, path=str(frame_path), origin="scene", ocr_text="测试板书")
    chunk = Chunk(index=0, start=0.0, end=3.0, text="第一句测试字幕。", frames=[frame])
    chapter = Chapter(index=0, title="开场", start=0.0, end=3.0, chunk_indexes=[0])
    return VideoNote(
        meta=meta,
        segments=[Segment(start=0.1, end=2.9, text="第一句测试字幕。", source="srt")],
        chunks=[chunk],
        chapters=[chapter],
        one_liner="这是一条本地视频笔记",
        transcript_source="srt",
    )


def test_export(video: Path, tmp: Path) -> None:
    print("\n[4] 导出：本地笔记不生成空链接")
    from PIL import Image

    shot = tmp / "shot.jpg"
    Image.new("RGB", (320, 180), (90, 120, 150)).save(shot)

    meta, _ = local_source.probe(video)
    note = _note(meta, shot)
    md_path = exporter.export(note, {"name": "测试"}, tmp / "out-local", note.segments)
    md = md_path.read_text(encoding="utf-8")
    check("> **来源** 本地视频" in md, "头部标注「来源 本地视频」")
    check("[原视频]" not in md and "?t=" not in md, "没有原视频链接 / 空时间码链接")
    check("本地字幕文件" in md, "字幕来源映射成「本地字幕文件」")
    check("![代表画面]" in md, "关键帧仍然进笔记")

    print("\n[5] 导出：B站笔记行为不变")
    bili_meta = VideoMeta(
        bvid="BV1xx411c7mD", cid=1, title="B站测试笔记", uploader="测试UP",
        duration=3.0, pubdate=1700000000, cover="", webpage_url="https://www.bilibili.com/video/BV1xx411c7mD",
    )
    bili_note = _note(bili_meta, shot)
    bili_md = exporter.export(bili_note, {"name": "测试"}, tmp / "out-bili", bili_note.segments)
    text = bili_md.read_text(encoding="utf-8")
    check("[原视频](https://www.bilibili.com/video/BV1xx411c7mD)" in text, "B站笔记保留原视频链接")
    check("?t=2" in text or "t=2" in text, "B站笔记保留时间码跳转")


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="bn-local-test-"))
    video = make_video(tmp)
    test_validate(video, tmp)
    test_url(video)
    test_probe_audio_sub(video, tmp)
    test_export(video, tmp)
    print("\n" + "=" * 40)
    if FAILS:
        print(f"失败 {len(FAILS)} 项：")
        for f in FAILS:
            print("  -", f)
        return 1
    print("本地视频链路全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
