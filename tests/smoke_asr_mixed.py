"""混说补救：外语原声 + 中文讲解的视频，中文部分要单独再认一遍。

真实故障：Dr.Dng陪跑 CSAPP p19（博主播英文课件 + 中文补充讲解）。
整段一个语种参数 → 课程英文占多数 → 整段按英语转 → 博主讲中文的那几秒
**根本产不出段落**（en 遍 avg_logprob = -9，等于「没这段」），笔记里
完全没有他的补充分析。

这条链路：
  _zh_scan          用中文语种扫全片，拿到每段的文本与置信度
  _pick_zh_windows  用置信度对比挑出「本来就是中文语音」的区间
  _merge_with_zh    把外语结果里对应的段换成中文结果
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "vendor"))

from app.core.config import Settings
from app.core.models import Segment
from app.services import transcribe as T

FAILED = 0


def ok(cond: bool, msg: str) -> None:
    global FAILED
    if cond:
        print(f"  OK   {msg}")
    else:
        FAILED += 1
        print(f"  FAIL {msg}")


def test_zh_count() -> None:
    print("\n--- 中文字数统计 ---")
    ok(T._zh_count("编译器帮我们做了大部分工作") == 13, "全中文计数正确")
    ok(T._zh_count("plain english only") == 0, "纯英文为 0")
    ok(T._zh_count("") == 0, "空串为 0")


def test_pick_zh_windows() -> None:
    print("\n--- 挑出「本来就是中文语音」的区间 ---")
    # 外语遍：00:00 英文（置信度正常）、博主讲中文的那段外语遍压根没出段
    first = [
        Segment(start=0.0, end=10.0, text="English course content here.", source="asr"),
    ]
    first_lp = [-0.3]
    # 中文扫描遍：
    zh_segs = [
        (0.0, 10.0, "英文硬转中文的胡言乱语内容", -0.80),   # 置信度比外语遍低 → 丢掉
        (30.0, 38.0, "然后内存到内存是不行的", -0.45),       # 外语遍在这里没段 → 保留
    ]
    picked = T._pick_zh_windows(zh_segs, first, first_lp)
    ok(len(picked) == 1, f"只挑出 1 段（实际 {len(picked)}）")
    ok(picked and abs(picked[0][0] - 30.0) < 0.01, "挑中的是中文那段的起始时间")
    ok(picked and "内存到内存" in picked[0][2], "保留了中文文本")


def test_merge_with_zh() -> None:
    print("\n--- 合并：中文区间替换外语区间 ---")
    first = [
        Segment(start=0.0, end=10.0, text="English part.", source="asr"),
        Segment(start=10.0, end=20.0, text="More English.", source="asr"),
        Segment(start=20.0, end=30.0, text="Tail English.", source="asr"),
    ]
    winners = [(10.0, 20.0, "这里是博主的补充讲解", -0.4)]
    merged = T._merge_with_zh(first, winners)
    texts = [s.text for s in merged]
    ok(len(merged) == 3, f"段数不变（实际 {len(merged)}）")
    ok("这里是博主的补充讲解" in texts, "中文段进来了")
    ok("More English." not in texts, "被覆盖的英文段换掉了")
    ok("English part." in texts and "Tail English." in texts, "没被覆盖的英文段留着")
    ok([s.start for s in merged] == sorted(s.start for s in merged), "按时间排好")


def test_switch() -> None:
    print("\n--- 开关 ---")
    st = Settings()
    ok(st.asr_mixed_language is True, "默认开启")
    st.asr_mixed_language = False
    ok(st.asr_mixed_language is False, "可以关掉")


def test_settings_api_and_ui() -> None:
    print("\n--- 接口与界面都接上了 ---")
    api = Path("app/api/routes.py").read_text(encoding="utf-8")
    ok('"asr_mixed_language": settings.asr_mixed_language' in api, "设置接口会返回它")
    ok("asr_mixed_language: bool | None = None" in api, "设置接口能接收它")
    html = Path("web/index.html").read_text(encoding="utf-8")
    ok('id="setAsrMixed"' in html, "设置页有开关")
    js = Path("web/app.js").read_text(encoding="utf-8")
    ok("$('setAsrMixed').checked" in js, "前端回填了勾选状态")
    ok("asr_mixed_language: $('setAsrMixed').checked" in js, "前端提交了开关值")


if __name__ == "__main__":
    test_zh_count()
    test_pick_zh_windows()
    test_merge_with_zh()
    test_switch()
    test_settings_api_and_ui()
    print()
    if FAILED:
        print(f"失败 {FAILED} 项")
        sys.exit(1)
    print("混说补救：全部通过")
