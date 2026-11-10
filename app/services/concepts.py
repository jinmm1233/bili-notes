"""跨视频概念索引：把每篇笔记里的「关键概念」汇总成一张概念网。

解决的是这个局面：58 集的内容被切成 58 篇笔记，同一个概念（比如「单位矩阵」）
在第 3、11、22 集都讲过，但你只能一篇篇翻。这里按概念名把讲过的所有地方聚起来，
顺带把卡片挂到概念上——复习时就能看到"这个概念我一共有几张卡"。

数据来源两处，按优先级合并：
  1. notes.md 里 `## 关键概念` 的表格（术语 | 解释）——这是主来源，带上下文解释；
  2. cards.json 里每张卡片的 concept 字段——即使笔记没写关键概念表，卡片也能撑起索引。

不建数据库：几百篇笔记全量扫描是毫秒级，起个缓存反而容易脏。
"""

from __future__ import annotations

import logging
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from ..core.config import Settings
from . import cards as card_svc
from . import library

log = logging.getLogger(__name__)

_HEAD = re.compile(r"^##\s*关键概念\s*$", re.M)
_NEXT_HEAD = re.compile(r"^##\s", re.M)
_STRIP = re.compile(r"[*`_~]+")
_TRAIL = re.compile(r"[\s：:，,。.；;、]+$")


def clean_term(raw: str) -> str:
    """术语清洗：去掉 markdown 装饰与尾部标点，压掉多余空白。"""
    text = _STRIP.sub("", str(raw or ""))
    text = re.sub(r"\s+", " ", text).strip()
    return _TRAIL.sub("", text)[:40]


def name_key(name: str) -> str:
    """归并用的键：忽略大小写、空格、常见中英标点差异。"""
    text = clean_term(name).casefold()
    text = re.sub(r"[\s\-_·・/]+", "", text)
    return text


def parse_concepts(note_md: str) -> list[tuple[str, str]]:
    """从一篇笔记里抽出 (术语, 解释)。没有这一节就返回空列表。"""
    text = note_md or ""
    match = _HEAD.search(text)
    if not match:
        return []
    rest = text[match.end():]
    nxt = _NEXT_HEAD.search(rest)
    block = rest[: nxt.start()] if nxt else rest

    rows: list[tuple[str, str]] = []
    seen: set[str] = set()
    for line in block.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 2:
            continue
        term, desc = cells[0], cells[1]
        if re.fullmatch(r":?-{2,}:?", term.replace(" ", "")):
            continue                       # |---|---| 分隔行
        if term in ("术语", "Term", "概念"):
            continue                       # 表头
        name = clean_term(term)
        key = name_key(name)
        if not key or key in seen:
            continue
        seen.add(key)
        desc = re.sub(r"\s+", " ", desc).strip()
        rows.append((name, desc[:200]))
    return rows


def _tag_snapshot(settings: Settings) -> dict[str, dict[str, Any]]:
    """取分类缓存。函数内 import 避免 concepts <-> concept_tags 循环依赖。"""
    from . import concept_tags

    return concept_tags.load(settings)


def _canonical(tags: dict[str, dict[str, Any]], name: str) -> str:
    """规范名。可能是一串（A 并进 B，B 后来又并进 C），跟着走到头。"""
    cur = name
    seen: set[str] = set()
    for _ in range(3):
        row = tags.get(name_key(cur)) or {}
        nxt = str(row.get("canonical") or "").strip()
        key = name_key(nxt)
        if not nxt or nxt == cur or not key or key in seen:
            break
        seen.add(key)
        cur = nxt
    return cur if cur != name else ""


def build(settings: Settings) -> list[dict[str, Any]]:
    """汇总所有笔记的概念，并按规范名合并。

    合并解决的是"同一件事写了三遍"：`IP`、`IP属于NP`、`整数规划` 都归到
    「整数规划问题（IP）」下面——`IP属于NP` 本身就成了一条内容（各集怎么讲），
    而不是一个并列的概念。规范名由 concept_tags 提供，没有就退回原名。
    """
    notes = library.scan(settings)
    root = Path(settings.output_dir)
    tags = _tag_snapshot(settings)

    # key（规范名）-> {"names": Counter, "notes": [...], "by_note": {}, "aliases": set}
    groups: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"names": Counter(), "notes": [], "by_note": {},
                 "aliases": set(), "tag_name": ""}
    )

    def resolve(name: str) -> tuple[str, str]:
        """原名 -> (规范名 key, 显示名)。"""
        canon = _canonical(tags, name)
        display = canon or name
        return name_key(display), display

    for note in notes:
        note_id = note["id"]
        path = root / note_id / "notes.md"
        try:
            md = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for name, desc in parse_concepts(md):
            key, display = resolve(name)
            if not key:
                continue
            group = groups[key]
            group["names"][display] += 1
            if not group["tag_name"] and name_key(name) in tags:
                group["tag_name"] = name
            if display != name:
                group["aliases"].add(name)
            entry = group["by_note"].get(note_id)
            if entry is None:
                entry = {
                    "id": note_id,
                    "title": note.get("title") or note_id,
                    "uploader": note.get("uploader") or "",
                    "collection": note.get("collection") or "",
                    "explanation": desc,
                    "mtime": note.get("mtime") or 0,
                }
                group["by_note"][note_id] = entry
                group["notes"].append(entry)
            elif desc and desc not in entry["explanation"]:
                # 同一篇笔记里两行讲同一件事：都留着，别丢信息
                entry["explanation"] = f"{entry['explanation']} ／ {desc}".strip(" ／")

    # 卡片：即使笔记没写概念表，卡片上的 concept 也算一次"出现过"
    card_count: Counter = Counter()
    card_notes: dict[str, set[str]] = defaultdict(set)
    for card in card_svc.load(settings):
        concept = clean_term(card.get("concept") or "")
        if not concept:
            continue
        key, display = resolve(concept)
        if not key:
            continue
        card_count[key] += 1
        card_notes[key].add(str(card.get("note_id") or ""))
        group = groups[key]
        group["names"][display] += 0        # 只登记名字，不增加笔记计数
        if not group["tag_name"] and name_key(concept) in tags:
            group["tag_name"] = concept
        if display != concept:
            group["aliases"].add(concept)

    items: list[dict[str, Any]] = []
    for key, group in groups.items():
        if not group["names"]:
            continue
        # 没有规范名时，同一个概念可能有多种写法（"单位矩阵" / "单位 矩阵"）：
        # 出现次数最多的胜出；平票取更短更干净的，别让带空格的变体当门面。
        names = group["names"]
        display = sorted(names.items(), key=lambda kv: (-kv[1], len(kv[0]), kv[0]))[0][0]
        items.append({
            "key": key,
            "name": display,
            "aliases": sorted(a for a in group["aliases"] if name_key(a) != key),
            # 分类信息可能挂在别名上（规范名本身没有 tag 条目）
            "tag_name": group["tag_name"] or display,
            "notes": list(group["notes"]),
            "note_count": len(group["notes"]),
            "cards": card_count.get(key, 0),
            "card_notes": len(card_notes.get(key, set())),
        })

    items.sort(key=lambda x: (-x["note_count"], -x["cards"], x["name"]))
    return items


def summary(settings: Settings) -> dict[str, Any]:
    items = build(settings)
    # 重点标记与自足释义来自 concept_explain 缓存（没有就当没判过）。
    # 没判过的概念排在已判过的后面，但不会被藏起来——用户可能还没点过补全。
    try:
        from . import concept_explain
        cache = concept_explain.load(settings)
    except Exception:                          # pragma: no cover - 缓存坏不该拖垮整页
        cache = {}

    out: list[dict[str, Any]] = []
    for i in items:
        row = cache.get(i["key"]) or {}
        out.append({
            "key": i["key"],
            "name": i["name"],
            "note_count": i["note_count"],
            "cards": i["cards"],
            "aliases": i.get("aliases") or [],
            "tag_name": i.get("tag_name") or i["name"],
            # 按视频分组用：这个概念主要来自哪篇笔记
            "primary_note": (i["notes"][0]["title"] if i["notes"] else ""),
            # core：模型判定这个概念在视频里是不是被讲透的重点概念
            # None = 还没判过
            "core": (bool(row.get("core")) if row.get("explain") else None),
            # explain：自足的一句释义（"材料中记为E"这种废话不算）
            "explain": (str(row.get("explain") or "") if row.get("explain") else ""),
        })
    # 排序：先按重点度，再按跨篇数、卡片数
    out.sort(key=lambda x: (
        0 if x["core"] else (1 if x["core"] is None else 2),
        -x["note_count"], -x["cards"], x["name"],
    ))
    return {"total": len(out), "items": out}


def detail(settings: Settings, key_or_name: str) -> dict[str, Any] | None:
    wanted = name_key(key_or_name)
    tags = _tag_snapshot(settings)
    for item in build(settings):
        alias_keys = {name_key(a) for a in item.get("aliases") or []}
        if item["key"] != wanted and wanted not in alias_keys:
            continue
        cards = []
        for card in card_svc.load(settings):
            concept = clean_term(card.get("concept") or "")
            canon = _canonical(tags, concept) or concept
            if name_key(canon) == item["key"]:
                cards.append({
                    "id": card.get("id"),
                    "front": card.get("front"),
                    "back": card.get("back"),
                    "kind": card.get("kind"),
                    "note_title": card.get("note_title"),
                    "concept": concept,
                })
        # 释义：优先用模型写的那份自足解释；没有就退回笔记表里的原话
        explain = ""
        core: bool | None = None
        try:
            from . import concept_explain
            row = concept_explain.get(settings, item["name"])
            if row.get("explain"):
                explain = str(row["explain"])
                core = bool(row.get("core"))
        except Exception:                      # pragma: no cover
            pass
        if not explain:
            for note in item["notes"]:
                if note.get("explanation"):
                    explain = str(note["explanation"])
                    break
        return {
            "key": item["key"],
            "name": item["name"],
            "aliases": item.get("aliases") or [],
            "notes": item["notes"],
            "cards": cards,
            "explain": explain,
            "core": core,
        }
    return None
