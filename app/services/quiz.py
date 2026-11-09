"""自测卷：让模型按笔记出题，用户在手机上做，做错的题直接变成复习卡片。

为什么要单独一层：
  笔记是"看懂"，卡片是"记住"，自测是"检查自己到底会不会"——testing effect 是
  学习科学里最稳的结论之一。出题和判分都在本地完成，不依赖任何在线服务。

题型（`KINDS`）：
  single 单选 / multi 多选 / judge 判断 / blank 填空 / short 简答
  手机上纯选择题最省事，所以默认 `single + judge`；填空/简答要打字，按需开。

存储：`data_dir/quizzes.json`，同样"临时文件 + 原子替换"。
判分：选择、判断、填空全自动；简答交给用户自评（模型判简答不可靠）。
错题：一键转成卡片，挂到 `quiz:<id>` 这个虚拟笔记下，复习页单独归一组。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Iterable

from ..core.config import Settings
from ..core.llm import LLMClient

log = logging.getLogger(__name__)

QUIZ_FILE = "quizzes.json"
KINDS = ("single", "multi", "judge", "blank", "short")
KIND_LABEL = {"single": "单选", "multi": "多选", "judge": "判断", "blank": "填空", "short": "简答"}
DEFAULT_KINDS = ("single", "judge")
MAX_QUESTIONS = 30
MAX_OPTIONS = 6
QUIZ_NOTE_PREFIX = "quiz:"

_SYSTEM = """你是出题老师，正在根据一份学习笔记给学生出**自测题**。

【铁律】
1. 只考材料里真实出现的内容，禁止编造；材料没讲的一律不出。
2. 一道题只考一个点，题干必须能独立成立（不出现"如上文""如图"）。
3. 优先考"为什么""怎么用""两者的区别"，不要只考名词解释。
4. 公式用 LaTeX：行内 $...$，独立 $$...$$。数字和符号照抄材料。

【各题型】
- single 单选：4 个选项，**只有一个**正确；干扰项要像样但确定错，不要出现"以上都对"。
- multi 多选：4~5 个选项，**2~3 个**正确，题干明确问"哪些"。
- judge 判断：一句话陈述，正误都要有；不要写"以上说法都正确"这类套话。
- blank 填空：一句话里挖 **一个** 空，用 ____ 表示；答案 1~8 个字、唯一确定。
- short 简答：问"为什么/怎么做"，参考答案给 2~3 条要点。

【每题都要给】
- concept：这道题考的知识点名称，4~12 字，**优先用材料「关键概念」表里的写法**；
  同一知识点要用完全相同的写法（错题卡会按它归组）。
- explain：一两句话说明为什么（答错后看），不超过 80 字。

只输出 JSON：
{"questions":[
 {"kind":"single","stem":"题干","options":["A 的内容","B 的内容","C 的内容","D 的内容"],"answer":[0],"concept":"","explain":""},
 {"kind":"judge","stem":"陈述句","answer":true,"concept":"","explain":""},
 {"kind":"blank","stem":"……____……","answer":["标准答案"],"concept":"","explain":""},
 {"kind":"short","stem":"题干","answer":"参考答案要点","concept":"","explain":""}
]}
answer 一律写成数组（判断题写 true/false）。"""


# ---------------------------------------------------------------- 存储
def _path(settings: Settings) -> Path:
    return Path(settings.data_dir) / QUIZ_FILE


def load(settings: Settings) -> list[dict[str, Any]]:
    path = _path(settings)
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        log.warning("自测卷文件损坏，按空处理：%s", path)
        return []
    quizzes = data.get("quizzes") if isinstance(data, dict) else data
    return [q for q in (quizzes or []) if isinstance(q, dict) and q.get("id")]


def save(settings: Settings, quizzes: list[dict[str, Any]]) -> None:
    path = _path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": 1, "updated": time.time(), "quizzes": quizzes}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def get(settings: Settings, quiz_id: str) -> dict[str, Any] | None:
    for quiz in load(settings):
        if quiz.get("id") == quiz_id:
            return quiz
    return None


def remove(settings: Settings, quiz_id: str) -> bool:
    quizzes = load(settings)
    kept = [q for q in quizzes if q.get("id") != quiz_id]
    if len(kept) == len(quizzes):
        return False
    save(settings, kept)
    return True


def title_map(settings: Settings) -> dict[str, str]:
    """`quiz:<id>` → 卷名。复习页据此给错题卡分组。"""
    return {f"{QUIZ_NOTE_PREFIX}{q['id']}": q.get("title") or "自测卷" for q in load(settings)}


# ---------------------------------------------------------------- 解析与校验
def _clean(text: Any, limit: int = 400) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()[:limit]


def parse_questions(data: Any, kinds: Iterable[str]) -> list[dict[str, Any]]:
    """把模型给的东西收拾成能判分的题；脏数据直接丢，不抛异常。"""
    allowed = [k for k in kinds if k in KINDS]
    raw = data.get("questions") if isinstance(data, dict) else data
    if not isinstance(raw, list):
        return []

    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or "").strip().lower()
        if kind not in allowed:
            continue
        stem = _clean(item.get("stem"), 500)
        if len(stem) < 6:
            continue
        key = re.sub(r"\s+", "", stem).lower()
        if key in seen:
            continue

        question: dict[str, Any] = {
            "kind": kind,
            "stem": stem,
            "concept": _clean(item.get("concept"), 24),
            "explain": _clean(item.get("explain"), 160),
        }
        if kind in ("single", "multi"):
            options = [_clean(o, 120) for o in (item.get("options") or []) if _clean(o, 120)]
            options = list(dict.fromkeys(options))[:MAX_OPTIONS]
            if len(options) < 3:
                continue
            answer = item.get("answer")
            answer = answer if isinstance(answer, list) else [answer]
            idx = [int(a) for a in answer if isinstance(a, (int, float))
                   or (isinstance(a, str) and a.strip().isdigit())]
            idx = [i for i in dict.fromkeys(idx) if 0 <= i < len(options)]
            if kind == "single" and len(idx) != 1:
                continue
            if kind == "multi" and len(idx) < 2:
                continue
            question["options"] = options
            question["answer"] = idx
        elif kind == "judge":
            value = item.get("answer")
            if isinstance(value, list) and value:
                value = value[0]
            if isinstance(value, str):
                value = value.strip().lower() in ("true", "对", "正确", "yes", "1")
            if not isinstance(value, bool):
                continue
            question["answer"] = value
        elif kind == "blank":
            answer = item.get("answer")
            answer = answer if isinstance(answer, list) else [answer]
            answers = [_clean(a, 40) for a in answer if _clean(a, 40)]
            if not answers:
                continue
            question["answer"] = answers[:4]
        else:                                    # short：参考答案，自评
            answer = _clean(item.get("answer"), 600)
            if not answer:
                continue
            question["answer"] = answer

        seen.add(key)
        question["id"] = f"q{len(out) + 1}"
        out.append(question)
    return out


# ---------------------------------------------------------------- 生成
def source_md(note: dict[str, Any]) -> str:
    """笔记正文裁到出题够用的长度：完整转录太长，出题反而被口水话带跑。"""
    md = str(note.get("markdown") or "")
    detail = re.search(r"<details>[\s\S]*?</details>", md)
    if detail and len(md) > 9000:
        md = md[: detail.start()] + "\n（完整转录已省略）\n" + md[detail.end():]
    return md[:12000]


def build(client: LLMClient, material: str, title: str, kinds: Iterable[str],
          count: int) -> list[dict[str, Any]]:
    """调模型出题（不落盘）。素材太短就返回空列表，别硬出。"""
    body = (material or "").strip()
    if len(body) < 300:
        return []
    kind_list = [k for k in kinds if k in KINDS] or list(DEFAULT_KINDS)
    labels = "、".join(f"{k}（{KIND_LABEL[k]}）" for k in kind_list)
    prompt = (
        f"这份笔记叫《{title}》。\n\n【材料】\n{body}\n\n"
        f"请出 {count} 道题，题型只能用：{labels}。\n"
        f"题量分配尽量均匀；如果材料不足以出满，宁可少出，不要凑数。"
    )
    data = client.complete_json(_SYSTEM, prompt, max_tokens=4096)
    return parse_questions(data, kind_list)


def create(settings: Settings, notes: list[dict[str, Any]], kinds: Iterable[str],
           count: int, title: str = "") -> dict[str, Any]:
    """把题目组装成一份卷子并落盘。"""
    questions = [q for n in notes for q in (n.get("questions") or [])]
    if not questions:
        return {}
    for i, q in enumerate(questions[:MAX_QUESTIONS], 1):
        q["id"] = f"q{i}"
    questions = questions[:MAX_QUESTIONS]
    stamp = int(time.time())
    quiz = {
        "id": hashlib.sha1(f"{title}|{stamp}|{len(questions)}".encode()).hexdigest()[:12],
        "title": title or "自测卷",
        "created": stamp,
        "kinds": list(dict.fromkeys(q["kind"] for q in questions)),
        "sources": [{"id": n.get("id") or "", "title": n.get("title") or ""} for n in notes],
        "note_id": f"{QUIZ_NOTE_PREFIX}{hashlib.sha1(str(stamp).encode()).hexdigest()[:8]}",
        "questions": questions,
        "attempts": [],
    }
    quiz["note_id"] = f"{QUIZ_NOTE_PREFIX}{quiz['id']}"
    quizzes = load(settings)
    quizzes.insert(0, quiz)
    save(settings, quizzes[:60])          # 只留最近 60 份，别把文件撑大
    return quiz


# ---------------------------------------------------------------- 判分
_PUNCT = re.compile(r"[\s，。、；：？！“”‘’（）()《》〈〉,.!?;:'\"/\\|·—－-]+")


def _norm(text: str) -> str:
    return _PUNCT.sub("", str(text or "")).casefold()


def _equal_text(given: str, expect: str) -> bool:
    a, b = _norm(given), _norm(expect)
    if not a:
        return False
    if a == b:
        return True
    # 填空题常有"增广矩阵" vs "增广矩阵（[A|E]）"这类，允许包含关系，但要求够长
    return len(b) >= 2 and (b in a or a in b) and abs(len(a) - len(b)) <= 4


def grade(quiz: dict[str, Any], answers: dict[str, Any]) -> dict[str, Any]:
    """判分。answers: {question_id: 值}。

    选择/多选传下标数组，判断传 bool，填空传字符串，简答传 true/false（自评）。
    """
    items: list[dict[str, Any]] = []
    score = 0
    for q in quiz.get("questions") or []:
        qid = q["id"]
        given = answers.get(qid)
        correct = False
        if q["kind"] in ("single", "multi"):
            chosen = given if isinstance(given, list) else ([] if given is None else [given])
            chosen = [int(x) for x in chosen if str(x).lstrip("-").isdigit()]
            correct = sorted(set(chosen)) == sorted(set(q.get("answer") or []))
        elif q["kind"] == "judge":
            correct = isinstance(given, bool) and given == q.get("answer")
        elif q["kind"] == "blank":
            correct = isinstance(given, str) and any(
                _equal_text(given, expect) for expect in (q.get("answer") or [])
            )
        else:                                   # short：用户自评
            correct = given is True
        score += int(correct)
        items.append({
            "id": qid, "kind": q["kind"], "correct": correct,
            "expect": q.get("answer"), "concept": q.get("concept") or "",
        })
    total = len(quiz.get("questions") or [])
    return {"score": score, "total": total, "items": items}


def record_attempt(settings: Settings, quiz_id: str, result: dict[str, Any]) -> None:
    quizzes = load(settings)
    for quiz in quizzes:
        if quiz["id"] == quiz_id:
            quiz.setdefault("attempts", []).append({
                "at": time.time(),
                "score": result["score"],
                "total": result["total"],
                "wrong": [i["id"] for i in result["items"] if not i["correct"]],
            })
            quiz["attempts"] = quiz["attempts"][-20:]
            save(settings, quizzes)
            return


# ---------------------------------------------------------------- 错题转卡片
def _answer_text(q: dict[str, Any]) -> str:
    kind = q["kind"]
    if kind in ("single", "multi"):
        opts = q.get("options") or []
        picked = [opts[i] for i in (q.get("answer") or []) if 0 <= i < len(opts)]
        return "、".join(picked) or "（未给出）"
    if kind == "judge":
        return "对" if q.get("answer") else "错"
    if kind == "blank":
        return "／".join(q.get("answer") or [])
    return str(q.get("answer") or "")


def to_cards(settings: Settings, quiz: dict[str, Any],
             question_ids: Iterable[str], known_concepts: Iterable[str] | None = None) -> dict[str, int]:
    """把错题变成卡片，交给复习队列。返回 {added, dup}。"""
    from . import cards as card_svc  # noqa: PLC0415  避免循环导入
    from . import concept_review  # noqa: PLC0415

    wanted = set(question_ids or [])
    picked = [q for q in (quiz.get("questions") or []) if q["id"] in wanted]
    if not picked:
        return {"added": 0, "dup": 0}

    kind_map = {"single": "概念", "multi": "概念", "judge": "易错", "blank": "概念", "short": "概念"}
    items = []
    for q in picked:
        answer = _answer_text(q)
        explain = q.get("explain") or ""
        back = f"{answer}。{explain}".strip("。 ") if explain else answer
        concept = q.get("concept") or ""
        items.append({
            "front": f"【{KIND_LABEL[q['kind']]}】{q['stem']}",
            "back": back,
            "concept": concept or kind_map.get(q["kind"], "概念"),
            "kind": kind_map.get(q["kind"], "概念"),
        })
    # 概念名过闸：别让"XX 的优势"这类派生说法把索引撑碎
    concept_review.review_card_concepts(items, known_concepts)

    for item in items:
        item["front"] = item["front"][:300]
        item["back"] = item["back"][:800]
    added, dup = card_svc.add(settings, quiz["note_id"], quiz.get("title") or "自测卷", items)
    return {"added": added, "dup": dup}


# ---------------------------------------------------------------- 对外视图
def summary(quiz: dict[str, Any], with_questions: bool = False) -> dict[str, Any]:
    """列表/答题页共用：**不下发答案**（除简答的参考答案要在答完后看）。"""
    row = {
        "id": quiz["id"],
        "title": quiz.get("title") or "",
        "created": quiz.get("created") or 0,
        "kinds": quiz.get("kinds") or [],
        "count": len(quiz.get("questions") or []),
        "sources": quiz.get("sources") or [],
        "attempts": len(quiz.get("attempts") or []),
        "last": (quiz.get("attempts") or [{}])[-1] if quiz.get("attempts") else {},
    }
    if with_questions:
        row["questions"] = [
            {
                "id": q["id"], "kind": q["kind"], "stem": q["stem"],
                "options": q.get("options") or [], "concept": q.get("concept") or "",
                # 简答是自评制：答完要点开参考答案自己对照，不下发就无从自评。
                # 客观题仍然绝不下发答案——那边判分靠后端，抄了没意义。
                **({"ref": q.get("answer") or ""} if q["kind"] == "short" else {}),
            }
            for q in (quiz.get("questions") or [])
        ]
    return row


def detail_for_review(quiz: dict[str, Any], answers: dict[str, Any]) -> dict[str, Any]:
    """交卷后：逐题给正确答案与解析，供结果页使用。"""
    return {
        "id": quiz["id"],
        "title": quiz.get("title") or "",
        "questions": [
            {
                "id": q["id"], "kind": q["kind"], "stem": q["stem"],
                "options": q.get("options") or [],
                "answer": q.get("answer"),
                "answer_text": _answer_text(q),
                "explain": q.get("explain") or "",
                "given": answers.get(q["id"]),
            }
            for q in (quiz.get("questions") or [])
        ],
    }
