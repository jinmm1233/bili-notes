"""导出：把结构化结果渲染成可直接阅读/分享的 Markdown 笔记包。"""
from __future__ import annotations

import json
import logging
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from ..core.models import Chapter, Frame, VideoNote
from ..core.paths import fmt_ts, safe_dirname, ts_link
from . import itinerary, mindmap as mm
from .subtitle import to_srt

log = logging.getLogger(__name__)

_SOURCE_LABEL = {"cc": "CC 字幕（人工）", "ai": "B站 AI 字幕", "asr": "本地语音转写"}


def make_frame_name(frame: Frame, index: int, suffix: str = "jpg") -> str:
    return f"frame_{fmt_ts(frame.ts).replace(':', '-')}_{index:03d}.{suffix}"


def export(
    note: VideoNote,
    tree: dict[str, Any],
    out_root: Path,
    segments=None,
    llm_label: str = "",
) -> Path:
    out_dir = out_root / safe_dirname(f"{note.meta.title}")
    assets = out_dir / "assets"
    assets.mkdir(parents=True, exist_ok=True)

    # 1. 复制关键帧，文件名内嵌时间码
    image_map: dict[int, str] = {}
    all_frames: list[Frame] = []
    for chapter in note.chapters:
        for idx in chapter.chunk_indexes:
            all_frames.extend(note.chunks[idx].frames)
    seen: set[str] = set()
    for i, frame in enumerate(all_frames, 1):
        if frame.path in seen:
            continue
        seen.add(frame.path)
        name = make_frame_name(frame, i)
        try:
            shutil.copy2(frame.path, assets / name)
            image_map[id(frame)] = f"assets/{name}"
        except OSError as exc:
            log.warning("复制关键帧失败 %s: %s", frame.path, exc)

    # 2. 正文
    body = render_markdown(note, tree, image_map, llm_label)
    md_path = out_dir / "notes.md"
    md_path.write_text(body, encoding="utf-8", newline="\n")

    # 3. 附属文件
    if tree and (tree.get("children") or tree.get("name")):
        (out_dir / "mindmap.html").write_text(
            mm.to_markmap_html(tree, note.meta.title), encoding="utf-8", newline="\n"
        )
        (out_dir / "mindmap.json").write_text(mm.to_json(tree), encoding="utf-8", newline="\n")
    (out_dir / "meta.json").write_text(
        json.dumps(note.meta.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8", newline="\n",
    )
    if segments:
        (out_dir / "subtitle.srt").write_text(to_srt(segments), encoding="utf-8", newline="\n")

    log.info("笔记已导出 %s（图片 %d 张）", md_path, len(image_map))
    return md_path


def render_markdown(
    note: VideoNote,
    tree: dict[str, Any],
    image_map: dict[int, str],
    llm_label: str = "",
) -> str:
    meta = note.meta
    tags = _collect_tags(note)
    lines: list[str] = []

    lines.append("---")
    lines.append(f'title: "{_yaml_safe(meta.title)}"')
    lines.append(f'source: "{meta.webpage_url}"')
    lines.append(f'uploader: "{_yaml_safe(meta.uploader)}"')
    if meta.collection:
        lines.append(f'collection: "{_yaml_safe(meta.collection)}"')
    lines.append(f'duration: "{fmt_ts(meta.duration)}"')
    lines.append(f'created: "{datetime.now().strftime("%Y-%m-%d %H:%M")}"')
    lines.append(f'transcript: "{note.transcript_source}"')
    if llm_label:
        lines.append(f'model: "{llm_label}"')
    if tags:
        lines.append(f"tags: [{', '.join(tags)}]")
    lines.append("---")
    lines.append("")

    lines.append(f"# {meta.title}")
    lines.append("")
    pub = datetime.fromtimestamp(meta.pubdate).strftime("%Y-%m-%d") if meta.pubdate else "—"
    lines.append(
        f"> **UP主** {meta.uploader or '—'} · **时长** {fmt_ts(meta.duration)} · "
        f"**发布** {pub} · [原视频]({meta.webpage_url})"
    )
    if note.segments:
        lines.append(f"> 字幕来源：{_SOURCE_LABEL.get(note.transcript_source, note.transcript_source)}")
    else:
        # 没有一句转录：视频无音轨 / 纯配乐 / 人声太轻。别说成"字幕来源"误导人。
        lines.append("> 本视频没有可识别的语音，笔记依据画面内容整理")
    if meta.part_count > 1:
        lines.append(
            f"> ⚠ 本视频共 **{meta.part_count}** 个分P，本笔记仅覆盖第 **{meta.page_index}** P；"
            "如需全部，可在采集页勾选「展开合集/分P」重新处理"
        )
    lines.append("")

    if note.one_liner:
        lines.append("## 一句话总结")
        lines.append("")
        lines.append(note.one_liner)
        lines.append("")
        lines.extend(_render_hero(note, image_map))

    if note.outline:
        lines.extend(_render_outline(note, image_map))
    elif note.highlights:
        lines.append("## 核心要点")
        lines.append("")
        for h in note.highlights:
            lines.append(f"- {h}")
        lines.append("")
        if not note.one_liner:
            lines.extend(_render_hero(note, image_map))

    if note.problems:
        lines.extend(_render_problems(note))

    if note.itinerary:
        lines.extend(itinerary.render_section(note.itinerary, meta.webpage_url))

    if note.chapters:
        lines.extend(_render_structure(note))

    # 有结构化大纲时，正文已经按主题讲清楚了，章节再贴一遍图和解读只会重复。
    # 这里只留一条折叠的时间轴：想回原片看某一段时用它定位。
    if note.chapters:
        if note.outline:
            lines.extend(_render_timeline(note))
        else:
            lines.append("## 章节")
            lines.append("")
            for chapter in note.chapters:
                lines.extend(_render_chapter(chapter, note, image_map, with_bullets=True))

    if note.glossary:
        lines.append("## 关键概念")
        lines.append("")
        lines.append("| 术语 | 解释 |")
        lines.append("|---|---|")
        for item in note.glossary:
            lines.append(f"| {item['term']} | {_table_safe(item['explain'])} |")
        lines.append("")

    if note.text_fixes:
        lines.extend(_render_fixes(note.text_fixes))

    if note.segments:
        lines.append("## 完整转录")
        lines.append("")
        n_tr = sum(1 for s in note.segments if s.translation)
        if n_tr:
            lines.append(
                f"<details><summary>展开全文（{len(note.segments)} 句，"
                f"含 {n_tr} 句中文译文）</summary>"
            )
        else:
            lines.append(f"<details><summary>展开全文（{len(note.segments)} 句）</summary>")
        lines.append("")
        for seg in note.segments:
            lines.append(f"`{fmt_ts(seg.start)}` {seg.text}")
            if seg.translation:
                lines.append(f"`中` {seg.translation}")
        lines.append("")
        lines.append("</details>")
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append(
        f"*由 BiliNotes 生成于 {datetime.now().strftime('%Y-%m-%d %H:%M')} · "
        f"内容基于视频字幕与画面文字自动整理，关键结论建议回原视频核对。*"
    )
    return "\n".join(lines)


def _render_structure(note: VideoNote) -> list[str]:
    """内容结构：章节 + 每章要点，纯列表。

    以前这里是「知识地图」mermaid 流程图，把每章要点也画成节点——
    30 字的长句横着铺开，手机上要左右拖才看得全，图反而成了噪声。
    结构本来就是一串标题，列表最省事：章节一层，要点在原位，
    导出 Word/PDF 也不跑版（图只留在同目录的 mindmap.html 里）。

    要点数量按章节数自适应：章节多就每章少列几条，免得整段变成正文的复读。
    """
    sections: list[tuple[str, float, list[str]]] = []
    for sec in note.outline or []:
        points: list[str] = []
        for group in sec.get("groups") or []:
            points += [str(x).strip() for x in (group.get("items") or []) if str(x).strip()]
        sections.append((
            str(sec.get("title") or "").strip(),
            float(sec.get("cover_ts") or 0),
            points,
        ))
    if not sections:
        for chapter in note.chapters or []:
            sections.append((
                str(chapter.title or "").strip(),
                float(chapter.start or 0),
                [b.strip() for b in (chapter.bullets or []) if b.strip()],
            ))
    sections = [s for s in sections if s[0]]
    if not sections:
        return []

    multi = len(sections) > 1
    keep = 3 if multi else 5
    lines = ["## 内容结构" if multi else "## 要点速览", ""]
    for idx, (title, ts, points) in enumerate(sections, 1):
        # 标题自带「一、」「1.」时不再加序号，否则会出现「1. 一、…」
        prefix = "" if re.match(r"^(?:[一二三四五六七八九十百]+[、.]|\d+[、.)]|（[一二三四五六七八九十]+）)", title) else f"{idx}. "
        head = f"**{prefix}{title}**" if multi else f"**{title}**"
        if ts:
            head += f"　`{fmt_ts(ts)}`"
        lines.append(head)
        lines.append("")
        for point in points[:keep]:
            lines.append(f"- {point}")
        if points:
            lines.append("")
    if multi:
        lines.append("> 想看能缩放的结构图：同目录 `mindmap.html`（只画章节，不含逐条要点）")
        lines.append("")
    return lines


def _render_fixes(fixes: list[dict]) -> list[str]:
    """把自动修正记录写进笔记 —— 改了哪里必须让人看得见。"""
    from ..core.paths import fmt_ts  # noqa: PLC0415

    lines = ["## 文本校验", ""]
    lines.append(
        f"> 自动修正了 {len(fixes)} 处疑似识别错误（同音字/术语），"
        "依据是画面文字与视频标题的比对结果，未改写原意。"
    )
    lines.append("")
    lines.append("| 时间 | 原文 | 修正 | 依据 |")
    lines.append("|---|---|---|---|")
    reason_label = {"ocr": "画面文字", "meta": "标题/简介", "rule": "规则"}
    for fix in fixes[:20]:
        stamp = fmt_ts(fix.get("start", 0))
        lines.append(
            f"| {stamp} | {fix.get('wrong', '')} | {fix.get('right', '')} "
            f"| {reason_label.get(fix.get('reason', ''), fix.get('reason', ''))} |"
        )
    if len(fixes) > 20:
        lines.append(f"| … | 另有 {len(fixes) - 20} 处 | | |")
    lines.append("")
    return lines


_CN_NUM = "一二三四五六七八九十"


def _frames_of(note: VideoNote) -> list[Frame]:
    """笔记里所有关键帧（按 chunk 顺序，去重）。"""
    out: list[Frame] = []
    seen: set[int] = set()
    for chunk in note.chunks:
        for frame in chunk.frames:
            if id(frame) in seen:
                continue
            seen.add(id(frame))
            out.append(frame)
    return out


def _nearest_frame(frames: list[Frame], ts: float, tol: float = 75.0) -> Frame | None:
    """找时间上最接近 ts 的一帧；差得太多就不要（宁可没图，也不给错图）。"""
    best: Frame | None = None
    best_gap = tol
    for frame in frames:
        gap = abs(frame.ts - ts)
        if gap <= best_gap:
            best, best_gap = frame, gap
    return best


def _hero_image(note: VideoNote, image_map: dict[int, str]) -> tuple[Frame, str] | None:
    """全片代表画面：优先"信息量评分最高"，同分取有画面要点的。"""
    frames = [f for f in _frames_of(note) if image_map.get(id(f))]
    if not frames:
        return None
    best = max(frames, key=lambda f: (f.score, 1 if (f.screen_points or f.screen_title) else 0))
    return best, image_map[id(best)]


def _render_hero(note: VideoNote, image_map: dict[int, str]) -> list[str]:
    """总结下面那张"全片代表画面"。"""
    hero = _hero_image(note, image_map)
    if not hero:
        return []
    frame, rel = hero
    return [
        f"![代表画面]({rel})",
        "",
        f"<sub>▲ {fmt_ts(frame.ts)} · 全片代表画面 · "
        f"[跳转到此处]({ts_link(note.meta.webpage_url, frame.ts)})</sub>",
        "",
    ]


def _section_shot(
    note: VideoNote,
    node: dict[str, Any],
    image_map: dict[int, str],
    used: set[int],
    fallback_order: int | None = None,
) -> tuple[Frame, str] | None:
    """给一段总结配一张对应画面。

    时间点来自模型的 cover_ts（它看得见材料里的时间标记，挑的是画面信息最足的那一刻）。
    - 一级主题：模型没给或给得不靠谱时，按"主题顺序 ≈ 时间顺序"均分时间轴兜底，
      保证开头每个大段都有图；
    - 二级小段：模型给空字符串就表示"这段没什么可截的"，直接不配图。
    已经用过的帧不会再用——同一张图在总结里出现两次只会让人困惑。
    """
    frames = [f for f in _frames_of(note) if image_map.get(id(f)) and id(f) not in used]
    if not frames:
        return None
    ts = float(node.get("cover_ts") or 0)
    if ts <= 0:
        if fallback_order is None:
            return None
        total = len(note.outline) or 1
        span = note.meta.duration or max(f.ts for f in frames)
        ts = span * (fallback_order + 0.5) / total
    frame = _nearest_frame(frames, ts)
    if frame is None:
        return None
    used.add(id(frame))
    return frame, image_map[id(frame)]


def _screen_block(frame: Frame) -> list[str]:
    """一张画面的「画面要点」块（blockquote 行，首行是标题，后跟要点列表）。

    有 screen_title/screen_points 用新结构；旧笔记数据只有 caption，
    兜底渲染成一行。什么都没有就返回空 —— 宁可没块，不写废话。
    """
    if frame.screen_title or frame.screen_points:
        head = "> **画面要点**" + (f"：**{frame.screen_title}**" if frame.screen_title else "")
        out = [head]
        out.extend(f"> - {p}" for p in frame.screen_points)
        return out
    if frame.caption:
        return [f"> **画面要点**：{frame.caption}"]
    return []


def _render_shot(
    note: VideoNote,
    shot: tuple[Frame, str] | None,
    *,
    with_caption: bool = False,
) -> list[str]:
    """一张配图 + 时间码跳转（可选：跟一块画面要点）。

    画面要点放在图下面读起来最顺：先看画面，再对上面的文字要点。
    """
    if not shot:
        return []
    frame, rel = shot
    out = [
        f"![画面 {fmt_ts(frame.ts)}]({rel})",
        "",
        f"<sub>▲ {fmt_ts(frame.ts)} · "
        f"[跳转到此处]({ts_link(note.meta.webpage_url, frame.ts)})</sub>",
        "",
    ]
    if with_caption:
        out.extend(_screen_block(frame))
        if frame.screen_title or frame.screen_points or frame.caption:
            out.append("")
    return out


def _render_outline(note: VideoNote, image_map: dict[int, str]) -> list[str]:
    """渲染结构化笔记：主题 → 子主题 → 要点。

    这是笔记的主体。时间轴放在后面的「章节」里，两者分工不同：
    这里回答"视频讲了什么"，那里回答"第几分钟讲了什么"。
    每段总结下面配一张对应画面：文字给结论，画面给证据。
    """
    lines: list[str] = ["## 笔记", ""]
    used: set[int] = set()

    for i, topic in enumerate(note.outline):
        mark = _CN_NUM[i] if i < len(_CN_NUM) else str(i + 1)
        lines.append(f"### {mark}、{topic.get('title', '')}")
        lines.append("")

        summary = topic.get("summary") or ""
        if summary:
            lines.append(f"> {summary}")
            lines.append("")

        lines.extend(_render_shot(
            note, _section_shot(note, topic, image_map, used, i), with_caption=True
        ))

        for group in topic.get("groups") or []:
            gtitle = group.get("title") or ""
            if gtitle:
                lines.append(f"#### {gtitle}")
                lines.append("")
            lines.extend(_render_shot(
                note, _section_shot(note, group, image_map, used), with_caption=True
            ))
            for item in group.get("items") or []:
                lines.append(f"- {item}")
            lines.append("")

    if note.takeaways:
        lines.append("## 结论")
        lines.append("")
        for item in note.takeaways:
            lines.append(f"- {item}")
        lines.append("")

    return lines


def _render_problems(note: VideoNote) -> list[str]:
    """例题精解：题干 → 解题关键（荧光笔）→ 步骤 → 易错点。

    和正文的分工：正文讲"这道题在说什么"，这里回答"拿到这道题怎么下手"。
    题眼单独一行并高亮，是因为复盘时人往往只记得住那一句。
    """
    if not note.problems:
        return []

    lines: list[str] = ["## 例题精解", ""]
    for i, prob in enumerate(note.problems):
        head = f"### 例题 {i + 1}"
        if prob.title:
            head += f"：{prob.title}"
        if prob.ts:
            # 不要用 [..] 把时间码和链接再包一层：Markdown 链接语法会从外层 [ 开始匹配，
            # 文字会变成「01:16 · [跳到讲解」，多出半个方括号。
            head += (
                f" <sub>{fmt_ts(prob.ts)} · "
                f"[跳到讲解]({ts_link(note.meta.webpage_url, prob.ts)})</sub>"
            )
        lines.append(head)
        lines.append("")

        if prob.statement:
            lines.append("> **题目**")
            lines.append(">")
            for para in prob.statement.split("\n"):
                lines.append(f"> {para}".rstrip())
            lines.append("")

        if prob.key:
            lines.append(f"> **解题关键**：{prob.key}")
            lines.append("")

        if prob.steps:
            lines.append("**步骤**")
            lines.append("")
            for n, step in enumerate(prob.steps, 1):
                lines.append(f"{n}. {step}")
            lines.append("")

        if prob.pitfall:
            lines.append(f"> **易错**：{prob.pitfall}")
            lines.append("")

    return lines


def _render_timeline(note: VideoNote) -> list[str]:
    """折叠的时间轴：只列"第几段讲了什么"，点时间码跳回原片。

    内容和图片都在上面的「笔记」里，这里不再重复，只负责定位。
    """
    lines: list[str] = ["## 时间轴", ""]
    lines.append(
        f"<details><summary>按原视频顺序的 {len(note.chapters)} 个片段"
        "（点时间码跳到原片）</summary>"
    )
    lines.append("")
    for chapter in note.chapters:
        stamp = fmt_ts(chapter.start)
        link = ts_link(note.meta.webpage_url, chapter.start)
        lines.append(f"- [`{stamp}`]({link}) {chapter.title}")
    lines.append("")
    lines.append("</details>")
    lines.append("")
    return lines


def _render_chapter(
    chapter: Chapter,
    note: VideoNote,
    image_map: dict[int, str],
    with_bullets: bool = True,
) -> list[str]:
    lines: list[str] = []
    lines.append(f"### {chapter.index + 1:02d} {chapter.title} `[{fmt_ts(chapter.start)}]`")
    lines.append("")
    if chapter.summary:
        lines.append(chapter.summary)
        lines.append("")

    bullets: list[str] = []
    if with_bullets:
        for idx in chapter.chunk_indexes:
            for b in note.chunks[idx].bullets:
                if b not in bullets:
                    bullets.append(b)
        if bullets:
            for b in bullets[:8]:
                lines.append(f"- {b}")
            lines.append("")

    for idx in chapter.chunk_indexes:
        chunk = note.chunks[idx]
        for frame in chunk.frames:
            rel = image_map.get(id(frame))
            if not rel:
                continue
            stamp = fmt_ts(frame.ts)
            lines.append(
                f"![画面 {stamp}]({rel})"
            )
            lines.append("")
            lines.append(f"<sub>▲ {stamp} · [跳转到此处]({ts_link(note.meta.webpage_url, frame.ts)})</sub>")
            lines.append("")
            # 画面要点是唯一说明：OCR 原文是行列错乱的碎片，直接给读者看
            # 等于没加工（用户反馈"太乱"）；v2 的"画面解读"又只是复述正文。
            lines.extend(_screen_block(frame))
            if frame.screen_title or frame.screen_points or frame.caption:
                lines.append("")
    return lines


def _collect_tags(note: VideoNote) -> list[str]:
    counts: dict[str, int] = {}
    for chunk in note.chunks:
        for kw in chunk.keywords:
            counts[kw] = counts.get(kw, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:8]
    return [f'"{_yaml_safe(k)}"' for k, _ in ranked]


def _yaml_safe(text: str) -> str:
    return (text or "").replace('"', "'").replace("\n", " ")


def _table_safe(text: str) -> str:
    return (text or "").replace("|", "／").replace("\n", " ")
