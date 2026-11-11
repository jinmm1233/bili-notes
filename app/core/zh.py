"""繁简统一：笔记一律输出简体中文。

为什么需要：
- 本地语音转写（Whisper）遇到中文语音时，经常输出**繁体**（「邊緣系幫我們…」
  「後面用動詞圓形」），简体字在模型权重里更常见，但也确实会出繁体；
- B 站的 CC / AI 字幕本身也可能是繁体（很多外语视频用繁体字幕）；
- 视觉模型照着画面上的繁体字抄，也会把繁体带进「画面要点」。

统一在源头转一遍，导出时再兜底一遍，保证笔记里看不到繁体。

依赖 zhconv（纯 Python）。两个坑都处理了：
1. 它在 try/except 里导入，PyInstaller 静态分析扫不到 → build.spec 显式声明；
2. 它用 pkg_resources 找自己的数据文件，冻结环境里不可靠 →
   这里自己把字典读进内存再喂给它（loaddict 支持传绝对路径）。
没装上也不影响运行——转换退化为原样返回，绝不能因为缺个库就让整篇笔记生成失败。
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

log = logging.getLogger(__name__)

try:                        # 纯 Python 包，打包时由 build.spec 的 hiddenimports 带进去
    from zhconv import convert as _convert
except Exception as exc:    # pragma: no cover - 取决于运行环境
    _convert = None
    log.warning("繁简转换不可用（zhconv 未安装），笔记里可能保留繁体：%s", exc)

_TRAD_PROBE = "邊緣幫們為會極簡緣譯彙優級應處結構體內記憶點時間觀點講議據認證"


def _dict_candidates() -> list[Path]:
    """zhcdict.json 可能在哪。"""
    out: list[Path] = []
    base = getattr(sys, "_MEIPASS", None)
    if base:
        out.append(Path(base) / "zhconv" / "zhcdict.json")
        out.append(Path(base) / "zhcdict.json")
    here = Path(__file__).resolve().parent.parent      # app/core → app
    out.append(here / "zhconv" / "zhcdict.json")
    out.append(here / "zhcdict.json")
    return out


def _load_dict_explicitly() -> bool:
    """冻结环境里自己把字典读进 zhconv。

    zhconv 默认用 pkg_resources 找自己的数据文件，打包后那套机制不可用，
    结果就是「转换不报错、但一个字也没转」——最难发现的那种坏。
    这里直接找到 json 文件读进去，再交给它用。
    """
    if _convert is None:
        return False
    try:
        from zhconv import zhconv as _zc          # noqa: PLC0415
    except Exception:
        return False
    if getattr(_zc, "zhcdicts", None):
        return True
    for path in _dict_candidates():
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            data["SIMPONLY"] = frozenset(data["SIMPONLY"])
            data["TRADONLY"] = frozenset(data["TRADONLY"])
            _zc.zhcdicts = data
            log.info("繁简字典已从 %s 载入", path)
            return True
        except Exception as exc:
            log.warning("载入繁简字典失败 %s：%s", path, str(exc)[:120])
    return False


def to_simplified(text: str) -> str:
    """繁体转简体。已是简体或纯英文的内容原样返回。"""
    if not text or _convert is None:
        return text or ""
    if not _load_dict_explicitly():
        # 显式载入失败就退回包自己的加载逻辑（源码环境能成）
        pass
    try:
        return _convert(text, "zh-cn")
    except Exception as exc:
        log.warning("繁简转换失败，原样保留：%s", str(exc)[:120])
        return text


def has_traditional(text: str) -> bool:
    """文本里是否含繁体字（只用于日志与排查，不影响流程）。"""
    if not text:
        return False
    return any(ch in text for ch in _TRAD_PROBE)
