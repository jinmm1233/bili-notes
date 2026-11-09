"""知识库浏览、检索与总览统计。"""
from __future__ import annotations

import hashlib
import io
import logging
import os
import re
import subprocess
import sys
import time
import zipfile
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from ..core.config import get_settings
from ..core.errors import AppError
from ..core.llm import LLMClient
from ..services import export_docx, export_pdf, lecture, library
from ..services.task_manager import Task, TaskManager

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api")

_scan_cache: dict[str, Any] = {"items": [], "at": 0.0}
_SCAN_TTL = 5.0


def _notes(force: bool = False) -> list[dict[str, Any]]:
    now = time.time()
    if force or (now - _scan_cache["at"]) > _SCAN_TTL:
        _scan_cache["items"] = library.scan(get_settings())
        _scan_cache["at"] = now
    return _scan_cache["items"]


def invalidate_library() -> None:
    _scan_cache["at"] = 0.0


@router.get("/library")
def list_notes(
    refresh: bool = False, collection: str = "", sort: str = library.SORT_CREATED
) -> dict[str, Any]:
    """笔记列表。sort 决定“新”指什么：

    - `created`（默认）：按笔记生成时间，重排版/补结构不会打乱顺序；
    - `mtime`：按文件修改时间，刚动过的那篇排最前。

    缓存里存的是默认序，切换口径只是重排，不用重扫磁盘。
    """
    order = library.normalize_sort(sort)
    notes = _notes(force=refresh)
    visible = library.filter_by(notes, collection)
    # 大类永远返回全量统计，前端进到某个收藏夹里时只换右侧网格。
    # 合集卡片只按当前收藏夹里的笔记算，别把别的收藏夹的分集并进来。
    return {
        "items": library.sort_notes(visible, order),
        # 只在进了收藏夹时算合集：第一级只显示收藏夹大类，不需要这组数据，
        # 也算不出「跨收藏夹的同名系列」该归给谁。
        "series": library.series_groups(visible) if collection.strip() else [],
        "collections": library.collections(notes, library.custom_names(get_settings())),
        "stats": library.stats(notes),
        "collection": collection,
        "sort": order,
    }


class CollectionIn(BaseModel):
    name: str = Field(min_length=1, max_length=40)


class MoveIn(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=500)
    to: str = Field(min_length=1, max_length=40)


@router.post("/collections")
def create_collection(body: CollectionIn) -> dict[str, Any]:
    try:
        name = library.add_custom(get_settings(), body.name)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    invalidate_library()
    return {"ok": True, "name": name}


@router.delete("/collections")
def delete_collection(name: str = "") -> dict[str, Any]:
    try:
        result = library.remove_collection(get_settings(), name)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    invalidate_library()
    return {"ok": True, **result}


@router.post("/collections/move")
def move_notes(body: MoveIn) -> dict[str, Any]:
    try:
        result = library.move_notes(get_settings(), body.ids, body.to)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    invalidate_library()
    return {"ok": True, **result}


@router.get("/library/search")
def search_notes(
    q: str = "", limit: int = 30, collection: str = "", sort: str = library.SORT_CREATED
) -> dict[str, Any]:
    if not q.strip():
        return {"items": [], "query": q}
    items = library.search(get_settings(), q, limit=limit * 3 if collection else limit)
    items = library.sort_notes(library.filter_by(items, collection), sort)[:limit]
    return {"items": items, "query": q, "count": len(items), "collection": collection}


@router.post("/library/{note_id}/reveal")
def reveal_note(note_id: str) -> dict[str, Any]:
    root = Path(get_settings().output_dir).resolve()
    folder = (root / note_id).resolve()
    if root not in folder.parents and folder != root:
        raise HTTPException(403, "非法路径")
    if not folder.is_dir():
        raise HTTPException(404, "笔记目录不存在")
    try:
        if sys.platform == "win32":
            os.startfile(str(folder))  # noqa: S606
        elif sys.platform == "darwin":
            subprocess.run(["open", str(folder)], check=False)
        else:
            subprocess.run(["xdg-open", str(folder)], check=False)
        return {"ok": True}
    except OSError as exc:
        raise HTTPException(500, f"无法打开目录：{exc}") from exc


@router.get("/library/{note_id}")
def get_note(note_id: str) -> dict[str, Any]:
    try:
        return library.get_note(get_settings(), note_id)
    except FileNotFoundError as exc:
        raise HTTPException(404, "笔记不存在") from exc
    except ValueError as exc:
        raise HTTPException(403, "非法路径") from exc


@router.post("/library/{note_id}/lecture")
def make_lecture(note_id: str, request: Request) -> dict[str, Any]:
    """给已有笔记补生成「知识点讲义」。

    只读 notes.md（里面已含完整转录、画面要点、例题），不重跑视频、不碰关键帧，
    代价就是一次文本模型调用。前端在阅读器里点"生成讲义"走这里。
    """
    settings = get_settings()
    root = Path(settings.output_dir).resolve()
    folder = (root / note_id).resolve()
    if root not in folder.parents and folder != root:
        raise HTTPException(403, "非法路径")
    if not (folder / "notes.md").is_file():
        raise HTTPException(404, "笔记不存在")

    client = LLMClient.from_settings(settings, request.app.state.store)
    if client is None:
        raise HTTPException(400, "还没有配置文字模型 Key，先去设置里填一个")

    try:
        path = lecture.generate_for_dir(client, folder)
    except AppError as exc:
        raise HTTPException(502, f"讲义生成失败：{exc}") from exc
    except Exception as exc:                      # 模型抽风别把接口打炸
        log.warning("讲义生成失败 note=%s", note_id, exc_info=True)
        raise HTTPException(502, f"讲义生成失败：{str(exc)[:120]}") from exc

    if path is None:
        raise HTTPException(502, "模型没有返回可用内容，稍后再试")
    invalidate_library()
    text = path.read_text(encoding="utf-8")
    return {"ok": True, "chars": len(text), "model": client.model, "path": str(path)}


@router.get("/library/{note_id}/export")
def export_note(
    note_id: str,
    request: Request,
    fmt: str = "pdf",
    transcript: bool = False,
) -> FileResponse:
    """把笔记导出成 PDF 或 Word，给打印用。

    transcript=False（默认）不打印完整转录——那部分通常比正文长好几倍。
    PDF 走本机浏览器的无头打印（公式、图片所见即所得）；Word 走 python-docx，
    公式转成 Word 原生公式，出问题退回文本。
    """
    settings = get_settings()
    root = Path(settings.output_dir).resolve()
    folder = (root / note_id).resolve()
    if root not in folder.parents and folder != root:
        raise HTTPException(403, "非法路径")
    if not (folder / "notes.md").is_file():
        raise HTTPException(404, "笔记不存在")

    fmt = (fmt or "").lower()
    if fmt not in ("pdf", "docx"):
        raise HTTPException(400, "只支持 pdf 或 docx")

    safe = _safe_filename(note_id)
    out_dir = settings.cache_dir / "exports"
    out_dir.mkdir(parents=True, exist_ok=True)

    if fmt == "docx":
        target = out_dir / f"{safe}.docx"
        try:
            export_docx.export(folder, target, include_transcript=transcript)
        except Exception as exc:
            log.exception("Word 导出失败 note=%s", note_id)
            raise HTTPException(500, f"Word 导出失败：{str(exc)[:120]}") from exc
    else:
        target = out_dir / f"{safe}.pdf"
        url = export_pdf.print_url(str(request.base_url), note_id, transcript)
        result = export_pdf.export(url, target)
        if result is None:
            raise HTTPException(
                503,
                "没能生成 PDF：这台电脑上没找到 Edge/Chrome。"
                "可以在阅读器里点「打印」，用浏览器自带的\"另存为 PDF\"。",
            )

    _cleanup_exports(out_dir)
    filename = f"{safe}.{fmt}"
    return FileResponse(
        str(target),
        media_type=(
            "application/pdf" if fmt == "pdf"
            else "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        ),
        filename=filename,
    )


def _safe_filename(name: str) -> str:
    """文件名里不能有 Windows 保留字符，顺手截断避免超长路径。"""
    cleaned = re.sub(r'[\\/:*?"<>|\r\n]+', "_", name).strip(" .")
    return (cleaned or "note")[:80]


def _cleanup_exports(out_dir: Path, keep_hours: float = 24.0) -> None:
    """导出文件攒着没用，超过一天的删掉。"""
    try:
        now = time.time()
        for path in out_dir.glob("*"):
            if path.is_file() and now - path.stat().st_mtime > keep_hours * 3600:
                path.unlink(missing_ok=True)
    except OSError:
        pass


@router.delete("/library/{note_id}")
def delete_note(note_id: str) -> dict[str, Any]:
    """删除一篇笔记：移到回收站，之后去采集收藏可以重新处理这个视频。"""
    try:
        result = library.delete_note(get_settings(), note_id)
    except FileNotFoundError as exc:
        raise HTTPException(404, "笔记不存在") from exc
    except (ValueError, OSError) as exc:
        raise HTTPException(400, f"删除失败：{exc}") from exc
    invalidate_library()
    return {"ok": True, **result}


@router.get("/library/{note_id}/file/{rel_path:path}")
def serve_note_file(note_id: str, rel_path: str) -> FileResponse:
    """为阅读器提供笔记内的图片等附属文件。"""
    root = Path(get_settings().output_dir).resolve()
    folder = (root / note_id).resolve()
    if root not in folder.parents and folder != root:
        raise HTTPException(403, "非法路径")
    target = (folder / rel_path).resolve()
    if folder not in target.parents and target != folder:
        raise HTTPException(403, "非法路径")
    if not target.is_file():
        raise HTTPException(404, "文件不存在")
    return FileResponse(target)


@router.get("/library/{note_id}/download")
def download_note(note_id: str) -> StreamingResponse:
    """把单篇笔记连同图片打包成 zip。"""
    root = Path(get_settings().output_dir).resolve()
    folder = (root / note_id).resolve()
    if root not in folder.parents and folder != root:
        raise HTTPException(403, "非法路径")
    if not folder.is_dir():
        raise HTTPException(404, "笔记不存在")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in folder.rglob("*"):
            if path.is_file():
                zf.write(path, path.relative_to(folder))
    buf.seek(0)
    filename = quote(f"{note_id}.zip")
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{filename}"},
    )


_IMAGE_HOSTS = (".hdslb.com", ".bilibili.com", ".bilivideo.com")

# 图片代理复用同一个连接池：收藏夹一页 20+ 张封面，每张都新建 TLS 连接既慢又容易
# 被图床当成异常流量拒掉，重试时还得重新握手。
_IMG_CLIENT = httpx.Client(
    timeout=httpx.Timeout(12.0, connect=6.0),
    follow_redirects=True,
    limits=httpx.Limits(max_connections=16, max_keepalive_connections=10),
)
_IMG_HEADERS = {
    "Referer": "https://www.bilibili.com/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
}
_COVER_TTL = 7 * 24 * 3600   # 封面一周内不会变，缓存久一点少打图床
_COVER_DIR_NAME = "covers"


def _cover_cache_path(key: str) -> Path:
    folder = get_settings().cache_dir / _COVER_DIR_NAME
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{key}.jpg"


def _read_cover_cache(path: Path) -> bytes | None:
    try:
        if path.exists() and (time.time() - path.stat().st_mtime) < _COVER_TTL:
            data = path.read_bytes()
            return data or None
    except OSError:
        pass
    return None


def _write_cover_cache(path: Path, data: bytes) -> None:
    """先写临时文件再改名，避免并发读到半张图。"""
    try:
        tmp = path.with_suffix(".part")
        tmp.write_bytes(data)
        tmp.replace(path)
    except OSError:
        pass  # 缓存写不进去不影响返回


@router.get("/proxy/image")
def proxy_image(url: str, w: int = 0) -> Response:
    """代理 B站图片。

    浏览器直接加载会被 ORB（Opaque Response Blocking）拦掉 ——
    跨源、no-cors 请求的响应如果缺少 CORS 头就会被 Chromium 判定为不透明并阻止，
    表现出来就是封面全白。走一次后端转发最省事，顺带补上 Referer。

    图床对突发并发会零星拒单（表现为个别封面一直空白），所以：
      1. 连接池复用，别再一张一个新连接；
      2. 失败重试一次；
      3. 成功结果落到本地缓存，翻页/重进直接命中，不再打扰图床。
    """
    if not url.startswith(("http://", "https://")):
        raise HTTPException(400, "非法地址")

    host = httpx.URL(url).host or ""
    if not any(host.endswith(suffix) for suffix in _IMAGE_HOSTS):
        raise HTTPException(403, "只允许代理 B站图床")

    key = hashlib.sha1(f"{url}|{w}".encode()).hexdigest()[:32]
    cache_path = _cover_cache_path(key)
    cached = _read_cover_cache(cache_path)
    if cached is not None:
        return Response(
            content=cached,
            media_type="image/jpeg",
            headers={"Cache-Control": "public, max-age=604800", "X-Cover-Cache": "hit"},
        )

    content: bytes | None = None
    last_error = ""
    for attempt in range(2):                      # 偶发拒单重试一次就够了
        try:
            resp = _IMG_CLIENT.get(url, headers=_IMG_HEADERS)
            resp.raise_for_status()
            media_type = resp.headers.get("content-type", "image/jpeg")
            if not media_type.startswith("image/"):
                raise HTTPException(502, "返回的不是图片")
            content = _shrink(resp.content, w)
            break
        except HTTPException:
            raise
        except httpx.HTTPError as exc:
            last_error = str(exc)[:80]
            if attempt == 0:
                time.sleep(0.35)

    if content is None:
        raise HTTPException(502, f"图片获取失败：{last_error}")

    _write_cover_cache(cache_path, content)
    return Response(
        content=content,
        media_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=604800", "X-Cover-Cache": "miss"},
    )


def _shrink(content: bytes, w: int) -> bytes:
    """把封面缩到指定宽度。

    原本想用 B站图床的 @480w.jpg 参数，但 httpx 会对 URL 做规范化，
    @ 符号传过去变成 404（curl 反而正常）。自己缩更可靠，也适用于任何图床。
    """
    if not w or w <= 0:
        return content
    try:
        from io import BytesIO

        from PIL import Image  # noqa: PLC0415

        with Image.open(BytesIO(content)) as img:
            if img.width <= w:
                return content
            ratio = w / img.width
            resized = img.convert("RGB").resize(
                (w, max(1, int(img.height * ratio))), Image.LANCZOS
            )
            buf = BytesIO()
            resized.save(buf, format="JPEG", quality=82, optimize=True)
            return buf.getvalue()
    except Exception:
        return content


@router.get("/overview")
def overview(request: Request) -> dict[str, Any]:
    manager: TaskManager = request.app.state.manager
    tasks: list[Task] = manager.list()
    notes = _notes()

    def count(status: str) -> int:
        return sum(1 for t in tasks if t.status == status)

    return {
        "tasks": {
            "total": len(tasks),
            "running": count("running") + count("queued"),
            "done": count("done"),
            "failed": count("failed"),
        },
        "library": library.stats(notes),
        "recent_tasks": [t.to_dict() for t in tasks[:6]],
        "recent_notes": notes[:6],
    }
