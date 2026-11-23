"""分章节合集的展开回归测试（不联网，mock 掉 view 接口）。

背景：B站合集可以分章节（sections），例如「期末冲刺上大分【大学篇】」下面
按学科分成 数学类 / 计算机类 / …。老逻辑把所有章节摊平成一个大列表，
收藏一集线性代数会把整季 78 集都排进队列。

规则：
1. 当前视频在哪个章节，就只展开那个章节；
2. 单章节合集行为不变（还是整季）；
3. 章节里只有当前这一集时，不产生可展开的系列；
4. 章节 id 参与 series id，避免不同学科在知识库里折成一张卡。
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import get_settings  # noqa: E402
from app.services import series as svc  # noqa: E402

FAILS: list[str] = []


def check(cond: bool, label: str) -> None:
    print(("  ok  " if cond else "  FAIL") + f"  {label}")
    if not cond:
        FAILS.append(label)


def ep(bvid: str, title: str, duration: int = 600) -> dict:
    return {"bvid": bvid, "title": title, "arc": {"duration": duration}}


def season(sections: list[dict], title: str = "期末冲刺上大分【大学篇】", sid: int = 5525752) -> dict:
    return {"code": 0, "data": {"title": "任意投稿", "pages": [{"page": 1}],
                                "ugc_season": {"id": sid, "title": title, "sections": sections}}}


MULTI = season([
    {"id": 6142731, "title": "数学类", "episodes": [ep("BV1math00001", "线代 1"), ep("BV1math00002", "线代 2"),
                                                ep("BV1math00003", "高数 1")]},
    {"id": 6186288, "title": "计算机类", "episodes": [ep(f"BV1cs000000{i}", f"计组 {i}") for i in range(1, 6)]},
])

SINGLE = season([{"id": 5228452, "title": "正片", "episodes": [ep(f"BV1algo0000{i}", f"算法 {i}") for i in range(1, 5)]}],
                title="【计算机基础】", sid=4684254)

LONELY = season([{"id": 7954168, "title": "正片", "episodes": [ep("BV1only00001", "唯一一集")]}],
                title="27新版：计算机组成原理", sid=7209214)


def main() -> int:
    settings = get_settings()

    print("[1] 多章节：只展开当前视频所在章节")
    with patch.object(svc, "_view", return_value=MULTI["data"]):
        found = svc.resolve(settings, "BV1math00002")
    check(found.kind == "season", f"kind={found.kind}")
    check(found.total == 3, f"只取数学类 3 集，实际 {found.total}")
    check(found.title == "数学类", f"标题用章节名：{found.title}")
    check(found.season_title == "期末冲刺上大分【大学篇】", f"父合集：{found.season_title}")
    check(found.section_title == "数学类", f"章节：{found.section_title}")
    check(found.sid == "5525752-6142731", f"系列 id 带章节：{found.sid}")
    check([i.bvid for i in found.items] == ["BV1math00001", "BV1math00002", "BV1math00003"],
          "没有混进计算机类")
    check(len(found.sections) == 2 and found.sections[1]["count"] == 5,
          f"章节列表完整：{[(s['title'], s['count']) for s in found.sections]}")

    print("\n[2] 单章节合集：行为不变（整季）")
    with patch.object(svc, "_view", return_value=SINGLE["data"]):
        found = svc.resolve(settings, "BV1algo00002")
    check(found.total == 4, f"整季 4 集，实际 {found.total}")
    check(found.title == "【计算机基础】", f"标题仍是合集名：{found.title}")
    check(found.section_title == "" and found.sections == [], "不标记章节")

    print("\n[3] 章节只有一集：不当成系列")
    with patch.object(svc, "_view", return_value=LONELY["data"]):
        found = svc.resolve(settings, "BV1only00001")
    check(found.kind == "", f"kind={found.kind or '空'}，total={found.total}")

    print("\n[4] 章节缺 episodes：退回整季，不崩")
    broken = season([{"id": 1, "title": "甲", "episodes": []},
                     {"id": 2, "title": "乙", "episodes": [ep("BV1x0000001", "乙一"), ep("BV1x0000002", "乙二")]}])
    with patch.object(svc, "_view", return_value=broken["data"]):
        found = svc.resolve(settings, "BV1unknown1")
    check(found.kind == "season" and found.total == 2, f"整季兜底 {found.total} 集")

    print("\n[5] 显式指定章节（前端切换章节）")
    with patch.object(svc, "_view", return_value=MULTI["data"]):
        other = svc.resolve(settings, "BV1math00002", section_id="6186288")
    check(other.total == 5, f"切到计算机类 5 集，实际 {other.total}")
    check(other.title == "计算机类", f"标题跟着章节走：{other.title}")
    check(other.sid == "5525752-6186288", f"id 带新章节：{other.sid}")
    check(all("cs" in i.bvid for i in other.items), "列表换成了计算机类")
    check(other.section_title == "计算机类", "section_title 与选择一致")

    print("\n[6] 显式选一个只有一集的章节：仍然展示这一集")
    with patch.object(svc, "_view", return_value=LONELY["data"]):
        one = svc.resolve(settings, "BV1only00001", section_id="7954168")
    check(one.total == 1 and one.items[0].bvid == "BV1only00001", f"单集章节：{one.total} 集")

    print("\n[7] 多P视频同时属于合集：优先分P，不展开整季")
    multi_p = {
        "title": "《线性代数》4小时速成课",
        "pages": [{"page": i, "part": f"第{i}讲", "duration": 1800} for i in range(1, 4)],
        "ugc_season": {"id": 5525752, "title": "期末冲刺上大分【大学篇】", "sections": [
            {"id": 6142731, "title": "数学类", "episodes": [
                ep("BV1math00009", "线代速成"), ep("BV1other0001", "高数速成"),
            ]},
            {"id": 6186288, "title": "计算机类", "episodes": [ep("BV1cs0000001", "计组速成")]},
        ]},
    }
    with patch.object(svc, "_view", return_value=multi_p):
        found = svc.resolve(settings, "BV1math00009")
    check(found.kind == "pages", f"kind={found.kind}，不是 season")
    check(found.total == 3, f"取自己的 3 个分P，实际 {found.total}")
    check(all(i.bvid == "BV1math00009" for i in found.items), "分P属于同一个 BV")
    check([i.page for i in found.items] == [1, 2, 3], "page 递增")
    check(found.self_total == 3, f"self_total={found.self_total}")
    check(found.season_title == "期末冲刺上大分【大学篇】" and len(found.sections) == 2,
          "合集信息仍带上，供前端切换")

    print("\n[8] 多P视频切到合集视图（mode=season）")
    with patch.object(svc, "_view", return_value=multi_p):
        season_view = svc.resolve(settings, "BV1math00009", mode="season")
    check(season_view.kind == "season", f"kind={season_view.kind}")
    check(season_view.title == "数学类", f"标题={season_view.title}")
    check(season_view.self_total == 3, f"仍知道自己有 {season_view.self_total} 个分P，可切回")

    print("\n========================================")
    if FAILS:
        print(f"失败 {len(FAILS)} 项：" + "；".join(FAILS))
        return 1
    print("分章节展开全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
