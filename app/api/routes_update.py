"""内置更新接口：状态 / 配置 / 检查 / 下载 / 安装。

单独一个文件，不塞进 routes.py：那一坨是主业务，更新这块要能独立测、独立改。
所有接口都不抛异常到框架层——网络不通只返回人话提示，前端照常能用。
"""
from __future__ import annotations

import logging
import threading
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from ..core import updater

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/update")


class ConfigIn(BaseModel):
    repo: str | None = None
    mirror: str | None = None
    auto_check: bool | None = None


class CheckIn(BaseModel):
    force: bool = True


@router.get("/status")
def get_status() -> dict[str, Any]:
    return updater.status()


@router.post("/config")
def set_config(body: ConfigIn) -> dict[str, Any]:
    updates = body.model_dump(exclude_none=True)
    try:
        cfg = updater.save_config(updates)
    except ValueError as exc:
        return {"ok": False, "message": str(exc)}
    return {"ok": True, "config": cfg, "repo": updater.effective_repo()}


@router.post("/check")
def check(body: CheckIn | None = None) -> dict[str, Any]:
    return updater.check(force=(body.force if body else True))


@router.post("/auto")
def auto_check() -> dict[str, Any]:
    """启动时静默查一次：后台线程，不阻塞首屏，也不因网络问题报错。"""
    cfg = updater.get_config()
    if not cfg["auto_check"] or not updater.effective_repo():
        return {"started": False}
    state = updater.status()
    if state["last_checked"]:
        import time  # noqa: PLC0415

        if time.time() - state["last_checked"] < updater.CHECK_INTERVAL:
            return {"started": False, "reason": "recent"}

    def _run() -> None:
        try:
            updater.check(force=False)
        except Exception as exc:                 # 理论上 updater 已全兜住，这里再兜一层
            log.info("自动检查更新异常（已忽略）：%s", exc)

    threading.Thread(target=_run, daemon=True).start()
    return {"started": True}


@router.post("/download")
def download() -> dict[str, Any]:
    return updater.start_download()


@router.post("/install")
def install() -> dict[str, Any]:
    return updater.install()
