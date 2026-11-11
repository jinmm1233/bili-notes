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
from ..core import zh
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
    if not all_frames:
        # 没有章节时也要把关键帧带出去，否则整篇笔记一张图都没有
        all_frames = [f for c in note.chunks for f in c.frames]
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
    # 兜底：整篇再过一遍繁简，保证笔记里看不到繁体
    md_path.write_text(zh.to_simplified(body), encoding="utf-8", newline="\n")

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
            float(sec.get("ts") or sec.get("cover_ts") or 0),
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


def _nearest_frame(frames: list[Frame], ts: float, tol: float = 8.0) -> Frame | None:
    """找时间上最接近 ts 的一帧；差得太多就不要（宁可没图，也不给错图）。

    以前容差是 75 秒：模型猜的 cover_ts 偏一会儿，配图就落到别的知识点上
    （TED 讲解里 00:02 讲的 this is a true story 配了 00:36 的单词卡）。
    现在时间码来自音画锚定的真实帧，±8 秒足够覆盖抽帧间隔，再远就是错图。
    """
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
    fallback_order: int | None = None,
) -> tuple[Frame, str] | None:
    """给一段总结配一张对应画面。

    时间不再让模型猜：优先用 node["ts"] —— 那是要点从音画锚点抄来、并已吸附到
    真实帧的时刻；旧笔记没有这个字段时才退回模型给的 cover_ts（容差同样收紧）。
    - 一级主题：连 cover_ts 都没有时，按「主题顺序 ≈ 时间顺序」均分时间轴兜底；
    - 二级小段：什么时间都没有就不配图。

    这里不再维护「一张图全篇只能用一次」的全局集合：同一页 PPT 讲两个知识点，
    本来就该在两个地方各出现一次，读者才对得上。连续重复由渲染层去重。
    """
    frames = [f for f in _frames_of(note) if image_map.get(id(f))]
    if not frames:
        return None
    ts = float(node.get("ts") or 0)
    if ts > 0:
        # ts 是真实帧时刻，先按 2 秒找；高清替换/抽帧误差再放宽到 8 秒
        frame = _nearest_frame(frames, ts, tol=2.0) or _nearest_frame(frames, ts, tol=8.0)
    else:
        cover = float(node.get("cover_ts") or 0)
        if cover <= 0:
            if fallback_order is None:
                return None
            total = len(note.outline) or 1
            span = note.meta.duration or max(f.ts for f in frames)
            cover = span * (fallback_order + 0.5) / total
        frame = _nearest_frame(frames, cover, tol=8.0)
    if frame is None:
        return None
    return frame, image_map[id(frame)]


def _item_line(note: VideoNote, text: str, ts: float) -> str:
    """一条要点 + 它讲的那帧画面时间码（可跳回原片）。"""
    if ts <= 0:
        return f"- {text}"
    stamp = fmt_ts(ts)
    url = note.meta.webpage_url or ""
    if url:
        return f"- {text}　<sub>[`{stamp}`]({ts_link(url, ts)})</sub>"
    return f"- {text}　<sub>`{stamp}`</sub>"


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


def _page_of(frame: Frame) -> str:
    """一帧所属的「页」标识，用来判断两条要点是不是同一页 PPT。

    模型给的 page_key 才是权威（它看的是画面内容）；没有时退回画面标题。
    """
    key = (getattr(frame, "page_key", "") or "").strip()
    if not key:
        key = (getattr(frame, "screen_title", "") or "").strip()
    return "".join(ch for ch in key.lower() if ch not in " \t\r\n，。、：:；;·-—_()（）【】")[:40]


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
    last_path = ""   # 只去掉紧挨着的重复图，不再限制一张图全篇出现一次

    def shot(node: dict[str, Any], order: int | None = None):
        nonlocal last_path
        result = _section_shot(note, node, image_map, order)
        if result and result[0].path == last_path:
            return None
        if result:
            last_path = result[0].path
        return result

    for i, topic in enumerate(note.outline):
        mark = _CN_NUM[i] if i < len(_CN_NUM) else str(i + 1)
        lines.append(f"### {mark}、{topic.get('title', '')}")
        lines.append("")

        summary = topic.get("summary") or ""
        if summary:
            lines.append(f"> {summary}")
            lines.append("")

        lines.extend(_render_shot(note, shot(topic, i), with_caption=True))

        for group in topic.get("groups") or []:
            gtitle = group.get("title") or ""
            if gtitle:
                lines.append(f"#### {gtitle}")
                lines.append("")

            items = [str(x) for x in (group.get("items") or [])]
            times = [float(t or 0) for t in (group.get("items_ts") or [])]
            anchored = len(times) == len(items) and any(t > 0 for t in times)

            if anchored:
                # 每条要点按**自己**的时刻找画面：这一帧没被选进笔记就不给图，
                # 也不让这条要点去蹭上一条的图——蹭了就成了张冠李戴
                # （真跑过一轮：标 03:28 的「well-dressed」上面贴着 03:04 的图）。
                #
                # 同一页 PPT 只出一次图：PPT 一页停留几分钟时，这一页会被
                # 多个知识点引用。用户要的是「一张图 + 把这页内容说清楚」，
                # 不是每条知识点都贴一张一样的图。
                # 所以：这张图第一次出现时把画面要点（页面标题 + 全部要点）
                # 完整写出来，后面同页的要点只留文字，不再重复贴图。
                page_shot_done: set[str] = set()
                for text, ts in zip(items, times):
                    result = shot({"ts": ts})
                    if result:
                        frame = result[0]
                        pkey = _page_of(frame)
                        if pkey and pkey in page_shot_done:
                            # 同一页已经贴过图：不再重复贴，
                            # 但这一条要点仍然要写出来（图下面那块画面要点
                            # 已经把这一页的内容说清楚了）。
                            lines.append(_item_line(note, text, frame.ts))
                            lines.append("")
                            continue
                        if pkey:
                            page_shot_done.add(pkey)
                        # 画面要点要带上：用户要的是「把这页 PPT 的内容说清楚」，
                        # 光给一张图加一句话等于没讲。with_caption=True 会把
                        # 画面上的标题和要点原文渲染出来。
                        lines.extend(_render_shot(note, result, with_caption=True))
                        # 时间码用这张图的时间：点一下正好落在它讲的那一页上，
                        # 图和时间码说的是同一件事，不会出现「图 03:44、码 03:49」
                        lines.append(_item_line(note, text, frame.ts))
                    else:
                        lines.append(_item_line(note, text, ts))
                    lines.append("")
            else:
                # 旧笔记 / 模型没抄时间码：保留原来的「二级主题一张封面图」
                lines.extend(_render_shot(note, shot(group), with_caption=True))
                for item in items:
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
        seen_bullets: set[str] = set()
        for idx in chapter.chunk_indexes:
            chunk = note.chunks[idx]
            times = (
                chunk.bullet_ts
                if len(chunk.bullet_ts) == len(chunk.bullets) else []
            )
            for j, b in enumerate(chunk.bullets):
                if b in seen_bullets:
                    continue
                seen_bullets.add(b)
                bullets.append(_item_line(note, b, times[j] if times else 0.0))
        if bullets:
            for b in bullets[:8]:
                lines.append(b)
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
