"""转录润色：用 LLM 给转录文本补全/修正标点，并给外语加中文译文。

只影响最终"完整转录"区（以及导出的 .srt），不改动已经生成的结构化笔记。
两条规则都只在配置了模型 Key 时生效；没有 Key 就原样返回。
解析失败一律回退到原句，绝不丢文本。

走 JSON 模式，比让模型自由发挥稳定得多。
"""
from __future__ import annotations

import logging
from typing import Callable

from ..core.config import Settings
from ..core.llm import LLMClient
from ..core.models import Segment
from ..core import zh
from .summarize import is_foreign

log = logging.getLogger(__name__)

ProgressFn = Callable[[float, str], None]
CancelFn = Callable[[], bool]

# 每批送多少句给模型：太大容易超 token、太小则往返次数多
_BATCH = 30

_SYSTEM = (
    "你是一名字幕校对小助手。我会发来一批按编号排列的转录文本行，"
    "每行是一句话的原文。请只做两件事：\n"
    "1) 给每行补全 / 修正标点符号，让它读起来通顺；"
    "绝对不要增删、改写或翻译原文字词（外语也只补标点，不翻译）。\n"
    "2) 如果该行是中文，dst 填空字符串；如果该行不是中文，dst 填这一行的中文译文"
    "（准确、口语化、不增删原意）。\n"
    "严格只返回 JSON：{\"lines\":[{\"src\":\"...\",\"dst\":\"...\"}]}，"
    "lines 的顺序和数量必须与输入逐条对应，不要输出任何解释文字。"
)


def _sample_is_foreign(segments: list[Segment]) -> bool:
    """用前若干句判断整体语种，避免逐句判断的抖动。"""
    sample = " ".join(s.text for s in segments[:200])
    return is_foreign(sample)


def polish_segments(
    segments: list[Segment],
    settings: Settings,
    client: LLMClient | None,
    on_progress: ProgressFn | None = None,
    cancel: CancelFn | None = None,
) -> list[Segment]:
    """对转录文本做润色：补标点 + （外语）加中文译文（一行原文一行译文）。

    没有模型 Key、或中文且关闭标点时，原样返回。
    """
    if not segments:
        return segments
    if client is None:
        return segments

    want_punct = settings.polish_punctuation
    want_trans = settings.bilingual_transcript and _sample_is_foreign(segments)
    if not want_punct and not want_trans:
        return segments

    mode = "标点 + 中文译文" if want_trans else "标点"
    log.info("转录润色开始：模式=%s，共 %d 句", mode, len(segments))

    out: list[Segment] = list(segments)
    total = len(segments)
    done = 0
    for start in range(0, total, _BATCH):
        if cancel and cancel():
            break
        batch = segments[start:start + _BATCH]
        user = "\n".join(f"{i + 1}. {s.text}" for i, s in enumerate(batch))
        try:
            data = client.complete_json(_SYSTEM, user, max_tokens=4096)
            lines = data.get("lines") or []
        except Exception as exc:  # noqa: BLE001
            log.warning("转录润色单批失败，本批回退原句：%s", exc)
            lines = []

        for i, seg in enumerate(batch):
            rec = lines[i] if i < len(lines) else {}
            src = (rec.get("src") or "").strip()
            dst = (rec.get("dst") or "").strip()
            out[start + i] = Segment(
                start=seg.start,
                end=seg.end,
                source=seg.source,
                text=src or seg.text,
                translation=zh.to_simplified(dst) if (want_trans and dst) else "",
            )
        done += len(batch)
        if on_progress:
            pct = min(99.0, done / total * 100.0)
            on_progress(pct, f"转录润色 {pct:.0f}%")

    if on_progress:
        on_progress(100.0, "转录润色完成")
    n_trans = sum(1 for s in out if s.translation)
    log.info("转录润色完成：补译文 %d 句", n_trans)
    return out
