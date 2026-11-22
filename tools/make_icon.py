"""生成应用图标 app.ico（多尺寸）+ web 图标 + 预览 PNG。

设计取自界面侧边栏 brand-mark（科技主题配色）：
  青色渐变底（135° #0891b2 → #22d3ee）+ 白色圆角框 + 顶部两个装订环 + 一条横线。

为什么每个尺寸单独画：
  18/20/24/32px 的任务栏图标如果直接由 256 缩下来，线条会糊成一团。
  所以 ≥32px 保留装订环，24px 起省略过细细节并加粗描边，
  16px 只留「框 + 一条横线」，靠形状传达「笔记卡片」。

用法：python tools/make_icon.py
产物：app.ico、app_icon_preview.png、app_icon_preview_32.png、
      web/icon.svg、web/icon-192.png、web/icon-512.png
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)
SUPER = 4          # 超采样倍数：先画大图再缩小，边缘才不毛刺

# 青色渐变（比界面 brand-tile 提亮一档，小图标更醒目）：#16b4d4 → #4ae0f2
TILE_FROM = (22, 180, 212)
TILE_TO = (74, 224, 242)
WHITE = (255, 255, 255, 255)


def _lerp(a, b, t):
    return tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(3))


def _gradient(size: int) -> Image.Image:
    """135° 线性渐变：左上 TILE_FROM → 右下 TILE_TO。"""
    img = Image.new("RGB", (size, size))
    px = img.load()
    denom = max(1, 2 * (size - 1))
    for y in range(size):
        for x in range(size):
            px[x, y] = _lerp(TILE_FROM, TILE_TO, (x + y) / denom)
    return img


def _params(size: int):
    """按尺寸给描边粗细与细节开关。返回 (stroke, hooks, box_top, box_bottom)。"""
    if size >= 48:
        return 2.00, True, 5.0, 19.0
    if size >= 32:
        return 2.30, True, 5.1, 19.0
    if size >= 24:
        return 2.55, False, 5.3, 18.8
    if size >= 20:
        return 2.80, False, 5.2, 18.8
    return 2.90, False, 5.0, 19.0


def render(size: int) -> Image.Image:
    s = size * SUPER
    u = s / 24.0

    mask = Image.new("L", (s, s), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, s - 1, s - 1), radius=int(s * 0.225), fill=255)
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    img.paste(_gradient(s), (0, 0), mask)

    draw = ImageDraw.Draw(img)
    stroke, hooks, box_top, box_bottom = _params(size)
    w = max(2, int(round(stroke * u)))

    def X(v: float) -> float:
        return v * u

    # 主体圆角框
    draw.rounded_rectangle(
        (X(3.0), X(box_top), X(21.0), X(box_bottom)),
        radius=X(3.2), outline=WHITE, width=w)

    # 顶部两个装订环：小尺寸省略，否则糊成一坨
    if hooks:
        draw.line((X(8.0), X(3.0), X(10.0), X(6.0)), fill=WHITE, width=w)
        draw.line((X(16.0), X(3.0), X(14.0), X(6.0)), fill=WHITE, width=w)

    # 中间横线
    cy = (box_top + box_bottom) / 2 + 0.3
    draw.line((X(9.0), X(cy), X(15.0), X(cy)), fill=WHITE, width=w)

    return img.resize((size, size), Image.LANCZOS)


def build_ico(images: list[Image.Image], path: Path) -> None:
    """写多尺寸 ICO：Pillow 按 sizes 逐个挑尺寸完全匹配的那张，
    所以小尺寸用的是专门画的简化版，而不是从 256 缩下来的。"""
    ordered = sorted(images, key=lambda im: im.size[0])
    sizes = [(im.size[0], im.size[1]) for im in ordered]
    ordered[-1].save(path, format="ICO", sizes=sizes, append_images=ordered[:-1])


SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="100" height="100">
  <defs>
    <linearGradient id="bnTile" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="#16B4D4"/>
      <stop offset="1" stop-color="#4AE0F2"/>
    </linearGradient>
  </defs>
  <rect width="24" height="24" rx="5.4" fill="url(#bnTile)"/>
  <g fill="none" stroke="#FFFFFF" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
    <rect x="3" y="5" width="18" height="14" rx="3"/>
    <path d="M8 3l2 3M16 3l-2 3"/>
    <path d="M9 12h6"/>
  </g>
</svg>
"""


def main() -> int:
    # 统一以 256px 的完整设计为准：先画一张 256（内部 4x 超采样到 1024），
    # 再等比缩到所有尺寸，保证每个尺寸形状完全一致，只差分辨率。
    master = render(256)
    images = [master.resize((s, s), Image.LANCZOS) for s in SIZES]
    build_ico(images, ROOT / "app.ico")

    images[-1].save(ROOT / "app_icon_preview.png")

    (ROOT / "web" / "icon.svg").write_text(SVG, encoding="utf-8")
    for s in (192, 512):
        master.resize((s, s), Image.LANCZOS).save(
            ROOT / "web" / f"icon-{s}.png", format="PNG")

    print(f"已生成 app.ico（{len(SIZES)} 个尺寸，统一由 256 缩放）+ 预览图 + web 图标")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
