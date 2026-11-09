"""知识库：同一合集折成一张卡，点进去才是全部分集。

跑法：python tests/smoke_series_group.py —— 不联网、不碰真实笔记目录。
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.main import create_app  # noqa: E402
from app.services import library, series as series_svc  # noqa: E402

FAILS: list[str] = []


def check(cond: bool, label: str, extra: str = "") -> None:
    print(("  ok  " if cond else "  FAIL") + f"  {label}" + (f"  {extra}" if extra else ""))
    if not cond:
        FAILS.append(label)


def make_note(
    root: Path,
    name: str,
    title: str,
    source: str,
    *,
    meta: dict | None = None,
    created: str = "2026-11-09 10:00",
    collection: str = "默认收藏夹",
) -> None:
    folder = root / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "notes.md").write_text(
        "---\n"
        f'title: "{title}"\n'
        f'source: "{source}"\n'
        'uploader: "某UP主"\n'
        f'collection: "{collection}"\n'
        f'created: "{created}"\n'
        '---\n\n# ' + title + '\n\n### 01 小节\n\n正文。\n',
        encoding="utf-8",
    )
    if meta is not None:
        (folder / "meta.json").write_text(
            json.dumps(meta, ensure_ascii=False), encoding="utf-8"
        )


def fresh_root() -> Path:
    root = Path(tempfile.mkdtemp(prefix="bn_series_")) / "output"
    root.mkdir(parents=True)
    return root


def settings_for(root: Path):
    s = get_settings()
    s.output_dir = root
    s.data_dir = root.parent / "data"
    s.data_dir.mkdir(parents=True, exist_ok=True)
    return s


def test_pages() -> None:
    """多 P 投稿：同一个 BV 的 p12/p13/p14 折成一个合集，散篇不动。"""
    print("\n[1] 多 P 分集按 BV 折叠")
    root = fresh_root()
    for p in (12, 13, 14):
        make_note(
            root, f"csapp-p{p}",
            f"CMU 15-213 CSAPP 深入理解计算机系统 p{p} 第{p}课 存储器层次结构",
            f"https://www.bilibili.com/video/BV1Vh411r7ef?p={p}",
            created=f"2026-11-0{p - 10} 10:00",
        )
    make_note(root, "单篇", "一口气看完AI发展史｜时间开始加速了",
              "https://www.bilibili.com/video/BV1dgnG62E33")
    notes = library.scan(settings_for(root))
    groups = library.series_groups(notes)

    check(len(groups) == 1, f"只有 1 个合集（实际 {len(groups)}）")
    g = groups[0] if groups else {}
    check(g.get("count") == 3, f"合集含 3 集（{g.get('count')}）")
    check(g.get("kind") == "pages", "识别为多 P 投稿")
    check("CMU 15-213" in g.get("title", ""), f"合集名取公共前缀：{g.get('title')}")
    check(g.get("first_order") == 12 and g.get("last_order") == 14,
          f"集数范围 12~14（{g.get('first_order')}~{g.get('last_order')}）")
    parts = [n for n in notes if n["series_key"] == g.get("key")]
    check([n["series_order"] for n in sorted(parts, key=lambda x: x["series_order"])]
          == [12, 13, 14], "每集都带正确集号，进去能按顺序排")
    alone = [n for n in notes if n["id"] == "单篇"]
    check(not alone[0]["series_key"], "无关单篇不进合集")


def test_explicit_season() -> None:
    """UP 主合集（不同 BV）：导入时记下的 series_id 优先。"""
    print("\n[2] UP 主合集按 series_id 折叠")
    root = fresh_root()
    for i in range(1, 4):
        make_note(
            root, f"ep{i}", f"旅行合集 第{i}集 分集标题{i}",
            f"https://www.bilibili.com/video/BV1AAA000000{i}",
            meta={"bvid": f"BV1AAA000000{i}", "page_index": 1,
                  "series_id": "season:9988", "series_title": "环游中国 · 第一季",
                  "series_order": i},
        )
    notes = library.scan(settings_for(root))
    groups = library.series_groups(notes)
    check(len(groups) == 1, f"1 个合集（{len(groups)}）")
    g = groups[0] if groups else {}
    check(g.get("kind") == "season", "识别为 UP 主合集")
    check(g.get("title") == "环游中国 · 第一季", f"用导入时记下的标题：{g.get('title')}")
    check(g.get("count") == 3, "3 集都在")
    check(all(n["series_key"] == "id:season:9988" for n in notes), "三集共享同一 key")


def test_title_fallback() -> None:
    """老笔记没有 meta.json、URL 也没有 p= 时，靠标题集数兜底。"""
    print("\n[3] 标题集数兜底 / 不乱并")
    root = fresh_root()
    make_note(root, "a", "算法设计与分析期末速成 第1期 绪论",
              "https://www.bilibili.com/video/BV1CCC0000001")
    make_note(root, "b", "算法设计与分析期末速成 第2期 动态规划",
              "https://www.bilibili.com/video/BV1CCC0000002")
    make_note(root, "c", "完全不相关的另外一个视频",
              "https://www.bilibili.com/video/BV1DDD0000003")
    notes = library.scan(settings_for(root))
    groups = library.series_groups(notes)
    check(len(groups) == 1, f"标题带集数的两篇合成一个（{len(groups)}）")
    check(groups[0]["count"] == 2 if groups else False, "只并了这两篇")
    check(not next(n for n in notes if n["id"] == "c")["series_key"], "无关视频不进组")

    # 同一个 BV 重复处理两遍（都是 p=1）不该被当成合集
    root2 = fresh_root()
    make_note(root2, "x", "同一个视频第一次", "https://www.bilibili.com/video/BV1EEE0000004")
    make_note(root2, "y", "同一个视频第二次", "https://www.bilibili.com/video/BV1EEE0000004")
    check(not library.series_groups(library.scan(settings_for(root2))),
          "同 BV 重复笔记不误判成多集合集")


def test_api() -> None:
    """接口：series 只算当前收藏夹，且 items 带上 series_key 供第三级筛选。"""
    print("\n[4] 接口返回合集卡片")
    root = fresh_root()
    for p in (1, 2):
        make_note(root, f"p{p}", f"计算机网络课程 p{p} 第{p}章 物理层",
                  f"https://www.bilibili.com/video/BV1FFF0000009"
                  + (f"?p={p}" if p > 1 else ""),
                  collection="编程" if p == 1 else "默认收藏夹")
    s = settings_for(root)
    with TestClient(create_app()) as client:
        d = client.get("/api/library", params={"collection": "默认收藏夹"}).json()
        check(len(d.get("items", [])) == 1, "收藏夹过滤仍生效")
        check(not d.get("series"), "不同收藏夹的分集不合并")
        d2 = client.get("/api/library").json()
        check(not d2.get("series"), "第一级不算合集（合集只在收藏夹内折叠）")
        cols = {c["name"]: c for c in d2.get("collections", [])}
        check(all("series_count" in c for c in cols.values()), "收藏夹卡带合集数")
    _ = s


def test_import_identity() -> None:
    """展开合集导入时，每集都带系列身份；单集不带，且不继承上一集。"""
    print("\n[5] 展开导入写入系列身份")
    from app.api import routes_bili as rb  # noqa: PLC0415

    fake = series_svc.Series(
        "season", "测试合集",
        [series_svc.SeriesItem(f"BV1AAA{i:07d}", 1, f"第{i}集", 600) for i in range(1, 4)],
        sid="12345",
    )
    single = series_svc.Series("", "", [])

    def fake_resolve(_settings, bvid):
        return fake if bvid == "BV1SEASON001" else single

    settings = get_settings()
    with patch.object(rb.series_svc, "resolve", fake_resolve):
        items, _ = rb._expand_items(
            settings, [{"bvid": "BV1SEASON001"}, {"bvid": "BV1SINGLE001"}], limit=50,
        )
    check(len(items) == 4, f"3 集 + 1 单集（{len(items)}）")
    season_items = [x for x in items if x.get("series_id")]
    check(len(season_items) == 3, f"3 集带系列身份（{len(season_items)}）")
    check(season_items[0]["series_id"] == "12345", "用 B站合集 id，跨导入稳定")
    check([x["series_order"] for x in season_items] == [1, 2, 3], "集数顺序 1/2/3")
    check(not any(x.get("series_id") for x in items if x["bvid"] == "BV1SINGLE001"),
          "单集不带系列身份")

    class FakeManager:
        def __init__(self): self.calls = []

        def create(self, url, options):
            self.calls.append((url, dict(options)))
            return type("T", (), {"id": "x", "status": "queued"})()

    mgr = FakeManager()
    rb._create_tasks(mgr, items, {"collection": "编程"}, skip_done=False)
    check(len([c for c in mgr.calls if c[1].get("series_id")]) == 3,
          "只有合集的 3 集任务带系列身份")
    check("series_id" not in mgr.calls[-1][1], "单集任务不继承上一集的身份")


def main() -> int:
    test_pages()
    test_explicit_season()
    test_title_fallback()
    test_api()
    test_import_identity()
    print("\n" + "=" * 40)
    if FAILS:
        print(f"失败 {len(FAILS)} 项：{FAILS}")
        return 1
    print("合集折叠全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
