"""知识库与总览接口测试：索引、检索、路径防护。"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.main import create_app  # noqa: E402
from app.services import library  # noqa: E402

FAILS: list[str] = []


def check(cond: bool, label: str) -> None:
    print(("  ok  " if cond else "  FAIL") + f"  {label}")
    if not cond:
        FAILS.append(label)


NOTE_TMPL = """---
title: "{title}"
source: "https://www.bilibili.com/video/{bvid}"
uploader: "{uploader}"
duration: "12:30"
created: "{created}"
transcript: "cc"
model: "deepseek/deepseek-chat"
tags: ["缓存", "系统设计"]
---

# {title}

> **UP主** {uploader}

## 一句话总结

讲了缓存失效的三种策略与取舍。

## 核心要点

- TTL 过期实现简单但有延迟

## 章节

### 01 三种失效策略 `[00:00]`

正文内容，包含关键词 {keyword}。

![画面 00:12](assets/frame_00-12_001.jpg)

### 02 性能对比 `[05:00]`

![画面 05:10](assets/frame_05-10_002.jpg)
"""


def make_note(
    root: Path,
    name: str,
    title: str,
    bvid: str,
    keyword: str,
    uploader: str = "某UP主",
    created: str = "2026-10-31 10:00",
) -> None:
    folder = root / name
    (folder / "assets").mkdir(parents=True, exist_ok=True)
    (folder / "notes.md").write_text(
        NOTE_TMPL.format(
            title=title, bvid=bvid, uploader=uploader, keyword=keyword, created=created
        ),
        encoding="utf-8",
    )
    (folder / "assets" / "frame_00-12_001.jpg").write_bytes(b"\xff\xd8\xff\xd9")
    (folder / "assets" / "frame_05-10_002.jpg").write_bytes(b"\xff\xd8\xff\xd9")


def main() -> int:
    settings = get_settings()
    tmp = Path(tempfile.mkdtemp(prefix="bilinotes_lib_"))
    settings.output_dir = tmp / "output"
    settings.output_dir.mkdir(parents=True, exist_ok=True)

    # 用独立 data_dir，保证测试里的 cookies 不存在 —— 否则用户本机已登录，
    # "未登录应返回 401" 的断言会失效
    settings.data_dir = tmp / "data"
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    from app.api import routes_bili as _rb  # noqa: PLC0415
    _rb._invalidate()

    # 故意让两种口径排出相反的顺序：
    # 缓存失效策略生成得晚（created 新），但先落盘（mtime 早）；
    # 分布式一致性生成得早，后落盘 → mtime 更晚。
    make_note(settings.output_dir, "缓存失效策略", "缓存失效策略", "BV1AA0000001", "淘汰算法",
              created="2026-11-05 09:00")
    make_note(settings.output_dir, "分布式一致性", "分布式一致性", "BV1BB0000002", "Raft 协议",
              "另一个UP", created="2026-10-31 10:00")

    print("[1] 索引扫描")
    notes = library.scan(settings)
    check(len(notes) == 2, f"扫到 {len(notes)} 篇")
    first = notes[0]
    check(first["title"] in {"缓存失效策略", "分布式一致性"}, "标题解析正确")
    check(first["chapters"] == 2, f"章节数 {first['chapters']}")
    check(first["images"] == 2, f"图片数 {first['images']}")
    check(first["cover"].startswith("assets/"), f"封面路径 {first['cover']}")
    check("缓存" in first["tags"], f"标签 {first['tags']}")
    check(first["transcript"] == "cc", "转录来源解析")

    print("\n[2] 统计")
    st = library.stats(notes)
    check(st["notes"] == 2 and st["images"] == 4, f"统计 {st}")

    print("\n[3] 排序口径")
    # 默认按生成时间：后生成的在前，跟文件什么时候被改过无关
    check(notes[0]["title"] == "缓存失效策略", f"默认按生成时间（第一篇 {notes[0]['title']}）")
    by_mtime = library.scan(settings, order="mtime")
    check(by_mtime[0]["title"] == "分布式一致性",
          f"选 mtime 时按文件时间（第一篇 {by_mtime[0]['title']}）")
    check(library.scan(settings, order="乱填")[0]["title"] == notes[0]["title"],
          "非法口径回退到生成时间")
    # 批量重写正文只动文件时间，不会打乱默认顺序 —— 这就是不用 mtime 做默认的原因
    content = (settings.output_dir / "缓存失效策略" / "notes.md").read_text(encoding="utf-8")
    (settings.output_dir / "缓存失效策略" / "notes.md").write_text(
        content + "\n<!-- 重写一次 -->\n", encoding="utf-8"
    )
    check(library.scan(settings)[0]["title"] == "缓存失效策略",
          "重写正文后默认顺序不变")
    check(library.scan(settings, order="mtime")[0]["title"] == "缓存失效策略",
          "但按修改时间看，它确实跑到最前了")

    print("\n[4] 检索")
    hits = library.search(settings, "Raft")
    check(len(hits) == 1, f"命中 {len(hits)} 篇")
    check("Raft" in hits[0]["snippet"], "摘要包含关键词")
    check(len(library.search(settings, "不存在的词")) == 0, "无结果正常返回")
    check(len(library.search(settings, "  ")) == 0, "空查询返回空")

    print("\n[5] 读取与路径防护")
    got = library.get_note(settings, "缓存失效策略")
    check(got["title"] == "缓存失效策略", "读取单篇")
    check("一句话总结" in got["markdown"], "markdown 完整")
    try:
        library.get_note(settings, "../../etc")
        check(False, "路径穿越应被拒绝")
    except ValueError:
        check(True, "路径穿越被拒绝")

    print("\n[6] API 层")
    app = create_app()
    with TestClient(app) as client:
        r = client.get("/api/library", params={"refresh": "true"})
        check(r.status_code == 200 and len(r.json()["items"]) == 2, "GET /api/library")
        check(r.json().get("sort") == "created", "接口回报当前排序口径")

        r = client.get("/api/library", params={"sort": "mtime"})
        check(r.json()["items"][0]["title"] == "缓存失效策略",
              "接口支持 sort=mtime（最近改过的那篇在前）")
        r = client.get("/api/library", params={"sort": "bogus"})
        check(r.json()["sort"] == "created", "非法 sort 回退到 created")

        r = client.get("/api/library/search", params={"q": "缓存"})
        body = r.json()
        check(r.status_code == 200 and body["count"] >= 1, f"搜索接口 {body.get('count')} 条")

        r = client.get("/api/overview")
        ov = r.json()
        check(ov["library"]["notes"] == 2, "总览含笔记统计")
        check("tasks" in ov and "recent_notes" in ov, "总览字段完整")

        r = client.get("/api/library/缓存失效策略")
        check(r.status_code == 200, "按 id 读取")

        r = client.get("/api/library/..%2F..%2Fetc")
        check(r.status_code in (403, 404), f"非法 id 被拒绝（{r.status_code}）")

        r = client.get("/api/auth/status")
        check("logged_in" in r.json(), "登录状态接口")

        r = client.get("/api/favorites/folders")
        check(r.status_code == 401, "未登录访问收藏夹返回 401")

        r = client.post("/api/favorites/import", json={"bvids": []})
        check(r.status_code == 422, "空列表被拒绝")
        r = client.post("/api/favorites/import", json={"bvids": [], "episodes": []})
        check(r.status_code == 422, "bvids 与 episodes 都空也被拒绝")
        # 只给选集也要被接受：只校验模型层，避免真去建任务（那会真的下载）
        from app.api.routes_bili import ImportIn  # noqa: PLC0415
        picked = ImportIn(episodes=[{"bvid": "BV1xx411c7mD", "page": 3}])
        check(len(picked.episodes) == 1 and picked.episodes[0].page == 3,
              "只给选集（bvid + 分P）也能通过校验")

        print("\n[7] 按数量导入")
        from app.api.routes_bili import _library_done  # noqa: PLC0415
        done = {(bv, p) for bv, p in _library_done()}
        check(("BV1AA0000001", 1) in done and ("BV1BB0000002", 1) in done,
              f"已处理集合来自知识库（{len(done)} 条）")

        r = client.post("/api/favorites/import-count", json={"media_id": 1, "limit": 0})
        check(r.status_code == 422, "limit 越界被拒绝")

        r = client.post("/api/favorites/import-count", json={"media_id": 1, "limit": 101})
        check(r.status_code == 422, "limit 超过 100 被拒绝")

        r = client.post("/api/favorites/import-count", json={"media_id": 1, "limit": 5})
        check(r.status_code == 401, "未登录不能按量导入")

        print("\n[8] 图片代理")
        r = client.get("/api/proxy/image", params={"url": "https://example.com/a.jpg"})
        check(r.status_code == 403, "非 B站图床被拒绝（防 SSRF）")

        r = client.get("/api/proxy/image", params={"url": "file:///etc/passwd"})
        check(r.status_code == 400, "非 http 协议被拒绝")

        r = client.get("/api/proxy/image", params={"url": "https://i0.hdslb.com/a.jpg"})
        check(r.status_code in (200, 502), f"B站图床放行（{r.status_code}）")

    print("\n" + "=" * 40)
    if FAILS:
        print(f"失败 {len(FAILS)} 项：{FAILS}")
        return 1
    print("知识库与总览接口全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
