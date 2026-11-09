"""字幕校验测试：音画互校能否抓住真实同音误写，且不误伤正常词。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.models import Segment  # noqa: E402
from app.services import text_check as tc  # noqa: E402

FAILS: list[str] = []


def check(cond: bool, label: str) -> None:
    print(("  ok  " if cond else "  FAIL") + f"  {label}")
    if not cond:
        FAILS.append(label)


# 模拟画面 OCR 抓到的 PPT 文字（权威参照）
OCR_TRUSTED = """
卷积神经网络 梯度下降 Transformer 反向传播 注意力机制
学习率 过拟合 正则化 损失函数 批量归一化
"""

def seg(text: str, start: float = 0.0) -> Segment:
    return Segment(start=start, end=start + 4, text=text, source="ai")


def main() -> int:
    print("[1] 音画互校：同音误写")
    cases = [
        ("我们来看卷机神经网络的结构", "卷积神经网络"),
        ("这里用剃度下降来优化", "梯度下降"),
        ("增加正泽化可以缓解过你合", "正则化"),   # 正泽→正则、过你→过拟，均为同音
    ]
    for raw, expected in cases:
        segs = [seg(raw)]
        report = tc.verify(segs, OCR_TRUSTED, source="ocr")
        fixed = tc.apply_fixes(segs, report)
        check(expected in fixed[0].text, f"「{raw}」→「{fixed[0].text}」")
        check(report.count >= 1, f"  记录了 {report.count} 处修正")

    print("\n[2] 不应误伤")
    safe_cases = [
        "这个模型的效果非常好",       # 通用词
        "我们今天讲三个重点",         # 通用词
        "卷积神经网络很重要",         # 本来就正确
    ]
    for raw in safe_cases:
        segs = [seg(raw)]
        report = tc.verify(segs, OCR_TRUSTED, source="ocr")
        fixed = tc.apply_fixes(segs, report)
        check(fixed[0].text == raw, f"「{raw}」保持不变")

    print("\n[3] 重复与标点")
    segs = [seg("这个这个这个模型很好，，我们要注意。。")]
    report = tc.verify(segs, OCR_TRUSTED, source="ocr")
    fixed = tc.apply_fixes(segs, report)
    check("这个这个" not in fixed[0].text, f"重复已去除：{fixed[0].text}")
    check("，，" not in tc.normalize_text(segs[0].text), "连续标点已合并")

    print("\n[4] 英文术语大小写")
    segs = [seg("我们看 transformer 的注意力机制"), seg("attention 是关键")]
    out, fixes = tc.unify_ascii_case(segs, OCR_TRUSTED)
    check("Transformer" in out[0].text, f"大小写已统一：{out[0].text}")
    check(len(fixes) == 1, f"记录 {len(fixes)} 处")

    print("\n[5] 句柄接口与块重拼")
    segs = [seg("卷机神经网络", 0), seg("剃度下降", 5)]
    report = tc.verify(segs, OCR_TRUSTED, source="ocr")
    fixed = tc.apply_fixes(segs, report)
    check(fixed[0].text == "卷积神经网络" and fixed[1].text == "梯度下降", "两句均修正")

    class C:
        def __init__(self, s, e):
            self.start, self.end, self.text = s, e, ""

    chunks = [C(0, 4.5), C(5, 9)]
    tc.rebuild_chunk_text(chunks, fixed)
    check(chunks[0].text == "卷积神经网络", f"块文本重拼：{chunks[0].text}")

    print("\n[6] 空数据")
    empty = tc.verify([], OCR_TRUSTED)
    check(empty.count == 0, "空字幕不报错")
    no_trust = tc.verify([seg("随便一句话")], "")
    check(no_trust.count == 0 and no_trust.skipped_reason, "无可信文本时跳过")

    print("\n[7] 拼音判据符合预期")
    check(tc._py("卷机") == tc._py("卷积"), "卷机 == 卷积（同音）")
    check(tc._py("剃度") == tc._py("梯度"), "剃度 == 梯度（同音）")
    check(tc._py("卷机") != tc._py("卷积核")[:4], "长度不同不匹配")

    print("\n" + "=" * 40)
    if FAILS:
        print(f"失败 {len(FAILS)} 项：{FAILS}")
        return 1
    print("字幕校验全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
