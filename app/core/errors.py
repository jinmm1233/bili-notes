"""类型化异常体系：对外只暴露 code/message，绝不泄露堆栈。"""
from __future__ import annotations

from typing import Any


class AppError(Exception):
    code = "internal_error"
    http_status = 500

    def __init__(self, message: str, *, detail: Any = None, http_status: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail
        if http_status is not None:
            self.http_status = http_status

    def to_dict(self, request_id: str = "") -> dict[str, Any]:
        payload: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.detail:
            payload["detail"] = self.detail
        if request_id:
            payload["request_id"] = request_id
        return payload


class InputError(AppError):
    code = "invalid_input"
    http_status = 400


class AuthRequiredError(AppError):
    code = "auth_required"
    http_status = 401


class NotFoundError(AppError):
    code = "not_found"
    http_status = 404


class ConflictError(AppError):
    code = "conflict"
    http_status = 409


class UpstreamError(AppError):
    code = "upstream_error"
    http_status = 502


class VideoUnavailableError(UpstreamError):
    code = "video_unavailable"


class FFmpegError(AppError):
    code = "ffmpeg_failed"


class LLMError(UpstreamError):
    code = "llm_failed"


class CancelledError(AppError):
    code = "cancelled"
    http_status = 499


class DependencyMissingError(AppError):
    code = "dependency_missing"
    http_status = 503
