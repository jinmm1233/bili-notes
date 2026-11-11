"""LLM 网关：统一 OpenAI 兼容协议，一套代码切供应商。

用户自带 Key，所以这里必须把"Key 无效 / 余额不足 / 网络不通"翻译成
人话，而不是把 401 原文抛给用户。
"""
from __future__ import annotations

import json
import logging
import threading
import re
import time
from typing import Any

import httpx

from .config import Settings
from .errors import AuthRequiredError, LLMError
from .secretstore import SecretStore

log = logging.getLogger(__name__)

PROVIDERS: dict[str, dict[str, str]] = {
    "deepseek": {
        "label": "DeepSeek",
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "hint": "价格最低，中文长文本表现好；key 申请：platform.deepseek.com",
        "needs_key": "1",
        # DeepSeek 目前没有开放视觉模型，选它当看图模型等于不可用
        "vision_model": "",
    },
    "zhipu": {
        "label": "智谱 GLM",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "model": "glm-4-flash",
        # 智谱视觉模型迭代很快（4V-Flash → 4.5V → 4.6V → 5.3 原生多模态），
        # 控制台里也不一定把老模型列出来，所以这里只是默认值，设置页可以改。
        # 默认用 flashx：实测最稳、便宜；免费的 4.6v-flash 高峰期会排队（429）
        "vision_model": "glm-4.6v-flashx",
        "vision_options": (
            "glm-4.6v-flashx",   # 推荐：9B，快且便宜
            "glm-4v-flash",      # 完全免费（老款，4K 上下文）
            "glm-4.6v-flash",    # 免费，但高峰期常排队
            "glm-4.6v",          # 最强，收费
            "glm-5.3-flash",     # 5.3 原生多模态，也能看图
        ),
        # 首选模型排队时自动换这些（实测同一时刻 5 个里 3 个在排队，
        # 换一个往往就通了）。顺序按实测可用性排。
        "vision_backups": ("glm-5.3-flash", "glm-4.6v-flash", "glm-4v-flash"),
        "hint": "有免费额度；key 申请：bigmodel.cn",
        "needs_key": "1",
    },
    "qwen": {
        "label": "通义千问",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen-plus",
        "vision_model": "qwen-vl-plus",
        "vision_options": ("qwen-vl-plus", "qwen-vl-max", "qwen3-vl-plus"),
        "hint": "长上下文能力强；key 申请：dashscope.console.aliyun.com",
        "needs_key": "1",
    },
    "moonshot": {
        "label": "Moonshot",
        "base_url": "https://api.moonshot.cn/v1",
        "model": "moonshot-v1-8k",
        "vision_model": "moonshot-v1-8k-vision-preview",
        "vision_options": ("moonshot-v1-8k-vision-preview", "moonshot-v1-32k-vision-preview"),
        "hint": "Kimi 同源；key 申请：platform.moonshot.cn",
        "needs_key": "1",
    },
    "openai": {
        "label": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
        "vision_model": "gpt-4o-mini",
        "vision_options": ("gpt-4o-mini", "gpt-4o", "gpt-4.1-mini"),
        "hint": "需要海外网络环境",
        "needs_key": "1",
    },
    "ollama": {
        "label": "本地 Ollama",
        "base_url": "http://localhost:11434/v1",
        "model": "qwen2.5:7b",
        "vision_model": "qwen2.5vl:7b",
        "vision_options": ("qwen2.5vl:7b", "llava:7b", "minicpm-v"),
        "hint": "完全离线，先在本地执行 ollama pull qwen2.5vl:7b",
        "needs_key": "0",
    },
    "custom": {
        "label": "自定义（OpenAI 兼容）",
        "base_url": "",
        "model": "",
        "hint": "填入任何兼容 /chat/completions 的服务地址",
        "needs_key": "1",
    },
}


def _estimate_tokens(text: str) -> int:
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    return int(cjk / 1.5 + (len(text) - cjk) / 4) + 1


# 各家模型名的家族特征词，用来判断"设置里手填的模型名"是不是这家的
_MODEL_FAMILY: dict[str, tuple[str, ...]] = {
    "zhipu": ("glm",),
    "qwen": ("qwen", "qvq"),
    "moonshot": ("moonshot", "kimi"),
    "openai": ("gpt", "o1", "o3", "o4", "chatgpt"),
}


def _model_fits(provider: str, model: str) -> bool:
    family = _MODEL_FAMILY.get(provider)
    if not family:  # custom / ollama：模型名随便填
        return True
    name = model.lower()
    return any(token in name for token in family)


def _backoff(attempt: int, status_code: int = 0) -> float:
    """重试等待时间（秒）。

    限流（429）是「排队」不是「失败」——免费模型排队常常要几十秒，
    2/4 秒的退避太短，等于白等一次就放弃了。所以限流单独走长退避。
    """
    if status_code == 429:
        return min(60.0, 8.0 * (2 ** attempt))
    return float(min(2 ** attempt, 8))


class FallbackVisionClient:
    """看图模型的自动降级包装。

    免费模型排队限流是常态（实测同一时刻 5 个智谱视觉模型里 3 个在排队）。
    一个模型排不上队不该让整篇笔记的看图判断全部作废 ——
    这里在首选模型连续失败时自动切到备用模型。

    降级顺序：调用方给的 clients 按顺序试，第一个能用的就一直用。
    """

    def __init__(self, clients: list["LLMClient"], names: list[str] | None = None):
        self.clients = [c for c in clients if c is not None]
        self.names = names or []
        self.current = 0
        self._fail_streak = 0
        self.switched_to: str = ""
        # 并发调用时（视觉判断现在 4 路并发），
        # 切换模型和计失败次数必须加锁，否则状态会打架。
        self._lock = threading.Lock()

    @property
    def model(self) -> str:
        return getattr(self.clients[self.current], "model", "") if self.clients else ""

    def _name(self, idx: int) -> str:
        if idx < len(self.names):
            return self.names[idx]
        return getattr(self.clients[idx], "model", f"model{idx}")

    def _try_switch(self, exc: Exception | None = None) -> bool:
        """失败了就换一个模型。返回是否换了。

        限流（排队）立刻换 —— 重试同一个模型没用，队列不会因为你重试就变短。
        其他错误（网络抖动等）连续两次才换，避免偶发抖动就换模型。
        """
        rate_limited = bool(getattr(exc, "rate_limited", False))
        need = 1 if rate_limited else 2
        with self._lock:
            if self._fail_streak < need or self.current + 1 >= len(self.clients):
                return False
            prev = self._name(self.current)
            self.current += 1
            self._fail_streak = 0
            self.switched_to = self._name(self.current)
        log.warning("看图模型 %s 失败，切换到备用模型 %s", prev, self.switched_to)
        return True

    def complete_vision(self, system, user, images, *, max_tokens=1024):
        last_exc: Exception | None = None
        while True:
            with self._lock:
                client = self.clients[self.current]
            try:
                out = client.complete_vision(system, user, images, max_tokens=max_tokens)
                with self._lock:
                    self._fail_streak = 0
                return out
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                with self._lock:
                    self._fail_streak += 1
                if not self._try_switch(exc):
                    raise last_exc

    def complete_json(self, system, user, *, max_tokens=2048):
        last_exc: Exception | None = None
        while True:
            with self._lock:
                client = self.clients[self.current]
            try:
                out = client.complete_json(system, user, max_tokens=max_tokens)
                with self._lock:
                    self._fail_streak = 0
                return out
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                with self._lock:
                    self._fail_streak += 1
                if not self._try_switch(exc):
                    raise last_exc

    def complete(self, system, user, *, json_mode=False, max_tokens=2048):
        last_exc: Exception | None = None
        while True:
            with self._lock:
                client = self.clients[self.current]
            try:
                out = client.complete(system, user, json_mode=json_mode, max_tokens=max_tokens)
                with self._lock:
                    self._fail_streak = 0
                return out
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                with self._lock:
                    self._fail_streak += 1
                if not self._try_switch(exc):
                    raise last_exc


class LLMClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        temperature: float = 0.3,
        timeout: float = 120.0,
        max_retries: int = 3,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.temperature = temperature
        self.timeout = timeout
        self.max_retries = max_retries
        self.total_tokens = 0

    # ---- 构造 ----
    @classmethod
    def from_settings(cls, settings: Settings, store: SecretStore, provider: str | None = None) -> "LLMClient | None":
        provider = provider or settings.llm_provider
        preset = PROVIDERS.get(provider, PROVIDERS["custom"])
        base_url = preset["base_url"] if provider != "custom" else settings.llm_base_url
        model = preset["model"] if provider != "custom" else settings.llm_model
        key = store.get(provider) or ""

        if preset.get("needs_key") == "1" and not key:
            return None
        if not base_url or not model:
            return None
        return cls(
            base_url=base_url,
            api_key=key or "ollama",
            model=model,
            temperature=settings.llm_temperature,
            timeout=settings.llm_timeout,
        )

    # ---- 调用 ----
    @classmethod
    def from_vision_settings(cls, settings: Settings, store: SecretStore) -> "LLMClient | None":
        """构造"看图"客户端。未配置视觉模型、或所选服务没有视觉能力时返回 None。

        Key 复用文本模型的密钥库：比如文字用 DeepSeek、看图用智谱，
        只要在设置里给智谱存过 key（哪怕文字模型没选它）就能直接用。

        模型名可以用 settings.vision_model 覆盖预设——各家模型改名很勤
        （智谱 4V-Flash → 4.6V → 5.3 都叫过），写死了就废。
        """
        provider = (settings.vision_provider or "").strip()
        if not provider:
            return None
        preset = PROVIDERS.get(provider, PROVIDERS["custom"])
        override = (settings.vision_model or "").strip()
        if provider == "custom":
            base_url = (settings.vision_base_url or "").strip()
            model = override
        else:
            base_url = preset["base_url"]
            # 覆盖名必须还是这家的模型，否则说明是换供应商时残留的旧值
            model = override if override and _model_fits(provider, override) else (
                preset.get("vision_model") or ""
            )
        if not base_url or not model:
            return None
        key = store.get(provider) or ""
        if preset.get("needs_key") == "1" and not key:
            return None

        # 备用模型：免费模型排队限流是常态（实测同一时刻智谱 5 个视觉模型
        # 里 3 个在排队）。首选排不上队时自动换一个，别让整篇笔记的看图判断作废。
        backups = [b for b in preset.get("vision_backups", ()) if b != model]
        if backups:
            clients = [cls(
                base_url=base_url,
                api_key=key or "ollama",
                model=model,
            )]
            for b in backups:
                clients.append(cls(
                    base_url=base_url,
                    api_key=key or "ollama",
                    model=b,
                ))
            return FallbackVisionClient(clients, [model, *backups])

        return cls(
            base_url=base_url,
            api_key=key or "ollama",
            model=model,
            temperature=0.2,
            timeout=settings.llm_timeout,
        )

    def complete_vision(
        self,
        system: str,
        user: str,
        images: list[tuple[bytes, str]],
        *,
        max_tokens: int = 1024,
    ) -> str:
        """带图片的对话。images 为 (图片字节, mime) 列表，按 OpenAI 兼容格式编码。"""
        import base64 as _b64  # noqa: PLC0415

        content: list[dict[str, Any]] = [{"type": "text", "text": user}]
        for data, mime in images:
            content.append({
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{_b64.b64encode(data).decode('ascii')}"},
            })
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": content},
            ],
            "temperature": self.temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        prompt_tokens = _estimate_tokens(system) + _estimate_tokens(user)
        last_error = ""
        last_code = 0
        for attempt in range(1, self.max_retries + 1):
            try:
                with httpx.Client(timeout=self.timeout) as client:
                    resp = client.post(
                        f"{self.base_url}/chat/completions", json=payload, headers=headers
                    )
            except httpx.TimeoutException:
                last_error = "请求超时"
            except httpx.HTTPError as exc:
                last_error = f"网络错误：{type(exc).__name__}"
            else:
                if resp.status_code == 200:
                    return self._extract(resp.json(), prompt_tokens)
                last_error = self._translate_error(resp)
                last_code = resp.status_code
                if resp.status_code == 429:
                    # 限流是「排队」不是「报错」：同一个模型重试基本没用
                    # （单帧请求照样排），立刻抛出去让上层换备用模型。
                    raise LLMError(
                        f"看图模型调用失败：{last_error}",
                        http_status=429, rate_limited=True)
                if resp.status_code < 500:
                    raise LLMError(last_error, http_status=502)
            if attempt < self.max_retries:
                time.sleep(_backoff(attempt, last_code))
        raise LLMError(f"看图模型调用失败：{last_error}")

    def complete(
        self,
        system: str,
        user: str,
        *,
        json_mode: bool = False,
        max_tokens: int = 2048,
    ) -> str:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": self.temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        prompt_tokens = _estimate_tokens(system) + _estimate_tokens(user)
        last_error = ""
        last_code = 0

        for attempt in range(1, self.max_retries + 1):
            try:
                with httpx.Client(timeout=self.timeout) as client:
                    resp = client.post(
                        f"{self.base_url}/chat/completions", json=payload, headers=headers
                    )
            except httpx.TimeoutException:
                last_error = "请求超时"
            except httpx.HTTPError as exc:
                last_error = f"网络错误：{type(exc).__name__}"
            else:
                if resp.status_code == 200:
                    return self._extract(resp.json(), prompt_tokens)
                last_error = self._translate_error(resp)
                last_code = resp.status_code
                if resp.status_code == 429:
                    raise LLMError(last_error, http_status=429, rate_limited=True)
                if resp.status_code < 500:
                    raise LLMError(last_error, http_status=502)

            if attempt < self.max_retries:
                time.sleep(_backoff(attempt, last_code))

        raise LLMError(f"模型调用失败：{last_error}")

    def _extract(self, data: dict[str, Any], prompt_tokens: int) -> str:
        try:
            content = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError("模型返回格式异常") from exc
        usage = data.get("usage") or {}
        self.total_tokens += int(usage.get("total_tokens") or prompt_tokens)
        return content.strip()

    @staticmethod
    def _translate_error(resp: httpx.Response) -> str:
        code = resp.status_code
        if code == 401:
            raise AuthRequiredError("API Key 无效或已过期，请在设置里重新填写")
        if code == 402 or code == 403:
            return "账户余额不足或无权访问该模型"
        if code == 404:
            return "模型不存在，请检查模型名称"
        if code == 429:
            return "该模型当前排队/限流（免费模型高峰期常见）：稍后重试，或在设置里换一个模型名"
        detail = resp.text[:200].replace("\n", " ")
        return f"服务返回 {code}：{detail}"

    def complete_json(self, system: str, user: str, *, max_tokens: int = 2048) -> dict[str, Any]:
        raw = self.complete(system, user, json_mode=True, max_tokens=max_tokens)
        return parse_json_loose(raw)


def _close_truncated(text: str) -> str:
    """把被 max_tokens 截断的 JSON 补到能解析。

    模型输出到一半被截断时（例如画面要点里 points 数组写到第三条就停了），
    直接解析会失败，整批结果全丢——只能保住已完成的那部分。
    这里补齐缺失的括号、去掉末尾没写完的字符串。
    """
    stack: list[str] = []
    in_str = False
    esc = False
    for ch in text:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append(ch)
        elif ch in "}]" and stack:
            stack.pop()
    if in_str:                       # 末尾是半截字符串，砍掉
        cut = text.rfind('"')
        if cut > 0:
            text = text[:cut]
        text = text.rstrip().rstrip(",")
        # 砍完可能剩个悬空的 "key": ，一并去掉
        text = re.sub(r',?\s*"[^"]*"\s*:\s*$', "", text).rstrip().rstrip(",")
    return text + "".join("}" if c == "{" else "]" for c in reversed(stack))


def parse_json_loose(raw: str) -> dict[str, Any]:
    """模型常把 JSON 包在 ```json 里或前后加解释，这里做容错提取。"""
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else {"data": data}
    except json.JSONDecodeError:
        pass

    match = re.search(r"\{.*\}", text, re.S)
    if match:
        try:
            data = json.loads(match.group(0))
            return data if isinstance(data, dict) else {"data": data}
        except json.JSONDecodeError:
            pass

    # 被截断：补齐括号再试一次，保住已完成的那部分
    repaired = _close_truncated(text)
    if repaired != text:
        try:
            data = json.loads(repaired)
            if isinstance(data, dict) and data:
                log.warning("JSON 被截断，已按已完成部分解析：%s", text[:120])
                return data
        except json.JSONDecodeError:
            pass
    log.warning("JSON 解析失败，返回空结构：%s", text[:120])
    return {}


def test_connection(base_url: str, api_key: str, model: str, timeout: float = 20.0) -> tuple[bool, str]:
    """首启向导里的"测试连接"，用最小请求验证三元组是否可用。"""
    try:
        client = LLMClient(base_url, api_key or "ollama", model, timeout=timeout, max_retries=1)
        reply = client.complete("你是连通性测试助手。", "只回复两个字：正常", max_tokens=16)
        return True, reply[:40] or "已连通"
    except AuthRequiredError as exc:
        return False, exc.message
    except LLMError as exc:
        return False, exc.message
    except Exception as exc:  # noqa: BLE001
        return False, f"连接失败：{str(exc)[:120]}"
