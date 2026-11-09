"""拿已有笔记里的转写，只重跑结构化重组，快速验证 prompt 效果。

用法：python tests/retry_outline.py "<笔记目录路径>"

不用走完下载/转写/OCR，几十秒就能看出分层是否符合预期 ——
调 prompt 时每次重跑 5 分钟全流程是不可接受的。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.core.config import get_settings  # noqa: E402
from app.core.llm import LLMClient  # noqa: E402
from app.core.models import Chunk, Segment, VideoMeta  # noqa: E402
from app.core.secretstore import SecretStore  # noqa: E402
from app.services import chunking, summarize  # noqa: E402

NOTE = (
    Path(sys.argv[1]) / "notes.md"
    if len(sys.argv) > 1
    else Path("C:/Users/86159/Documents/BiliNotes/越南本地大学生带我玩越南！到底有啥不一样？/notes.md")
)


def load_segments() -> list[Segment]:
    text = NOTE.read_text(encoding="utf-8")
    body = text.split("## 完整转录", 1)[-1]
    segs: list[Segment] = []
    for line in body.splitlines():
        m = re.match(r"`(\d{1,2}:\d{2}(?::\d{2})?)`\s+(.*)", line.strip())
        if not m:
            continue
        parts = [int(x) for x in m.group(1).split(":")]
        ts = parts[0] * 60 + parts[1] if len(parts) == 2 else parts[0] * 3600 + parts[1] * 60 + parts[2]
        segs.append(Segment(start=float(ts), end=float(ts) + 3.0, text=m.group(2).strip(), source="asr"))
    return segs


def main() -> int:
    segments = load_segments()
    print(f"从笔记读到 {len(segments)} 句")

    settings = get_settings()
    store = SecretStore(settings.data_dir / "secrets.dat")
    client = LLMClient.from_settings(settings, store)
    if client is None:
        print("没有可用的模型 Key")
        return 1

    chunks = chunking.build_chunks(segments, target=settings.chunk_target_chars, max_chars=settings.chunk_max_chars)
    print(f"切成 {len(chunks)} 块，共 {sum(len(c.text) for c in chunks)} 字")

    meta = VideoMeta(
        bvid="BV1EsptzJE3y", cid=32443599982,
        title="越南本地大学生带我玩越南！到底有啥不一样？",
        uploader="超Carry的柴西", duration=1064.0, pubdate=0, cover="",
        webpage_url="https://www.bilibili.com/video/BV1EsptzJE3y",
    )

    outline, takeaways = summarize.build_outline(client, meta, chunks)

    print(f"\n=== 一级主题 {len(outline)} 个 ===")
    for i, topic in enumerate(outline):
        print(f"{i + 1}. {topic['title']}")
        for g in topic["groups"]:
            print(f"     [{g['title']}] {len(g['items'])} 条")

    print("\n=== 结论 ===")
    for t in takeaways:
        print("·", t)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
