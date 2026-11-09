"""控制器层：知识卡片的生成、复习与统计。业务规则在 services/cards.py。"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from ..core.config import get_settings
from ..core.errors import AppError
from ..core.llm import LLMClient
from ..services import cards as card_svc
from ..services import quiz as quiz_svc
from ..services import concepts, library

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api")


class GenerateIn(BaseModel):
    note_id: str = Field(min_length=1, max_length=200)


class ReviewIn(BaseModel):
    grade: str = Field(pattern="^(again|hard|good|easy)$")


def _client(request: Request) -> LLMClient:
    client = LLMClient.from_settings(get_settings(), request.app.state.store)
    if client is None:
        raise HTTPException(400, "还没有配置文字模型 Key，先去设置里填一个")
    return client


def _scope_note_ids(collection: str | None, note_ids: str | None) -> set[str] | None:
    """把「收藏夹 / 逗号分隔的笔记 ID」翻译成允许的笔记集合。

    返回 None 表示不筛选（全部）。两个条件都给时取交集，
    这样「选中某几篇」和「只看某个收藏夹」不会互相打架。
    """
    ids: set[str] | None = None
    if collection:
        settings = get_settings()
        ids = {
            n["id"] for n in library.scan(settings)
            if (n.get("collection") or library.DEFAULT_COLLECTION) == collection
        }
    if note_ids:
        wanted = {x.strip() for x in note_ids.split(",") if x.strip()}
        ids = wanted if ids is None else (ids & wanted)
    return ids


@router.get("/cards/stats")
def card_stats(collection: str | None = None, note_ids: str | None = None) -> dict[str, Any]:
    return card_svc.stats(get_settings(), note_ids=_scope_note_ids(collection, note_ids))


@router.get("/cards/due")
def cards_due(limit: int = 30, collection: str | None = None,
              note_ids: str | None = None, mode: str = "due") -> dict[str, Any]:
    """到期的卡片。mode=all 时忽略到期时间，把这个范围全部过一遍。"""
    settings = get_settings()
    limit = max(1, min(limit, 200))
    scope = _scope_note_ids(collection, note_ids)
    items = card_svc.due(settings, limit, note_ids=scope,
                         include_future=(mode == "all"))
    # 每张卡带上"按各档会排到多久"，前端直接显示在按钮上
    for card in items:
        card["preview"] = card_svc.preview(card)
    st = card_svc.stats(settings, note_ids=scope)
    return {
        "cards": items,
        "total_due": st["due"],
        "total": st["total"],
        "mode": "all" if mode == "all" else "due",
    }


@router.get("/cards/groups")
def card_groups() -> dict[str, Any]:
    """按收藏夹 → 笔记分组，给出每组的卡片数与待复习数。

    卡片本身不存收藏夹（笔记搬家后就不准了），每次按笔记当前的归属现算。
    """
    settings = get_settings()
    notes = library.scan(settings)
    counts = card_svc.per_note(settings)
    groups: dict[str, dict[str, Any]] = {}
    seen: set[str] = set()

    for note in notes:
        note_id = note["id"]
        seen.add(note_id)
        name = note.get("collection") or library.DEFAULT_COLLECTION
        row = counts.get(note_id) or {"total": 0, "due": 0}
        group = groups.setdefault(name, {
            "name": name, "cards": 0, "due": 0, "notes": [],
        })
        group["cards"] += row["total"]
        group["due"] += row["due"]
        group["notes"].append({
            "id": note_id,
            "title": note.get("title") or note_id,
            "uploader": note.get("uploader") or "",
            "cards": row["total"],
            "due": row["due"],
            "mtime": note.get("mtime") or 0,
        })

    # 自测卷的错题卡挂在 quiz:<id> 这个虚拟笔记下，复习页给它们单独一组
    quiz_titles = quiz_svc.title_map(settings)
    quiz_ids = [nid for nid in counts if str(nid).startswith(quiz_svc.QUIZ_NOTE_PREFIX)]
    if quiz_ids:
        group = groups.setdefault("自测卷", {
            "name": "自测卷", "cards": 0, "due": 0, "notes": [],
        })
        for note_id in quiz_ids:
            row = counts[note_id]
            group["cards"] += row["total"]
            group["due"] += row["due"]
            group["notes"].append({
                "id": note_id,
                "title": quiz_titles.get(note_id, "自测卷"),
                "uploader": "",
                "cards": row["total"], "due": row["due"], "mtime": 0,
            })

    # 笔记删了但卡片还在（历史数据），单独归一组，免得这些问题凭空消失
    orphans = [nid for nid in counts if nid not in seen and nid not in set(quiz_ids)]
    if orphans:
        group = groups.setdefault("已删除的笔记", {
            "name": "已删除的笔记", "cards": 0, "due": 0, "notes": [],
        })
        for note_id in orphans:
            row = counts[note_id]
            group["cards"] += row["total"]
            group["due"] += row["due"]
            group["notes"].append({
                "id": note_id, "title": note_id, "uploader": "",
                "cards": row["total"], "due": row["due"], "mtime": 0,
            })

    items = []
    for group in groups.values():
        if not group["cards"]:
            continue                      # 没生成过卡片的笔记不占位置
        group["notes"].sort(key=lambda n: (-n["due"], -n["cards"], -n["mtime"]))
        items.append(group)
    items.sort(key=lambda g: (g["name"] != library.DEFAULT_COLLECTION, -g["cards"], g["name"]))

    total = card_svc.stats(settings)
    return {"groups": items, "total": total["total"], "due": total["due"]}


@router.get("/cards/list")
def cards_list(note_id: str | None = None, collection: str | None = None,
               limit: int = 400) -> dict[str, Any]:
    """浏览用：列出范围内所有卡片（不看到期时间）。"""
    scope = _scope_note_ids(collection, note_id)
    if note_id and not collection:
        scope = {note_id}
    items = card_svc.list_cards(get_settings(), note_ids=scope,
                                limit=max(1, min(limit, 1000)))
    return {"cards": items, "total": len(items)}


@router.get("/cards/sources")
def card_sources() -> dict[str, Any]:
    """每篇笔记的卡片情况 + 是否已经生成过，供「卡片来源」列表使用。"""
    settings = get_settings()
    counts = card_svc.per_note(settings)
    rows = []
    for note in library.scan(settings):
        row = counts.get(note["id"]) or {"total": 0, "due": 0}
        rows.append({
            "id": note["id"],
            "title": note["title"],
            "uploader": note.get("uploader") or "",
            "collection": note.get("collection") or library.DEFAULT_COLLECTION,
            "cards": row["total"],
            "due": row["due"],
            "mtime": note.get("mtime") or 0,
        })
    return {"notes": rows}


@router.post("/cards/generate")
def cards_generate(body: GenerateIn, request: Request) -> dict[str, Any]:
    """给一篇已有笔记生成卡片。只读 notes.md，不重跑视频。"""
    settings = get_settings()
    client = _client(request)
    try:
        note = library.get_note(settings, body.note_id)
    except FileNotFoundError as exc:
        raise HTTPException(404, "笔记不存在") from exc
    except ValueError as exc:
        raise HTTPException(403, "非法路径") from exc

    # 让模型复用笔记里的概念名、别重复已有题目：这两个约束直接决定
    # 卡片在概念页能不能归到一组、以及反复点"补充"会不会堆重复题。
    known = [name for name, _desc in concepts.parse_concepts(note["markdown"])]
    existing = card_svc.fronts_for(settings, body.note_id)
    try:
        items = card_svc.build(client, note["markdown"], note["title"],
                               known_concepts=known, existing_fronts=existing)
    except AppError as exc:
        raise HTTPException(502, f"生成失败：{exc}") from exc
    except Exception as exc:                       # 模型抽风不炸接口
        log.warning("生成卡片失败 note=%s", body.note_id, exc_info=True)
        raise HTTPException(502, f"生成失败：{str(exc)[:120]}") from exc

    if not items:
        raise HTTPException(502, "模型没有给出可用卡片，稍后再试")

    added, dup = card_svc.add(settings, body.note_id, note["title"], items)
    log.info("卡片生成 note=%s 新增 %d 重复 %d", body.note_id, added, dup)
    return {"ok": True, "created": added, "duplicate": dup, "model": client.model}


@router.post("/cards/clean-examples")
def cards_clean_examples(dry: bool = True) -> dict[str, Any]:
    """清理"例题卡"：答案在复述某道题的具体系数（第一行乘 1/3、第二行乘 -1…）。

    复习卡该考"换个例子还成立"的知识；具体题目归自测卷。
    dry=1（默认）只报告，dry=0 才删。
    """
    return card_svc.clean_examples(get_settings(), dry_run=dry)


@router.post("/cards/dedupe")
def cards_dedupe(dry: bool = True) -> dict[str, Any]:
    """查重：同一篇笔记里"换个说法问同一件事"的卡片。

    dry=1（默认）只报告不删；dry=0 才真删，保留复习次数多的那张。
    旧版本「补充卡片」不看已有题目，堆了一批同义题，这里是历史数据兜底。
    """
    return card_svc.dedupe(get_settings(), dry_run=dry)


@router.post("/cards/{card_id}/reset")
def cards_reset(card_id: str) -> dict[str, Any]:
    """把一张卡提到"现在到期"，卡片浏览器里点「现在复习」用。"""
    card = card_svc.reset(get_settings(), card_id)
    if card is None:
        raise HTTPException(404, "卡片不存在")
    return {"ok": True, "card": card}


@router.post("/cards/{card_id}/review")
def cards_review(card_id: str, body: ReviewIn) -> dict[str, Any]:
    card = card_svc.review(get_settings(), card_id, body.grade)
    if card is None:
        raise HTTPException(404, "卡片不存在")
    return {"ok": True, "card": card, "stats": card_svc.stats(get_settings())}


@router.delete("/cards/{card_id}")
def cards_delete(card_id: str) -> dict[str, Any]:
    if not card_svc.remove(get_settings(), card_id):
        raise HTTPException(404, "卡片不存在")
    return {"ok": True}


@router.delete("/cards/note/{note_id}")
def cards_delete_note(note_id: str) -> dict[str, Any]:
    removed = card_svc.remove_note(get_settings(), note_id)
    return {"ok": True, "removed": removed}
