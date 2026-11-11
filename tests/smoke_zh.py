"""繁简统一：笔记里不出现繁体。

来源有三处：
- 本地语音转写遇到中文语音常输出繁体（「邊緣系幫我們…」「後面用動詞圓形」）
- B 站 CC / AI 字幕本身可能是繁体
- 视觉模型照抄画面上的繁体字
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "vendor"))

from app.core import zh

FAILED = 0


def ok(cond: bool, msg: str) -> None:
    global FAILED
    if cond:
        print(f"  OK   {msg}")
    else:
        FAILED += 1
        print(f"  FAIL {msg}")


def test_convert() -> None:
    print("\n--- 繁转简 ---")
    ok(zh.to_simplified("邊緣系幫我們做了大部分工作") == "边缘系帮我们做了大部分工作",
       "繁体转简体正确")
    ok(zh.to_simplified("Happen to是偶然碰巧,後面用動詞圓形")
       == "Happen to是偶然碰巧,后面用动词圆形", "中英混排也转")
    ok(zh.to_simplified("視頻軟體裡的設置很複雜") == "视频软件里的设置很复杂",
       "台湾用语词也转（軟體→软件）")
    ok(zh.to_simplified("這是简体中文") == "这是简体中文", "简体原样返回")
    ok(zh.to_simplified("plain english only") == "plain english only", "纯英文不动")
    ok(zh.to_simplified("") == "", "空串安全")


def test_no_traditional_leftover() -> None:
    print("\n--- 输出里不该有繁体 ---")
    text = zh.to_simplified("Compiler,邊緣系幫我們做了大部分工作。因為會邊緣,它是極簡的緣。")
    bad = [ch for ch in text if ch in "邊緣幫們為會極簡緣"]
    ok(not bad, f"没有残留繁体（残留 {bad}）")


def test_integration() -> None:
    print("\n--- 各处都接上了 ---")
    src = Path("app/services/subtitle.py").read_text(encoding="utf-8")
    ok("zh.to_simplified" in src, "字幕转简体")
    tr = Path("app/services/transcribe.py").read_text(encoding="utf-8")
    ok("zh.to_simplified" in tr, "语音转写转简体")
    fc = Path("app/services/frame_caption.py").read_text(encoding="utf-8")
    ok("zh.to_simplified" in fc, "画面要点转简体")
    tp = Path("app/services/transcript_polish.py").read_text(encoding="utf-8")
    ok("zh.to_simplified" in tp, "译文转简体")
    ex = Path("app/services/exporter.py").read_text(encoding="utf-8")
    ok("zh.to_simplified(body)" in ex, "导出时整篇兜底")
    spec = Path("build.spec").read_text(encoding="utf-8")
    ok('"zhconv"' in spec, "打包清单里有 zhconv（否则打包后失效）")


if __name__ == "__main__":
    test_convert()
    test_no_traditional_leftover()
    test_integration()
    print()
    if FAILED:
        print(f"失败 {FAILED} 项")
        sys.exit(1)
    print("繁简统一：全部通过")
