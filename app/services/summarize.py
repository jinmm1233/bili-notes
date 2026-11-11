"""总结层：Map（逐块）→ Reduce（章节 → 全局）。

三个反幻觉约束贯穿所有 prompt：
1. 只依据给定材料，不引入外部知识
2. 材料没有的写"材料未提及"，绝不编造
3. 温度锁 0.3，输出结构固定
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable

from ..core.errors import CancelledError
from ..core.llm import LLMClient
from ..core.models import Chapter, Chunk, Problem, VideoMeta
from ..core.paths import fmt_ts
from . import anchors as anchors_mod
from . import chunking, frame_caption, mathfmt

log = logging.getLogger(__name__)

CancelFn = Callable[[], bool]

_SYSTEM = (
    "你是严谨的中文视频笔记整理专家。你只依据用户提供的字幕与画面文字工作。\n"
    "铁律：\n"
    "1. 禁止引入材料之外的知识、背景或推测。\n"
    "2. 材料没提到的内容一律写「材料未提及」，不要用常识补全。\n"
    "3. 不要复述客套话、口头禅、语气词。\n"
    "4. 输出必须是合法 JSON，不要加任何解释文字或代码块围栏。"
)

_CHUNK_TMPL = """视频《{title}》的片段，时间 {start} 至 {end}。

下面是按时间排好的「画面 + 同期讲解」音画对。每一组里：
- 【画面】是当时屏幕上本来就有的文字（例句 / PPT / 板书）；
- 【讲解】是这段时间博主说的话，常常没有字幕，是他对这一帧的补充和分析；
- 方括号里的「画面 MM:SS」就是这一组对应的真实画面时间点。

{material}

请输出 JSON：
{{
  "title": "本段小标题，不超过 14 字",
  "summary": "2-4 句话概括这段讲了什么、结论是什么",
  "bullets": ["要点，每条不超过 40 字"],
  "bullet_ts": ["00:06", "00:36"],
  "keywords": ["关键词"]
}}
要求：
- bullets 3-6 条，必须是材料中真实出现的信息；讲解里的补充、分析、语法点也要写进要点，不能只抄画面上的字；
- **绝不要写「材料未提及…」「材料中没有…」这类话**：材料就是全部依据，
  没提到就是这段确实没讲那么多，只写实际有的内容，写不出来就少写一条；
- bullet_ts 与 bullets 一一对应，每条写它讲的那组方括号里的「画面 MM:SS」，照抄材料里的时间码，不要自己编；
  一条要点综合了多组材料就取最主要那一组；keywords 2-5 个。"""

_CHAPTER_TMPL = """下面是视频《{title}》按时间顺序切出的 {n} 个内容块。

{listing}

请把它们合并成 5-12 个章节。输出 JSON：
{{
  "chapters": [
    {{"title": "章节名，不超过 16 字", "start_chunk": 0, "end_chunk": 2, "summary": "1-2 句本章概括"}}
  ]
}}
要求：
- 必须覆盖全部的块，从 0 到 {last}，区间连续、不重叠、不遗漏
- 同一主题的相邻块合并，主题明显切换处断开
- 章节名要具体（如「三种排序算法对比」），不要用「第一部分」这类空标题"""

_GLOBAL_TMPL = """视频《{title}》（UP主：{uploader}，时长 {duration}）的章节摘要如下：

{listing}

请输出 JSON：
{{
  "one_liner": "一句话总结整个视频（不超过 50 字，要说清讲了什么、结论是什么）",
  "highlights": ["全片最值得记住的要点，每条不超过 40 字，用 ==双等号== 圈出其中最关键的词"],
  "glossary": [{{"term": "术语", "explain": "用材料里的说法解释，不超过 50 字"}}]
}}
要求：highlights 5-8 条，按重要性排序。

glossary 是**关键概念表**，只收视频里**真正解释过**的词：
1. **视频给了定义或解释**：讲清了它是什么、为什么用它、它和相近的东西怎么区别、
   或者给了公式/判据。这类才是值得单独记住的。
2. **视频花时间推导或对比过的**：花了好几段在讲它，也收。

反过来，这些**不要收**：
- 只是被提到名字、视频里没给任何解释的（哪怕它确实是个专业术语）——
  收了也讲不清它是什么，白占一个位置；
- 只作为举例背景出现的专有名词、地名店名、人名；
- 口语词和虚词（happens to、involve 这类）、英文语法词、只出现一次的普通名词；
- 视频明确说"不重要/顺便提一下"的东西。

宁缺毋滥：一条视频 3-8 个就够。判断标准是"看完这条我能不能想起它讲了什么"，
不是"这个词出现过没有"。拿不准就不收。

explain 的写法：**直接说这个概念是什么**，不要写"材料中…""视频里提到…"这类
描述材料而不是描述概念的话。
- 好：「用二进制位表示数字，负数用补码存储」
- 坏：「材料中记为E」「链接器执行的第二步」「材料所述使用链接器的原因之一」
- 坏：「存储器层次结构」写成「材料中作为缓存概念引入的层次化存储组织」"""



_OUTLINE_TMPL = """你在为视频《{title}》（UP主 {uploader}，时长 {duration}）写一份结构化笔记，
读者是"没看过视频、想快速掌握内容"的人。

下面是素材材料，按时间排成一组组「画面 + 同期讲解」。
【画面】是当时屏幕上本来就有的文字（例句 / PPT / 板书），可信度更高；
【讲解】是这段时间博主说的话，可能没有字幕，是他对这一帧的补充与分析。
每组方括号里的「画面 MM:SS」是这组对应的真实画面时间点。

{material}

请把它**重组成结构化笔记**，严格遵守：

【零、只写材料里真实有的内容】
禁止写「材料未提及…」「材料中没有…」「仅标注了画面时间点」这类话——
材料就是全部依据，没提到就是这段确实没讲那么多。写不出来就少写一条，
不要用占位句凑数。

【一、只留信息，丢掉对话】
寒暄、口头禅、语气词、主播与嘉宾的客套、重复表达、与主题无关的闲聊，全部删除。
对话类内容只保留其中有信息量的部分（观点、事实、价格、建议、经验）。

【二、外语一律译成中文】
英文或其他语言的句子译成简体中文；地名、店名、菜品、品牌保留通用中文译名，
必要时在括号里附原文。不要在正文里夹杂英文句子。
专业术语首次出现时写成「中文译名（English）」，后文用中文即可。

【三、自己决定怎么分层，不要套模板】
先判断这是什么类型的视频，再决定结构：
- 旅游 / 探店：**一级 = 地点**（城市或区域），二级 = 景点 / 美食 / 交通 / 住宿 / 花费
- 教程 / 知识：一级 = 知识模块，二级 = 具体方法、注意点
- 访谈 / 观点：一级 = 话题，二级 = 各方观点与结论

**最关键的一条：一级主题的数量要少、要粗。**
如果一个地点讲了景点又讲了吃饭，那它们都是这个地点下的**二级**，
不要拆成"某地：景点""某地：美食"这样多个并列的一级主题。
正确：一级「河内」→ 二级「景点」「美食」「交通」
错误：一级「河内：火车街与文庙」+ 一级「河内：餐饮与消费」+ 一级「河内：手工艺」

一级主题按视频里出现的顺序排列，不要用"第一部分""其他"这类空标题。
二级不是每类都要有，有什么写什么，没有的不要硬凑。

【四、每条要点都要具体】
保留可核对的信息：名称、价格、时长、数量、路线、营业形态。
好的例子：「文庙：越南第一所大学，考生考前会来祈福，进士石碑上刻的是汉字」
坏的例子：「参观了很有历史意义的地方」

【五、不编造】
材料没有的一律不写，不要用常识补全背景。

【六、自动标出重点】
每条要点（items）与每条结论（takeaways）里，用 ==双等号== 包住这一条最关键的词、数字或结论性短语
（每条例句最多一处），用于黄色荧光笔高亮。只圈关键词/关键数字/关键结论，不要整句包住；
不必每条都标，挑真正重要的来标即可（约三分之一的要点标一处）。== 必须成对出现。

【六之二、每条要点都要带上它讲的那一刻】
items 里每一条都写成 "[MM:SS] 要点内容"，方括号里的时间码**照抄**该要点所讲那组材料
的「画面 MM:SS」，不要写讲解区间、不要自己编、不要省略。
一条要点综合了相邻两组材料时，取它主要讲的那组。
这个时间码会用来在笔记里配对应画面并跳回原视频，写错就会张冠李戴。

【七、用好画面材料，讲的和画面要对得上】
- 一条要点必须在它讲的那组材料附近，不要跨组把不同画面的内容揉成一条；
- 【画面】里的专有名词、术语、公式、数字、表格数据比转写可信，冲突时以画面为准；
- 画面里的 PPT 题目/小标题适合直接作为一级或二级主题名，与讲解互相印证；
- 画面给出的定义、公式、表格数据必须保留成要点，不要只写"讲解了某公式"；
- 讲解里一带而过、画面写得很具体的，照画面的写；同一画面在多个时间点重复出现是正常的，合并即可。

【九、有题目就单独抽出来】
如果材料里老师在讲一道**具体的题**（例题、习题、真题、算法题、编程题、证明题），
除了把它揉进正文，还要在 problems 里单独记一份，供读者照着做：
- statement：题干。按卷面的样子写全——已知条件、数字、要求求什么，一个都不能少；
  公式和矩阵用 LaTeX；不要写"老师讲了…"这类叙述；
- title：这道题叫什么，4-12 字，如「矩阵连乘」「用初等行变换求逆矩阵」；
- key：**解题的关键**，回答"为什么想到这么做"。写给没听懂的人看，一句话，15-50 字；
  必须是方法层面的突破口（如"连乘顺序不同代价差很多，等价于给矩阵序列划分区间"），
  不要写"按照老师讲的方法计算"这种空话；用 ==双等号== 圈住其中最关键的半句；
- steps：照着能算出来的步骤 3-6 条，按顺序，每条一句话，公式用 LaTeX；
- pitfall：最容易错的地方，一句话；材料没提就留空字符串 ""；
- ts：讲这道题的时间点（材料里的 MM:SS）。
材料里没有具体题目（纯概念讲解、游记、访谈）就输出空的 problems 数组，不要硬凑。

【八、公式与矩阵用 LaTeX 写】
- 矩阵、行列式、分式、求和、积分等数学内容，一律写成 LaTeX：
  行内的用 $...$（如 $A^{{-1}}$、$a_{{ij}}$），独立成行的用 $$...$$；
  矩阵写 $$\\begin{{bmatrix}} 1 & 2 \\\\ 3 & 4 \\end{{bmatrix}}$$（& 分列、\\\\ 换行）；
- 有具体数字的矩阵要照抄数字，也不要写成 [[1,2],[3,4]] 这种列表；
- 公式前后的说明照常用中文，不要把公式拆成纯文字叙述。

输出 JSON（不要代码块围栏）：
{{
  "structure": [
    {{
      "title": "一级主题",
      "summary": "一句话说清这段整体是什么情况",
      "cover_ts": "12:30",
      "groups": [
        {{"title": "二级主题", "cover_ts": "13:10", "items": ["[13:10] 要点1", "[13:24] 要点2"]}}
      ]
    }}
  ],
  "takeaways": ["看完最该记住的 3-6 条结论"],
  "problems": [
    {{
      "title": "题目名称，4-12 字",
      "statement": "题干（条件+数字+求什么），公式用 LaTeX",
      "key": "解题关键：==最关键的那半句==，说明为什么这么做",
      "steps": ["第 1 步", "第 2 步", "第 3 步"],
      "pitfall": "最容易错的地方，没有就写空字符串",
      "ts": "03:12"
    }}
  ]
}}
要求：一级主题 2-8 个；每个一级下 1-5 个二级；每个二级 1-6 条要点；
每条要点 15-60 字，一句话说完，不要写成段落；problems 最多 3 道，没有题目就给 []。

关于 cover_ts（兜底配图时间点）：一级主题和二级主题都要给，
写成材料里出现过的时间（MM:SS 或 HH:MM:SS，如 "12:30"）。
配图优先用 items 里的 [MM:SS]，cover_ts 只在漏写时间码时兜底。
- 挑"画面信息最足"的那一刻：PPT、地图、菜单、界面、招牌、实物特写、人物出镜讲解；
- 不要挑空镜、转场、黑屏、糊掉的动作画面；
- 同一个主题下，二级之间的时间点尽量错开，不要都指到同一秒；
- **如果这一小段确实没有值得截图的画面（纯口播、闲聊、过场），就把 cover_ts 写成空字符串 ""**，
  宁可不配图，也不要瞎填一个时间点。"""


def _fix(text: Any) -> str:
    """要点文本收尾：数学写法转 LaTeX（矩阵能真渲染） + == 高亮补成对。"""
    return _balance_marks(mathfmt.normalize(str(text or "").strip()))


def _balance_marks(text: str) -> str:
    """确保 ==重点== 成对。模型偶尔漏一个，留着会在页面上显示成裸符号。"""
    if not text:
        return text
    if text.count("==") % 2:
        return text.replace("==", "")
    return text


def parse_cover_ts(value: Any, duration: float) -> float:
    """解析模型给的主题配图时间点。

    模型会写成 "12:30"、"1:02:33" 甚至 "12分30秒"，也可能干脆编一个。
    超出视频时长的一律作废（返回 0），交给渲染层退回到"该主题时间段内最清晰的一张"。
    """
    if isinstance(value, (int, float)):
        ts = float(value)
    else:
        text = str(value or "").strip().replace("：", ":")
        if not text:
            return 0.0
        if "分" in text or "秒" in text:
            import re as _re  # noqa: PLC0415

            m = _re.search(r"(?:(\d+)\s*时)?\s*(?:(\d+)\s*分)?\s*(?:(\d+)\s*秒)?", text)
            if not m:
                return 0.0
            h, mi, se = (int(g) if g else 0 for g in m.groups())
            ts = h * 3600 + mi * 60 + se
        else:
            parts = [p for p in text.split(":") if p.strip()]
            if not parts or len(parts) > 3:
                return 0.0
            try:
                nums = [int(p) for p in parts]
            except ValueError:
                return 0.0
            ts = 0.0
            for n in nums:
                ts = ts * 60 + n
    if ts <= 0:
        return 0.0
    if duration and ts > duration + 1:
        return 0.0
    return ts


def snap_anchor_ts(ts: float, chunks: list[Chunk], tol: float = 15.0) -> float:
    """把模型抄来的时间码吸附到它讲的那一帧。

    模型偶尔会抄成讲解区间的起点（例如材料写 [00:02–00:30｜画面 00:12]，
    它抄了 00:02），或写错几秒。只要这个时刻落在某段画面停留区间内，
    就直接归到那张画面——这正是「博主暂停讲这一帧」的定义，不受容差限制；
    落在区间外再按最近的真实帧时刻吸附一次，避免落到别的知识点上。
    """
    if ts <= 0:
        return 0.0
    anchors = [a for c in chunks for a in c.anchors if a.frame_ts > 0]
    for a in anchors:
        if a.span_end > a.span_start and a.span_start - 0.5 <= ts <= a.span_end + 0.5:
            return a.frame_ts
    times = [a.frame_ts for a in anchors]
    if not times:
        return ts
    best = min(times, key=lambda t: abs(t - ts))
    if abs(best - ts) <= tol:
        return best
    # 那一页可能因为和邻页太像被去重掉了（没帧入选），这时退到时间上
    # 最近的有图页：视觉上是内容相近的那一页，宁可时间码靠近它，
    # 也不要让要点去蹭一张不相干的图。
    near = min(anchors, key=lambda a: abs(a.frame_ts - ts))
    return near.frame_ts if abs(near.frame_ts - ts) <= tol * 2 else ts


def _infer_item_times(items: list[str], chunks: list[Chunk]) -> list[float]:
    """大纲模型漏写 [MM:SS] 时，从逐块要点继承它讲的画面时刻。

    逐块总结和大纲看的是同一批材料，措辞会改写但关键词大半重合。
    只在整组都没时间码时启用，且相似度不够就留 0 —— 宁可走 cover_ts
    兜底，也不要凭一点点字面重合把要点指到错的画面上。
    """
    refs = [
        (chunking.bigrams(b), ts)
        for c in chunks
        for b, ts in zip(c.bullets, c.bullet_ts)
        if ts > 0
    ]
    if not refs:
        return [0.0] * len(items)
    out: list[float] = []
    for text in items:
        grams = chunking.bigrams(text)
        if not grams:
            out.append(0.0)
            continue
        best_score, best_ts = 0.0, 0.0
        for ref, ts in refs:
            score = len(grams & ref) / len(grams)
            if score > best_score:
                best_score, best_ts = score, ts
        out.append(best_ts if best_score >= 0.55 else 0.0)
    return out


def _density_note(level: str) -> str:
    """按笔记详细度追加一段「写多细」的要求，覆盖提示词里的默认条数上限。"""
    if level == "detailed":
        return (
            "\n\n【详细度：详细】用户明确要求「每个知识点都写全、不许漏」："
            "每个二级主题下的要点尽量 4-8 条，讲解里出现的每个定义、公式、数字、"
            "例子、前提与边界条件都要单独成条，不要合并同类项、不要只写结论；"
            "画面给的公式和表格数据必须原样保留；takeaways 可以到 8-10 条。"
        )
    if level == "brief":
        return "\n\n【详细度：简洁】每个二级主题 1-3 条要点，只保留主干结论。"
    return ""


def build_outline(
    client: LLMClient | None,
    meta: VideoMeta,
    chunks: list[Chunk],
    max_chars_per_chunk: int = 2600,
    note_detail: str = "standard",
) -> tuple[list[dict[str, Any]], list[str], list[Problem]]:
    """把时间线材料重组成「主题 → 子主题 → 要点」的结构。

    与 chapters 的分工：chapters 保留时间轴（用于跳转原视频），
    outline 负责内容逻辑（同一地点可能横跨多个时间块，
    一个时间块里也可能先后出现两个地点，只有模型能重排）。
    """
    if client is None or not chunks:
        return [], [], []

    # 材料改成「画面 + 同期讲解」成对、带真实时间码的锚点。
    # 以前讲解和画面各是一整坨没时间的正文，模型只能猜配图时间；
    # 现在每条要点都能抄到它讲的那帧，位置才有据可依。
    def _block(c: Chunk) -> str:
        if c.anchors:
            return anchors_mod.anchor_material(c, max_chars=max_chars_per_chunk)
        body = f"【讲解】\n{c.text[:max_chars_per_chunk]}"
        screen = "\n".join(frame_caption.chunk_screen_lines(c, with_ts=True))
        if screen:
            body += f"\n【画面】\n{screen[:600]}"
        return body

    material = "\n\n".join(
        f"=== [{fmt_ts(c.start)} - {fmt_ts(c.end)}] ===\n{_block(c)}"
        for c in chunks
    )

    prompt = _OUTLINE_TMPL.format(
        title=meta.title,
        uploader=meta.uploader or "未知",
        duration=fmt_ts(meta.duration),
        material=material,
    )
    # 材料是外语时不点明，模型有时会"顺手"保留一整段原文
    if is_foreign(material):
        prompt += (
            "\n\n【特别提示】上面的材料是外语原文（非中文）。"
            "成稿必须是纯中文笔记，所有要点都要译过来；"
            "只有专有名词、术语、作品名这类才在括号里附原文。"
        )

    detailed = note_detail == "detailed"
    data = client.complete_json(
        _SYSTEM + _density_note(note_detail), prompt,
        max_tokens=6144 if detailed else 4096,
    )

    outline: list[dict[str, Any]] = []
    for item in data.get("structure") or []:
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        groups: list[dict[str, Any]] = []
        for grp in item.get("groups") or []:
            gtitle = str(grp.get("title") or "").strip()
            # 要点开头的 [MM:SS] 是模型从音画锚点抄来的真实画面时刻，
            # 剥掉存进 items_ts，items 保持纯文本，别影响导图和内容结构。
            pairs = [
                anchors_mod.split_item_ts(str(x)) for x in (grp.get("items") or [])
                if str(x).strip()
            ]
            items: list[str] = []
            item_ts: list[float] = []
            for ts, text in pairs:
                if not text:
                    continue
                items.append(_fix(text))
                item_ts.append(snap_anchor_ts(ts, chunks) if ts else 0.0)
            if not items:
                continue
            # 模型整组都漏写时间码：从逐块要点继承，别整段退回「猜 cover_ts」
            if not any(t > 0 for t in item_ts):
                item_ts = _infer_item_times(items, chunks)
            cover = parse_cover_ts(grp.get("cover_ts"), meta.duration)
            fallback = snap_anchor_ts(cover, chunks) if cover else 0.0
            groups.append({
                "title": gtitle,
                "items": items,
                "items_ts": item_ts,
                # 配图优先用第一条要点的真实时刻，模型猜的 cover_ts 只作兜底
                "ts": next((t for t in item_ts if t > 0), fallback),
                "cover_ts": cover,
            })
        if not groups:
            continue
        topic_cover = parse_cover_ts(item.get("cover_ts"), meta.duration)
        # 一级主题的代表画面：模型挑的「信息量最足」那一刻，吸附到真实帧。
        # 不能顺手拿第一个二级主题的 ts——那样两张图会是同一帧，白占位置。
        topic_ts = snap_anchor_ts(topic_cover, chunks) if topic_cover else 0.0
        if topic_ts <= 0:
            topic_ts = next((g["ts"] for g in groups if g["ts"] > 0), 0.0)
        outline.append({
            "title": title,
            "summary": _fix(item.get("summary")),
            "ts": topic_ts,
            "cover_ts": topic_cover,
            "groups": groups,
        })

    takeaways = [
        _fix(x) for x in (data.get("takeaways") or []) if str(x).strip()
    ]

    problems: list[Problem] = []
    for raw in (data.get("problems") or [])[:3]:
        if not isinstance(raw, dict):
            continue
        statement = _fix(raw.get("statement"))
        key = _fix(raw.get("key"))
        steps = [_fix(x) for x in (raw.get("steps") or []) if str(x).strip()][:6]
        if not statement:
            continue
        problems.append(Problem(
            title=str(raw.get("title") or "").strip()[:24] or "例题",
            statement=statement,
            key=key,
            steps=steps,
            pitfall=_fix(raw.get("pitfall")),
            ts=parse_cover_ts(raw.get("ts"), meta.duration),
        ))

    log.info(
        "结构化大纲 %d 个主题 / %d 条结论 / %d 道例题",
        len(outline), len(takeaways), len(problems),
    )
    return outline, takeaways[:8], problems


def summarize_chunks(
    client: LLMClient | None,
    chunks: list[Chunk],
    meta: VideoMeta,
    concurrency: int = 4,
    on_progress: Callable[[float, str], None] | None = None,
    cancel: CancelFn | None = None,
    note_detail: str = "standard",
) -> None:
    if client is None:
        for chunk in chunks:
            _fallback_chunk(chunk)
        if on_progress:
            on_progress(100.0, "未配置模型，已用原文摘要代替")
        return

    total = len(chunks)
    done = 0
    errors = 0

    def work(chunk: Chunk) -> None:
        if cancel and cancel():
            raise CancelledError("任务已取消")
        # 音画锚定材料：按时间排好的「这一帧画面 + 这段时间博主说的话」。
        # 旧任务缓存 / 抽帧失败时 anchors 为空，退回原来「讲解 + 画面文字」两坨文本。
        if chunk.anchors:
            material = anchors_mod.anchor_material(chunk, max_chars=3600)
        else:
            screen = "\n".join(frame_caption.chunk_screen_lines(chunk, with_ts=True))
            material = (
                f"【讲解】\n{chunk.text[:2400]}\n\n"
                f"【画面】\n{(screen or chunk.ocr_text or '（无画面文字）')[:800]}"
            )
        prompt = _CHUNK_TMPL.format(
            title=meta.title,
            start=fmt_ts(chunk.start),
            end=fmt_ts(chunk.end),
            material=material,
        )
        detailed = note_detail == "detailed"
        data = client.complete_json(
            _SYSTEM + _density_note(note_detail), prompt,
            max_tokens=1600 if detailed else 1024,
        )
        chunk.title = _fix(data.get("title") or _first_sentence(chunk.text))[:30]
        chunk.summary = _fix(data.get("summary"))
        chunk.bullets = [
            _fix(b) for b in (data.get("bullets") or []) if str(b).strip()
        ][:10 if detailed else 6]
        # 每条要点讲的是哪一帧：模型照抄材料里的时间码，就近吸附到真实帧，
        # 长度对不上时用块内锚点顺序兜底，别让位置信息整块丢掉。
        raw_ts = list(data.get("bullet_ts") or [])
        times = [
            snap_anchor_ts(anchors_mod.parse_ts(t), chunks)
            for t in raw_ts[:len(chunk.bullets)]
        ]
        while len(times) < len(chunk.bullets):
            idx = len(times)
            fallback = (
                chunk.anchors[min(idx, len(chunk.anchors) - 1)].frame_ts
                if chunk.anchors else 0.0
            )
            times.append(fallback)
        chunk.bullet_ts = times
        chunk.keywords = [str(k).strip() for k in (data.get("keywords") or []) if str(k).strip()][:5]
        if not chunk.summary:
            _fallback_chunk(chunk)

    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        futures = {pool.submit(work, c): c for c in chunks}
        for future in as_completed(futures):
            if cancel and cancel():
                raise CancelledError("任务已取消")
            done += 1
            try:
                future.result()
            except CancelledError:
                raise
            except Exception as exc:  # 单块失败不拖垮整个任务
                errors += 1
                log.warning("块 %d 总结失败：%s", futures[future].index, exc)
                _fallback_chunk(futures[future])
            if on_progress:
                on_progress(done / total * 100.0, f"总结内容块 {done}/{total}")

    if errors:
        log.warning("共 %d 个块降级为原文摘要", errors)


def build_chapters(
    client: LLMClient | None,
    chunks: list[Chunk],
    meta: VideoMeta,
) -> list[Chapter]:
    if not chunks:
        return []
    if client is None or len(chunks) <= 2:
        return _fallback_chapters(chunks)

    listing = "\n".join(
        f"[{c.index}] {fmt_ts(c.start)} {c.title or _first_sentence(c.text)}：{c.summary or c.text[:80]}"
        for c in chunks
    )
    data = client.complete_json(
        _SYSTEM,
        _CHAPTER_TMPL.format(title=meta.title, n=len(chunks), listing=listing, last=len(chunks) - 1),
        max_tokens=2048,
    )
    raw = data.get("chapters") or []
    chapters: list[Chapter] = []
    for item in raw:
        try:
            s = int(item.get("start_chunk", 0))
            e = int(item.get("end_chunk", s))
        except (TypeError, ValueError):
            continue
        s = max(0, min(s, len(chunks) - 1))
        e = max(s, min(e, len(chunks) - 1))
        members = chunks[s: e + 1]
        if not members:
            continue
        chapters.append(
            Chapter(
                index=len(chapters),
                title=str(item.get("title") or members[0].title or "未命名章节")[:30],
                start=members[0].start,
                end=members[-1].end,
                chunk_indexes=[c.index for c in members],
                summary=_fix(item.get("summary")),
                bullets=[b for c in members for b in c.bullets][:8],
            )
        )

    covered = {i for ch in chapters for i in ch.chunk_indexes}
    missing = [c.index for c in chunks if c.index not in covered]
    if missing:
        log.warning("章节未覆盖块 %s，已补入末尾章节", missing)
        if chapters:
            tail = [c for c in chunks if c.index in missing]
            chapters[-1].chunk_indexes.extend(missing)
            chapters[-1].end = max(chapters[-1].end, tail[-1].end)

    return chapters or _fallback_chapters(chunks)


def finalize(
    client: LLMClient | None,
    meta: VideoMeta,
    chapters: list[Chapter],
    chunks: list[Chunk],
    speechless: bool = False,
) -> tuple[str, list[str], list[dict[str, str]]]:
    """返回 (一句话总结, 核心要点, 术语表)。

    speechless=True 表示这次没拿到任何语音句子（无音轨 / 纯配乐 / 人声太轻），
    笔记是按画面内容整理的——文案要说清楚，别让人误以为"模型没配好"。
    """
    if client is None or not chapters:
        # 没有模型时不要假装能做摘要：把整段正文当"要点"列出来最难读。
        # 改成给章节时间轴 —— 至少能当目录用，也便于点回原视频。
        head = (chunks[0].summary if chunks else "") or (chunks[0].ocr_text if chunks else "") or meta.title
        if speechless and client is None:
            one = ("（本次音频没有识别到人声，也未配置模型；"
                   f"以下章节按画面内容整理）{head[:60]}")
        elif speechless:
            one = f"（本次音频没有识别到人声，以下章节按画面内容整理）{head[:60]}"
        elif client is None:
            one = f"（未配置模型，以下为自动切分的章节；配置模型后会生成真正的摘要）{head[:60]}"
        else:
            one = f"（本次没有拿到可整理的正文，以下仅为标题占位）{meta.title}"
        highlights = [
            f"{fmt_ts(ch.start)}  {ch.title}" for ch in chapters[:10] if ch.title
        ] or [meta.title]
        return one, highlights, []

    listing = "\n".join(
        f"{ch.index + 1}. {ch.title}（{fmt_ts(ch.start)}）：{ch.summary or '；'.join(ch.bullets[:3])}"
        for ch in chapters
    )
    data = client.complete_json(
        _SYSTEM,
        _GLOBAL_TMPL.format(
            title=meta.title,
            uploader=meta.uploader,
            duration=fmt_ts(meta.duration),
            listing=listing,
        ),
        max_tokens=2048,
    )
    one_liner = str(data.get("one_liner") or "").strip()
    highlights = [
        _fix(h) for h in (data.get("highlights") or []) if str(h).strip()
    ][:10]
    glossary = []
    for item in data.get("glossary") or []:
        term = str(item.get("term") or "").strip()
        explain = str(item.get("explain") or "").strip()
        if term and explain:
            glossary.append({"term": _fix(term), "explain": _fix(explain)})
    return one_liner, highlights, glossary[:15]


# ---- 无模型时的降级实现 ----
def _split_sentences(text: str, max_len: int = 80) -> list[str]:
    """按标点切句，切不断的长串按长度硬切。

    必须做硬切：B站 AI 字幕和 ASR 的文本几乎没有标点，
    整块十几分钟的话就是一条超长串。只按 "。" 切会让
    bullets 变成"整篇正文"，笔记里"核心要点"直接变成一坨（真实踩过）。

    英文句点只在两种情况下算断句：后面是空格/结尾，或后面紧跟大写字母
    （ASR 常丢空格）。且句点前至少两个字母 —— 否则 3.14、U.S.A. 会被切烂。
    """
    import re as _re  # noqa: PLC0415

    pieces = _re.split(
        r"[。！？!?…；;\n]+|(?<=[A-Za-z]{2})\.(?=\s|$)|(?<=[a-z]{2})\.(?=[A-Z])",
        text,
    )
    out: list[str] = []
    for piece in pieces:
        piece = piece.strip()
        while len(piece) > max_len:
            out.append(piece[:max_len])
            piece = piece[max_len:]
        if len(piece) >= 8:
            out.append(piece)
    return out


def _fallback_chunk(chunk: Chunk) -> None:
    text = chunk.text.strip()
    if not text:
        # 没有语音的视频块正文为空，靠画面文字/画面要点当内容
        text = (chunk.ocr_text or "").strip()
    if not text:
        try:
            lines = [ln.strip() for ln in frame_caption.chunk_screen_lines(chunk) if ln.strip()]
        except Exception:               # 画面要点还没生成，别在这里炸
            lines = []
        text = "；".join(lines)[:400]
    sentences = _split_sentences(text)
    chunk.title = chunk.title or (sentences[0][:20] if sentences else text[:20]) or "内容片断"
    chunk.summary = chunk.summary or (text[:200] + ("…" if len(text) > 200 else ""))
    if not chunk.bullets:
        chunk.bullets = sentences[:4]
    # 降级重造要点后，旧的时间码已经对不上了，宁可不带也别张冠李戴
    if len(chunk.bullet_ts) != len(chunk.bullets):
        chunk.bullet_ts = []


def _fallback_chapters(chunks: list[Chunk], size: int = 5) -> list[Chapter]:
    chapters: list[Chapter] = []
    for i in range(0, len(chunks), size):
        group = chunks[i: i + size]
        chapters.append(
            Chapter(
                index=len(chapters),
                title=group[0].title or _first_sentence(group[0].text),
                start=group[0].start,
                end=group[-1].end,
                chunk_indexes=[c.index for c in group],
                summary=group[0].summary,
                bullets=[b for c in group for b in c.bullets][:8],
            )
        )
    return chapters


def is_foreign(text: str, sample: int = 4000) -> bool:
    """判断材料主体是不是外语（非中文）。

    只看汉字占比会误判中英混排的技术视频，所以同时要求拉丁字母有足够量：
    汉字占比 < 15% 且拉丁字母 >= 200 个，才认定是外语素材。
    """
    import re as _re  # noqa: PLC0415

    head = (text or "")[:sample]
    if not head:
        return False
    cjk = len(_re.findall(r"[\u4e00-\u9fff]", head))
    latin = len(_re.findall(r"[A-Za-z]", head))
    return cjk < len(head) * 0.15 and latin >= 200


def _first_sentence(text: str) -> str:
    for sep in ("。", "！", "？", "，", ","):
        if sep in text[:40]:
            return text.split(sep)[0].strip()[:20]
    return text[:20].strip() or "内容片断"
