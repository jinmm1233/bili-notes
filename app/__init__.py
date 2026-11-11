"""BiliNotes：B站视频智能笔记生成器。"""

import sys
from pathlib import Path

# 唯一版本来源：/healthz、FastAPI 元信息、/api/settings、iss 的 AppVersion 都跟它对齐。
# 改版本号时同步改 installer/BiliNotes.iss 里的 MyAppVersion。
__version__ = "0.2.6"

# GitHub Releases 更新源。上传到仓库后填成 "用户名/仓库名"，也可以用环境变量
# BILINOTES_GITHUB_REPO 覆盖，或直接在设置页里填（存到用户目录，不用重新打包）。
# 优先级：设置页 > 环境变量 > 这里写死的默认值。仓库必须是 Public，
# 否则匿名请求拿不到 Release 资产（404），内置更新等于废掉。
__update_repo__ = "jinmm1233/bili-notes"

# 导出 Word 用到的 python-docx / latex2mathml / mathml2omml 放在项目内的 vendor/，
# 不往用户机器的 site-packages 里装；开发态和 PyInstaller 打包态都从这里加载。
_vendor = Path(__file__).resolve().parent.parent / "vendor"
if _vendor.is_dir() and str(_vendor) not in sys.path:
    sys.path.insert(0, str(_vendor))
