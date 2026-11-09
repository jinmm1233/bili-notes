"""生成应用图标 app.ico（多尺寸）+ 预览 PNG。

为什么自己画而不是用现成素材：
  - 旧图标是「白纸 + 右下角贴一个粉圆」，小尺寸下又糊又像网页图标；
  - 这次要保证 16px 任务栏 / 资源管理器小图标也认得出，所以分两套画法：
    ≥48px 画完整（渐变底 + 笔记页 + 标题/正文线 + 播放钮），
    <48px 画简化（渐变底 + 笔记页 + 播放三角），细节全省掉。

图标含义：深蓝渐变底 = 工具类应用；白纸 = 笔记；粉色播放钮 = 视频（B 站粉）。

用法：python tools/make_icon.py
产物：app.ico（16/20/24/32/40/48/64/128/256）、app_icon_preview.png
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

ROOT = Path(__file__).resolve().parents[1]
SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)
SUPER = 4          # 超采样倍数：先画大图再缩小，边缘才不毛刺

BG_TOP = (58, 74, 122)
BG_BOTTOM = (26, 32, 58)
PAPER = (255, 255, 255)
PAPER_LINE = (201, 210, 232)
ACCENT = (251, 114, 153)      # B 站粉
ACCENT_DEEP = (240, 65, 124)
TITLE_LINE = (251, 114, 153)


def _rounded_mask(size: int, radius: int) -> Image.Image:
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, size - 1, size - 1), radius=radius, fill=255)
    return mask


def _gradient(size: int) -> Image.Image:
    """竖直渐变底：上浅下深，比纯色更有体积感。"""
    top = np.array(BG_TOP, dtype=np.float32)
    bottom = np.array(BG_BOTTOM, dtype=np.float32)
    ramp = np.linspace(0.0, 1.0, size, dtype=np.float32)[:, None]
    band = top[None, :] * (1 - ramp) + bottom[None, :] * ramp
    return Image.fromarray(np.repeat(band[:, None, :], size, axis=1)
                           .astype(np.uint8), "RGB").convert("RGBA")


def _draw_play(draw: ImageDraw.ImageDraw, cx: float, cy: float, r: float) -> None:
    """粉色播放钮：径向渐变圆 + 白边 + 白色三角。"""
    steps = 28
    for i in range(steps, 0, -1):
        t = i / steps
        rr = r * t
        color = (
            int(ACCENT[0] * (1 - t) + ACCENT_DEEP[0] * t),
            int(ACCENT[1] * (1 - t) + ACCENT_DEEP[1] * t),
            int(ACCENT[2] * (1 - t) + ACCENT_DEEP[2] * t),
        )
        draw.ellipse((cx - rr, cy - rr, cx + rr, cy + rr), fill=color + (255,))
    draw.ellipse((cx - r, cy - r, cx + r, cy + r),
                 outline=(255, 255, 255, 235), width=max(2, int(r * 0.13)))
    # 三角略偏左，视觉上才是「居中」
    w, h = r * 0.92, r * 1.16
    draw.polygon(
        [(cx - w * 0.42, cy - h * 0.5), (cx - w * 0.42, cy + h * 0.5),
         (cx + w * 0.58, cy)],
        fill=(255, 255, 255, 255),
    )


def render(size: int, detailed: bool) -> Image.Image:
    s = size * SUPER
    img = _gradient(s)
    img.putalpha(_rounded_mask(s, int(s * 0.225)))

    # 顶部一层柔光，避免大色块发闷
    gloss = Image.new("RGBA", (s, s), (255, 255, 255, 0))
    ImageDraw.Draw(gloss).ellipse(
        (-s * 0.45, -s * 0.75, s * 1.45, s * 0.48), fill=(255, 255, 255, 17))
    gloss = gloss.filter(ImageFilter.GaussianBlur(s * 0.05))
    img = Image.alpha_composite(img, gloss)

    draw = ImageDraw.Draw(img)

    # 笔记页（右下角被播放钮压住，所以略偏左上）
    px0, py0 = s * 0.215, s * 0.165
    px1, py1 = s * 0.775, s * 0.845
    radius = int(s * 0.055)

    shadow = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle(
        (px0 + s * 0.012, py0 + s * 0.022, px1 + s * 0.012, py1 + s * 0.022),
        radius=radius, fill=(6, 10, 26, 120))
    shadow = shadow.filter(ImageFilter.GaussianBlur(s * 0.022))
    img = Image.alpha_composite(img, shadow)
    draw = ImageDraw.Draw(img)

    draw.rounded_rectangle((px0, py0, px1, py1), radius=radius, fill=PAPER + (255,))

    if detailed:
        # 右上角折角：一眼看出是「一页纸」
        fold = s * 0.135
        draw.polygon(
            [(px1 - fold, py0), (px1, py0), (px1, py0 + fold)],
            fill=(228, 233, 245, 255))
        draw.line([(px1 - fold, py0), (px1 - fold, py0 + fold), (px1, py0 + fold)],
                  fill=(206, 214, 233, 255), width=max(1, int(s * 0.008)))
        # 标题线（粉色）+ 三条正文线
        draw.rounded_rectangle(
            (px0 + s * 0.075, py0 + s * 0.105,
             px0 + s * 0.075 + (px1 - px0) * 0.52, py0 + s * 0.105 + s * 0.052),
            radius=int(s * 0.026), fill=TITLE_LINE + (255,))
        for i in range(3):
            y = py0 + s * (0.235 + i * 0.105)
            width = (px1 - px0) * (0.80 if i < 2 else 0.56)
            draw.rounded_rectangle(
                (px0 + s * 0.075, y, px0 + s * 0.075 + width, y + s * 0.036),
                radius=int(s * 0.018), fill=PAPER_LINE + (255,))
    else:
        # 小尺寸只留一条粗线，靠形状而不是细节传达「笔记」
        draw.rounded_rectangle(
            (px0 + s * 0.085, py0 + s * 0.12,
             px0 + s * 0.085 + (px1 - px0) * 0.60, py0 + s * 0.12 + s * 0.055),
            radius=int(s * 0.028), fill=PAPER_LINE + (255,))

    _draw_play(draw, s * 0.700, s * 0.725, s * 0.192)

    return img.resize((size, size), Image.LANCZOS)


def build_ico(images: list[Image.Image], path: Path) -> None:
    """写多尺寸 ICO。

    用 Pillow 的 append_images：它按 sizes 逐个挑尺寸完全匹配的那张，
    所以小尺寸用的是我们专门画的简化版（大尺寸缩下来会糊），
    而不是从 256 缩的。
    """
    # 主图必须给最大的那张：Pillow 以主图尺寸为上限裁剪其它尺寸
    ordered = sorted(images, key=lambda im: im.size[0])
    sizes = [(im.size[0], im.size[1]) for im in ordered]
    ordered[-1].save(
        path, format="ICO", sizes=sizes, append_images=ordered[:-1],
    )


def main() -> int:
    images: list[Image.Image] = []
    for size in SIZES:
        images.append(render(size, detailed=size >= 48))
    build_ico(images, ROOT / "app.ico")
    images[-1].save(ROOT / "app_icon_preview.png")
    # 32px 单独存一张，方便肉眼确认小尺寸还认得出
    images[SIZES.index(32)].resize((128, 128), Image.NEAREST).save(
        ROOT / "app_icon_preview_32.png")
    print(f"已生成 app.ico（{len(SIZES)} 个尺寸）+ 预览图")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
