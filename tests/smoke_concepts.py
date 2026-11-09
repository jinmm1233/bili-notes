"""概念索引回归：表格解析、术语归并、跨笔记聚合、卡片挂载。

用临时目录造几篇笔记，不碰真实数据、不联网。
  python tests/smoke_concepts.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "vendor"))

from app.core.config import Settings  # noqa: E402
from app.services import cards as C  # noqa: E402
from app.services import concept_review as R  # noqa: E402
from app.services import concept_tags as T  # noqa: E402
from app.services import concepts  # noqa: E402

FAILED = 0


def ok(cond: bool, msg: str) -> None:
    global FAILED
    print(("[OK] " if cond else "[FAIL] ") + msg)
    if not cond:
        FAILED += 1


NOTE_TMPL = """---
title: "{title}"
uploader: "测试UP"
collection: "线代"
---

# {title}

## 一句话总结

测试用笔记。

## 关键概念

| 术语 | 解释 |
|---|---|
{table}

## 时间轴

00:10 开场
"""


def make_note(root: Path, folder: str, title: str, rows: list[tuple[str, str]]) -> None:
    path = root / folder
    path.mkdir(parents=True, exist_ok=True)
    table = "\n".join(f"| {a} | {b} |" for a, b in rows)
    (path / "notes.md").write_text(
        NOTE_TMPL.format(title=title, table=table), encoding="utf-8", newline="\n"
    )


def test_parse() -> None:
    md = """## 关键概念

| 术语 | 解释 |
|---|---|
| **初等行变换** | 三种基本操作 |
| 单位矩阵： | 记为 E |
| 单位矩阵 | 重复行应被本笔记内去重 |
|  | 空术语应丢弃 |

## 下一节
"""
    rows = concepts.parse_concepts(md)
    names = [n for n, _ in rows]
    ok(names == ["初等行变换", "单位矩阵"], f"表头/分隔行/空行被跳过，术语去装饰（{names}）")
    ok(rows[0][1] == "三种基本操作", "解释保留")
    ok(concepts.parse_concepts("# 没有这一节") == [], "没有关键概念小节时返回空")
    ok(concepts.clean_term("  **单位 矩阵**： ") == "单位 矩阵", "清洗：去星号与尾部冒号")
    ok(concepts.name_key("单位 矩阵") == concepts.name_key("单位矩阵"),
       "归并键忽略空格")
    ok(concepts.name_key("NP-完全") == concepts.name_key("np完全"),
       "归并键忽略大小写与连接符")


def test_build(tmp: Path) -> None:
    out = tmp / "out"
    settings = Settings(data_dir=tmp / "data", output_dir=out)

    make_note(out, "第01集", "线代 01 矩阵基础", [
        ("单位矩阵", "记为 E，主对角线为 1"),
        ("逆矩阵", "AA⁻¹ = E"),
    ])
    make_note(out, "第02集", "线代 02 初等行变换", [
        ("单位 矩阵", "第二集的写法，中间带空格"),
        ("初等行变换", "对换、倍乘、倍加"),
        ("逆矩阵", "这一集从行变换的角度再讲一次"),
    ])
    make_note(out, "第03集", "线代 03 秩", [
        ("矩阵的秩", "行阶梯形非零行数"),
    ])

    items = concepts.build(settings)
    ok(len(items) == 4, f"4 个概念（实际 {len(items)}）")

    by_key = {i["key"]: i for i in items}
    key = concepts.name_key("单位矩阵")
    ok(key in by_key, f"「单位矩阵」被合并成一个（{sorted(by_key)}）")
    ok(by_key[key]["name"] == "单位矩阵",
       f"平票时显示更干净的写法（实际「{by_key[key]['name']}」）")
    unit = by_key[key]
    ok(unit["note_count"] == 2, f"跨 2 篇笔记命中（实际 {unit['note_count']}）")
    ok(len(unit["notes"]) == 2, "两篇笔记的解释都保留")
    ok({n["id"] for n in unit["notes"]} == {"第01集", "第02集"}, "命中的笔记正确")

    inv = by_key[concepts.name_key("逆矩阵")]
    ok(inv["note_count"] == 2, "「逆矩阵」也在两篇里出现")

    ok(items[0]["note_count"] >= items[-1]["note_count"], "按出现笔记数降序排序")

    # 卡片挂到概念上
    C.add(settings, "第01集", "线代 01 矩阵基础", [
        {"front": "单位矩阵长什么样？", "back": "主对角线为 1。", "concept": "单位矩阵", "kind": "概念"},
        {"front": "逆矩阵的定义式？", "back": "$AA^{-1}=E$", "concept": "逆矩阵", "kind": "公式"},
    ])
    items2 = concepts.build(settings)
    unit2 = next(i for i in items2 if i["key"] == key)
    ok(unit2["cards"] == 1, f"卡片计数挂到概念上（{unit2['cards']}）")
    ok(unit2["note_count"] == 2, "挂卡片不会污染笔记计数")

    detail = concepts.detail(settings, "单位矩阵")
    ok(detail is not None and len(detail["notes"]) == 2, "详情接口返回两篇笔记")
    ok(detail["cards"] and detail["cards"][0]["front"].startswith("单位矩阵"),
       "详情里带出相关卡片")
    ok(concepts.detail(settings, "不存在的概念") is None, "不存在的概念返回 None")

    # 只有卡片、没有关键概念表的概念也要出现在索引里
    C.add(settings, "第03集", "线代 03 秩", [
        {"front": "秩怎么算？", "back": "行阶梯形的非零行数。", "concept": "行阶梯形", "kind": "步骤"},
    ])
    names3 = {i["name"] for i in concepts.build(settings)}
    ok("行阶梯形" in names3, "只在卡片里出现的概念也进索引")

    summary = concepts.summary(settings)
    ok(summary["total"] == len(summary["items"]), "summary 的 total 与条目数一致")
    ok(all({"key", "name", "note_count", "cards"} <= set(i) for i in summary["items"]),
       "summary 字段完整")


def test_tags(tmp: Path) -> None:
    """非知识条目判别：规则、缓存、手动覆盖。"""
    ok(T.heuristic("中庵索道") == "place", "地点后缀 → place")
    ok(T.heuristic("茅家埠") == "place", "「埠」也算地点后缀")
    ok(T.heuristic("做不好也要做") == "soup", "鸡汤句式 → soup")
    ok(T.heuristic("广泛性焦虑症") == "knowledge", "学科术语 → knowledge")
    ok(T.heuristic("边界") == "knowledge", "边界不被「界」误判为地点")
    ok(T.heuristic("十里琅珰") == "other", "规则拿不准的留 other（不隐藏）")
    ok(T.heuristic("C盘") == "trivial", "盘符这类常识操作 → trivial")
    ok(T.heuristic("安装包") == "trivial", "安装包 → trivial")
    ok(T.heuristic("第二列化零") == "step", "例题里的具体步骤 → step")
    ok(T.heuristic("化零顺序") == "step", "化零顺序 → step")
    ok(T.heuristic("逆矩阵") == "knowledge", "方法/概念仍是 knowledge")

    settings = Settings(data_dir=tmp)
    names = ["中庵索道", "做不好也要做", "单位矩阵", "十里琅珰"]

    # 没有模型时用规则先兜底，并且要落盘
    result = T.classify(settings, None, names)
    ok(result["tagged"] == 4, f"规则判完 4 条（{result}）")
    tags = T.load(settings)
    ok(T.get_cat(tags, "中庵索道") == "place", "规则结论写进缓存")
    ok(T.get_cat(tags, "单位矩阵") == "knowledge", "学科词不被藏")

    # 只有 place / soup 会被前端隐藏，other 保持可见
    ok(T.HIDDEN_CATS == ("place", "soup", "trivial", "step", "relation", "artifact"),
       f"隐藏非知识条目六类（{T.HIDDEN_CATS}）")
    ok(T.get_cat(tags, "十里琅珰") not in T.HIDDEN_CATS, "拿不准的不会被藏")

    # 手动纠正优先，且 force 也盖不掉
    T.set_manual(settings, "中庵索道", "knowledge")
    ok(T.get_cat(T.load(settings), "中庵索道") == "knowledge", "手动纠正生效")
    T.classify(settings, None, names, force=True)
    ok(T.get_cat(T.load(settings), "中庵索道") == "knowledge", "force 重判也不覆盖手动结论")
    T.set_manual(settings, "中庵索道", "place")
    ok(T.get_cat(T.load(settings), "中庵索道") == "place", "手动可以改回去（误点也能撤销）")

    # annotate 给条目补 cat
    items = [{"name": "中庵索道"}, {"name": "单位矩阵"}]
    T.annotate(items, T.load(settings))
    ok(items[0]["cat"] == "place" and items[1]["cat"] == "knowledge",
       f"annotate 补上 cat 字段（{items[0]['cat']}/{items[1]['cat']}）")

    # 模型给出的层级要能落盘；手动改类别时不丢层级
    class FakeClient:
        model = "fake"

        def complete_json(self, system, prompt, max_tokens=0):
            return {"tags": [
                {"name": "主对角线", "cat": "knowledge", "parent": "初等行变换法"},
                {"name": "增广矩阵", "cat": "knowledge", "parent": "初等行变换法"},
            ]}

    T.classify(settings, FakeClient(), ["主对角线", "增广矩阵"])
    ok(T.get_parent(T.load(settings), "主对角线") == "初等行变换法",
       "模型判出的上级被保存")
    T.set_manual(settings, "主对角线", "knowledge")
    ok(T.get_parent(T.load(settings), "主对角线") == "初等行变换法",
       "手动改类别不丢上级")

    # 模型把「第二列化零」判成知识点时，硬规则要能纠正
    class StepClient:
        model = "fake"

        def complete_json(self, system, prompt, max_tokens=0):
            return {"tags": [{"name": "第二列化零", "cat": "knowledge", "parent": "初等行变换法"}]}

    T.classify(settings, StepClient(), ["第二列化零"])
    ok(T.get_cat(T.load(settings), "第二列化零") == "step",
       "模型判错时，实例步骤被硬规则纠正")
    ok(T.load(settings)[T.name_key("第二列化零")]["src"] == "fixed",
       "硬规则结论标记为 fixed，重判不会翻回去")

    nested = [{"name": "主对角线"}, {"name": "初等行变换法"}]
    T.annotate(nested, T.load(settings))
    ok(nested[0]["parent"] == "初等行变换法" and nested[0]["parent_key"] == "初等行变换法",
       "annotate 同时给出 parent 与 parent_key")

    # 缓存文件坏掉时按空处理，不抛异常
    (tmp / T.TAG_FILE).write_text("{坏", encoding="utf-8")
    ok(T.load(settings) == {}, "缓存损坏时安全降级")


def test_canonical_merge(tmp: Path) -> None:
    """规范名合并：关系句并进主体、变量不并、普通词不改名。"""
    known = {concepts.name_key("整数规划"): "整数规划"}
    ok(T._accept_canonical("IP属于NP", "relation", "整数规划问题（IP）", known)
       == "整数规划", "关系句并到列表里已有的主体上")
    ok(T._accept_canonical("可满足到可行解", "knowledge", "整数规划", known) == "",
       "普通词不接受规范名（防止子问题被合并）")
    ok(T._accept_canonical("NP", "knowledge", "NP（非确定性多项式时间）", known) != "",
       "短全大写缩写允许展开")

    out = tmp / "out"
    settings = Settings(data_dir=tmp / "data", output_dir=out)
    make_note(out, "第01集", "整数规划 01", [
        ("整数规划", "实例由约束和目标阈值组成。"),
        ("IP属于NP", "给定证书可在多项式时间验证。"),
        ("证书x", "给定后可在多项式时间验证的解。"),
    ])
    (settings.data_dir).mkdir(parents=True, exist_ok=True)
    (settings.data_dir / T.TAG_FILE).write_text(json.dumps({"version": 1, "tags": {
        "整数规划": {"name": "整数规划", "cat": "knowledge", "src": "manual"},
        "ip属于np": {"name": "IP属于NP", "cat": "relation",
                     "canonical": "整数规划", "src": "manual"},
        "证书x": {"name": "证书x", "cat": "artifact", "src": "manual"},
    }}, ensure_ascii=False), encoding="utf-8", newline="\n")

    items = concepts.build(settings)
    names = {i["name"] for i in items}
    ok(names == {"整数规划", "证书x"}, f"关系句被并入主体，不再单独成条（{names}）")

    merged = next(i for i in items if i["name"] == "整数规划")
    ok(merged["aliases"] == ["IP属于NP"], f"别名记录了原来的说法（{merged['aliases']}）")
    ok("给定证书" in merged["notes"][0]["explanation"],
       "关系句的解释并进主体的内容里，没有丢")
    ok(merged["note_count"] == 1, "同一篇笔记只算一次，不重复计数")

    detail = concepts.detail(settings, "IP属于NP")
    ok(detail is not None and detail["name"] == "整数规划",
       "用旧说法也能打开这个概念的详情")


def test_review(tmp: Path) -> None:
    """概念审核：派生名回流、上级必须落在真实概念上、生成期过闸。"""
    known = {concepts.name_key(n): n for n in
             ["初等行变换法", "整数规划", "归约", "倍乘化单位矩阵", "焦虑症"]}

    ok(R.merge_target("初等行变换法优势", known) == "初等行变换法",
       "「X + 优势」并回 X")
    ok(R.merge_target("整数规划实例", known) == "整数规划", "「X + 实例」并回 X")
    ok(R.same_concept("倍乘化单位阵", known) == "倍乘化单位矩阵",
       "差一个字的两种写法合成一个")
    ok(R.merge_target("可满足到可行解",
                      {"可行解到可满足": "可行解到可满足"}) == "",
       "同字异序（A到B / B到A）不当成同一个")
    ok(R.merge_target("第二列化零", {"第一列化零": "第一列化零"}) == "",
       "带行列定位词的不合并")
    ok(R.merge_target("完全不相干的概念", known) == "", "不相关的词不动")

    # 名字是"上级概念的碎片 + 限定词"：靠父子关系 + 残渣落点判断，不靠字面相似
    ok(R.via_parent("变换顺序", "初等行变换法", known) == "初等行变换法",
       "「变换顺序」是初等行变换法的碎片，并进去")
    matrix_known = {**known, concepts.name_key("矩阵连乘问题"): "矩阵连乘问题"}
    ok(R.via_parent("计算次序", "矩阵连乘问题", matrix_known) == "",
       "残渣「计算」不在父名里，不并（顺序/次序这类通用词不能乱吞）")
    ok(R.via_parent("整数规划判定问题", "整数规划", known) == "",
       "下级概念（父名只是它的一截）不会被并成上级")
    ok(R.via_parent("变换顺序", "初等行变换法", known, has_children=True) == "",
       "自己带下级的节点不合并，免得压平整棵子树")
    ok(R.via_unique_fragment("行变换使用限制", known) == "初等行变换法",
       "没有父级时，残渣全库唯一才认")
    ok(R.via_unique_fragment("变换顺序", {**known,
                                          concepts.name_key("矩阵变换"): "矩阵变换"}) == "",
       "残渣出现在两个概念名里就放弃，不猜")

    ok(R.resolve_parent("整数规划问题（IP）", "目标阈值", known) == "整数规划",
       "剥掉「问题（IP）」包装后挂到整数规划")
    ok(R.resolve_parent("归约示例", "变量映射", known) == "归约",
       "上级是「归约示例」时挂到归约")
    ok(R.resolve_parent("根本不存在的东西", "某个概念", known) == "",
       "上级对不上就清空，绝不造一个空父节点")
    ok(R.resolve_parent("整数规划", "整数规划", known) == "",
       "概念不能当自己的上级")
    ok(R.derive_parent("整数规划判定问题", known) == "整数规划",
       "没给上级时，「X判定问题」自动挂到 X")
    ok(R.derive_parent("矩阵相乘次数", known) == "",
       "名字里只是含了某个概念，不算它的下级")
    ok(R.judge("方法对比") and not R.judge("初等行变换法"),
       "空泛词判掉，真概念留下")

    out = tmp / "out"
    settings = Settings(data_dir=tmp / "data", output_dir=out)
    make_note(out, "第01集", "线代 01", [
        ("初等行变换法", "对换、倍乘、倍加"),
        ("单位矩阵", "记为 E"),
        ("中庵索道", "旅游视频里的景点"),
    ])
    T.classify(settings, None, ["初等行变换法", "单位矩阵", "中庵索道", "方法对比"])

    kept, report = R.review_glossary(settings, [
        {"term": "初等行变换法优势", "explain": "只用第三类操作"},
        {"term": "中庵索道", "explain": "景点"},
        {"term": "方法对比", "explain": "空话"},
        {"term": "单位矩阵", "explain": "主对角线为 1"},
        {"term": "单位矩阵", "explain": "重复的一行"},
    ])
    names = [k["term"] for k in kept]
    ok(names == ["初等行变换法", "单位矩阵"], f"生成期过闸只留知识条目（{names}）")
    ok("中庵索道" in report["dropped"] and "方法对比" in report["dropped"],
       f"景点与空泛词被剔除（{report['dropped']}）")
    ok(report["renamed"] == [("初等行变换法优势", "初等行变换法")],
       f"派生说法被并回已有概念（{report['renamed']}）")
    ok("重复的一行" in kept[1]["explain"], "同篇笔记里重复的条目并成一行，解释不丢")

    # 卡片的概念名也过闸
    cards = [{"front": "怎么把矩阵化成单位阵？", "back": "倍乘。",
              "concept": "初等行变换法优势", "kind": "步骤"}]
    ok(R.review_card_concepts(cards, ["初等行变换法"]) == 1
       and cards[0]["concept"] == "初等行变换法", "卡片上的派生概念名被收回")

    # 存量整理：碎片合并 + 层级重挂，且整理后不能再有孤儿父节点
    C.add(settings, "第01集", "线代 01", [
        {"front": "初等行变换法优势是什么？", "back": "少算一步。",
         "concept": "初等行变换法优势", "kind": "概念"},
        {"front": "整数规划判定问题怎么判定？", "back": "解 IP 看是否可行。",
         "concept": "整数规划判定问题", "kind": "概念"},
    ])
    T.set_manual(settings, "整数规划判定问题", "knowledge")
    tidy = R.tidy(settings)
    ok(["初等行变换法优势", "初等行变换法"] in tidy["merged"],
       f"整理时把派生卡片概念并回主体（{tidy['merged']}）")

    items = concepts.build(settings)
    T.annotate(items, T.load(settings))
    keys = {i["key"] for i in items}
    orphans = [i["name"] for i in items if i["parent_key"] and i["parent_key"] not in keys]
    ok(not orphans, f"页面上不出现挂在不存在父节点上的条目（{orphans}）")
    ok(next(i for i in items if i["name"] == "整数规划判定问题")["cat"] == "knowledge",
       "手动判过的类别不会被整理改掉")


def test_repoint_cards(tmp: Path) -> None:
    """卡片概念回流：别名合并后，卡片上残留的旧概念名要跟着改。"""
    out = tmp / "out"
    settings = Settings(data_dir=tmp / "data", output_dir=out)
    (settings.data_dir).mkdir(parents=True, exist_ok=True)
    (settings.data_dir / T.TAG_FILE).write_text(json.dumps({"version": 1, "tags": {
        "初等行变换法": {"name": "初等行变换法", "cat": "knowledge", "src": "manual"},
        "变换顺序": {"name": "变换顺序", "cat": "knowledge",
                     "canonical": "初等行变换法", "src": "manual"},
    }}, ensure_ascii=False), encoding="utf-8", newline="\n")
    C.add(settings, "n1", "笔记一", [
        {"front": "q1", "back": "a1", "concept": "变换顺序", "kind": "概念"},
        {"front": "q2", "back": "a2", "concept": "初等行变换法", "kind": "概念"},
    ])
    changed = R.repoint_cards(settings)
    ok(changed == 1, f"只有旧名卡片被改（changed={changed}）")
    rows = {c["front"]: c["concept"] for c in C.load(settings)}
    ok(rows["q1"] == "初等行变换法", "卡片概念改成规范名")
    ok(rows["q2"] == "初等行变换法", "规范名卡片不动")

    ok(R.repoint_cards(settings) == 0, "幂等：再跑一遍没有可改的")


def test_ai_review(tmp: Path) -> None:
    """AI 复核：只送可疑的、只认词表里的合并、keep 记得住。"""
    out = tmp / "out"
    settings = Settings(data_dir=tmp / "data", output_dir=out)
    make_note(out, "第01集", "线代 01", [("初等行变换法", "对换、倍乘、倍加")])
    C.add(settings, "第01集", "线代 01", [
        {"front": "行变换的三条准则是什么？", "back": "对换、倍乘、倍加。",
         "concept": "行变换的三条准则", "kind": "概念"},
        {"front": "行变换的安全前提是什么？", "back": "矩阵可逆。",
         "concept": "行变换的安全前提", "kind": "概念"},
    ])
    T.classify(settings, None, ["初等行变换法"])

    class FakeAI:
        model = "fake"

        def __init__(self, verdicts):
            self.verdicts = verdicts
            self.prompts = []

        def complete_json(self, system, prompt, max_tokens=0):
            self.prompts.append(prompt)
            return {"verdicts": self.verdicts}

    ai = FakeAI([
        {"name": "行变换的三条准则", "action": "merge", "into": "初等行变换法", "why": "细节"},
        {"name": "行变换的三条准则", "action": "merge", "into": "这个词表里没有"},
        {"name": "初等行变换法", "action": "merge", "into": "初等行变换法"},
        {"name": "模型瞎编的词", "action": "merge", "into": "初等行变换法"},
        {"name": "行变换的安全前提", "action": "keep", "into": "", "why": "独立注意事项"},
    ])
    result = R.ai_review(settings, ai)
    ok(result["checked"] == 2, f"只把可疑的送模型（送 {result['checked']} 条）")
    ok("初等行变换法" in ai.prompts[0] and "行变换的三条准则" in ai.prompts[0],
       "已有概念词表和候选词都进了 prompt")
    ok([m[:2] for m in result["merged"]] == [["行变换的三条准则", "初等行变换法"]],
       f"只认词表里存在的合并（{result['merged']}）")
    names = {i["name"] for i in concepts.build(settings)}
    ok("行变换的三条准则" not in names, "合并后不再单独成条")
    ok("行变换的安全前提" in names, "keep 的保持独立")
    ok(T.load(settings)[T.name_key("行变换的安全前提")].get("ai_review") == 1,
       "keep 写了 ai_review 标记")

    again = R.ai_review(settings, FakeAI([]))
    ok(again["checked"] == 0 and again["reason"] == "nothing",
       f"复核过的、已合并的都不再送（{again}）")

    none = R.ai_review(settings, None)
    ok(none["reason"] == "no_key", "没配模型时明确返回 no_key，不报错")


def main() -> int:
    print("=== 表格解析 ===")
    test_parse()
    print("\n=== 跨笔记聚合 ===")
    with tempfile.TemporaryDirectory() as raw:
        test_build(Path(raw))
    print("\n=== 非知识条目判别 ===")
    with tempfile.TemporaryDirectory() as raw:
        test_tags(Path(raw))
    print("\n=== 规范名合并 ===")
    with tempfile.TemporaryDirectory() as raw:
        test_canonical_merge(Path(raw))
    print("\n=== 概念审核 ===")
    with tempfile.TemporaryDirectory() as raw:
        test_review(Path(raw))
    print("\n=== 卡片概念回流 ===")
    with tempfile.TemporaryDirectory() as raw:
        test_repoint_cards(Path(raw))
    print("\n=== AI 复核 ===")
    with tempfile.TemporaryDirectory() as raw:
        test_ai_review(Path(raw))
    print()
    if FAILED:
        print(f"{FAILED} 项未通过")
        return 1
    print("概念索引全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
