"""结构化 JSON 日志，贯穿 request_id / task_id。"""
from __future__ import annotations

import json
import logging
import sys
import time
from contextvars import ContextVar
from datetime import datetime
from typing import Any

request_id_var: ContextVar[str] = ContextVar("request_id", default="")
task_id_var: ContextVar[str] = ContextVar("task_id", default="")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        rid = request_id_var.get()
        tid = task_id_var.get()
        if rid:
            entry["request_id"] = rid
        if tid:
            entry["task_id"] = tid
        extra = getattr(record, "extra_fields", None)
        if isinstance(extra, dict):
            entry.update(extra)
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False)


def setup_logging(level: str = "INFO", to_file: bool = False, log_dir=None) -> None:
    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(JsonFormatter())
    root.addHandler(stream)

    if to_file and log_dir is not None:
        from logging.handlers import RotatingFileHandler

        # 日志文件偶尔会被别的东西占住（上一轮进程没退干净、杀软扫描、同步盘占用）。
        # 这种情况下宁可少一份文件日志，也不能让整个软件起不来：
        # 先试 app.log，写不了就换带时间戳的文件，实在不行只输出到控制台。
        candidates = [log_dir / "app.log", log_dir / f"app-{time.strftime('%Y%m%d-%H%M%S')}.log"]
        for path in candidates:
            try:
                handler = RotatingFileHandler(
                    path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
                )
            except OSError:
                continue
            handler.setFormatter(JsonFormatter())
            root.addHandler(handler)
            if path.name != "app.log":
                root.warning("app.log 不可写，本次日志改写到 %s", path.name)
            break
        else:
            root.warning("日志目录不可写，仅输出到控制台")

    for noisy in ("httpx", "httpx2", "httpcore", "urllib3", "faster_whisper", "urllib3.connectionpool"):
        logging.getLogger(noisy).setLevel("WARNING")


def log_extra(**kwargs: Any) -> dict[str, Any]:
    return {"extra_fields": kwargs}
