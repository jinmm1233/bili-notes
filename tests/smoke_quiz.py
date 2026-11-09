"""自测卷回归：出题校验、判分、错题转卡片、存储。

用假模型客户端，不联网；笔记用字典造，不碰真实库。
  python tests/smoke_quiz.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "vendor"))

from app.core.config import Settings  # noqa: E402
from app.services import cards as C  # noqa: E402
from app.services import quiz as Q  # noqa: E402

FAILED = 0


def ok(cond: bool, msg: str) -> None:
    global FAILED
    print(("[OK] " if cond else "[FAIL] ") + msg)
    if not cond:
        FAILED += 1


GOOD = {
    "questions": [
        {"kind": "single", "stem": "用初等行变换求逆时，要把 $[A\\mid E]$ 化成什么？",
         "options": ["$[E\\mid A^{-1}]$", "$[A^{-1}\\mid E]$", "$[E\\mid A]$", "$[A\\mid E]$"],
         "answer": [0], "concept": "初等行变换法", "explain": "左边化成单位阵，右边就是逆矩阵"},
        {"kind": "judge", "stem": "初等行变换只能用上一行去改变下一行。",
         "answer": True, "concept": "初等行变换法", "explain": "这是保证不把已化好的列再破坏"},
        {"kind": "multi", "stem": "初等行变换包括哪些？",
         "options": ["对换两行", "某行乘非零常数", "某行加另一行的倍数", "转置"],
         "answer": [0, 1, 2], "concept": "初等行变换法", "explain": "转置不是行变换"},
        {"kind": "blank", "stem": "把 $[A\\mid E]$ 的左半化成单位阵，右半就是 ____。",
         "answer": ["逆矩阵", "$A^{-1}$"], "concept": "初等行变换法", "explain": "定义如此"},
        # 下面三条都该被丢掉
        {"kind": "single", "stem": "答案下标越界", "options": ["甲", "乙", "丙"], "answer": [9]},
        {"kind": "single", "stem": "选两个的单选题", "options": ["甲", "乙", "丙"], "answer": [0, 1]},
        {"kind": "blank", "stem": "没有答案的填空", "answer": []},
        {"kind": "short", "stem": "简答题不在本次题型里", "answer": "不该出现"},
    ]
}


class FakeClient:
    model = "fake"

    def __init__(self, payload):
        self.payload = payload
        self.prompts = []

    def complete_json(self, system, prompt, max_tokens=0):
        self.prompts.append(prompt)
        return self.payload


def test_parse() -> None:
    qs = Q.parse_questions(GOOD, ("single", "judge", "multi", "blank"))
    ok(len(qs) == 4, f"只留下合法的 4 题（实际 {len(qs)}）")
    ok([q["id"] for q in qs] == ["q1", "q2", "q3", "q4"], "题号自动排好")
    ok(qs[0]["options"][0].startswith("$[E"), "选项里的公式原样保留")
    ok(qs[1]["answer"] is True, "判断题答案是真布尔值")
    ok(qs[2]["kind"] == "multi" and qs[2]["answer"] == [0, 1, 2], "多选题保留多个正确项")
    ok(qs[3]["answer"] == ["逆矩阵", "$A^{-1}$"], "填空题保留多个可接受答案")
    ok(all(q["concept"] == "初等行变换法" for q in qs), "concept 原样带出（错题卡要用）")

    # 题型过滤：只要判断题时，其他题型全部丢掉
    only_judge = Q.parse_questions(GOOD, ("judge",))
    ok(len(only_judge) == 1 and only_judge[0]["kind"] == "judge", "按题型过滤")


def test_grade() -> None:
    qs = Q.parse_questions(GOOD, ("single", "judge", "multi", "blank"))
    quiz = {"questions": qs}
    answers = {"q1": [0], "q2": True, "q3": [0, 1, 2], "q4": "逆矩阵"}
    r = Q.grade(quiz, answers)
    ok(r["score"] == 4 and r["total"] == 4, f"全对 4/4（{r['score']}/{r['total']}）")

    r = Q.grade(quiz, {"q1": [1], "q2": False, "q3": [0, 1], "q4": "增广矩阵"})
    ok(r["score"] == 0, f"全错 0/4（{r['score']}）")

    # 多选题少选不能给分
    r = Q.grade(quiz, {"q3": [0, 1]})
    ok(next(i for i in r["items"] if i["id"] == "q3")["correct"] is False, "多选少选算错")

    # 填空：允许标点/空格/全半角差异，也允许带上限定语
    r = Q.grade(quiz, {"q4": " 逆 矩 阵。 "})
    ok(next(i for i in r["items"] if i["id"] == "q4")["correct"] is True, "填空忽略空格与标点")
    r = Q.grade(quiz, {"q4": "$A^{-1}$"})
    ok(next(i for i in r["items"] if i["id"] == "q4")["correct"] is True, "填空命中多个可接受答案")
    r = Q.grade(quiz, {"q4": "单位矩阵"})
    ok(next(i for i in r["items"] if i["id"] == "q4")["correct"] is False, "填空答错就是错")

    # 没答的题不算对，也不崩
    r = Q.grade(quiz, {})
    ok(r["score"] == 0 and r["total"] == 4, "空白卷 0 分，不抛异常")


SHORT = {
    "questions": [
        {"kind": "short", "stem": "为什么初等行变换求逆时要把 $[A\\mid E]$ 化成 $[E\\mid A^{-1}]$？",
         "answer": "行变换相当于左乘可逆矩阵，把 A 变成 E 的那串变换左乘到 E 上，恰好给出 A 的逆。",
         "concept": "初等行变换法", "explain": "本质是可逆矩阵的分解"},
    ]
}


def test_short() -> None:
    """简答自评：参考答案随答题页下发（自评要对照），判分只认真布尔。"""
    qs = Q.parse_questions(SHORT, ("short",))
    ok(len(qs) == 1 and qs[0]["kind"] == "short", "简答题能通过校验")

    quiz = {"id": "t1", "title": "t", "kinds": ["short"], "sources": [],
            "attempts": [], "questions": qs}
    view = Q.summary(quiz, with_questions=True)
    qv = view["questions"][0]
    ok(bool(qv.get("ref")), "简答题下发参考答案（自评要对照）")
    ok("answer" not in qv, "客观题字段照旧不下发")

    r = Q.grade(quiz, {qs[0]["id"]: True})
    ok(r["score"] == 1, "自评「答上了」给分")
    r = Q.grade(quiz, {qs[0]["id"]: False})
    ok(r["score"] == 0, "自评「没答上」不给分")
    r = Q.grade(quiz, {qs[0]["id"]: "true"})
    ok(r["score"] == 0, "字符串 \"true\" 不算自评（判分只认布尔）")
    ok(Q.grade(quiz, {})["score"] == 0, "没自评不给分")


def test_lifecycle(tmp: Path) -> None:
    settings = Settings(data_dir=tmp, output_dir=tmp / "out")
    note = {"id": "第01集", "title": "线代 22 初等行变换", "markdown": "x" * 400}

    client = FakeClient(GOOD)
    qs = Q.build(client, note["markdown"], note["title"], ("single", "judge"), 10)
    ok(len(qs) == 2, f"按题型出到 2 题（{len(qs)}）")
    ok("单选" in client.prompts[0] and "判断" in client.prompts[0], "提示词里写明了题型")
    ok(Q.build(client, "太短", "t", ("single",), 5) == [], "材料太短不出题")

    quiz = Q.create(settings, [{"id": "第01集", "title": note["title"], "questions": qs}],
                    ("single", "judge"), 10, title="线代 22 自测")
    ok(bool(quiz.get("id")), "落盘后拿到卷子 id")
    ok(quiz["note_id"].startswith(Q.QUIZ_NOTE_PREFIX), "错题卡挂在 quiz: 虚拟笔记下")
    ok(len(Q.load(settings)) == 1, "文件里有一份卷子")

    view = Q.summary(quiz, with_questions=True)
    ok("answer" not in view["questions"][0], "答题页不下发答案")

    result = Q.grade(quiz, {quiz["questions"][0]["id"]: [0]})
    Q.record_attempt(settings, quiz["id"], result)
    ok(Q.load(settings)[0]["attempts"][-1]["score"] == 1, "交卷记录写进卷子")

    rv = Q.detail_for_review(quiz, {quiz["questions"][0]["id"]: [0]})
    ok(rv["questions"][0]["answer_text"], "交卷后给出正确答案文本（结果页要显示）")

    # 错题转卡片
    wrong_ids = [q["id"] for q in quiz["questions"]]
    counts = Q.to_cards(settings, quiz, wrong_ids, known_concepts=["初等行变换法"])
    ok(counts["added"] == 2, f"两道题变两张卡（{counts}）")
    again = Q.to_cards(settings, quiz, wrong_ids, known_concepts=["初等行变换法"])
    ok(again["added"] == 0 and again["dup"] == 2, f"重复点不会重复加卡（{again}）")
    cards = C.load(settings)
    ok(all(c["note_id"] == quiz["note_id"] for c in cards), "卡片挂在卷子的虚拟笔记下")
    ok(cards[0]["front"].startswith("【"), "题干带上题型前缀，复习时一眼看出是什么题")

    ok(Q.remove(settings, quiz["id"]) is True, "删除卷子")
    ok(Q.get(settings, quiz["id"]) is None, "删完就查不到")
    ok(Q.title_map(settings) == {}, "卷子删了，错题卡的分组名也跟着清空")


def main() -> int:
    print("=== 解析与校验 ===")
    test_parse()
    print("\n=== 判分 ===")
    test_grade()
    print("\n=== 简答自评 ===")
    test_short()
    print("\n=== 出题 / 交卷 / 转卡片 ===")
    with tempfile.TemporaryDirectory() as raw:
        test_lifecycle(Path(raw))
    print()
    if FAILED:
        print(f"{FAILED} 项未通过")
        return 1
    print("自测卷全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
