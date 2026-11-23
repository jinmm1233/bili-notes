"""DeepSeek 作为文字模型 / 看图模型的回归检查（`python tests/smoke_llm_deepseek.py`）。

守两件事：
  1. DeepSeek 的模型名不能再是已停用的 deepseek-chat，看图也要有默认模型；
  2. 请求体里必须带 thinking=disabled —— deepseek-flash 默认开思考，
     看图只有 1024 token 预算，不关会把正文挤没。
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.core import llm  # noqa: E402
from app.core.llm import LLMClient, PROVIDERS, _model_fits  # noqa: E402

FAILED = 0


def ok(cond: bool, msg: str) -> None:
    global FAILED
    print(("[OK] " if cond else "[FAIL] ") + msg)
    if not cond:
        FAILED += 1


# ---- 1. 预设本身 ----
ds = PROVIDERS["deepseek"]
ok(ds["model"] == "deepseek-flash", f"文字模型是 deepseek-flash（当前 {ds['model']}）")
ok(ds["model"] not in {"deepseek-chat", "deepseek-reasoner"}, "没有再用已停用的旧模型名")
ok(ds.get("vision_model") == "deepseek-flash", "看图默认模型是 deepseek-flash")
ok("deepseek-flash" in ds.get("vision_options", ()), "看图模型下拉里有 deepseek-flash")
ok(ds.get("extra_payload", {}).get("thinking", {}).get("type") == "disabled",
   "预设里关掉了思考模式")

# ---- 2. 模型名归属校验 ----
ok(_model_fits("deepseek", "deepseek-flash"), "deepseek-flash 被认作 DeepSeek 的模型")
ok(not _model_fits("deepseek", "gpt-4o-mini"), "别的家的模型名不会误用到 DeepSeek")

# ---- 3. 真实请求体 ----
class _Resp:
    status_code = 200
    text = ""

    def json(self):
        return {
            "choices": [{"message": {"content": '{"ok": true}'}}],
            "usage": {"total_tokens": 12},
        }


class _FakeClient:
    captured: list[dict] = []

    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def post(self, url, json=None, headers=None):
        _FakeClient.captured.append(json)
        return _Resp()


real_client = llm.httpx.Client
llm.httpx.Client = _FakeClient
try:
    c = LLMClient(
        base_url=ds["base_url"],
        api_key="sk-test",
        model=ds["vision_model"],
        extra_payload=ds["extra_payload"],
    )
    c.complete_vision("只用一句话描述画面", "这一帧在讲什么", [(b"\xff\xd8\xff", "image/jpeg")])
    vision_payload = _FakeClient.captured[-1]
    ok(vision_payload["model"] == "deepseek-flash", "看图请求用 deepseek-flash")
    ok(vision_payload.get("thinking") == {"type": "disabled"}, "看图请求带 thinking=disabled")
    block = vision_payload["messages"][-1]["content"][-1]
    ok(block["type"] == "image_url" and block["image_url"]["url"].startswith("data:image/jpeg;base64,"),
       "图片按 OpenAI 兼容的 image_url + base64 发送")

    c.complete("你是摘要助手", "总结这段文字", max_tokens=256)
    text_payload = _FakeClient.captured[-1]
    ok(text_payload.get("thinking") == {"type": "disabled"}, "文字请求也关掉思考")
    ok("response_format" not in text_payload, "普通调用不乱加 response_format")

    c.complete_json("你是摘要助手", "输出 JSON", max_tokens=256)
    json_payload = _FakeClient.captured[-1]
    ok(json_payload.get("response_format") == {"type": "json_object"}, "JSON 模式仍带 response_format")
finally:
    llm.httpx.Client = real_client

# ---- 4. 从设置里能构造出 DeepSeek 看图客户端 ----
class _Store:
    def get(self, provider: str) -> str:
        return "sk-test" if provider == "deepseek" else ""


settings = SimpleNamespace(
    vision_provider="deepseek",
    vision_model="",
    vision_base_url="",
    llm_timeout=30.0,
)
client = LLMClient.from_vision_settings(settings, _Store())
ok(isinstance(client, LLMClient), "选了 DeepSeek 看图能构造出客户端")
ok(client is not None and client.model == "deepseek-flash", "没手填模型名时用 deepseek-flash")
ok(client is not None and client.extra_payload.get("thinking") == {"type": "disabled"},
   "从设置构造时也带上了 thinking 开关")

settings.vision_model = "gpt-4o-mini"   # 换供应商残留的旧值
client2 = LLMClient.from_vision_settings(settings, _Store())
ok(client2 is not None and client2.model == "deepseek-flash",
   "残留的别家模型名会被忽略，回落到 deepseek-flash")

print()
if FAILED:
    print(f"{FAILED} 项未通过：DeepSeek 看图接入有问题，先别发版。")
    sys.exit(1)
print("全部通过：DeepSeek 文字与看图预设一致，思考模式已关闭。")
