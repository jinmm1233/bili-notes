"""视觉链路冒烟：造一个 PPT 式视频 → 场景抽帧 → OCR → 打分选帧。

不依赖网络，验证 ffmpeg 抽帧、RapidOCR 中文识别、四维打分三件事。
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image, ImageDraw, ImageFont  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.core.models import Chunk, Segment  # noqa: E402
from app.services import frames as frames_mod  # noqa: E402
from app.services import ocr  # noqa: E402

FONT_CANDIDATES = [
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\simsun.ttc",
]

SLIDES = [
    ("第三章 缓存失效策略", ["1. TTL 过期删除", "2. 主动失效通知", "3. 写时更新（Write-Through）"]),
    ("性能对比数据", ["方案 A：响应时间 120ms", "方案 B：响应时间 45ms", "提升约 62%"]),
    ("核心公式", ["QPS = 并发数 / 平均响应时间", "命中率 = 命中次数 / 总请求数"]),
]


def find_font(size: int) -> ImageFont.FreeTypeFont:
    for path in FONT_CANDIDATES:
        if Path(path).is_file():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def make_slide(title: str, bullets: list[str], out: Path) -> None:
    img = Image.new("RGB", (1280, 720), (255, 255, 255))
    draw = ImageDraw.Draw(img)
    draw.rectangle([0, 0, 1280, 8], fill=(0, 120, 200))
    draw.text((80, 80), title, font=find_font(56), fill=(20, 20, 20))
    y = 220
    for b in bullets:
        draw.text((100, y), b, font=find_font(38), fill=(60, 60, 60))
        y += 90
    draw.text((80, 650), "BiliNotes 测试画面", font=find_font(24), fill=(150, 150, 150))
    img.save(out)


def build_video(tmp: Path) -> Path:
    for i, (title, bullets) in enumerate(SLIDES):
        make_slide(title, bullets, tmp / f"slide{i}.png")

    listing = tmp / "list.txt"
    lines = []
    for i in range(len(SLIDES)):
        lines.append(f"file 'slide{i}.png'")
        lines.append("duration 10")
    lines.append(f"file 'slide{len(SLIDES) - 1}.png'")
    listing.write_text("\n".join(lines), encoding="utf-8")

    video = tmp / "deck.mp4"
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "concat", "-safe", "0", "-i", str(listing),
        "-vf", "fps=6,format=yuv420p", "-c:v", "libx264", "-t", "30", str(video),
    ]
    subprocess.run(cmd, check=True, capture_output=True)
    return video


def main() -> int:
    settings = get_settings()
    fails: list[str] = []

    def check(cond: bool, label: str) -> None:
        print(("  ok  " if cond else "  FAIL") + f"  {label}")
        if not cond:
            fails.append(label)

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        print("[1] 生成测试视频")
        video = build_video(tmp)
        size_mb = video.stat().st_size / 1024 / 1024
        check(video.is_file() and size_mb > 0.05, f"视频已生成 {size_mb:.2f} MB")

        print("\n[2] 场景抽帧")
        from app.core.ffmpeg import probe_duration  # noqa: PLC0415
        duration = probe_duration(str(video))
        check(abs(duration - 30) < 2, f"时长探测 {duration:.1f}s")

        candidates = frames_mod.generate_candidates(
            settings, video, duration, tmp / "frames",
            [Segment(t, t + 5, "我们来看这张图的数据对比", "cc") for t in (2, 12, 22)],
        )
        check(len(candidates) >= 3, f"候选帧 {len(candidates)} 张")
        kinds = {c.origin for c in candidates}
        check("scene" in kinds, f"检出场景切换帧（来源 {kinds}）")

        print("\n[3] OCR 识别")
        texts = []
        for f in candidates[:6]:
            text, blocks = ocr.ocr_image(f.path, 0.5)
            texts.append(text)
            f.ocr_text = text
            f.ocr_blocks = blocks
            f.text_density = min(1.0, len(text) / 120)
            if text:
                print(f"       {Path(f.path).name}: {text.splitlines()[0][:40]}")
        joined = "\n".join(texts)
        check("缓存" in joined, "识别出「缓存」")
        check("QPS" in joined.upper(), "识别出「QPS」")
        check("62" in joined, "识别出数字 62")

        print("\n[4] 打分选帧")
        chunks = [Chunk(0, 0, 10, "我们来看缓存失效策略的三种做法"),
                  Chunk(1, 10, 20, "这张图是性能对比数据，响应时间从120ms降到45ms"),
                  Chunk(2, 20, 30, "核心公式 QPS 等于并发数除以平均响应时间")]
        selected = frames_mod.score_and_select(settings, candidates, chunks)
        # max_frames_total=0 表示不设全片配额（默认），别把 0 当成上限
        cap = settings.max_frames_total or len(candidates)
        check(1 <= len(selected) <= cap, f"入选 {len(selected)} 张")
        check(len(selected) >= 3, "三个知识块至少各有代表画面")
        check(all(s.score > 0 for s in selected), "分数已计算")
        origin_kinds = {s.origin for s in selected}
        print(f"       来源分布：{origin_kinds}")

    print("\n" + "=" * 40)
    if fails:
        print(f"失败 {len(fails)} 项：{fails}")
        return 1
    print("视觉链路全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
