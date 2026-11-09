"""用假 LLM 验证 build_outline 的解析与渲染（不花钱、不联网）。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.models import Chunk, Frame, VideoMeta, VideoNote  # noqa: E402
from app.services import exporter, summarize  # noqa: E402

FAKE = {
    "structure": [
        {
            "title": "河内",
            "summary": "行程第一站，主要看大学、文庙和火车街，吃的偏酸辣。",
            "cover_ts": "03:10",
            "groups": [
                {"title": "玩的", "cover_ts": "04:30", "items": [
                    "文庙：越南第一所大学，考生考前会来祈福，进士石碑上刻的是汉字",
                    "火车街：窄巷改造，火车从房前穿堂而过，两侧挤满咖啡店",
                ]},
                {"title": "吃的", "cover_ts": "", "items": ["烤肉米线：一份 18 个，约 24 元"]},
            ],
        },
        {
            "title": "岘港",
            "summary": "第二站海滨城市，主打巴拿山乐园。",
            "cover_ts": "12:40",
            "groups": [{"title": "玩的", "cover_ts": "", "items": ["巴拿山：佛手桥、法国村、室内游乐场，门票已含多数项目"]}],
        },
        {"title": "", "summary": "空标题应被丢弃", "groups": [{"title": "x", "items": ["y"]}]},
    ],
    "takeaways": ["越南大学生中文普及度比预期高", "摩托车是城市最主要交通方式"],
    "problems": [
        {
            "title": "矩阵连乘",
            "statement": "给定 4 个矩阵 $A_1..A_4$，求最少乘法次数。",
            "key": "==把连乘顺序当成区间划分==，用区间 DP 从小到大枚举。",
            "steps": ["先算长度 2 的区间", "再递推到长度 4", "取最小值"],
            "pitfall": "区间长度从 2 开始，别从 1 开始",
            "ts": "12:30",
        },
        {"title": "空题干应被丢弃", "statement": "", "key": "x", "steps": []},
    ],
}


class FakeClient:
    def complete_json(self, *_a, **_kw):
        return FAKE


def main() -> int:
    meta = VideoMeta(
        bvid="BV1", cid=1, title="越南", uploader="超Carry", duration=1064,
        pubdate=0, cover="", webpage_url="http://x",
    )
    chunks = [Chunk(0, 0.0, 300.0, "越南河内的文庙是第一所大学"), Chunk(1, 300.0, 600.0, "巴拿山佛手桥")]

    outline, takeaways, problems = summarize.build_outline(FakeClient(), meta, chunks)
    print(f"[1] 解析出 {len(outline)} 个主题（空标题已丢弃）")
    assert len(outline) == 2, outline
    assert outline[0]["title"] == "河内"
    assert len(outline[0]["groups"]) == 2
    print("    主题：", [t["title"] for t in outline])
    print("    河内子主题：", [g["title"] for g in outline[0]["groups"]])

    assert len(problems) == 1, problems
    assert problems[0].title == "矩阵连乘"
    assert problems[0].ts == 750.0, problems[0].ts
    assert problems[0].steps and len(problems[0].steps) == 3
    print("    例题：", problems[0].title, "| 关键：", problems[0].key[:20])

    note = VideoNote(meta=meta, transcript_source="ai")
    note.outline = outline
    note.takeaways = takeaways
    note.problems = problems
    text = "\n".join(exporter._render_outline(note, {}))
    print("\n[2] 渲染结果：")
    print(text)

    assert "### 一、河内" in text and "#### 玩的" in text
    assert "### 二、岘港" in text
    assert "## 结论" in text
    assert "空标题" not in text
    assert "空题干应被丢弃" not in text

    # 例题板块：题干、题眼（高亮）、有序步骤、易错
    body = "\n".join(exporter._render_problems(note))
    assert "## 例题精解" in body and "### 例题 1：矩阵连乘" in body, body
    assert "> **题目**" in body and "> **解题关键**：" in body
    assert "==把连乘顺序当成区间划分==" in body
    assert "1. 先算长度 2 的区间" in body and "3. 取最小值" in body
    assert "> **易错**：" in body
    print("\n[2.5] 例题板块：")
    print(body)
    print("[3] 断言全部通过")

    # 无大纲时不应渲染"## 笔记"
    empty = VideoNote(meta=meta)
    assert exporter._render_outline(empty, {}) == ["## 笔记", ""]
    assert exporter._render_problems(empty) == []
    print("[4] 空大纲不会产生假内容")

    # ---- 配图：一级/二级各配一张，模型留空的段不配，且不重复用同一张 ----
    print("\n[5] 配图逻辑")
    assert outline[0]["cover_ts"] == 190.0, outline[0]["cover_ts"]   # 03:10
    assert outline[0]["groups"][0]["cover_ts"] == 270.0               # 04:30
    assert outline[0]["groups"][1]["cover_ts"] == 0.0                 # 留空 → 不配图
    print("    时间点解析：", [t["cover_ts"] for t in outline], [g["cover_ts"] for t in outline for g in t["groups"]])

    for i, ts in enumerate([10.0, 195.0, 275.0, 700.0]):
        note.chunks.append(
            Chunk(index=i, start=ts, end=ts + 60, text=f"内容{i}",
                  frames=[Frame(ts=ts, path=f"/tmp/of{i}.jpg", score=0.5)])
        )
    imap = {id(f): f"assets/of{i}.jpg" for i, ch in enumerate(note.chunks) for f in ch.frames}
    body = "\n".join(exporter._render_outline(note, imap))
    # 河内(190→195) 玩的(270→275) 岘港(760→700) 三个段有图；"吃的"没有
    used = [line for line in body.splitlines() if line.startswith("![")]
    print("    生成的图片行：", used)
    assert len(used) == 3, used
    assert len(set(used)) == 3, "同一张图不应被两段重复使用"
    assert body.index("assets/of1.jpg") < body.index("#### 玩的")
    print("[6] 每段一张、缺内容就跳过、不重复 — 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
