"""数学写法归一化冒烟：模型爱写的"伪公式"要能转成 LaTeX，且别误伤正文。

背景：看图模型能把 PPT 上的矩阵读得一个不差，但爱写成
[[3,2,1],[3,1,5]]、A^-1、[A:E] 这种文本，笔记里就是一串字符。
"""
import sys

sys.path.insert(0, ".")

from app.services import mathfmt as mf


def test_list_matrix():
    out = mf.normalize("设矩阵A = [[3,2,1],[3,1,5],[3,4,-3]]，求A的逆矩阵")
    assert r"\begin{bmatrix}" in out and out.count("\\\\") == 2, out
    assert "3 & 2 & 1" in out
    assert out.startswith("设矩阵A = $$") and out.endswith("，求A的逆矩阵")
    print("[OK] [[a,b],[c,d]] → bmatrix")


def test_fraction_matrix():
    out = mf.normalize("逆矩阵为[[23/12, -5/6], [-2, 1/2]]")
    assert "23/12 & -5/6" in out and "-2 & 1/2" in out
    print("[OK] 分数元素也转")


def test_semi_matrix():
    out = mf.normalize("矩阵 [1 2 3; 4 5 6]")
    assert r"\begin{bmatrix} 1 & 2 & 3 \\ 4 & 5 & 6 \end{bmatrix}" in out
    print("[OK] 分号写法 → bmatrix")


def test_caret_and_unicode():
    assert mf.normalize("A^-1 与 x^2") == "$A^{-1}$ 与 $x^{2}$"
    assert "$A^{-1}$" in mf.normalize("逆矩阵A⁻¹")
    print("[OK] A^-1 / A⁻¹ → 上标")


def test_augmented():
    out = mf.normalize("将[A:E]化为[E:A^-1]")
    assert out == r"将$[A \mid E]$化为$[E \mid A^{-1}]$", out
    assert "$" not in out.replace("$[", "").replace("]$", ""), "不能出现 $ 套 $"
    print("[OK] [A:E] / [E:A^-1] → 增广矩阵且不嵌套 $")


def test_augmented_with_dollars():
    """模型自己写了 $ 的情况：[E:$A^{-1}$] 不能变成 $$...$$"""
    out = mf.normalize("化为[E:$A^{-1}$]来")
    assert out == r"化为$[E \mid A^{-1}]$来", out
    assert "$$A" not in out, out
    print("[OK] 模型自带的 $ 会剥掉，不形成 $$ 套 $$")


def test_existing_math_untouched():
    src = r"已知 $$\begin{bmatrix} 1 & 2 \\ 3 & 4 \end{bmatrix}$$"
    assert mf.normalize(src) == src
    print("[OK] 已是 LaTeX 的不重复加工")


def test_no_false_positive():
    for text in ("价格 [[不太可能]] 别转", "[提示: 这里不是矩阵]",
                 "两个矩阵相乘次数 = 3 x 4 x 2 = 24 次", "第 [1,2] 步"):
        assert mf.normalize(text) == text, (text, mf.normalize(text))
    print("[OK] 正文/中文/单行列表不误伤")


def test_mindmap_downgrade():
    src = mf.normalize("设矩阵A = [[3,2,1],[3,1,5]]，求 $A^{-1}$")
    mm = mf.for_mindmap(src)
    assert "bmatrix" not in mm and "\\" not in mm, mm
    assert "矩阵/公式见正文" in mm and "A⁻¹" in mm, mm
    print("[OK] 导图不出现 LaTeX，公式降级为提示")


def test_empty():
    assert mf.normalize("") == "" and mf.for_mindmap("") == ""
    assert mf.normalize(None) is None or mf.normalize(None) == ""
    print("[OK] 空值安全")


if __name__ == "__main__":
    test_list_matrix()
    test_fraction_matrix()
    test_semi_matrix()
    test_caret_and_unicode()
    test_augmented()
    test_augmented_with_dollars()
    test_existing_math_untouched()
    test_no_false_positive()
    test_mindmap_downgrade()
    test_empty()
    print("\nALL PASS")
