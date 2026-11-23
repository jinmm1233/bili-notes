"""公式判定（isMath）回归测试（`python tests/smoke_math.py`）。

直接从 web/app.js 里抽出真实的 isMath 函数交给 node 执行，不复制一份逻辑，
避免"测试通过、线上还是旧的"。

守两件事：
  1. 公式里夹中文但带 LaTeX 命令（矩阵箭头上的「初等行变换」、\\text{元}）→ 认作公式；
  2. 纯文字/价格（"50 元"）没有反斜杠 → 仍然按普通文字，不被误渲染成公式。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_JS = ROOT / "web" / "app.js"

FAILED = 0


def ok(cond: bool, msg: str) -> None:
    global FAILED
    print(("[OK] " if cond else "[FAIL] ") + msg)
    if not cond:
        FAILED += 1


def extract_function(source: str, name: str) -> str:
    """按花括号配平，抽出 function name(...) {...} 整段。"""
    start = source.index(f"function {name}(")
    i = source.index("{", start)
    depth = 0
    in_str: str | None = None
    esc = False
    for pos in range(i, len(source)):
        ch = source[pos]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == in_str:
                in_str = None
            continue
        if ch in "\"'`":
            in_str = ch
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return source[start:pos + 1]
    raise ValueError(f"没有找到完整的 {name} 函数")


CASES = [
    # (公式内容, 期望是否当作公式, 说明)
    (r"(\alpha_1)\xrightarrow{\text{初等行变换}}", True, "矩阵箭头上的中文步骤名"),
    (r"\text{元}", True, "单位包在 \\text 里"),
    (r"\begin{pmatrix}2&-1\\5&3\end{pmatrix}", True, "普通矩阵"),
    ("A", True, "单个变量"),
    ("-1", True, "负数是数学"),
    ("[A|E]", True, "增广矩阵记号"),
    ("50 元", False, "价格不能被当公式"),
    ("50 和 60", False, "两个价格之间的文字"),
    ("这是中文", False, "纯中文"),
    (r"16,%rsp 在栈上分配 16 字节空间", False, "无 LaTeX 命令的代码片段仍按文字"),
    ("", False, "空字符串"),
    ("x" * 400, False, "超长内容不渲染"),
]

def main() -> int:
    node = shutil.which("node")
    if not node:
        print("找不到 node，跳过（本机发版流程需要 node）")
        return 0

    source = APP_JS.read_text(encoding="utf-8")
    fn = extract_function(source, "isMath")

    cases_js = json.dumps([[c[0], c[1]] for c in CASES], ensure_ascii=False)
    script = f"""
{fn}
const cases = {cases_js};
const out = cases.map(([s]) => isMath(s));
console.log(JSON.stringify(out));
"""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "check.js"
        path.write_text(script, encoding="utf-8")
        proc = subprocess.run([node, str(path)], capture_output=True, text=True,
                              encoding="utf-8", timeout=60)
    if proc.returncode != 0:
        print("node 执行失败：", proc.stderr[:400])
        return 1

    got = json.loads(proc.stdout.strip())
    for (tex, expect, label), actual in zip(CASES, got):
        ok(actual == expect, f"{label}：isMath={actual}（期望 {expect}）")

    print()
    if FAILED:
        print(f"{FAILED} 项未通过：公式判定会把该渲染的挡掉，或把文字误当公式。")
        return 1
    print("全部通过：夹中文的 LaTeX 能渲染，价格与纯文字不受影响。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
