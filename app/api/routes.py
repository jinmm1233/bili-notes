"""控制器层：校验入参、调用服务、格式化响应。不含业务逻辑。"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import os
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from ..core import lan
from ..core.config import get_settings, save_overrides
from ..core.errors import DependencyMissingError
from ..core.llm import LLMClient, PROVIDERS, test_connection
from ..core.secretstore import SecretStore
from ..services import cache_clean, ffmpeg_boot
from ..services.task_manager import FINAL, TaskManager

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api")


def _mgr(request: Request) -> TaskManager:
    return request.app.state.manager


def _store(request: Request) -> SecretStore:
    return request.app.state.store


# ---------- 入参模型 ----------
class CreateTaskIn(BaseModel):
    url: str = Field(min_length=2, max_length=2000)
    frames: bool = True
    ocr: bool = True
    summary: bool = True
    height: int = Field(default=480, ge=144, le=1080)
    provider: str | None = None


class KeyIn(BaseModel):
    provider: str
    key: str = Field(min_length=1, max_length=400)


class SettingsIn(BaseModel):
    llm_provider: str | None = None
    llm_model: str | None = None
    llm_base_url: str | None = None
    asr_model: str | None = None
    asr_language: str | None = None
    output_dir: str | None = None
    max_frames_total: int | None = Field(default=None, ge=0, le=400)
    # 笔记详细度：brief / standard / detailed（同时决定候选密度与每段配额）
    note_detail: str | None = Field(default=None, pattern="^(brief|standard|detailed)$")
    hd_frames: bool | None = None
    parallel_pipeline: bool | None = None
    open_browser: bool | None = None
    polish_punctuation: bool | None = None
    bilingual_transcript: bool | None = None
    frame_captions: bool | None = None
    travel_itinerary: bool | None = None
    vision_provider: str | None = None
    vision_base_url: str | None = None
    vision_model: str | None = None
    cache_policy: str | None = None


class TestIn(BaseModel):
    provider: str
    key: str | None = None


# ---------- 任务 ----------
@router.post("/tasks")
def create_task(body: CreateTaskIn, request: Request) -> dict[str, Any]:
    settings = get_settings()
    output = Path(settings.output_dir).resolve()
    if not output.is_dir():
        raise HTTPException(400, "输出目录不可用，请在设置里重新选择")

    task = _mgr(request).create(body.url, body.model_dump(exclude={"url"}))
    return {"id": task.id, "status": task.status}


@router.get("/tasks")
def list_tasks(request: Request) -> dict[str, Any]:
    return {"items": [t.to_dict() for t in _mgr(request).list()]}


@router.get("/tasks/{task_id}")
def get_task(task_id: str, request: Request) -> dict[str, Any]:
    task = _mgr(request).get(task_id)
    if task is None:
        raise HTTPException(404, "任务不存在")
    return task.to_dict()


@router.get("/tasks/{task_id}/events")
async def task_events(task_id: str, request: Request) -> StreamingResponse:
    """SSE 进度流：状态变化即推送，客户端断开自动结束。"""
    manager = _mgr(request)

    async def gen():
        last: str | None = None
        idle = 0
        while True:
            if await request.is_disconnected():
                break
            task = manager.get(task_id)
            if task is None:
                yield 'data: {"error":"任务不存在"}\n\n'
                break
            payload = json.dumps(task.to_dict(), ensure_ascii=False)
            if payload != last:
                yield f"data: {payload}\n\n"
                last = payload
                idle = 0
            else:
                idle += 1
                if idle % 25 == 0:  # 心跳，防止代理断开
                    yield ": ping\n\n"
            if task.status in FINAL:
                break
            await asyncio.sleep(0.4)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/tasks/{task_id}/cancel")
def cancel_task(task_id: str, request: Request) -> dict[str, Any]:
    ok = _mgr(request).cancel(task_id)
    return {"ok": ok}


@router.post("/tasks/{task_id}/pause")
def pause_task(task_id: str, request: Request) -> dict[str, Any]:
    ok = _mgr(request).pause(task_id)
    if not ok:
        raise HTTPException(409, "只有排队中或处理中的任务可以暂停")
    return {"ok": True}


@router.post("/tasks/{task_id}/unpause")
def unpause_task(task_id: str, request: Request) -> dict[str, Any]:
    ok = _mgr(request).unpause(task_id)
    if not ok:
        raise HTTPException(409, "任务不在暂停状态")
    return {"ok": True}


@router.post("/tasks/pause-all")
def pause_all_tasks(request: Request) -> dict[str, Any]:
    return {"ok": True, "count": _mgr(request).pause_all()}


@router.post("/tasks/resume-all")
def resume_all_tasks(request: Request) -> dict[str, Any]:
    return {"ok": True, "count": _mgr(request).resume_all()}


@router.post("/tasks/{task_id}/retry")
def retry_task(task_id: str, request: Request) -> dict[str, Any]:
    ok = _mgr(request).resume(task_id)
    if not ok:
        raise HTTPException(409, "只有失败或已取消的任务可以重试")
    return {"ok": True}


def _safe_output(request: Request, task_id: str) -> tuple[Any, Path]:
    task = _mgr(request).get(task_id)
    if task is None:
        raise HTTPException(404, "任务不存在")
    if not task.output_dir:
        raise HTTPException(409, "任务尚未产出结果")
    out = Path(task.output_dir).resolve()
    root = Path(get_settings().output_dir).resolve()
    if root not in out.parents and out != root:
        raise HTTPException(403, "非法的输出路径")
    return task, out


@router.get("/tasks/{task_id}/markdown")
def get_markdown(task_id: str, request: Request) -> dict[str, Any]:
    _task, out = _safe_output(request, task_id)
    md = out / "notes.md"
    if not md.is_file():
        raise HTTPException(404, "笔记文件不存在")
    return {"path": str(md), "content": md.read_text(encoding="utf-8")}


@router.get("/tasks/{task_id}/file/{rel_path:path}")
def serve_artifact(task_id: str, rel_path: str, request: Request) -> FileResponse:
    """为前端预览提供图片/附属文件，路径必须落在该任务的输出目录内。"""
    _task, out = _safe_output(request, task_id)
    target = (out / rel_path).resolve()
    if target != out and out not in target.parents:
        raise HTTPException(403, "非法路径")
    if not target.is_file():
        raise HTTPException(404, "文件不存在")
    return FileResponse(target)


@router.get("/tasks/{task_id}/download")
def download_zip(task_id: str, request: Request) -> StreamingResponse:
    task, out = _safe_output(request, task_id)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in out.rglob("*"):
            if path.is_file():
                zf.write(path, path.relative_to(out))
    buf.seek(0)
    name = quote(f"{task.title or 'notes'}.zip")
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{name}"},
    )


@router.post("/tasks/{task_id}/reveal")
def reveal(task_id: str, request: Request) -> dict[str, Any]:
    """在资源管理器里打开笔记所在目录。"""
    _task, out = _safe_output(request, task_id)
    try:
        if sys.platform == "win32":
            os.startfile(str(out))  # noqa: S606
        elif sys.platform == "darwin":
            subprocess.run(["open", str(out)], check=False)
        else:
            subprocess.run(["xdg-open", str(out)], check=False)
        return {"ok": True}
    except OSError as exc:
        raise HTTPException(500, f"无法打开目录：{exc}") from exc


# ---------- 配置与密钥 ----------
@router.get("/providers")
def list_providers(request: Request) -> dict[str, Any]:
    store = _store(request)
    items = []
    for pid, preset in PROVIDERS.items():
        items.append({
            "id": pid,
            "label": preset["label"],
            "base_url": preset["base_url"],
            "model": preset["model"],
            "vision_model": preset.get("vision_model", ""),
            "vision_options": list(preset.get("vision_options", ())),
            "hint": preset["hint"],
            "needs_key": preset.get("needs_key") == "1",
            "has_key": bool(store.get(pid)),
            "masked": store.masked(pid),
        })
    return {"providers": items}


@router.get("/settings")
def read_settings(request: Request) -> dict[str, Any]:
    settings = get_settings()
    store = _store(request)
    provider = settings.llm_provider
    preset = PROVIDERS.get(provider, {})
    needs_key = preset.get("needs_key") == "1"
    ready = bool(store.get(provider)) or not needs_key

    return {
        "llm_provider": provider,
        "llm_model": preset.get("model") or settings.llm_model,
        "llm_base_url": preset.get("base_url") or settings.llm_base_url,
        "asr_model": settings.asr_model,
        "asr_language": settings.asr_language,
        "output_dir": str(settings.output_dir),
        "max_frames_total": settings.max_frames_total,
        # 前端靠它回显三档选择；只返回 total 会让「详细」下次打开变回「适中」
        "note_detail": settings.note_detail,
        "hd_frames": settings.hd_frames,
        "parallel_pipeline": settings.parallel_pipeline,
        "open_browser": settings.open_browser,
        "polish_punctuation": settings.polish_punctuation,
        "bilingual_transcript": settings.bilingual_transcript,
        "frame_captions": settings.frame_captions,
        "travel_itinerary": settings.travel_itinerary,
        "vision_provider": settings.vision_provider,
        "vision_base_url": settings.vision_base_url,
        "vision_model": settings.vision_model,
        "vision_ready": LLMClient.from_vision_settings(settings, _store(request)) is not None,
        "cache_policy": settings.cache_policy,
        "cache": cache_clean.usage(settings.cache_dir),
        "has_key": bool(store.get(provider)),
        "masked": store.masked(provider),
        "needs_key": needs_key,
        "ready": ready,
        "first_run": not ready,
        "ffmpeg": ffmpeg_boot.status(),
        "version": __import__("app").__version__,
    }


@router.post("/settings")
def write_settings(body: SettingsIn, request: Request) -> dict[str, Any]:
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    if "output_dir" in updates:
        target = Path(updates["output_dir"])
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise HTTPException(400, f"目录不可创建：{exc}") from exc
        updates["output_dir"] = str(target)
    save_overrides(updates)
    return {"ok": True, "settings": read_settings(request)}


@router.get("/system/ffmpeg")
def read_ffmpeg() -> dict[str, Any]:
    return ffmpeg_boot.status()


@router.post("/system/ffmpeg/install")
def install_ffmpeg() -> dict[str, Any]:
    """按需下载 ffmpeg。同步执行（FastAPI 会放到线程池），前端给出进度条即可。"""
    try:
        state = ffmpeg_boot.install(on_progress=lambda p, m: log.info("[ffmpeg %.0f%%] %s", p, m))
    except DependencyMissingError as exc:
        raise HTTPException(502, exc.message) from exc
    except Exception as exc:  # 网络类异常要给用户看得懂的话
        log.warning("ffmpeg 安装失败：%s", exc)
        raise HTTPException(502, f"ffmpeg 安装失败：{str(exc)[:140]}") from exc
    return {"ok": True, "ffmpeg": state}


@router.get("/system/cache")
def read_cache(request: Request) -> dict[str, Any]:
    """查看下载缓存占用：视频/音频是体积大头，处理完即可清。"""
    settings = get_settings()
    info = cache_clean.usage(settings.cache_dir)
    info["policy"] = settings.cache_policy
    info["path"] = str(settings.cache_dir)
    return info


@router.post("/system/cache/clean")
def clean_cache(request: Request, mode: str = "media") -> dict[str, Any]:
    """手动一键清理。

    mode=media 只删已下载的视频/音频；mode=all 连抽帧和中间 json 一起删。
    正在跑的任务会被跳过——删掉它正在读的视频会直接把任务弄崩。
    """
    if mode not in ("media", "all"):
        raise HTTPException(400, "mode 只能是 media 或 all")
    settings = get_settings()
    active = [t.id for t in _mgr(request).list() if t.status not in FINAL]
    done = cache_clean.clean_all(settings.cache_dir, policy=mode, keep_ids=active)
    log.info("手动清理缓存：释放 %s，处理 %d 个任务", done["human"], done["removed"])
    return {"ok": True, **done, "cache": cache_clean.usage(settings.cache_dir)}


@router.post("/settings/key")
def save_key(body: KeyIn, request: Request) -> dict[str, Any]:
    if body.provider not in PROVIDERS:
        raise HTTPException(400, "未知的模型供应商")
    store = _store(request)
    store.set(body.provider, body.key.strip())
    return {"ok": True, "masked": store.masked(body.provider)}


@router.delete("/settings/key/{provider}")
def delete_key(provider: str, request: Request) -> dict[str, Any]:
    _store(request).delete(provider)
    return {"ok": True}


@router.post("/settings/test")
def test_provider(body: TestIn, request: Request) -> dict[str, Any]:
    preset = PROVIDERS.get(body.provider)
    if preset is None:
        raise HTTPException(400, "未知的模型供应商")
    key = (body.key or "").strip() or (_store(request).get(body.provider) or "")
    if preset.get("needs_key") == "1" and not key:
        return {"ok": False, "message": "请先填写 API Key"}
    ok, msg = test_connection(preset["base_url"], key, preset["model"])
    return {"ok": ok, "message": msg}


# ---------------------------------------------------------------- 双端（手机访问）
class LanIn(BaseModel):
    enabled: bool | None = None
    rotate: bool = False


def _lan_status(with_qr: bool = False) -> dict[str, Any]:
    """双端状态。with_qr=True 时把二维码内联成 data-uri 一起返回。

    为什么要内联：设置页原来是 <img src="/api/lan/qr">，那是**第二次请求**。
    从局域网地址打开设置页时（client.host 不是 127.0.0.1），那个接口会 403，
    页面上就只剩一个破图。改成随状态一起下发，一次请求拿全，也不会被
    浏览器或预览面板的图片策略拦住。
    """
    settings = get_settings()
    ip = lan.lan_ip()
    token = settings.lan_token or ""
    # 真实绑定的端口，不是配置里的。pick_port 会顺延，报错了手机就指向另一个实例。
    port = lan.bound_port(settings.port)
    base = f"http://{ip}:{port}" if ip else ""
    url = f"{base}/?k={token}" if (base and token) else ""
    # 端口是不是还在对外监听——和开关无关。关了开关但没重启时它仍是 True，
    # 这时手机其实已经打不开（守卫给 403），只是端口还没释放。
    bound = lan.reachable(ip, port)
    seen = lan.remember(ip, url)
    data = {
        "enabled": settings.lan_access,
        # 开关打开并且真绑上了，才算"现在就能用"
        "reachable": settings.lan_access and bound,
        "port_open": bound,
        "ip": ip,
        "port": port,
        "key": token,
        "url": url,
        # 地址换过（换 WiFi / DHCP 重新分配）：手机上的旧链接已经作废，
        # 前端要明确提示"重新扫码"，而不是让人以为软件坏了。
        "ip_changed": bool(seen.get("prev_ip") and seen["changed"]),
        "prev_ip": seen.get("prev_ip", ""),
        # 精简版页面（只有笔记/卡片/自测）：页面本身仍要从电脑加载，
        # 真正脱机请用「导出学习包」。留着这个地址是给旧链接兼容。
        "offline": f"{base}/offline?k={token}" if (base and token) else "",
    }
    if with_qr and url:
        svg = lan.qr_svg(url)
        data["qr"] = "data:image/svg+xml;base64," + base64.b64encode(
            svg.encode("utf-8")
        ).decode("ascii")
    return data


@router.get("/lan")
def lan_status(response: Response, qr: bool = False) -> dict[str, Any]:
    """手机访问的现状：开没开、地址、口令、这次是否真的生效了。

    必须 no-store：开关状态是"刚刚改过"的东西，被浏览器缓存住就会出现
    “点了关闭、界面还是开着”的假象（GET 没有缓存头时浏览器会自己拿旧的）。
    """
    response.headers["cache-control"] = "no-store"
    return _lan_status(with_qr=qr)


@router.get("/lan/doctor")
def lan_doctor(response: Response) -> dict[str, Any]:
    """手机连不上时的逐项体检。

    为什么要有它：以前只能给一句"检查防火墙"，用户面对的是浏览器里的
    "无法访问此网站"，根本分不清是软件没开、端口没绑、地址变了，还是这个
    WiFi 压根不让设备互通。这里按顺序把四件事都查一遍，并给出下一步动作。
    """
    response.headers["cache-control"] = "no-store"
    return lan.diagnose()


@router.post("/lan")
def lan_update(body: LanIn) -> dict[str, Any]:
    """开关双端 / 换口令。改监听地址必须重启进程，这里如实告诉前端。"""
    updates: dict[str, Any] = {}
    if body.enabled is not None:
        updates["lan_access"] = bool(body.enabled)
    if body.rotate or (body.enabled and not get_settings().lan_token):
        updates["lan_token"] = lan.new_token()
    if updates:
        save_overrides(updates)
        log.info("双端设置已更新：%s", {k: (v if k != "lan_token" else "***") for k, v in updates.items()})
    return _lan_status()


@router.get("/lan/qr")
def lan_qr(request: Request, target: str = "app"):
    """手机扫的二维码（独立接口，给需要直接拿 SVG 的场景）。

    二维码里带着口令，所以只发给"本机或已通过口令验证的人"。
    target=app 给完整界面（默认）；target=offline 给精简版页面。

    设置页自己不走这里——它用 `/lan?qr=1` 拿内联的 data-uri，
    省掉一次可能被 403 的请求（见 `_lan_status` 的注释）。
    """
    if not lan.authed(request):
        raise HTTPException(403, "二维码只给本机或已通过口令的会话")
    status = _lan_status()
    link = status["url"] if target != "offline" else status["offline"]
    if not link:
        raise HTTPException(400, "先打开手机访问")
    return Response(
        lan.qr_svg(link),
        media_type="image/svg+xml",
        headers={"cache-control": "no-store"},
    )


@router.post("/lan/login")
def lan_login(k: str = Form("")):  # noqa: B008
    """手机第一次带 ?k= 进来时用它换 cookie；口令错了给回登录页。"""
    settings = get_settings()
    if not settings.lan_access:
        return RedirectResponse("/", status_code=303)
    if not settings.lan_token or k.strip() != settings.lan_token:
        return HTMLResponse(lan.login_page("口令不对，再试一次"), status_code=401)
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(lan.COOKIE, settings.lan_token,
                        max_age=lan.COOKIE_MAX_AGE, httponly=True, samesite="lax")
    return response
