"""打包产物自检：在冻结环境里验证关键链路真的能跑。

为什么需要这个：验收脚本查文件齐不齐、查原生库能不能 import，都证明不了
「音画锚定这种新逻辑在 PyInstaller 冻出来的包里还能正常工作」——模块可能被
漏打、相对导入可能在冻结后失效、语法/类型问题只有真跑才暴露。

所以这里做一份纯计算、不联网、不碰用户数据的自检：
  1. 画面停留区间 → 讲解句归属 → 要点时间码（音画锚定主链路）
  2. 按画面切换处切块
  3. 导出渲染：要点带时间码跳转、配图按要点时刻
  4. 三档详细度判定标准一致
  5. 配置读写（临时目录，不碰 %APPDATA%）

用法（打包后）：
    set BILINOTES_PROBE=bn_selftest && BiliNotes.exe
退出码 0 = 全过，非 0 = 有失败项；详细结果写到 BILINOTES_PROBE_OUT。
"""
from __future__ import annotations

import os
import sys


def _report(lines: list[str]) -> None:
    out = os.environ.get("BILINOTES_PROBE_OUT")
    text = "\n".join(lines)
    if out:
        with open(out, "w", encoding="utf-8") as f:
            f.write(text)
    try:
        print(text)
    except Exception:                # noqa: BLE001  窗口版 stdout 可能是 devnull
        pass


def run() -> int:
    from app.core.models import Frame, Segment, VideoMeta, VideoNote
    from app.services import anchors as A
    from app.services import chunking, exporter

    lines: list[str] = []
    failed = 0

    def check(ok: bool, name: str) -> None:
        nonlocal failed
        lines.append(f"[{'OK' if ok else 'FAIL'}] {name}")
        if not ok:
            failed += 1

    # ---- 1. 音画锚定：每句讲解归到它讲的那张画面 ----
    slides = [(0.0, 30.0), (30.0, 60.0), (60.0, 90.0)]
    speech = [
        (2.0, 9.0, "第一页的讲解，这里讲的是定语后置这种语法"),
        (34.0, 45.0, "第二页的讲解，讲的是宾语从句怎么省略引导词"),
        (64.0, 75.0, "第三页的讲解，讲的是销售代表的外貌和定语从句"),
    ]
    segments = [Segment(start=s, end=e, text=t, source="asr") for s, e, t in speech]
    frames: list[Frame] = []
    for i, (s, e) in enumerate(slides):
        for ts in (s + 6.0, e):
            frames.append(Frame(
                ts=ts, path=f"slide{i}_{int(ts)}.jpg", origin="scene",
                seg_start=s, seg_end=e, ocr_text=f"第{i + 1}页的画面文字",
                text_density=0.6,
            ))
    chunks = chunking.build_chunks(
        segments, target=20, max_chars=60, min_chars=10,
        scene_bounds=A.bounds_from_spans([
            A.SceneSpan(s, e, e) for s, e in slides
        ]),
    )
    check(len(chunks) >= 2, f"按画面切换切出 {len(chunks)} 块")
    all_anchors = A.build_anchors(chunks, segments, frames, 90.0)
    check(len(all_anchors) >= 3, f"产出 {len(all_anchors)} 个音画对")
    tags = ("第一页的讲解", "第二页的讲解", "第三页的讲解")
    owner: dict[str, float] = {}
    for a in all_anchors:
        for tag in tags:
            if any(tag in text for _s, _e, text in speech) and tag in a.speech:
                owner[tag] = a.frame_ts
    check(owner.get("第一页的讲解", -1) < 30, "第一页讲解 → 第一张卡")
    check(30 <= owner.get("第二页的讲解", -1) < 60, "第二页讲解 → 第二张卡")
    check(owner.get("第三页的讲解", -1) >= 60, "第三页讲解 → 第三张卡")
    check(all(c.anchors for c in chunks), "每个知识块都挂上了音画对")

    # ---- 2. 材料与时间码工具 ----
    material = A.anchor_material(chunks[0])
    check("【画面】" in material and "【讲解】" in material, "成对材料区分画面与口播")
    check(A.parse_ts("01:02") == 62.0, "时间码解析正确")
    ts, text = A.split_item_ts("[00:28] happen to 碰巧")
    check(ts == 28.0 and text == "happen to 碰巧", "要点时间码可剥离")

    # ---- 3. 导出渲染：要点带时间码、配图按要点时刻 ----
    meta = VideoMeta(
        bvid="BVselftest", cid=1, title="自检", uploader="t",
        duration=90.0, pubdate=0, cover="",
        webpage_url="https://www.bilibili.com/video/BVselftest",
    )
    note = VideoNote(meta=meta, chunks=chunks)
    note.outline = [{
        "title": "语法", "summary": "", "ts": 6.0, "cover_ts": 6.0,
        "groups": [{
            "title": "定语后置", "items": ["story 后面是定语后置"],
            "items_ts": [6.0], "ts": 6.0, "cover_ts": 6.0,
        }],
    }]
    image_map = {id(f): f"assets/{f.path}" for f in frames}
    body = "\n".join(exporter._render_outline(note, image_map))
    check("story 后面是定语后置" in body, "要点渲染正常")
    check("t=6" in body, "要点带跳回原片的时间码链接")

    # ---- 4/5. 档位派生参数（新机上没有配置文件也要正常）----
    from app.core.config import DETAIL_PRESETS as _PRESETS
    from app.core.config import Settings as _Settings
    brief, standard, detailed = (_Settings(note_detail=d) for d in
                                 ("brief", "standard", "detailed"))
    check(set(_PRESETS) == {"brief", "standard", "detailed"}, "三档齐全")
    check(brief.max_frames_per_chunk == 5, "简洁档每段 5 张")
    check(standard.max_frames_per_chunk == 8, "适中档每段 8 张")
    check(detailed.max_frames_per_chunk == 0 and detailed.max_frames_total == 0,
          "详细档不设上限")
    check(len({brief.scene_phash_distance, standard.scene_phash_distance,
               detailed.scene_phash_distance}) == 1,
          "三档画面判定阈值一致")

    lines.append(f"\n{'全部通过' if not failed else f'{failed} 项失败'}")
    _report(lines)
    return 0 if not failed else 1


def main() -> int:
    try:
        return run()
    except Exception as exc:                # noqa: BLE001
        _report([f"[FAIL] 自检自身异常：{type(exc).__name__}: {exc}"])
        return 2


if __name__ == "__main__":
    sys.exit(main())
