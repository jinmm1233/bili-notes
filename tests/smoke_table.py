"""画面表格还原冒烟：几何聚类 → Markdown 表格。

用合成 OCR 盒（含 x/x2 坐标）验证，不跑真实 OCR ——
RapidOCR 的输出格式是固定的三段式，几何逻辑与识别引擎无关。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.services import ocr


def item(text: str, x: float, width: float, y: float, h: float = 20.0) -> dict:
    return {"text": text, "score": 0.95, "x": x, "x2": x + width, "y": y, "h": h}


def make_lines(rows: list[list[tuple[str, float]]], y0: float = 0.0, dy: float = 40.0,
               col_gap: float = 60.0, h: float = 20.0) -> list[dict]:
    """rows: 每行是 [(文字, 该列起始 x)]。列间距由调用方控制。"""
    lines = []
    for r, row in enumerate(rows):
        y = y0 + r * dy
        items = []
        for text, x in row:
            items.append(item(text, x, len(text) * h * 0.6, y, h))
        merged = ocr._group_lines(items)
        lines.extend(merged)
    return lines


def test_simple_table() -> None:
    rows = [
        [("项目", 0), ("价格", 300), ("备注", 600)],
        [("门票", 0), ("30元", 300), ("现场买", 600)],
        [("打车", 0), ("25元", 300), ("Grab", 600)],
        [("吃饭", 0), ("80元", 300), ("两人份", 600)],
    ]
    lines = make_lines(rows)
    md = ocr.build_table(lines)
    assert md is not None, "规整表格未被识别"
    body = md.splitlines()
    assert body[0].startswith("| 项目 | 价格 | 备注 |"), body[0]
    assert body[1].startswith("| --- |"), body[1]
    assert len(body) == 5, body          # 表头 + 分隔行 + 3 行数据
    print("  规整表格 →\n" + "\n".join("    " + b for b in body))


def test_not_enough_rows() -> None:
    rows = [
        [("项目", 0), ("价格", 300)],
        [("门票", 0), ("30元", 300)],
    ]
    assert ocr.build_table(make_lines(rows)) is None
    print("  两行：不算表格")


def test_prose_not_table() -> None:
    """普通正文：每行只有一段话，不能被当成表格。"""
    items = [
        item("我们今天", 0, 80, 0), item("来讲一下", 84, 80, 0),
        item("这个视频", 0, 80, 40), item("里的内容", 84, 80, 40),
        item("非常精彩", 0, 80, 80), item("值得一看", 84, 80, 80),
    ]
    lines = ocr._group_lines(items)
    assert ocr.build_table(lines) is None
    print("  普通正文：不算表格")


def test_title_plus_table() -> None:
    """标题 + 表格：表格要被还原，标题要保持原样。"""
    title = ocr._group_lines([item("越南花费一览", 0, 120, 0)])
    rows = [
        [("项目", 0), ("价格", 300)],
        [("门票", 0), ("30元", 300)],
        [("打车", 0), ("25元", 300)],
    ]
    lines = title + make_lines(rows, y0=60)
    text = ocr.render_lines(lines)
    assert text.splitlines()[0] == "越南花费一览", text.splitlines()[0]
    assert "| 项目 | 价格 |" in text
    print("  标题+表格：标题保留，表格还原")


def test_clean_keeps_duplicate_rows() -> None:
    """表格里的重复行不能被去重删掉。"""
    md = "| 项 | 值 |\n| --- | --- |\n| a | 1 |\n| a | 1 |"
    out = ocr._clean(md + "\n短")
    assert out.count("| a | 1 |") == 2, out
    print("  去重逻辑放行表格重复行")


def main() -> int:
    print("== 画面表格还原冒烟 ==")
    test_simple_table()
    test_not_enough_rows()
    test_prose_not_table()
    test_title_plus_table()
    test_clean_keeps_duplicate_rows()
    print("== 全部通过 ==")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
