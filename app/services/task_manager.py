"""任务管理：状态机、并发闸门、取消、进度广播。

单机桌面应用不需要 Celery/Redis，但必须做到两点：
1. 并发上限（CPU 转写时同时跑两个任务会让两个都变得极慢）
2. 单任务失败不影响其他任务
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..core.config import Settings
from ..core.errors import AppError, CancelledError
from ..core.secretstore import SecretStore
from . import cache_clean
from .pipeline import Pipeline, PipelineOptions

log = logging.getLogger(__name__)

ACTIVE = {"queued", "running"}
FINAL = {"done", "failed", "cancelled"}


@dataclass
class Task:
    id: str
    url: str
    options: dict[str, Any] = field(default_factory=dict)
    status: str = "queued"
    stage: str = ""
    progress: float = 0.0
    message: str = ""
    error: str = ""
    warnings: list[str] = field(default_factory=list)
    md_path: str = ""
    output_dir: str = ""
    title: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    finished_at: float = 0.0
    cancel_event: threading.Event = field(default_factory=threading.Event)
    # 暂停不是停线程，而是让任务在下一个检查点"挂起"等恢复信号；
    # paused_from 记住暂停前是 running 还是 queued，恢复时还原。
    pause_event: threading.Event = field(default_factory=threading.Event)
    paused_from: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "url": self.url,
            "status": self.status,
            "stage": self.stage,
            "progress": round(self.progress, 1),
            "message": self.message,
            "error": self.error,
            "warnings": self.warnings,
            "md_path": self.md_path,
            "output_dir": self.output_dir,
            "title": self.title,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "finished_at": self.finished_at,
        }


class TaskManager:
    def __init__(
        self,
        settings: Settings,
        store: SecretStore,
        on_finish: Callable[[], None] | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.on_finish = on_finish
        self._tasks: dict[str, Task] = {}
        self._lock = threading.Lock()
        self._pool = ThreadPoolExecutor(
            max_workers=max(1, settings.worker_concurrency),
            thread_name_prefix="task",
        )

    # 多久没有任何进度更新算"僵死"。转写/下载都会持续 emit，
    # 真正卡住时（模型下载被墙、ffmpeg 挂起）才会长时间静默。
    STALE_SECONDS = 600

    def _is_stale(self, task: Task) -> bool:
        return time.time() - task.updated_at > self.STALE_SECONDS

    def create(self, url: str, options: dict[str, Any] | None = None) -> Task:
        url = url.strip()
        with self._lock:
            # 同一视频已在排队/处理中就不再重复建任务，避免批量导入时重复点两次。
            # 但如果它已经很久没有任何进度（僵死），必须允许替换 ——
            # 否则用户点第二次只会"复用"那个卡住的任务，界面永远停在同一个百分比，
            # 看起来像软件坏了。
            for existing in list(self._tasks.values()):
                if existing.url != url or existing.status not in ACTIVE:
                    continue
                if not self._is_stale(existing) or existing.status == "paused":
                    log.info("任务已存在 %s，复用 %s", url, existing.id)
                    return existing
                log.warning(
                    "任务 %s 已 %.0f 秒无进度，判定为僵死，取消并重建",
                    existing.id, time.time() - existing.updated_at,
                )
                existing.cancel_event.set()
                existing.status = "failed"
                existing.error = "长时间无响应，已自动终止（可重新发起）"
                existing.finished_at = time.time()
            task = Task(id=uuid.uuid4().hex[:12], url=url, options=options or {})
            self._tasks[task.id] = task
        self._pool.submit(self._run, task)
        log.info("任务已创建 %s -> %s", task.id, url)
        return task

    def exists_done(self, url: str) -> bool:
        """是否已经成功处理过这个视频（批量导入时用于跳过）。"""
        target = url.strip()
        with self._lock:
            return any(t.url == target and t.status == "done" for t in self._tasks.values())

    def get(self, task_id: str) -> Task | None:
        with self._lock:
            return self._tasks.get(task_id)

    def list(self) -> list[Task]:
        with self._lock:
            return sorted(self._tasks.values(), key=lambda t: t.created_at, reverse=True)

    def cancel(self, task_id: str) -> bool:
        task = self.get(task_id)
        if task is None or task.status in FINAL:
            return False
        task.cancel_event.set()
        # 暂停中的任务阻塞在 gate 里，必须先解除暂停它才能看到取消信号
        task.pause_event.clear()
        task.message = "正在取消…"
        return True

    def pause(self, task_id: str) -> bool:
        task = self.get(task_id)
        if task is None or task.status not in ACTIVE:
            return False
        task.paused_from = task.status
        task.pause_event.set()
        task.status = "paused"
        task.message = "已暂停"
        return True

    def unpause(self, task_id: str) -> bool:
        task = self.get(task_id)
        if task is None or task.status != "paused":
            return False
        task.pause_event.clear()
        task.status = task.paused_from or "running"
        task.paused_from = ""
        task.message = "继续处理…"
        return True

    def pause_all(self) -> int:
        """全部暂停，返回生效数量。排队中的也会被暂停（轮到它跑时先挂起）。"""
        with self._lock:
            targets = [t for t in self._tasks.values() if t.status in ACTIVE]
        for t in targets:
            self.pause(t.id)
        return len(targets)

    def resume_all(self) -> int:
        with self._lock:
            targets = [t for t in self._tasks.values() if t.status == "paused"]
        for t in targets:
            self.unpause(t.id)
        return len(targets)

    def resume(self, task_id: str) -> bool:
        """失败/取消的任务重新排队跑一遍（产物有缓存，重复部分很快）。"""
        task = self.get(task_id)
        if task is None:
            return False
        if task.status == "paused":
            return self.unpause(task_id)
        if task.status not in {"failed", "cancelled"}:
            return False
        task.status = "queued"
        task.error = ""
        task.progress = 0.0
        task.cancel_event = threading.Event()
        self._pool.submit(self._run, task)
        return True

    # ---- 执行 ----
    def _run(self, task: Task) -> None:
        task.status = "running"
        task.stage = "start"

        def emit(stage: str, pct: float, msg: str) -> None:
            task.stage = stage
            task.progress = pct
            task.message = msg
            task.updated_at = time.time()

        def gate() -> bool:
            """传给 Pipeline 的取消检查：暂停时在这里挂起，取消时返回 True。

            Pipeline 在每个阶段边界、下载/转写/总结的进度循环里都会调它，
            所以暂停能在一两秒内生效，而不是等到整个阶段跑完。
            """
            while task.pause_event.is_set() and not task.cancel_event.is_set():
                task.updated_at = time.time()  # 暂停期间不算僵死
                time.sleep(0.25)
            return task.cancel_event.is_set()

        # Pipeline 的构造也在 try 里：任何一步抛异常都必须落到下面的状态机，
        # 否则线程静默死亡，任务永远停在"处理中"。
        try:
            pipeline = Pipeline(
                self.settings,
                self.store,
                emit=emit,
                cancel=gate,
            )
            opts = task.options
            options = PipelineOptions(
                do_frames=opts.get("frames", True),
                do_ocr=opts.get("ocr", True),
                do_summary=opts.get("summary", True),
                do_lecture=bool(opts.get("lecture", False)),
                max_height=int(opts.get("height", 480)),
                provider=opts.get("provider") or None,
                collection=str(opts.get("collection") or ""),
                series_id=str(opts.get("series_id") or ""),
                series_title=str(opts.get("series_title") or ""),
                series_order=int(opts.get("series_order") or 0),
            )
            result = pipeline.run(task.url, task_id=task.id, options=options)
        except CancelledError:
            task.status = "cancelled"
            task.message = "已取消"
        except AppError as exc:
            task.status = "failed"
            task.error = exc.message
            log.warning("任务 %s 失败：%s", task.id, exc.message, exc_info=True)
        except Exception as exc:  # noqa: BLE001
            task.status = "failed"
            task.error = f"未预期的错误：{str(exc)[:200]}"
            log.exception("任务 %s 崩溃", task.id)
        else:
            task.status = "done"
            task.progress = 100.0
            task.stage = "done"
            task.md_path = str(result.md_path)
            task.output_dir = str(result.md_path.parent)
            task.title = result.note.meta.title
            task.warnings = result.warnings
            task.message = f"完成，用时 {result.elapsed:.0f}s"
        finally:
            task.finished_at = time.time()
            try:
                self._cleanup(task)
            except Exception:  # 清理失败不该改变任务状态
                log.warning("任务 %s 缓存清理失败", task.id, exc_info=True)
            if self.on_finish:
                try:
                    self.on_finish()
                except Exception:  # 回调失败不应影响任务状态
                    log.warning("任务完成回调失败", exc_info=True)

    def _cleanup(self, task: Task) -> None:
        """按 cache_policy 清理该任务的下载缓存。

        视频/音频是整条链路最占地的中间产物（一条 17 分钟的视频
        video.mp4 + audio.m4a 约 70MB），处理完没有任何读取方：
        关键帧在导出时已复制进输出目录 assets/。
        所以成功、失败、取消都清媒体文件；只有成功时才连 json 一起删，
        失败保留 json 方便排查。
        """
        policy = self.settings.cache_policy
        if policy == "keep":
            return
        work = self.settings.cache_dir / task.id
        if not work.is_dir():
            return
        mode = "all" if (policy == "all" and task.status == "done") else "media"
        freed = cache_clean.clean_task(work, mode)
        if freed:
            log.info("任务 %s 清理缓存，释放 %s", task.id, cache_clean.human(freed))
            if task.status == "done" and task.message:
                task.message = f"{task.message}（已清理缓存 {cache_clean.human(freed)}）"

    def shutdown(self) -> None:
        # 暂停中的任务阻塞在 gate 的循环里，先唤醒（以取消收场），
        # 否则线程池的非 daemon 线程会让进程退不出去。
        with self._lock:
            active = [t for t in self._tasks.values() if t.status in ACTIVE or t.status == "paused"]
        for t in active:
            t.cancel_event.set()
            t.pause_event.clear()
        self._pool.shutdown(wait=False, cancel_futures=True)
