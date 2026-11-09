"""英语词库接口：导入、列表、删除。

导入走 multipart 文件上传（python-multipart 已在依赖里）。
坏行不静默丢弃：返回行号与原因，前端直接显示给人改。
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field

from ..core.config import get_settings
from ..core.llm import LLMClient
from ..services import wordbook as book_svc
from ..services import words
from .routes_library import invalidate_library

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api")


def _client(request: Request) -> LLMClient:
    client = LLMClient.from_settings(get_settings(), request.app.state.store)
    if client is None:
        raise HTTPException(400, "还没配置模型 Key，补不了释义")
    return client


@router.get("/wordbooks")
def list_wordbooks() -> dict[str, Any]:
    """词库清单 + 合并后的词条总数（前端用来显示"已导入 N 词"）。"""
    return book_svc.summary(get_settings())


@router.post("/wordbooks")
async def upload_wordbook(
    file: UploadFile = File(...),          # noqa: B008
    name: str = Form(""),                  # noqa: B008
    keep_missing: str = Form("0"),         # noqa: B008
) -> dict[str, Any]:
    """导入一份 CSV / TXT / JSON 词库。

    name 留空就用文件名当词库名；同名的会覆盖旧词库（便于"改完重新导入"）。
    keep_missing=1 时，只有单词没有释义的行也收进来（之后用「AI 补释义」回填）。
    """
    settings = get_settings()
    raw = await file.read()
    if not raw:
        raise HTTPException(400, "文件是空的")
    if len(raw) > book_svc.MAX_BYTES:
        raise HTTPException(400, f"文件太大了（{len(raw) // 1048576}MB），上限 5MB")

    parsed = book_svc.parse(raw, file.filename or "", keep_missing=keep_missing == "1")
    words = parsed.get("words") or []
    if not words:
        detail = "没解析出任何词条"
        if parsed.get("errors"):
            first = parsed["errors"][0]
            detail += f"：第 {first['line']} 行 {first['why']}"
        raise HTTPException(400, detail)

    book = book_svc.save(
        settings,
        parsed,
        name=name or Path(file.filename or "").stem,
        source=file.filename or "",
    )
    log.info("词库 %s 导入完成：%d 词", book["name"], book["count"])
    return {
        "ok": True,
        "book": book,
        "parsed": {
            "count": len(words),
            "skipped": parsed.get("skipped", 0),
            "format": parsed.get("format", ""),
            "errors": parsed.get("errors", []),
            "missing": parsed.get("missing", 0),
        },
        "summary": book_svc.summary(settings),
    }


class FillIn(BaseModel):
    book_id: str = Field(default="")


@router.get("/wordbooks/missing")
def wordbooks_missing(book_id: str = "") -> dict[str, Any]:
    """还剩多少个词没释义（前端显示进度/决定要不要再点一次）。"""
    return {"missing": book_svc.missing_count(get_settings(), book_id or None)}


@router.post("/wordbooks/to-cards")
def wordbooks_to_cards() -> dict[str, Any]:
    """把笔记里遇到过的生词变成单词卡，进复习队列（正面单词、背面释义）。"""
    result = book_svc.to_cards(get_settings())
    if result.get("added"):
        invalidate_library()
    return {"ok": True, **result}


@router.post("/wordbooks/fill")
def wordbooks_fill(body: FillIn, request: Request) -> dict[str, Any]:
    """AI 批量补释义。一次最多 12 批（360 词），没跑完的 remaining 会告诉前端。"""
    settings = get_settings()
    if not book_svc.load_all(settings):
        raise HTTPException(400, "还没有导入词库")
    remaining_before = book_svc.missing_count(settings, body.book_id or None)
    if not remaining_before:
        return {"ok": True, "filled": 0, "failed": 0, "remaining": 0, "batches": 0}
    result = book_svc.fill_definitions(settings, _client(request), body.book_id or None)
    # 释义补上了，笔记里「词库无释义」的占位要重算才看得到
    if result["filled"]:
        try:
            _rescan_notes(settings)
        except Exception as exc:                    # 重算失败不影响补释义本身
            log.warning("补释义后重算生词段失败：%s", exc)
    return {"ok": True, **result}


@router.delete("/wordbooks/{book_id}")
def delete_wordbook(book_id: str) -> dict[str, Any]:
    settings = get_settings()
    if not book_svc.delete(settings, book_id):
        raise HTTPException(404, "没有这个词库")
    return {"ok": True, "summary": book_svc.summary(settings)}


@router.post("/wordbooks/rescan")
def rescan_notes() -> dict[str, Any]:
    """把已有笔记按当前词库重算一遍生词段。

    导入词库之前生成的笔记不会有标注，用户又不该为此去命令行跑脚本，
    所以给个一键入口。只动外语笔记；没有命中就一个字符都不改。
    """
    settings = get_settings()
    if not book_svc.index(settings):
        raise HTTPException(400, "还没有导入词库")
    result = _rescan_notes(settings)
    return {"ok": True, **result}


def _rescan_notes(settings) -> dict[str, Any]:
    """重算所有外语笔记的生词段（补完释义后也要调一次，去掉「词库无释义」占位）。"""
    index = book_svc.index(settings)
    root = Path(settings.output_dir)
    changed = skipped = total_words = 0
    changed = skipped = total_words = 0
    for folder in sorted(root.iterdir()):
        md_path = folder / "notes.md"
        if not md_path.is_file():
            continue
        try:
            md = md_path.read_text(encoding="utf-8")
        except OSError:
            continue
        if not words.note_is_foreign(md):
            skipped += 1
            continue
        new_md, stats = words.annotate(md, index)
        if not stats.get("hits") or new_md == md:
            skipped += 1
            continue
        md_path.write_text(new_md, encoding="utf-8", newline="\n")
        (folder / "words.json").write_text(
            json.dumps({"generated_at": time.time(), "books": stats["books"],
                        "words": stats["words"]}, ensure_ascii=False, indent=1),
            encoding="utf-8", newline="\n",
        )
        changed += 1
        total_words += stats["hits"]

    invalidate_library()                     # 列表缓存里有旧的统计
    log.info("重算生词：更新 %d 篇，跳过 %d 篇", changed, skipped)
    return {"changed": changed, "skipped": skipped,
            "words": total_words, "books": [b["name"] for b in book_svc.load_all(settings)]}
