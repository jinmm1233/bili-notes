"""带断点续传的流式下载。

模型权重和 ffmpeg 都要下几十上百 MB，中途断网重来一遍太浪费，
所以统一在这里实现 Range 续传 + 进度回调 + 取消。
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable

from .errors import CancelledError

CancelFn = Callable[[], bool]


def stream_download(
    url: str,
    dest: Path,
    on_chunk: Callable[[int], None] | None = None,
    cancel: CancelFn | None = None,
    timeout: float = 120.0,
) -> int:
    """流式下载单个文件，支持断点续传。返回文件最终字节数。

    不用 huggingface_hub：它对镜像站会间歇性返回 502，
    且底层 Xet 协议在 hf-mirror 上直接 401。自己发请求反而最稳。
    """
    import httpx  # noqa: PLC0415

    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    existing = dest.stat().st_size if dest.is_file() else 0
    headers = {"Range": f"bytes={existing}-"} if existing else {}

    total = 0
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        with client.stream("GET", url, headers=headers) as resp:
            if resp.status_code == 416:      # 已完整
                return existing
            if resp.status_code == 404:
                raise FileNotFoundError(url)
            resp.raise_for_status()
            with dest.open("ab" if existing else "wb") as fh:
                for chunk in resp.iter_bytes(chunk_size=262144):
                    if cancel and cancel():
                        raise CancelledError("任务已取消")
                    fh.write(chunk)
                    total += len(chunk)
                    if on_chunk:
                        on_chunk(total)
    return existing + total
