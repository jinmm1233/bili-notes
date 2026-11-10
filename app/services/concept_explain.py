"""概念释义：判断这个词是不是视频重点讲的，并给它写一段自足的说明。

为什么要有这一层
----------------
概念页原本只有"各集怎么讲"，点一下跳到笔记里——但笔记里那行解释经常是废话：
「材料中记为E」「链接器执行的第二步」「材料所述使用链接器的原因之一」。
这些话描述的是**笔记怎么写的**，不是**概念是什么**，读者还是不知道这个词啥意思。

同时收录也太宽：顺带一词（happens to、involve）、举例背景里的地名店名、
只出现一次的普通词，全都占了概念位。用户明确要求：
  - 视频里**给了定义或解释**的才收；
  - 只是被提到名字、视频没解释的，不收。

所以这里做两件事：
  1. **判定**（core）：这个概念在视频里到底有没有被讲透；
  2. **释义**（explain）：用一句自足的话讲清它是什么，不依赖"材料中…"这种写法。

判定不能只靠规则。实测过"术语在正文出现几次"这个信号——没用：
「文件」在正文里出现 116 次也只是一句带过的泛词，「happen to」只出现 0 次
却是这一节真正要教的语法点。次数分不出重点，只分得出词频。
所以重点与否交给模型判断，规则只做兜底。

模型没配 Key 时的降级
--------------------
没模型就退到"笔记表里已有的解释"，并用规则把明显不行的剔掉
（元叙述开头、解释太短）。功能不会瘫，只是没有 AI 那么准。

缓存落在 data_dir/concept_explain.json，键是概念的归一化名。
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Iterable

from ..core.config import Settings
from ..core.llm import LLMClient
from . import concepts as concept_svc
from .concepts import clean_term, name_key

log = logging.getLogger(__name__)

CACHE_FILE = "concept_explain.json"

# 一次送多少个概念给模型。太多容易漏、也贵；太少来回次数多。
# 实测 10 个一批约 7 秒，142 个概念一次能跑完。
BATCH = 10
# 单次接口最多跑几批，防止用户点一次等太久
MAX_BATCHES = 15

# 解释里出现这些词开头，说明它在描述"笔记怎么写的"，不是在解释概念
_META_OPEN = re.compile(
    r"^\s*(材料中|材料所述|材料给出|材料里|材料中作为|视频中|视频里|笔记中|"
    r"上文|上文所述|本课|本视频中|材料)",
)
# 解释太短基本等于没解释（"一种矩阵"、"链接器"）
_TOO_SHORT = 6


def _path(settings: Settings) -> Path:
    return Path(settings.data_dir) / CACHE_FILE


def load(settings: Settings) -> dict[str, dict[str, Any]]:
    """读缓存。文件坏了就当空，不让概念页整个打不开。"""
    path = _path(settings)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        log.warning("概念释义缓存损坏，按空处理：%s", path)
        return {}
    rows = data.get("concepts") if isinstance(data, dict) else data
    out: dict[str, dict[str, Any]] = {}
    for key, row in (rows or {}).items():
        if isinstance(row, dict):
            out[str(key)] = row
    return out


def save(settings: Settings, rows: dict[str, dict[str, Any]]) -> None:
    path = _path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": 1, "updated": time.time(), "concepts": rows}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


# ---------------------------------------------------------------- 上下文取材
def _note_explanations(settings: Settings, name: str) -> list[tuple[str, str]]:
    """这个概念在各篇笔记的「关键概念」表里都写了什么。返回 [(笔记标题, 解释)]。"""
    out: list[tuple[str, str]] = []
    root = Path(settings.output_dir)
    if not root.is_dir():
        return out
    for folder in sorted(root.iterdir()):
        md_file = folder / "notes.md"
        if not md_file.is_file():
            continue
        try:
            md = md_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for term, desc in concept_svc.parse_concepts(md):
            if name_key(term) != name_key(name):
                continue
            if desc:
                out.append((folder.name, desc))
            break
    return out


def _note_context(settings: Settings, name: str, limit: int = 2) -> list[str]:
    """取讲到这个概念的笔记里的上下文，给模型当判定的依据。

    优先取「结论 / 核心要点 / 一句话总结」这几段——笔记里最有信息量的部分。
    但不是每篇笔记都有这些段（走章节结构的笔记就没有），所以取不到时要退回
    **正文里提到它的那几句**，否则送给模型的上下文是空的，模型只能靠名字瞎猜。

    实测踩过这个坑：「布尔代数」「位向量」「广泛性焦虑症」都在只有章节结构的笔记里，
    上下文一空就被判成"只是提到"，而它们明明是视频认真讲过的。
    """
    root = Path(settings.output_dir)
    if not root.is_dir():
        return []
    key = name_key(name)
    out: list[str] = []
    for folder in sorted(root.iterdir()):
        md_file = folder / "notes.md"
        if not md_file.is_file():
            continue
        try:
            md = md_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if not any(name_key(t) == key for t, _ in concept_svc.parse_concepts(md)):
            continue
        part = _pick_context(md, name)
        if part:
            out.append(f"《{folder.name}》\n{part}")
        if len(out) >= limit:
            break
    return out


def _pick_context(md: str, name: str) -> str:
    """从一篇笔记里挑一段最能说明这个概念的上下文。"""
    # 1) 重点段里提到了它 —— 整段都拿
    for section in _key_sections(md):
        if _mentions(section, name):
            return section.strip()[:900]
    # 2) 重点段没提到，但正文里有 —— 拿提到它的那几句
    hits = _sentences_around(md, name)
    if hits:
        return hits[:700]
    # 3) 都没有 —— 至少把全文要点段（可能不含这个词）给一点，让模型有背景
    sections = _key_sections(md)
    return (sections[0].strip()[:500]) if sections else ""


# 概念名常带括号注释：「MMU（内存管理单元）」在正文里往往只写「MMU」。
# 拿全名去正文里找会找不到（实测：MMU 的上下文一直是空的，被判成非重点）。
_BRACKET = re.compile(r"[（(][^）)]{0,20}[）)]\s*$")


def _core_forms(name: str) -> list[str]:
    """概念名的各种写法：全名、去括号注释、去括号里剩下的部分。"""
    forms = [name]
    stripped = _BRACKET.sub("", name).strip()
    if stripped and stripped != name:
        forms.append(stripped)
    # 「MMU（内存管理单元）」→ 也要能用「内存管理单元」找
    inner = re.search(r"[（(]([^）)]{2,20})[）)]", name)
    if inner:
        forms.append(inner.group(1))
    return [f for f in dict.fromkeys(forms) if f and len(f) >= 2]


def _mentions(text: str, name: str) -> bool:
    """正文里有没有提到这个词（按各种写法试）。"""
    return any(form in text for form in _core_forms(name))


def _sentences_around(md: str, name: str, radius: int = 1, cap: int = 4) -> str:
    """抓出正文里提到这个词的句子，连带前后几句。

    按换行断句：B 站字幕与 ASR 的文本几乎没有标点，按句号断会切不出东西。
    头部 frontmatter 整段跳过——里面是标题、时长、来源这些元数据，
    跟概念讲没讲过没关系，抓进来只会误导模型。
    """
    if not name:
        return ""
    lines = [ln.strip() for ln in _strip_frontmatter(md).splitlines()]
    lines = [ln for ln in lines if ln]
    forms = _core_forms(name)
    picked: list[str] = []
    for i, line in enumerate(lines):
        if not any(form in line for form in forms):
            continue
        lo = max(0, i - radius)
        hi = min(len(lines), i + radius + 1)
        chunk = " ".join(lines[lo:hi])
        if chunk and chunk not in picked:
            picked.append(chunk)
        if len(picked) >= cap:
            break
    return "\n".join(picked)


def _strip_frontmatter(md: str) -> str:
    """去掉开头的 YAML frontmatter。"""
    text = md or ""
    if not text.startswith("---"):
        return text
    end = text.find("\n---", 3)
    if end < 0:
        return text
    return text[end + 4:]


_SECTION = re.compile(r"^##\s*(结论|核心要点|一句话总结)\s*$", re.M)
_NEXT_H2 = re.compile(r"^##\s", re.M)


def _key_sections(md: str) -> list[str]:
    """抽出「结论 / 核心要点 / 一句话总结」这几段——笔记里最有信息量的部分。"""
    out: list[str] = []
    for match in _SECTION.finditer(md or ""):
        rest = md[match.end():]
        nxt = _NEXT_H2.search(rest)
        out.append(rest[: nxt.start()] if nxt else rest)
    return out


# ---------------------------------------------------------------- 模型判定
_SYSTEM = """你在给一个学习笔记软件整理「概念索引」。用户的要求很具体。

一、判断每个词是不是**视频真正讲过的重点概念**（core）。

先看「笔记里关于它的内容」里给了什么：
- 有一整句在**解释它是什么**（定义了它、讲了它的原理/区别/用法）
  → core=true
- 只在句子里**被提到名字**，没有任何解释
  → core=false
- 属于举例背景、地名店名、人名、软件界面操作（安装到 C 盘、点 Finish）
  → core=false
- 口语词、虚词、英文语法词（happens to、involve、art 这类英文单词）
  → core=false

**判断依据是"上下文里有没有解释它"，不是"它出现了几次"。**
一集视频专门讲一个概念，它当然只出现一次，但它是这一集最该记住的词。

二、给每个词写一句**自足的解释**（explain），说清它是什么。
- 直接讲这个概念本身，不要写"材料中…""视频里提到…""笔记里写的是…"这种
  描述笔记而不描述概念的话。
- 用你自己的话把概念讲明白，不要照抄下面的"笔记里的内容"。
- 一到两句，40-80 字。公式用 $LaTeX$。
- core=false 的词，explain 就写它字面上是什么，一句话即可。

只输出 JSON：{"items":[{"name":"原样返回这个词","core":true,"explain":"解释"}]}"""


def _build_prompt(names: list[str], contexts: dict[str, list[str]]) -> str:
    lines: list[str] = []
    for name in names:
        chunk = [f"- {name}"]
        ctx = contexts.get(name_key(name)) or []
        if ctx:
            chunk.append("  笔记里关于它的内容：")
            for part in ctx:
                chunk.append("  " + part.replace("\n", "\n  "))
        lines.append("\n".join(chunk))
    return "请判断并解释下面这些词：\n\n" + "\n".join(lines)


def _parse_items(data: Any) -> dict[str, dict[str, Any]]:
    """模型输出容错：可能返回 {"items":[...]}、直接 [...]，或者字段名不一致。"""
    rows = data.get("items") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = clean_term(str(row.get("name") or ""))
        if not name:
            continue
        explain = re.sub(r"\s+", " ", str(row.get("explain") or "")).strip()
        out[name_key(name)] = {
            "name": name,
            "core": bool(row.get("core")),
            "explain": explain[:400],
        }
    return out


# ---------------------------------------------------------------- 硬规则兜底
# 光靠提示词判不准：模型有时把 art、happens to、C盘 全判成重点，
# 有时又把 倍乘、化零顺序 判成非重点。所以模型只给"倾向"，最后由规则定夺。
#
# 两条硬规则，都是从真实数据里踩出来的：
#  1. 英文单词/短语（TED 演讲里的语法点）不是知识点，除非它是专有名词；
#  2. 软件界面操作词、地名店名这类永远不是知识点。
_TRIVIAL_PAT = re.compile(
    r"^(C盘|D盘|安装包|Finish|Next|OK|Yes|No|向导|默认设置|"
    r"art|arts|happen to|happens to|involve|involves|"
    r"not just…but to…|not…any more|"
    r"shell程序|init进程|"
    r"方法对比|方法比较|对比|比较|总结|概述|其他|其它)$"
)


def _is_trivial_name(name: str) -> bool:
    """明显不是知识点的词。命中即判非重点，不再交给模型。"""
    return bool(_TRIVIAL_PAT.match(clean_term(name)))


# 已经被分类系统判成"非知识点"的类别（地点、鸡汤、常识操作、例题步骤…）
# 这些根本不是知识概念，不该出现在概念页的重点里。
# 复用 concept_tags 的分类结果，不重新发明一套判断。
_NON_KNOWLEDGE_CATS = frozenset({
    "place", "soup", "trivial", "step", "relation", "artifact",
})


def _known_cat(settings: Settings, name: str) -> str:
    """查这个概念已有的分类（没有就空串）。"""
    try:
        from . import concept_tags
        tags = concept_tags.load(settings)
        row = tags.get(name_key(name)) or {}
        return str(row.get("cat") or "")
    except Exception:                          # pragma: no cover
        return ""


def _is_non_knowledge(settings: Settings, name: str) -> bool:
    """这个概念是不是已被判成非知识点（地点/鸡汤/常识操作…）。

    这条比任何正则都准：分类系统已经用模型+规则判过一轮了，
    而且用户可以在概念页上手动纠正过（手动结论优先级最高）。
    旅行笔记里的「白堤」「乌龟潭」「佛手桥」都归在 place，
    装教程里的「C盘」「Finish」归在 trivial——它们压根不是知识概念。
    """
    return _known_cat(settings, name) in _NON_KNOWLEDGE_CATS


def _apply_hard_rules(name: str, core: bool, explain: str,
                      context: str) -> tuple[bool, str]:
    """把模型的判定过一遍硬规则。

    规则只做减法（把明显错的判成非重点），不做加法——
    宁可少收几个，也不要把 art、C盘 这种词放进概念页。
    """
    if _is_trivial_name(name):
        return False, explain
    # 解释整句都在说"材料没讲"→ 视频没真讲它
    if explain and re.search(r"(未给出|没有给出|未提及|不详|未说明|材料未)", explain):
        return False, explain
    # 上下文里只有"提到"、没有任何解释，且解释本身也很短 → 不算讲过
    if not core and len(explain) < _TOO_SHORT and context:
        return False, explain
    return core, explain


# ---------------------------------------------------------------- 上下文证据
# 模型的判定会漂：同一批概念，分两批跑下来「MMU」「子句约束构造」
# 一轮判重点、一轮判非重点。原因是它只看得到片段，判定标准靠不住。
#
# 这里用**笔记原文**做二次裁决，不问模型：
#   笔记里如果有一整句在解释这个词（解释了它是什么、它的原理/区别/用法），
#   那视频确实讲过它 → 判重点；
#   只是被提到名字（"顺带提一下 Git"）→ 判非重点。
# 这个判据只依赖笔记本身，跑多少次都一样。
_EXPLAIN_HINT = re.compile(
    r"(是指|指的是|是一种|定义为|用来|用于|是将|就是|表示|全称|简称|分为|"
    r"包含|满足|条件是|由.+组成|本质|即|指的是|"
    r"[^，。；]{1,10}的[^，。；]{2,}(工具|方法|结构|机制|过程|运算|技术|"
    r"单元|部件|策略|模型|算法|体系|问题|概念|规则|标准|集合|序列|"
    r"单位|形式|类型|方式|手段|能力))"
)


def _explained_in_body(md: str, name: str) -> bool:
    """笔记正文里有没有在讲这个概念。

    这条判据很重要：**不是每篇笔记都有「关键概念」表**——走结构化大纲的笔记
    概念写在正文的小节里（「#### MMU的作用」「构造增广矩阵[A|E]…」）。
    只看概念表会把这些真概念全判成非重点（实测「MMU」「单位矩阵」被误杀）。

    判法：正文里提到它的那些行，如果本身就是在"讲"它（有解释谓语、
    或者它自己是个小标题），就算讲过。

    但要排除**列举性上下文**——旅行笔记里「白堤」「乌龟潭」「佛手桥」
    会被列进路线，但那是"清单"不是"讲解"。判据是：这一句在讲别的什么，
    它只是清单里的一项。
    """
    forms = _core_forms(name)
    if not forms:
        return False
    body = _strip_frontmatter(md)
    # 概念表那一段跳过——那是词条列表，不是讲解
    body = re.sub(r"^##\s*关键概念\s*$[\s\S]*?(?=^##\s|\Z)", "", body, flags=re.M)
    for raw in body.splitlines():
        line = raw.strip()
        if not line or line.startswith("|") or line.startswith("!["):
            continue
        if not any(form in line for form in forms):
            continue
        # 去掉 markdown 装饰后判断这句是不是在讲它
        text = re.sub(r"[#>*`\-\[\]()!]", "", line).strip()
        if len(text) < _TOO_SHORT:
            continue
        # 小标题（如「MMU的作用」）本身就是这一节在讲它
        if raw.lstrip().startswith("#"):
            if not _is_listing_context(text):
                return True
            continue
        if _is_listing_context(text):
            continue
        if _EXPLAIN_HINT.search(text):
            return True
        # 整句在讲它（够长、有谓语结构）
        if len(text) >= 18 and ("，" in text or ":" in text or "：" in text):
            return True
    return False


# 列举性上下文：句子主体是别的东西，这个词只是清单里的一项。
# 「西湖核心区打卡路线的一部分，从龙翔桥出发后沿白堤游览」——在讲路线，不在讲白堤。
_LISTING = re.compile(
    r"(路线|行程|景点|节点|打卡|经过|途经|游览|走法|安排|"
    r"安装|配置|设置|下载|点击|按钮|界面|菜单|选项|步骤之一)"
)


def _is_listing_context(text: str) -> bool:
    """这句是在列举/流程里提到它，而不是在解释它。"""
    # 词出现在"…的一部分""…之一"里 → 列举
    if re.search(r"[^，。；]{0,6}的(一部分|之一|环节|路线|节点|景点)", text):
        return True
    # 句子里有明显的流程/清单词，且这个词本身没被解释
    if _LISTING.search(text) and not _EXPLAIN_HINT.search(text):
        return True
    return False


def _explained_in_body_any(settings: Settings, name: str) -> bool:
    """所有笔记里，正文有没有在讲这个概念。任一篇讲了就算讲了。"""
    root = Path(settings.output_dir)
    if not root.is_dir():
        return False
    for folder in sorted(root.iterdir()):
        md_file = folder / "notes.md"
        if not md_file.is_file():
            continue
        try:
            md = md_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if _explained_in_body(md, name):
            return True
    return False


def _note_explains_concept(explanations: list[str]) -> bool | None:
    """笔记的「关键概念」表里那句解释，是"在讲它"还是"只是提到它"。

    返回 True/False；连解释都没有就返回 None（交给模型判）。

    这条判据只依赖笔记原文，不问模型——所以跑多少次结论都一样，
    不会出现同一批概念分两轮跑、一轮重点一轮非重点的漂移。

    **逐条看，不能整批否**：一个词可能在好几篇笔记里出现，
    其中一篇写得认真（"求逆矩阵的方法，包含倍乘、倍加和对换"），
    另一篇只写了"材料中记为E"。只要有一篇真的在解释它，就算讲过。
    （踩过这个坑：「初等行变换法」「单位矩阵」「MMU」都被整批误杀。）

    元叙述开头也要往下看：
    「材料中记为E，是左半部分经初等行变换后要化成的目标」——
    前面半句是在说材料，但整句确实讲清了这个概念 → 算讲过。
    反过来「材料中未给出更具体定义」——整句都在说材料没讲 → 不算。
    """
    real = [(d or "").strip() for d in explanations if (d or "").strip()]
    if not real:
        return None
    for text in real:
        if len(text) < _TOO_SHORT:
            continue
        # 「XX的一部分」「XX的步骤之一」——只是说它归在哪，没讲它是什么
        if re.search(r"[^，。；]{2,}的(一部分|之一|环节|步骤|组成|成员|下属)", text):
            continue
        body = text
        if _META_OPEN.match(text):
            # 剥掉"材料中…"这类前缀，看剩下的部分有没有在讲概念
            body = re.sub(r"^[^，。；]{0,12}?(材料|视频|笔记|上文|本课|本视频)[^，。；]{0,8}?[，,]", "", text)
            body = body.strip() or text
            # 剥完还是"没给定义"这类，那就是真没讲
            if re.search(r"(未给出|没有给出|未提及|没提|不详|未说明)", body):
                continue
        if _EXPLAIN_HINT.search(body):
            # 「指学科」这种残缺句式不是真解释（art 的解释就是这样）
            if len(body) <= _TOO_SHORT:
                continue
            return True
        if "：" in body or "，" in body:
            return True
    return False


# ---------------------------------------------------------------- 最终裁决
def _decide_core(name: str, model_core: bool, explain: str,
                  table_verdict: bool | None, body_says: bool,
                  non_knowledge: bool = False) -> bool:
    """定夺这个概念算不算"视频重点讲过的"。

    证据优先级：
      0. **分类系统**（non_knowledge）：已经被判成地点/鸡汤/常识操作 →
         根本不是知识概念，直接出局。用户手动纠正过的结论也在这里生效。
      1. **正文证据**（body_says）：笔记正文里真的在讲它 → 重点。
         「MMU」在正文里有专门小节「#### MMU的作用」，必须在重点里。
      2. **概念表证据**（table_verdict）：那行解释本身在解释概念 → 重点；
         整行都在说材料没讲 → 非重点。
      3. **模型判定**（model_core）：前两路都说不清时的兜底。
    """
    if _is_trivial_name(name):
        return False
    if non_knowledge:
        return False
    if body_says:
        return True
    if table_verdict is not None:
        return table_verdict
    return model_core


# ---------------------------------------------------------------- 无模型时的降级
def _rule_explain(settings: Settings, name: str) -> tuple[bool, str]:
    """没配模型时的降级判断：沿用笔记表里已有的解释，并挡掉明显不行的。

    core 的判据只能用"解释是否像样"：能写出自足解释的，才算视频讲过。
    """
    rows = _note_explanations(settings, name)
    best = ""
    for _title, desc in rows:
        if len(desc) > len(best):
            best = desc
    best = best.strip()
    if not best or len(best) < _TOO_SHORT:
        return False, ""
    if _META_OPEN.match(best):
        return False, ""
    return True, best


def fallback(settings: Settings, names: Iterable[str]) -> dict[str, dict[str, Any]]:
    """没模型（或模型调用失败）时的兜底：只留解释像样的。"""
    out: dict[str, dict[str, Any]] = {}
    for name in names:
        core, explain = _rule_explain(settings, name)
        core, explain = _apply_hard_rules(name, core, explain, "")
        if core and explain:
            out[name_key(name)] = {"name": name, "core": True, "explain": explain,
                                   "src": "rule"}
    return out


# ---------------------------------------------------------------- 对外入口
def pending(settings: Settings, items: list[dict[str, Any]]) -> list[str]:
    """还没判过重点度、也没缓存释义的概念名。"""
    cache = load(settings)
    out: list[str] = []
    for item in items:
        name = str(item.get("name") or "")
        if not name:
            continue
        row = cache.get(name_key(name))
        if row and row.get("explain"):
            continue
        out.append(name)
    return out


def explain(settings: Settings, client: LLMClient | None,
            items: list[dict[str, Any]], names: Iterable[str] | None = None,
            force: bool = False) -> dict[str, Any]:
    """给概念补重点标记和自足释义。

    items 是概念索引的条目（要带 name / note_count / cards / notes）。
    names 限定只处理这几个；不传就处理全部还没判过的。

    返回 {"done": n, "core": n, "skipped": n, "reason": "..."}。
    """
    cache = load(settings)
    if names:
        wanted = [clean_term(n) for n in names if clean_term(n)]
    elif force:
        # 强制重判：不管判过没有，全部重来一遍（提示词或取材逻辑改了之后用）
        wanted = [clean_term(str(i.get("name") or "")) for i in items]
        wanted = [n for n in wanted if n]
    else:
        wanted = pending(settings, items)

    # 已经判过的直接跳过，别重复花钱；force 时不跳过
    todo: list[str] = []
    for name in wanted:
        row = cache.get(name_key(name))
        if force or not row or not row.get("explain"):
            todo.append(name)
    if not todo:
        return {"done": 0, "core": 0, "skipped": len(wanted), "reason": "nothing"}

    if client is None:
        got = fallback(settings, todo)
        if got:
            cache.update(got)
            save(settings, cache)
        return {"done": len(got), "core": len(got), "skipped": len(todo) - len(got),
                "reason": "no_key"}

    contexts: dict[str, list[str]] = {}
    note_exps: dict[str, list[str]] = {}
    body_says: dict[str, bool] = {}
    for name in todo:
        parts: list[str] = []
        # 笔记表里那句解释（哪怕是"材料中记为E"这种废话）也是线索：
        # 笔记作者认为这个词值得单列一行，本身就是"视频讲过它"的证据
        exps = [desc for _title, desc in _note_explanations(settings, name)]
        note_exps[name_key(name)] = exps
        for title, desc in _note_explanations(settings, name):
            parts.append(f"《{title}》的概念表里写着：{desc}")
        parts.extend(_note_context(settings, name))
        if parts:
            contexts[name_key(name)] = parts
        # 正文证据：走结构化大纲的笔记没有概念表，概念就在正文小节里
        body_says[name_key(name)] = _explained_in_body_any(settings, name)

    done = 0
    core_count = 0
    batches = 0
    for start in range(0, len(todo), BATCH):
        if batches >= MAX_BATCHES:
            break
        batch = todo[start:start + BATCH]
        batches += 1
        try:
            data = client.complete_json(
                _SYSTEM, _build_prompt(batch, contexts), max_tokens=2048)
        except Exception as exc:                # 模型抽风就退回规则，不弄坏索引
            log.warning("概念释义调用失败：%s", exc)
            got = fallback(settings, batch)
            for key, row in got.items():
                cache[key] = row
                done += 1
                if row.get("core"):
                    core_count += 1
            continue
        parsed = _parse_items(data)
        for name in batch:
            row = parsed.get(name_key(name))
            if not row or not row.get("explain"):
                continue
            key = name_key(name)
            ctx = "\n".join(contexts.get(key) or [])
            model_core = bool(row.get("core"))
            explain = str(row.get("explain") or "")
            # 硬规则先压一道（art、C盘 这种直接出局）
            model_core, explain = _apply_hard_rules(name, model_core, explain, ctx)
            # 三路证据定夺：分类 > 正文 > 概念表 > 模型
            core = _decide_core(
                name, model_core, explain,
                _note_explains_concept(note_exps.get(key) or []),
                bool(body_says.get(key)),
                non_knowledge=_is_non_knowledge(settings, name),
            )
            row["core"] = core
            row["explain"] = explain
            row["src"] = "llm"
            row["at"] = time.time()
            cache[key] = row
            done += 1
            if core:
                core_count += 1
        if done:
            save(settings, cache)

    if done:
        save(settings, cache)
    return {"done": done, "core": core_count, "skipped": len(todo) - done,
            "reason": "ok"}


def get(settings: Settings, name: str) -> dict[str, Any]:
    """取某个概念的释义与重点标记。没有就返回空字典。"""
    row = load(settings).get(name_key(name))
    return row or {}
