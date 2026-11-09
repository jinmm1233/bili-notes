"""英语词库：导入、存储、合并索引。

只做"词 -> 释义"的静态数据，不含学习状态（那在 words.py / words.json）。
文件放 data_dir/wordbooks/<id>.json，一篇笔记默认用全部词库；
多个词库有同一个词时，保留第一条释义，只把来源词库名并列记下来。

解析刻意宽容：CSV / TSV / TXT / JSON 都收，列名中英文都认。
但坏行不静默丢弃——返回行号和原因，让人能改。
"""
from __future__ import annotations

import csv
import io
import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Callable

from ..core.config import Settings

log = logging.getLogger(__name__)

BOOK_DIR = "wordbooks"
MAX_BYTES = 5 * 1024 * 1024          # 5MB，够放十万词；再大多半是导错了文件
MAX_WORD_LEN = 40
ERROR_SAMPLES = 20                    # 只回传前若干条，避免整页刷屏

# 列名映射：同一个意思多种写法都认（导出工具千奇百怪）
_WORD_KEYS = ("word", "w", "单词", "词", "term", "headword", "vocabulary")
_POS_KEYS = ("pos", "词性", "type", "part of speech", "p")
_CN_KEYS = ("meaning", "cn", "释义", "中文", "中文释义", "definition",
            "translation", "意思", "解释", "zh")

_WORD_RE = re.compile(r"^[A-Za-z][A-Za-z'\-\s.]*$")
_POS_RE = re.compile(r"^([a-z]+\.(?:\s*&\s*[a-z]+\.)?)\s*(.+)$", re.I)


def book_dir(settings: Settings) -> Path:
    path = Path(settings.data_dir) / BOOK_DIR
    path.mkdir(parents=True, exist_ok=True)
    return path


def _pick(row: dict[str, str], keys: tuple[str, ...]) -> str:
    """从一行里挑出我们要的列，键名大小写、空格都不敏感。"""
    lowered = {str(k).strip().lower(): (v or "").strip() for k, v in row.items()}
    for key in keys:
        if key in lowered and lowered[key]:
            return lowered[key]
    return ""


def _clean_pos(pos: str) -> str:
    text = (pos or "").strip()
    if not text:
        return ""
    return text if text.endswith(".") else text + "."


def _split_text_line(line: str) -> tuple[str, str, str]:
    """一行 TXT 拆成 词 / 词性 / 释义。

    优先按 Tab 分列；没有 Tab 就按"第一个空白"切成词和剩余，
    剩余里若以 `adj.`、`n. & v.` 这类开头，再拆出词性。
    """
    raw = line.strip()
    if not raw:
        return "", "", ""
    if "\t" in raw:
        parts = [p.strip() for p in raw.split("\t") if p.strip()]
        word = parts[0] if parts else ""
        rest = parts[1:]
        pos, cn = "", ""
        if rest:
            match = _POS_RE.match(rest[0])
            # 「adj.」「n.」这种只有词性的一列也认（三列式 CSV/TXT 常见）
            if match:
                pos, cn = match.group(1), match.group(2).strip()
                cn = cn or " ".join(rest[1:])
            elif re.fullmatch(r"[a-z]+\.", rest[0], re.I):
                pos, cn = rest[0], " ".join(rest[1:])
            else:
                cn = " ".join(rest)
        return word, _clean_pos(pos), cn

    match = re.match(r"^(\S+)\s+(.*)$", raw)
    if not match:
        return raw, "", ""
    word, rest = match.group(1), match.group(2).strip()
    pos, cn = "", rest
    pos_match = _POS_RE.match(rest)
    if pos_match:
        pos, cn = pos_match.group(1), pos_match.group(2).strip()
    return word, _clean_pos(pos), cn


def parse(content: bytes, filename: str = "", keep_missing: bool = False) -> dict[str, Any]:
    """解析上传的词库文件，返回 {words, skipped, errors, format, missing}。

    words 已按词去重（同词保留第一条有释义的）。
    keep_missing=True 时，只有单词没有释义的行也收进来（cn 留空、记进 missing），
    之后走「AI 补释义」回填；默认还是当坏行报出来，免得手滑导错文件没感觉。
    """
    name = (filename or "").lower()
    text = content.decode("utf-8-sig", errors="replace")
    errors: list[dict[str, Any]] = []
    words: dict[str, dict[str, str]] = {}
    skipped = 0
    missing = 0

    def bad(lineno: int, raw: str, why: str) -> None:
        nonlocal skipped
        skipped += 1
        if len(errors) < ERROR_SAMPLES:
            errors.append({"line": lineno, "text": raw[:60], "why": why})

    def take(word: str, pos: str, cn: str, lineno: int, raw: str) -> None:
        nonlocal skipped, missing
        word = (word or "").strip()
        cn = (cn or "").strip()
        if not word:
            skipped += 1
            return
        if len(word) > MAX_WORD_LEN or not _WORD_RE.match(word):
            bad(lineno, raw, "看着不像英文单词")
            return
        key = word.lower()
        if key in words:
            # 同一个词出现两次：补全释义，不当作错误
            if not words[key]["cn"] and cn:
                words[key]["cn"] = cn
            if not words[key]["pos"] and pos:
                words[key]["pos"] = pos
            return
        if not cn:
            if keep_missing:
                words[key] = {"w": word, "pos": _clean_pos(pos), "cn": ""}
                missing += 1
                return
            bad(lineno, raw, "缺释义")
            return
        words[key] = {"w": word, "pos": _clean_pos(pos), "cn": cn}

    fmt = "json" if name.endswith(".json") else ("csv" if name.endswith((".csv", ".tsv")) else "text")

    if fmt == "json":
        try:
            data = json.loads(text)
        except ValueError as exc:
            return {"words": [], "skipped": 0, "format": fmt,
                    "errors": [{"line": 0, "text": "", "why": f"JSON 解析失败：{exc}"}]}
        items = data.get("words") if isinstance(data, dict) else data
        if not isinstance(items, list):
            return {"words": [], "skipped": 0, "format": fmt,
                    "errors": [{"line": 0, "text": "", "why": "JSON 里没有词条数组"}]}
        for i, item in enumerate(items, 1):
            if isinstance(item, dict):
                take(_pick(item, _WORD_KEYS), _pick(item, _POS_KEYS), _pick(item, _CN_KEYS), i, str(item)[:60])
            elif isinstance(item, str):
                w, pos, cn = _split_text_line(item)
                take(w, pos, cn, i, item)
        return {"words": list(words.values()), "skipped": skipped, "format": fmt, "errors": errors, "missing": missing}

    if fmt == "csv":
        sample = text[:4096]
        delimiter = "\t" if sample.count("\t") > sample.count(",") else ","
        reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
        fieldnames = [f.lower().strip() for f in (reader.fieldnames or [])]
        has_header = any(k in fieldnames for k in _WORD_KEYS)
        if has_header:
            for i, row in enumerate(reader, 2):
                take(_pick(row, _WORD_KEYS), _pick(row, _POS_KEYS), _pick(row, _CN_KEYS), i, str(row)[:60])
        else:
            # 没有表头：按"词 释义"或"词 词性 释义"的固定顺序读
            reader2 = csv.reader(io.StringIO(text), delimiter=delimiter)
            for i, row in enumerate(reader2, 1):
                cells = [c.strip() for c in row if c.strip()]
                if not cells:
                    continue
                pos, cn = "", ""
                if len(cells) >= 3:
                    pos, cn = cells[1], " ".join(cells[2:])
                elif len(cells) == 2:
                    if _POS_RE.match(cells[1]):
                        pos = cells[1]
                    else:
                        cn = cells[1]
                take(cells[0], pos, cn, i, ",".join(cells)[:60])
        return {"words": list(words.values()), "skipped": skipped, "format": fmt, "errors": errors, "missing": missing}

    for i, line in enumerate(text.splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        w, pos, cn = _split_text_line(line)
        take(w, pos, cn, i, line)
    return {"words": list(words.values()), "skipped": skipped, "format": fmt, "errors": errors, "missing": missing}


def _slug(name: str) -> str:
    base = Path(name or "词库").stem
    cleaned = re.sub(r"[^\w\u4e00-\u9fff-]+", "-", base).strip("-")
    return (cleaned or "book")[:40]


def save(settings: Settings, parsed: dict[str, Any], name: str = "",
         source: str = "") -> dict[str, Any]:
    """把解析结果落盘。同名词库覆盖旧的，不同名各存一份。"""
    words = parsed.get("words") or []
    label = (name or Path(source or "").stem or "未命名词库").strip()[:40]
    book_id = _slug(label)
    path = book_dir(settings) / f"{book_id}.json"
    payload = {
        "id": book_id,
        "name": label,
        "lang": "en",
        "source": source,
        "imported_at": time.time(),
        "count": len(words),
        "format": parsed.get("format", ""),
        "words": words,
    }
    tmp = path.with_suffix(".part")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8", newline="\n")
    tmp.replace(path)
    log.info("导入词库 %s：%d 个词（跳过 %d 行）", label, len(words), parsed.get("skipped", 0))
    return {k: v for k, v in payload.items() if k != "words"}


def load_all(settings: Settings) -> list[dict[str, Any]]:
    """列出所有词库（不含词条，省内存）。"""
    out: list[dict[str, Any]] = []
    for path in sorted(book_dir(settings).glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            log.warning("词库文件坏了，跳过：%s", path.name)
            continue
        out.append({
            "id": data.get("id") or path.stem,
            "name": data.get("name") or path.stem,
            "lang": data.get("lang", "en"),
            "count": data.get("count") or len(data.get("words") or []),
            "imported_at": data.get("imported_at") or 0,
            "source": data.get("source", ""),
        })
    out.sort(key=lambda b: b["imported_at"], reverse=True)
    return out


def delete(settings: Settings, book_id: str) -> bool:
    safe = re.sub(r"[^\w\u4e00-\u9fff-]+", "", book_id or "")
    if not safe:
        return False
    path = book_dir(settings) / f"{safe}.json"
    if not path.is_file():
        return False
    path.unlink()
    log.info("删除词库 %s", safe)
    return True


def index(settings: Settings) -> dict[str, dict[str, Any]]:
    """合并所有词库成 一个 hash 表：小写词 -> {w, pos, cn, books}。

    匹配时逐词查这张表，几千词条也是毫秒级；不必建倒排索引。
    """
    merged: dict[str, dict[str, Any]] = {}
    for path in sorted(book_dir(settings).glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        book = data.get("name") or path.stem
        for item in data.get("words") or []:
            word = str(item.get("w") or "").strip()
            if not word:
                continue
            key = word.lower()
            row = merged.get(key)
            if row is None:
                merged[key] = {
                    "w": word,
                    "pos": item.get("pos") or "",
                    "cn": item.get("cn") or "",
                    "books": [book],
                }
            else:
                if not row["cn"] and item.get("cn"):
                    row["cn"] = item["cn"]
                if not row["pos"] and item.get("pos"):
                    row["pos"] = item["pos"]
                if book not in row["books"]:
                    row["books"].append(book)
    return merged


def summary(settings: Settings) -> dict[str, Any]:
    books = load_all(settings)
    return {
        "books": books,
        "books_count": len(books),
        "total_words": sum(b["count"] for b in books),
        "index_size": len(index(settings)) if books else 0,
        "dir": str(book_dir(settings)),
    }


# ---------------------------------------------------------------- AI 补释义
# 词书导出时常常只有单词没有释义（比如自己抄的生词列表）。空释义的词在笔记里
# 只会显示「词库无释义」，等于白标。这里用配置好的模型批量补：一次一小批，
# 失败不写盘，跑不完报告剩余量让人再点一次。
FILL_BATCH = 30              # 一批 30 个词：快、便宜、超时也好重试
FILL_MAX_BATCHES = 12        # 单次接口最多 12 批（360 词），防止一次请求拖几分钟

_FILL_SYSTEM = (
    "你是英汉词典编辑。给定的每个英文单词给出一条简洁的中文释义和词性。"
    "要求：\n"
    "1. 释义格式如「n. 流水线」「v. 维持」，词性用 n./v./adj./adv./prep. 等；\n"
    "2. 释义尽量短（不超过 12 个字），取最常用的意思；\n"
    "3. 只输出 JSON：{\"词\": {\"pos\": \"n.\", \"cn\": \"流水线\"}}，"
    "不要解释、不要 Markdown 代码块。"
)


def _iter_book_paths(settings: Settings, book_id: str | None) -> list[Path]:
    if book_id:
        safe = re.sub(r"[^\w\u4e00-\u9fff-]+", "", book_id or "")
        path = book_dir(settings) / f"{safe}.json"
        return [path] if path.is_file() else []
    return sorted(book_dir(settings).glob("*.json"))


def missing_count(settings: Settings, book_id: str | None = None) -> int:
    """还有多少个词没释义。接口轻量轮询用。"""
    total = 0
    for path in _iter_book_paths(settings, book_id):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        total += sum(1 for w in data.get("words") or [] if not (w.get("cn") or "").strip())
    return total


def fill_definitions(settings: Settings, client: Any, book_id: str | None = None,
                     progress: Callable[[int, int], None] | None = None) -> dict[str, Any]:
    """给没有释义的词批量补中文释义。返回 {filled, failed, remaining}。

    一批失败只丢那一批（记 failed），不中断整体；写盘按词库整文件原子替换。
    """
    filled = failed = 0
    batches_done = 0
    pending_paths = _iter_book_paths(settings, book_id)
    seen_words: set[str] = set()

    for path in pending_paths:
        if batches_done >= FILL_MAX_BATCHES:
            break
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        words_list = data.get("words") or []
        blanks = [w for w in words_list
                  if not (w.get("cn") or "").strip() and (w.get("w") or "").strip()]
        if not blanks:
            continue

        changed = False
        for start in range(0, len(blanks), FILL_BATCH):
            if batches_done >= FILL_MAX_BATCHES:
                break
            batch = [w for w in blanks[start:start + FILL_BATCH] if w["w"].lower() not in seen_words]
            if not batch:
                continue
            batches_done += 1
            seen_words.update(w["w"].lower() for w in batch)
            names = "、".join(w["w"] for w in batch)
            try:
                answer = client.complete_json(_FILL_SYSTEM,
                                              f"请给这些词补释义：{names}", max_tokens=2048)
            except Exception as exc:
                log.warning("AI 补释义一批失败（%d 词）：%s", len(batch), exc)
                failed += len(batch)
                if progress:
                    progress(filled, failed)
                continue

            table = answer if isinstance(answer, dict) else {}
            for row in batch:
                hit = table.get(row["w"]) or table.get(row["w"].lower()) or {}
                cn = str(hit.get("cn") or "").strip() if isinstance(hit, dict) else ""
                pos = str(hit.get("pos") or "").strip() if isinstance(hit, dict) else ""
                if not cn:
                    failed += 1
                    continue
                row["cn"] = cn[:60]
                if pos and not row.get("pos"):
                    row["pos"] = _clean_pos(pos)
                filled += 1
                changed = True
            if progress:
                progress(filled, failed)

        if changed:
            data["words"] = words_list
            data["count"] = len(words_list)
            tmp = path.with_suffix(".part")
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8", newline="\n")
            tmp.replace(path)
            log.info("词库 %s 补释义：本机已填 %d 词", data.get("name") or path.stem, filled)

    return {
        "filled": filled,
        "failed": failed,
        "remaining": missing_count(settings, book_id),
        "batches": batches_done,
    }


# ---------------------------------------------------------------- 单词卡
# 词库只解决"看笔记时查得到"；单词卡解决"过几天还记不记得"。
# 只把**真的在笔记里遇到过的词**变成卡（words.json 是重算生词时落下的统计），
# 整本四六级倒进来就 3000 张卡，那就不是复习是刷库了。
WORD_NOTE_ID = "word:英语单词"


def encountered_words(settings: Settings) -> dict[str, int]:
    """所有笔记的 words.json 里出现过的词 -> 最大出现次数。"""
    root = Path(settings.output_dir)
    out: dict[str, int] = {}
    if not root.is_dir():
        return out
    for path in root.glob("*/words.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for row in data.get("words") or []:
            word = str(row.get("w") or "").strip()
            if not word:
                continue
            try:
                count = max(1, int(row.get("count") or 1))
            except (TypeError, ValueError):
                count = 1
            key = word.lower()
            out[key] = max(out.get(key, 0), count)
    return out


def to_cards(settings: Settings) -> dict[str, Any]:
    """把遇到过的生词变成单词卡（正面单词，背面释义）。返回 {added, dup, ...}。"""
    from . import cards as card_svc  # noqa: PLC0415  避免循环导入

    idx = index(settings)
    if not idx:
        return {"added": 0, "dup": 0, "hint": "还没有导入词库"}
    enc = encountered_words(settings)
    if not enc:
        return {"added": 0, "dup": 0, "hint": "还没有英语笔记的生词记录"}

    existing_ids = {c["id"] for c in card_svc.load(settings)}
    now = time.time()
    added = dup = candidates = 0
    cards = card_svc.load(settings)
    for key, count in sorted(enc.items()):
        row = idx.get(key)
        if not row:
            continue
        candidates += 1
        pos = f"{row['pos']} " if row.get("pos") else ""
        cn = (row.get("cn") or "").strip() or "（词库无释义，可在设置里用 AI 补）"
        back = f"{pos}{cn}" + (f"\n\n文中出现 {count} 次" if count > 1 else "")
        cid = card_svc._card_id(WORD_NOTE_ID, row["w"])
        if cid in existing_ids:
            dup += 1
            continue
        cards.append({
            "id": cid,
            "note_id": WORD_NOTE_ID,
            "note_title": "英语单词",
            "front": row["w"],
            "back": back,
            "concept": "单词",
            "kind": "单词",
            "created": now,
            "due": now,
            "interval": 0.0,
            "ease": 2.5,
            "reps": 0,
            "lapses": 0,
            "last": 0.0,
        })
        existing_ids.add(cid)
        added += 1
    if added:
        card_svc.save(settings, cards)
    return {"added": added, "dup": dup, "candidates": candidates}
