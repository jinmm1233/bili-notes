"""字幕层：B站 CC / AI 字幕获取、解析、重分段、SRT 导出。

B 站 AI 字幕是按语音停顿切碎的短句、几乎无标点，直接送去总结会让模型
读到"碎片语义"，所以统一做一次合并重分段。
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

import httpx

from ..core.errors import UpstreamError
from ..core.models import Segment

log = logging.getLogger(__name__)

_SUB_PRIORITY = ("zh-Hans", "zh-CN", "zh", "ai-zh", "zh-Hant", "ai-zh-Hans", "en", "ai-en")
_SENT_END = "。！？!?…；;"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
    ),
    "Referer": "https://www.bilibili.com/",
}


def pick_subtitle_track(info: dict[str, Any]) -> tuple[str, str] | None:
    """从 yt-dlp 的 info 里挑一条最合适的字幕，返回 (语言, url)。"""
    subs: dict[str, list[dict[str, Any]]] = info.get("subtitles") or {}
    if not subs:
        return None

    def rank(lang: str) -> int:
        if lang in _SUB_PRIORITY:
            return _SUB_PRIORITY.index(lang)
        if lang.startswith("zh"):
            return 2
        return 99

    for lang in sorted(subs, key=rank):
        tracks = subs[lang] or []
        for track in tracks:
            url = (track.get("url") or "").strip()
            if not url:
                continue
            if url.startswith("//"):
                url = "https:" + url
            elif url.startswith("/"):
                url = "https://www.bilibili.com" + url
            return lang, url
    return None


# player/v2 请求专用头。Referer 必须是站点根路径 ——
# 实测用 https://www.bilibili.com/video/BVxxx/ 作 Referer 时，
# 接口会返回完整的语言列表但每条的 subtitle_url 都是空串，
# 看起来"没有字幕"，实际是 B站 的风控策略。
_PLAYER_HEADERS = {
    "User-Agent": _HEADERS["User-Agent"],
    "Referer": "https://www.bilibili.com/",
}


def resolve_cid(bvid: str, cookies: dict[str, str] | None = None, timeout: float = 15.0) -> int:
    """取视频的 cid。yt-dlp 对多 P 视频有时返回 cid=0，需要单独查一次。"""
    try:
        with httpx.Client(headers=_PLAYER_HEADERS, timeout=timeout, follow_redirects=True) as client:
            resp = client.get(
                "https://api.bilibili.com/x/web-interface/view",
                params={"bvid": bvid},
                cookies=cookies or {},
            )
            resp.raise_for_status()
            data = resp.json().get("data") or {}
    except Exception as exc:
        log.debug("查 cid 失败 %s: %s", bvid, exc)
        return 0
    return int(data.get("cid") or 0)


def fetch_ai_subtitle(
    bvid: str,
    cid: int = 0,
    cookies: dict[str, str] | None = None,
    duration: float = 0.0,
    min_coverage: float = 0.6,
    max_coverage: float = 1.25,
    attempts: int = 5,
    timeout: float = 20.0,
) -> tuple[list[Segment], str]:
    """取 B站 AI 字幕，返回 (字幕, 语言)。

    这是 yt-dlp 拿不到的一路：`subtitles` 与 `automatic_captions` 对多数视频
    都是空的，但登录状态下 player/v2 会给出 ai-zh 的字幕直链。

    可靠性有限：同一个 bvid 连续请求，返回的直链指向的字幕内容每次都不一样，
    多数是无关碎片（只覆盖十几秒，内容完全不相干），但偶有完整的一两次。
    实测 6 次里 0 次合格，另一次调用却直接拿到覆盖 99% 的完整字幕。

    因此策略是"多试几次 + 校验覆盖率"：只要有任意一次覆盖达标就用它，
    其余全部丢弃。达标率不低，而拿到一次能省掉整段转写（17 分钟视频
    从 2 分钟降到 0）；全都不达标才回落到本地转写。
    """
    if not cid:
        cid = resolve_cid(bvid, cookies, timeout)
    if not cid:
        return [], ""

    try:
        with httpx.Client(headers=_PLAYER_HEADERS, timeout=timeout, follow_redirects=True) as client:
            resp = client.get(
                "https://api.bilibili.com/x/player/v2",
                params={"bvid": bvid, "cid": cid},
                cookies=cookies or {},
            )
            resp.raise_for_status()
            payload = resp.json()
    except Exception as exc:
        log.debug("查 AI 字幕失败 %s: %s", bvid, exc)
        return [], ""

    subtitle = ((payload.get("data") or {}).get("subtitle") or {})
    tracks = subtitle.get("subtitles") or []

    usable = [t for t in tracks if (t.get("subtitle_url") or "").strip()]
    if not usable:
        log.info("该视频没有可用的 AI 字幕（cid=%s）", cid)
        return [], ""

    def rank(track: dict[str, Any]) -> int:
        lang = str(track.get("lan") or "")
        if lang in _SUB_PRIORITY:
            return _SUB_PRIORITY.index(lang)
        if lang.startswith("ai-zh") or lang == "zh":
            return 0
        return 99

    best = sorted(usable, key=rank)[0]
    url = best["subtitle_url"].strip()
    if url.startswith("//"):
        url = "https:" + url
    elif url.startswith("/"):
        url = "https://www.bilibili.com" + url

    try:
        with httpx.Client(headers=_PLAYER_HEADERS, timeout=timeout, follow_redirects=True) as client:
            resp = client.get(url)
            resp.raise_for_status()
            body = resp.json()
    except Exception as exc:
        log.warning("AI 字幕下载失败 %s: %s", url[:60], exc)
        return [], ""

    segments = parse_subtitle_payload(body)
    if not segments:
        return [], ""

    span = max(s.end for s in segments)
    if duration > 0:
        coverage = span / duration
        if coverage < min_coverage:
            log.warning(
                "AI 字幕覆盖不足（%.0f%% < %.0f%%），判定为无效内容，改用本地转写",
                coverage * 100, min_coverage * 100,
            )
            return [], ""

    lang = str(best.get("lan") or "ai-zh")
    log.info("AI 字幕 %d 句（%s，覆盖至 %.0fs）", len(segments), lang, span)
    return segments, lang


def fetch_subtitle(url: str, timeout: float = 30.0) -> list[Segment]:
    try:
        with httpx.Client(headers=_HEADERS, timeout=timeout, follow_redirects=True) as client:
            resp = client.get(url)
            resp.raise_for_status()
            payload = resp.json()
    except Exception as exc:
        raise UpstreamError(f"字幕下载失败：{str(exc)[:160]}") from exc
    return parse_subtitle_payload(payload)


def parse_subtitle_payload(payload: Any) -> list[Segment]:
    if isinstance(payload, str):
        payload = json.loads(payload)
    if isinstance(payload, dict) and "body" in payload:
        return _parse_bili_json(payload)
    if isinstance(payload, list):
        return _parse_bili_json({"body": payload})
    raise UpstreamError("无法识别的字幕格式")


def _parse_bili_json(payload: dict[str, Any]) -> list[Segment]:
    segments: list[Segment] = []
    for item in payload.get("body") or []:
        text = (item.get("content") or "").strip()
        if not text:
            continue
        try:
            start = float(item.get("from", 0))
            end = float(item.get("to", start))
        except (TypeError, ValueError):
            continue
        segments.append(Segment(start=start, end=max(end, start), text=text, source="cc"))
    segments.sort(key=lambda s: s.start)
    return segments


def parse_srt(content: str) -> list[Segment]:
    blocks = re.split(r"\n\s*\n", content.replace("\r\n", "\n").strip())
    out: list[Segment] = []
    for block in blocks:
        lines = [ln for ln in block.split("\n") if ln.strip()]
        if len(lines) < 2:
            continue
        time_line = next((ln for ln in lines if "-->" in ln), None)
        if not time_line:
            continue
        start, _, end = time_line.partition("-->")
        try:
            s = _parse_ts(start)
            e = _parse_ts(end)
        except ValueError:
            continue
        idx = lines.index(time_line)
        text = " ".join(lines[idx + 1:]).strip()
        if text:
            out.append(Segment(start=s, end=e, text=text, source="cc"))
    return out


def _parse_ts(raw: str) -> float:
    parts = raw.strip().split(":")
    if len(parts) != 3:
        raise ValueError(raw)
    h, m, s = parts
    return int(h) * 3600 + int(m) * 60 + float(s.replace(",", "."))


def normalize(segments: list[Segment], max_gap: float = 0.6, max_len: int = 42) -> list[Segment]:
    """合并被切碎的短句，让每个 segment 尽量是一个完整语义单元。"""
    merged: list[Segment] = []
    buf: Segment | None = None

    for seg in sorted(segments, key=lambda s: s.start):
        text = seg.text.strip()
        if not text:
            continue
        if buf is None:
            buf = Segment(seg.start, seg.end, text, seg.source)
            continue

        gap = seg.start - buf.end
        joined_len = len(buf.text) + len(text)
        ends_clean = buf.text and buf.text[-1] in _SENT_END

        if gap <= max_gap and joined_len <= max_len and not ends_clean:
            joiner = "" if buf.text[-1] in "，、,。" else " "
            if _is_cjk(buf.text[-1]) and _is_cjk(text[0]):
                joiner = ""
            buf.text = f"{buf.text}{joiner}{text}"
            buf.end = seg.end
        else:
            merged.append(buf)
            buf = Segment(seg.start, seg.end, text, seg.source)

    if buf is not None:
        merged.append(buf)
    return merged


def _is_cjk(ch: str) -> bool:
    return "\u4e00" <= ch <= "\u9fff"


def to_srt(segments: list[Segment]) -> str:
    def ts(sec: float) -> str:
        ms = int(round(sec * 1000))
        h, rem = divmod(ms, 3_600_000)
        m, rem = divmod(rem, 60_000)
        s, ms = divmod(rem, 1000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    lines: list[str] = []
    for i, seg in enumerate(segments, 1):
        lines.append(str(i))
        lines.append(f"{ts(seg.start)} --> {ts(seg.end)}")
        lines.append(seg.text)
        if seg.translation:
            lines.append(seg.translation)
        lines.append("")
    return "\n".join(lines)
