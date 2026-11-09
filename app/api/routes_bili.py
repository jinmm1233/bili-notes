"""B站账号与收藏夹相关接口。"""
from __future__ import annotations

import logging
import re
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, model_validator

from ..core.config import get_settings
from ..core.errors import AuthRequiredError
from ..services import bili_auth, favorites, library, series as series_svc
from ..services.task_manager import TaskManager

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api")

# 登录态缓存：nav 接口是外部请求，前端轮询不该每次都打过去
_state_cache: dict[str, Any] = {"state": None, "at": 0.0}
_CACHE_TTL = 30.0


def _state(force: bool = False) -> bili_auth.LoginState:
    now = time.time()
    if force or _state_cache["state"] is None or (now - _state_cache["at"]) > _CACHE_TTL:
        _state_cache["state"] = bili_auth.check_login(get_settings())
        _state_cache["at"] = now
    return _state_cache["state"]


def _invalidate() -> None:
    _state_cache["state"] = None
    _state_cache["at"] = 0.0


class ImportCountIn(BaseModel):
    media_id: int
    limit: int = Field(default=20, ge=1, le=100)
    frames: bool = True
    ocr: bool = True
    summary: bool = True
    lecture: bool = False          # 同时生成知识点讲义
    provider: str | None = None
    skip_done: bool = True
    expand_series: bool = False   # 展开多P / 合集，把系列按集数排进队列
    series_limit: int = Field(default=0, ge=0, le=200)  # 每个合集最多取几集，0=全部
    collection: str = ""           # 收藏夹名，写进笔记供知识库归类


class EpisodePick(BaseModel):
    """用户手动挑的一集：合集里每集是独立 BV，多P则是同一个 BV 的不同 p。"""
    bvid: str = Field(min_length=4, max_length=24)
    page: int = Field(default=1, ge=1, le=5000)


class ImportIn(BaseModel):
    bvids: list[str] = Field(default_factory=list, max_length=200)
    episodes: list[EpisodePick] = Field(default_factory=list, max_length=500)
    frames: bool = True
    ocr: bool = True
    summary: bool = True
    lecture: bool = False          # 同时生成知识点讲义
    provider: str | None = None
    skip_done: bool = True
    collection: str = ""           # 收藏夹名，写进笔记供知识库归类
    expand_series: bool = False   # 勾选后按系列展开（受 series_limit 限制）
    series_limit: int = Field(default=0, ge=0, le=200)
    media_id: int | None = None    # 供后端反查收藏夹名（前端没传 collection 时用）

    @model_validator(mode="after")
    def _need_something(self) -> "ImportIn":
        """要么给 bvids，要么给 episodes；两个都空说明前端传错了，直接拒绝。"""
        if not self.bvids and not self.episodes:
            raise ValueError("至少要选一个视频或一集")
        return self


# ---------- 登录 ----------
@router.post("/auth/qrcode")
def create_qrcode() -> dict[str, Any]:
    return bili_auth.generate_qrcode()


@router.get("/auth/qrcode/poll")
def poll_qrcode(key: str) -> dict[str, Any]:
    if not key:
        raise HTTPException(400, "缺少二维码 key")
    result = bili_auth.poll_qrcode(get_settings(), key)
    if result.get("logged_in"):
        _invalidate()
    return result


@router.get("/auth/status")
def auth_status() -> dict[str, Any]:
    return _state(force=True).to_dict()


@router.post("/auth/logout")
def auth_logout() -> dict[str, Any]:
    bili_auth.logout(get_settings())
    _invalidate()
    return {"ok": True}


# ---------- 收藏夹 ----------
@router.get("/favorites/folders")
def fav_folders() -> dict[str, Any]:
    state = _state()
    if not state.logged_in:
        raise AuthRequiredError("请先扫码登录 B站")
    folders = favorites.list_folders(get_settings(), state.mid)
    return {"folders": folders, "total": sum(f["count"] for f in folders)}


@router.get("/favorites/items")
def fav_items(media_id: int, page: int = 1) -> dict[str, Any]:
    if page < 1:
        raise HTTPException(400, "页码从 1 开始")
    return favorites.list_items(get_settings(), media_id, page=page)


_BV_RE = re.compile(r"(BV[0-9A-Za-z]{10})")
_PAGE_RE = re.compile(r"[?&]p=(\d+)")


def _already_processed(done: set[tuple[str, int]], bvid: str, page: int) -> bool:
    """判断某一集是否已经处理过——只认磁盘上还存在的笔记。

    以前这里还会查内存里的任务队列，结果是：在知识库里把笔记删掉之后，
    队列里那条"已完成"仍在，去采集收藏重新导入还是会被跳过。
    现在以产物为准：笔记在 → 算处理过；笔记删了 → 可以重新处理。
    按 (BV, 分P) 判定，多P视频删掉第 3 集不会连累其他集。
    """
    return (bvid, page) in done


def _library_done() -> set[tuple[str, int]]:
    """从知识库笔记的 source 字段里收集已产出的 (BV号, 分P)。"""
    done: set[tuple[str, int]] = set()
    for note in library.scan(get_settings()):
        source = note.get("source") or ""
        match = _BV_RE.search(source)
        if not match:
            continue
        page_match = _PAGE_RE.search(source)
        done.add((match.group(1), int(page_match.group(1)) if page_match else 1))
    return done


def _build_options(body: Any) -> dict[str, Any]:
    return {
        "frames": body.frames,
        "ocr": body.ocr,
        "summary": body.summary,
        "lecture": getattr(body, "lecture", False),
        "provider": body.provider,
        "collection": (getattr(body, "collection", "") or "").strip(),
    }


def _collection_name(media_id: int, fallback: str = "") -> str:
    """收藏夹名兜底：前端没带就用 media_id 反查，免得老前端导入的笔记没法归类。"""
    name = (fallback or "").strip()
    if name or not media_id:
        return name
    try:
        state = _state()
        if not state.logged_in:
            return ""
        for folder in favorites.list_folders(get_settings(), state.mid):
            if int(folder.get("media_id") or 0) == int(media_id):
                return str(folder.get("title") or "").strip()
    except Exception:  # 查不到名字不该让导入失败
        log.warning("解析收藏夹名失败 media_id=%s", media_id, exc_info=True)
    return ""


def _create_tasks(
    manager: TaskManager,
    items: list[dict[str, Any]],
    options: dict[str, Any],
    skip_done: bool,
    max_created: int | None = None,
) -> tuple[list[dict[str, str]], int, float]:
    done = _library_done() if skip_done else set()
    created: list[dict[str, str]] = []
    skipped = 0
    duration = 0.0

    for item in items:
        if max_created is not None and len(created) >= max_created:
            break            # 在创建前就停，别让多余任务挤进队列
        bvid = str(item.get("bvid", "")).strip()
        if not bvid.startswith("BV"):
            skipped += 1
            continue
        page = int(item.get("page") or 1)
        url = f"https://www.bilibili.com/video/{bvid}"
        if page > 1:
            url = f"{url}?p={page}"
        if skip_done and _already_processed(done, bvid, page):
            skipped += 1
            continue
        # 系列身份是「每一集」的属性，必须每个任务单独带，且不带就不能继承
        # 上一集的值——否则手动选的单集会被误塞进刚展开的合集里。
        task_options = dict(options)
        for key in ("series_id", "series_title", "series_order"):
            if item.get(key):
                task_options[key] = item[key]
            else:
                task_options.pop(key, None)
        task = manager.create(url, task_options)
        created.append({"id": task.id, "bvid": bvid, "status": task.status})
        duration += float(item.get("duration") or 0)

    return created, skipped, duration


@router.post("/favorites/import")
def fav_import(body: ImportIn, request: Request) -> dict[str, Any]:
    manager: TaskManager = request.app.state.manager
    settings = get_settings()
    if body.episodes:
        # 手动选定的集：原样入队，不做自动展开（用户已经精确指定了要哪几集）
        items = [{"bvid": e.bvid, "page": e.page} for e in body.episodes]
    else:
        items = [{"bvid": b} for b in body.bvids]
    expanded = 0
    if body.expand_series and not body.episodes:
        before = len(items)
        items, _failed = _expand_items(
            settings,
            items,
            limit=max(len(items) * max(body.series_limit, 1), len(items)),
            series_limit=body.series_limit,
            done=_library_done() if body.skip_done else None,
            skip_done=body.skip_done,
        )
        expanded = max(0, len(items) - before)
    options = _build_options(body)
    if body.media_id:
        options["collection"] = _collection_name(body.media_id, options.get("collection", ""))
    created, skipped, _dur = _create_tasks(manager, items, options, body.skip_done)
    log.info("批量导入 %d 个视频（展开 %d 集），跳过 %d 个", len(created), expanded, skipped)
    return {"created": created, "count": len(created), "skipped": skipped, "expanded": expanded}


@router.get("/videos/{bvid}/series")
def video_series(bvid: str) -> dict[str, Any]:
    """这个视频是否属于多P或合集，以及它的全部分集。"""
    if not bvid.startswith("BV"):
        raise HTTPException(400, "不是合法的 BV 号")
    try:
        found = series_svc.resolve(get_settings(), bvid)
    except Exception as exc:
        raise HTTPException(502, f"查询失败：{str(exc)[:120]}") from exc

    # 顺手标上"这集已经产出过笔记"，前端选集时可以把它们灰掉/跳过
    data = found.to_dict()
    done = _library_done()
    for item in data["items"]:
        item["done"] = (item["bvid"], int(item.get("page") or 1)) in done
    data["done_count"] = sum(1 for i in data["items"] if i["done"])
    return data


def _expand_items(
    settings: Any,
    items: list[dict[str, Any]],
    limit: int,
    series_limit: int = 0,
    done: set[tuple[str, int]] | None = None,
    skip_done: bool = False,
) -> tuple[list[dict[str, Any]], int]:
    """把收藏项展开成系列分集，返回 (展开后的列表, 失败数)。

    每个视频要查一次 view 接口（约 0.2 秒），所以最多展开到 limit*2 个源视频就停，
    避免"收藏了 20 个合集"时打几十次请求。

    series_limit：每个合集最多取多少集。收藏夹里往往几十个合集、每个几十集，
    全展开会瞬间把队列灌满；限定集数后可以"每个合集只跟 N 集"。
    配合 skip_done 时优先取**还没做过**的集，这样"每个合集 5 集"拿到的是 5 集新的，
    而不是把已经做完的前 5 集又排一遍。
    """
    expanded: list[dict[str, Any]] = []
    failed = 0
    sources = 0

    for item in items:
        if len(expanded) >= limit or sources >= limit * 2:
            break
        sources += 1
        bvid = str(item.get("bvid", ""))
        try:
            found = series_svc.resolve(settings, bvid)
        except Exception as exc:
            log.warning("展开系列失败 %s: %s", bvid, exc)
            failed += 1
            expanded.append(item)
            continue

        if found.total <= 1:
            expanded.append(item)
            continue

        pieces = list(found.items)
        if skip_done and done:
            pieces = [pc for pc in pieces if (pc.bvid, pc.page) not in done]
        if series_limit and series_limit > 0:
            pieces = pieces[:series_limit]
        if not pieces:
            log.info("《%s》的 %d 集都已处理过，跳过", found.title, found.total)
            continue

        log.info("展开 %s：《%s》共 %d 集，本次取 %d 集",
                 bvid, found.title, found.total, len(pieces))
        sid = found.sid or f"{found.kind}:{bvid}"
        for piece in pieces:
            if len(expanded) >= limit:
                break            # 内层也要停，否则一个 37 集的合集会冲破上限
            order = piece.page if found.kind == "pages" else found.items.index(piece) + 1
            expanded.append({
                "bvid": piece.bvid,
                "page": piece.page,
                "duration": piece.duration,
                "title": piece.title,
                # 写进笔记的系列身份：知识库据此把同一合集的多集折叠成一张卡
                "series_id": sid,
                "series_title": found.title,
                "series_order": order,
            })

    return expanded, failed


@router.post("/favorites/import-count")
def fav_import_count(body: ImportCountIn, request: Request) -> dict[str, Any]:
    """按数量从最新开始处理，不用一个个勾选。

    收藏夹通常几百条，全量跑要几十小时，所以默认只取最新的 N 条，
    并且自动跳过已经产出过笔记的。
    """
    manager: TaskManager = request.app.state.manager
    settings = get_settings()

    picked: list[dict[str, Any]] = []
    page = 1
    scanned = 0
    while len(picked) < body.limit and page <= 10:
        batch = favorites.list_items(settings, body.media_id, page=page)
        scanned += len(batch["items"])
        picked.extend(batch["items"])
        if not batch["has_more"]:
            break
        page += 1

    if not picked:
        return {"created": [], "count": 0, "skipped": 0, "total_duration": 0,
                "scanned": 0, "message": "这个收藏夹是空的"}

    expanded_count = 0
    if body.expand_series:
        before = len(picked)
        picked, failed = _expand_items(
            settings, picked, body.limit,
            series_limit=body.series_limit,
            done=_library_done() if body.skip_done else None,
            skip_done=body.skip_done,
        )
        expanded_count = max(0, len(picked) - before)
        if failed:
            log.warning("%d 个视频展开系列失败，按原样处理", failed)

    options = _build_options(body)
    options["collection"] = _collection_name(body.media_id, options.get("collection", ""))
    created, skipped, duration = _create_tasks(
        manager, picked, options, body.skip_done, max_created=body.limit
    )
    log.info(
        "按量导入：扫描 %d 条，选中 %d，跳过 %d，总时长 %.0f 秒",
        scanned, len(created), skipped, duration,
    )
    return {
        "created": created,
        "count": len(created),
        "skipped": skipped,
        "scanned": scanned,
        "expanded": expanded_count,
        "total_duration": round(duration, 1),
    }
