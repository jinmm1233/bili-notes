"""知识卡片：把笔记里的知识拆成能被"考"的小卡片，并安排复习时间。

为什么要有这一层：
  笔记是"读"的，卡片是"回忆"的。学习科学里最稳的两条结论是——
  主动回忆（retrieval practice）比反复阅读有效得多，间隔（spacing）比集中突击记得久。
  这两条都要求把知识拆成一个个可以被单独提问的最小单位，所以有了这个模块。

存储：单用户本地应用，直接用一个 JSON 文件（data_dir/cards.json），
写入走"临时文件 + 原子替换"，避免断电/崩溃留下半个文件。
排期：SM-2 的简化版（四档评分：忘记 / 模糊 / 记得 / 太简单），足够好用且可解释。
"""

from __future__ import annotations

import difflib
import hashlib
import json
import logging
import os
import re
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

from ..core.config import Settings
from ..core.llm import LLMClient

log = logging.getLogger(__name__)

CARD_FILE = "cards.json"
MAX_CARDS_PER_NOTE = 60         # 一篇笔记最多留 60 张，避免刷屏
GEN_LIMIT = 18                  # 单次生成上限
INTERVAL_CAP = 180.0            # 间隔最长半年
GRADES = ("again", "hard", "good", "easy")

# 近似重复：同一道题换个说法，相似度多在 0.87~0.97；
# 而"第二列下方化零"和"第二列上方化零"这类相邻步骤也会到 0.88，
# 光看相似度会误杀，所以额外比对"第X列 / 第X行 / 上方 / 下方"这些定位词：
# 两边都有定位词且不完全一致时，判定为不同题目。
SIMILAR_THRESHOLD = 0.86

_MATH_BLOCK = re.compile(r"\$\$[\s\S]*?\$\$")
_MATH_INLINE = re.compile(r"\$[^$]*?\$")
_ANCHOR = re.compile(r"第[一二三四五六七八九十百\d]+[行列步]|上方|下方|左侧|右侧")

_SYSTEM = """你是擅长把"看懂"变成"记住"的老师，正在为学生制作复习卡片。

【任务】把给定笔记拆成若干张独立可考的卡片。读者看到卡片正面要能"想起"，而不是"再读一遍原文"。

【每张卡片】
- front：一句能独立成立的问题。必须带上足够的语境，脱离原文也能看懂在问什么。
- back：简明答案，1~3 句；该给公式就给公式，该给步骤就给编号步骤。不要客套话。
- concept：这张卡考察的知识点名称（4~12 字，用于按概念归组，同一概念要用完全相同的写法）。
  必须是**能跨视频复用的知识点或方法名**（如「初等行变换法」「整数规划」）。
  不要填某道例题里的具体步骤或编号（如「第二列化零」「第一步」「把第三行乘 2」）——
  那些属于 kind=步骤，不是概念；换个视频它们就不成立。
- kind：概念 / 公式 / 步骤 / 易错 / 对比 之一。

【硬性要求】
1. 只考笔记里真实出现的内容，禁止编造。
2. **卡片必须通用、可迁移**：换个例子、换个视频仍然成立。具体的例题数据不许当答案核心。
   反例（不要）：
   ✗「求逆矩阵最后一步，如何把左侧主对角线化为 1？」→「第一行乘 1/3，第二行乘 -1，第三行乘 1/4」
   正例（要）：
   ✓「把左侧化成单位矩阵时，各行乘的系数由什么决定？」→「由该行主对角线元素决定：整行除以它；
     其它列随之变化，右侧同步就得到 $A^{-1}$。」
   自检：把这张卡给没看过这期视频的人，他能不能用到别的题上？不能就别出。
   如果某个点只能靠具体例子讲，就把问题抬到"方法 / 判断依据 / 为什么"这一层；
   抬不上来就**不出这张卡**（具体例题归自测卷，不归复习卡）。
   例外：公式自带的常数（如 $R=8.314$、$c=3\times10^8$）是知识本身，不算例题。
3. 优先考"为什么"和"怎么用"，其次才是定义复述；纯记忆型定义最多占三分之一。
4. 一道题只考一个点，不要把三个知识点塞进一张卡。
5. 数学公式用 LaTeX：行内 $...$，独立 $$...$$。
6. 不要出现"如图所示""上文提到"这类依赖原文位置的指代。
7. 宁缺毋滥：笔记里信息不足以出卡就不出。
8. 如果用户消息里给了【已有概念】，concept 必须**原样复用**其中一个名称；确实没有合适的才自拟。
9. 如果给了【已有卡片】，不要重复出题（换个说法问同一件事也算重复）。

【输出】严格 JSON：{"cards":[{"front":"","back":"","concept":"","kind":"概念"}]}
"""


# ---------------------------------------------------------------- 存储
def _path(settings: Settings) -> Path:
    return Path(settings.data_dir) / CARD_FILE


def load(settings: Settings) -> list[dict[str, Any]]:
    path = _path(settings)
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        log.warning("卡片文件损坏，按空处理：%s", path)
        return []
    cards = data.get("cards") if isinstance(data, dict) else data
    return [c for c in (cards or []) if isinstance(c, dict) and c.get("id")]


def save(settings: Settings, cards: list[dict[str, Any]]) -> None:
    path = _path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": 1, "updated": time.time(), "cards": cards}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)          # 原子替换，别留半个 JSON


# ---------------------------------------------------------------- 生成
def _norm(text: str) -> str:
    """去重用的归一化：忽略空白、标点、大小写差异。"""
    return re.sub(r"[\s\W_]+", "", (text or "").lower())


def _card_id(note_id: str, front: str) -> str:
    return hashlib.sha1(f"{note_id}|{_norm(front)}".encode()).hexdigest()[:16]


def _clean(text: Any, limit: int = 400) -> str:
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    return value[:limit]


# 例题卡检测：卡片该是"换个例子还成立"的知识，不是某道题的具体系数。
# 模型偶尔会把「第一行乘 1/3，第二行乘 -1，第三行乘 1/4」抄成答案——
# 这种卡复习时既记不住也迁移不了，属于自测卷该干的事。
_ROW_RE = re.compile(r"第\s*[一二三四五六七八九1-9]\s*行")
_ROW_OP_RE = re.compile(
    r"第\s*[一二三四五六七八九1-9]\s*行[^。；;，,\n]{0,20}?(?:乘以|乘上|乘|除以)"
    r"[^。；;，,\n]{0,10}?\$?-?\d"
)
_FRAC_RE = re.compile(
    r"\\d?frac\{[^{}]{1,12}\}\{[^{}]{1,12}\}|(?<![\w./])\d+\s*/\s*\d+(?![\w.])"
)
# 题干里直接贴了一个带数字的矩阵：这是"这道题"，不是知识
_MATRIX_DIGIT_RE = re.compile(
    r"\\begin\{(?:b|p|v|small)?matrix\}|\\begin\{array\}"
)
# "第一行的 -1 倍加到第二行"：行变换用了具体倍数
_ROW_SCALE_RE = re.compile(
    r"第\s*[一二三四五六七八九1-9]\s*行\s*的?\s*\$?-?"
    r"(?:\d+(?:\.\d+)?|\\frac\{[^{}]+\}\{[^{}]+\})\$?\s*倍"
)
# 具体逻辑公式实例（z_1∨¬z_2…），配合出现多个带下标变量才算
_LOGIC_SYM_RE = re.compile(r"\\(?:lor|land)\b")
_INDEXED_VAR_RE = re.compile(r"[a-zA-Z]_\{?\d")

# 通用公式里本来就带常数（$R=8.314$）不算例题；这里只盯"逐行操作 + 具体数字"
def is_example_card(front: str, back: str) -> bool:
    """这张卡是不是"某道例题的具体操作"（换个例子就不成立）。

    判据（任一成立即为例题）：
      1. 出现两个以上「第X行…乘/除以具体数字」；
      2. 出现两个以上不同分数；
      3. 题干贴了带数字的矩阵；
      4. 出现「第X行的 N 倍」这类具体系数；
      5. 题干是具体逻辑公式实例（多个带下标变量 + ∨/∧）。
    变量写法（第 i 行减第 j 行的 k 倍）与公式常数（$R=8.314$）不误伤。
    """
    text = f"{front}\n{back}"
    rows = _ROW_RE.findall(text)
    if len(rows) >= 2 and _ROW_OP_RE.search(text):
        return True
    fracs = {f.replace(" ", "") for f in _FRAC_RE.findall(text)}
    if len(fracs) >= 2:
        return True
    if _MATRIX_DIGIT_RE.search(front):
        return True
    if _ROW_SCALE_RE.search(text):
        return True
    if _LOGIC_SYM_RE.search(front) and len(set(_INDEXED_VAR_RE.findall(front))) >= 2:
        return True
    return False


def parse_cards(data: Any, dropped: list[str] | None = None) -> list[dict[str, str]]:
    """把模型返回的东西收拾成规范卡片；脏数据直接丢掉而不是抛异常。

    dropped 传一个列表进来，会被填入"因为是例题卡而被丢掉"的题干，
    用于日志和后续提示——不静默丢，免得人以为模型没出够卡。
    """
    raw = data.get("cards") if isinstance(data, dict) else data
    if not isinstance(raw, list):
        return []
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        front = _clean(item.get("front"))
        back = _clean(item.get("back"), 800)
        if len(front) < 4 or len(back) < 2:
            continue
        key = _norm(front)
        if key in seen:
            continue
        seen.add(key)
        if is_example_card(front, back):
            if dropped is not None:
                dropped.append(front[:60])
            continue
        kind = _clean(item.get("kind"), 8) or "概念"
        out.append({
            "front": front,
            "back": back,
            "concept": _clean(item.get("concept"), 24) or kind,
            "kind": kind if kind in ("概念", "公式", "步骤", "易错", "对比") else "概念",
        })
    return out


def build(client: LLMClient, note_md: str, note_title: str = "",
          known_concepts: Iterable[str] | None = None,
          existing_fronts: Iterable[str] | None = None) -> list[dict[str, str]]:
    """调用模型生成卡片（不落盘）。素材不足时返回空列表。

    known_concepts：笔记「关键概念」表里的术语。让模型原样复用，卡片才能挂到
        已有概念上——否则它会自造「变换顺序」「化零顺序」这类同义说法，
        概念页就散成一堆只出现一次的条目。
    existing_fronts："补充卡片"时把已有的问题告诉模型，减少重复出题。
    """
    body = re.sub(r"\A---\n[\s\S]*?\n---\n", "", note_md or "")
    if len(body.strip()) < 200:
        return []
    # 完整转录很长，只留开头一段当语境，避免把 token 全烧在口语转录上
    detail = re.search(r"<details>[\s\S]*?</details>", body)
    if detail and len(body) > 12000:
        body = body[: detail.start()] + "\n\n（完整转录已省略）\n" + body[detail.end():]
    if len(body) > 14000:
        body = body[:14000]

    extra = ""
    terms = [t for t in (known_concepts or []) if t][:24]
    if terms:
        extra += "\n\n【已有概念】" + "、".join(terms)
    old = [re.sub(r"\s+", " ", f).strip()[:90] for f in (existing_fronts or []) if f][:40]
    if old:
        extra += "\n\n【已有卡片】（不要重复）\n" + "\n".join(f"- {f}" for f in old)

    prompt = f"视频标题：{note_title}\n\n【笔记原文】\n{body}{extra}\n\n请按系统提示出卡。"
    data = client.complete_json(_SYSTEM, prompt, max_tokens=4000)
    dropped: list[str] = []
    items = parse_cards(data, dropped)
    if dropped:
        log.info("丢掉 %d 张例题卡（换个例子不成立）：%s", len(dropped), "；".join(dropped[:3]))
    # 概念名过闸：模型爱把「初等行变换法优势」「倍乘化单位阵」当新概念，
    # 这些其实是已有概念的侧面，不收回就会在索引里再长出一堆碎片。
    from . import concept_review  # noqa: PLC0415  避免循环导入

    concept_review.review_card_concepts(items, terms)
    return items


def _cmp_text(text: str) -> str:
    """相似度比较用的文本：公式压成 M，再去掉空白和标点。

    矩阵这类长公式在多道题里一模一样，不压掉的话会把相似度整体抬高，
    让"换个说法"和"换了一个步骤"都变成 0.9+，没法区分。
    """
    t = _MATH_BLOCK.sub("M", text or "")
    t = _MATH_INLINE.sub("M", t)
    return _norm(t)


def _similar(a: str, b: str) -> bool:
    """两道题是不是"换个说法问同一件事"。a、b 需先过 _cmp_text。"""
    if not a or not b:
        return False
    short, long = (a, b) if len(a) <= len(b) else (b, a)
    if len(short) / max(1, len(long)) < 0.6:
        return False
    ratio = difflib.SequenceMatcher(None, a, b).ratio()
    if ratio < SIMILAR_THRESHOLD:
        return False
    # 定位词不同 → 问的是不同位置/步骤，不能算重复
    ta, tb = set(_ANCHOR.findall(a)), set(_ANCHOR.findall(b))
    if ta and tb and ta != tb:
        return False
    # 数字不同 → 大概率是不同题（第 1 步 / 第 2 步、n=3 与 n=4）
    na, nb = set(re.findall(r"\d+", a)), set(re.findall(r"\d+", b))
    if na and nb and na != nb:
        return False
    return True


def add(settings: Settings, note_id: str, note_title: str,
        items: list[dict[str, str]]) -> tuple[int, int]:
    """写入卡片，按 (笔记, 问题) 去重。

    去重分两层：先比对 ID（完全相同），再用相似度挡掉"换了个说法"的重复题。
    后者很重要——"补充卡片"每点一次，模型都会用不同措辞把同一道题再出一遍。
    返回 (新增, 已存在/近似重复)。
    """
    cards = load(settings)
    existing = {c["id"] for c in cards}
    same_note = [
        _cmp_text(c.get("front") or "")
        for c in cards if c.get("note_id") == note_id
    ]
    now = time.time()
    added = dup = 0
    per_note = sum(1 for c in cards if c.get("note_id") == note_id)
    for item in items:
        if per_note >= MAX_CARDS_PER_NOTE:
            break
        key = _cmp_text(item["front"])
        cid = _card_id(note_id, item["front"])
        if cid in existing:
            dup += 1
            continue
        if any(_similar(key, old) for old in same_note):
            dup += 1
            continue
        cards.append({
            "id": cid,
            "note_id": note_id,
            "note_title": note_title or note_id,
            "front": item["front"],
            "back": item["back"],
            "concept": item.get("concept") or "概念",
            "kind": item.get("kind") or "概念",
            "created": now,
            "due": now,
            "interval": 0.0,
            "ease": 2.5,
            "reps": 0,
            "lapses": 0,
            "last": 0.0,
        })
        existing.add(cid)
        same_note.append(key)
        added += 1
        per_note += 1
    if added:
        save(settings, cards)
    return added, dup


def fronts_for(settings: Settings, note_id: str) -> list[str]:
    """这篇笔记已有的问题，用于让模型别重复出题。"""
    return [c.get("front") or "" for c in load(settings) if c.get("note_id") == note_id]


# ---------------------------------------------------------------- 历史重复清理
def find_duplicates(settings: Settings) -> list[dict[str, Any]]:
    """找出"换个说法问同一件事"的卡片，按组返回。

    只在**同一篇笔记内**比较：不同笔记讲同一个知识点各出一张卡是合理的，
    复习时按范围抽卡，不会天天撞。生成时已有去重，这里是给老数据兜底——
    旧版本的「补充卡片」不看已有题目，堆了一批同义题。
    """
    by_note: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for card in load(settings):
        by_note[str(card.get("note_id") or "")].append(card)

    groups: list[dict[str, Any]] = []
    for note_id, group in by_note.items():
        if len(group) < 2:
            continue
        texts = [(c, _cmp_text(c.get("front") or "")) for c in group]
        taken: set[str] = set()
        for i, (card, text) in enumerate(texts):
            if card["id"] in taken:
                continue
            same = [
                other for other, other_text in texts[i + 1:]
                if other["id"] not in taken and _similar(text, other_text)
            ]
            if not same:
                continue
            members = [card, *same]
            # 留复习次数多的；同分留更早创建的（复习历史更长，弃之可惜）
            members.sort(key=lambda c: (-int(c.get("reps") or 0), float(c.get("created") or 0)))
            keep, drop = members[0], members[1:]
            taken.add(keep["id"])
            taken.update(c["id"] for c in drop)
            groups.append({"note_id": note_id, "keep": keep, "drop": drop})
    return groups


def dedupe(settings: Settings, dry_run: bool = True) -> dict[str, Any]:
    """清理近似重复卡片：留复习次数多的那张，删掉同义的另一张。

    默认 dry_run=True 只报告不删——删卡片是不可逆的，先让人看一眼再确认。
    """
    groups = find_duplicates(settings)
    preview = [
        {
            "note_id": g["note_id"],
            "note_title": g["keep"].get("note_title") or g["note_id"],
            "keep": {"id": g["keep"]["id"], "front": g["keep"].get("front") or "",
                     "reps": int(g["keep"].get("reps") or 0)},
            "drop": [{"id": c["id"], "front": c.get("front") or ""} for c in g["drop"]],
        }
        for g in groups
    ]
    drop_ids = {d["id"] for g in preview for d in g["drop"]}
    if dry_run or not drop_ids:
        return {"groups": preview, "duplicates": len(drop_ids), "removed": 0}
    save(settings, [c for c in load(settings) if c.get("id") not in drop_ids])
    return {"groups": preview, "duplicates": len(drop_ids), "removed": len(drop_ids)}


# ---------------------------------------------------------------- 清理例题卡
def find_example_cards(settings: Settings) -> list[dict[str, Any]]:
    """找出历史里的例题卡（答案在复述某道题的具体系数/分数）。

    自测卷的错题卡（note_id 以 `quiz:` 开头）不算：那边本来就在做题，
    考"这道题的这一步"是正常的。这里针对的是**复习卡**混进了例题。
    """
    return [
        c for c in load(settings)
        if not str(c.get("note_id") or "").startswith("quiz:")
        and is_example_card(c.get("front") or "", c.get("back") or "")
    ]


def clean_examples(settings: Settings, dry_run: bool = True) -> dict[str, Any]:
    """删掉例题卡。默认只报告——删卡片不可逆，先让人看一眼。"""
    bad = find_example_cards(settings)
    preview = [
        {
            "id": c["id"],
            "front": c.get("front") or "",
            "back": (c.get("back") or "")[:120],
            "note_title": c.get("note_title") or c.get("note_id") or "",
        }
        for c in bad
    ]
    ids = {c["id"] for c in bad}
    if dry_run or not ids:
        return {"cards": preview, "count": len(ids), "removed": 0}
    save(settings, [c for c in load(settings) if c.get("id") not in ids])
    log.info("删除 %d 张例题卡", len(ids))
    return {"cards": preview, "count": len(ids), "removed": len(ids)}


# ---------------------------------------------------------------- 排期
def schedule(card: dict[str, Any], grade: str, now: float | None = None) -> dict[str, Any]:
    """SM-2 简化版：四档评分 → 下次复习时间。纯函数，方便单测。"""
    now = time.time() if now is None else now
    ease = float(card.get("ease") or 2.5)
    interval = float(card.get("interval") or 0.0)
    reps = int(card.get("reps") or 0)

    if grade not in GRADES:
        grade = "good"

    if grade == "again":
        ease = max(1.3, ease - 0.2)
        interval = 10 / 1440           # 10 分钟后同一场复习里再来一次
        reps = 0
        card["lapses"] = int(card.get("lapses") or 0) + 1
    elif grade == "hard":
        ease = max(1.3, ease - 0.15)
        interval = 1.0 if reps == 0 else max(1.0, round(interval * 1.2))
        reps += 1
    elif grade == "good":
        if reps == 0:
            interval = 1.0
        elif reps == 1:
            interval = 3.0
        else:
            interval = max(1.0, round(interval * ease))
        reps += 1
    else:  # easy
        ease = min(2.8, ease + 0.15)
        if reps == 0:
            interval = 3.0
        else:
            interval = max(1.0, round(interval * ease * 1.3))
        reps += 1

    interval = min(INTERVAL_CAP, interval)
    card["ease"] = round(ease, 3)
    card["interval"] = interval
    card["reps"] = reps
    card["last"] = now
    card["due"] = now + interval * 86400
    return card


def _human_gap(interval_days: float) -> str:
    """把"几天后"说人话：10 分钟 / 3 小时 / 5 天 / 2 个月。"""
    if interval_days * 1440 < 60:
        return f"{max(1, round(interval_days * 1440))} 分钟后"
    if interval_days < 1:
        return f"{max(1, round(interval_days * 24))} 小时后"
    if interval_days < 30:
        return f"{round(interval_days)} 天后"
    if interval_days >= INTERVAL_CAP - 0.5:
        return "半年后"                      # 封顶就是 180 天，直接说半年更好懂
    return f"约 {max(1, round(interval_days / 30))} 个月后"


def preview(card: dict[str, Any], now: float | None = None) -> dict[str, str]:
    """四档评分各自会把这个卡推到多久之后。

    前端按钮上要显示"忘记 · 10 分钟后 / 记得 · 3 天后"这类提示——
    在这里算而不是前端另写一份，保证显示的和真正排期用的是同一套规则。
    """
    out: dict[str, str] = {}
    for grade in GRADES:
        probe = dict(card)
        schedule(probe, grade, now=now)
        out[grade] = _human_gap(float(probe["interval"]))
    return out


def _in_scope(card: dict[str, Any], note_ids: set[str] | None) -> bool:
    """note_ids 为 None 表示不筛选（全部）。空集合表示"这个范围里没有笔记"，结果应为空。"""
    if note_ids is None:
        return True
    return str(card.get("note_id") or "") in note_ids


def due(settings: Settings, limit: int = 30, now: float | None = None,
        note_ids: set[str] | None = None,
        include_future: bool = False) -> list[dict[str, Any]]:
    """到期的卡片。note_ids 用于"只复习某个收藏夹/某几篇笔记"。

    include_future=True 时不看到期时间，全部按到期顺序排——给"这个范围还没有到期的，
    但我现在想练一遍"用。
    """
    now = time.time() if now is None else now
    cards = [c for c in load(settings) if _in_scope(c, note_ids)]
    if not include_future:
        cards = [c for c in cards if float(c.get("due") or 0) <= now]
    cards.sort(key=lambda c: (float(c.get("due") or 0), c.get("id") or ""))
    return cards[:limit]


def list_cards(settings: Settings, note_ids: set[str] | None = None,
               limit: int = 300) -> list[dict[str, Any]]:
    """按生成时间倒序列出卡片，给"所有卡片"浏览器用（不看到期）。"""
    cards = [c for c in load(settings) if _in_scope(c, note_ids)]
    cards.sort(key=lambda c: (-float(c.get("created") or 0), c.get("id") or ""))
    return cards[:limit]


def reset(settings: Settings, card_id: str) -> dict[str, Any] | None:
    """把一张卡提到"现在到期"，用于卡片浏览器里点「现在复习」。不影响 ease/连击。"""
    cards = load(settings)
    for card in cards:
        if card.get("id") == card_id:
            card["due"] = time.time()
            save(settings, cards)
            return card
    return None


def review(settings: Settings, card_id: str, grade: str) -> dict[str, Any] | None:
    cards = load(settings)
    for card in cards:
        if card.get("id") == card_id:
            schedule(card, grade)
            save(settings, cards)
            return card
    return None


def stats(settings: Settings, now: float | None = None,
          note_ids: set[str] | None = None) -> dict[str, Any]:
    now = time.time() if now is None else now
    cards = [c for c in load(settings) if _in_scope(c, note_ids)]
    due_cards = [c for c in cards if float(c.get("due") or 0) <= now]
    learned = [c for c in cards if int(c.get("reps") or 0) > 0]
    return {
        "total": len(cards),
        "due": len(due_cards),
        "learned": len(learned),
        "notes": len({c.get("note_id") for c in cards}),
    }


def per_note(settings: Settings) -> dict[str, dict[str, int]]:
    """每篇笔记的卡片数 / 待复习数，给「卡片来源」列表用。"""
    now = time.time()
    out: dict[str, dict[str, int]] = {}
    for card in load(settings):
        key = str(card.get("note_id") or "")
        row = out.setdefault(key, {"total": 0, "due": 0})
        row["total"] += 1
        if float(card.get("due") or 0) <= now:
            row["due"] += 1
    return out


def remove(settings: Settings, card_id: str) -> bool:
    cards = load(settings)
    kept = [c for c in cards if c.get("id") != card_id]
    if len(kept) == len(cards):
        return False
    save(settings, kept)
    return True


def remove_note(settings: Settings, note_id: str) -> int:
    cards = load(settings)
    kept = [c for c in cards if c.get("note_id") != note_id]
    removed = len(cards) - len(kept)
    if removed:
        save(settings, kept)
    return removed
