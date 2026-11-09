"""真实链路探测：找一个短视频，验证元数据与字幕获取。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.services import downloader, subtitle  # noqa: E402

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
    ),
    "Referer": "https://www.bilibili.com/",
}


def pick_candidates(limit: int = 8) -> list[dict]:
    resp = httpx.get(
        "https://api.bilibili.com/x/web-interface/ranking/v2",
        params={"rid": 0, "type": "all"},
        headers=HEADERS,
        timeout=20,
        follow_redirects=True,
    )
    resp.raise_for_status()
    items = resp.json()["data"]["list"]
    out = []
    for item in items:
        dur = int(item.get("duration") or 0)
        if 120 <= dur <= 900:
            out.append({"bvid": item["bvid"], "title": item["title"][:40], "duration": dur})
        if len(out) >= limit:
            break
    return out


def main() -> int:
    settings = get_settings()
    print(f"cookies: {'有' if settings.cookies_file.is_file() else '无（AI 字幕可能拿不到）'}")

    try:
        candidates = pick_candidates()
    except Exception as exc:
        print(f"排行榜拉取失败：{exc}")
        return 2

    if not candidates:
        print("没有 2-15 分钟的视频，换一个来源")
        return 2

    print(f"候选 {len(candidates)} 个\n")
    for cand in candidates[:3]:
        print(f"→ {cand['bvid']}  {cand['duration']}s  {cand['title']}")
        try:
            meta, info = downloader.probe(settings, cand["bvid"])
        except Exception as exc:
            print(f"   探测失败：{str(exc)[:120]}\n")
            continue

        subs = info.get("subtitles") or {}
        print(f"   标题：{meta.title[:50]}")
        print(f"   UP主：{meta.uploader} · 时长 {meta.duration:.0f}s · cid {meta.cid}")
        print(f"   字幕轨：{list(subs.keys()) or '无'}")
        track = subtitle.pick_subtitle_track(info)
        if track:
            lang, url = track
            print(f"   选中：{lang} -> {url[:70]}…")
            try:
                segs = subtitle.fetch_subtitle(url)
                merged = subtitle.normalize(segs)
                print(f"   解析：原始 {len(segs)} 句 → 合并 {len(merged)} 句")
                print(f"   样例：{merged[0].text[:60] if merged else '(空)'}")
            except Exception as exc:
                print(f"   字幕下载失败：{str(exc)[:120]}")
        print(f"   ▶ 可用于端到端测试：{meta.webpage_url}")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
