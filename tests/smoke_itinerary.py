"""行程单（A 档）冒烟：抽取、去重、时间戳吸附、渲染。

用假客户端，不消耗真实 Key，也不联网。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.models import Chunk, Segment, VideoMeta  # noqa: E402
from app.services import itinerary  # noqa: E402


class FakeClient:
    name = "fake"

    def __init__(self, payload):
        self.payload = payload
        self.calls = 0

    def complete_json(self, system, user, max_tokens=1024):
        self.calls += 1
        assert "只做搬运" in system
        assert "绝不编造" in user
        return self.payload


def meta() -> VideoMeta:
    return VideoMeta(
        bvid="BV1", cid=1, title="越南七天游攻略", uploader="u",
        duration=1200, pubdate=0, cover="", webpage_url="https://b23.tv/x",
    )


def segs() -> list[Segment]:
    return [
        Segment(0, 10, "今天我们到了河内"),
        Segment(100, 110, "第二站是火车街，火车真的会穿街"),
        Segment(200, 210, "然后我们去了文庙祈福"),
        Segment(300, 310, "文庙旁边吃了碗粉"),
    ]


def chunks() -> list[Chunk]:
    return [Chunk(index=0, start=0, end=310, text="今天我们到了河内 第二站是火车街 然后去文庙")]


def test_travel_score():
    """真实的旅行视频转录是几千字、信号词密集，这里就按真实密度造。"""
    travel_text = (
        "今天我们到了河内，当地大学生做导游带我们玩。先去了火车街，"
        "这条街两边都是咖啡店，门票不要钱。中午吃小吃，打车过去二十块。"
        "下午去文庙，景区人很多。晚上住民宿，老板很热情。第二天包车去岘港，"
        "路上经过几个景点顺便打卡，机场那边也有直达大巴。住宿和餐饮都比国内便宜。"
    )
    travel = itinerary.travel_score(
        meta(), [Chunk(index=0, start=0, end=310, text=travel_text)]
    )
    tech = itinerary.travel_score(
        VideoMeta(bvid="BV2", cid=1, title="Claude Code 全流程实战", uploader="u",
                  duration=600, pubdate=0, cover="", webpage_url="u"),
        [Chunk(index=0, start=0, end=600,
               text="这一节讲组件封装和状态管理，先写接口再写测试，最后接数据库")],
    )
    assert travel >= 6, travel
    assert tech < 6, tech
    print(f"  旅行判定：旅行视频 {travel} 分命中 / 技术视频 {tech} 分 → 不误判")


def test_build_and_dedupe():
    payload = {
        "is_travel": True,
        "summary": "河内两日",
        "stops": [
            {"order": 9, "name": "火车街", "city": "河内", "kind": "景点", "ts": 0, "what": "看火车"},
            {"order": 9, "name": "火车街", "city": "河内", "kind": "景点", "ts": 0, "what": "重复"},
            {"name": "文庙", "city": "河内", "kind": "景点", "ts": 2000, "what": "祈福"},
            {"name": "", "city": "河内"},
        ],
        "legs": [
            {"from_order": 1, "to_order": 2, "transport": "摩托车", "cost": "", "duration": "10分钟"},
            {"from_order": 1, "to_order": 99, "transport": "飞机"},
        ],
        "budget": "只提到椰子船 45 块",
    }
    client = FakeClient(payload)
    it = itinerary.build_itinerary(client, meta(), chunks(), segments=segs())
    assert it is not None
    assert [s["name"] for s in it["stops"]] == ["火车街", "文庙"], it["stops"]
    assert [s["order"] for s in it["stops"]] == [1, 2], "编号必须重排为连续"
    assert len(it["legs"]) == 1, "指向不存在地点的路段要丢掉"
    assert it["cities"] == ["河内"]
    print(f"  抽取：去重 / 重编号 / 丢弃越界路段 → {len(it['stops'])} 站 {len(it['legs'])} 段")


def test_snap_ts():
    payload = {
        "stops": [
            {"name": "火车街", "ts": 0},
            {"name": "文庙", "ts": 0},
        ],
        "legs": [],
    }
    it = itinerary.build_itinerary(FakeClient(payload), meta(), chunks(), segments=segs())
    ts = [s["ts"] for s in it["stops"]]
    assert ts == [100.0, 200.0], f"时间戳应吸附到原句：{ts}"
    print(f"  时间戳吸附：两个都写 0 的块起点 → 钉到原句 {ts}")


def test_too_few_stops():
    it = itinerary.build_itinerary(
        FakeClient({"stops": [{"name": "河内"}], "legs": []}), meta(), chunks()
    )
    assert it is None, "只有一个点不该出行程单"
    assert itinerary.build_itinerary(None, meta(), chunks()) is None
    print("  少于两个点 / 无客户端 → 返回 None，不出半成品")


def test_render():
    payload = {
        "summary": "河内两日",
        "stops": [
            {"name": "火车街", "city": "河内", "kind": "景点", "ts": 100, "what": "看火车", "cost": ""},
            {"name": "文庙", "city": "河内", "kind": "景点", "ts": 200, "what": "祈福", "cost": "30块钱"},
        ],
        "legs": [{"from_order": 1, "to_order": 2, "transport": "", "cost": "", "duration": ""}],
        "budget": "只提到文庙 30 块",
    }
    it = itinerary.build_itinerary(FakeClient(payload), meta(), chunks(), segments=segs())
    md = "\n".join(itinerary.render_section(it, url="https://b23.tv/x"))
    assert "## 行程路线" in md
    assert "| 1 | 火车街" in md
    assert "t=100" in md, "要能点回视频"
    assert "未提及" in md, "没说的交通要显式写未提及"
    assert "不做推测" in md, "必须带免责说明"
    print("  渲染：表格 / 时间戳跳转 / 未提及留白 / 免责声明 齐全")




def test_itinerary_enters_notes():
    """流水线集成：note.itinerary 有内容时，notes.md 必须出现「行程路线」一节。"""
    import tempfile, shutil
    from app.core.models import VideoNote
    from app.services.exporter import export

    root = Path(tempfile.mkdtemp())
    try:
        it = itinerary.build_itinerary(None, meta(), chunks())
        assert it is None  # 无客户端不生成

        it = {
            "summary": "两日城市漫步", "budget": "",
            "cities": ["A城", "B城"],
            "stops": [
                {"order": 1, "name": "老街", "city": "A城", "kind": "街区",
                 "ts": 100.0, "what": "看老建筑", "cost": ""},
                {"order": 2, "name": "海港", "city": "B城", "kind": "景点",
                 "ts": 200.0, "what": "坐船", "cost": "50 元"},
            ],
            "legs": [
                {"from_order": 1, "to_order": 2, "transport": "大巴",
                 "cost": "30 元", "duration": "2 小时", "note": ""},
            ],
        }
        m = meta()
        note = VideoNote(meta=m, itinerary=it)
        md_path = export(note, None, root / "out", [], "")
        md = md_path.read_text(encoding="utf-8")
        assert "## 行程路线" in md, "行程路线必须出现在笔记里"
        assert "老街" in md and "大巴" in md and "50 元" in md
        assert "[01:40]" in md, "时间戳要渲染成可跳转格式"
        note2 = VideoNote(meta=m)   # 非旅行视频：不出这一节
        md2 = export(note2, None, root / "out2", [], "")
        assert "行程路线" not in md2.read_text(encoding="utf-8")
        print("  集成：旅行笔记含「行程路线」，非旅行笔记不含")
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    print("itinerary smoke:")
    test_travel_score()
    test_build_and_dedupe()
    test_snap_ts()
    test_too_few_stops()
    test_render()
    test_itinerary_enters_notes()
    print("all passed")
