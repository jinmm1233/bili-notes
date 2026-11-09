"""给已有笔记补/更新「生词」段，不重跑视频。

只处理外语笔记，且必须已经导入过词库；没有词库时直接退出（不改任何文件）。
原文件备份成 notes.md.bak-words，重复跑是幂等的。

用法：
    python tools/rebuild_words.py            # 全部笔记
    python tools/rebuild_words.py --dry      # 只看会改哪些，不落盘
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "vendor"))

from app.core.config import Settings  # noqa: E402
from app.services import words as W  # noqa: E402
from app.services import wordbook as B  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry", action="store_true", help="只报告，不写文件")
    args = parser.parse_args()

    settings = Settings()
    root = Path(settings.output_dir)
    print(f"笔记目录：{root}")

    index = B.index(settings)
    if not index:
        print("还没有导入任何词库：设置 → 英语词库。没有词库时不改笔记。")
        return 1
    books = [b["name"] for b in B.load_all(settings)]
    print(f"词库：{'、'.join(books)}（合并 {len(index)} 词）")

    changed = skipped = 0
    for folder in sorted(root.iterdir()):
        md_path = folder / "notes.md"
        if not md_path.is_file():
            continue
        md = md_path.read_text(encoding="utf-8")
        if not W.note_is_foreign(md):
            skipped += 1
            continue
        new_md, stats = W.annotate(md, index)
        if not stats.get("hits"):
            skipped += 1
            print(f"  跳过 {folder.name[:36]}（词库里的词没出现）")
            continue
        if new_md == md:
            print(f"  不变 {folder.name[:36]}（{stats['hits']} 词）")
            continue
        changed += 1
        print(f"  更新 {folder.name[:36]}：{stats['hits']} 词（{'、'.join(stats['books'])}）")
        if args.dry:
            continue
        bak = md_path.with_suffix(".md.bak-words")
        if not bak.is_file():                    # 只备份第一次，别把原稿冲掉
            bak.write_text(md, encoding="utf-8", newline="\n")
        md_path.write_text(new_md, encoding="utf-8", newline="\n")
        (folder / "words.json").write_text(
            json.dumps({
                "generated_at": time.time(),
                "books": stats["books"],
                "words": stats["words"],
            }, ensure_ascii=False, indent=1),
            encoding="utf-8", newline="\n",
        )

    tail = "（未写入）" if args.dry else ""
    print(f"\n更新 {changed} 篇，跳过 {skipped} 篇{tail}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
