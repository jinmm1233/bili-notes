"""离线学习包回归：导出的 HTML 必须真的自包含（数据/图片/公式都在里面）。

不联网；用临时目录造一篇带图的笔记。
  python tests/smoke_offline.py
"""

from __future__ import annotations

import json
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "vendor"))

from PIL import Image  # noqa: E402

from app.core.config import Settings  # noqa: E402
from app.services import cards as C  # noqa: E402
from app.services import offline_pack as P  # noqa: E402
from app.services import quiz as Q  # noqa: E402

FAILED = 0

NOTE = """---
title: "线代 22 初等行变换"
uploader: "测试UP"
collection: "线代"
---

# 线代 22 初等行变换

## 一句话总结

用行变换把 $[A\\mid E]$ 化成 $[E\\mid A^{-1}]$。

## 关键概念

| 术语 | 解释 |
|---|---|
| 初等行变换法 | 对换、倍乘、倍加 |

![幻灯片](assets/frame_01.jpg)

- 第一行加到第二行
- 第二行乘 2
"""


def ok(cond: bool, msg: str) -> None:
    global FAILED
    print(("[OK] " if cond else "[FAIL] ") + msg)
    if not cond:
        FAILED += 1


def build(tmp: Path) -> Settings:
    out = tmp / "out"
    note_dir = out / "第01集"
    (note_dir / "assets").mkdir(parents=True)
    (note_dir / "notes.md").write_text(NOTE, encoding="utf-8", newline="\n")
    Image.new("RGB", (1280, 720), (240, 240, 245)).save(note_dir / "assets" / "frame_01.jpg")

    settings = Settings(data_dir=tmp / "data", output_dir=out)
    (tmp / "data").mkdir(exist_ok=True)
    C.save(settings, [{
        "id": "c1", "note_id": "第01集", "note_title": "线代 22",
        "front": "把左半化成什么？", "back": "单位矩阵", "concept": "初等行变换法",
        "kind": "概念", "due": 0, "ease": 2.5, "interval": 0, "reps": 0, "lapses": 0, "last": 0,
    }])
    quiz = {
        "id": "qz1", "title": "线代自测", "kinds": ["single"], "created": 1,
        "note_id": "quiz:qz1", "sources": [{"id": "第01集", "title": "线代 22"}],
        "questions": [{
            "id": "q1", "kind": "single", "stem": "左半化成什么？",
            "options": ["零矩阵", "单位矩阵", "转置"], "answer": [1],
            "explain": "这样右半才是逆矩阵", "concept": "初等行变换法",
        }],
        "attempts": [],
    }
    Q.save(settings, [quiz])
    return settings


def test_export(tmp: Path) -> None:
    settings = build(tmp)
    bundle = P.bundle(settings)
    ok(bundle["counts"] == {"notes": 1, "cards": 1, "quizzes": 1}, f"包里有三类数据（{bundle['counts']}）")
    ok("初等行变换" in bundle["notes"][0]["markdown"], "笔记正文读到了（scan 只给元信息）")

    files = P._collect_images(settings, bundle["notes"])
    ok(len(files) == 1 and files["第01集/assets/frame_01.jpg"].startswith("data:image/jpeg"),
       f"截图被压成 data-uri 内联（{len(files)} 张）")

    html = P.build_html(settings)
    ok("BiliNotes 学习包" in html.split("</title>")[0], "标题带上导出日期")
    ok("window.__BN_OFFLINE__" in html, "数据内联进了页面")

    # 真正把内联的 JSON 取出来，确认结构可解析、答案是齐的
    m = re.search(r"window\.__BN_OFFLINE__=(\{.*?\});</script>", html, re.S)
    ok(bool(m), "内联数据是一段合法脚本")
    if m:
        data = json.loads(m.group(1))
        ok(len(data["bundle"]["cards"]) == 1, "卡片在包里")
        ok(data["bundle"]["quizzes"][0]["questions"][0]["answer"] == [1],
           "自测卷答案在包里（离线判分要用）")
        ok(data["bundle"]["quizzes"][0]["questions"][0]["answer_text"] == "单位矩阵",
           "答案文本也预先算好")

    ok("data:font/woff2" in html, "KaTeX 字体内联，断网也有公式字形")
    ok("katex.min.css" not in html and "katex.min.js" not in html, "不再引用外部 KaTeX 文件")
    ok('rel="manifest"' not in html and "apple-touch-icon" not in html, "去掉 PWA 引用（file:// 下会 404）")
    ok("/sw.js" not in html, "单文件不再注册 Service Worker")
    ok("data:image/svg+xml;base64" in html, "图标也内联了")
    ok(len(html.encode("utf-8")) > 200_000, f"包体正常（{len(html) / 1024:.0f} KB）")


def test_progress(tmp: Path) -> None:
    """离线进度合并：只覆盖排期字段，不碰卡片内容，也不复活已删的卡。"""
    settings = build(tmp)
    from app.api import routes_offline as R  # noqa: PLC0415
    from app.core import config as cfg  # noqa: PLC0415

    cfg._settings = settings          # 接口内部用 get_settings()，测试要指到临时目录

    body = R.ProgressIn(
        cards={"c1": {"due": 123.0, "ease": 2.7, "interval": 6.0, "reps": 3,
                      "lapses": 1, "last": 100.0, "front": "被改过的题面"}},
        quizzes=[{"quiz_id": "qz1", "at": 999.0, "score": 1, "total": 1, "wrong": []},
                 {"quiz_id": "qz1", "at": 999.0, "score": 1, "total": 1, "wrong": []},
                 {"quiz_id": "不存在", "at": 1.0, "score": 0, "total": 0}],
    )
    if hasattr(R, "offline_progress"):
        result = R.offline_progress(body)
    else:                                            # 函数被包成路由对象时兜底
        result = None
    cards = C.load(settings)
    card = cards[0]
    ok(card["reps"] == 3 and card["interval"] == 6.0, "复习进度并回来了")
    ok(card["front"] == "把左半化成什么？", "卡片内容没被离线端改掉")
    quiz = Q.load(settings)[0]
    ok(len(quiz["attempts"]) == 1, f"成绩只记一次，重复同步不重复记（{len(quiz['attempts'])}）")
    if result is not None:
        ok(result["cards"] == 1 and result["quizzes"] == 1, f"返回合并计数（{result}）")
    cfg._settings = None


def main() -> int:
    print("=== 导出内容 ===")
    with tempfile.TemporaryDirectory() as raw:
        test_export(Path(raw))
    print("\n=== 进度合并 ===")
    with tempfile.TemporaryDirectory() as raw:
        test_progress(Path(raw))
    print()
    if FAILED:
        print(f"{FAILED} 项未通过")
        return 1
    print("离线学习包全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
