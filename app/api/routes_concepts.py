"""控制器层：概念索引的汇总、详情与非知识条目的判别。"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from ..core.config import get_settings
from ..core.llm import LLMClient
from ..services import concept_explain as explain_svc
from ..services import concept_review as review_svc
from ..services import concept_tags as tag_svc
from ..services import concepts as concept_svc

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api")


class TagIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)


class ExplainIn(BaseModel):
    names: list[str] = Field(default_factory=list, max_length=200)
    force: bool = False
    cat: str = Field(pattern="^(knowledge|place|soup|other)$")


@router.get("/concepts")
def list_concepts(q: str = "", limit: int = 500) -> dict[str, Any]:
    """概念清单。q 传入时按名字做子串过滤（中文场景比分词更准）。

    每一项都带 cat（knowledge / place / soup / other），前端默认隐藏 place 与 soup；
    另外返回 untagged，前端据此决定要不要自动跑一次判别。
    """
    settings = get_settings()
    data = concept_svc.summary(settings)
    items = data["items"]
    tags = tag_svc.load(settings)
    info = tag_svc.annotate(items, tags) or {}

    needle = (q or "").strip().lower()
    if needle:
        items = [i for i in items if needle in i["name"].lower()]
    limit = max(1, min(limit, 2000))

    hidden = sum(1 for i in items if i["cat"] in tag_svc.HIDDEN_CATS)
    # untagged 只数"完全没判过"的。规则判过的也算有结论，否则没配 Key 的用户
    # 每次进这一页都会触发一次无意义的判别。
    untagged = sum(
        1 for i in items if concept_svc.name_key(i["name"]) not in tags
    )
    # 规则判不出、又看着可疑的（"XX 顺序""XX 使用限制"这类）：提示可以去让 AI 复核
    known = {i["key"]: i["name"] for i in items if i["cat"] not in tag_svc.HIDDEN_CATS}
    reviewable = len(review_svc.ai_candidates(items, tags, known))
    # 还没判过重点度、也没写释义的：概念页上点"补全释义"要处理的就是这些
    unjudged = sum(1 for i in items if i.get("core") is None)
    return {
        "total": len(items),
        "items": items[:limit],
        "hidden": hidden,
        "untagged": untagged,
        "reviewable": reviewable,
        # 有几条的上级被重新对齐过（模型写的父节点在列表里并不存在）。
        # 前端看到大于 0 就自动整理一次，把修正结果写回缓存。
        "stale": info.get("repaired", 0),
        "unjudged": unjudged,
    }


@router.get("/concepts/detail")
def concept_detail(name: str) -> dict[str, Any]:
    settings = get_settings()
    data = concept_svc.detail(settings, name)
    if data is None:
        raise HTTPException(404, "没有这个概念")
    tags = tag_svc.load(settings)
    items = concept_svc.summary(settings)["items"]
    tag_svc.annotate(items, tags)
    row = next((i for i in items if i["key"] == data["key"]), None)
    data["cat"] = tag_svc.get_cat(tags, data.get("name") or name)
    data["parent"] = (row or {}).get("parent") or ""
    return data


@router.post("/concepts/classify")
def classify_concepts(request: Request, force: bool = False) -> dict[str, Any]:
    """把还没判过的概念交给模型判一次，结果长期缓存。

    force=1 时连已判过的也重判（改了规则/提示词后想刷新时用）。
    没有配置 Key 时退回本地规则（景点后缀、鸡汤句式），仍然能挡掉大部分噪声。
    """
    settings = get_settings()
    items = concept_svc.summary(settings)["items"]
    names = [i["name"] for i in items]
    # 把来源笔记标题一起给模型：同一篇旅游 vlog 里的词，判成景点的把握大得多
    contexts = {}
    for item in items:
        notes = item.get("notes") or []
        contexts[concept_svc.name_key(item["name"])] = (
            notes[0]["title"] if notes else "（来自卡片）"
        )
    client = LLMClient.from_settings(settings, request.app.state.store)
    result = tag_svc.classify(settings, client, names, contexts=contexts, force=force)
    log.info("概念判别：%s", result)
    return {"ok": True, **result}


@router.post("/concepts/tag")
def tag_concept(body: TagIn) -> dict[str, Any]:
    """手动纠正某一条的分类，之后不再被自动覆盖。"""
    return tag_svc.set_manual(get_settings(), body.name, body.cat)


@router.post("/concepts/tidy")
def tidy_concepts(request: Request, ai: bool = False) -> dict[str, Any]:
    """整理概念索引：碎片并回主体、空泛词隐藏、层级重挂到真实存在的概念上。

    流水线每次生成完都会自动跑一次规则整理，这里是手动入口
    （历史数据 / 规则改过之后）。`ai=1` 时再多做一层模型复核——
    规则只认名字形态，「变换顺序」这种换角度命名的只能靠模型认出来。
    只动自动判定的条目，用户手动改过的分类与层级一概不碰。
    """
    settings = get_settings()
    client = LLMClient.from_settings(settings, request.app.state.store) if ai else None
    result = review_svc.tidy(settings, client=client)
    log.info("概念整理：%s", {k: v for k, v in result.items() if k != "ai"})
    return {"ok": True, **result}


@router.post("/concepts/explain")
def explain_concepts(body: ExplainIn, request: Request) -> dict[str, Any]:
    """给概念补"重点标记 + 自足释义"。

    概念页上点一下就能知道这个词是什么意思，靠的就是这里写下的 explain。
    同时判定 core：这个概念在视频里到底有没有被讲透——只是顺带提一句的，
    会被标成非重点，不占概念页的前排。

    没配文字模型时退回本地规则：只保留笔记表里解释像样的词。
    一次最多 12 批 × 8 个，没跑完前端会提示"再点一次继续"。
    """
    settings = get_settings()
    items = concept_svc.summary(settings)["items"]
    names = [n for n in (body.names or []) if n and n.strip()]
    client = LLMClient.from_settings(settings, request.app.state.store)
    result = explain_svc.explain(settings, client, items, names=names, force=body.force)
    log.info("概念释义：%s", result)
    return {"ok": True, **result}
