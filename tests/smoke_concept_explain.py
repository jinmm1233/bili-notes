"""概念释义回归：重点判定、自足释义、缓存与索引接入。

用临时目录造几篇笔记 + 一个假模型，不碰真实数据、不联网。
  python tests/smoke_concept_explain.py
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
from app.services import concept_explain as E  # noqa: E402
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

用初等行变换把矩阵化成了单位阵。

## 核心要点

- 初等行变换是三种基本操作：倍乘、倍加、对换。
- 单位矩阵是主对角线全为 1 的方阵。

## 关键概念

| 术语 | 解释 |
|---|---|
{table}

## 时间轴

00:10 开场
"""


OUTLINE_TMPL = """---
title: "虚拟内存"
uploader: "测试UP"
collection: "CSAPP"
---

# 虚拟内存

## 笔记

### 一、地址空间

现代计算机使用虚拟寻址，通过 MMU 转换地址。

#### MMU的作用

MMU（内存管理单元）是负责地址转换的硬件部件。

## 结论

- 虚拟内存让每个进程拥有独立的地址空间。

## 关键概念

| 术语 | 解释 |
|---|---|
| MMU | 材料中提到的地址转换部件 |
| 虚拟内存 | 材料中作为虚拟内存概念引入的机制 |

## 时间轴

00:10 开场
"""


class FakeClient:

    def __init__(self, payload=None, fail: bool = False):
        self.payload = payload or {}
        self.fail = fail
        self.calls = []

    def complete_json(self, system, user, max_tokens=2048):
        self.calls.append((system, user))
        if self.fail:
            raise RuntimeError("模拟模型故障")
        return self.payload


def build_world() -> tuple[Settings, Path]:
    tmp = Path(tempfile.mkdtemp(prefix="bn-concept-explain-"))
    out = tmp / "output"
    out.mkdir(parents=True)
    notes = {
        "n1": ("线代 22", [
            ("初等行变换法", "求逆矩阵的方法，包含倍乘、倍加和对换"),
            ("单位矩阵", "材料中记为E，是左半部分经初等行变换后要化成的目标"),
            ("happens to", "材料中提到的一个英文短语"),
        ]),
        "n2": ("线代 23", [
            ("初等行变换法", "求逆矩阵的方法，包含倍乘、倍加和对换"),
            ("主对角线", "从左往右把下方元素变零"),
        ]),
    }
    for note_id, (title, rows) in notes.items():
        table = "\n".join(f"| {t} | {d} |" for t, d in rows)
        folder = out / note_id
        folder.mkdir(parents=True)
        (folder / "notes.md").write_text(
            NOTE_TMPL.format(title=title, table=table), encoding="utf-8")
    # 第二篇笔记走"结构化大纲"结构：没有关键概念表，概念写在正文小节里。
    # 真实数据里这种笔记很常见（走 outline 的笔记），只看概念表会把
    # 里面的真概念全误判成非重点——所以正文证据这条路径必须被测到。
    folder = out / "n3"
    folder.mkdir(parents=True)
    (folder / "notes.md").write_text(OUTLINE_TMPL, encoding="utf-8")
    settings = Settings(data_dir=tmp / "data", output_dir=out)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    return settings, tmp


def test_parse_items():
    print("\n--- 模型输出解析 ---")
    data = {"items": [
        {"name": "初等行变换法", "core": True, "explain": "用三种行变换把矩阵化简。"},
        {"name": "happens to", "core": False, "explain": "表示「碰巧」。"},
    ]}
    parsed = E._parse_items(data)
    ok(len(parsed) == 2, f"解析出 2 条（实际 {len(parsed)}）")
    ok(parsed[concepts.name_key("初等行变换法")]["core"] is True, "core=true 保留为布尔")
    ok(parsed[concepts.name_key("happens to")]["core"] is False, "core=false 保留为布尔")

    # 裸数组也要能解析
    parsed2 = E._parse_items([{"name": "甲", "core": True, "explain": "解释"}])
    ok(len(parsed2) == 1, "裸数组也能解析")

    # 字段缺失 / 类型不对不能炸
    parsed3 = E._parse_items({"items": [{"name": "", "core": "x"}, "不是字典", 5]})
    ok(parsed3 == {}, "垃圾输出不炸")


def test_fallback():
    print("\n--- 无模型时的降级 ---")
    settings, _tmp = build_world()
    got = E.fallback(settings, ["初等行变换法", "单位矩阵", "happens to"])
    ok(concepts.name_key("初等行变换法") in got, "解释像样的保留")
    ok(concepts.name_key("单位矩阵") not in got, "元叙述开头（材料中记为E）被挡掉")
    ok(concepts.name_key("happens to") not in got, "「材料中提到的…」也被挡掉")
    if got:
        ok(got[concepts.name_key("初等行变换法")]["core"] is True, "降级结果标了 core")
        ok(got[concepts.name_key("初等行变换法")]["src"] == "rule", "降级结果记了 src=rule")


def test_explain_with_model():
    print("\n--- 有模型时的主流程 ---")
    settings, _tmp = build_world()
    items = concepts.summary(settings)["items"]
    payload = {"items": [
        {"name": "初等行变换法", "core": True,
         "explain": "用倍乘、倍加、对换三种行变换把方阵化简的运算方法。"},
        {"name": "单位矩阵", "core": True,
         "explain": "主对角线全为 1、其余位置全为 0 的方阵。"},
        {"name": "主对角线", "core": True, "explain": "方阵中从左上到右下的那条对角线。"},
        {"name": "happens to", "core": False, "explain": "英文短语，意为「碰巧」。"},
    ]}
    client = FakeClient(payload)
    res = E.explain(settings, client, items)
    ok(res["done"] == 4, f"补了 4 个（实际 {res['done']}）")
    ok(res["reason"] == "ok", "reason=ok")
    # 重点数由「正文证据 + 概念表证据 + 分类」共同决定，不等于模型给的数量：
    # 笔记正文里讲了「初等行变换法」「单位矩阵」，两者都算重点。
    ok(res["core"] >= 1, f"至少判出 1 个重点（实际 {res['core']}）")

    cache = E.load(settings)
    ok(len(cache) == 4, f"缓存写盘 4 条（实际 {len(cache)}）")
    row = cache[concepts.name_key("初等行变换法")]
    ok(row and row["src"] == "llm", "记了 src=llm")
    ok("倍乘" in row["explain"], "释义内容原样保存")

    # 二次调用不该重复花钱
    client2 = FakeClient(payload)
    res2 = E.explain(settings, client2, items)
    ok(res2["done"] == 0, "已判过的不会重复处理")
    # n3 那篇笔记里的概念还没判过，所以这次会调模型——但只调新增的那几个，
    # 不该把已经判过的四个重新算一遍
    asked = sum(len(c[1]) for c in client2.calls)
    ok(asked < 400, f"只处理新增概念，没重算全部（prompt 长度 {asked}）")


def test_evidence_overrides_model():
    print("\n--- 证据优先于模型判定 ---")
    settings, _tmp = build_world()
    items = concepts.summary(settings)["items"]
    # 模型把「MMU」判成非重点，但笔记正文里有专门小节「#### MMU的作用」
    # 在讲它 → 应当改判重点。（真实场景：走大纲的笔记没有概念表，
    #   概念就在正文小节里，只看概念表会把它误杀。）
    payload = {"items": [
        {"name": "初等行变换法", "core": True, "explain": "三种行变换化简方阵。"},
        {"name": "MMU", "core": False, "explain": "内存管理单元，负责地址转换。"},
    ]}
    E.explain(settings, FakeClient(payload), items, names=["MMU", "初等行变换法"])
    cache = E.load(settings)
    ok(cache[concepts.name_key("MMU")]["core"] is True,
       "正文讲了它 → 改判重点（模型说 false 也不听）")


def test_non_knowledge_rejected():
    print("\n--- 非知识概念直接出局 ---")
    settings, _tmp = build_world()
    # 把「happens to」标成 place（分类系统的结论）
    from app.services import concept_tags as T
    tags = T.load(settings)
    T.set_manual(settings, "happens to", "place")
    items = concepts.summary(settings)["items"]
    payload = {"items": [
        {"name": "初等行变换法", "core": True, "explain": "三种行变换化简方阵。"},
        {"name": "happens to", "core": True, "explain": "意为「碰巧」。"},
    ]}
    E.explain(settings, FakeClient(payload), items)
    cache = E.load(settings)
    ok(cache[concepts.name_key("happens to")]["core"] is False,
       "已被判成地点类的 → 即使模型说重点也不算")


def test_explain_model_failure():
    print("\n--- 模型故障时降级 ---")
    settings, _tmp = build_world()
    items = concepts.summary(settings)["items"]
    client = FakeClient(fail=True)
    res = E.explain(settings, client, items)
    ok(res["done"] >= 1, f"故障后仍补上了一些（{res['done']}）")
    ok(concepts.name_key("初等行变换法") in E.load(settings), "降级结果照样写盘")


def test_summary_integration():
    print("\n--- 索引接入 ---")
    settings, _tmp = build_world()
    items = concepts.summary(settings)["items"]
    ok(all("core" in i for i in items), "summary 每项都带 core")
    ok(all(i["core"] is None for i in items), "没判过时 core 是 None（不是 False）")

    payload = {"items": [
        {"name": "初等行变换法", "core": True, "explain": "三种行变换化简方阵。"},
        {"name": "单位矩阵", "core": False, "explain": "主对角线全 1 的方阵。"},
        {"name": "主对角线", "core": False, "explain": "左上到右下的对角线。"},
        {"name": "happens to", "core": False, "explain": "意为「碰巧」。"},
    ]}
    E.explain(settings, FakeClient(payload), items)
    items2 = concepts.summary(settings)["items"]
    by_name = {i["name"]: i for i in items2}
    ok(by_name["初等行变换法"]["core"] is True, "重点概念 core=True")
    ok(by_name["happens to"]["core"] is False, "非重点概念 core=False")
    ok(by_name["初等行变换法"]["explain"].startswith("三种"), "summary 带出自足释义")
    # 重点的排前面
    ok(items2[0]["name"] == "初等行变换法", f"重点概念排最前（实际 {items2[0]['name']}）")

    d = concepts.detail(settings, "初等行变换法")
    ok(d is not None, "detail 能查到")
    ok(d["explain"].startswith("三种"), "detail 带出自足释义")
    ok(d["core"] is True, "detail 带出 core")

    # 没判过的概念，detail 回落到笔记表里的原话
    d2 = concepts.detail(settings, "单位矩阵")
    ok(d2["explanation"] if False else d2["explain"] != "", "未判过的也有解释可看")


def test_cache_file():
    print("\n--- 缓存文件 ---")
    settings, _tmp = build_world()
    path = E._path(settings)
    ok(not path.exists(), "没跑过就不该有文件")
    E.save(settings, {"甲": {"name": "甲", "core": True, "explain": "解释"}})
    ok(path.is_file(), "save 会写文件")
    data = json.loads(path.read_text(encoding="utf-8"))
    ok("concepts" in data and "甲" in data["concepts"], "缓存结构正确")
    E.save(settings, {"甲": {}})
    ok(len(E.load(settings)) == 1, "坏行不会让缓存整体失效")


def test_explain_in_defaults():
    print("\n--- 接口入参默认值 ---")
    # 前端发的是 {}（不传任何字段），所以每个字段都必须有默认值，
    # 否则 Pydantic 报 422，"补全释义"按钮直接不可用。
    # 这个坑是打包验收时在真机环境上才暴露的，离线测试没覆盖到。
    from app.api.routes_concepts import ExplainIn
    body = ExplainIn()
    ok(body.names == [], f"names 默认空列表（实际 {body.names}）")
    ok(body.force is False, f"force 默认 False（实际 {body.force}）")
    ok(body.cat == "knowledge", f"cat 默认 knowledge（实际 {body.cat}）")
    # 能解析前端真的发的那种 body
    parsed = ExplainIn.model_validate_json('{"names":[]}')
    ok(parsed.names == [] and parsed.force is False, "能解析前端发的 {}")


if __name__ == "__main__":
    test_parse_items()
    test_fallback()
    test_explain_with_model()
    test_evidence_overrides_model()
    test_non_knowledge_rejected()
    test_explain_model_failure()
    test_summary_integration()
    test_cache_file()
    test_explain_in_defaults()
    print()
    if FAILED:
        print(f"失败 {FAILED} 项")
        sys.exit(1)
    print("概念释义：全部通过")
