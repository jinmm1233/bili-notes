"""系列展开测试：多P / 合集识别、URL 拼接、展开限流。"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.api.routes_bili import _expand_items  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.services import series as series_svc  # noqa: E402

FAILS: list[str] = []
SKIPPED: list[str] = []


def check(cond: bool, label: str) -> None:
    print(("  ok  " if cond else "  FAIL") + f"  {label}")
    if not cond:
        FAILS.append(label)


def skip(label: str) -> None:
    print(f"  skip  {label}")
    SKIPPED.append(label)


def main() -> int:
    settings = get_settings()

    print("[1] 真实合集识别（需要网络）")
    try:
        found = series_svc.resolve(settings, "BV17sxY6xEWK")
        check(found.kind == "season", f"识别为合集（kind={found.kind}）")
        check(found.total > 10, f"共 {found.total} 集")
        check(bool(found.title), f"合集名《{found.title[:20]}》")
        check(found.items[0].bvid.startswith("BV"), f"首集 {found.items[0].bvid}")
        check(all(i.page == 1 for i in found.items), "合集分集均为独立 BV")
    except Exception as exc:
        skip(f"合集查询失败（网络）：{str(exc)[:80]}")

    print("\n[2] 单视频不应被误判为系列")
    try:
        single = series_svc.resolve(settings, "BV1JYsd6sE4k")
        check(single.kind != "season", f"不是合集（kind={single.kind or '空'}）")
        if single.kind == "":
            check(single.total == 0, "单视频无分集")
    except Exception as exc:
        skip(f"单视频查询失败（网络）：{str(exc)[:80]}")

    print("\n[3] URL 拼接")
    a = series_svc.SeriesItem("BV1xx411c7mD", 1, "P1", 100)
    b = series_svc.SeriesItem("BV1xx411c7mD", 3, "P3", 100)
    check(a.url == "https://www.bilibili.com/video/BV1xx411c7mD", f"P1 不带参数：{a.url}")
    check(b.url.endswith("?p=3"), f"P3 带参数：{b.url}")

    print("\n[4] 展开逻辑（mock）")
    fake_season = series_svc.Series(
        "season", "测试合集",
        [series_svc.SeriesItem(f"BV1AAA{i:07d}", 1, f"第{i}集", 600) for i in range(1, 9)],
    )
    single_series = series_svc.Series("", "", [])

    def fake_resolve(_settings, bvid):
        if bvid == "BV1SEASON001":
            return fake_season
        return single_series

    with patch("app.api.routes_bili.series_svc.resolve", fake_resolve):
        items = [{"bvid": "BV1SEASON001"}, {"bvid": "BV1SINGLE001"}]
        expanded, failed = _expand_items(settings, items, limit=50)
        check(len(expanded) == 9, f"1 个合集(8集)+1 个单集 → {len(expanded)} 项")
        check(sum(1 for e in expanded if e.get("page", 1) == 1) == 9, "页码均为 1")
        check(failed == 0, "无失败")

        limited, _ = _expand_items(settings, items, limit=5)
        check(len(limited) <= 5, f"limit=5 时截断到 {len(limited)} 项")

        # 每个合集最多取 N 集：8 集的合集只取 3 集，单集仍是 1 项
        capped, _ = _expand_items(settings, items, limit=50, series_limit=3)
        check(len(capped) == 4, f"每合集限 3 集 → {len(capped)} 项（3 + 1 个单集）")
        check([e["bvid"] for e in capped][:3] == [f"BV1AAA{i:07d}" for i in (1, 2, 3)],
              "取的是合集里的前 3 集")

        # 跳过已处理：前 3 集做过的话，应该从第 4 集开始取 3 集
        done = {(f"BV1AAA{i:07d}", 1) for i in (1, 2, 3)}
        fresh, _ = _expand_items(settings, items, limit=50, series_limit=3,
                                 done=done, skip_done=True)
        got = [e["bvid"] for e in fresh]
        check(got[:3] == [f"BV1AAA{i:07d}" for i in (4, 5, 6)],
              f"已处理过的集被跳过，取到第 4~6 集：{got[:3]}")

        # 全部做过 → 这个合集不再产生任务
        all_done = {(f"BV1AAA{i:07d}", 1) for i in range(1, 9)}
        none_left, _ = _expand_items(settings, [{"bvid": "BV1SEASON001"}], limit=50,
                                     done=all_done, skip_done=True)
        check(len(none_left) == 0, "合集全做完时不再入队")

        # series_limit=0 表示全部，行为与旧版一致
        zero, _ = _expand_items(settings, items, limit=50, series_limit=0)
        check(len(zero) == 9, f"series_limit=0 → 全部展开（{len(zero)} 项）")

    print("\n[5] 重做已做过的集")
    from app.api.routes_bili import _create_tasks  # noqa: PLC0415

    class FakeTask:
        id = "t1"
        status = "queued"

    class FakeManager:
        def __init__(self) -> None:
            self.created: list[str] = []

        def create(self, url, options):
            self.created.append(url)
            return FakeTask()

    done_item = {"bvid": "BV1DONE00001", "page": 1, "duration": 100}
    with patch("app.api.routes_bili._library_done",
               return_value={("BV1DONE00001", 1)}):
        m1 = FakeManager()
        created, skipped, _ = _create_tasks(m1, [done_item], {}, skip_done=True)
        check(len(created) == 0 and skipped == 1, "默认跳过已做过的集")

        m2 = FakeManager()
        created, skipped, _ = _create_tasks(m2, [done_item], {}, skip_done=False)
        check(len(created) == 1 and skipped == 0, "开关关掉后会真的重新处理")
        check(m2.created and m2.created[0].endswith("BV1DONE00001"),
              f"重建的任务 URL 正确：{m2.created[0]}")

    print("\n[6] 展开失败时回退为原样")
    def boom(_settings, _bvid):
        raise RuntimeError("接口挂了")

    with patch("app.api.routes_bili.series_svc.resolve", boom):
        expanded, failed = _expand_items(settings, [{"bvid": "BV1FAIL00001"}], limit=10)
        check(len(expanded) == 1, "失败时保留原始项")
        check(failed == 1, "记录了失败数")

    print("\n" + "=" * 40)
    if SKIPPED:
        print(f"跳过 {len(SKIPPED)} 项（网络相关）")
    if FAILS:
        print(f"失败 {len(FAILS)} 项：{FAILS}")
        return 1
    print("系列展开全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
