"""自动标重点冒烟：验证 ==重点== 的成对性兜底与 prompt 指令存在。"""
import sys

sys.path.insert(0, ".")

from app.services import summarize


def test_balance_even_kept():
    assert summarize._balance_marks("缓存 ==LRU== 命中率最高") == "缓存 ==LRU== 命中率最高"
    print("[OK] 成对标记保留")


def test_balance_odd_stripped():
    assert summarize._balance_marks("这里有一个 ==落单 标记") == "这里有一个 落单 标记"
    print("[OK] 落单标记被清除，不会露出裸符号")


def test_balance_empty():
    assert summarize._balance_marks("") == ""
    print("[OK] 空串安全")


def test_prompt_has_highlight_rule():
    assert "==双等号==" in summarize._OUTLINE_TMPL
    assert "黄色荧光笔" in summarize._OUTLINE_TMPL
    assert "==" in summarize._GLOBAL_TMPL
    print("[OK] 生成 prompt 已含标重点指令")


if __name__ == "__main__":
    test_balance_even_kept()
    test_balance_odd_stripped()
    test_balance_empty()
    test_prompt_has_highlight_rule()
    print("\nALL PASS")
