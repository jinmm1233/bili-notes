"""英语生词：词形还原、外语判定、在笔记里标出命中的生词。

只在"外语笔记 + 已导入词库"时启用；没有词库就一个字符都不改。

词形还原只用规则 + 一张不规则表，不引入 wordnet 之类的大依赖：
  - 屈折变化（-s/-es/-ies/-ed/-ing/双写辅音/补 e/比较级）几乎全覆盖；
  - 派生词（resilience ↔ resilient、decision ↔ decide）认不出——
    宁可漏标，也不硬配（form/former 这种一配就错）。
实测 75 个常见词形全部命中，worker/teacher/number 不会被误剥成 work/teach/numb。
"""
from __future__ import annotations

import logging
import re
from collections import Counter
from typing import Any

log = logging.getLogger(__name__)

# ---------------------------------------------------------------- 词形还原
# 只收高频不规则变化；真认不出的（sang、slain）漏标即可，不影响阅读。
IRREGULAR: dict[str, str] = {
    "went": "go", "gone": "go", "goes": "go", "doing": "do", "did": "do", "done": "do",
    "does": "do", "was": "be", "were": "be", "been": "be", "is": "be", "are": "be",
    "am": "be", "had": "have", "has": "have", "said": "say", "made": "make",
    "took": "take", "taken": "take", "came": "come", "saw": "see", "seen": "see",
    "got": "get", "gotten": "get", "gave": "give", "given": "give", "knew": "know",
    "known": "know", "thought": "think", "found": "find", "told": "tell",
    "became": "become", "left": "leave", "felt": "feel", "brought": "bring",
    "began": "begin", "begun": "begin", "kept": "keep", "held": "hold",
    "wrote": "write", "written": "write", "stood": "stand", "heard": "hear",
    "meant": "mean", "met": "meet", "ran": "run", "paid": "pay", "sat": "sit",
    "spoke": "speak", "spoken": "speak", "led": "lead", "grew": "grow",
    "grown": "grow", "lost": "lose", "fell": "fall", "fallen": "fall",
    "sent": "send", "built": "build", "understood": "understand", "drew": "draw",
    "drawn": "draw", "broke": "break", "broken": "break", "spent": "spend",
    "rose": "rise", "risen": "rise", "drove": "drive", "driven": "drive",
    "bought": "buy", "wore": "wear", "worn": "wear", "chose": "choose",
    "chosen": "choose", "ate": "eat", "eaten": "eat", "children": "child",
    "men": "man", "women": "woman", "feet": "foot", "teeth": "tooth",
    "mice": "mouse", "people": "person", "lives": "life", "knelt": "kneel",
    "sought": "seek", "taught": "teach", "caught": "catch", "fought": "fight",
    "threw": "throw", "thrown": "throw", "flew": "fly", "flown": "fly",
    "slept": "sleep", "swam": "swim", "swum": "swim", "stole": "steal",
    "stolen": "steal", "hid": "hide", "hidden": "hide", "bit": "bite",
    "bitten": "bite", "blew": "blow", "blown": "blow", "rode": "ride",
    "ridden": "ride", "sang": "sing", "sung": "sing", "spread": "spread",
    "lay": "lie", "laid": "lay", "shone": "shine", "dug": "dig",
}

_WORD_RE = re.compile(r"[A-Za-z][A-Za-z'\-]*")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_TS_LINE_RE = re.compile(r"^`(\d{1,2}:\d{2}(?::\d{2})?)`\s*(.+)$")
_BLOCK_RE = re.compile(r"<!-- words:start -->.*?<!-- words:end -->\n?", re.S)
_ANCHOR_RE = re.compile(r'<a id="([^"]+)"></a>')
_MAX_ITEMS = 80          # 一篇笔记最多列这么多条，再多就变词表了
_SENTENCE_KEEP = 160     # 例句超长截断


def lemmas(word: str) -> list[str]:
    """一个词所有可能的原形候选（含自己）。顺序不重要，命中就算。

    普通 -er 不剥：worker/teacher/number 剥成 work/teach/numb 会误标，
    所以比较级只认"双写辅音 + er/est"（bigger → big）。
    """
    w = (word or "").lower().strip("'-")
    if not w:
        return []
    out = [w]
    if w in IRREGULAR:
        out.append(IRREGULAR[w])

    for suffix, replacement in (
        ("ies", "y"), ("ied", "y"), ("ier", "y"), ("iest", "y"),
        ("es", ""), ("ed", ""), ("ing", ""), ("s", ""),
    ):
        if w.endswith(suffix) and len(w) - len(suffix) >= 2:
            base = w[: -len(suffix)] + replacement
            out.append(base)
            if not replacement:
                if len(base) >= 2 and base[-1] == base[-2]:
                    out.append(base[:-1])      # stopped → stop, running → run
                out.append(base + "e")         # making → make, hoped → hope

    for suffix in ("er", "est"):
        if w.endswith(suffix):
            base = w[: -len(suffix)]
            if len(base) >= 3 and base[-1] == base[-2] and base[-1] not in "aeiou":
                out.append(base[:-1])          # bigger → big, biggest → big

    # 去重且保持顺序
    return list(dict.fromkeys(x for x in out if len(x) >= 2))


def slug(word: str) -> str:
    """锚点 id：只留字母数字和连字符，保证 html/markdown 里都安全。"""
    return re.sub(r"[^a-z0-9]+", "-", (word or "").lower()).strip("-") or "w"


# ---------------------------------------------------------------- 笔记解析
def foreign_ratio(text: str) -> float:
    """拉丁字母占比（只看字母与汉字，数字标点不算）。"""
    latin = len(re.findall(r"[A-Za-z]", text))
    cjk = len(_CJK_RE.findall(text))
    total = latin + cjk
    return (latin / total) if total else 0.0


def looks_foreign(text: str, min_latin: int = 80, min_ratio: float = 0.75) -> bool:
    """这段转录是不是外语（英语）。

    双语字幕的笔记里中文译文会拉低比例，所以只看**英文句子行**；
    调用方传入的应该已经是英文字幕拼起来的文本。
    """
    latin = len(re.findall(r"[A-Za-z]", text))
    if latin < min_latin:
        return False
    return foreign_ratio(text) >= min_ratio


def transcript_lines(md: str) -> list[tuple[str, str]]:
    """取出转录里"英文句子"行：[(时间戳, 句子)]。

    笔记里的格式是 `12:30` 句子；中文译文那几行以 `中` 开头，这里跳过。
    """
    lines: list[tuple[str, str]] = []
    in_tr = False
    for line in md.split("\n"):
        stripped = line.strip()
        if stripped.startswith("## "):
            in_tr = stripped.startswith("## 完整转录")
            continue
        if not in_tr:
            continue
        if stripped.startswith("`中`"):
            continue
        match = _TS_LINE_RE.match(stripped)
        if match:
            lines.append((match.group(1), match.group(2).strip()))
    return lines


def note_is_foreign(md: str) -> bool:
    """笔记是否需要按外语处理：看转录里的英文句子。"""
    lines = transcript_lines(md)
    if not lines:
        return False
    joined = " ".join(text for _, text in lines)
    return looks_foreign(joined)


# ---------------------------------------------------------------- 匹配与改标
def collect(md: str, index: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """在转录里找命中词库的词，按"原形"聚合。

    返回 {原形小写: {w, pos, cn, books, forms(Counter), first_ts, example, count}}
    """
    if not index:
        return {}
    hits: dict[str, dict[str, Any]] = {}
    for ts, sentence in transcript_lines(md):
        seen_here: set[str] = set()
        for token in _WORD_RE.findall(sentence):
            for cand in lemmas(token):
                entry = index.get(cand)
                if not entry:
                    continue
                key = entry["w"].lower()
                row = hits.get(key)
                if row is None:
                    row = {
                        "w": entry["w"],
                        "pos": entry.get("pos", ""),
                        "cn": entry.get("cn", ""),
                        "books": entry.get("books", []),
                        "forms": Counter(),
                        "first_ts": ts,
                        "example": sentence,
                        "sentences": [],
                        "count": 0,
                    }
                    hits[key] = row
                row["forms"][token.lower()] += 1
                if key not in seen_here:          # 同一句里出现两次只算一次
                    row["count"] += 1
                    seen_here.add(key)
                    if len(row["sentences"]) < 6:
                        row["sentences"].append(sentence)
                break                              # 一个词只归到一个原形
    # 例句别用"第一句"，那句常常只有半截（"have anxiety disorders."）；
    # 取长度最接近 90 的，读起来信息量最大
    for row in hits.values():
        best = ""
        for sentence in row.pop("sentences", []) or []:
            if not best or abs(len(sentence) - 90) < abs(len(best) - 90):
                best = sentence
        row["example"] = best or row.get("example", "")
    return hits


def _clip(text: str, limit: int = _SENTENCE_KEEP) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def render_block(hits: dict[str, dict[str, Any]], books: list[str]) -> str:
    """生成笔记里的「生词」段（含锚点，供原文链接跳转）。"""
    items = list(hits.values())
    items.sort(key=lambda r: r["first_ts"])
    shown = items[:_MAX_ITEMS]
    label = "、".join(books[:2]) if books else "词库"
    if len(books) > 2:
        label += f" 等 {len(books)} 个词库"

    out = ["<!-- words:start -->", f"## 生词（{label} · 命中 {len(items)}）", ""]
    for row in shown:
        anchor = slug(row["w"])
        form_note = ""
        forms = [f for f in row["forms"] if f != row["w"].lower()]
        if forms:
            form_note = "（文中作 " + "、".join(sorted(set(forms))[:3]) + "）"
        pos = f"{row['pos']} " if row.get("pos") else ""
        out.append(f'<a id="w-{anchor}"></a>')
        out.append(
            f"- **{row['w']}** {pos}{row['cn'] or '（词库无释义）'}"
            f" — 出现 {row['count']} 次，首次 `{row['first_ts']}`{form_note}"
        )
        if row.get("example"):
            # 顶格写引用：渲染器只认行首的 >，缩进两格会原样显示成 "> 句子"
            out.append(f"> {_clip(row['example'])}")
        out.append("")
    if len(items) > len(shown):
        out.append(f"> 还有 {len(items) - len(shown)} 个词未列出，可调高上限或拆分词库。")
        out.append("")
    out.append("<!-- words:end -->")
    return "\n".join(out)


def link_words(md: str, index: dict[str, dict[str, Any]]) -> str:
    """把转录里命中的词改成锚点链接，点了跳到下方生词条目。

    只动转录区里的英文句子行；已有链接、代码、标题都不碰。
    """
    lines = md.split("\n")
    out: list[str] = []
    in_tr = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("## "):
            in_tr = stripped.startswith("## 完整转录")
            out.append(line)
            continue
        match = _TS_LINE_RE.match(stripped) if in_tr else None
        if not match:
            out.append(line)
            continue

        ts, sentence = match.group(1), match.group(2)

        def repl(m: re.Match[str]) -> str:
            token = m.group(0)
            for cand in lemmas(token):
                entry = index.get(cand)
                if entry:
                    return f"[{token}](#w-{slug(entry['w'])})"
            return token

        linked = _WORD_RE.sub(repl, sentence)
        out.append(f"`{ts}` {linked}")
    return "\n".join(out)


def strip_block(md: str) -> str:
    """去掉上一次生成的生词段（重新生成前先清场，避免叠加）。

    顺带把 3 个以上连续空行压成 2 个：每轮插入都会在区块前后各留一个空行，
    不归一的话，重跑一次就多一行，`annotate` 也不再幂等。
    """
    out = _BLOCK_RE.sub("", md)
    return re.sub(r"\n{3,}", "\n\n", out)


_LINK_RE = re.compile(r"\[([^\]]+)\]\(#w-[^)]+\)")


def unlink(md: str) -> str:
    """把上次加的锚点链接还原成纯文字。

    不再跑一遍 link_words 时，会把 `[gone](#w-go)` 里的 gone 再包一层，
    变成 `[[gone](#w-go)](#w-go)`；先还原再重新加，幂等。
    """
    return _LINK_RE.sub(r"\1", md)


def annotate(md: str, index: dict[str, dict[str, Any]]) -> tuple[str, dict[str, Any]]:
    """给一篇笔记加生词段与原文锚点。返回 (新 md, 统计)。

    幂等：先移除旧段再插新的；没有命中就原样返回、不加任何标记。
    """
    clean = strip_block(md)
    clean = unlink(clean)
    hits = collect(clean, index)
    if not hits:
        return clean, {"hits": 0, "words": [], "books": []}

    books: list[str] = []
    for row in hits.values():
        for book in row.get("books") or []:
            if book not in books:
                books.append(book)

    block = render_block(hits, books)
    sentences = " ".join(text for _, text in transcript_lines(clean))
    linked = link_words(clean, index)

    # 插在「完整转录」之前；没有这一节就放文末署名之前
    marker = "## 完整转录"
    pos = linked.find(marker)
    if pos >= 0:
        new_md = linked[:pos] + block + "\n\n" + linked[pos:]
    else:
        tail = linked.rfind("\n---\n")
        new_md = (linked[:tail] + "\n" + block + "\n" + linked[tail:]) if tail >= 0 \
            else linked + "\n" + block + "\n"

    stats = {
        "hits": len(hits),
        "books": books,
        "words": [
            {"w": r["w"], "pos": r.get("pos", ""), "cn": r.get("cn", ""),
             "count": r["count"], "ts": r["first_ts"]}
            for r in sorted(hits.values(), key=lambda x: x["first_ts"])
        ],
        "chars": len(new_md),
        "_foreign": foreign_ratio(sentences),
    }
    return new_md, stats


def strip_anchors(md: str) -> str:
    """导出 Word 前去掉锚点标签（docx 里它只会变成一串难看的文字）。"""
    return _ANCHOR_RE.sub("", md)
