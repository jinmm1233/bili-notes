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
    ffmpeg_boot._extract(archive, target, ("ffmpeg.exe", "ffprobe.exe"))
    assert sorted(p.name for p in target.iterdir()) == ["ffmpeg.exe", "ffprobe.exe"]
    assert (target / "ffmpeg.exe").read_bytes() == b"MZfake-ffmpeg"
    print("  解压：只取 ffmpeg.exe / ffprobe.exe，文档被丢弃")


def test_extract_mac_single(tmp: Path) -> None:
    """macOS 包：单个无后缀文件 + __MACOSX 元数据，元数据要被忽略。"""
    archive = tmp / "ffmpeg-mac.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("ffmpeg", b"MAC-ffmpeg")
        zf.writestr("__MACOSX/._ffmpeg", b"meta")

    target = tmp / "bin_mac"
    target.mkdir()
    old = ffmpeg_boot.IS_WINDOWS
    ffmpeg_boot.IS_WINDOWS = False        # 触发 chmod 分支（本机 chmod 不报错）
    try:
        ffmpeg_boot._extract(archive, target, ("ffmpeg",))
    finally:
        ffmpeg_boot.IS_WINDOWS = old
    assert sorted(p.name for p in target.iterdir()) == ["ffmpeg"]
    assert (target / "ffmpeg").read_bytes() == b"MAC-ffmpeg"
    print("  解压(mac)：只取无后缀 ffmpeg，忽略 __MACOSX 元数据")


def test_plan_platforms() -> None:
    """三个平台各自生成正确的下载计划。"""
    win_plan = ffmpeg_boot._plan()
    assert len(win_plan) == 1
    assert win_plan[0].names == ("ffmpeg.exe", "ffprobe.exe")

    old_w, old_m, old_pf = ffmpeg_boot.IS_WINDOWS, ffmpeg_boot.IS_MAC, ffmpeg_boot.platform
    try:
        ffmpeg_boot.IS_WINDOWS, ffmpeg_boot.IS_MAC = False, True
        ffmpeg_boot.platform = type("P", (), {"machine": staticmethod(lambda: "arm64")})()
        arm = ffmpeg_boot._plan()
        assert [x.names for x in arm] == [("ffmpeg",), ("ffprobe",)]
        assert all("arm" in x.url for x in arm), [x.url for x in arm]

        ffmpeg_boot.platform = type("P", (), {"machine": staticmethod(lambda: "x86_64")})()
        intel = ffmpeg_boot._plan()
        assert all("intel" in x.url for x in intel), [x.url for x in intel]
    finally:
        ffmpeg_boot.IS_WINDOWS, ffmpeg_boot.IS_MAC, ffmpeg_boot.platform = old_w, old_m, old_pf
    print("  计划：Windows 单包 / macOS arm+intel 双包，URL 正确")


def test_broken_archive_returns_empty(tmp: Path) -> None:
    archive = tmp / "bad.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("readme.txt", "not an ffmpeg build")
    target = tmp / "bin2"
    target.mkdir()
    ffmpeg_boot._extract(archive, target, ("ffmpeg.exe", "ffprobe.exe"))
    assert list(target.iterdir()) == []
    print("  坏包：不落地任何文件，调用方会删缓存并提示重试")


def main() -> int:
    print("== 启动依赖冒烟 ==")
    test_status_shape()
    test_plan_platforms()
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        test_extract_only_two_exes(tmp)
        test_extract_mac_single(tmp)
        test_broken_archive_returns_empty(tmp)
    print("== 全部通过 ==")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
