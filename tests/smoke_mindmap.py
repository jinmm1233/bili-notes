# -*- coding: utf-8 -*-
"""知识地图冒烟：纵向布局不压字、SVG 零依赖、flowchart 能把步骤串成一列。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.models import Chapter, VideoMeta
from app.services import mindmap as mm


def meta() -> VideoMeta:
    return VideoMeta(
        bvid="BV1", cid=1, title="测试视频标题" * 5, uploader="u",
        duration=600, pubdate=0, cover="", webpage_url="https://www.bilibili.com/video/BV1",
    )


def chapters() -> list[Chapter]:
    long = "这是一个非常非常长的要点，用来测试折行与横向跨度分配是否正确，超过三十个字就会折成多行"
    return [
        Chapter(index=0, title="章节一", start=0, end=300, chunk_indexes=[0],
                bullets=[long, "短要点", long]),
        Chapter(index=1, title="章节二标题也写得比较长一些来测换行", start=300, end=600,
                chunk_indexes=[1], bullets=["要点甲", "要点乙"]),
    ]


def test_default_tree_is_chapters_only():
    """默认只画章节：要点是 30 字长句，画成节点必然横着铺满一屏。"""
    tree = mm.build_tree(meta(), chapters())
    assert len(tree["children"]) == 2
    assert all("children" not in c for c in tree["children"])


def test_layout_no_overlap():
    # 想看要点时仍可显式打开（比如导出给别的工具），布局不能压字
    nodes, w, h = mm._layout(mm.build_tree(meta(), chapters(), with_points=True))
    assert len(nodes) >= 8 and w > 0 and h > 0
    by_depth: dict[int, list] = {}
    for n in nodes:
        by_depth.setdefault(n["depth"], []).append(n)
    for d, ns in by_depth.items():
        ns.sort(key=lambda n: n["x"])
        for a, b in zip(ns, ns[1:]):
            gap = (b["x"] - b["w"] / 2) - (a["x"] + a["w"] / 2)
            assert gap >= -0.1, f"深度{d} 横向压字: {a['lines']} vs {b['lines']}"
        ys = {n["y"] for n in ns}
        assert len(ys) == 1, f"深度{d} 应在同一行"
    for n in nodes:
        if n["parent"] is not None:
            assert n["y"] > nodes[n["parent"]]["y"], "子节点必须在父节点下方"
    print(f"  布局：{len(nodes)} 节点 / {w:.0f}x{h:.0f}，无压字、层级向下正确")


def test_html_self_contained():
    # 默认树只有章节（3 个节点），这里要看节点渲染，所以显式带上要点
    html = mm.to_markmap_html(mm.build_tree(meta(), chapters(), with_points=True), "标题")
    assert "cdn.jsdelivr" not in html and "markmap-autoloader" not in html
    assert html.count("<rect") >= 8, "必须是静态 SVG 节点，不是等 JS 画"
    assert "<script type=\"text/template\">" not in html, "不能再靠内嵌模板等外部脚本渲染"
    print(f"  HTML：{len(html)} 字节，零外部依赖，SVG 直接可看")


def test_svg_escapes():
    ch = chapters()
    ch[0].title = '带<标签>和 "引号" 的标题'
    tree = mm.build_tree(meta(), ch)
    html = mm.to_markmap_html(tree, '标题&<x>')
    assert "&lt;标签&gt;" in html and "标题&amp;&lt;x&gt;" in html
    print("  转义：特殊字符不会破坏 SVG 结构")


def test_mermaid_flowchart():
    # 带要点（给 Obsidian 之类的外部工具用）仍然是层级树
    tree = mm.build_tree(meta(), chapters(), with_points=True)
    mer = mm.to_mermaid(tree)
    # 多个并列章节：横着展开（层级树），不会宽到没法看
    assert mer.startswith("flowchart LR"), mer[:40]
    # 根→2 个章节，章节各 3 / 2 个要点；无顺序词，全部并排
    assert mer.count("-->") == 7
    # 正文用的默认树只画章节：边数 = 章节数
    assert mm.to_mermaid(mm.build_tree(meta(), chapters())).count("-->") == 2
    assert mm.to_outline_md(tree).count("\n- ") == 2
    print("  三形态：flowchart / outline 输出正常")


def test_steps_chain_in_one_column():
    """递进的步骤必须串成一列，且按执行顺序（而不是模型给的原始顺序）。"""
    tree = {"name": "【线代】22、初等行变换法求逆矩阵", "children": [
        {"name": "初等行变换法求逆矩阵", "children": [
            {"name": "将待求逆矩阵A与单位矩阵E组成增广矩阵"},
            {"name": "通过初等行变换使左边变为单位矩阵E"},
            {"name": "变换后右边即为A的逆矩阵"},
            {"name": "先将主对角线下方元素依次变为零"},
            {"name": "再将主对角线上方元素依次变为零"},
        ]},
    ]}
    assert mm._chainify(tree)[1], "单章节 + 顺序步骤 = 步骤链"
    mer = mm.to_mermaid(tree)
    assert mer.startswith("flowchart TD"), "步骤链要竖着排一列"
    assert "① 将待求逆矩阵" in mer and "⑤ 变换后右边即为" in mer, mer
    # 步骤之间必须是"上一步 --> 下一步"的链，而不是都挂在章节下面
    assert "n3 --> n4" in mer and "n6 --> n7" in mer
    assert mer.count("n2 -->") == 1, "章节只连到第一步，其余靠链式箭头"
    print("  步骤：5 步串成一列，顺序 = 组成增广矩阵 → 变换 → 先下后上 → 得逆矩阵")


def test_parallel_points_not_chained():
    tree = {"name": "T", "children": [
        {"name": "并列要点", "children": [{"name": "苹果"}, {"name": "香蕉"}, {"name": "橘子"}]},
    ]}
    mer = mm.to_mermaid(tree)
    assert "①" not in mer and mer.count("-->") == 4
    assert not mm._chainify(tree)[1]
    print("  并列：无顺序词不硬串成链，整图走横向层级树")


def test_mermaid_label_safe():
    tree = {"name": 'T "引号" <尖括号>', "children": [
        {"name": "章节: 冒号", "children": [{"name": '要点 "带引号" 和 `反引号`'}]},
    ]}
    mer = mm.to_mermaid(tree)
    assert mer.count('"') % 2 == 0, "引号必须成对"
    assert "<尖括号>" not in mer and "`" not in mer
    print("  转义：引号/尖括号/反引号不会破坏 mermaid 语法")


def test_orientation_by_shape():
    """形状决定方向：步骤链竖排一列，并列主题横向铺开。"""
    chain = {"name": "T", "children": [
        {"name": "方法", "children": [{"name": "先做甲"}, {"name": "再做乙"}, {"name": "最后得丙"}]},
    ]}
    wide = {"name": "T", "children": [
        {"name": f"主题{i}", "children": [{"name": "要点甲"}, {"name": "要点乙"}]} for i in range(4)
    ]}
    chain2, has_chain = mm._chainify(chain)
    assert has_chain and not mm._chainify(wide)[1]
    nodes, vw, vh = mm._layout(chain2, vertical=True)
    assert len({n["x"] for n in nodes}) == 1, "步骤链的节点必须在同一列"
    assert len({n["y"] for n in nodes}) == len(nodes), "步骤链每步各占一行"
    nodes2, hw, hh = mm._layout(wide, vertical=False)
    assert len({n["x"] for n in nodes2}) >= 3, "并列主题要横向分层"
    print(f"  方向：步骤链 {vw:.0f}x{vh:.0f}（一列 {len(nodes)} 行）/ "
          f"并列主题 {hw:.0f}x{hh:.0f}（横向分层）")


if __name__ == "__main__":
    print("mindmap smoke:")
    test_default_tree_is_chapters_only()
    test_layout_no_overlap()
    test_html_self_contained()
    test_svg_escapes()
    test_mermaid_flowchart()
    test_steps_chain_in_one_column()
    test_parallel_points_not_chained()
    test_mermaid_label_safe()
    test_orientation_by_shape()
    print("all passed")
