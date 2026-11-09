"""核心算法冒烟：分块、字幕解析、导图、Markdown 渲染（不依赖网络）。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.models import Chapter, Chunk, Frame, Segment, VideoMeta  # noqa: E402
from app.services import chunking, exporter, mindmap, subtitle  # noqa: E402

FAILS: list[str] = []


def check(cond: bool, label: str) -> None:
    print(("  ok  " if cond else "  FAIL") + f"  {label}")
    if not cond:
        FAILS.append(label)


def build_segments(count: int = 320) -> list[Segment]:
    """造一段像真人讲课的字幕：长短句混合、有停顿、有转折词。"""
    topics = [
        "我们先来看第一个概念，它的核心是把复杂问题拆成可以独立处理的小块",
        "这个设计的好处是每一块都能单独验证，出了问题也容易定位",
        "接下来看第二个要点，缓存的失效策略决定了数据一致性的上限",
        "这里有一个常见的误区，很多人以为加机器就能解决所有性能问题",
        "实际上瓶颈往往在数据库的写入路径上，而不是应用服务器",
        "总结一下，这一节我们讲了三个约束条件和它们之间的取舍关系",
        "我们来看这张图，横向是时间轴，纵向是各个环节的耗时分布",
        "公式里的这个系数代表重试带来的额外开销，通常取零点三比较合适",
    ]
    segs: list[Segment] = []
    t = 0.0
    for i in range(count):
        text = topics[i % len(topics)] + f"，这是第{i}段讲解内容，用来撑长度。"
        dur = 3.0 + (i % 5) * 0.8
        segs.append(Segment(start=t, end=t + dur, text=text, source="cc"))
        t += dur + (1.4 if i % 7 == 0 else 0.3)
    return segs


def main() -> int:
    segs = build_segments()
    duration = segs[-1].end

    print("\n[1] 分块")
    chunks = chunking.build_chunks(segs, target=800, max_chars=1200)
    check(len(chunks) >= 5, f"块数合理（{len(chunks)} 块）")
    check(all(c.char_count <= 1400 for c in chunks), "无超长块")
    check(all(c.end > c.start for c in chunks), "时间区间有效")
    covered = chunking.coverage_ratio(chunks, duration)
    check(covered > 0.95, f"时间覆盖率 {covered:.1%}")
    total_len = sum(c.char_count for c in chunks)
    check(total_len >= sum(len(s.text) for s in segs) * 0.98, "文本无丢失")
    contiguous = all(chunks[i].end <= chunks[i + 1].start + 0.01 for i in range(len(chunks) - 1))
    check(contiguous, "块之间无重叠")
    avg = total_len / len(chunks)
    print(f"       平均块长 {avg:.0f} 字，最长 {max(c.char_count for c in chunks)} 字")

    print("\n[2] 字幕解析")
    srt = "1\n00:00:01,000 --> 00:00:03,500\n第一句\n\n2\n00:00:04,000 --> 00:00:06,000\n第二句\n"
    parsed = subtitle.parse_srt(srt)
    check(len(parsed) == 2, "SRT 解析出 2 条")
    check(abs(parsed[0].start - 1.0) < 1e-6, "SRT 时间戳正确")
    check("00:00:01,000 --> 00:00:03,500" in subtitle.to_srt(parsed), "SRT 往返一致")

    fragments = [
        Segment(0, 1.0, "那么我们今天", "ai"),
        Segment(1.1, 2.0, "来讲一下缓存", "ai"),
        Segment(2.1, 3.0, "的失效策略。", "ai"),
    ]
    merged = subtitle.normalize(fragments)
    check(len(merged) == 1, f"碎片句合并（{len(merged)} 条）")
    check(merged[0].text == "那么我们今天来讲一下缓存的失效策略。", f"合并文本：{merged[0].text}")

    print("\n[3] 思维导图")
    chapters = [
        Chapter(0, "问题拆解", chunks[0].start, chunks[0].end,
                [chunks[0].index], "讲了拆块思路", ["每块可独立验证", "问题定位更快"]),
        Chapter(1, "缓存与一致性", chunks[1].start, chunks[1].end,
                [chunks[1].index], "讲了失效策略", ["失效策略决定一致性上限"]),
    ]
    meta = VideoMeta("BV1xx411c7mD", 123, "系统设计的三个取舍", "某UP主", duration, 1700000000,
                     "", "https://www.bilibili.com/video/BV1xx411c7mD")
    tree = mindmap.build_tree(meta, chapters)
    mermaid = mindmap.to_mermaid(tree)
    check(mermaid.startswith("flowchart"), "知识地图是 mermaid flowchart（步骤链 TD / 并列 LR）")
    check('-->' in mermaid, "mermaid 有连线")
    html = mindmap.to_markmap_html(tree, meta.title)
    check("markmap-autoloader" not in html and html.count("<rect") >= 2, "知识地图 HTML 零依赖（静态 SVG）")

    print("\n[4] Markdown 渲染")
    fake_frames = [
        Frame(ts=125.0, path="/tmp/a.jpg", ocr_text="QPS = 1200\n缓存命中率 87%", score=0.8),
    ]
    chunks[0].frames = fake_frames
    note = type("N", (), {})()
    from app.core.models import VideoNote  # noqa: PLC0415
    note = VideoNote(
        meta=meta, segments=segs, chunks=chunks, chapters=chapters,
        one_liner="讲了系统设计中的三个关键取舍。",
        highlights=["拆块让问题可独立验证", "缓存失效策略决定一致性上限"],
        glossary=[{"term": "缓存命中率", "explain": "请求在缓存中直接命中的比例"}],
        transcript_source="cc",
    )
    md = exporter.render_markdown(note, tree, {id(fake_frames[0]): "assets/frame_02-05_001.jpg"})
    check(md.startswith("---"), "有 YAML frontmatter")
    check("# 系统设计的三个取舍" in md, "有标题")
    check("## 一句话总结" in md and "## 核心要点" in md, "有总结段")
    # 正文不再放思维导图（长句当节点，手机上看不全），换成可读的结构列表；
    # 图只留在同目录 mindmap.html 里
    check("```mermaid" not in md, "正文不嵌思维导图")
    check("## 内容结构" in md or "## 要点速览" in md, "有内容结构列表")
    check("assets/frame_02-05_001.jpg" in md, "图片引用正确")
    check("画面文字" in md, "OCR 文字入正文")
    check("<details>" in md, "转录折叠")
    check("t=125" in md, "时间戳跳转链接")

    out = Path(__file__).parent / "_sample_output.md"
    out.write_text(md, encoding="utf-8")
    print(f"\n样张已写入 {out}（{len(md)} 字符）")

    print("\n" + ("=" * 40))
    if FAILS:
        print(f"失败 {len(FAILS)} 项：{FAILS}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
