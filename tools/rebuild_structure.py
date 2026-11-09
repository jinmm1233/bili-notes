# -*- coding: utf-8 -*-
"""把已有笔记的「知识地图」流程图换成「内容结构」列表（不用重新处理视频）。

以前的导图把每章要点画成节点，30 字长句横着铺开，手机上要左右拖才看得全。
现在改成列表：章节一行，下面 3~5 条完整要点，纵向滚动就能看全。

要点从**正文 `## 笔记` 段**里取（完整句子），不用 mindmap.json 里的截断版。
原文件备份成 notes.md.bak-structure；重复执行会覆盖同一份备份。

用法：
    python tools/rebuild_structure.py            # 输出目录下全部笔记
    python tools/rebuild_structure.py "D:/笔记/某视频"
"""

from __future__ import annotations

import json
import re
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import Settings          # noqa: E402
from app.services import mindmap as mm        # noqa: E402

# 旧块叫「知识地图」，上一版改叫「要点速览/内容结构」，三种都要能被替换
_BLOCK = re.compile(r"## (?:知识地图|要点速览|内容结构)\s*\n[\s\S]*?(?=\n## |\Z)")
_TIMESTAMP = re.compile(r"(?:画面\s*|▲\s*)(\d{1,2}:\d{2}(?::\d{2})?)")


def _to_seconds(stamp: str) -> int:
    parts = [int(p) for p in stamp.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0)
    return parts[0] * 3600 + parts[1] * 60 + parts[2]


def _stamp(seconds: int) -> str:
    if not seconds:
        return ""
    h, rem = divmod(int(seconds), 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"


def parse_body(md: str) -> list[dict]:
    """从 `## 笔记` 段里抽章节与要点：`###` 是一级，行首 `- ` 是这条的要点。"""
    start = md.find("\n## 笔记")
    if start < 0:
        return []
    block = md[start:]
    nxt = block.find("\n## ", 5)
    if nxt > 0:
        block = block[:nxt]

    sections: list[dict] = []
    current: dict | None = None
    for line in block.split("\n"):
        stripped = line.strip()
        if stripped.startswith("### "):
            title = stripped[4:].strip()
            if not title:
                continue
            current = {"title": title, "ts": 0, "points": []}
            sections.append(current)
            continue
        if current is None:
            continue
        if stripped.startswith("#### "):
            continue                    # 二级标题不进列表，避免层级太深
        if not current["ts"]:
            hit = _TIMESTAMP.search(stripped)
            if hit:
                current["ts"] = _to_seconds(hit.group(1))
        if stripped.startswith("- ") and not stripped.startswith("- ["):
            point = stripped[2:].strip()
            if point and not point.startswith("![") and len(point) >= 6:
                current["points"].append(point)
    return [s for s in sections if s["points"] or s["title"]]


_NUMBERED = re.compile(r"^(?:[一二三四五六七八九十百]+[、.]|\d+[、.)]|（[一二三四五六七八九十]+）)")


def render(sections: list[dict]) -> str:
    if not sections:
        return ""
    multi = len(sections) > 1
    keep = 3 if multi else 5
    lines = ["## 内容结构" if multi else "## 要点速览", ""]
    for idx, sec in enumerate(sections, 1):
        # 标题自己带「一、」「1.」时别再套一层序号，不然会出现「1. 一、…」
        prefix = "" if _NUMBERED.match(sec["title"]) else f"{idx}. "
        head = f"**{prefix}{sec['title']}**" if multi else f"**{sec['title']}**"
        stamp = _stamp(int(sec.get("ts") or 0))
        if stamp:
            head += f"　`{stamp}`"
        lines.append(head)
        lines.append("")
        for point in sec["points"][:keep]:
            lines.append(f"- {point}")
        if sec["points"]:
            lines.append("")
    if multi:
        lines.append("> 想看能缩放的结构图：同目录 `mindmap.html`（只画章节，不含逐条要点）")
        lines.append("")
    return "\n".join(lines)


def rebuild(folder: Path) -> str:
    md_path = folder / "notes.md"
    if not md_path.is_file():
        return "跳过（不是笔记目录）"

    md = md_path.read_text(encoding="utf-8")
    if not _BLOCK.search(md):
        return "跳过（没有这一节）"

    sections = parse_body(md)
    block = render(sections)
    if not block:
        return "跳过（正文里没解析出章节）"

    shutil.copy2(md_path, folder / "notes.md.bak-structure")
    md = _BLOCK.sub(lambda m: block, md, count=1)
    md_path.write_text(md, encoding="utf-8", newline="\n")

    # 顺手把可缩放的那张图也简化掉（只留章节），别让旧的长句节点继续躺着
    tree_path = folder / "mindmap.json"
    if tree_path.is_file():
        slim = {
            "name": folder.name,
            "children": [{"name": s["title"]} for s in sections],
        }
        tree_path.write_text(json.dumps(slim, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
        try:
            (folder / "mindmap.html").write_text(
                mm.to_markmap_html(slim, str(slim["name"] or "结构图")), encoding="utf-8", newline="\n"
            )
        except Exception as exc:                 # 图重画失败不影响正文
            return f"正文已更新，图重画失败：{exc}"
    return f"已更新（{len(sections)} 节）"


def main() -> int:
    settings = Settings()
    if len(sys.argv) > 1:
        folders = [Path(sys.argv[1])]
    else:
        folders = sorted(p for p in Path(settings.output_dir).iterdir() if p.is_dir())

    done = 0
    for folder in folders:
        try:
            result = rebuild(folder)
        except Exception as exc:
            result = f"失败：{exc}"
        if result.startswith("已更新"):
            done += 1
        print(f"{result.ljust(18)} {folder.name}")
    print(f"\n共更新 {done} 篇")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
