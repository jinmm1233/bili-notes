"""知识库：扫描输出目录，把已生成的笔记索引成可浏览、可检索的集合。

不引入数据库——笔记本身就是文件，索引每次扫描即可，几百篇的量级毫秒级完成。
"""
from __future__ import annotations

import datetime
import json
import logging
import os
import re
import shutil
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

from ..core.config import Settings
from . import lecture

log = logging.getLogger(__name__)

_FM_RE = re.compile(r"^---\n(.*?)\n---\n", re.S)
_CHAPTER_RE = re.compile(r"^### ", re.M)
_IMAGE_RE = re.compile(r"!\[[^\]]*\]\((assets/[^)]+)\)")
_BV_RE = re.compile(r"BV[0-9A-Za-z]{10}")
_PAGE_RE = re.compile(r"[?&]p=(\d+)")
# 标题里的集数标记：p12 / P3 / 第5期 / 第12讲 …
# 用命中位置把标题切两半：前半是合集名，后半是本集标题。
_PART_RE = re.compile(
    r"(?:^|[\s\-—_（(【\[:：])[pP]\s*(\d{1,3})(?=$|[\s\-—_）)】\]:：，,。·|])"
    r"|第\s*(\d{1,3})\s*[集期讲节课话部]"
)

# 没记来源收藏夹的笔记（早期产出、单条添加）统一归到这里。
# 和 B 站“默认收藏夹”的语义一致：没指定去哪儿的视频就存在默认收藏夹里，
# 所以这里不做“未归类”这种空组，直接算作默认收藏夹的一员。
DEFAULT_COLLECTION = "默认收藏夹"

# 自建收藏夹只是一份名字清单，真正的归属写在每篇笔记的 frontmatter 里，
# 这样笔记单独拷走也带着分类，不依赖这个文件。
_COLLECTION_FILE = "collections.json"

# frontmatter 里的 created 只精确到分钟，历史数据可能还带秒、或只有日期。
# 宽进：几种写法都试一遍；都不成就退回文件时间。
_CREATED_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d",
    "%Y/%m/%d %H:%M",
    "%Y/%m/%d",
)


def created_ts(raw: str, fallback: float) -> float:
    """笔记“生成时间”的时间戳。列表排序用它，不用文件修改时间。

    mtime 会被任何一次重写刷新——补结构段、重建导图、修公式，都会把所有
    笔记的 mtime 顶到“刚刚”，顺序于是集体洗牌，最新生成的那篇反而掉到后面。
    created 写在 frontmatter 里，属于内容本身，改正文不会动它。
    """
    text = (raw or "").strip().strip('"').strip("'")
    if text:
        for fmt in _CREATED_FORMATS:
            try:
                return datetime.datetime.strptime(text, fmt).timestamp()
            except ValueError:
                continue
    return fallback


# 列表排序口径。默认按“生成时间”，因为 mtime 会被任何一次重写刷新
# （补结构段、重建导图、修公式），用它排会集体洗牌。但有人习惯“最近动过的在前”，
# 所以做成可选项，由前端记住选择。
SORT_CREATED = "created"
SORT_MTIME = "mtime"
SORTS = (SORT_CREATED, SORT_MTIME)


def normalize_sort(order: str) -> str:
    return order if order in SORTS else SORT_CREATED


def sort_notes(notes: list[dict[str, Any]], order: str = SORT_CREATED) -> list[dict[str, Any]]:
    """按指定口径倒序重排。

    created：笔记生成时间（frontmatter），内容属性，重写正文不影响它。
    mtime：文件修改时间，“最近动过的”排前面——批量改过排版时顺序会一起变，
           这正是选它的人想要的。
    两种口径都留了兜底键，保证同值时顺序稳定，不会每次刷新跳来跳去。
    """
    if normalize_sort(order) == SORT_MTIME:
        return sorted(notes, key=lambda n: (n["mtime"], n["created_ts"], n["id"]), reverse=True)
    return sorted(notes, key=lambda n: (n["created_ts"], n["mtime"], n["id"]), reverse=True)


def _parse_frontmatter(text: str) -> dict[str, str]:
    match = _FM_RE.match(text)
    if not match:
        return {}
    data: dict[str, str] = {}
    for line in match.group(1).split("\n"):
        if ":" not in line or line.lstrip().startswith("#"):
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        # 去掉成对的引号；标题里含冒号时 partition 只切第一刀，后面原样保留
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        data[key] = value
    return data


def scan(settings: Settings, order: str = SORT_CREATED) -> list[dict[str, Any]]:
    root = Path(settings.output_dir)
    if not root.is_dir():
        return []

    notes: list[dict[str, Any]] = []
    extra_meta: list[dict[str, Any]] = []
    for folder in root.iterdir():
        if not folder.is_dir():
            continue
        md = folder / "notes.md"
        if not md.is_file():
            continue
        try:
            text = md.read_text(encoding="utf-8")
        except OSError:
            continue

        meta = _parse_frontmatter(text)
        images = _IMAGE_RE.findall(text)
        assets = folder / "assets"
        cover = ""
        if images:
            candidate = folder / images[0]
            if candidate.is_file():
                cover = images[0]
        elif assets.is_dir():
            first = next(iter(sorted(assets.glob("*.jpg"))), None)
            if first:
                cover = f"assets/{first.name}"

        stat = md.stat()
        created = meta.get("created", "")
        notes.append({
            "id": folder.name,
            "title": meta.get("title") or folder.name,
            "uploader": meta.get("uploader", ""),
            "collection": (meta.get("collection") or "").strip() or DEFAULT_COLLECTION,
            "source": meta.get("source", ""),
            "duration": meta.get("duration", ""),
            "created": created,
            # 排序与“最近更新”都用它，别用 mtime（任何一次重写都会刷新 mtime）
            "created_ts": created_ts(created, stat.st_mtime),
            "model": meta.get("model", ""),
            "transcript": meta.get("transcript", ""),
            "tags": _parse_tags(meta.get("tags", "")),
            "chapters": len(_CHAPTER_RE.findall(text)),
            "images": len(images),
            "words": len(text),
            "cover": cover,
            "lecture": lecture.exists(folder),
            "mtime": stat.st_mtime,
        })
        extra_meta.append(_read_meta(folder))

    # 合集归属要横看全部笔记才能定：同一合集至少两集才值得折叠成一张卡。
    _annotate_series(notes, extra_meta)

    # 默认按生成时间倒序：最新生成的永远在最前，跟“文件什么时候被改过”无关。
    # 想看“最近动过的在前”，接口侧传 order=mtime 重排即可（见 sort_notes）。
    return sort_notes(notes, order)


def _read_meta(folder: Path) -> dict[str, Any]:
    """读导出时写下的 meta.json；老笔记没有这个文件也不能让扫描失败。"""
    try:
        data = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _part_info(title: str) -> tuple[str, int]:
    """从标题里拆出（合集名，集数）。没有集数标记就返回 ('', 0)。"""
    match = _PART_RE.search(title or "")
    if not match:
        return "", 0
    stem = (title or "")[:match.start()].strip(" \t-—_（(【[:：，,·|")
    order = int(match.group(1) or match.group(2) or 0)
    return stem, order


def _common_series_title(titles: list[str], explicit: str = "") -> str:
    """多 P 分集没有统一的投稿标题，用各集标题的公共前缀倒推合集名。

    公共前缀常停在半个集数标记上（p1 与 p12 会共到 "... p1"），
    所以收尾要把 "p" 和不完整数字一起削掉。
    """
    if explicit.strip():
        return explicit.strip()[:60]
    if not titles:
        return "未命名合集"
    prefix = os.path.commonprefix(titles)
    prefix = re.sub(r"[\s\-—_（(【\[:：]?[pP第]\s*\d*$", "", prefix)
    prefix = prefix.strip(" \t-—_（(【[:：，,·|")
    if len(prefix) < 6:
        return _part_info(titles[0])[0] or titles[0][:40]
    return prefix[:60]


def _annotate_series(notes: list[dict[str, Any]], extra: list[dict[str, Any]]) -> None:
    """给每篇笔记写 series_key / series_title / series_order / series_kind。

    判定优先级（都是本地信息，不联网、不依赖 B 站接口还活着）：
      1. 导出时记下的 series_id（UP 主合集展开导入时写入，最准）
      2. 同一个 BV 号、不同 p=N（多 P 投稿，如 CSAPP p12~p18）
      3. 标题有集数标记 + 足够长的公共前缀（老笔记、跨 BV 合集的兜底）
    少于两集的组不折叠——单篇就是普通笔记卡片。
    """
    info: list[dict[str, Any]] = []
    for note, raw in zip(notes, extra):
        source = str(note.get("source") or "") or str(raw.get("webpage_url") or "")
        bv_match = _BV_RE.search(source) or _BV_RE.search(str(raw.get("bvid") or ""))
        page_match = _PAGE_RE.search(source)
        stem, part_order = _part_info(str(note.get("title") or ""))
        try:
            page = int(page_match.group(1)) if page_match else int(raw.get("page_index") or 1)
        except (TypeError, ValueError):
            page = 1
        try:
            explicit_order = int(raw.get("series_order") or 0)
        except (TypeError, ValueError):
            explicit_order = 0
        info.append({
            "bv": bv_match.group(0) if bv_match else "",
            "page": page,
            "part": part_order or explicit_order,
            "stem": stem,
            "explicit_id": str(raw.get("series_id") or "").strip(),
            "explicit_title": str(raw.get("series_title") or "").strip(),
            "explicit_order": explicit_order,
        })

    groups: dict[str, list[int]] = {}
    kinds: dict[str, str] = {}
    titles: dict[str, str] = {}

    # 1) 明确的合集 id（展开导入时记的）
    by_id: dict[str, list[int]] = defaultdict(list)
    for i, x in enumerate(info):
        if x["explicit_id"]:
            by_id[x["explicit_id"]].append(i)
    for sid, idxs in by_id.items():
        if len(idxs) < 2:
            continue
        key = f"id:{sid}"
        groups[key] = idxs
        kinds[key] = "season"
        titles[key] = _common_series_title(
            [str(notes[i]["title"]) for i in idxs],
            next((info[i]["explicit_title"] for i in idxs if info[i]["explicit_title"]), ""),
        )

    assigned = {i for idxs in groups.values() for i in idxs}

    # 2) 同一 BV 的多 P：含 p=1（URL 不带参数）也要并进来
    by_bv: dict[str, list[int]] = defaultdict(list)
    for i, x in enumerate(info):
        if i in assigned or not x["bv"]:
            continue
        by_bv[x["bv"]].append(i)
    for bv, idxs in by_bv.items():
        if len(idxs) < 2 or len({info[i]["page"] for i in idxs}) < 2:
            continue
        key = f"pages:{bv}"
        groups[key] = idxs
        kinds[key] = "pages"
        titles[key] = _common_series_title(
            [str(notes[i]["title"]) for i in idxs],
            next((info[i]["explicit_title"] for i in idxs if info[i]["explicit_title"]), ""),
        )
    assigned = {i for idxs in groups.values() for i in idxs}

    # 3) 标题集数兜底：公共前缀至少 10 个字符、且至少两个不同集数
    by_stem: dict[str, list[int]] = defaultdict(list)
    for i, x in enumerate(info):
        if i in assigned or not x["stem"] or x["part"] <= 0:
            continue
        norm = re.sub(r"\s+", "", x["stem"]).lower()
        if len(norm) >= 10:
            by_stem[norm].append(i)
    for norm, idxs in by_stem.items():
        if len(idxs) < 2 or len({info[i]["part"] for i in idxs}) < 2:
            continue
        key = f"title:{norm}"
        groups[key] = idxs
        kinds[key] = "title"
        titles[key] = _common_series_title([str(notes[i]["title"]) for i in idxs])

    for note in notes:
        note["series_key"] = ""
        note["series_title"] = ""
        note["series_order"] = 0
        note["series_kind"] = ""
    for key, idxs in groups.items():
        for i in idxs:
            note = notes[i]
            note["series_key"] = key
            note["series_title"] = titles[key]
            note["series_kind"] = kinds[key]
            note["series_order"] = info[i]["page"] or info[i]["part"]


def series_groups(notes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把一组笔记里的合集聚成卡片数据（供知识库第二级渲染）。"""
    groups: dict[str, dict[str, Any]] = {}
    for note in notes:
        key = str(note.get("series_key") or "")
        if not key:
            continue
        group = groups.get(key)
        if group is None:
            groups[key] = {
                "key": key,
                "title": note.get("series_title") or "未命名合集",
                "kind": note.get("series_kind") or "",
                "count": 1,
                "words": note["words"],
                "images": note["images"],
                "mtime": note["created_ts"],
                "cover": note["cover"],
                "cover_id": note["id"],
                "latest": note["title"],
                "orders": [note.get("series_order") or 0],
            }
            continue
        group["count"] += 1
        group["words"] += note["words"]
        group["images"] += note["images"]
        group["orders"].append(note.get("series_order") or 0)
        if note["created_ts"] > group["mtime"]:
            group["mtime"] = note["created_ts"]
            group["latest"] = note["title"]
        if not group["cover"] and note["cover"]:
            group["cover"] = note["cover"]
            group["cover_id"] = note["id"]

    out = [g for g in groups.values() if g["count"] >= 2]
    for g in out:
        ordered = [o for o in g["orders"] if o]
        g["first_order"] = min(ordered) if ordered else 0
        g["last_order"] = max(ordered) if ordered else 0
        del g["orders"]
    return sorted(out, key=lambda g: g["mtime"], reverse=True)


def _parse_tags(raw: str) -> list[str]:
    if not raw:
        return []
    cleaned = raw.strip().strip("[]")
    return [t.strip().strip('"').strip("'") for t in cleaned.split(",") if t.strip()][:6]


def stats(notes: list[dict[str, Any]]) -> dict[str, Any]:
    total_words = sum(n["words"] for n in notes)
    return {
        "notes": len(notes),
        "words": total_words,
        "images": sum(n["images"] for n in notes),
        "chapters": sum(n["chapters"] for n in notes),
    }


def collections(
    notes: list[dict[str, Any]], extra: list[str] | None = None
) -> list[dict[str, Any]]:
    """按归属收藏夹分组，给出每个大类的规模与封面。

    “最近更新”取组内**最大**的生成时间，不依赖传入顺序——列表按修改时间排时
    第一篇未必是最新生成的那篇，靠“第一次出现”会算错。
    封面用第一张能拿到的（最新那篇可能没图）。
    extra 是用户自建、但暂时还没有笔记的收藏夹——不补进来的话，
    新建完就“看不见”，只能等搬进第一篇才出现。
    """
    groups: dict[str, dict[str, Any]] = {}
    for note in notes:
        name = note.get("collection") or DEFAULT_COLLECTION
        group = groups.get(name)
        if group is None:
            groups[name] = {
                "name": name,
                "count": 1,
                "words": note["words"],
                "images": note["images"],
                # 收藏夹卡片上那个日期 = 组里最近一篇笔记的生成时间
                "mtime": note["created_ts"],
                "cover": note["cover"],
                "cover_id": note["id"],
                "latest": note["title"],
                "series": {note["series_key"]} if note.get("series_key") else set(),
            }
            continue
        group["count"] += 1
        group["words"] += note["words"]
        group["images"] += note["images"]
        if note.get("series_key"):
            group["series"].add(note["series_key"])
        if note["created_ts"] > group["mtime"]:
            group["mtime"] = note["created_ts"]
            group["latest"] = note["title"]
        # 最新的那篇可能没图，往回找第一篇有封面的当门面
        if not group["cover"] and note["cover"]:
            group["cover"] = note["cover"]
            group["cover_id"] = note["id"]

    for raw in extra or []:
        name = (raw or "").strip()
        if name and name not in groups:
            groups[name] = {
                "name": name,
                "count": 0,
                "words": 0,
                "images": 0,
                "mtime": 0.0,
                "cover": "",
                "cover_id": "",
                "latest": "",
                "series": set(),
            }

    for name, group in groups.items():
        group["is_default"] = name == DEFAULT_COLLECTION
        group["series_count"] = len(group["series"])
        del group["series"]

    return sorted(groups.values(), key=lambda g: g["mtime"] or 0, reverse=True)


# ---------------------------------------------------------------- 自建收藏夹
# 收藏夹本身只是一个名字清单，真正的归属写在每篇笔记的 frontmatter 里。
# 这样笔记单独拷走也带着分类，不依赖这个文件。

def _collection_file(settings: Settings) -> Path:
    return Path(settings.data_dir) / _COLLECTION_FILE


def _read_custom(settings: Settings) -> list[str]:
    path = _collection_file(settings)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    items = data.get("collections") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return []
    return [str(x).strip() for x in items if str(x).strip()]


def _write_custom(settings: Settings, names: list[str]) -> None:
    """原子写：先落临时文件再改名，免得断电留下半截 JSON。"""
    path = _collection_file(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".part")
    tmp.write_text(
        json.dumps({"collections": names}, ensure_ascii=False, indent=2),
        encoding="utf-8",
        newline="\n",
    )
    tmp.replace(path)


def custom_names(settings: Settings) -> list[str]:
    return _read_custom(settings)


def add_custom(settings: Settings, name: str) -> str:
    cleaned = (name or "").strip()
    if not cleaned:
        raise ValueError("收藏夹名不能为空")
    if len(cleaned) > 40:
        raise ValueError("收藏夹名最多 40 个字")
    if cleaned == DEFAULT_COLLECTION:
        raise ValueError("默认收藏夹已经存在")
    names = _read_custom(settings)
    if cleaned in names:
        raise ValueError(f"已经有「{cleaned}」这个收藏夹了")
    names.append(cleaned)
    _write_custom(settings, names)
    log.info("新建收藏夹 %s", cleaned)
    return cleaned


def drop_custom(settings: Settings, name: str) -> None:
    names = [n for n in _read_custom(settings) if n != name]
    _write_custom(settings, names)


def set_collection(settings: Settings, note_id: str, name: str) -> None:
    """把一篇笔记改归到某个收藏夹：只动 frontmatter 的 collection 那一行。

    正文一个字都不碰——分类是元信息，改它不该让正文被整体重写一遍
    （那会刷新文件时间，也会丢掉人工微调过的排版）。
    """
    folder = _note_folder(settings, note_id)
    md = folder / "notes.md"
    text = md.read_text(encoding="utf-8")
    target = (name or "").strip() or DEFAULT_COLLECTION

    match = _FM_RE.match(text)
    if match:
        body = match.group(1)
        if re.search(r"^collection:.*$", body, re.M):
            body = re.sub(
                r"^collection:.*$", f'collection: "{target}"', body, count=1, flags=re.M
            )
        else:
            body = body + f'\ncollection: "{target}"'
        text = f"---\n{body}\n---\n" + text[match.end():]
    else:
        text = f'---\ncollection: "{target}"\n---\n' + text

    md.write_text(text, encoding="utf-8", newline="\n")


def move_notes(settings: Settings, ids: list[str], to: str) -> dict[str, Any]:
    """批量搬收藏夹。单篇失败不影响其余，失败项原样报回去。"""
    target = (to or "").strip()
    if not target:
        raise ValueError("目标收藏夹不能为空")
    known = set(_read_custom(settings)) | {DEFAULT_COLLECTION}
    if target not in known:
        raise ValueError(f"没有「{target}」这个收藏夹")

    moved: list[str] = []
    failed: list[str] = []
    for note_id in ids:
        try:
            set_collection(settings, note_id, target)
            moved.append(note_id)
        except (OSError, ValueError):
            failed.append(note_id)
    log.info("搬移笔记到「%s」：成功 %d，失败 %d", target, len(moved), len(failed))
    return {"moved": moved, "failed": failed, "to": target}


def remove_collection(settings: Settings, name: str) -> dict[str, Any]:
    cleaned = (name or "").strip()
    if not cleaned:
        raise ValueError("要删哪个收藏夹？")
    if cleaned == DEFAULT_COLLECTION:
        raise ValueError("默认收藏夹不能删除")
    notes = scan(settings)
    ids = [n["id"] for n in notes if (n.get("collection") or DEFAULT_COLLECTION) == cleaned]
    for note_id in ids:
        set_collection(settings, note_id, DEFAULT_COLLECTION)
    drop_custom(settings, cleaned)
    log.info("删除收藏夹 %s，%d 篇笔记退回默认收藏夹", cleaned, len(ids))
    return {"deleted": cleaned, "moved": len(ids)}


def filter_by(notes: list[dict[str, Any]], collection: str) -> list[dict[str, Any]]:
    name = (collection or "").strip()
    if not name:
        return notes
    return [n for n in notes if (n.get("collection") or DEFAULT_COLLECTION) == name]


def _note_folder(settings: Settings, note_id: str) -> Path:
    root = Path(settings.output_dir).resolve()
    folder = (root / note_id).resolve()
    if root not in folder.parents and folder != root:
        raise ValueError("非法路径")
    if not folder.is_dir():
        raise FileNotFoundError(note_id)
    return folder


def get_note(settings: Settings, note_id: str) -> dict[str, Any]:
    """读单篇：正文 + 元信息 + 讲义（没有讲义时给空串，前端据此显示按钮）。"""
    folder = _note_folder(settings, note_id)
    md = folder / "notes.md"
    if not md.is_file():
        raise FileNotFoundError(note_id)

    text = md.read_text(encoding="utf-8")
    meta = _parse_frontmatter(text)
    lecture_text = ""
    lecture_path = folder / lecture.LECTURE_FILE
    if lecture_path.is_file():
        try:
            lecture_text = lecture_path.read_text(encoding="utf-8")
        except OSError:
            lecture_text = ""

    # 生词数据（有词库且这篇是外语笔记时才有）：前端拿它做悬停气泡
    side = folder / "words.json"
    word_data = None
    if side.is_file():
        try:
            word_data = json.loads(side.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            word_data = None

    return {
        "id": folder.name,
        "title": meta.get("title") or folder.name,
        "markdown": text,
        "meta": meta,
        "lecture": lecture_text,
        "words": word_data,
    }


# ---------------------------------------------------------------- 检索
def _snippet(text: str, keyword: str, width: int = 40) -> str:
    """命中位置前后各取一段，压成一行——列表里只要看“为什么命中”。"""
    low = text.lower()
    pos = low.find(keyword.lower())
    if pos < 0:
        return ""
    start = max(0, pos - width)
    end = min(len(text), pos + len(keyword) + width)
    piece = re.sub(r"\s+", " ", text[start:end]).strip()
    # 切在中间时首尾可能留下半个词，用省略号示意
    if start > 0:
        piece = "…" + piece
    if end < len(text):
        piece = piece + "…"
    return piece


def search(settings: Settings, query: str, limit: int = 30) -> list[dict[str, Any]]:
    """全文关键词检索（标题 + 正文）。

    直接扫正文而不是维护倒排索引：几十篇、每篇几十 KB，扫一遍是毫秒级，
    省掉索引与文件不同步的麻烦。结果沿用 scan 的顺序（最新生成的在最前）。
    """
    word = (query or "").strip()
    if not word:
        return []
    low = word.lower()

    hits: list[dict[str, Any]] = []
    for note in scan(settings):
        try:
            text = (Path(settings.output_dir) / note["id"] / "notes.md").read_text(
                encoding="utf-8"
            )
        except OSError:
            continue
        if low not in text.lower() and low not in (note.get("title") or "").lower():
            continue
        row = dict(note)
        row["snippet"] = _snippet(text, word)
        row["hits"] = text.lower().count(low)
        hits.append(row)
        if len(hits) >= limit:
            break
    return hits


# ---------------------------------------------------------------- 删除笔记
# 不直接 rmtree：笔记是花时间整理出来的，先挪进回收站，后悔还能捞回来。
# 回收站只留最近 _TRASH_KEEP 项，超出的自动清掉最老的，免得越堆越大。

_TRASH_KEEP = 30


def delete_note(settings: Settings, note_id: str) -> dict[str, Any]:
    folder = _note_folder(settings, note_id)

    trash = Path(settings.data_dir) / "trash"
    trash.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    stem = folder.name[:60]
    target = trash / f"{stamp}-{stem}"
    seq = 1
    while target.exists():
        seq += 1
        target = trash / f"{stamp}-{stem}-{seq}"

    shutil.move(str(folder), str(target))
    pruned = _prune_trash(trash)

    # 这篇笔记生成的生词卡片要一起清：笔记进了回收站，卡片留在复习队列里
    # 就成了打不开来源的脏数据。以前只有 DELETE /api/cards/note/{id} 能清，
    # 而前端删笔记并不调它，等于这个接口没人用。
    cards_removed = 0
    try:
        from . import cards as card_svc  # noqa: PLC0415
        cards_removed = card_svc.remove_note(settings, note_id)
    except Exception as exc:                    # 清卡片失败不该挡住删笔记
        log.warning("删除笔记后清理卡片失败（不影响删除本身）：%s", exc)

    log.info("删除笔记 %s → 回收站 %s（顺带清理 %d 项 / 卡片 %d 张）",
             note_id, target.name, pruned, cards_removed)
    return {"id": note_id, "trash_name": target.name, "pruned": pruned,
            "cards_removed": cards_removed}


def _prune_trash(trash: Path, keep: int = _TRASH_KEEP) -> int:
    items = [p for p in trash.iterdir() if p.is_dir()]
    items.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    removed = 0
    for old in items[keep:]:
        shutil.rmtree(old, ignore_errors=True)
        removed += 1
    return removed
