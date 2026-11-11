"""转写层：faster-whisper 本地 CPU 推理。

用 CTranslate2 而非 PyTorch 是刻意选择——打包体积从 2GB+ 降到 30MB，
代价是不能用 GPU（本机也没有）。模型首次使用时自动下载到 models/。

模型下载必须可控：默认档位 500MB 级的文件在某些网络下要十几分钟，
用户只看到一句"加载中"会以为卡死了。这里自己接管下载，轮询目录体积
报进度，并支持取消。
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from ..core.config import Settings
from ..core.errors import CancelledError, DependencyMissingError
from ..core.http import stream_download
from ..core.models import Segment
from ..core import zh

if TYPE_CHECKING:                      # 只为类型注解，运行期不引入 numpy
    import numpy as np

log = logging.getLogger(__name__)

ProgressFn = Callable[[float, str], None]
CancelFn = Callable[[], bool]

# 混说补救用：中文段重转时的引导语（Whisper 会照着提示的语种/字形输出）
_MIXED_ZH_PROMPT = "以下是中文讲解，请用简体中文输出。"
_ZH_RE = re.compile(r"[\u4e00-\u9fff]")


def _zh_count(text: str) -> int:
    return len(_ZH_RE.findall(text or ""))

# 各档位的模型体积（MB，int8 量化后约等于 fp16 的一半）。
# 用于下载进度估算 —— 拿不到 Content-Length 时只能按这个走。
_MODEL_SIZE_MB = {
    "tiny": 75,
    "tiny.en": 75,
    "base": 145,
    "base.en": 145,
    "small": 484,
    "small.en": 484,
    "medium": 1530,
    "large-v3": 3090,
}

# 下载哪些文件就够推理了，其余（如 .txt 词表、onnx 目录）不下，省一半体积
_MODEL_FILES = ["config.json", "model.bin", "tokenizer.json", "vocabulary.txt", "preprocessor_config.json"]

# 相对实时的倍率（转写耗时 / 音频时长）。实测（i7-11370H 4核8线程、int8、beam=1、VAD，
# 17 分钟音频）：tiny 133s = 0.13x，base 112s = 0.11x。
# base 竟然快于 tiny，说明这档规模下解码不是瓶颈、VAD 裁剪影响更大。
# 取值比实测略悲观，避免让用户觉得"说好的 3 分钟变成 10 分钟"。
_SPEED_FACTOR = {
    "tiny": 0.15,
    "tiny.en": 0.15,
    "base": 0.15,
    "base.en": 0.15,
    "small": 0.35,
    "small.en": 0.35,
    "medium": 0.80,
    "large-v3": 1.50,
}


def estimate_seconds(duration: float, model: str) -> float:
    """预估转写耗时（秒）。只用于给用户心理预期，不作为调度依据。"""
    return duration * _SPEED_FACTOR.get(model, 0.4)


# 语种检测时的采样窗口（秒）：开头 / 中间 / 尾部各取一段。
# 只看开头是不够的 —— 大量视频前 30 秒是片头曲或空镜，
# VAD 裁掉后剩下的静音会把语种判成 "nn"（无语音）。
_DETECT_WINDOW_S = 25.0
_DETECT_STARTS = (0.05, 0.45, 0.85)


def detect_language(model, audio, cancel: CancelFn | None = None) -> tuple[str, float]:
    """用三段采样投票判断音频语种，返回 (语言码, 置信度 0-1)。

    硬编码 zh 的代价：英文视频会被强行"听写"成汉字，整篇笔记是音译垃圾。
    而全量自动检测又太慢（等于多跑一遍转写），所以只在 3 段各 25 秒的
    采样上跑，约 10 秒出结果，对十分钟的视频可以忽略。
    """
    sr = 16000
    total = len(audio)
    if total < sr * 3:
        return "zh", 0.0

    duration = total / sr
    windows: list[tuple[int, int]] = []
    if duration <= _DETECT_WINDOW_S * 1.5:
        windows = [(0, total)]
    else:
        for ratio in _DETECT_STARTS:
            lo = int(duration * ratio * sr)
            hi = min(total, lo + int(_DETECT_WINDOW_S * sr))
            if hi - lo >= sr * 2:
                windows.append((lo, hi))
    if not windows:
        windows = [(0, total)]

    votes: dict[str, float] = {}
    for lo, hi in windows:
        if cancel and cancel():
            raise CancelledError("任务已取消")
        _, info = model.transcribe(
            audio[lo:hi],
            beam_size=1,                     # 只要语种，不需要搜索
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 500, "speech_pad_ms": 200},
            condition_on_previous_text=False,
        )
        lang = str(getattr(info, "language", "") or "").strip()
        prob = float(getattr(info, "language_probability", 0.0) or 0.0)
        # "nn" 是 faster-whisper 对"没听出人声"的返回值，不能当语种
        if not lang or lang == "nn" or prob <= 0:
            continue
        votes[lang] = votes.get(lang, 0.0) + prob

    if not votes:
        return "zh", 0.0
    best = max(votes.items(), key=lambda kv: kv[1])
    # 票数除以窗口数，得到 0-1 的置信度
    return best[0], min(1.0, best[1] / len(windows))


def resolve_language(settings: Settings, model, audio, cancel: CancelFn | None = None) -> str:
    """决定转写用的语种：配置指定 > 模型自带语种 > 采样检测。"""
    configured = (settings.asr_language or "auto").strip().lower()
    if configured and configured != "auto":
        return configured
    # .en 模型只有英文词表，传 zh 会直接报 tokenizer 错
    if settings.asr_model.endswith(".en"):
        return "en"
    lang, _prob = detect_language(model, audio, cancel)
    return lang or "zh"


def decode_audio(path: str, sample_rate: int = 16000) -> "np.ndarray":
    """用 ffmpeg 解码成 16kHz 单声道 float32 数组。

    不走 faster-whisper 内置的 PyAV 解码：PyAV 19 移除了 `metadata_errors`
    参数，而 faster-whisper 1.2 仍在传它，于是
        TypeError: open() got an unexpected keyword argument 'metadata_errors'
    转写还没开始就崩了。ffmpeg 本来就是必需依赖（抽帧、合并音视频都用它），
    自己解成 PCM 交给模型，既绕开 PyAV 的版本坑，也不受容器格式限制。
    """
    import numpy as np  # noqa: PLC0415

    from ..core.ffmpeg import find_binary  # noqa: PLC0415

    exe = find_binary("ffmpeg")
    args = [
        exe, "-hide_banner", "-loglevel", "error", "-i", str(path),
        "-f", "s16le", "-acodec", "pcm_s16le",
        "-ac", "1", "-ar", str(sample_rate), "-",
    ]
    proc = subprocess.run(
        args,
        capture_output=True,
        timeout=1800,
        creationflags=0x08000000 if os.name == "nt" else 0,
    )
    if proc.returncode != 0:
        raise DependencyMissingError(
            f"音频解码失败：{proc.stderr.decode('utf-8', 'replace')[-200:]}"
        )
    if not proc.stdout:
        raise DependencyMissingError("音频解码结果为空，文件可能损坏")

    audio = np.frombuffer(proc.stdout, dtype=np.int16).astype(np.float32)
    audio /= 32768.0
    return audio


def _dir_size_mb(path: Path) -> float:
    total = 0
    for item in path.rglob("*"):
        try:
            if item.is_file():
                total += item.stat().st_size
        except OSError:
            continue
    return total / 1048576


def _model_dir(settings: Settings, model: str) -> Path:
    return Path(settings.models_dir) / f"fw-{model.replace('/', '_')}"


def _http_download(
    url: str,
    dest: Path,
    on_chunk: Callable[[int], None] | None = None,
    cancel: CancelFn | None = None,
    timeout: float = 120.0,
) -> int:
    """下载单个文件（实现见 core.http，此处保留名字便于测试与调用）。"""
    return stream_download(url, dest, on_chunk=on_chunk, cancel=cancel, timeout=timeout)


def ensure_model(
    settings: Settings,
    on_progress: ProgressFn | None = None,
    cancel: CancelFn | None = None,
) -> Path:
    """确保模型已下载到本地，返回模型目录。

    已有完整模型时立即返回（不联网）。否则按文件逐个下，
    进度用已下载字节 / Content-Length 计算，比轮询目录体积准得多。
    """
    model = settings.asr_model
    target = _model_dir(settings, model)

    if (target / "model.bin").is_file() and (target / "config.json").is_file():
        return target

    target.mkdir(parents=True, exist_ok=True)
    base_url = f"{settings.hf_endpoint.rstrip('/')}/Systran/faster-whisper-{model}/resolve/main"
    expected_mb = _MODEL_SIZE_MB.get(model, 484)

    if on_progress:
        on_progress(0.0, f"首次使用，正在下载 {model} 模型（约 {expected_mb:.0f}MB）…")

    started = time.time()
    done_bytes = [0]          # 已完成的文件累计
    last_emit = [0.0]

    def report(current_bytes: int) -> None:
        """下载过程中实时报进度，最多每 0.4 秒一次，避免刷屏。"""
        now = time.time()
        if not on_progress or now - last_emit[0] < 0.4:
            return
        last_emit[0] = now
        done_mb = (done_bytes[0] + current_bytes) / 1048576
        pct = min(99.0, done_mb / expected_mb * 100.0)
        speed = done_mb / max(0.1, now - started) * 60
        on_progress(
            pct,
            f"下载 {model} 模型 {pct:.0f}%（{done_mb:.0f}/{expected_mb:.0f}MB，"
            f"{speed:.0f}MB/分钟）",
        )

    for name in _MODEL_FILES:
        dest = target / name
        url = f"{base_url}/{name}"
        try:
            size = _http_download(
                url, dest,
                on_chunk=report,
                cancel=cancel,
                timeout=180.0,
            )
            done_bytes[0] += size
        except FileNotFoundError:
            # vocabulary.txt / preprocessor_config.json 并非每个仓库都有
            log.debug("模型文件不存在，跳过：%s", name)
            continue
        except CancelledError:
            raise
        except Exception as exc:
            got = done_bytes[0] / 1048576
            raise DependencyMissingError(
                f"语音模型 {model} 下载失败（已下 {got:.0f}/{expected_mb:.0f}MB）："
                f"{str(exc)[:110]}。可重试继续下载（支持断点续传），"
                "或在设置里换成 tiny 档位（仅 75MB）。",
                detail={"models_dir": str(target), "model": model, "url": url},
            ) from exc

    if not (target / "model.bin").is_file():
        raise DependencyMissingError(
            "模型下载不完整（缺少 model.bin），请重试或在设置里换用 tiny 档位"
        )

    if on_progress:
        on_progress(100.0, f"{model} 模型就绪（{_dir_size_mb(target):.0f}MB）")
    log.info("模型 %s 就绪，用时 %.1fs", model, time.time() - started)
    return target


def _zh_scan(
    settings: Settings,
    model,
    audio: "np.ndarray",
    cancel: CancelFn | None = None,
) -> list[tuple[float, float, str, float]]:
    """第二遍：用中文语种把全片扫一遍。

    返回 (start, end, 文本, avg_logprob)。

    为什么必须有这一遍：整段只有一个语种参数时，外语原声占多数 → 整段按英语转，
    而博主讲中文的那几秒**根本产不出段落**（实测 en 遍在那些位置 avg_logprob=-9，
    相当于「没有这段语音」）。所以中文不是被转坏了，是压根没被听见。
    """
    segs, _info = model.transcribe(
        audio,
        language="zh",
        beam_size=1,                    # 只用来定位，精度靠后面的重转
        vad_filter=settings.asr_vad,
        vad_parameters={"min_silence_duration_ms": 500, "speech_pad_ms": 200},
        condition_on_previous_text=False,
        initial_prompt=_MIXED_ZH_PROMPT,
    )
    out: list[tuple[float, float, str, float]] = []
    for s in segs:
        if cancel and cancel():
            raise CancelledError("任务已取消")
        text = (s.text or "").strip()
        if text:
            out.append((float(s.start), float(s.end), text,
                        float(getattr(s, "avg_logprob", 0.0) or 0.0)))
    return out


def _pick_zh_windows(
    zh_segs: list[tuple[float, float, str, float]],
    first: list[Segment],
    first_lp: list[float],
    margin: float = 0.15,
) -> list[tuple[float, float, str, float]]:
    """从中文扫描结果里，挑出「这一段本来就是中文语音」的部分。

    判据（不用 logprob 跨语种比较——实测不可靠：TED 纯英文视频里
    中文硬转的乱码 logprob 反而比正经英文高，会把英文全改成中文）：

    1. **外语遍自己都吐了中文**：说明那段确实是中文语音，外语解码器认出来了
       （哪怕只认出几个字，也是强信号）；
    2. 或者**外语遍在那儿完全哑了**（一个段都没出）：中文语音让外语解码器
       直接放弃输出，这是陪跑视频里最典型的形态（实测 en 遍 avg_logprob=-9）。

    两条都不满足的区间一律不动，纯外语视频因此完全不受影响。
    """
    picked: list[tuple[float, float, str, float]] = []
    for st, en, text, _lp in zh_segs:
        zh = _zh_count(text)
        if zh < 4:
            continue
        # 中文密度太低的多半是英文硬转的噪声
        if zh < 0.25 * len(text.replace(" ", "")):
            continue
        seg_zh = 0
        has_seg = False
        for seg in first:
            if min(en, seg.end) - max(st, seg.start) > 0.3:
                has_seg = True
                seg_zh += _zh_count(seg.text)
        if not (seg_zh >= 2 or not has_seg):
            continue
        picked.append((st, en, text, _lp))
    return picked


def _merge_with_zh(
    first: list[Segment],
    winners: list[tuple[float, float, str, float]],
) -> list[Segment]:
    """把中文区域的段替换进外语结果里。"""
    out: list[Segment] = []
    for seg in first:
        covered = False
        for st, en, _text, _lp in winners:
            if min(seg.end, en) - max(seg.start, st) > 0.5 * (en - st):
                covered = True
                break
        if not covered:
            out.append(seg)
    for st, en, text, _lp in winners:
        out.append(Segment(start=st, end=en, text=zh.to_simplified(text), source="asr"))
    out.sort(key=lambda s: s.start)
    return out


def transcribe(
    settings: Settings,
    audio_path: str,
    on_progress: ProgressFn | None = None,
    cancel: CancelFn | None = None,
) -> list[Segment]:
    try:
        from faster_whisper import WhisperModel  # noqa: PLC0415
    except ImportError as exc:
        raise DependencyMissingError(
            "未安装 faster-whisper，无法进行语音转写（可改用仅有字幕的视频）"
        ) from exc

    os.environ.setdefault("HF_ENDPOINT", settings.hf_endpoint)
    os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

    model_path = ensure_model(settings, on_progress, cancel)

    if on_progress:
        on_progress(0.0, f"加载 {settings.asr_model} 模型到内存…")

    started = time.time()
    try:
        model = WhisperModel(
            str(model_path),
            device=settings.asr_device,
            compute_type=settings.asr_compute_type,
            local_files_only=True,
        )
    except Exception as exc:
        raise DependencyMissingError(
            f"语音模型 {settings.asr_model} 加载失败：{str(exc)[:160]}",
            detail={"model_dir": str(model_path)},
        ) from exc

    load_elapsed = time.time() - started

    if on_progress:
        on_progress(0.0, "解码音频…")
    audio = decode_audio(audio_path)
    audio_seconds = len(audio) / 16000.0

    language = resolve_language(settings, model, audio, cancel)
    self_check = (settings.asr_language or "auto").strip().lower() in ("", "auto")
    if on_progress:
        hint = "（自动检测）" if self_check else "（设置指定）"
        on_progress(0.0, f"识别语种：{language}{hint}，开始转写…")
    log.info("转写语种 %s（自动检测=%s）", language, self_check)

    segments_iter, info = model.transcribe(
        audio,
        language=language,
        beam_size=settings.asr_beam_size,
        vad_filter=settings.asr_vad,
        vad_parameters={"min_silence_duration_ms": 500, "speech_pad_ms": 200},
        condition_on_previous_text=False,  # 防止长视频里的重复幻觉
    )

    total = float(getattr(info, "duration", 0) or 0) or audio_seconds
    result: list[Segment] = []
    raw_texts: list[str] = []          # 与 result 一一对应：第一遍转出来的原文
    first_lp: list[float] = []         # 与 result 一一对应：第一遍每段的置信度
    # 剩余时间用滑动窗口估：全局平均会把模型加载、音频解码的时间算进去，
    # 开头几秒得出的"还要 16 分钟"完全是误导（真实只要 2 分钟）。
    samples: list[tuple[float, float]] = []
    for seg in segments_iter:
        if cancel and cancel():
            raise CancelledError("任务已取消")
        text = (seg.text or "").strip()
        if text:
            result.append(Segment(start=float(seg.start), end=float(seg.end),
                                  text=zh.to_simplified(text), source="asr"))
            raw_texts.append(text)
            first_lp.append(float(getattr(seg, "avg_logprob", 0.0) or 0.0))
        if on_progress and total > 0:
            pct = min(99.0, float(seg.end) / total * 100.0)
            done_s = float(seg.end)
            now = time.time()
            samples.append((now, done_s))

            remain = ""
            recent = [(t, d) for t, d in samples if now - t <= 90]
            if len(recent) >= 3:
                dt = recent[-1][0] - recent[0][0]
                dd = recent[-1][1] - recent[0][1]
                if dt >= 8 and dd >= 8:
                    rate = dt / dd
                    left = (total - done_s) * rate
                    # 少于 20 秒就不报了，免得最后一直闪
                    if left > 20:
                        remain = f"，约剩 {left / 60:.0f} 分钟" if left > 60 else f"，约剩 {left:.0f} 秒"
            on_progress(pct, f"转写 {pct:.0f}%（{len(result)} 句{remain}）")

    # ---- 混说补救：外语原声 + 中文讲解的视频，中文部分得单独再认一遍 ----
    # 陪跑类视频（博主播放外语音频 + 自己用中文补充讲解）整段只有一个语种参数：
    # 课程英文占绝对多数 → 整段按英语转 → 博主讲中文的那几秒**根本产不出段落**，
    # 补充讲解等于全丢（实测 en 遍在那些位置的 avg_logprob = -9，等于「没这段」）。
    #
    # 补救：再扫一遍全片（这次用中文语种），用置信度对比挑出「本来就是中文语音」
    # 的区间，把外语结果里对应的段换成中文结果。
    # 触发条件：第一遍输出里已经带中文（免费信号）——纯外语视频不会有，零额外开销。
    if (settings.asr_mixed_language and language != "zh" and result):
        # 触发信号：外语遍自己都吐出了中文 —— 说明这段音频里确实有中文语音。
        # 纯外语视频不会命中，零额外开销。
        suspected = sum(_zh_count(t) for t in raw_texts) >= 4
        if suspected:
            log.info("检测到疑似中英混说（外语为主但夹中文），开始中文补扫…")
            if on_progress:
                on_progress(99.0, "检测到中英混说，正在补扫中文段…")
            zh_segs = _zh_scan(settings, model, audio, cancel=cancel)
            winners = _pick_zh_windows(zh_segs, result, first_lp)
            if winners:
                log.info("混说补救：%d 段改用中文识别", len(winners))
                result = _merge_with_zh(result, winners)
            else:
                log.info("混说补救：没找到确属中文语音的段，保持原样")
        else:
            log.info("外语转写里没有中文，按纯外语处理")

    elapsed_total = time.time() - started
    log.info(
        "转写完成 segments=%d 总耗时=%.1fs（加载 %.1fs）音频=%.0fs 实时倍率=%.1fx",
        len(result), elapsed_total, load_elapsed, total,
        elapsed_total / total if total else 0,
    )
    if on_progress:
        on_progress(100.0, f"转写完成，共 {len(result)} 句（用时 {elapsed_total / 60:.0f} 分钟）")
    return result
