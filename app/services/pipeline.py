"""流水线编排：把十个环节串起来，每一步的产物都落盘。

设计取向：视觉环节（抽帧/OCR）失败不阻断出笔记——宁可少几张图，
也不能让用户等了十分钟最后什么都拿不到。
"""
from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ..core.config import Settings
from ..core.errors import AppError, CancelledError
from ..core.llm import PROVIDERS, LLMClient
from ..core.models import Chunk, Frame, SceneSpan, Segment, VideoNote
from ..core.secretstore import SecretStore
from . import anchors as anchors_mod
from . import bili_auth, chunking, concept_review, downloader, exporter, itinerary, lecture, mindmap, ocr, subtitle, summarize, text_check, transcribe, wordbook, words
from . import frames as frames_mod

log = logging.getLogger(__name__)

EmitFn = Callable[[str, float, str], None]


@dataclass
class PipelineOptions:
    do_frames: bool = True
    do_ocr: bool = True
    do_summary: bool = True
    do_lecture: bool = False   # 额外生成"知识点讲义"（单独 lecture.md），要额外一次大模型调用
    max_height: int = 480
    provider: str | None = None
    collection: str = ""  # 来源收藏夹名，导出时写进笔记，知识库据此归类
    # 同一合集的身份（批量展开导入时由后端给出），知识库据此把多集折叠成一张卡
    series_id: str = ""
    series_title: str = ""
    series_order: int = 0


@dataclass
class PipelineResult:
    task_id: str
    md_path: Path
    note: VideoNote
    elapsed: float
    warnings: list[str] = field(default_factory=list)
    text_fixes: list[dict] = field(default_factory=list)


class Pipeline:
    def __init__(
        self,
        settings: Settings,
        store: SecretStore | None = None,
        emit: EmitFn | None = None,
        cancel: Callable[[], bool] | None = None,
    ) -> None:
        self.settings = settings
        self.store = store or SecretStore(settings.data_dir / "secrets.dat")
        self._emit_fn = emit
        self._cancel = cancel
        self.warnings: list[str] = []
        self.text_fixes: list[dict] = []
        # 并行阶段的两个子进度，按权重合成总进度
        self._sub: dict[str, float] = {"text": 0.0, "vision": 0.0}
        self._weights: dict[str, float] = {"text": 45.0, "vision": 30.0}
        self._sub_lock = threading.Lock()
        # 全片画面停留区间，抽帧阶段填好，音画锚定/切块都要用
        self._scene_spans: list[SceneSpan] = []

    def _emit(self, stage: str, pct: float, msg: str) -> None:
        log.info("[%s %.0f%%] %s", stage, pct, msg)
        if self._emit_fn:
            self._emit_fn(stage, round(pct, 1), msg)

    def _emit_sub(self, group: str, pct: float, msg: str) -> None:
        """并行阶段用：把某个子任务的 0-100 进度按权重折算成总进度。"""
        with self._sub_lock:
            self._sub[group] = max(0.0, min(100.0, pct))
            weighted = sum(self._sub[k] * self._weights.get(k, 0.0) for k in self._sub) / 100.0
            total = 5.0 + weighted
        # 视觉子任务对外仍归入 frames 阶段，前端只需认识 text / frames
        self._emit("frames" if group == "vision" else group, total, msg)

    def _check_cancel(self) -> None:
        if self._cancel and self._cancel():
            raise CancelledError("任务已取消")

    def run(self, url: str, task_id: str | None = None, options: PipelineOptions | None = None) -> PipelineResult:
        opts = options or PipelineOptions()
        task_id = task_id or uuid.uuid4().hex[:12]
        work = self.settings.cache_dir / task_id
        work.mkdir(parents=True, exist_ok=True)
        started = time.time()
        self._scene_spans = []

        # 1. 元数据
        self._emit("probe", 2, "解析视频信息…")
        meta, info = downloader.probe(self.settings, url)
        # 来源收藏夹：从收藏夹批量导入时带上，知识库按它分组
        if opts.collection:
            meta.collection = opts.collection
        # 展开合集/多P时把系列身份一并写进 meta.json，导出后知识库靠它折叠
        if opts.series_id:
            meta.series_id = opts.series_id
            meta.series_title = opts.series_title
            meta.series_order = opts.series_order
        (work / "meta.json").write_text(
            json.dumps(meta.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        self._emit("probe", 5, f"《{meta.title}》{meta.duration / 60:.0f} 分钟")

        # 2/3. 并行：文本获取（字幕或转写）∥ 视觉素材准备（下载+抽帧+OCR）
        #      两者数据源不同、互不依赖，串行跑等于白等
        segments, source, sampled, video = self._run_parallel(meta, info, work, opts)
        (work / "transcript.json").write_text(
            json.dumps([s.to_dict() for s in segments], ensure_ascii=False), encoding="utf-8"
        )

        # 3.5 文本校验第一轮：以标题/简介为准做术语锚定（此时画面 OCR 已就绪但先做轻量的）
        segments = self._verify_text(segments, meta, ocr_corpus="")

        # 4. 分块（帧打分需要块文本做语义相关度）
        self._check_cancel()
        chunks = chunking.build_chunks(
            segments,
            target=self.settings.chunk_target_chars,
            max_chars=self.settings.chunk_max_chars,
            scene_bounds=anchors_mod.bounds_from_spans(self._scene_spans)
            or anchors_mod.scene_bounds(sampled),
            # 一块里讲过的画面页数不超过该段的图片上限：保证每页都配得到图
            max_scene_spans=self.settings.max_frames_per_chunk,
        )
        if not chunks and sampled:
            # 没有语音（无音轨 / 只有配乐 / 人声太轻）时转写会是 0 句。
            # 这时块为空会让 attach_frames 整批丢弃关键帧，笔记只剩标题。
            # 改成用画面当骨架：画面上的文字就是这一段的"讲解"。
            chunks = chunking.chunks_from_frames(
                sampled,
                meta.duration,
                target_chars=self.settings.chunk_target_chars,
            )
            self.warnings.append(
                "本次音频没有识别到人声（视频可能没有音轨或只有配乐），"
                f"笔记已改用画面内容整理（{len(chunks)} 段）。"
            )
            self._emit("chunk", 81,
                       f"没有语音内容，改用画面切分为 {len(chunks)} 个知识块")
        else:
            self._emit("chunk", 81, f"切分为 {len(chunks)} 个知识块")

        # 4.5 视觉定稿：补讲解峰值帧 → 打分 → 高清重取
        selected = self._finalize_visual(sampled, video, meta, info, work, segments, chunks, opts)

        # 4.5 文本校验第二轮：音画互校（把 PPT 上的正确写法当锚点）
        ocr_corpus = "\n".join(f.ocr_text for f in selected if f.ocr_text)
        if ocr_corpus:
            before = len(segments)
            segments = self._verify_text(segments, meta, ocr_corpus=ocr_corpus)
            text_check.rebuild_chunk_text(chunks, segments)
            self._emit("summary", 84, f"音画互校完成（{before} 句）")

        # 5. 挂帧
        chunking.attach_frames(
            chunks, selected,
            per_chunk=self.settings.max_frames_per_chunk,
            total_cap=self.settings.max_frames_total,
        )
        (work / "chunks.json").write_text(
            json.dumps([_chunk_dict(c) for c in chunks], ensure_ascii=False, indent=1),
            encoding="utf-8",
        )

        # 6. 总结 + 转录润色 都可能需要模型，统一在这里建一次客户端
        client = None
        need_llm = (
            opts.do_summary
            or self.settings.polish_punctuation
            or self.settings.bilingual_transcript
        )
        if need_llm:
            client = LLMClient.from_settings(self.settings, self.store, opts.provider)
            if client is None:
                if opts.do_summary:
                    self.warnings.append("未配置可用的模型 Key，摘要已降级为原文截取")
                    head_text = "".join(c.text for c in chunks[:8])
                    if summarize.is_foreign(head_text):
                        self.warnings.append(
                            "素材是外语但未配置模型，笔记保留原文；配置 Key 后会自动译为中文"
                        )
                if self.settings.polish_punctuation or self.settings.bilingual_transcript:
                    self.warnings.append(
                        "未配置模型，转录润色（标点 / 译中）已跳过，转录保留原始文本"
                    )
                self._emit("summary", 72, "未配置模型，使用原文摘要")

        self._check_cancel()
        # 6.1 画面要点：先于总结提炼——逐块总结和结构化大纲都要吃这份材料，
        #     PPT 的题目/分点才能长进笔记结构里
        self._screen_notes(chunks, client)
        # 画面要点比 OCR 碎片干净，回填到音画锚点里再喂给总结/大纲
        anchors_mod.refresh_screens(chunks)

        self._check_cancel()
        self._emit("summary", 87, "逐块总结中…")
        summarize.summarize_chunks(
            client, chunks, meta,
            concurrency=self.settings.llm_max_concurrency,
            on_progress=lambda p, m: self._emit("summary", 87 + p * 0.06, m),
            cancel=self._cancel,
            note_detail=self.settings.note_detail,
        )

        self._check_cancel()
        self._emit("chapter", 93, "归并章节…")
        chapters = summarize.build_chapters(client, chunks, meta)

        self._emit("chapter", 94, "重组为结构化笔记…")
        outline, takeaways, problems = summarize.build_outline(
            client, meta, chunks, note_detail=self.settings.note_detail,
        )
        if problems:
            self._emit("chapter", 95, f"抽出 {len(problems)} 道例题的解题关键")

        self._emit("chapter", 96, "生成全局总结…")
        one_liner, highlights, glossary = summarize.finalize(
            client, meta, chapters, chunks, speechless=not segments,
        )
        # 概念过闸：模型自由生成的词条先审一遍，别把景点/鸡汤/派生说法写进索引
        glossary = self._review_concepts(glossary)

        # 6.5 转录润色：智能标点 + 外语译中双语（只改"完整转录"区，不动已生成的笔记）
        self._check_cancel()
        segments = self._polish_transcript(segments, client)

        # 6.7 行程路线：旅行类视频专有，抽「地点顺序 + 交通 + 花费」
        it_data: dict = {}
        if client is not None and self.settings.travel_itinerary \
                and itinerary.looks_like_travel(meta, chunks):
            self._emit("itinerary", 96, "识别为旅行内容，正在整理行程路线…")
            it_data = itinerary.build_itinerary(client, meta, chunks, segments=segments) or {}
            if it_data:
                self._emit("itinerary", 96, f"行程单完成：{len(it_data.get('stops', []))} 个地点")

        # 7. 导图 + 导出
        self._emit("export", 97, "生成思维导图与笔记…")
        note = VideoNote(
            meta=meta,
            segments=segments,
            chunks=chunks,
            chapters=chapters,
            outline=outline,
            takeaways=takeaways,
            problems=problems,
            one_liner=one_liner,
            highlights=highlights,
            glossary=glossary,
            transcript_source=source,
            generated_at=time.strftime("%Y-%m-%d %H:%M"),
            text_fixes=self.text_fixes,
            itinerary=it_data,
        )
        tree = mindmap.build_tree(meta, chapters)
        note.mindmap = tree

        label = f"{self.settings.llm_provider}/{client.model}" if client else ""
        md_path = exporter.export(note, tree, self.settings.output_dir, segments, label)

        # 7.3 英语生词：外语笔记 + 有词库才标；没有词库就完全不动笔记
        self._mark_words(md_path)

        # 7.4 概念索引整理：新词条的上级对齐到已有概念，碎片并回主体；
        #     配了模型就再让 AI 认一遍规则判不出的（"XX 顺序"这类换角度命名的）。
        #     失败不影响笔记——索引是附加视图，不能因为它阻断交付。
        try:
            tidy = concept_review.tidy(self.settings, client=client)
            ai = tidy.get("ai") or {}
            if tidy.get("merged") or tidy.get("reparented") or tidy.get("hidden") or ai.get("merged"):
                log.info(
                    "概念整理：合并 %d 条，重挂层级 %d 条，隐藏空泛词 %d 个，AI 并回 %d 条",
                    len(tidy.get("merged") or []), tidy.get("reparented", 0),
                    len(tidy.get("hidden") or []), len(ai.get("merged") or []),
                )
        except Exception as exc:
            log.warning("概念整理失败（不影响笔记）：%s", exc)

        # 7.5 知识点讲义：在笔记旁边多写一份 lecture.md（阅读器里以标签页呈现）
        if opts.do_lecture and client is not None:
            self._check_cancel()
            self._emit("lecture", 98, "写知识点讲义…")
            try:
                body = lecture.build(client, md_path.read_text(encoding="utf-8"), meta.title)
                if body:
                    lecture.save(md_path.parent, body, meta.title)
                    self._emit("lecture", 99, "讲义完成")
            except AppError:
                raise
            except Exception as exc:      # 讲义失败不该让整条笔记作废
                log.warning("生成讲义失败：%s", exc, exc_info=True)
                self.warnings.append(f"讲义生成失败：{exc}")

        elapsed = time.time() - started
        self._emit("done", 100, f"完成，用时 {elapsed:.0f}s")
        return PipelineResult(
            task_id=task_id,
            md_path=md_path,
            note=note,
            elapsed=elapsed,
            warnings=self.warnings,
            text_fixes=self.text_fixes,
        )

    # ---- 文本素材：字幕直接拿，没有就只下音频（都很快）----
    def _prepare_text(
        self, meta, info, work: Path
    ) -> tuple[list[Segment], str, Path | None]:
        """按"零成本 → 高成本"的顺序找文本源。

        1. yt-dlp 的 CC 字幕（几乎瞬时）
        2. B站 AI 字幕（登录后走 player 接口 —— yt-dlp 完全拿不到这一路）
        3. 都没有才下载音频、加载本地模型转写

        第 2 步是决定性的：绝大多数视频都有 AI 字幕，拿到它就等于
        跳过 480MB 模型下载和十几分钟 CPU 推理。
        """
        track = subtitle.pick_subtitle_track(info)
        if track:
            lang, url = track
            self._emit_sub("text", 10, f"发现字幕轨（{lang}），直接下载")
            try:
                raw = subtitle.fetch_subtitle(url)
                if raw:
                    source = "ai" if "ai" in lang.lower() else "cc"
                    for seg in raw:
                        seg.source = source
                    merged = subtitle.normalize(raw)
                    self._emit_sub("text", 100, f"字幕 {len(merged)} 句")
                    return merged, source, None
            except AppError as exc:
                self.warnings.append(f"字幕下载失败，改用语音转写：{exc.message}")
                log.warning("字幕不可用：%s", exc)

        # yt-dlp 的 subtitles / automatic_captions 对多数 B站视频都是空的，
        # 但登录态下 player/v2 会给出 ai-zh 直链，这一路不能漏
        cookies = bili_auth.load_cookies(self.settings.cookies_file)
        if not self.settings.use_ai_subtitle:
            log.info("已关闭 B站 AI 字幕（内容不可信），走本地转写")
        elif cookies.get("SESSDATA"):
            self._emit_sub("text", 22, "查询 B站 AI 字幕…")
            try:
                ai_segs, ai_lang = subtitle.fetch_ai_subtitle(
                    meta.bvid, meta.cid, cookies, duration=meta.duration
                )
                if ai_segs:
                    for seg in ai_segs:
                        seg.source = "ai"
                    merged = subtitle.normalize(ai_segs)
                    self._emit_sub("text", 100, f"AI 字幕 {len(merged)} 句（{ai_lang}）")
                    log.info("使用 B站 AI 字幕，跳过语音转写")
                    return merged, "ai", None
            except Exception as exc:
                log.warning("AI 字幕获取失败：%s", exc)
        else:
            log.info("未登录，无法查询 B站 AI 字幕")

        self._emit_sub("text", 8, "下载音频轨…")
        audio = downloader.download_media(
            self.settings, meta.webpage_url, work, kind="audio",
            on_progress=lambda p, m: self._emit_sub("text", 8 + p * 0.22, m),
        )
        self._emit_sub("text", 30, "音频就绪，等待转写")
        return [], "asr", audio

    # ---- 转写：纯 CPU 重活（并行阶段跑）----
    def _transcribe(self, meta, audio: Path) -> list[Segment]:
        est = transcribe.estimate_seconds(meta.duration, self.settings.asr_model)
        self._emit_sub("text", 32, f"转写预计 {est / 60:.0f} 分钟（{self.settings.asr_model} 模型，CPU）")
        segs = transcribe.transcribe(
            self.settings, str(audio),
            on_progress=lambda p, m: self._emit_sub("text", 32 + p * 0.67, m),
            cancel=self._cancel,
        )
        return subtitle.normalize(segs, max_gap=0.8)

    # ---- 转录润色 ----
    def _polish_transcript(self, segments: list[Segment], client) -> list[Segment]:
        """出最终转录前，用 LLM 补标点 + （外语）加中文译文。"""
        from . import transcript_polish  # noqa: PLC0415

        return transcript_polish.polish_segments(
            segments,
            self.settings,
            client,
            on_progress=lambda p, m: self._emit("summary", 90 + p * 0.07, m),
            cancel=self._cancel,
        )

    # ---- 画面要点 ----
    def _screen_notes(self, chunks: list[Chunk], client) -> None:
        """从关键帧里「有选择地」提炼画面文字要点（PPT 题目/分点 + 讲解补全）。

        配了看图模型就真看图，否则用 OCR 文字线索合成。
        产出的要点会回喂总结层，所以必须在总结之前跑。
        """
        from . import frame_caption  # noqa: PLC0415

        vision = LLMClient.from_vision_settings(self.settings, self.store)
        if self.settings.vision_provider and vision is None:
            label = PROVIDERS.get(self.settings.vision_provider, {}).get(
                "label", self.settings.vision_provider
            )
            self.warnings.append(
                f"看图模型（{label}）还不能用：设置 →「看图模型」下方的「看图 API Key」没填。"
                "本次画面要点先按画面文字合成，不影响其他部分。"
            )
        self._emit("summary", 82, "提炼画面要点…")
        frame_caption.caption_frames(
            client,
            chunks,
            self.settings,
            vision_client=vision,
            on_progress=lambda p, m: self._emit("summary", 82 + p * 0.05, m),
            cancel=self._cancel,
        )
        # 视觉判断有没有真的跑起来？跑不起来（模型排队/限流）要告诉用户，
        # 否则会以为「AI 已经逐帧看过画面」，实际上一帧都没判，
        # 后面既不会剔同页重复，也不会认人物空镜。
        # 判据是「有没有帧被真正判过」（judged），不是「有没有取到内容」——
        # 文字模式兜底也能产出内容，但那不算「看过图」。
        if vision is not None:
            total_frames = sum(len(c.frames) for c in chunks)
            judged_frames = sum(1 for c in chunks for f in c.frames if f.judged)
            if total_frames and judged_frames == 0:
                self.warnings.append(
                    "看图模型这次完全没排上队（免费模型限流），画面没被逐帧看过。"
                    "本次配图不做同页去重、也不筛人物空镜；"
                    "换个看图模型或过一会儿再生成会好很多。"
                )
        # 看图模型顺手判了「这帧有没有信息 / 是不是同一页」，据此剔一遍：
        # 纯人物、空镜、同页重复的图不该出现在笔记里。
        # 默认开（设置页可关）；没配视觉模型时所有帧都是「没判过」，这一步不生效。
        if getattr(self.settings, "vision_frame_judge", True):
            stats = frame_caption.prune(chunks)
            if stats["useless"] or stats["duplicate"]:
                self.warnings.append(
                    f"看图模型判掉 {stats['useless']} 张无信息画面、"
                    f"{stats['duplicate']} 张同页重复画面"
                )
            if stats.get("reattached"):
                self.warnings.append(
                    f"{stats['reattached']} 处配图重新指向了保留下来的画面"
                )

    # ---- 概念审核 ----
    def _review_concepts(self, glossary: list[dict[str, str]]) -> list[dict[str, str]]:
        """「关键概念」表落盘前过一遍闸。

        模型会把景点、鸡汤、例题步骤，以及「初等行变换法优势」这类派生说法
        一起塞进来。审核只做可判定的收编，改动都写进 warnings 让人看得见。
        """
        if not glossary:
            return glossary
        try:
            kept, report = concept_review.review_glossary(self.settings, glossary)
        except Exception as exc:          # 审核出问题不能连累笔记
            log.warning("概念审核失败，保留原样：%s", exc)
            return glossary
        dropped = report.get("dropped") or []
        renamed = report.get("renamed") or []
        if dropped:
            head = "、".join(dropped[:5]) + ("…" if len(dropped) > 5 else "")
            self.warnings.append(f"概念审核：剔除 {len(dropped)} 个非知识词条（{head}）")
        if renamed:
            pairs = "、".join(f"{a}→{b}" for a, b in renamed[:3])
            self.warnings.append(f"概念审核：{len(renamed)} 个派生说法已并回已有概念（{pairs}）")
        return kept

    # ---- 英语生词标注 ----
    def _mark_words(self, md_path: Path) -> None:
        """给外语笔记标出生词（只在这两件事同时成立时做）：导入过词库、转录是外语。

        用户明确要求：没套词库就保持原样，一个标记都不加。
        词表与原文锚点都写进 notes.md 本身（导出 Word/PDF 也能看到），
        另外落一份 words.json 给前端做悬停气泡用。
        """
        try:
            index = wordbook.index(self.settings)
            if not index:
                return
            md = md_path.read_text(encoding="utf-8")
            if not words.note_is_foreign(md):
                log.info("生词标注跳过：这篇不是外语内容")
                return
            new_md, stats = words.annotate(md, index)
            if not stats.get("hits"):
                log.info("生词标注：词库里的词一个都没出现")
                return
            md_path.write_text(new_md, encoding="utf-8", newline="\n")
            side = md_path.parent / "words.json"
            side.write_text(
                json.dumps(
                    {
                        "generated_at": time.time(),
                        "books": stats.get("books", []),
                        "words": stats.get("words", []),
                    },
                    ensure_ascii=False,
                    indent=1,
                ),
                encoding="utf-8",
                newline="\n",
            )
            log.info("生词标注：命中 %d 个词（%s）", stats["hits"], "、".join(stats.get("books") or []))
        except Exception as exc:                    # 标注失败绝不能影响笔记交付
            log.warning("生词标注失败（不影响笔记）：%s", exc)

    # ---- 兼容旧名 ----
    def _caption_frames(self, chunks: list[Chunk], client) -> None:
        self._screen_notes(chunks, client)

    # ---- 文本校验 ----
    def _verify_text(self, segments: list[Segment], meta, ocr_corpus: str = "") -> list[Segment]:
        """以标题/简介/画面文字为权威参照，修正字幕里的同音误写。"""
        trusted = text_check.trusted_text_from(
            meta.title, meta.page_title, meta.description, ocr_corpus
        )
        if not trusted.strip():
            return segments

        source = "ocr" if ocr_corpus else "meta"
        report = text_check.verify(segments, trusted, source=source)
        if report.count:
            segments = text_check.apply_fixes(segments, report)
            self.text_fixes.extend(f.to_dict() for f in report.fixes)
            log.info("文本校验（%s）修正 %d 处", source, report.count)

        segments, case_fixes = text_check.unify_ascii_case(segments, trusted)
        if case_fixes:
            self.text_fixes.extend(f.to_dict() for f in case_fixes)
        return segments

    # ---- 并行调度 ----
    def _run_parallel(self, meta, info, work: Path, opts: PipelineOptions):
        """文本获取与视觉素材准备并行跑。

        文本：拿字幕或下载音频转写（CPU 密集）
        视觉：下载 480p + 抽帧 + OCR（I/O 与 CPU 混合）
        两者数据源不同、互不依赖，串行跑等于白等一半时间。
        """
        self._weights = {"text": 45.0, "vision": 30.0 if opts.do_frames else 0.0}
        if not opts.do_frames:
            self._weights["text"] = 75.0

        # 关掉并行时退回串行：弱 CPU 上两个重任务互相抢核，并行反而更慢
        if not self.settings.parallel_pipeline:
            segments, source, audio = self._prepare_text(meta, info, work)
            if audio is not None:
                segments = self._transcribe(meta, audio)
            sampled, video = ([], None)
            if opts.do_frames:
                self._weights["vision"] = 0.0
                self._weights["text"] = 75.0
                self._sub["text"] = 100.0
                sampled, video = self._prepare_visual(meta, info, work, opts)
            return segments, source, sampled, video

        # 素材先串行取（字幕几次请求 / 音频几 MB），避免两个下载互相抢带宽
        segments, source, audio = self._prepare_text(meta, info, work)

        pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="pipe")
        try:
            text_future = pool.submit(self._transcribe, meta, audio) if audio else None
            vision_future = (
                pool.submit(self._prepare_visual, meta, info, work, opts)
                if opts.do_frames else None
            )
            if text_future is not None:
                segments = text_future.result()
            sampled: list[Frame] = []
            video = None
            if vision_future is not None:
                sampled, video = vision_future.result()
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

        log.info("并行阶段完成：文本 %d 句 / 候选帧 %d 张", len(segments), len(sampled))
        return segments, source, sampled, video

    # ---- 视觉阶段一：下载 + 抽帧 + OCR（不依赖文本，可并行）----
    def _prepare_visual(self, meta, info, work: Path, opts: PipelineOptions):
        if not opts.do_frames:
            return [], None
        video: Path | None = None
        try:
            self._emit_sub("vision", 2, "下载画面（480p，仅用于定位）")
            video = downloader.download_media(
                self.settings, meta.webpage_url, work, kind="video",
                max_height=opts.max_height,
                on_progress=lambda p, m: self._emit_sub("vision", 2 + p * 0.28, m),
            )
            self._check_cancel()

            self._emit_sub("vision", 32, "检测画面切换…")
            candidates = frames_mod.generate_candidates(
                self.settings, video, meta.duration, work / "frames",
                spans_out=self._scene_spans,
            )
            # 阶段一只是「撒网」：候选全部留下，交给后面的打分按档位筛。
            # 别在这里按 max_frames_total 截断——三档的 total 都是 0（不限），
            # 老公式 max(50, 0*2)=50 会把 738 张候选硬砍成 50 张，
            # 详细档的「不限」就完全失效了。只有极端超长视频才做上限保护。
            _CANDIDATE_CAP = 800
            if len(candidates) > _CANDIDATE_CAP:
                sampled = _spread(candidates, _CANDIDATE_CAP)
                self._emit_sub("vision", 100,
                               f"候选 {len(candidates)} 张，超长视频抽样 {len(sampled)} 张")
            else:
                sampled = candidates
                self._emit_sub("vision", 100, f"候选 {len(candidates)} 张，全部进入筛选")

            # 注意：OCR 不在这里做。
            # 转写和 OCR 都是纯 CPU 任务，同时跑会互相抢核：
            # 实测同一段 1064s 音频，单独转写 112s（0.11x），
            # 与 OCR 并行时变成 286s（0.3x）—— 并行反而是负优化。
            # 所以阶段一只做「下载（网络）+ 抽帧（IO 为主）」，
            # OCR 留到转写结束后独占 CPU，见 _finalize_visual。
            self._check_cancel()
            return sampled, video
        except CancelledError:
            raise
        except Exception as exc:
            log.warning("画面素材准备失败，将生成纯文字笔记：%s", exc, exc_info=True)
            self.warnings.append(f"画面提取失败：{str(exc)[:100]}")
            return [], video

    # ---- 视觉阶段二：补峰值帧 + 打分 + 高清重取（依赖文本）----
    def _finalize_visual(
        self, sampled: list[Frame], video, meta, info, work: Path,
        segments: list[Segment], chunks: list[Chunk], opts: PipelineOptions,
    ) -> list[Frame]:
        if not sampled or video is None:
            return []
        try:
            pooled = list(sampled)

            # 此时转写已经结束，CPU 空闲，OCR 独占跑得快
            if opts.do_ocr and pooled:
                self._emit("frames", 81, "识别画面文字…")
                texty, skipped = frames_mod.filter_text_frames(
                    pooled,
                    on_progress=lambda p, m: self._emit("frames", 81 + p * 0.01, m),
                )
                if skipped:
                    self._emit("frames", 81, f"跳过 {skipped} 张无文字画面")
                if texty:
                    ocr.ocr_frames(
                        texty, self.settings,
                        on_progress=lambda p, m: self._emit("frames", 82 + p * 0.05, m),
                        max_edge=720,   # 候选帧只用于打分，低分辨率足够
                    )

            # 场景帧和均匀帧在阶段一就抽好了，但"看这个图"这类峰值帧要等文本
            extra = frames_mod.extract_cue_frames(
                self.settings, video, meta.duration, work / "frames", segments
            )
            fresh = [f for f in extra if not any(abs(f.ts - g.ts) < 0.5 for g in pooled)]
            if fresh:
                if opts.do_ocr:
                    ocr.ocr_frames(fresh, self.settings)
                pooled.extend(fresh)
                self._emit("frames", 82, f"补充讲解峰值画面 {len(fresh)} 张")

            # 音画锚定：算出「每句讲解讲的是哪一帧」，让总结/大纲/配图
            # 都拿到带真实时间码的成对材料，而不是两坨没有时间的正文。
            anchors_mod.build_anchors(
                chunks, segments, pooled, meta.duration,
                spans=self._scene_spans,
            )
            must_keep = anchors_mod.must_keep_counts(chunks)
            selected = frames_mod.score_and_select(
                self.settings, pooled, chunks, must_keep=must_keep
            )
            # 同页只留一张时，锚点要改指到入选的那张（语义是「哪一页」）
            anchors_mod.reconcile_anchors(chunks, selected)

            # 低清候选只负责定位，进笔记的这几十张换成原画质
            if selected and self.settings.hd_frames:
                hi_url = downloader.pick_stream_url(info, self.settings.hd_frame_edge)
                if hi_url:
                    self._emit("frames", 82, f"高清重取 {len(selected)} 张关键帧…")
                    upgraded = frames_mod.upgrade_selected(
                        selected, hi_url, work / "frames", self.settings.hd_frame_edge
                    )
                    if upgraded and opts.do_ocr:
                        ocr.ocr_frames(selected, self.settings)
            self._emit("frames", 83, f"关键画面 {len(selected)} 张")
            return selected
        except CancelledError:
            raise
        except Exception as exc:
            log.warning("画面筛选失败：%s", exc, exc_info=True)
            self.warnings.append(f"画面筛选失败：{str(exc)[:100]}")
            return []


def _spread(items: list[Frame], cap: int) -> list[Frame]:
    """候选过多时按时间均匀抽样，避免把 OCR 时间全花在前十分钟。"""
    if len(items) <= cap:
        return items
    items = sorted(items, key=lambda f: f.ts)
    step = len(items) / cap
    return [items[int(i * step)] for i in range(cap)]


def _chunk_dict(chunk: Chunk) -> dict:
    return {
        "index": chunk.index,
        "start": chunk.start,
        "end": chunk.end,
        "title": chunk.title,
        "summary": chunk.summary,
        "bullets": chunk.bullets,
        "keywords": chunk.keywords,
        "ocr_text": chunk.ocr_text,
        "bullet_ts": chunk.bullet_ts,
        "anchors": [a.to_dict() for a in chunk.anchors],
        "frames": [f.to_dict() for f in chunk.frames],
        "chars": chunk.char_count,
    }
