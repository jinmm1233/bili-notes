"""导出功能回归：Word 结构与公式、PDF 无头打印链路。

用临时造的笔记目录跑，不碰用户真实数据，也不需要联网。
  python tests/smoke_export.py
"""

from __future__ import annotations

import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "vendor"))

FAILED = 0


def ok(cond: bool, msg: str) -> None:
    global FAILED
    print(("[OK] " if cond else "[FAIL] ") + msg)
    if not cond:
        FAILED += 1


NOTE = r"""---
title: "导出测试：初等行变换"
uploader: "测试UP"
duration: "03:30"
source: "https://www.bilibili.com/video/BV1TEST"
created: "2026-11-04 20:00"
---

# 导出测试：初等行变换

> **UP主** 测试UP · **时长** 03:30 · [原视频](https://www.bilibili.com/video/BV1TEST)
> 字幕来源：本地语音转写

## 一句话总结

用初等行变换求逆矩阵，公式 $A^{-1}=\frac{1}{|A|}A^*$ 只是对照。

## 笔记

### 一、方法

- 构造增广矩阵 $[A \mid E]$，把 $A$ 化为 $E$
- 第二步：$\begin{bmatrix} 1 & 2 \\\\ 3 & 4 \\end{bmatrix}$ 转置

1. 先化下方为 0
2. 再化上方为 0

> 注意：只能做行变换

$$
P_k \\cdots P_1 A = E
$$

| 方法 | 复杂度 |
|---|---|
| 伴随法 | 高 |
| 行变换 | 低 |

```python
# 伪代码
for row in matrix:
    pass
```

![代表画面](assets/frame.jpg)

## 文本校验

这段是给网页看的，导出时应当被丢掉。

## 完整转录

<details><summary>完整转录</summary>

这里是转录正文，默认不导出。

</details>
"""


def make_note(folder: Path) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "notes.md").write_text(NOTE, encoding="utf-8", newline="\n")
    assets = folder / "assets"
    assets.mkdir(exist_ok=True)
    try:
        from PIL import Image

        Image.new("RGB", (640, 360), (40, 60, 90)).save(assets / "frame.jpg", quality=70)
    except Exception:
        # 没有 Pillow 就跳过图片断言
        pass
    (folder / "mindmap.json").write_text(
        '{"name":"导出测试","children":[{"name":"方法","children":[{"name":"构造增广矩阵"}]}]}',
        encoding="utf-8",
    )


def test_docx(tmp: Path) -> None:
    from app.services import export_docx

    folder = tmp / "note"
    make_note(folder)
    out = tmp / "out.docx"
    export_docx.export(folder, out)
    ok(out.is_file() and out.stat().st_size > 5000, f"Word 生成成功（{out.stat().st_size // 1024}KB）")

    with zipfile.ZipFile(out) as z:
        xml = z.read("word/document.xml").decode("utf-8")
        media = [n for n in z.namelist() if n.startswith("word/media/")]
    ok(xml.count("<m:oMath>") >= 3, f"公式转成 Word 原生公式（{xml.count('<m:oMath>')} 个）")
    ok(len(media) == 1, "关键帧图片写进了文档")
    ok("<w:tbl>" in xml, "表格保留")
    ok("文本校验" not in xml, "网页专用区块（文本校验）没有导出")
    ok("这里是转录正文" not in xml, "完整转录默认不导出")

    from docx import Document

    doc = Document(str(out))
    heads = [p.text for p in doc.paragraphs if p.style.name.startswith("Heading")]
    ok(any("方法" in h for h in heads), "标题层级保留")
    ok(any("构造增广矩阵" in p.text for p in doc.paragraphs), "知识地图转成了层级列表")

    # 勾选转录时要能导出
    out2 = tmp / "out_transcript.docx"
    export_docx.export(folder, out2, include_transcript=True)
    with zipfile.ZipFile(out2) as z:
        xml2 = z.read("word/document.xml").decode("utf-8")
    ok("这里是转录正文" in xml2, "勾选后完整转录会导出")


def test_pdf(tmp: Path) -> None:
    from app.services import export_pdf

    browser = export_pdf.find_browser()
    if not browser:
        print("[SKIP] 这台机器没有 Edge/Chrome，跳过 PDF 打印测试")
        return
    ok(True, f"找到浏览器：{Path(browser).name}")

    html = tmp / "probe.html"
    html.write_text("<html><body><h1>PDF 自检</h1><p>中文与公式 $x^2$</p></body></html>",
                    encoding="utf-8")
    out = tmp / "probe.pdf"
    result = export_pdf.export(html.as_uri(), out, timeout=90)
    ok(result is not None and out.is_file() and out.read_bytes()[:5] == b"%PDF-",
       "无头浏览器能打印出合法 PDF")
    if result:
        ok(out.stat().st_size > 1000, f"PDF 有实际内容（{out.stat().st_size // 1024}KB）")


def main() -> int:
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        print("=== Word 导出 ===")
        test_docx(tmp)
        print("\n=== PDF 导出 ===")
        test_pdf(tmp)
    print()
    if FAILED:
        print(f"{FAILED} 项未通过")
        return 1
    print("导出功能全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
