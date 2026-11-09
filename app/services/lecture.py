"""知识点讲义：把一份已经成稿的笔记，重写成"老师讲课"的讲义。

和 notes.md 的分工（这是这个模块存在的理由）：
  - 笔记 = 现场记录。画面要点、时间轴、原文转录、例题步骤，忠于视频、密度高、偏"速查"。
  - 讲义 = 课后复盘。为什么需要这个概念、从哪来、怎么推、怎么用、会错在哪，偏"讲透"。

所以讲义不是把笔记再抄一遍，而是拿笔记（含完整转录）当素材重新组织：
先讲动机，再下定义，再推原理，再看例题，最后补易错和自测。

Prompt 结构参考了"名师备课"的套路：动机 → 定义 → 推导 → 方法 → 例题 → 易错 → 自测。
不追求篇幅，追求每一节都在回答"为什么"，这也是它能和笔记拉开差距的地方。
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from ..core.llm import LLMClient
from . import mathfmt

log = logging.getLogger(__name__)

LECTURE_FILE = "lecture.md"
MAX_INPUT = 26000     # 笔记 + 转录喂太多没意义，超出就截断（模型上下文和费用都省）
MAX_TOKENS = 6000

_SYSTEM = """你是一位能把枯燥内容讲活的名师，正在为一段视频课程写**知识点讲义**。

【你的读者】
看过视频、现在要真正学会的人。笔记他们已经有了（速查用），讲义要解决的是"当时听懂了，回头还是不会"。

【必须做到】
1. 每一节先回答"为什么"：为什么需要这个概念、它解决什么问题、不用它会怎样。禁止只罗列定义。
2. 概念要拆开讲：定义 → 直观解释（打个比方）→ 符号/术语约定 → 一个最小例子。
3. 推导要写清每一步的依据，宁可多写一句"这一步用了什么"，也不要跳步。
4. 方法要写成可操作的编号步骤，指出每一步的判断依据（"看到 X 就想到 Y"）。
5. 例题不能只给答案：要写「读题 → 判断用什么工具 → 关键一步 → 易错点」。
6. 把视频里出现的时间点写成 [mm:ss]，方便回看原片。
7. 结尾给 3~5 道自测题，答案折叠在 <details><summary>答案</summary>…</details> 里。

【禁止】
- 不要复述笔记原文，不要写"视频中提到""老师讲到"这类转述腔，直接讲知识本身。
- 不要编造视频里没有的数据、结论或例题；素材不足的地方就不写，不要凑字数。
- 不要写"本文将介绍…"这种空话开头，直接进入内容。

【格式】
- 纯 Markdown。标题用 ## / ###，不要出现一级标题（标题由程序加）。
- 数学公式一律 LaTeX：行内 $...$，独立 $$...$$；矩阵用 \begin{bmatrix}。
- 表格用于对比/辨析，不要用表格做排版。
- 中文写作，术语第一次出现时给英文原词。
- 篇幅 1500~3000 字，宁可精炼，不要注水。

【输出骨架】（可根据素材增减，但顺序保持）
## 这节课在解决什么问题
## 核心概念
## 原理与推导
## 方法与步骤
## 例题精讲
## 容易踩的坑
## 自测
"""


def _strip_frontmatter(text: str) -> str:
    return re.sub(r"\A---\n[\s\S]*?\n---\n", "", text or "")


def _transcript_body(md: str) -> str:
    """笔记末尾的 <details> 里塞着完整转录，那是讲义最有价值的素材，单独拎出来。"""
    match = re.search(r"<details>\s*<summary>[^<]*</summary>([\s\S]*?)</details>", md)
    if not match:
        return ""
    body = re.sub(r"<[^>]+>", "", match.group(1))
    return re.sub(r"\n{3,}", "\n\n", body).strip()


def _material(note_md: str) -> str:
    """拼出喂给模型的素材：笔记正文（去掉转录块）+ 转录正文，超长从头尾各取一半。"""
    md = _strip_frontmatter(note_md)
    notes_part = re.sub(r"<details>[\s\S]*?</details>", "", md).strip()
    transcript = _transcript_body(md)
    parts = ["【已成稿的笔记】\n" + notes_part]
    if transcript:
        parts.append("【视频转录原文（补充细节用，不必逐句引用）】\n" + transcript)
    material = "\n\n".join(parts)
    if len(material) > MAX_INPUT:
        head = material[: int(MAX_INPUT * 0.55)]
        tail = material[-int(MAX_INPUT * 0.4) :]
        material = f"{head}\n\n……（中间省略）……\n\n{tail}"
    return material


def title_of(note_md: str, fallback: str = "") -> str:
    for line in _strip_frontmatter(note_md).splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return fallback.strip()


def build(client: LLMClient, note_md: str, title: str = "") -> str:
    """生成讲义正文（不含一级标题）。素材为空时返回空串，让调用方跳过。"""
    material = _material(note_md)
    if len(material) < 200:
        return ""
    name = title or title_of(note_md)
    prompt = (
        f"视频标题：{name}\n\n"
        f"{material}\n\n"
        "请按系统提示的要求，写这份知识点讲义。"
    )
    text = client.complete(_SYSTEM, prompt, max_tokens=MAX_TOKENS)
    text = (text or "").strip()
    if not text:
        return ""
    # 模型偶尔会自作主张加一级标题，去掉，统一由 save() 加
    text = re.sub(r"\A#\s+[^\n]*\n+", "", text)
    text = mathfmt.normalize(text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text


def save(note_dir: Path, body: str, title: str) -> Path:
    """写进笔记目录，frontmatter 保留来源，方便以后单独导出/检索。"""
    note_dir.mkdir(parents=True, exist_ok=True)
    path = note_dir / LECTURE_FILE
    header = f"# {title} · 知识点讲义\n\n> 由笔记内容重新组织成讲义，供理解与复习使用；原始记录见 notes.md。\n\n"
    path.write_text(header + body + "\n", encoding="utf-8", newline="\n")
    return path


def exists(note_dir: Path) -> bool:
    path = note_dir / LECTURE_FILE
    try:
        return path.is_file() and path.stat().st_size > 200
    except OSError:
        return False


def generate_for_dir(client: LLMClient, note_dir: Path, title: str = "") -> Path | None:
    """对磁盘上已有的笔记补生成讲义（不重跑视频，只花一次文本 token）。"""
    src = note_dir / "notes.md"
    if not src.is_file():
        return None
    note_md = src.read_text(encoding="utf-8", errors="replace")
    body = build(client, note_md, title or title_of(note_md, note_dir.name))
    if not body:
        return None
    return save(note_dir, body, title or title_of(note_md, note_dir.name))
