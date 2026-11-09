"""字幕校验：规则层 + 术语锚定 + 音画互校。

设计原则：只做**可解释的确定性修正**，不猜、不改写。
核心判据是"拼音完全相同但汉字不同"——这是同音误写的强证据，
比让模型润色可靠，也零成本。

音画互校是本项目独有的能力：画面上的 PPT 文字是 UP 主亲手打的，
可以作为字幕的权威参照。纯音频工具做不到这一点。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Iterable

from pypinyin import lazy_pinyin

from ..core.models import Segment

log = logging.getLogger(__name__)

_CJK = r"\u4e00-\u9fff"
# 只校验 3-4 字的中文片段：2 字太容易误报（"时候" vs "事后"），5 字以上命中率低
_WIN_SIZES = (3, 4)
# 常见通用词，即便拼音撞上也不改 —— 这类词被误改的代价远大于收益
_STOPWORDS = {
    "我们", "你们", "他们", "这个", "那个", "什么", "怎么", "这样", "那样",
    "时候", "现在", "可以", "应该", "需要", "因为", "所以", "但是", "如果",
    "已经", "还是", "就是", "一个", "一些", "这些", "那些", "自己", "大家",
    "今天", "然后", "其实", "可能", "知道", "觉得", "看到", "没有", "不是",
    "一定", "非常", "比较", "很多", "东西", "问题", "地方", "时间", "情况",
}
_REPEAT_RE = re.compile(rf"([{_CJK}]{{2,6}})\1+")
_PUNCT_RE = re.compile(r"([，。！？、；：])\1+")
_ASCII_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_\-]{2,}")


@dataclass
class Fix:
    seg_index: int
    start: float
    wrong: str
    right: str
    reason: str  # rule | ocr | meta
    context: str = ""
    pos: int = -1

    def to_dict(self) -> dict:
        return {
            "start": round(self.start, 2),
            "wrong": self.wrong,
            "right": self.right,
            "reason": self.reason,
            "context": self.context,
        }


@dataclass
class Report:
    fixes: list[Fix] = field(default_factory=list)
    checked: int = 0
    source: str = ""
    skipped_reason: str = ""

    @property
    def count(self) -> int:
        return len(self.fixes)

    def to_dict(self) -> dict:
        by_reason: dict[str, int] = {}
        for f in self.fixes:
            by_reason[f.reason] = by_reason.get(f.reason, 0) + 1
        return {
            "count": self.count,
            "checked": self.checked,
            "source": self.source,
            "by_reason": by_reason,
            "fixes": [f.to_dict() for f in self.fixes[:50]],
        }


def _py(text: str) -> str:
    return "".join(lazy_pinyin(text))


def build_index(trusted: str) -> dict[str, set[str]]:
    """从可信文本建立「拼音 → 汉字写法」索引。"""
    index: dict[str, set[str]] = {}
    for chunk in _cjk_runs(trusted):
        n = len(chunk)
        for size in _WIN_SIZES:
            for i in range(n - size + 1):
                token = chunk[i: i + size]
                if token in _STOPWORDS:
                    continue
                index.setdefault(_py(token), set()).add(token)
    return index


def _cjk_runs(text: str) -> Iterable[str]:
    """切出连续中文片段，跨标点不组词。"""
    for run in re.findall(rf"[{_CJK}]{{2,}}", text or ""):
        yield run


def verify(
    segments: list[Segment],
    trusted_text: str,
    source: str = "ocr",
    max_fixes: int = 200,
) -> Report:
    """比对字幕与可信文本，返回修正建议（不修改原数据）。"""
    report = Report(checked=len(segments), source=source)
    if not segments:
        return report

    index = build_index(trusted_text)
    if not index:
        report.skipped_reason = "可信文本为空，跳过校验"
        return report

    for i, seg in enumerate(segments):
        if len(report.fixes) >= max_fixes:
            break
        fixes = _check_sentence(seg.text, index, i, seg.start)
        report.fixes.extend(fixes)

    return report


def _check_sentence(text: str, index: dict[str, set[str]], idx: int, start: float) -> list[Fix]:
    fixes: list[Fix] = []

    # 规则 1：机械重复（"这个这个这个"）
    for m in _REPEAT_RE.finditer(text):
        word = m.group(1)
        if len(word) >= 2:
            fixes.append(Fix(idx, start, m.group(0), word, "rule", _ctx(text, m.start()), m.start()))

    # 规则 2：音画互校 —— 拼音相同、汉字不同
    for run in _cjk_runs(text):
        offset = text.find(run)
        n = len(run)
        for size in _WIN_SIZES:
            for i in range(n - size + 1):
                token = run[i: i + size]
                if token in _STOPWORDS:
                    continue
                candidates = index.get(_py(token))
                if not candidates or token in candidates:
                    continue
                # 只有一个候选才改；多个候选说明存在歧义，宁可不动
                if len(candidates) != 1:
                    continue
                right = next(iter(candidates))
                if len(right) != len(token) or right == token:
                    continue
                # 目标写法本来就出现在句子里，说明不是误写
                if right in text:
                    continue
                pos = offset + i
                fixes.append(Fix(idx, start, token, right, "ocr", _ctx(text, pos), pos))

    # 重叠修正只保留最长的那个，否则"卷机神经"+"机神经"会重复改同一处
    fixes.sort(key=lambda f: (-len(f.wrong), f.pos))
    kept: list[Fix] = []
    taken: list[tuple[int, int]] = []
    for fix in fixes:
        lo, hi = fix.pos, fix.pos + len(fix.wrong)
        if fix.pos < 0:
            kept.append(fix)
            continue
        if any(lo < h and l < hi for l, h in taken):
            continue
        kept.append(fix)
        taken.append((lo, hi))
    kept.sort(key=lambda f: f.pos)
    return kept


def _ctx(text: str, pos: int, radius: int = 12) -> str:
    lo = max(0, pos - radius)
    hi = min(len(text), pos + radius)
    return text[lo:hi]


def apply_fixes(segments: list[Segment], report: Report) -> list[Segment]:
    """把修正应用到句子上，返回新列表（原列表不变）。"""
    if not report.fixes:
        return list(segments)

    by_index: dict[int, list[Fix]] = {}
    for fix in report.fixes:
        by_index.setdefault(fix.seg_index, []).append(fix)

    out: list[Segment] = []
    for i, seg in enumerate(segments):
        text = seg.text
        # 长词优先，避免短词先替换后把长词打断
        for fix in sorted(by_index.get(i, []), key=lambda f: -len(f.wrong)):
            text = text.replace(fix.wrong, fix.right, 1)
        out.append(Segment(start=seg.start, end=seg.end, text=text, source=seg.source))
    return out


def normalize_text(text: str) -> str:
    """纯规则清理：连续标点、多余空白。"""
    cleaned = _PUNCT_RE.sub(r"\1", text or "")
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
    return cleaned


def unify_ascii_case(segments: list[Segment], trusted_text: str) -> tuple[list[Segment], list[Fix]]:
    """把字幕里小写的英文术语统一成可信文本里的写法。"""
    variants: dict[str, str] = {}
    for word in _ASCII_WORD_RE.findall(trusted_text or ""):
        variants.setdefault(word.lower(), word)

    fixes: list[Fix] = []
    out: list[Segment] = []
    for i, seg in enumerate(segments):
        text = seg.text
        for m in _ASCII_WORD_RE.finditer(text):
            word = m.group(0)
            canonical = variants.get(word.lower())
            if canonical and canonical != word and canonical.lower() == word.lower():
                if canonical not in text:
                    fixes.append(Fix(i, seg.start, word, canonical, "meta", _ctx(text, m.start())))
                    text = text.replace(word, canonical, 1)
        out.append(Segment(seg.start, seg.end, normalize_text(text), seg.source))
    return out, fixes


def trusted_text_from(*parts: str) -> str:
    return " ".join(p for p in parts if p)


def rebuild_chunk_text(chunks, segments: list[Segment]) -> None:
    """字幕被修正后，按时间重新拼接每个知识块的文本。

    没有语音时（转写 0 句）知识块是靠画面切出来的，正文就是画面文字；
    这时不能拿空的 segments 去重拼——那会把块正文整片清空。
    """
    if not segments:
        return
    for chunk in chunks:
        chunk.text = "".join(
            s.text for s in segments
            if chunk.start - 0.01 <= s.start < chunk.end
        ).strip()
