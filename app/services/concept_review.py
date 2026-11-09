"""概念审核：凡是进概念索引的词，先过这一遍。

为什么必须有一道闸：
  模型很爱"发明概念"。同一件事，它会在笔记表里写「初等行变换法」、在卡片里写
  「初等行变换法优势」「倍乘化单位阵」，再给它们编一个列表里根本不存在的上级
  （「整数规划问题（IP）」「归约示例」）。索引于是散成几十个只出现一次的碎片，
  层级看着像断了——因为父节点压根不存在。

审核只做**可判定**的事，宁可放过也不误杀：
  1. 派生名回流：「X + 优势/原理/实例/结构/限制/设置/顺序/示例…」→ X；
     只要 X 已经在索引里，就说明这个"新概念"其实是 X 的一个侧面。
  2. 近似同一个：字差 ≤2、公共前缀 ≥3、字符组成不同（防「A到B / B到A」）。
  3. 空泛词（方法对比 / 总结 / 概述）：判 trivial，隐藏但可找回。
  4. 上级必须落在**已存在的概念**上：先精确匹配，再剥掉「问题/判定问题/示例/（IP）」
     这类包装，再取包含关系里最长的那个；都不中就清空。
     —— 绝不凭模型一句话造出一个没有内容的父节点。
  5. 没给上级的，如果名字本身就是「X判定问题」「广泛性焦虑症」这种 X 的下一层，
     自动挂到 X 下面（前缀/后缀包含 + 至少多两个字）。

两处调用：
  - 生成时：`review_glossary`（笔记的关键概念表）+ `review_card_concepts`（卡片的
    concept 字段），新内容进库前就被拦一道；
  - 存量时：`tidy`（概念页的「整理」），把历史碎片合并、层级重挂。
"""

from __future__ import annotations

import difflib
import logging
import re
import time
from typing import Any, Iterable

from ..core.config import Settings
from ..core.llm import LLMClient
from . import concept_tags as tag_svc
from . import concepts as concept_svc
from .concepts import clean_term, name_key as _key

log = logging.getLogger(__name__)

# 派生名的"抽象后缀"：这些词挂在概念后面，说明它只是这个概念的一个侧面，不是新概念
TAIL_MERGE = (
    "优势", "优点", "缺点", "劣势", "原理", "特点", "特征", "结构", "限制", "局限",
    "设置", "配置", "验证", "校验", "顺序", "次序", "先后", "流程", "细节", "要点",
    "使用", "应用", "说明", "方法", "意义", "作用",
    "对比", "比较", "区别", "差异", "实例", "示例", "例题", "总结", "概述",
    "简介", "介绍", "前言", "注意点", "易错点",
)

# 上级名里的"包装词"：剥掉之后才是它真正指向的那个概念
PARENT_WRAP = (
    "判定问题", "决策问题", "优化问题", "问题", "示例", "例题", "实例", "专题",
    "方法", "原理", "优势", "总结", "概述", "章节", "部分", "相关", "系列",
)

# 一眼就知道没有信息量的词
JUNK = {
    "方法对比", "方法比较", "对比", "比较", "总结", "小结", "概述", "简介",
    "前言", "介绍", "其他", "其它", "无", "略", "知识点", "重点", "考点",
}

_TAIL_CHARS = re.compile(r"[（(][^）)]{0,16}[）)]\s*$")
_ANCHOR = re.compile(r"第[一二三四五六七八九十百\d]+[行列步]|上方|下方|左侧|右侧")
_DIGIT = re.compile(r"\d")


def _count(text: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for ch in text:
        out[ch] = out.get(ch, 0) + 1
    return out


def _strip_once(name: str, tails: Iterable[str]) -> str:
    """剥掉一层包装：先去掉尾部括号注释，再试各个后缀词。"""
    text = clean_term(name)
    bare = _TAIL_CHARS.sub("", text).strip()
    if bare and bare != text:
        return bare
    for tail in tails:
        if text.endswith(tail) and len(text) > len(tail) + 1:
            return text[: -len(tail)].strip()
    return ""


def variants(name: str, tails: Iterable[str], depth: int = 3) -> list[str]:
    """逐层剥壳后的候选名，用于"这就是已有概念"的判定。

    「整数规划问题（IP）」→「整数规划问题」→「整数规划」。
    只有长度 ≥2 的候选才算，剥到单字就没有意义了。
    """
    out: list[str] = []
    cur = clean_term(name)
    for _ in range(depth):
        nxt = _strip_once(cur, tails)
        if not nxt or nxt == cur or _key(nxt) == _key(cur):
            break
        out.append(nxt)
        cur = nxt
    return [c for c in out if len(c) >= 2]


def judge(name: str) -> bool:
    """空泛词：不含任何具体信息，不该占一个概念位。"""
    text = clean_term(name)
    if not text:
        return True
    if text in JUNK:
        return True
    return False


def merge_target(name: str, known: dict[str, str]) -> str:
    """「X + 抽象后缀」→ X。known 是 key → 显示名。"""
    own = _key(name)
    if not own or own in known:
        return ""
    for cand in variants(name, TAIL_MERGE):
        hit = known.get(_key(cand))
        if hit and _key(hit) != own:
            return hit
    return ""


def _common_prefix(a: str, b: str) -> int:
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n


def sim_target(name: str, known: dict[str, str], buckets: dict[str, list[str]] | None = None) -> str:
    """同一个概念的两种写法（倍乘化单位阵 / 倍乘化单位矩阵）。

    防误杀四条：字差 >2 不比、公共前缀 <3 不比、同字异序（A到B / B到A）不比、
    带数字或定位词（第 2 列 / 上方）不比。
    """
    own = _key(name)
    if len(own) < 3:
        return ""
    pool = buckets.get(own[:3]) if buckets else known
    for key in pool or ():
        if key == own or len(key) < 3:
            continue
        if abs(len(key) - len(own)) > 2:
            continue
        if _common_prefix(key, own) < 3:
            continue
        if _DIGIT.search(key + own) or _ANCHOR.search(name):
            continue
        if _count(key) == _count(own):
            continue                       # 同字异序：A到B vs B到A，方向正好相反
        if difflib.SequenceMatcher(None, key, own).ratio() < 0.86:
            continue
        return known[key]
    return ""


def via_parent(name: str, parent: str, known: dict[str, str],
               has_children: bool = False) -> str:
    """名字是**上级概念的碎片 + 限定词**时，并进上级。

    和 same_concept 的分工：那两条靠"名字像"（剥后缀后相等 / 字面近似），
    这条靠"父子关系已知 + 残渣落在父名里"。
      变换顺序   （父 初等行变换法）→ 残渣「变换」在父名里 → 并进去
      行变换使用限制（父 初等行变换法）→ 残渣「行变换」在父名里 → 并进去
      计算次序   （父 矩阵连乘问题）→ 残渣「计算」不在父名里 → 不动
      整数规划判定问题（父 整数规划）→ 名字反过来包住了父名 → 不动（它是下级，不是碎片）

    三条护栏：
    1. 只对**父名**比对，不跟全库比——否则「顺序」「矩阵」这种通用词会把一堆东西吞掉；
    2. 残渣必须 ≥2 字，且必须整段出现在父名里；
    3. 自己已经有下级的节点不并——它是个真的分组节点，压平会丢一整棵子树。
    """
    if has_children:
        return ""
    target = known.get(_key(parent)) or ""
    if not target:
        return ""
    tk = _key(target)
    for cand in [name, *variants(name, TAIL_MERGE)]:
        ck = _key(cand)
        if len(ck) < 2 or ck == tk:
            continue
        if ck in tk:
            return target
    return ""


def via_unique_fragment(name: str, known: dict[str, str]) -> str:
    """连父级都没有时，看残渣是不是**全库只在一个概念名里**出现过。

    「行变换使用限制」→ 残渣「行变换」只出现在「初等行变换法」里 → 并进去。
    全库唯一才敢认；出现两处以上就放弃——「矩阵」「顺序」这种通用词到处都是，
    猜错一次就会把一个真概念吞掉。
    """
    own = _key(name)
    for cand in variants(name, TAIL_MERGE):
        ck = _key(cand)
        if len(ck) < 2:
            continue
        owners = [disp for k, disp in known.items() if k != own and ck in k]
        if len(owners) != 1:
            continue
        target = owners[0]
        if len(ck) / max(1, len(_key(target))) < 0.4:
            continue                       # 残渣只占目标的一小截，八成不是同一件事
        return target
    return ""


def same_concept(name: str, known: dict[str, str]) -> str:
    """这个词是不是已有概念的另一种写法？先看派生后缀，再看字面近似。"""
    return merge_target(name, known) or sim_target(name, known)


def _contained(key: str, known: dict[str, str], hidden: set[str], own: str) -> str:
    """上级名里含某个已有概念（取最长的那个）。"""
    best = ""
    for k, display in known.items():
        if len(k) < 2 or k == own or k in hidden:
            continue
        if k in key and len(k) > len(best):
            best = k
    return known.get(best, "") if best else ""


def resolve_parent(parent: str, self_name: str, known: dict[str, str],
                   hidden: set[str] | None = None) -> str:
    """把模型写的上级对齐到**真实存在**的概念上；对不上就返回空串。

    空串意味着"它是顶层概念"，比挂在一个不存在的父节点下面好——
    后者会让整条子树的层级在页面上断掉。
    """
    text = clean_term(parent or "")
    hidden = hidden or set()
    if not text:
        return ""
    own = _key(self_name)
    pk = _key(text)
    if pk == own:
        return ""
    if pk in known and pk not in hidden:
        return known[pk]
    for cand in variants(text, PARENT_WRAP):
        ck = _key(cand)
        if not ck or ck == own or ck in hidden:
            continue
        if ck in known:
            return known[ck]
        hit = _contained(ck, known, hidden, own)
        if hit:
            return hit
    return _contained(pk, known, hidden, own)


def derive_parent(name: str, known: dict[str, str], hidden: set[str] | None = None) -> str:
    """没给上级时，从名字本身推：'整数规划判定问题' → '整数规划'。

    只认前缀/后缀整段包含，且至少多出两个字——
    '矩阵相乘次数' 这类不会被硬塞到某个概念下面。
    """
    own = _key(name)
    hidden = hidden or set()
    best = ""
    for key, display in known.items():
        if len(key) < 2 or key == own or key in hidden or len(own) - len(key) < 2:
            continue
        if own.startswith(key) or own.endswith(key):
            if len(key) > len(best):
                best = key
    return known.get(best, "") if best else ""


# ---------------------------------------------------------------- 写回缓存
def _touch(tags: dict[str, dict[str, Any]], name: str, **fields: Any) -> None:
    """改一条分类缓存的字段，保留 src（manual 不能被自动流程碰）。"""
    key = _key(name)
    if not key:
        return
    row = dict(tags.get(key) or {})
    row.setdefault("name", name)
    row.setdefault("cat", tag_svc.heuristic(name))
    row.setdefault("parent", "")
    row.setdefault("canonical", "")
    row.setdefault("src", "rule")
    row.update(fields)
    row["at"] = time.time()
    tags[key] = row


def tidy(settings: Settings, client: LLMClient | None = None) -> dict[str, Any]:
    """把存量概念整理一遍：碎片合并、空泛词隐藏、层级重挂。

    只动 src != manual 的条目——用户亲手改过的分类和层级，永远不自动改。
    传了 client 就再做一层 AI 复核（规则判不出的"换角度命名"交给模型）。
    """
    tags = tag_svc.load(settings)
    items = concept_svc.summary(settings)["items"]
    # 权重大的先处理：合并方向永远是"碎片 → 主体"
    pool = sorted(
        items,
        key=lambda i: (-(i.get("note_count", 0) * 10 + i.get("cards", 0)), -len(i["name"]), i["name"]),
    )
    hidden = {k for k, row in tags.items() if row.get("cat") in tag_svc.HIDDEN_CATS}
    result: dict[str, Any] = {"merged": [], "reparented": 0, "hidden": [], "total": len(items)}

    # 先算一遍"每条挂在哪"：合并判定要用（碎片能不能在父名里找到）。
    # 只用显式给的 parent，不做推导——推导出来的归属还不够硬，不能拿来吞条目。
    names_all = {_key(i["name"]): i["name"] for i in pool}
    parent_of: dict[str, str] = {}
    for item in pool:
        key = _key(item["name"])
        row = tags.get(_key(item.get("tag_name") or item["name"])) or {}
        stored = str(row.get("parent") or "").strip()
        if key and stored:
            parent_of[key] = resolve_parent(stored, item["name"], names_all, hidden)
    has_children = {_key(p) for p in parent_of.values() if p}

    known: dict[str, str] = {}
    for item in pool:
        name = item["name"]
        key = _key(name)
        if not key:
            continue
        row = tags.get(_key(item.get("tag_name") or name)) or {}
        manual = row.get("src") == "manual"
        if judge(name):
            hidden.add(key)
            # 已经判成隐藏类的不要重复计数，否则每点一次「整理」都说"隐藏 1 个"
            if not manual and row.get("cat") not in tag_svc.HIDDEN_CATS:
                _touch(tags, name, cat="trivial", src="fixed")
                result["hidden"].append(name)
            continue
        target = ""
        if not manual:
            target = same_concept(name, known) or via_parent(
                name, parent_of.get(key, ""), {**names_all, **known}, key in has_children,
            ) or via_unique_fragment(name, {**names_all, **known})
        if target:
            _touch(tags, name, canonical=target)
            result["merged"].append([name, target])
            continue
        known[key] = name

    for item in pool:
        name = item["name"]
        key = _key(name)
        row = tags.get(_key(item.get("tag_name") or name)) or {}
        if key not in known or row.get("src") == "manual":
            continue
        stored = str(row.get("parent") or "").strip()
        parent = resolve_parent(stored, name, known, hidden) if stored \
            else derive_parent(name, known, hidden)
        if parent != stored:
            _touch(tags, name, parent=parent)
            result["reparented"] += 1

    tag_svc.save(settings, tags)
    # 合并/隐藏之后，卡片上可能还写着旧概念名（合并只改索引，卡片字段不会自己跟上来）
    result["repointed"] = repoint_cards(settings)
    # 规则判不出的残留，再让模型看一遍；没配 Key 就跳过，不影响规则整理
    result["ai"] = ai_review(settings, client) if client is not None else {
        "checked": 0, "merged": [], "reason": "skipped"}
    return result


def repoint_cards(settings: Settings) -> int:
    """把卡片上残留的旧概念名改成规范名（顺着 canonical 链走到头）。

    「变换顺序」并进「初等行变换法」后，索引里它是别名，但两张卡片的
    concept 字段还写着旧名字——复习页点概念跳转就对不上了。每次 tidy
    顺手把这类卡片改正，幂等：已经是规范名的卡片一个字都不动。
    """
    from . import cards as card_svc  # noqa: PLC0415

    tags = tag_svc.load(settings)
    cards = card_svc.load(settings)
    changed = 0
    for card in cards:
        name = (card.get("concept") or "").strip()
        if not name:
            continue
        canon = concept_svc._canonical(tags, name)
        if canon and canon != name:
            card["concept"] = canon
            changed += 1
    if changed:
        card_svc.save(settings, cards)
        log.info("卡片概念回流：%d 张改为规范名", changed)
    return changed


# ---------------------------------------------------------------- AI 复核
# 规则只认形态，认不出"换了个角度命名"的同一件事。这一层把规则判不出的残留
# 交给模型看一遍：它知道「变换顺序」是初等行变换法里的一个细节。
MAX_AI = 40          # 一次最多送这么多条，够用且便宜
MAX_AI_KNOWN = 80    # 词表上限：多了 prompt 反而糊

_AI_SYSTEM = """你在给一个学习笔记软件的「概念索引」做收尾核对。

索引里已经有一批概念（见【已有概念】）。现在有几个"候选词"，它们可能是：
1. 某个已有概念的**另一种说法**（例：变换顺序 ← 初等行变换法）；
2. 某个已有概念的**下级细节**（例：行变换使用限制 ← 初等行变换法）；
3. **真正独立**的概念（例：NP-完全、矩阵连乘问题）。

请逐个判断，只允许两种结论：
- merge：你**确信**它讲的就是某个已有概念，或只是那个概念的一个细节。
  into 必须**原样**抄【已有概念】里的一个名字，不许改写、不许发明、不许拼接。
- keep：拿不准、或它确实自成一体，就 keep。

判断只依据名字和学科常识，不要臆测内容。宁可 keep，也不要把两个不同的
概念并成一个——并错会把一个真概念连同它的卡片一起吞掉。

只输出 JSON：{"verdicts":[{"name":"原样返回候选词","action":"merge","into":"已有概念名","why":"不超过 12 字的理由"}]}
keep 的写成 {"name":"...","action":"keep","into":"","why":"..."}"""


def _lcs(a: str, b: str) -> int:
    """最长公共子串长度：判断两个名字有没有共同的核心词。"""
    if not a or not b:
        return 0
    best = 0
    prev = [0] * (len(b) + 1)
    for ca in a:
        cur = [0] * (len(b) + 1)
        for j, cb in enumerate(b, 1):
            if ca == cb:
                cur[j] = prev[j - 1] + 1
                if cur[j] > best:
                    best = cur[j]
        prev = cur
    return best


def suspicious(name: str, known: dict[str, str]) -> bool:
    """值不值得让模型看一眼：带抽象限定词，或与某个概念共享 ≥2 个连续字。

    这一步是省钱的关键——把 3-SAT、Git 这种一眼独立的过滤掉，
    只把「XX 顺序」「XX 使用限制」这类真的有歧义的送上去。
    """
    own = _key(name)
    if any(name.endswith(t) for t in TAIL_MERGE):
        return True
    for key in known:
        if key != own and len(key) >= 2 and _lcs(own, key) >= 2:
            return True
    return False


def _weights(items: list[dict[str, Any]]) -> dict[str, int]:
    """概念的"分量"：被几篇笔记讲过、挂了几张卡。用于判断合并方向。"""
    return {
        _key(i["name"]): i.get("note_count", 0) * 10 + i.get("cards", 0)
        for i in items if i.get("name")
    }


def ai_candidates(items: list[dict[str, Any]], tags: dict[str, dict[str, Any]],
                  known: dict[str, str], limit: int = MAX_AI) -> list[dict[str, Any]]:
    """挑出"规则没判、又看着可疑"的条目。items 需已过 annotate（有 cat/parent_key）。

    只送**明显比相似对象更轻**的那一个：主体概念会跟一堆碎片共享字面，
    但它自己是合并的终点，不该被送去问"你要并进谁"。
    「逆矩阵 / 单位矩阵」「焦虑症 / 广泛性焦虑症」这种同量级的，谁也不是谁的碎片，不送。
    """
    weight = _weights(items)
    out: list[dict[str, Any]] = []
    for item in items:
        name = item.get("name") or ""
        key = _key(name)
        if not key:
            continue
        row = tags.get(_key(item.get("tag_name") or name)) or {}
        if row.get("src") == "manual" or row.get("ai_review"):
            continue                       # 手动结论、已经问过模型的，都别再问
        if item.get("parent_key"):
            continue                       # 已经有上级，层级是清楚的
        if item.get("cat") in tag_svc.HIDDEN_CATS:
            continue
        if not suspicious(name, known):
            continue
        own_weight = weight.get(key, 0)
        partners = [k for k in known if k != key and _lcs(key, k) >= 2]
        if partners and not any(weight.get(p, 0) > own_weight for p in partners) \
                and not any(name.endswith(t) for t in TAIL_MERGE):
            continue                       # 它自己就是这一簇里最像正名的，没什么可问的
        out.append(item)
        if len(out) >= limit:
            break
    return out


def _canonical_end(tags: dict[str, dict[str, Any]], name: str) -> str:
    """顺着 canonical 链走到头，防止 A→B、B→A 这种互指。"""
    cur = name
    seen: set[str] = set()
    for _ in range(4):
        row = tags.get(_key(cur)) or {}
        nxt = str(row.get("canonical") or "").strip()
        key = _key(nxt)
        if not nxt or nxt == cur or not key or key in seen:
            break
        seen.add(key)
        cur = nxt
    return cur


def ai_review(settings: Settings, client: LLMClient | None, limit: int = MAX_AI) -> dict[str, Any]:
    """让模型复核规则判不出的残留。

    权限只有两个：merge（并进某个**已有**概念）/ keep（保持独立）。
    **不支持 drop**——删概念是规则层的事，模型没有删东西的权限；
    也不能凭空发明一个 into，必须在词表里逐字命中。
    """
    if client is None:
        return {"checked": 0, "merged": [], "reason": "no_key"}

    tags = tag_svc.load(settings)
    items = concept_svc.summary(settings)["items"]
    tag_svc.annotate(items, tags)          # 补上 cat / parent_key
    hidden = {k for k, row in tags.items() if row.get("cat") in tag_svc.HIDDEN_CATS}
    known = {_key(i["name"]): i["name"] for i in items
             if i.get("name") and _key(i["name"]) not in hidden}
    cands = ai_candidates(items, tags, known, limit)
    if not cands:
        return {"checked": 0, "merged": [], "reason": "nothing"}

    # 词表里只放"能并进去的"：隐藏类（景点/鸡汤/实例步骤…）不给模型看见，
    # 否则它会挑一个你不能并的节点（实测挑过 hidden 的「化零顺序」）。
    listed = [i for i in items if i.get("name") and _key(i["name"]) not in hidden]
    table = "\n".join(
        f"- {i['name']}（{i['note_count']} 篇 / {i['cards']} 卡）"
        for i in listed[:MAX_AI_KNOWN]
    )
    listing = "\n".join(f"- {i['name']}" for i in cands)
    try:
        data = client.complete_json(
            _AI_SYSTEM,
            f"【已有概念】\n{table}\n\n【候选词】\n{listing}",
            max_tokens=1500,
        )
    except Exception as exc:               # 模型抽风就当作没复核，不能弄坏索引
        log.warning("概念 AI 复核失败：%s", exc)
        return {"checked": len(cands), "merged": [], "reason": "error"}

    rows = data.get("verdicts") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        return {"checked": len(cands), "merged": [], "reason": "bad"}

    cand_keys = {_key(i["name"]) for i in cands}
    by_key = {_key(v): v for v in known.values()}
    merged: list[list[str]] = []
    kept = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "").strip()
        key = _key(name)
        if key not in cand_keys:
            continue                        # 模型自由发挥出来的词，不认
        action = str(row.get("action") or "").strip().lower()
        if action == "merge":
            target = by_key.get(_key(row.get("into") or ""))
            if not target or _key(target) == key or _key(target) in hidden:
                # into 不在词表 / 指向自己 / 指向隐藏项：不认，但记住问过了，
                # 同一个问题不值得再花一次钱（模型重问一遍还是这个答案）
                log.info("AI 复核结论被否决：%s -> %s", name, row.get("into"))
                _touch(tags, name, ai_review=1)
                continue
            if _key(_canonical_end(tags, target)) == key:
                _touch(tags, name, ai_review=1)
                continue                    # 会成环
            _touch(tags, name, canonical=target)
            merged.append([name, target, str(row.get("why") or "")[:20]])
        else:
            _touch(tags, name, ai_review=1)  # keep：记住问过了，下次别再花钱
            kept += 1

    if merged or kept:
        tag_svc.save(settings, tags)
    log.info("概念 AI 复核：送 %d 条，并回 %d 条，保持 %d 条", len(cands), len(merged), kept)
    return {"checked": len(cands), "kept": kept, "merged": merged, "reason": "ok"}


# ---------------------------------------------------------------- 生成期审核
def review_glossary(settings: Settings, glossary: list[dict[str, str]]) -> tuple[list[dict[str, str]], dict[str, Any]]:
    """笔记「关键概念」表过闸：合并派生名、剔掉非知识与空泛词、去重。

    返回 (保留的词条, 审核记录)。记录给流水线写进"本次注意事项"，让人看得见改了哪里。
    """
    tags = tag_svc.load(settings)
    hidden = {k for k, row in tags.items() if row.get("cat") in tag_svc.HIDDEN_CATS}
    known: dict[str, str] = {}
    for key, row in tags.items():
        name = str(row.get("name") or "").strip()
        if name and key not in hidden:
            known[key] = name

    kept: list[dict[str, str]] = []
    dropped: list[str] = []
    renamed: list[tuple[str, str]] = []
    seen: dict[str, dict[str, str]] = {}

    for row in glossary:
        name = clean_term(row.get("term") or "")
        explain = str(row.get("explain") or "").strip()
        if not name:
            continue
        key = _key(name)
        cat = (tags.get(key) or {}).get("cat") or tag_svc.heuristic(name)
        if judge(name) or (cat in tag_svc.HIDDEN_CATS and (tags.get(key) or {}).get("src") != "manual"):
            dropped.append(name)
            continue
        target = same_concept(name, known) or via_unique_fragment(name, known)
        final = target or name
        fkey = _key(final)
        if fkey in seen:
            # 同一篇笔记里两个说法指同一件事：解释并起来，别让表里出现两行
            prev = seen[fkey]
            if explain and explain not in prev["explain"]:
                prev["explain"] = f"{prev['explain']} ／ {explain}".strip(" ／")
            continue
        row_out = {"term": final, "explain": explain}
        kept.append(row_out)
        seen[fkey] = row_out
        known.setdefault(fkey, final)
        if final != name:
            renamed.append((name, final))

    return kept, {"dropped": dropped, "renamed": renamed}


def review_card_concepts(items: list[dict[str, str]],
                         known_names: Iterable[str] | None) -> int:
    """卡片的 concept 字段过闸：把「X 的优势」这类派生写法收回 X。

    只做重命名，不删卡——卡片本身是有内容的，错的只是它挂的那个名字。
    返回改写的张数。
    """
    known: dict[str, str] = {}
    for name in known_names or []:
        if name:
            known.setdefault(_key(name), clean_term(name))
    if not known:
        return 0
    changed = 0
    for item in items:
        concept = clean_term(item.get("concept") or "")
        if not concept or _key(concept) in known:
            continue
        target = same_concept(concept, known)
        if target and target != concept:
            item["concept"] = target
            changed += 1
    return changed
