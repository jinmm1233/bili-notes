"""密钥归属审计（node 之外的纯 Python 检查，直接 `python tests/smoke_keys.py`）。

这条测试要守住的是一个产品承诺：**软件不内置任何 API Key，所有模型调用都用用户自己填的那把**。
只要有人（包括未来的我）图省事在代码里塞一把"公共 key"或"开发者试用 key"，这里就会红。

检查四件事：
  1. 源码里不存在形如 sk-xxx / Bearer xxx / api_key="xxx" 的字面量；
  2. 供应商预设表里没有 key / api_key / token 字段（预设只允许有 base_url、model 这类信息）；
  3. 本地密钥库为空时，LLMClient 必须返回 None（即"没填就不能用"，而不是偷偷降级到内置 key）；
  4. 本机 secrets.dat 里不能出现明文 key（必须整段是 DPAPI 密文）。
"""

from __future__ import annotations

import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FAILED = 0


def ok(cond: bool, msg: str) -> None:
    global FAILED
    print(("[OK] " if cond else "[FAIL] ") + msg)
    if not cond:
        FAILED += 1


# ---- 1. 源码里不能有密钥字面量 ----
PATTERNS = [
    ("OpenAI 风格 key", re.compile(r"sk-[A-Za-z0-9_\-]{16,}")),
    ("Bearer 令牌", re.compile(r"(?i)bearer\s+[A-Za-z0-9_\-\.]{20,}")),
    ("api_key 字面量", re.compile(r"(?i)api[_-]?key\s*[:=]\s*['\"][^'\"]{12,}['\"]")),
    ("access_token 字面量", re.compile(r"(?i)access[_-]?token\s*[:=]\s*['\"][^'\"]{16,}['\"]")),
]

SCAN_TARGETS: list[Path] = []
for pattern in ("app/**/*.py", "web/*.js", "web/*.html", "*.spec", "entry.py"):
    SCAN_TARGETS.extend(ROOT.glob(pattern))

hits: list[str] = []
for path in SCAN_TARGETS:
    if not path.is_file() or "__pycache__" in path.parts:
        continue
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        continue
    for label, rx in PATTERNS:
        for match in rx.finditer(text):
            snippet = match.group(0)[:40]
            hits.append(f"{path.relative_to(ROOT)} · {label} · {snippet}")

ok(not hits, f"源码中没有密钥字面量（扫描 {len(SCAN_TARGETS)} 个文件）")
for h in hits[:5]:
    print("      可疑：", h)

# ---- 2. 供应商预设不含密钥字段 ----
from app.core.llm import PROVIDERS  # noqa: E402

FORBIDDEN = {"key", "api_key", "apikey", "token", "access_token", "secret"}
leaky = [
    f"{name}:{field}"
    for name, preset in PROVIDERS.items()
    for field in preset
    if field.lower() in FORBIDDEN
]
ok(not leaky, f"{len(PROVIDERS)} 个供应商预设都没有内置密钥字段")

# ---- 3. 空密钥库时不允许调用模型 ----
from app.core.config import get_settings  # noqa: E402
from app.core.llm import LLMClient  # noqa: E402
from app.core.secretstore import SecretStore  # noqa: E402

with tempfile.TemporaryDirectory() as tmp:
    empty_store = SecretStore(Path(tmp) / "secrets.dat")
    client = LLMClient.from_settings(get_settings(), empty_store, "deepseek")
    ok(client is None, "没填 Key 时 LLMClient 返回 None（不会降级到任何内置 Key）")

    vision = LLMClient.from_vision_settings(get_settings(), empty_store)
    ok(vision is None, "没填 Key 时看图模型同样不可用")

# ---- 4. 本机密钥文件里不能有明文 ----
secrets_path = get_settings().data_dir / "secrets.dat"
if secrets_path.is_file():
    raw = secrets_path.read_bytes()
    plain = any(rx.search(raw.decode("utf-8", "ignore")) for _, rx in PATTERNS)
    ok(not plain, f"本机密钥文件是密文（{len(raw)} 字节，无明文 key）")
    ok(b"PLAIN:" not in raw, "本机密钥文件没有退化成明文存储")
else:
    print("[SKIP] 本机还没填过 Key，跳过密钥文件检查")

print()
if FAILED:
    print(f"{FAILED} 项未通过：模型 Key 的归属可能被破坏，先别发版。")
    sys.exit(1)
print("全部通过：软件只使用用户自己填的 Key。")
