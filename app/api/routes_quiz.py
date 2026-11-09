"""控制器层：自测卷的出题、交卷判分、错题转卡片。"""

from __future__ import annotations

import logging
import re
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from ..core.config import get_settings
from ..core.llm import LLMClient
from ..services import concepts as concept_svc
from ..services import export_pdf
from ..services import library
from ..services import quiz as quiz_svc
from ..services import quiz_export

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api")


class GenerateIn(BaseModel):
    note_ids: list[str] = Field(default_factory=list)
    collection: str = ""
    kinds: list[str] = Field(default_factory=lambda: list(quiz_svc.DEFAULT_KINDS))
    count: int = Field(default=10, ge=3, le=quiz_svc.MAX_QUESTIONS)
    title: str = ""


class SubmitIn(BaseModel):
    answers: dict[str, Any] = Field(default_factory=dict)


class CardsIn(BaseModel):
    ids: list[str] = Field(default_factory=list)


def _client(request: Request) -> LLMClient:
    client = LLMClient.from_settings(get_settings(), request.app.state.store)
    if client is None:
        raise HTTPException(400, "还没配置模型 Key，出不了题")
    return client


@router.get("/quiz")
def list_quizzes() -> dict[str, Any]:
    settings = get_settings()
    items = [quiz_svc.summary(q) for q in quiz_svc.load(settings)]
    return {"total": len(items), "items": items}


@router.get("/quiz/{quiz_id}")
def read_quiz(quiz_id: str) -> dict[str, Any]:
    """取一份卷子用于答题。**不下发答案**——答案只在交卷时给（简答的参考要点除外）。"""
    quiz = quiz_svc.get(get_settings(), quiz_id)
    if quiz is None:
        raise HTTPException(404, "没有这份卷子")
    return quiz_svc.summary(quiz, with_questions=True)


@router.post("/quiz/generate")
def generate_quiz(body: GenerateIn, request: Request) -> dict[str, Any]:
    """按选中的笔记/合集出题。题型由前端勾选，手机上建议只选单选 + 判断。"""
    settings = get_settings()
    kinds = [k for k in body.kinds if k in quiz_svc.KINDS] or list(quiz_svc.DEFAULT_KINDS)

    notes: list[dict[str, Any]] = []
    for note_id in body.note_ids[:12]:
        try:
            notes.append(library.get_note(settings, note_id))
        except (FileNotFoundError, ValueError):
            continue
    if not notes and body.collection:
        notes = [
            n for n in library.scan(settings)
            if (n.get("collection") or library.DEFAULT_COLLECTION) == body.collection
        ][:12]
    if not notes:
        raise HTTPException(400, "先选几篇笔记（或一个收藏夹）")

    title = (body.title or "").strip()
    if not title:
        if len(notes) == 1:
            title = str(notes[0].get("title") or "自测卷")
        else:
            name = notes[0].get("collection") or library.DEFAULT_COLLECTION
            title = f"{name} · {len(notes)} 篇"

    per_note = max(1, round(body.count / len(notes)))
    built: list[dict[str, Any]] = []
    client = _client(request)
    for note in notes:
        try:
            qs = quiz_svc.build(
                client, quiz_svc.source_md(note), str(note.get("title") or ""),
                kinds, min(per_note, body.count),
            )
        except Exception as exc:                      # 单篇失败不拖垮整份卷子
            log.warning("出题失败 note=%s: %s", note.get("id"), exc)
            qs = []
        if qs:
            built.append({"id": note.get("id"), "title": note.get("title"), "questions": qs})
        if sum(len(b["questions"]) for b in built) >= body.count:
            break

    total = sum(len(b["questions"]) for b in built)
    if not built:
        raise HTTPException(502, "模型没给出可用题目，换个题型或换篇笔记再试")
    if total < body.count:
        log.info("出题数不足：要 %d 实际 %d", body.count, total)

    quiz = quiz_svc.create(settings, built, kinds, body.count, title=title)
    log.info("生成自测卷 %s：%d 题（%s）", quiz["id"], len(quiz["questions"]), quiz["kinds"])
    return {"ok": True, **quiz_svc.summary(quiz, with_questions=True)}


@router.post("/quiz/{quiz_id}/submit")
def submit_quiz(quiz_id: str, body: SubmitIn) -> dict[str, Any]:
    """交卷：选择题/判断题/填空题自动判，简答题按用户自评。"""
    settings = get_settings()
    quiz = quiz_svc.get(settings, quiz_id)
    if quiz is None:
        raise HTTPException(404, "没有这份卷子")
    result = quiz_svc.grade(quiz, body.answers)
    quiz_svc.record_attempt(settings, quiz_id, result)
    return {**result, "review": quiz_svc.detail_for_review(quiz, body.answers)}


@router.post("/quiz/{quiz_id}/cards")
def wrong_to_cards(quiz_id: str, body: CardsIn) -> dict[str, Any]:
    """错题一键变卡片。concept 会先过一遍概念审核，避免把索引撑碎。"""
    settings = get_settings()
    quiz = quiz_svc.get(settings, quiz_id)
    if quiz is None:
        raise HTTPException(404, "没有这份卷子")
    known: list[str] = []
    for src in quiz.get("sources") or []:
        try:
            note = library.get_note(settings, src.get("id") or "")
        except (FileNotFoundError, ValueError):
            continue
        known += [name for name, _desc in concept_svc.parse_concepts(note["markdown"])]
    counts = quiz_svc.to_cards(settings, quiz, body.ids, known)
    return {"ok": True, **counts}


@router.delete("/quiz/{quiz_id}")
def delete_quiz(quiz_id: str) -> dict[str, Any]:
    return {"ok": quiz_svc.remove(get_settings(), quiz_id)}


# ---------------------------------------------------------------- 导出
# 打印页要题目 + 答案 + 解析，答题页为了防作弊是不下发的，所以单开一个只给
# "卷面"用的接口。同一台机器上本来就是本人，不存在要防的人。
@router.get("/quiz/{quiz_id}/sheet")
def quiz_sheet(quiz_id: str) -> dict[str, Any]:
    quiz = quiz_svc.get(get_settings(), quiz_id)
    if quiz is None:
        raise HTTPException(404, "没有这份卷子")
    sheet = quiz_svc.detail_for_review(quiz, {})
    return {
        **sheet,
        "created": quiz.get("created") or 0,
        "kinds": quiz.get("kinds") or [],
        "sources": quiz.get("sources") or [],
    }


def _safe(name: str) -> str:
    """卷子标题当文件名：去掉 Windows 保留字符，限长。"""
    text = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", str(name or "")).strip(" .")
    return (text or "自测卷")[:60]


@router.get("/quiz/{quiz_id}/export")
def export_quiz(quiz_id: str, request: Request, fmt: str = "pdf") -> FileResponse:
    """把卷子导出成 PDF 或 Word（含参考答案）。"""
    settings = get_settings()
    quiz = quiz_svc.get(settings, quiz_id)
    if quiz is None:
        raise HTTPException(404, "没有这份卷子")

    fmt = (fmt or "").lower()
    if fmt not in ("pdf", "docx"):
        raise HTTPException(400, "只支持 pdf 或 docx")

    out_dir = settings.cache_dir / "exports"
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"quiz-{_safe(quiz.get('title'))}"
    target = out_dir / f"{stem}.{fmt}"

    if fmt == "docx":
        try:
            quiz_export.export_docx_file(quiz, target)
        except Exception as exc:                      # 排版内核出错要说清是哪一步
            log.exception("卷子 Word 导出失败 quiz=%s", quiz_id)
            raise HTTPException(500, f"Word 导出失败：{str(exc)[:120]}") from exc
    else:
        url = quiz_export.export_pdf_url(str(request.base_url), quiz_id)
        if export_pdf.export(url, target) is None:
            raise HTTPException(
                503,
                "没找到 Edge/Chrome，生成不了 PDF。可以先导出 Word，"
                "或在卷子页面用 Ctrl+P 另存为 PDF。",
            )

    return FileResponse(
        str(target),
        media_type=(
            "application/pdf" if fmt == "pdf"
            else "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        ),
        filename=f"{stem}.{fmt}",
    )
