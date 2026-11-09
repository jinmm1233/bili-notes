"""文字预筛测试：能不能把纯画面挡掉、把有文字的留下，以及够不够快。"""
from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image, ImageDraw, ImageFilter, ImageFont  # noqa: E402

from app.services import frames as fm  # noqa: E402

FAILS: list[str] = []
FONT_PATHS = [r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\simhei.ttf"]


def _font(size: int):
    for p in FONT_PATHS:
        if Path(p).is_file():
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()


def make_ppt(path: Path) -> None:
    img = Image.new("RGB", (1280, 720), (255, 255, 255))
    d = ImageDraw.Draw(img)
    d.text((80, 70), "第三章 缓存失效策略", font=_font(52), fill=(20, 20, 20))
    for i, line in enumerate(["1. TTL 过期删除，实现简单但有延迟", "2. 主动失效通知，实时性最好",
                              "3. 写时更新，一致性最强"]):
        d.text((100, 220 + i * 80), line, font=_font(36), fill=(60, 60, 60))
    img.save(path)


def make_table(path: Path) -> None:
    img = Image.new("RGB", (1280, 720), (255, 255, 255))
    d = ImageDraw.Draw(img)
    d.text((80, 60), "性能对比", font=_font(44), fill=(20, 20, 20))
    x0, y0, cw, rh = 90, 180, 280, 70
    for r in range(4):
        for c in range(3):
            d.rectangle([x0 + c * cw, y0 + r * rh, x0 + (c + 1) * cw, y0 + (r + 1) * rh],
                        outline=(90, 90, 90), width=2)
    for r, row in enumerate([["方案", "响应时间", "提升"], ["A", "120ms", "-"],
                             ["B", "45ms", "62%"], ["C", "38ms", "68%"]]):
        for c, cell in enumerate(row):
            d.text((x0 + c * cw + 20, y0 + r * rh + 18), cell, font=_font(30), fill=(40, 40, 40))
    img.save(path)


def make_gradient(path: Path) -> None:
    img = Image.new("RGB", (1280, 720))
    d = ImageDraw.Draw(img)
    for y in range(720):
        t = y / 720
        d.line([(0, y), (1280, y)], fill=(int(30 + 120 * t), int(60 + 90 * t), int(140 + 60 * t)))
    img.save(path)


def make_portrait(path: Path) -> None:
    """模拟人物/插画画面：大色块 + 曲线，没有文字。"""
    img = Image.new("RGB", (1280, 720), (240, 230, 220))
    d = ImageDraw.Draw(img)
    d.ellipse([440, 140, 840, 540], fill=(225, 190, 165))
    d.ellipse([520, 260, 590, 320], fill=(60, 45, 40))
    d.ellipse([690, 260, 760, 320], fill=(60, 45, 40))
    d.arc([560, 340, 720, 440], start=20, end=160, fill=(150, 90, 80), width=6)
    d.ellipse([300, 200, 500, 460], fill=(90, 110, 140))
    img = img.filter(ImageFilter.GaussianBlur(1.2))
    img.save(path)


def main() -> int:
    def check(cond: bool, label: str) -> None:
        print(("  ok  " if cond else "  FAIL") + f"  {label}")
        if not cond:
            FAILS.append(label)

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        cases = {
            "ppt": (make_ppt, True),
            "table": (make_table, True),
            "gradient": (make_gradient, False),
            "portrait": (make_portrait, False),
        }

        print("[1] 判别力")
        results = {}
        for name, (maker, expect_text) in cases.items():
            path = root / f"{name}.png"
            maker(path)
            score = fm.text_score(str(path))
            results[name] = score
            ok = score >= 0.35 if expect_text else score < 0.35
            check(ok, f"{name:9s} 得分 {score:.2f}  {'应为有文字' if expect_text else '应为无文字'}")

        check(results["ppt"] > results["portrait"], "PPT 明显高于人物画面")
        check(results["table"] > results["gradient"], "表格明显高于渐变")

        print("\n[2] 性能")
        path = root / "ppt.png"
        t0 = time.time()
        for _ in range(30):
            fm.text_score(str(path))
        per = (time.time() - t0) / 30 * 1000
        check(per < 15, f"单张 {per:.1f}ms（OCR 约 500-1000ms）")

        print("\n[3] 批量过滤")
        from app.core.models import Frame  # noqa: PLC0415
        pool = []
        for name in cases:
            pool.append(Frame(ts=float(len(pool)), path=str(root / f"{name}.png")))
        kept, skipped = fm.filter_text_frames(pool)
        check(len(kept) == 2, f"保留 {len(kept)} 张（PPT + 表格）")
        check(skipped == 2, f"跳过 {skipped} 张（渐变 + 人物）")
        check({Path(k.path).stem for k in kept} == {"ppt", "table"}, "保留的正是有文字的")

    print("\n" + "=" * 40)
    if FAILS:
        print(f"失败 {len(FAILS)} 项：{FAILS}")
        return 1
    print("文字预筛全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
