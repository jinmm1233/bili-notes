"""API 冒烟：启动服务、验证关键端点、密钥加密往返。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.main import create_app  # noqa: E402

FAILS: list[str] = []

# 必须把数据目录指到临时位置再建 app。
# 这里的密钥往返测试会「保存再删除」API Key，如果指向真实目录，
# 跑一次测试就把用户填好的 Key 抹掉了（真实踩过）。
_TMP = Path(__file__).resolve().parents[1] / ".tmp_test"
_TMP.mkdir(exist_ok=True)
_settings = get_settings()
_settings.data_dir = _TMP / "data"
_settings.output_dir = _TMP / "output"
_settings.data_dir.mkdir(parents=True, exist_ok=True)
_settings.output_dir.mkdir(parents=True, exist_ok=True)


def check(cond: bool, label: str) -> None:
    print(("  ok  " if cond else "  FAIL") + f"  {label}")
    if not cond:
        FAILS.append(label)


def main() -> int:
    app = create_app()
    with TestClient(app) as client:
        print("[1] 基础端点")
        r = client.get("/healthz")
        check(r.status_code == 200 and r.json()["ok"], "健康检查")

        r = client.get("/")
        check(r.status_code == 200 and "BiliNotes" in r.text, "首页可访问")
        check("总览" in r.text and "知识库" in r.text, "前端多视图已加载")

        print("\n[2] 配置")
        r = client.get("/api/providers")
        provs = r.json()["providers"]
        check(len(provs) >= 6, f"供应商 {len(provs)} 个")
        check(any(p["id"] == "deepseek" for p in provs), "含 DeepSeek")
        check(all("masked" in p for p in provs), "返回脱敏字段")

        r = client.get("/api/settings")
        s = r.json()
        check("output_dir" in s and "asr_model" in s, "设置字段完整")
        check("has_key" in s and "ready" in s, "含就绪状态")

        print("\n[3] 密钥加密往返")
        r = client.post("/api/settings/key", json={"provider": "deepseek", "key": "sk-test-1234567890abcdef"})
        check(r.status_code == 200, "保存密钥")
        masked = r.json().get("masked", "")
        check(masked.startswith("sk-") and "**" in masked, f"脱敏显示 {masked}")
        check("1234567890abcdef" not in masked, "明文不外泄")

        store = app.state.store
        check(store.get("deepseek") == "sk-test-1234567890abcdef", "可正确解密读取")

        secrets_file = Path(store.path)
        raw = secrets_file.read_text(encoding="utf-8")
        check("sk-test-1234567890" not in raw, "磁盘上无明文密钥")
        check(secrets_file.stat().st_size > 0, f"密文文件 {secrets_file.stat().st_size} 字节")

        r = client.delete("/api/settings/key/deepseek")
        check(r.status_code == 200 and store.get("deepseek") is None, "删除密钥")

        print("\n[4] 错误处理")
        r = client.get("/api/tasks/nonexistent")
        check(r.status_code == 404, "不存在的任务返回 404")
        body = r.json()
        check("detail" in body or "message" in body, "错误结构规范")
        check("Traceback" not in r.text, "不泄露堆栈")

        r = client.post("/api/tasks", json={"url": "x"})
        check(r.status_code in (400, 422), "非法输入被拒绝")

        print("\n[5] 参数校验")
        r = client.post("/api/tasks", json={"url": "BV1xx411c7mD", "height": 5000})
        check(r.status_code == 422, "越界参数被拒绝")

    print("\n" + "=" * 40)
    if FAILS:
        print(f"失败 {len(FAILS)} 项：{FAILS}")
        return 1
    print("API 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
