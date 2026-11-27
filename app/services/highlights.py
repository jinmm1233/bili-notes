"""笔记高亮（标重点）：单独存 `highlights.json`，不写回 notes.md。

正文是模型生成的，重跑视频会整篇覆盖；高亮属于用户数据，必须另存。
锚点用「原文 + 前文 + 后文」而不是字符位置：文字有增删时还能重新找回来，
对不上的直接跳过——宁可少标一处，也不能标错位置。
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from ..core.config import Settings

HL_FILE = "highlights.json"
MAX_ITEMS = 1000          # 一篇笔记最多存这么多条，防止异常数据把文件撑爆
MAX_QUOTE = 400           # 单条高亮的原文上限
MAX_CTX = 80              # 前文 / 后文各留多少字做定位上下文


def _folder(settings: Settings, note_id: str) -> Path:
    """定位笔记文件夹；越界或笔记不存在都按 FileNotFoundError 处理。"""
    root = Path(settings.output_dir).resolve()
    folder = (root / note_id).resolve()
    if root not in folder.parents and folder != root:
        raise FileNotFoundError(note_id)
    if not (folder / "notes.md").is_file():
        raise FileNotFoundError(note_id)
    return folder


def _clean(item: Any) -> dict[str, Any] | None:
    """把前端传来的一条高亮收成固定结构；缺原文的直接丢掉。"""
    if not isinstance(item, dict):
        return None
    quote = str(item.get("quote") or "").strip()
    if not quote:
        return None
    hid = str(item.get("id") or "").strip() or uuid.uuid4().hex[:12]
    try:
        created = int(item.get("created") or 0)
    except (TypeError, ValueError):
        created = 0
    return {
        "id": hid[:40],
        "quote": quote[:MAX_QUOTE],
        # prefix 取尾部（紧挨着原文的那几十字才有定位价值）
        "prefix": str(item.get("prefix") or "")[-MAX_CTX:],
        "suffix": str(item.get("suffix") or "")[:MAX_CTX],
        "created": created,
    }


def load(settings: Settings, note_id: str) -> list[dict[str, Any]]:
    folder = _folder(settings, note_id)
    path = folder / HL_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    items = data.get("items") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return []
    out: list[dict[str, Any]] = []
    for raw in items[:MAX_ITEMS]:
        row = _clean(raw)
        if row:
            out.append(row)
    return out


def save(settings: Settings, note_id: str, items: list[Any] | None) -> list[dict[str, Any]]:
    folder = _folder(settings, note_id)
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in list(items or [])[:MAX_ITEMS]:
        row = _clean(raw)
        if not row or row["id"] in seen:
            continue
        seen.add(row["id"])
        out.append(row)

    path = folder / HL_FILE
    try:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps({"items": out}, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)                      # 先写临时文件再换名，避免写一半断电留下坏 JSON
    except OSError as exc:
        raise RuntimeError(f"高亮保存失败：{exc}") from exc
    return out
