# -*- coding: utf-8 -*-
"""重建已有笔记的知识地图（不用重新处理视频）。

笔记目录里已经存了 `mindmap.json`（树）和 `mindmap.html`，所以改渲染方式后
可以直接用现成数据重画：不需要再跑一遍下载/转写/总结，也不花 API 额度。

用法：
    python tools/rebuild_maps.py                 # 默认输出目录下全部笔记
    python tools/rebuild_maps.py "D:/笔记/某视频"  # 只重建指定目录
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import Settings          # noqa: E402
from app.services import mindmap as mm        # noqa: E402

_BLOCK = re.compile(r"(## 知识地图\s*\n+```mermaid\s*\n)([\s\S]*?)(\n```)")


def rebuild(folder: Path) -> str:
    tree_path = folder / "mindmap.json"
    md_path = folder / "notes.md"
    if not tree_path.exists() or not md_path.exists():
        return "跳过（不是笔记目录）"

    tree = json.loads(tree_path.read_text(encoding="utf-8"))
    if not isinstance(tree, dict) or not tree.get("name"):
        return "跳过（导图数据为空）"

    md = md_path.read_text(encoding="utf-8")
    new_block = mm.to_mermaid(tree)
    if _BLOCK.search(md):
        md = _BLOCK.sub(lambda m: m.group(1) + new_block + m.group(3), md, count=1)
    else:
        md = md.rstrip() + "\n\n## 知识地图\n\n```mermaid\n" + new_block + "\n```\n"
    md = md.replace(
        "> 交互式版本见同目录 `mindmap.html`（可折叠、可缩放、可导出图片）",
        "> 纵向流程图：有先后顺序的步骤串成一列；想看可缩放的独立版本见同目录 `mindmap.html`",
    )
    md_path.write_text(md, encoding="utf-8", newline="\n")

    (folder / "mindmap.html").write_text(
        mm.to_markmap_html(tree, tree.get("name", "")), encoding="utf-8"
    )
    kind = "步骤链" if "① " in new_block else "并列树"
    return f"已重建（{kind}，{new_block.count('-->')} 条连线）"


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    if args:
        targets = [Path(args[0])]
    else:
        root = Settings().output_dir
        targets = sorted(p for p in root.iterdir() if p.is_dir())

    for folder in targets:
        print(f"{folder.name}：{rebuild(folder)}")


if __name__ == "__main__":
    main()
