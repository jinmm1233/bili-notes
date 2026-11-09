"""英语生词回归：词库解析、词形还原、外语判定、笔记标注。

跑法：python tests/smoke_words.py
不联网、不碰真实数据目录（全部在临时目录里）。
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
from app.services import wordbook as B  # noqa: E402
from app.services import words as W  # noqa: E402

FAILED = 0


def ok(cond: bool, msg: str) -> None:
    global FAILED
    print(("[OK] " if cond else "[FAIL] ") + msg)
    if not cond:
        FAILED += 1


NOTE = """---
title: "测试：晨间习惯"
---

# 测试：晨间习惯

## 一句话总结

讲了习惯怎么养成。

## 完整转录

<details><summary>展开全文（3 句，含 3 句中文译文）</summary>

`00:10` I went to the park and stopped running every morning.
`中` 我去公园，每天早上停止跑步。
`00:22` She studies harder and sets bigger goals than before.
`中` 她更努力学习，定的目标更大。
`00:31` A worker with a number can plan the day.
`中` 一个有编号的工人可以规划一天。

</details>

---

*测试用*
"""


def test_parse(tmp: Path) -> None:
    print("\n=== 词库解析 ===")
    settings = Settings(data_dir=tmp, output_dir=tmp / "out")

    csv_head = "word,pos,meaning\nresilient,adj.,有韧性的\nsustain,v.,维持\n"
    got = B.parse(csv_head.encode(), "cet6.csv")
    ok(len(got["words"]) == 2, f"带表头 CSV 解析出 {len(got['words'])} 词")
    ok(got["words"][0]["pos"] == "adj.", "词性带点")
    ok(got["format"] == "csv", "识别为 csv")

    csv_no_head = "resilient,有韧性的\nsustain,维持\n"
    got = B.parse(csv_no_head.encode(), "x.csv")
    ok(len(got["words"]) == 2 and got["words"][0]["cn"] == "有韧性的", "无表头 CSV 也认")

    txt = "resilient\tadj.\t有韧性的\nsustain vt. 维持\n"
    got = B.parse(txt.encode(), "x.txt")
    ok(len(got["words"]) == 2, f"TXT 两种写法都解析出来（{len(got['words'])}）")
    ok(got["words"][1]["pos"] == "vt.", f"空格分隔时能拆出词性（{got['words'][1]['pos']}）")
    ok(got["words"][1]["cn"] == "维持", "空格分隔时释义完整")

    js = '{"words":[{"w":"resilient","pos":"adj.","cn":"有韧性的"}]}'
    ok(len(B.parse(js.encode(), "x.json")["words"]) == 1, "JSON 也认")

    bad = "resilient,adj.,有韧性的\nnot_a_word_here,,\n\x00,,,\n"
    got = B.parse(bad.encode(), "x.csv")
    ok(got["skipped"] >= 1, f"坏行被计数（跳过 {got['skipped']}）")
    ok(bool(got["errors"]), "坏行给出原因而不是静默丢弃")
    ok(all("line" in e for e in got["errors"]), "报错带行号")

    book = B.save(settings, B.parse(csv_head.encode(), "cet6.csv"), name="六级")
    ok(book["count"] == 2 and (Path(settings.data_dir) / "wordbooks" / "六级.json").is_file(),
       "词库落盘")
    idx = B.index(settings)
    ok("resilient" in idx and idx["resilient"]["cn"] == "有韧性的", "合并索引可取到释义")
    ok(B.summary(settings)["total_words"] == 2, "汇总词数正确")
    ok(B.delete(settings, "六级") and not B.load_all(settings), "删除词库")


def test_lemma() -> None:
    print("\n=== 词形还原 ===")
    cases = [
        ("went", "go"), ("gone", "go"), ("stopped", "stop"), ("running", "run"),
        ("studies", "study"), ("studied", "study"), ("bigger", "big"),
        ("biggest", "big"), ("making", "make"), ("children", "child"),
        ("resources", "resource"), ("avoiding", "avoid"),
    ]
    for form, lemma in cases:
        ok(lemma in W.lemmas(form), f"{form} → {lemma}")

    for word in ("worker", "teacher", "number", "water", "corner", "other"):
        cands = W.lemmas(word)
        ok(len(cands) == 1 and cands[0] == word,
           f"{word} 不被误剥（候选 {cands}）")


def test_foreign() -> None:
    print("\n=== 外语判定 ===")
    ok(W.note_is_foreign(NOTE), "英文转录判为外语")
    zh = NOTE.replace("I went to the park and stopped running every morning.",
                      "我今天早上去公园跑步，然后停下来休息。")
    ok(not W.note_is_foreign(zh.replace("She studies harder and sets bigger goals than before.",
                                       "她更努力学习，目标也更大。")
                              .replace("A worker with a number can plan the day.",
                                       "一个有编号的工人可以规划一天。")), "中文转录不判为外语")
    ok(not W.note_is_foreign("只有一句话。"), "没有转录时不当外语")


def test_annotate(tmp: Path) -> None:
    print("\n=== 笔记标注 ===")
    settings = Settings(data_dir=tmp, output_dir=tmp / "out")
    B.save(settings, B.parse(
        "word,pos,meaning\ngo,v.,去\nstop,v.,停止\nrun,v.,跑\nstudy,v.,学习\n"
        "big,adj.,大的\nwork,n.,工作\nnumber,n.,数字\nplan,v.,规划\n".encode(),
        "demo.csv"), name="演示")
    index = B.index(settings)

    md, stats = W.annotate(NOTE, index)
    ok(stats["hits"] >= 5, f"命中 {stats['hits']} 个词")
    names = {w["w"] for w in stats["words"]}
    ok({"go", "stop", "run", "study", "big"} <= names, f"屈折形式都归到原形（{sorted(names)}）")

    ok("## 生词（演示 · 命中" in md, "生成了生词段")
    ok(md.index("## 生词") < md.index("## 完整转录"), "生词段在转录之前")
    ok('<a id="w-go"></a>' in md, "词条带锚点")
    ok("[went](#w-go)" in md, "原文里的词变成锚点链接")
    ok("（文中作 went）" in md, "标出文中用的是哪种形式")
    ok("`00:10`" in md, "例句带时间戳")

    again, stats2 = W.annotate(md, index)
    ok(again == md, "重复标注结果不变（幂等，不会套娃）")
    ok(again.count("<!-- words:start -->") == 1, "只有一段生词")

    # 词库为空 / 一个词都没命中：一个字符都不能动
    empty_md, empty_stats = W.annotate(NOTE, {})
    ok(empty_md == NOTE and empty_stats["hits"] == 0, "空词库不改笔记")
    other = {"zzzz": {"w": "zzzz", "pos": "", "cn": "无", "books": ["x"]}}
    same, s2 = W.annotate(NOTE, other)
    ok(same == NOTE and s2["hits"] == 0, "没命中就不加任何标记")

    ok(W.strip_anchors(md).count('<a id="w-') == 0, "导出前能去掉锚点标签")


def test_ui_data(tmp: Path) -> None:
    """前端悬停要的那份数据：words.json 的形状。"""
    print("\n=== 给前端的数据 ===")
    settings = Settings(data_dir=tmp, output_dir=tmp / "out")
    B.save(settings, B.parse("word,pos,meaning\nrun,v.,跑\n".encode(), "d.csv"), name="演示")
    md, stats = W.annotate(NOTE, B.index(settings))
    ok(stats["words"] and set(stats["words"][0]) >= {"w", "pos", "cn", "count", "ts"},
       "词条字段够前端用（词/词性/释义/次数/时间）")
    ok(W.slug("Run") == "run" and W.slug("well-known") == "well-known", "锚点 slug 稳定")


def test_fill(tmp: Path) -> None:
    print("\n=== AI 补释义 ===")
    settings = Settings(data_dir=tmp, output_dir=tmp / "out")

    # 「缺释义也导入」：单词行收进来（missing 计数），有释义的照常
    parsed = B.parse("pipeline\nthroughput\tadj.\t吞吐量\n".encode(), "w.txt", keep_missing=True)
    ok(parsed["missing"] == 1 and len(parsed["words"]) == 2,
       f"单词行也收进来（missing={parsed['missing']}）")
    ok(parsed["words"][0]["cn"] == "", "空释义留白等 AI 回填")

    # 默认行为不变：缺释义还是坏行
    strict = B.parse("pipeline\n".encode(), "w.txt")
    ok(strict["missing"] == 0 and strict["skipped"] == 1 and not strict["words"],
       "默认缺释义仍报坏行")

    B.save(settings, parsed, name="补测")

    class FakeClient:
        def complete_json(self, _system: str, user: str, max_tokens: int = 2048):
            assert "pipeline" in user
            return {"pipeline": {"pos": "n.", "cn": "流水线"}}

    got = B.fill_definitions(settings, FakeClient())
    ok(got["filled"] == 1 and got["remaining"] == 0,
       f"补上 1 个、剩余 0（filled={got['filled']} remaining={got['remaining']}）")
    data = json.loads((Path(settings.data_dir) / "wordbooks" / "补测.json").read_text(encoding="utf-8"))
    by_w = {w["w"]: w for w in data["words"]}
    ok(by_w["pipeline"]["cn"] == "流水线" and by_w["pipeline"]["pos"] == "n.",
       "释义与词性回写落盘")
    ok(by_w["throughput"]["cn"] == "吞吐量", "原有释义不动")

    # 全都有释义时是干净的空操作
    again = B.fill_definitions(settings, FakeClient())
    ok(again["filled"] == 0 and again["batches"] == 0, "幂等：没缺的不再调模型")


def test_word_cards(tmp: Path) -> None:
    print("\n=== 生词变卡片 ===")
    out = tmp / "out"
    note = out / "n1"
    note.mkdir(parents=True)
    # 一篇"英语笔记"里遇到过 pipeline（2 次）和 ghost word（不在词库里）
    (note / "words.json").write_text(json.dumps({
        "generated_at": 0, "books": ["词库"],
        "words": [{"w": "pipeline", "pos": "n.", "cn": "流水线", "count": 2, "ts": "00:01"},
                  {"w": "ghostword", "pos": "", "cn": "", "count": 1, "ts": "00:02"}],
    }, ensure_ascii=False), encoding="utf-8")
    settings = Settings(data_dir=tmp, output_dir=out)
    B.save(settings, B.parse("word,pos,meaning\npipeline,n.,流水线\n".encode(), "c.csv"), name="六级")

    got = B.to_cards(settings)
    ok(got["added"] == 1 and got["dup"] == 0,
       f"只在词库里的词变卡（added={got['added']}，ghostword 无释义被跳过）")
    from app.services import cards as card_svc  # noqa: PLC0415
    cards = card_svc.load(settings)
    ok(len(cards) == 1 and cards[0]["note_id"] == B.WORD_NOTE_ID
       and cards[0]["kind"] == "单词" and cards[0]["front"] == "pipeline",
       "卡片挂到单词卡虚拟笔记下")
    ok("出现 2 次" in cards[0]["back"], "背面带出现次数")

    again = B.to_cards(settings)
    ok(again["added"] == 0 and again["dup"] == 1, "再点一次不重复建卡")

    empty = B.to_cards(Settings(data_dir=tmp / "none", output_dir=tmp / "none2"))
    ok(empty["added"] == 0 and empty.get("hint"), "没有生词记录时给提示而不是报错")


def main() -> int:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        test_parse(root / "a")
        test_lemma()
        test_foreign()
        test_annotate(root / "b")
        test_ui_data(root / "c")
    with tempfile.TemporaryDirectory() as raw:
        test_fill(Path(raw))
    with tempfile.TemporaryDirectory() as raw:
        test_word_cards(Path(raw))
    print()
    if FAILED:
        print(f"{FAILED} 项未通过")
        return 1
    print("英语生词全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
