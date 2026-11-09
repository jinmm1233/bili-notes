"""知识卡片回归：解析、去重、SM-2 排期、到期筛选。

全部跑在临时目录里，不碰用户真实卡片，也不需要联网或模型。
  python tests/smoke_cards.py
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

FAILED = 0


def ok(cond: bool, msg: str) -> None:
    global FAILED
    print(("[OK] " if cond else "[FAIL] ") + msg)
    if not cond:
        FAILED += 1


def test_parse() -> None:
    raw = {"cards": [
        {"front": "什么是初等行变换？", "back": "三种：对换、倍乘、倍加。", "concept": "初等行变换", "kind": "概念"},
        {"front": "什么是初等行变换？", "back": "重复的一道", "concept": "初等行变换", "kind": "概念"},
        {"front": "太短", "back": "x", "concept": "", "kind": "概念"},
        {"front": "为什么倍乘要求 $k\\neq 0$？", "back": "否则该行信息被抹掉。", "concept": "初等行变换", "kind": "易错"},
        "垃圾数据",
    ]}
    items = C.parse_cards(raw)
    ok(len(items) == 2, f"脏数据被过滤、重复被去掉（剩 {len(items)} 张）")
    ok(items[0]["kind"] == "概念", "kind 保留")
    ok(all(len(i["front"]) > 4 for i in items), "过短的卡片被丢弃")

    ok(C.parse_cards({"cards": []}) == [], "空列表安全")
    ok(C.parse_cards({"nope": 1}) == [], "字段缺失安全")
    ok(C.parse_cards([{"front": "合法的问题内容", "back": "答案"}])[0]["kind"] == "概念",
       "裸数组也能解析，且缺省 kind 补成概念")


def test_schedule() -> None:
    now = 1_700_000_000.0
    card = {"ease": 2.5, "interval": 0.0, "reps": 0, "lapses": 0}

    C.schedule(card, "good", now=now)
    ok(abs(card["interval"] - 1) < 1e-6, "新卡点「记得」→ 1 天后")
    ok(abs(card["due"] - (now + 86400)) < 1, "到期时间 = 现在 + 1 天")

    C.schedule(card, "good", now=now)
    ok(abs(card["interval"] - 3) < 1e-6, "第二次「记得」→ 3 天")

    C.schedule(card, "good", now=now)
    ok(abs(card["interval"] - round(3 * card["ease"])) < 1e-6, "第三次起按 ease 增长")

    before_ease = card["ease"]
    C.schedule(card, "again", now=now)
    ok(card["interval"] < 0.02, "「忘记」→ 10 分钟后重来")
    ok(card["ease"] < before_ease and card["lapses"] == 1, "「忘记」降低 ease 并记一次 lapse")
    ok(card["reps"] == 0, "「忘记」把连击清零")

    fresh = {"ease": 2.5, "interval": 0.0, "reps": 0, "lapses": 0}
    C.schedule(fresh, "easy", now=now)
    ok(abs(fresh["interval"] - 3) < 1e-6, "新卡点「太简单」→ 3 天后")
    ok(fresh["ease"] > 2.5, "「太简单」提高 ease")

    capped = {"ease": 2.8, "interval": 170.0, "reps": 9, "lapses": 0}
    C.schedule(capped, "easy", now=now)
    ok(capped["interval"] <= C.INTERVAL_CAP, f"间隔封顶在 {C.INTERVAL_CAP} 天")

    floor = {"ease": 1.35, "interval": 10.0, "reps": 3, "lapses": 0}
    C.schedule(floor, "again", now=now)
    ok(floor["ease"] >= 1.3, "ease 不会低于 1.3")


def test_preview() -> None:
    """按钮上显示的"多久后再来"要和真实排期一致，而且不能改到原卡。"""
    card = {"ease": 2.5, "interval": 0.0, "reps": 0, "lapses": 0}
    before = dict(card)
    p = C.preview(card)
    ok(set(p) == set(C.GRADES), f"四档都有预览（{sorted(p)}）")
    ok(p["again"].endswith("分钟后"), f"忘记显示分钟（{p['again']}）")
    ok(p["easy"] == "3 天后", f"新卡「太简单」显示 3 天（{p['easy']}）")
    ok(card == before, "预览是纯查询，不修改卡片本身")

    # 与真实排期对得上：点"记得"后到期时间 ≈ 预览里的天数
    now = 1_700_000_000.0
    probe = dict(card)
    C.schedule(probe, "good", now=now)
    days = round((probe["due"] - now) / 86400)
    ok(f"{days} 天后" == p["good"], f"预览与实际排期一致（{p['good']} / 实际 {days} 天）")

    mature = {"ease": 2.5, "interval": 60.0, "reps": 4, "lapses": 0}
    pm = C.preview(mature)
    ok("个月后" in pm["good"] or "半年后" in pm["good"], f"长间隔说人话（{pm['good']}）")

    ok(C._human_gap(10 / 1440) == "10 分钟后", "10 分钟的表达")
    ok(C._human_gap(0.5) == "12 小时后", f"半天说成小时（{C._human_gap(0.5)}）")
    ok(C._human_gap(180) == "半年后", "封顶说成半年")


def test_similar() -> None:
    """近似去重：同一题换说法要挡住，相邻步骤不能被误杀。"""
    a = C._cmp_text("用初等行变换法求矩阵 A 的逆矩阵时，要构造什么矩阵，把它化成什么形式？")
    b = C._cmp_text("用初等行变换法求矩阵 A 的逆矩阵时，应构造什么矩阵，把它化成什么形式？")
    ok(C._similar(a, b), "换个虚词的同一道题被判为重复")

    same_anchor = C._cmp_text("初等行变换求逆时，为什么只能用上一行去改变下一行？")
    same_anchor2 = C._cmp_text("初等行变换求逆时，为什么只能拿上一行去改变下一行？")
    ok(C._similar(same_anchor, same_anchor2), "措辞不同但定位词相同，仍算重复")

    m = "$A=\\begin{bmatrix}1&2\\\\3&4\\end{bmatrix}$"
    down = C._cmp_text(f"对矩阵 {m}，把第二列下方元素化为 0。")
    up = C._cmp_text(f"对矩阵 {m}，把第二列上方元素化为 0。")
    ok(not C._similar(down, up), "定位词不同（下方/上方）不算重复")

    left = C._cmp_text(f"对矩阵 {m}，把第一列下方元素化为 0。")
    right = C._cmp_text(f"对矩阵 {m}，把第二列下方元素化为 0。")
    ok(not C._similar(left, right), "列号不同不算重复")


def test_scope_and_browse(tmp: Path) -> None:
    """按笔记筛选、浏览列表、把卡片提回待复习。"""
    settings = Settings(data_dir=tmp)

    def make(note: str, n: int, tag: str) -> list[dict[str, str]]:
        return [{
            "front": f"{tag}第{i}个问题：这里为什么这样？",
            "back": "答案",
            "concept": "甲",
            "kind": "概念",
        } for i in range(n)]

    C.add(settings, "A", "笔记 A", make("A", 3, "甲类"))
    C.add(settings, "B", "笔记 B", make("B", 2, "乙类"))

    ok(C.stats(settings, note_ids={"A"})["total"] == 3, "按笔记统计只算该篇")
    ok(len(C.due(settings, 50, note_ids={"B"})) == 2, "到期队列按笔记筛选")
    ok(C.stats(settings, note_ids=set())["total"] == 0, "空范围返回 0 而不是全部")
    ok(len(C.due(settings, 50, note_ids={"A"}, include_future=True)) == 3,
       "include_future 时忽略到期时间")

    listed = C.list_cards(settings, note_ids={"A"})
    ok(len(listed) == 3 and all(c["note_id"] == "A" for c in listed),
       "浏览列表按笔记筛选")
    ok(len(C.list_cards(settings)) == 5, "不传范围时列出全部")

    card = listed[0]
    C.review(settings, card["id"], "good")
    ok(C.stats(settings, note_ids={"A"})["due"] == 2, "复习一张后该篇只剩 2 张到期")
    C.reset(settings, card["id"])
    ok(C.stats(settings, note_ids={"A"})["due"] == 3, "reset 把卡片提回待复习")
    ok(C.reset(settings, "不存在") is None, "reset 不存在的卡片返回 None")

    # 同一篇里换个说法的题不该再进队列
    near = [
        {"front": "初等行变换求逆时，为什么只能用上一行去改变下一行？", "back": "a", "concept": "甲", "kind": "概念"},
        {"front": "初等行变换求逆时，为什么只能拿上一行去改变下一行？", "back": "b", "concept": "甲", "kind": "概念"},
    ]
    added, dup = C.add(settings, "C", "笔记 C", near)
    ok((added, dup) == (1, 1), f"近似重复被挡下（新增 {added}，重复 {dup}）")


def test_store(tmp: Path) -> None:
    settings = Settings(data_dir=tmp)
    ok(C.load(settings) == [], "初始没有卡片")

    items = [
        {"front": "问题一：为什么这样？", "back": "答案一", "concept": "甲", "kind": "概念"},
        {"front": "问题二：怎么用？", "back": "答案二", "concept": "乙", "kind": "步骤"},
    ]
    added, dup = C.add(settings, "笔记A", "笔记 A 标题", items)
    ok((added, dup) == (2, 0), f"首次写入 2 张（实际 {added}/{dup}）")

    added2, dup2 = C.add(settings, "笔记A", "笔记 A 标题", items)
    ok((added2, dup2) == (0, 2), "重复写入被按问题去重")

    # 同一句话放在另一篇笔记里应该算新卡片
    added3, _ = C.add(settings, "笔记B", "笔记 B 标题", items[:1])
    ok(added3 == 1, "不同笔记的同名问题各自独立")

    st = C.stats(settings)
    ok(st["total"] == 3 and st["due"] == 3, f"统计：共 3 张、全部到期（{st}）")

    # 复习一张 → 不再是"到期"
    card = C.load(settings)[0]
    C.review(settings, card["id"], "good")
    st2 = C.stats(settings)
    ok(st2["due"] == 2 and st2["learned"] == 1, f"复习后到期数下降（{st2}）")

    ok(len(C.due(settings, limit=10)) == 2, "到期队列只剩 2 张")

    # 到期顺序：先到期的排前面
    queue = C.due(settings, limit=10)
    ok(all(queue[i]["due"] <= queue[i + 1]["due"] for i in range(len(queue) - 1)),
       "到期队列按时间升序")

    per = C.per_note(settings)
    ok(per["笔记A"]["total"] == 2 and per["笔记B"]["total"] == 1, f"按笔记统计正确（{per}）")

    ok(C.remove(settings, card["id"]) is True, "删单张卡片")
    ok(C.remove(settings, "不存在") is False, "删不存在的卡片返回 False")
    ok(C.remove_note(settings, "笔记A") == 1, "按笔记清空剩下的卡片")
    ok(C.stats(settings)["total"] == 1, "清空后只剩另一篇的 1 张")

    # 损坏文件不应崩
    (tmp / C.CARD_FILE).write_text("{坏掉的 json", encoding="utf-8")
    ok(C.load(settings) == [], "卡片文件损坏时按空处理，不抛异常")


def test_dedupe(tmp: Path) -> None:
    """历史重复清理：只报不删、留复习次数多的、不跨笔记误伤。"""
    settings = Settings(data_dir=tmp)
    C.add(settings, "笔记A", "线代", [
        {"front": "用初等行变换求逆时，要构造什么矩阵？", "back": "增广矩阵。", "concept": "初等行变换法", "kind": "步骤"},
    ])
    # 换个说法问同一件事（旧版"补充卡片"就是这么堆出来的）
    cards = C.load(settings)
    now = cards[0]["created"]
    for i, (front, reps) in enumerate([
        ("用初等行变换求逆时，应构造什么矩阵？", 0),
        ("化零时为什么只能用上一行改变下一行？", 1),
    ]):
        cards.append({
            "id": f"人工{i}", "note_id": "笔记A", "note_title": "线代",
            "front": front, "back": "答案", "concept": "初等行变换法", "kind": "概念",
            "created": now + i + 1, "due": now, "interval": 0, "ease": 2.5,
            "reps": reps, "lapses": 0, "last": 0,
        })
    # 另一篇笔记里的同一句话：不该被算成重复
    cards.append({
        "id": "人工x", "note_id": "笔记B", "note_title": "另一篇",
        "front": "用初等行变换求逆时，要构造什么矩阵？", "back": "答案",
        "concept": "初等行变换法", "kind": "步骤",
        "created": now, "due": now, "interval": 0, "ease": 2.5,
        "reps": 0, "lapses": 0, "last": 0,
    })
    C.save(settings, cards)

    before = len(C.load(settings))
    report = C.dedupe(settings, dry_run=True)
    ok(report["duplicates"] == 1 and len(report["groups"]) == 1,
       f"只查同一篇笔记内的同义题（{report['duplicates']} 张）")
    ok(len(C.load(settings)) == before, "默认只报告，不删东西")

    kept = report["groups"][0]["keep"]["front"]
    ok("要构造" in kept, f"保留先出现的那张（{kept}）")

    done = C.dedupe(settings, dry_run=False)
    ok(done["removed"] == 1, f"确认后才真删（删 {done['removed']} 张）")
    left = C.load(settings)
    ok(len(left) == before - 1, "只少了那一张")
    ok(any(c["note_id"] == "笔记B" for c in left), "别的笔记里同样的题不受影响")
    ok(C.dedupe(settings, dry_run=True)["duplicates"] == 0, "清干净后再查为 0")



def test_example_cards(tmp: Path) -> None:
    """例题卡：答案是某道题的具体系数，换个例子就不成立，不该进复习。"""
    example = {
        "front": "初等行变换法求逆矩阵的最后一步，如何把左侧主对角线元素化为 1？",
        "back": r"对各行分别倍乘：第一行乘 $\frac{1}{3}$，第二行乘 $-1$，第三行乘 $\frac{1}{4}$。",
        "concept": "初等行变换法",
        "kind": "步骤",
    }
    general = {
        "front": "把左侧化成单位矩阵时，各行乘的系数由什么决定？",
        "back": "由该行主对角线元素决定：整行除以它，其它列随之变化，右侧同步得到 $A^{-1}$。",
        "concept": "初等行变换法",
        "kind": "概念",
    }
    kept_general = {
        "front": "第 i 行减去第 j 行的 k 倍，为什么要从左上往右下做？",
        "back": "为了不破坏已化好的列：用上面的行改下面的行，已为 0 的位置保持为 0。",
        "concept": "初等行变换法",
        "kind": "易错",
    }
    dropped: list[str] = []
    items = C.parse_cards({"cards": [example, general, kept_general]}, dropped)
    ok(len(items) == 2, f"例题卡被丢弃，留下 {len(items)} 张")
    ok(len(dropped) == 1 and "最后一步" in dropped[0], "丢弃记录里能说清是哪张")
    ok(C.is_example_card(example["front"], example["back"]), "识别：逐行乘具体系数")
    ok(not C.is_example_card(general["front"], general["back"]), "通用问答不误判")
    ok(not C.is_example_card(kept_general["front"], kept_general["back"]),
       "用变量 i/j/k 的步骤不误判")
    ok(not C.is_example_card("理想气体常数的数值是多少？", "R = 8.314 J/(mol·K)"),
       "公式自带常数不算例题")

    # 存量清理：默认只报告
    settings = Settings(data_dir=tmp)
    now = 1794000000.0
    def card(i, front, back):
        return {"id": f"c{i}", "note_id": "n", "note_title": "线代", "front": front,
                "back": back, "concept": "初等行变换法", "kind": "步骤",
                "created": now, "due": now, "interval": 0, "ease": 2.5,
                "reps": 0, "lapses": 0, "last": 0}
    C.save(settings, [card(1, example["front"], example["back"]),
                      card(2, general["front"], general["back"])])
    report = C.clean_examples(settings, dry_run=True)
    ok(report["count"] == 1 and report["removed"] == 0, "默认只报告")
    ok(len(C.load(settings)) == 2, "报告阶段不删东西")
    done = C.clean_examples(settings, dry_run=False)
    left = C.load(settings)
    ok(done["removed"] == 1 and len(left) == 1, "确认后只删例题卡")
    ok("单位矩阵" in (left[0]["front"] if left else ""), "通用卡保留")

def main() -> int:
    print("=== 解析与去重 ===")
    test_parse()
    print("\n=== SM-2 排期 ===")
    test_schedule()
    print("\n=== 间隔预览 ===")
    test_preview()
    print("\n=== 近似去重 ===")
    test_similar()
    print("\n=== 范围筛选与浏览 ===")
    with tempfile.TemporaryDirectory() as raw:
        test_scope_and_browse(Path(raw))
    print("\n=== 存储与统计 ===")
    with tempfile.TemporaryDirectory() as raw:
        test_store(Path(raw))
    print("\n=== 历史重复清理 ===")
    with tempfile.TemporaryDirectory() as raw:
        test_dedupe(Path(raw))
    print("\n=== 例题卡 ===")
    with tempfile.TemporaryDirectory() as raw:
        test_example_cards(Path(raw))
    print()
    if FAILED:
        print(f"{FAILED} 项未通过")
        return 1
    print("知识卡片全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
