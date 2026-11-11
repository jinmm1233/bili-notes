"""配置：全部来自环境变量 / .env，启动时集中校验，缺失即快速失败。"""
from __future__ import annotations

import json
from pathlib import Path

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .paths import bundle_root, default_output_dir, user_data_dir

# 三档笔记详细度。每档同时定「候选生成得多密」和「每段留几张」——
# 只放配额不改密度的话，详细档也只是从 29 张候选里多选几张，照样会漏。
DETAIL_PRESETS: dict[str, dict[str, float | int]] = {
    # 三档的**判定标准完全一致**：都按「画面停住、内容放全」取帧，
    # 也都能看到同样多的候选画面。唯一差别就是「每段最多留几张」。
    # 全片不再设总配额——总量由「段数 × 每段上限」自然决定，
    # 否则长视频会被二次砍一刀，又变成那种「明明讲到了却没图」。
    "brief": {"max_frames_per_chunk": 5, "max_frames_total": 0},
    "standard": {"max_frames_per_chunk": 8, "max_frames_total": 0},
    "detailed": {"max_frames_per_chunk": 0, "max_frames_total": 0},   # 0 = 不限
}
DETAIL_LABELS = {"brief": "简洁", "standard": "适中", "detailed": "详细"}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="BILINOTES_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---- 服务 ----
    host: str = "127.0.0.1"
    port: int = Field(default=8765, ge=1024, le=65535)
    open_browser: bool = True
    # ---- 双端（手机访问）----
    # 默认只听本机。打开后监听 0.0.0.0，同一 WiFi 的手机能打开，
    # 但必须带访问口令，否则同网段谁都能翻你的笔记。
    lan_access: bool = False
    lan_token: str = ""

    # ---- 目录 ----
    data_dir: Path = Field(default_factory=user_data_dir)
    output_dir: Path = Field(default_factory=default_output_dir)
    cache_max_gb: float = Field(default=5.0, gt=0)

    # ---- ffmpeg ----
    ffmpeg_path: str = ""

    # ---- 转写 ----
    asr_model: str = "base"
    asr_device: str = "cpu"
    asr_compute_type: str = "int8"
    asr_beam_size: int = 5
    asr_vad: bool = True
    # auto = 先用 3 段采样检测语种再转写；也可直接指定（如英文科普视频写 en）
    asr_language: str = "auto"
    hf_endpoint: str = "https://hf-mirror.com"
    # B站 AI 字幕接口返回的内容不可信：同一 bvid 多次请求会拿到
    # 时长相近但内容完全无关的其他视频字幕（实测越南旅游视频
    # 拿到了小米SU7 试驾的字幕，覆盖率还刚好达标）。
    # 拿到错内容的代价远大于多花两分钟转写，所以默认关闭。
    use_ai_subtitle: bool = False

    # ---- 转录润色 ----
    # 转录文本（字幕/ASR）往往断句混乱、外语无译文。用 LLM 在"出最终转录"
    # 这一步统一补标点、给外语加中文译文（一行原文一行译文）。
    # 两者都只在配置了模型 Key 时生效；没有 Key 则原样保留。
    polish_punctuation: bool = True       # 智能标点：补全/修正断句符号，不改写词句
    bilingual_transcript: bool = True     # 非中文转录加中文译文（一行外语一行中文）
    # 画面要点：从关键帧里「有选择地」提取画面文字（PPT 题目/分点/公式），
    # 内容太简略的用讲解补全；产出同时回喂总结层，笔记结构贴着课件骨架走。
    frame_captions: bool = True
    # 用看图模型挑画面（默认开）：同一次视觉调用里顺手判「这帧有没有信息、
    # 是不是和前面同一页」，据此剔掉纯人物/空镜/同页重复的图。
    # 没有配看图模型时自动不生效；关掉就退回按像素打分选图。
    vision_frame_judge: bool = True

    # ---- 笔记详细度（三档，用户只选一个）----
    # 档位是唯一真相：下面这几个抽帧参数由它派生，避免「档位选了详细、配额还是 3」。
    # 每段/全片写 0 表示不限——详细档就是靠这个把画面全部保留。
    note_detail: str = "standard"
    vision_max_frames: int = Field(default=80, ge=0)   # 超过就退回文字模式，防视觉请求爆炸

    # ---- 抽帧 ----
    # 稳定判定阈值：越小越敏感 → 轻微晃动就算「还在动」→ 稳定段更难形成。
    # 所以它不随档位变，固定在 14。
    scene_phash_distance: int = Field(default=14, ge=4, le=40)
    scene_min_gap: float = Field(default=1.5, gt=0)
    uniform_interval: float = Field(default=6.0, gt=0)
    max_frames_total: int = Field(default=0, ge=0)      # 0 = 不限，由每段上限自然决定
    max_frames_per_chunk: int = Field(default=8, ge=0)  # 0 = 不限（详细档）
    frame_max_edge: int = 1280
    hd_frames: bool = True
    hd_frame_edge: int = 1920
    frame_jpeg_quality: int = 85
    ocr_min_confidence: float = 0.60

    # ---- 分块 ----
    chunk_target_chars: int = Field(default=800, ge=200)
    chunk_max_chars: int = Field(default=1200, ge=400)
    chunk_overlap_chars: int = Field(default=80, ge=0)

    # ---- LLM（key 不在此处，存 DPAPI 加密文件）----
    llm_provider: str = "deepseek"
    llm_base_url: str = "https://api.deepseek.com/v1"
    llm_model: str = "deepseek-chat"
    llm_temperature: float = Field(default=0.3, ge=0, le=2)
    llm_timeout: float = Field(default=120.0, gt=0)
    llm_max_concurrency: int = Field(default=4, ge=1, le=16)

    # ---- 任务 ----
    worker_concurrency: int = Field(default=2, ge=1, le=8)
    parallel_pipeline: bool = True
    # 处理完如何处置下载的视频/音频（占空间的大头）：
    #   media 只删媒体文件（默认，省 ~85%，保留 json 可复查）
    #   all   连抽帧和中间 json 一起删（最省空间）
    #   keep  全部保留（要重跑同一视频、或排查问题时用）
    cache_policy: str = "media"
    # 旅行类视频自动生成「行程路线」一节（地点顺序 + 交通 + 花费）
    travel_itinerary: bool = True
    # 看图模型（可选）：让 AI 真正"看"关键帧画面来写解读，而不是只靠 OCR 文字。
    # 空 = 关闭（解读退回"讲解+画面文字"模式）。填 provider id，如 zhipu / qwen / ollama / custom
    vision_provider: str = ""
    vision_base_url: str = ""     # 仅 vision_provider=custom 时使用
    vision_model: str = ""        # 仅 vision_provider=custom 时使用

    @field_validator("note_detail")
    @classmethod
    def _check_detail(cls, v: str) -> str:
        key = (v or "").strip().lower()
        if key not in DETAIL_PRESETS:
            raise ValueError(f"note_detail 必须是 {sorted(DETAIL_PRESETS)} 之一，收到 {v}")
        return key

    @model_validator(mode="after")
    def _apply_detail(self) -> "Settings":
        """档位覆盖具体抽帧参数。

        档位是唯一真相：用户在界面上选「详细」，却因为 config.json 里还留着
        旧的 max_frames_per_chunk=3 而只留 3 张，这种自相矛盾必须从根上断掉。
        """
        for key, value in DETAIL_PRESETS[self.note_detail].items():
            setattr(self, key, value)
        return self

    @field_validator("asr_model")
    @classmethod
    def _check_model(cls, v: str) -> str:
        allowed = {"tiny", "base", "small", "medium", "large-v3"}
        if v not in allowed:
            raise ValueError(f"asr_model 必须是 {sorted(allowed)} 之一，收到 {v}")
        return v

    @field_validator("asr_language")
    @classmethod
    def _check_lang(cls, v: str) -> str:
        allowed = {
            "auto", "zh", "en", "yue", "ja", "ko", "fr", "de", "ru",
            "es", "pt", "it", "ar", "hi", "th", "vi", "id", "tr",
        }
        value = (v or "auto").strip().lower()
        if value not in allowed:
            raise ValueError(f"asr_language 必须是 auto 或 {sorted(allowed)} 之一，收到 {v}")
        return value

    @field_validator("cache_policy")
    @classmethod
    def _check_cache_policy(cls, v: str) -> str:
        allowed = {"keep", "media", "all"}
        value = (v or "media").strip().lower()
        if value not in allowed:
            raise ValueError(f"cache_policy 必须是 {sorted(allowed)} 之一，收到 {v}")
        return value

    @field_validator("asr_compute_type")
    @classmethod
    def _check_compute(cls, v: str) -> str:
        allowed = {"int8", "int8_float16", "float16", "float32"}
        if v not in allowed:
            raise ValueError(f"asr_compute_type 必须是 {sorted(allowed)} 之一")
        return v

    # ---- 派生路径 ----
    @property
    def cookies_file(self) -> Path:
        return self.data_dir / "cookies.txt"

    @property
    def models_dir(self) -> Path:
        path = self.data_dir / "models"
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def cache_dir(self) -> Path:
        path = self.data_dir / "cache"
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def logs_dir(self) -> Path:
        path = self.data_dir / "logs"
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def static_dir(self) -> Path:
        return bundle_root() / "web"

    def ensure_dirs(self) -> None:
        for p in (self.data_dir, self.output_dir, self.models_dir, self.cache_dir, self.logs_dir):
            p.mkdir(parents=True, exist_ok=True)


_settings: Settings | None = None


def _config_file() -> Path:
    """跟着当前 Settings 的 data_dir 走。

    原来写死 `user_data_dir()`，导致测试把 data_dir 指到临时目录也拦不住它，
    一路写进真实配置——一个非法档位就是这么被写坏、让下次启动直接起不来的。
    `get_settings()` 初始化期间 `_settings` 还是 None，那时才退回默认位置。
    """
    if _settings is not None:
        return _settings.data_dir / "config.json"
    return user_data_dir() / "config.json"


def load_overrides() -> dict:
    """用户通过界面保存的偏好（不含密钥）。"""
    path = _config_file()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def save_overrides(updates: dict) -> None:
    """合并写入 config.json —— 只允许白名单字段，避免把密钥写进来。"""
    allowed = {
        "llm_provider", "llm_base_url", "llm_model", "asr_model",
        "output_dir", "max_frames_total", "scene_phash_distance",
        "llm_max_concurrency", "cache_policy", "open_browser",
        "lan_access", "lan_token",
        "uniform_interval", "asr_vad", "hd_frames", "hd_frame_edge", "parallel_pipeline",
        "use_ai_subtitle", "asr_language", "polish_punctuation", "bilingual_transcript",
        "frame_captions", "vision_frame_judge", "travel_itinerary",
        # 笔记详细度：档位是真相，max_frames_* / scene_* / uniform_interval 由它派生
        "note_detail", "max_frames_per_chunk", "scene_min_gap", "vision_max_frames",
        "vision_provider", "vision_base_url", "vision_model",
    }
    path = _config_file()
    current = load_overrides()
    for key, value in updates.items():
        if key in allowed:
            current[key] = str(value) if isinstance(value, Path) else value

    # 先校验再落盘：以前是「先写文件再 Settings(**current)」，一个非法值
    # （比如 note_detail="nope"）会先被写进 config.json、再抛异常，
    # 于是下次启动直接起不来。校验失败时磁盘必须保持原样。
    candidate = Settings(**current)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")

    global _settings
    _settings = candidate
    _settings.ensure_dirs()


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings(**load_overrides())
        _settings.ensure_dirs()
    return _settings
