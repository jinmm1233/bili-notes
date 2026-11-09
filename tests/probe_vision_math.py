"""探一次真实看图模型：给张含矩阵的 PPT 图，看它到底能不能读出 LaTeX 矩阵。

用法：python tests/probe_vision_math.py <图片路径>
"""
import sys
from pathlib import Path

sys.path.insert(0, ".")

from app.core.config import get_settings
from app.core.llm import LLMClient
from app.core.secretstore import SecretStore
from app.services import frame_caption as fc

settings = get_settings()
store = SecretStore(settings.data_dir / "secrets.dat")
vision = LLMClient.from_vision_settings(settings, store)
print("看图模型：", vision.model if vision else "（不可用）")

img = sys.argv[1] if len(sys.argv) > 1 else ""
if not img or not Path(img).exists():
    print("给个图片路径")
    sys.exit(1)

data, mime = fc._shrink_jpeg(img)
user = (
    "【讲解文字】把原矩阵和单位矩阵并排，通过初等行变换把左边化成单位矩阵，右边就是逆矩阵。\n"
    "【文字线索】（略）\n"
    "请提取画面1（第76秒）上的文字要点，只返回约定格式的 JSON。"
)
raw = vision.complete_vision(fc._SYSTEM, user, [(data, mime)], max_tokens=900)
print("=== 模型原始返回 ===")
print(raw[:2000])
