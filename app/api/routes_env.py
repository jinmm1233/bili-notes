"""环境自检接口：给设置页/诊断页显示重型依赖能不能用。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from ..core import envcheck

router = APIRouter(prefix="/api")


@router.get("/env")
def env_report() -> dict[str, Any]:
    return envcheck.probe()
