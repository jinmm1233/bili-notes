"""离线学习包：把笔记/卡片/自测卷打成一个包，手机上配对一次之后离线可用。

为什么不是"发一个 HTML 文件"：
  iOS Safari 只能把 http(s) 页面"添加到主屏幕"，本地文件打开受限；而 Service Worker
  也只能在 http(s) 下注册。所以做成 PWA：手机在同一个 WiFi 访问一次 `/offline`，
  注册 SW 把页面、公式、图片、数据全缓存下来；之后电脑关机、换个网络，照样能打开。

数据：`GET /api/offline/bundle` 一次给全（含自测卷答案——离线判分必须在本地）。
进度：手机上的评分/成绩存在 localStorage，回到电脑旁打开页面时自动 POST 回来合并。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..core.config import get_settings
from ..services import cards as card_svc
from ..services import library
from ..services import offline_pack
from ..services import quiz as quiz_svc

log = logging.getLogger(__name__)

router = APIRouter()          # /offline 和 /sw.js 要在根路径下，不能带 /api 前缀
api = APIRouter(prefix="/api")


@router.get("/offline")
def offline_page():
    from fastapi.responses import FileResponse  # noqa: PLC0415

    path = get_settings().static_dir / "offline.html"
    if not path.is_file():
        raise HTTPException(404, "离线页没打包进来")
    return FileResponse(path, headers={"cache-control": "no-cache"})


@router.get("/sw.js")
def service_worker():
    """Service Worker 必须从根路径下发，否则 scope 只能到它所在目录。"""
    from fastapi.responses import FileResponse  # noqa: PLC0415

    path = get_settings().static_dir / "sw.js"
    if not path.is_file():
        raise HTTPException(404, "sw.js 缺失")
    return FileResponse(path, media_type="application/javascript",
                        headers={"cache-control": "no-cache", "service-worker-allowed": "/"})


def _card_view(card: dict[str, Any]) -> dict[str, Any]:
    keys = ("id", "note_id", "note_title", "front", "back", "concept", "kind",
            "due", "ease", "interval", "reps", "lapses", "last")
    return {k: card.get(k) for k in keys}


def _quiz_full(quiz: dict[str, Any]) -> dict[str, Any]:
    """离线判分必须有答案，所以这里把 answer 一起给出去——
    本地缓存里本来就有全文，藏答案没有意义，能离线做题才有意义。"""
    return {
        "id": quiz["id"],
        "title": quiz.get("title") or "",
        "kinds": quiz.get("kinds") or [],
        "created": quiz.get("created") or 0,
        "questions": [
            {
                "id": q["id"], "kind": q["kind"], "stem": q["stem"],
                "options": q.get("options") or [], "answer": q.get("answer"),
                "explain": q.get("explain") or "", "concept": q.get("concept") or "",
                "answer_text": quiz_svc._answer_text(q),
            }
            for q in (quiz.get("questions") or [])
        ],
    }


@api.get("/offline/bundle")
def offline_bundle() -> dict[str, Any]:
    """手机端一次拉全。笔记正文里图片是相对路径，前端按 note id 拼成 /api/library/.../file/。"""
    settings = get_settings()
    notes = []
    for note in library.scan(settings):
        notes.append({
            "id": note["id"],
            "title": note.get("title") or note["id"],
            "uploader": note.get("uploader") or "",
            "collection": note.get("collection") or library.DEFAULT_COLLECTION,
            "mtime": note.get("mtime") or 0,
            "markdown": note.get("markdown") or "",
        })
    quizzes = [_quiz_full(q) for q in quiz_svc.load(settings)]
    return {
        "generated": __import__("time").time(),
        "notes": notes,
        "cards": [_card_view(c) for c in card_svc.load(settings)],
        "quizzes": quizzes,
        "counts": {"notes": len(notes), "cards": len(card_svc.load(settings)),
                   "quizzes": len(quizzes)},
    }


class ProgressIn(BaseModel):
    cards: dict[str, dict[str, Any]] = Field(default_factory=dict)
    quizzes: list[dict[str, Any]] = Field(default_factory=list)


@api.get("/offline/export")
def offline_export():
    """导出自包含 HTML：数据、图片、公式全内联，手机打开就能离线用。

    这是"电脑不在身边也能复习"的真正解法——微信发个文件过去就行，
    不用同一 WiFi、不用电脑开机。
    """
    from fastapi.responses import Response  # noqa: PLC0415

    html = offline_pack.build_html(get_settings())
    # 文件名固定，别带日期：手机上同一个文件反复覆盖时，localStorage 里的
    # 复习进度才对得上（换个文件名等于换了个"网站"，进度就断了）
    name = "BiliNotes-pack.html"
    log.info("导出学习包 %s，%.1f MB", name, len(html.encode("utf-8")) / 1048576)
    return Response(
        html,
        media_type="text/html; charset=utf-8",
        headers={"content-disposition": f'attachment; filename="{name}"'},
    )


@api.post("/offline/progress")
def offline_progress(body: ProgressIn) -> dict[str, Any]:
    """把手机上的复习进度并回来。

    只覆盖排期相关字段（due/ease/interval/reps/lapses/last），卡片内容不动——
    手机上不可能改题面，能变的只有"复习到哪了"。
    """
    settings = get_settings()
    cards = card_svc.load(settings)
    by_id = {c["id"]: c for c in cards}
    fields = ("due", "ease", "interval", "reps", "lapses", "last")
    touched = 0
    for card_id, row in (body.cards or {}).items():
        card = by_id.get(card_id)
        if not card:
            continue                    # 电脑上已经删掉的卡，不复活
        for key in fields:
            if key in row and isinstance(row[key], (int, float)):
                card[key] = row[key]
        touched += 1
    if touched:
        card_svc.save(settings, cards)

    quizzes = quiz_svc.load(settings)
    known = {q["id"]: q for q in quizzes}
    added = 0
    for row in body.quizzes or []:
        quiz = known.get(str(row.get("quiz_id") or ""))
        if quiz is None:
            continue
        at = float(row.get("at") or 0)
        seen = {float(a.get("at") or 0) for a in quiz.get("attempts") or []}
        if not at or at in seen:
            continue                    # 同一次交卷重复同步不重复记
        quiz.setdefault("attempts", []).append({
            "at": at,
            "score": int(row.get("score") or 0),
            "total": int(row.get("total") or 0),
            "wrong": list(row.get("wrong") or [])[:50],
            "from": "offline",
        })
        quiz["attempts"] = quiz["attempts"][-20:]
        added += 1
    if added:
        quiz_svc.save(settings, quizzes)

    log.info("离线进度同步：卡片 %d 张，成绩 %d 条", touched, added)
    return {"ok": True, "cards": touched, "quizzes": added}
