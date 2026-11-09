"""概念分类：判断一个词条值不值得当成"知识点"。

为什么需要这一层：
  笔记的「关键概念」表由模型自由生成，旅游 vlog 会把景点写进去（中庵索道、乌龟潭），
  TED 类内容会把口号写进去（做不好也要做、做得差也没关系）。这些放进概念索引，
  真正的知识点就会被淹掉。

判别策略（从省到贵）：
  1. 命中"明确是知识"的构词（定理/公式/矩阵/症/资源…）→ 直接 knowledge，不问模型；
  2. 命中鸡汤句式或地点后缀 → soup / place，也不问模型；
  3. 剩下的交给文字模型判一次，结果写进 data_dir/concept_tags.json 长期缓存。
  4. 用户手动改过的（src=manual）永远不被覆盖。

只隐藏 place / soup；other（拿不准）保留可见——宁可多留，不要误删真知识。
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
from .concepts import name_key

log = logging.getLogger(__name__)

TAG_FILE = "concept_tags.json"
CATS = ("knowledge", "place", "soup", "trivial", "step", "relation", "artifact", "other")
# 默认折叠：景点、鸡汤、常识、例题步骤、关系判断、变量占位符
HIDDEN_CATS = ("place", "soup", "trivial", "step", "relation", "artifact")
MAX_BATCH = 40

_SYSTEM = """你在帮一个学习笔记软件整理「概念索引」。用户的要求很具体，请严格照做：

一、每个词条先判类别 cat：
- knowledge：学科概念、原理、方法、公式、定理、算法、术语、机制、病症、模型、步骤名。
- place：旅游/生活向的具体名词——景点、地名、路线点、山峰、村庄、寺庙、店铺、车站、酒店，
  也包括游玩项目、地方特产、服饰、导游/旅行社（例：摇橹船、放花灯、奥黛、免费导游组织）。
- soup：空泛的感悟、价值判断、口号、情绪安慰（例："做不好也要做""目的和意义"）。
  判断标准：**没有确定答案、每个人看法不同**的，就是 soup。
- trivial：常识性的操作细节、软件界面/路径、一看就懂的动词（例：C盘、安装包、Finish、加括号）。
  判断标准：**不需要专门学、看名字就知道怎么做**的，就是 trivial。
- step：**只存在于某一个例题/某一次具体操作里的步骤**（例：第二列化零、第一步、把第三行乘 2）。
  判断标准：换个视频它就不成立，或同一个词在别处指完全不同的操作。
  **即使它看起来像个方法，只要只对当前这道题成立，就是 step。**
- relation：**一句判断/关系，不是实体**（例：IP属于NP、X是Y的特例、A与B的区别）。
  这类不该单独成条——它的内容应该写在被判断的那个概念下面。
- artifact：例题里的变量、编号、占位符（例：证书x、x1、变量a）。
- other：以上都不是，或信息不足以判断。

二、再判层级 parent：
- 如果这个词条只是某个更大方法/主题下的细节，填那个上级的名称；本身就是独立概念就填空字符串 ""。
- 例：主对角线、倍乘、增广矩阵、单位矩阵 → parent "初等行变换法"
- 例：候选解验证、目标阈值 → parent "整数规划问题（IP）"
- 例：广泛性焦虑症、社交焦虑 → parent "焦虑症"（如果列表里有）
- 上级名称要尽量用列表里已有的词；没有合适的就概括一个方法名。
- **概念以"方法/主题"为单位**：细节不要和它所属的方法并列。

三、给每个词条一个规范名 canonical（重要，只解决"同一个东西的多种叫法"）：
- **只在两种情况下填 canonical**，其他一律留空字符串 ""：
  1. relation 句必须填，且**只能填列表里已有的概念名**（不要发明新名字）：
     例：列表里有「整数规划」，那么 "IP属于NP" 的 canonical 就填 "整数规划"；
  2. 词条本身是**短的全大写缩写**（IP / NP / SAT）：填展开后的全称，如 "IP（整数规划问题）"。
- **绝对不要**给普通术语加英文注释或改名：NP-完全 保持 "NP-完全"，3-SAT 保持 "3-SAT"。
- **不要把子问题、性质、证明方向合并成别名**：像"可满足到可行解""可行解到可满足"，
  应该用 parent 挂在「整数规划」下，canonical 留空。
  判断标准：它是"X 的另一个名字"还是"关于 X 的一件事"？后者用 parent。
- 每一行都必须带 canonical 字段，没有就写 ""，不要省略。

四、其他规则：
1. 词后面括号里是来源笔记标题："XX旅行/vlog/徒步" 来的具体名词优先 place；学科/技术笔记来的优先 knowledge。
2. 心理学/医学正式术语（广泛性焦虑症、自我同情、应对资源）是 knowledge，不是 soup。
3. 拿不准就 other，不要硬塞。

只输出 JSON：{"tags":[{"name":"原样返回","cat":"knowledge","parent":"","canonical":""}]}
"""

# 明确属于知识点的构词。命中即放过，省一次模型调用，也避免模型误判。
_TECH = re.compile(
    r"(定理|公式|算法|矩阵|函数|证明|复杂度|规划|约束|映射|不等式|方程式?|"
    r"结构|原理|方法|次序|次数|步骤|问题|变换|归约|推导|定义|性质|"
    r"症|障碍|资源|同情|掌控|焦虑|抑郁|情绪|认知|机制|模型|策略|"
    r"NP|SAT|AI|CPU|GPU|API|HTTP|SQL|Git|Python|Java)"
)

# 景点/地点构词。单字后缀要求词长 ≥3，避免"边界""领域"这类被误杀。
_PLACE_SUFFIX = re.compile(
    r"(索道|古镇|公园|乐园|客栈|酒店|民宿|餐厅|车站|机场|码头|瀑布|广场|大道|大桥|"
    r"寺|庙|塔|峰|岭|洞|潭|湾|堤|坡|尖|宕|桩|坞|埠|村|街|巷|山|湖|岛|园|馆|宫|泉|溪|谷|崖|坪|桥|船|顶|界|岩)$"
)
_PLACE_EXCEPT = {"边界", "世界", "视界", "领域", "界面", "山顶洞人"}

# 例题里的具体步骤：第 X 列/行 化零、第 X 步、化零顺序……
# 关系判断：一句话，不是实体
_RELATION = re.compile(
    r"(属于|是.{0,6}(特例|子类|一种|等价)|可归约到|蕴含|当且仅当|之间的?关系|的区别)"
)

# 例题里的变量/占位符：中文词 + 单个小写字母，或纯变量名
_ARTIFACT = re.compile(r"^[\u4e00-\u9fa5]{2,}[a-z]$|^[a-zA-Z]\d{1,2}$")

_STEP = re.compile(
    r"第[一二三四五六七八九十百\d]+\s*[列行].{0,8}(化零|化为\s*0|化成\s*0)"
    r"|^(第[一二三四五六七八九十百\d]+步)$|^化零顺序$"
)

# 常识操作：盘符、安装包、界面按钮这类"看名字就知道"的东西
_TRIVIAL = re.compile(r"^[A-Za-z]:|^[A-Za-z]盘$|安装包|安装目录|双击|右键|快捷键|进度条|^Finish$|^Next$|^OK$")

# 鸡汤/口号句式
_SOUP = re.compile(
    r"(也要|都没关系|也没关系|就好了|才是|不要怕|别怕|原谅|放下|接纳|坚持|"
    r"相信自己|努力|值得|意义|慢慢来|做自己|爱自己|加油|熬过去|会好的)"
)


def _path(settings: Settings) -> Path:
    return Path(settings.data_dir) / TAG_FILE


def load(settings: Settings) -> dict[str, dict[str, Any]]:
    path = _path(settings)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        log.warning("概念分类缓存损坏，按空处理：%s", path)
        return {}
    tags = data.get("tags") if isinstance(data, dict) else {}
    return tags if isinstance(tags, dict) else {}


def save(settings: Settings, tags: dict[str, dict[str, Any]]) -> None:
    path = _path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": 1, "updated": time.time(), "tags": tags}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def heuristic(name: str) -> str:
    """不用模型的快速判断。只处理有把握的，其余交 other。"""
    text = (name or "").strip()
    if not text:
        return "other"
    if _RELATION.search(text) and len(text) <= 14:
        return "relation"
    if _TECH.search(text):
        return "knowledge"
    if _STEP.search(text):
        return "step"
    if _ARTIFACT.match(text):
        return "artifact"
    if _RELATION.search(text) and len(text) <= 14:
        return "relation"
    if _SOUP.search(text) and len(text) <= 14:
        return "soup"
    if _TRIVIAL.search(text):
        return "trivial"
    if text in _PLACE_EXCEPT:
        return "knowledge"
    if _PLACE_SUFFIX.search(text) and 2 <= len(text) <= 10:
        return "place"
    return "other"


def get_cat(tags: dict[str, dict[str, Any]], name: str) -> str:
    row = tags.get(name_key(name))
    cat = (row or {}).get("cat")
    return cat if cat in CATS else "other"


def get_canonical(tags: dict[str, dict[str, Any]], name: str) -> str:
    """规范名。空串表示"用原名"，不强行改。"""
    return ((tags.get(name_key(name)) or {}).get("canonical") or "").strip()


def get_parent(tags: dict[str, dict[str, Any]], name: str) -> str:
    """所属的上级方法/主题；本身就是顶层概念时返回空串。"""
    return ((tags.get(name_key(name)) or {}).get("parent") or "").strip()


def _merge(tags: dict[str, dict[str, Any]], name: str, cat: str,
           src: str, parent: str = "", canonical: str = "") -> bool:
    """写入一个分类结果。manual 不被自动流程覆盖；llm 可以覆盖 rule。返回是否有变化。"""
    key = name_key(name)
    if not key or cat not in CATS:
        return False
    old = tags.get(key) or {}
    parent = (parent or "").strip()
    canonical = (canonical or "").strip()
    # 手动结论只允许被"另一次手动"修改；硬规则（fixed）也不许模型改
    if old.get("src") in ("manual", "fixed") and src not in ("manual", "fixed"):
        return False
    if old.get("src") == "manual" and src != "manual":
        return False
    if (old.get("cat") == cat and old.get("src") == src
            and (old.get("parent") or "") == parent
            and (old.get("canonical") or "") == canonical):
        return False
    if old.get("cat") == cat and old.get("src") == "llm" and src == "rule":
        return False                      # 已经模型判过，别被规则降级
    row = {
        "name": name, "cat": cat, "parent": parent, "canonical": canonical,
        "src": src, "at": time.time(),
    }
    if old.get("ai_review"):                 # 模型复核过的标记别丢，否则又要花一次钱
        row["ai_review"] = old["ai_review"]
    tags[key] = row
    return True


def _accept_canonical(name: str, cat: str, canonical: str,
                      known: dict[str, str]) -> str:
    """只接受两类规范名，避免模型把"相关"当成"同一个"。

    1. relation 句：必须并到**列表里已有的概念**上（允许模型写全称，
       只要包含某个已有概念名即可），这样"IP属于NP"会挂到"整数规划"下；
    2. 短全大写缩写（IP / NP / SAT）：允许展开成带注释的全称。
    其余一律忽略——子问题、证明方向不许被合并成别名。
    """
    canon = (canonical or "").strip()
    if not canon:
        return ""
    key = name_key(canon)
    if cat == "relation":
        if key in known:
            return known[key]
        for k, display in known.items():
            if k and k in key:
                return display
        return ""
    if re.fullmatch(r"[A-Z]{2,5}", name or ""):
        return canon
    return ""


def _override(tags: dict[str, dict[str, Any]], name: str) -> bool:
    """确定性规则，优先级高于模型。

    模型经常把「第二列化零」「证书x」「IP属于NP」当知识点放过，
    但这几类的形态是可判定的，直接钉死，避免每次重判又变回去。
    """
    text = name or ""
    key = name_key(text)
    kept = tags.get(key) or {}
    canon = str(kept.get("canonical") or "").strip()

    if _STEP.search(text):
        return _merge(tags, text, "step", "fixed", canonical=canon)
    if _ARTIFACT.match(text):
        return _merge(tags, text, "artifact", "fixed", canonical=canon)
    if _RELATION.search(text) and len(text) <= 14:
        # 关系句的归属：取「A 属于 B」里的 A，看它有没有规范名
        subject = re.split(r"(属于|是|可归约到|蕴含|当且仅当)", text)[0].strip()
        derived = ""
        if subject and subject != text:
            derived = str((tags.get(name_key(subject)) or {}).get("canonical") or "")
        return _merge(tags, text, "relation", "fixed",
                      canonical=derived or canon)
    return False


def classify(settings: Settings, client: LLMClient | None,
             names: Iterable[str], contexts: dict[str, str] | None = None,
             force: bool = False) -> dict[str, Any]:
    """给还没分类（或只有规则分类）的词条打标。

    返回 {"tagged": 本次写入数, "source": "llm"/"rule"/"none", "pending": 剩余未判数}
    """
    tags = load(settings)
    contexts = contexts or {}
    known = {name_key(n): n for n in names if n}
    unique: dict[str, str] = {}
    for name in names:
        if not name:
            continue
        key = name_key(name)
        row = tags.get(key)
        if row and row.get("src") in ("manual", "fixed"):
            continue                     # 手动纠正 / 硬规则结论永远不覆盖
        if not force and row and row.get("src") == "llm":
            continue
        unique[key] = name

    if not unique:
        return {"tagged": 0, "source": "none", "pending": 0}

    changed = 0
    source = "rule"

    if client is not None:
        pending = list(unique.values())
        for start in range(0, len(pending), MAX_BATCH):
            batch = pending[start:start + MAX_BATCH]
            try:
                lines = []
                for n in batch:
                    ctx = (contexts.get(name_key(n)) or "").strip()
                    lines.append(f"- {n}" + (f"（来自：{ctx[:24]}）" if ctx else ""))
                data = client.complete_json(
                    _SYSTEM,
                    "请给下面这些词条分类：\n" + "\n".join(lines),
                    max_tokens=2000,
                )
            except Exception as exc:        # 模型抽风就退回规则，不让接口炸
                log.warning("概念分类调用失败，退回规则：%s", exc)
                break
            rows = data.get("tags") if isinstance(data, dict) else data
            if not isinstance(rows, list):
                break
            by_key = {
                name_key(r.get("name") or ""): r
                for r in rows if isinstance(r, dict)
            }
            for name in batch:
                row = by_key.get(name_key(name)) or {}
                cat = row.get("cat")
                parent = str(row.get("parent") or "")
                canonical = _accept_canonical(
                    name, cat, str(row.get("canonical") or ""), known)
                if cat in CATS:
                    changed += int(_merge(tags, name, cat, "llm",
                                         parent=parent, canonical=canonical))
            source = "llm"
            # 模型漏掉的，用规则补；再用硬规则纠偏（模型爱把"第二列化零"当方法）
            for name in batch:
                if tags.get(name_key(name), {}).get("src") != "llm":
                    changed += int(_merge(tags, name, heuristic(name), "rule"))
                changed += int(_override(tags, name))

    # 没有模型 / 调用失败时，全部用规则先顶着
    for name in unique.values():
        if tags.get(name_key(name), {}).get("src") not in ("llm", "manual", "fixed"):
            changed += int(_merge(tags, name, heuristic(name), "rule"))
        changed += int(_override(tags, name))

    if changed:
        save(settings, tags)

    left = sum(1 for name in unique.values()
               if tags.get(name_key(name), {}).get("src") not in ("llm", "manual", "fixed"))
    return {"tagged": changed, "source": source, "pending": left}


def set_manual(settings: Settings, name: str, cat: str,
               parent: str | None = None) -> dict[str, Any]:
    """用户手动纠正，优先级最高，之后不会再被自动覆盖。

    parent 不传时沿用原来的层级——手动改类别不该把"挂在哪个方法下面"弄丢。
    """
    if cat not in CATS:
        cat = "knowledge"
    tags = load(settings)
    keep = get_parent(tags, name) if parent is None else parent
    _merge(tags, name, cat, "manual", parent=keep)
    save(settings, tags)
    return {"ok": True, "name": name, "cat": cat, "parent": keep}


def annotate(items: list[dict[str, Any]], tags: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """给概念条目补上 cat / parent / parent_key（原地修改）。

    层级在这里**对齐一次**：模型写的上级经常是列表里没有的名字
    （「整数规划问题（IP）」），直接渲染就会让整条子树从页面上掉下去。
    所以上级先按已有概念解析，解析不出就退到"名字本身就是 X 的下一层"，
    再不行才清空——页面上永远不出现没有内容的父节点。

    返回 {"repaired": 需要写回的条数}，供页面判断要不要自动整理一次。
    """
    from . import concept_review as review  # noqa: PLC0415  避免循环依赖

    known = {name_key(i["name"]): i["name"] for i in items if i.get("name")}
    hidden = {k for k, row in tags.items() if row.get("cat") in HIDDEN_CATS}
    repaired = 0
    for item in items:
        # 合并后的条目，分类可能挂在某个别名上
        name = item.get("tag_name") or item.get("name") or ""
        row = tags.get(name_key(name)) or {}
        stored = str(row.get("parent") or "").strip()
        parent = review.resolve_parent(stored, name, known, hidden) if stored \
            else review.derive_parent(name, known, hidden)
        item["cat"] = get_cat(tags, name)
        item["parent"] = parent
        item["parent_key"] = name_key(parent) if parent else ""
        item["canonical"] = get_canonical(tags, name)
        if parent != stored and row.get("src") != "manual":
            repaired += 1
    return {"repaired": repaired}
