"""命令行入口：不经 UI 直接跑完整流水线，用于调试与批量处理。

用法：
    python -m app.cli BV1xx411c7mD
    python -m app.cli https://www.bilibili.com/video/BV1xx411c7mD?p=2 --no-frames
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

if __package__ in (None, ""):  # 支持 python app/cli.py 直接执行
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import get_settings  # noqa: E402
from app.core.errors import AppError  # noqa: E402
from app.core.logging import setup_logging  # noqa: E402
from app.services.pipeline import Pipeline, PipelineOptions  # noqa: E402

log = logging.getLogger("cli")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bilinotes", description="B站视频 → 结构化 Markdown 笔记")
    parser.add_argument("url", help="视频链接或 BV 号")
    parser.add_argument("--no-frames", action="store_true", help="跳过抽帧与 OCR")
    parser.add_argument("--no-ocr", action="store_true", help="抽帧但不识别文字")
    parser.add_argument("--no-summary", action="store_true", help="跳过模型总结（只出转写+画面）")
    parser.add_argument("--provider", default=None, help="临时指定模型供应商（deepseek/zhipu/qwen/...）")
    parser.add_argument("--height", type=int, default=480, help="抽帧用的视频清晰度上限")
    parser.add_argument("--verbose", action="store_true", help="输出调试日志")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = get_settings()
    setup_logging("DEBUG" if args.verbose else "INFO")

    options = PipelineOptions(
        do_frames=not args.no_frames,
        do_ocr=not args.no_ocr,
        do_summary=not args.no_summary,
        max_height=args.height,
        provider=args.provider,
    )

    last_stage = {"name": ""}

    def emit(stage: str, pct: float, msg: str) -> None:
        if stage != last_stage["name"]:
            last_stage["name"] = stage
            print(f"\n── {stage} ──")
        print(f"  [{pct:5.1f}%] {msg}")

    pipeline = Pipeline(settings, emit=emit)
    try:
        result = pipeline.run(args.url, options=options)
    except AppError as exc:
        print(f"\n✗ {exc.code}: {exc.message}", file=sys.stderr)
        if exc.detail:
            print(f"  详情: {exc.detail}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n已中断", file=sys.stderr)
        return 130

    print(f"\n✓ 笔记已生成：{result.md_path}")
    print(f"  目录：{result.md_path.parent}")
    print(f"  用时：{result.elapsed:.0f}s · 章节 {len(result.note.chapters)} · 知识块 {len(result.note.chunks)}")
    for warn in result.warnings:
        print(f"  ⚠ {warn}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
