"""繁简统一：笔记一律输出简体中文。

为什么需要：
- 本地语音转写（Whisper）遇到中文语音时，经常输出**繁体**（「邊緣系幫我們…」
  「後面用動詞圓形」），简体字在模型权重里更常见，但也确实会出繁体；
- B 站的 CC / AI 字幕本身也可能是繁体（很多外语视频用繁体字幕）；
- 视觉模型照着画面上的繁体字抄，也会把繁体带进「画面要点」。

统一在源头转一遍，导出时再兜底一遍，保证笔记里看不到繁体。

依赖 zhconv（纯 Python，无二进制依赖）。没装上也不影响运行——
转换退化为原样返回，绝不能因为缺个库就让整篇笔记生成失败。
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)

try:                        # 纯 Python 包，打包时由 build.spec 的 hiddenimports 带进去
    from zhconv import convert as _convert
except Exception as exc:    # pragma: no cover - 取决于运行环境
    _convert = None
    log.warning("繁简转换不可用（zhconv 未安装），笔记里可能保留繁体：%s", exc)


def to_simplified(text: str) -> str:
    """繁体转简体。已是简体或纯英文的内容原样返回。"""
    if not text or _convert is None:
        return text or ""
    try:
        return _convert(text, "zh-cn")
    except Exception as exc:
        log.warning("繁简转换失败，原样保留：%s", str(exc)[:120])
        return text


def has_traditional(text: str) -> bool:
    """文本里是否含繁体字（只用于日志与排查，不影响流程）。"""
    if not text:
        return False
    probe = "邊緣編譯語彙優級應爲裡機處結構體內記憶點時間觀點講議據認證據"
    return any(ch in text for ch in probe)
