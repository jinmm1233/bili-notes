"""启动依赖冒烟：ffmpeg 状态检测与解压逻辑。

不真的下载 92MB（那属于联网用例），只验证：
状态来源判断正确、zip 只取两个 exe、坏包能被识别。
"""
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.services import ffmpeg_boot


def test_status_shape() -> None:
    st = ffmpeg_boot.status()
    assert set(st) == {"ready", "ffmpeg", "ffprobe", "source"}, st
    print(f"  状态：ready={st['ready']} source={st['source']}")
    if st["ready"]:
        assert Path(st["ffmpeg"]).is_file()
        assert st["source"] in {"system", "bundled", "downloaded"}


def test_extract_only_two_exes(tmp: Path) -> None:
    archive = tmp / "ffmpeg.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("ffmpeg-7.1-essentials_build/bin/ffmpeg.exe", b"MZfake-ffmpeg")
        zf.writestr("ffmpeg-7.1-essentials_build/bin/ffprobe.exe", b"MZfake-ffprobe")
        zf.writestr("ffmpeg-7.1-essentials_build/doc/README.txt", b"docs")
        zf.writestr("ffmpeg-7.1-essentials_build/LICENSE", b"gpl")

    target = tmp / "bin"
    target.mkdir()
    found = ffmpeg_boot._extract(archive, target)
    assert sorted(found) == ["ffmpeg.exe", "ffprobe.exe"], found
    assert sorted(p.name for p in target.iterdir()) == ["ffmpeg.exe", "ffprobe.exe"]
    assert (target / "ffmpeg.exe").read_bytes() == b"MZfake-ffmpeg"
    print("  解压：只取 ffmpeg.exe / ffprobe.exe，文档被丢弃")


def test_broken_archive_returns_empty(tmp: Path) -> None:
    archive = tmp / "bad.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("readme.txt", "not an ffmpeg build")
    target = tmp / "bin2"
    target.mkdir()
    assert ffmpeg_boot._extract(archive, target) == []
    assert list(target.iterdir()) == []
    print("  坏包：返回空，调用方会删缓存并提示重试")


def main() -> int:
    print("== 启动依赖冒烟 ==")
    test_status_shape()
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        test_extract_only_two_exes(tmp)
        test_broken_archive_returns_empty(tmp)
    print("== 全部通过 ==")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
