"""出题范围取笔记的回归测试（`python tests/smoke_quiz_scope.py`）。

背景：首页「自测」可以按单篇或整个收藏夹出题。合集分支曾直接使用
`library.scan()` 的结果——那是列表页的元信息，**没有 markdown**，
于是每篇都被当成"材料不足 300 字"直接返回空，一篇都没问过模型，
用户看到「模型没给出可用题目」；单篇分支用 `get_note()` 带正文，所以正常。

这里守住：两条分支都必须拿到正文，坏 id 跳过而不是抛错。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.api import routes_quiz  # noqa: E402
from app.services import library  # noqa: E402

FAILED = 0


def ok(cond: bool, msg: str) -> None:
    global FAILED
    print(("[OK] " if cond else "[FAIL] ") + msg)
    if not cond:
        FAILED += 1


BODY = "线性相关的定义是存在不全为零的系数使线性组合为零。" * 20

# scan() 的真实特征：只有元信息，没有 markdown
SCAN = [
    {"id": "n1", "title": "第一篇", "collection": "线代"},
    {"id": "n2", "title": "第二篇", "collection": "线代"},
    {"id": "n3", "title": "别的", "collection": "其他"},
]
FULL = {n["id"]: {"id": n["id"], "title": n["title"], "markdown": BODY,
                  "meta": {"collection": n["collection"]}} for n in SCAN}

real_scan, real_get = library.scan, library.get_note
try:
    library.scan = lambda settings: [dict(n) for n in SCAN]

    def fake_get(settings, note_id):
        if note_id not in FULL:
            raise FileNotFoundError(note_id)
        return dict(FULL[note_id])

    library.get_note = fake_get

    # 1. 整个收藏夹：必须带正文
    notes = routes_quiz._load_notes(object(), [], "线代")
    ok([n["id"] for n in notes] == ["n1", "n2"], f"合集只取本收藏夹的笔记（{len(notes)} 篇）")
    ok(all(len(n.get("markdown") or "") > 300 for n in notes), "合集分支每篇都带正文")
    ok(not any("markdown" in n for n in SCAN), "对照：scan() 本身确实没有正文")

    # 2. 单篇：同样带正文
    one = routes_quiz._load_notes(object(), ["n2"], "")
    ok(len(one) == 1 and len(one[0]["markdown"]) > 300, "单篇分支带正文")

    # 3. 坏 id 跳过，不整体失败
    mixed = routes_quiz._load_notes(object(), ["nope", "n1"], "")
    ok([n["id"] for n in mixed] == ["n1"], "不存在的笔记被跳过")

    # 4. 什么都没有 → 空列表（由路由给出「先选几篇笔记」）
    ok(routes_quiz._load_notes(object(), [], "") == [], "空范围返回空列表")

    # 5. 上限 12 篇，避免一次塞爆上下文
    many = [{"id": f"x{i}", "title": f"t{i}", "collection": "大合集"} for i in range(30)]
    library.scan = lambda settings: many
    FULL.update({f"x{i}": {"id": f"x{i}", "title": f"t{i}", "markdown": BODY, "meta": {}}
                 for i in range(30)})
    capped = routes_quiz._load_notes(object(), [], "大合集")
    ok(len(capped) == 12, f"合集最多取 12 篇（实际 {len(capped)}）")
finally:
    library.scan, library.get_note = real_scan, real_get

print()
if FAILED:
    print(f"{FAILED} 项未通过：出题范围取笔记的逻辑有问题。")
    sys.exit(1)
print("全部通过：合集与单篇都能拿到正文，坏 id 只跳过、不拖垮整份卷子。")
