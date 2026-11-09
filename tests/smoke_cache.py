"""缓存清理冒烟：处理完自动删视频，别把笔记里的图删了。

关键约束（这条错了会毁用户笔记）：
    media 策略只允许删视频/音频，绝不能碰 frames/ 和 json ——
    已生成的笔记虽然复制了图，但用户可能还想复查过程文件。
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import Settings  # noqa: E402
from app.services import cache_clean  # noqa: E402


def _fake_task(root: Path, name: str, mb: int = 1) -> Path:
    """造一个像真实任务的目录：video + audio + frames + json。"""
    work = root / name
    (work / "frames").mkdir(parents=True)
    blob = b"x" * (mb * 1024 * 1024)
    (work / "video.mp4").write_bytes(blob)
    (work / "audio.m4a").write_bytes(blob)
    (work / "transcript.json").write_text('{"a":1}', encoding="utf-8")
    (work / "frames" / "f1.jpg").write_bytes(b"jpgdata")
    return work


def test_usage():
    root = Path(tempfile.mkdtemp())
    try:
        _fake_task(root, "t1", mb=2)
        _fake_task(root, "t2", mb=1)
        info = cache_clean.usage(root)
        assert info["tasks"] == 2, info
        assert info["bytes"] > 6 * 1024 * 1024, info
        assert "MB" in info["human"], info["human"]
        print(f"  usage: {info['human']} / {info['tasks']} tasks")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_usage_ttl():
    """/api/settings 每次都扫 755MB 缓存要 3~6 秒；TTL 内复用，删过文件要失效。"""
    root = Path(tempfile.mkdtemp())
    try:
        cache_clean.invalidate_usage()
        _fake_task(root, "t1", mb=1)
        first = cache_clean.usage(root)
        (root / "t2").mkdir()
        (root / "t2" / "big.bin").write_bytes(b"y" * 1024)
        second = cache_clean.usage(root)
        assert second["tasks"] == first["tasks"], "TTL 内应复用上次统计"
        cache_clean.invalidate_usage()
        assert cache_clean.usage(root)["tasks"] == 2, "手动失效后重新统计"
        cache_clean.clean_task(root / "t2", "all")
        assert cache_clean.usage(root)["tasks"] == 1, "删过文件后数字必须更新"
        print("  usage TTL：复用 / 失效 / 清理后重算 均正确")
    finally:
        cache_clean.invalidate_usage()
        shutil.rmtree(root, ignore_errors=True)


def test_media_policy_keeps_frames():
    root = Path(tempfile.mkdtemp())
    try:
        work = _fake_task(root, "t1")
        freed = cache_clean.clean_task(work, "media")
        assert freed > 0, freed
        assert not (work / "video.mp4").exists(), "视频应被删除"
        assert not (work / "audio.m4a").exists(), "音频应被删除"
        assert (work / "frames" / "f1.jpg").exists(), "抽帧不能删（要复查）"
        assert (work / "transcript.json").exists(), "转录 json 不能删"
        print(f"  media: 释放 {cache_clean.human(freed)}，帧与 json 保留")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_all_and_keep():
    root = Path(tempfile.mkdtemp())
    try:
        keep = _fake_task(root, "keepme")
        assert cache_clean.clean_task(keep, "keep") == 0
        assert (keep / "video.mp4").exists(), "keep 策略不能删任何东西"

        gone = _fake_task(root, "goneme")
        freed = cache_clean.clean_task(gone, "all")
        assert freed > 0 and not gone.exists(), "all 策略应删掉整个目录"
        print(f"  keep 保留 / all 整目录删除（释放 {cache_clean.human(freed)}）")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_clean_all_skips_active():
    root = Path(tempfile.mkdtemp())
    try:
        _fake_task(root, "running")
        _fake_task(root, "done1")
        res = cache_clean.clean_all(root, policy="all", keep_ids=["running"])
        assert res["removed"] == 1, res
        assert (root / "running" / "video.mp4").exists(), "正在跑的任务不能被清理"
        assert not (root / "done1").exists()
        print(f"  clean_all: 跳过运行中任务，释放 {res['human']}")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_policy_config():
    assert Settings().cache_policy == "media", "默认应只删视频音频"
    try:
        Settings(cache_policy="nonsense")
    except Exception as exc:  # pydantic ValidationError
        assert "cache_policy" in str(exc), exc
        print("  配置校验：非法策略被拒绝")
    else:
        raise AssertionError("非法 cache_policy 应报错")


def test_render_uses_output_copy():
    """导出时关键帧必须已经复制进输出目录，否则清缓存会毁掉笔记图片。"""
    from app.core.models import Chapter, Chunk, Frame, VideoMeta, VideoNote
    from app.services.exporter import export

    root = Path(tempfile.mkdtemp())
    try:
        cache_work = _fake_task(root, "cache_task")
        frame_src = cache_work / "frames" / "f1.jpg"
        meta = VideoMeta(
            bvid="BV1", cid=1, title="缓存测试", uploader="u",
            duration=60, pubdate=0, cover="", webpage_url="https://example.com",
        )
        note = VideoNote(meta=meta)
        chunk = Chunk(index=0, start=0.0, end=60.0, text="讲解内容")
        chunk.frames = [Frame(ts=5.0, path=str(frame_src), origin="scene")]
        note.chunks = [chunk]
        note.chapters = [Chapter(index=0, title="一", start=0.0, end=60.0, chunk_indexes=[0])]

        md_path = export(note, {"name": "缓存测试", "children": []}, root / "out", [], "")
        assets = md_path.parent / "assets"
        assert any(assets.iterdir()), "关键帧应复制到输出目录 assets/"

        # 清掉整个缓存，笔记图片必须还在
        cache_clean.clean_task(cache_work, "all")
        copied = list(assets.iterdir())
        assert copied and copied[0].exists(), "清缓存后笔记图片不能消失"
        print(f"  导出后清缓存：{md_path.parent.name}/assets 图片完好（{len(copied)} 张）")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_task_manager_cleanup():
    """真正挂到任务收尾上的那段：成功删干净，失败只删媒体。"""
    from app.services.task_manager import Task, TaskManager

    root = Path(tempfile.mkdtemp())
    try:
        settings = Settings(data_dir=root, cache_policy="media")
        manager = TaskManager(settings, store=None)
        _fake_task(settings.cache_dir, "okdone")
        _fake_task(settings.cache_dir, "badone")

        done = Task(id="okdone", url="u", status="done", message="完成，用时 3s")
        manager._cleanup(done)
        work = settings.cache_dir / "okdone"
        assert not (work / "video.mp4").exists()
        assert (work / "transcript.json").exists(), "media 策略保留 json"
        assert "已清理缓存" in done.message, done.message

        failed = Task(id="badone", url="u", status="failed", message="崩了")
        manager._cleanup(failed)
        assert not (settings.cache_dir / "badone" / "video.mp4").exists()
        assert (settings.cache_dir / "badone" / "transcript.json").exists()

        # all 策略：任务成功后连目录一起没
        settings2 = Settings(data_dir=root, cache_policy="all")
        mgr2 = TaskManager(settings2, store=None)
        _fake_task(settings2.cache_dir, "alldone")
        mgr2._cleanup(Task(id="alldone", url="u", status="done"))
        assert not (settings2.cache_dir / "alldone").exists(), "all 策略应删整个任务目录"

        # keep 策略：一个字节都不动
        settings3 = Settings(data_dir=root, cache_policy="keep")
        mgr3 = TaskManager(settings3, store=None)
        _fake_task(settings3.cache_dir, "kept")
        mgr3._cleanup(Task(id="kept", url="u", status="done"))
        assert (settings3.cache_dir / "kept" / "video.mp4").exists(), "keep 不能删"
        print("  任务收尾：media/all/keep 三档行为正确")
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    print("cache_clean smoke:")
    test_usage()
    test_usage_ttl()
    test_media_policy_keeps_frames()
    test_all_and_keep()
    test_clean_all_skips_active()
    test_policy_config()
    test_render_uses_output_copy()
    test_task_manager_cleanup()
    print("all passed")
