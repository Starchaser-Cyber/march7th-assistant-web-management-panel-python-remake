"""v1.23 M1-1：任务收益解析 + 收益日报聚合（纯离线，不依赖真实日志/服务器）。

覆盖：
- parse_log 对真实日志行格式的提取（开拓力/挑战/副本/实训得分/玩法完成/备注）
- 容错：空文本、乱码行、日志格式变化时只少统计、不抛异常
- outcome_summary 摘要文案
- outcome_daily 按天聚合（最新快照覆盖旧值、跨天分桶、天数裁剪）
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config as cfg
import pytest

from services import eventdb
from services import history
from services import outcome

MAX_DAYS_CAP = outcome.MAX_DAYS

# 摘取的真实日志片段（格式与本体运行日志一致）
SAMPLE_LOG = """\
2026-10-03 08:02:17,761 | INFO | 领取巡星之礼奖励完成
2026-10-03 00:57:13,884 | INFO | 开拓力: 109/300
2026-10-03 00:57:13,884 | INFO | 开拓力: 109 = 2 次挑战
2026-10-03 08:04:56,596 | INFO | 第1次副本完成
2026-10-03 08:05:03,100 | INFO | 第2次副本完成
2026-10-03 08:05:24,443 | INFO | 登录游戏: 已完成 +  (+100分)
2026-10-03 08:05:24,443 | INFO | 派遣委托或收取1次委托奖励: 待完成
2026-10-03 08:05:24,443 | INFO | 使用1次「万能合成机」: 待完成
2026-10-03 04:57:00,000 | INFO | smtp 通知发送完成
2026-10-03 02:50:58,637 | INFO | 货币战争已完成
2026-10-03 05:00:00,000 | INFO | 「差分宇宙」积分奖励未开启
"""


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    """每个用例独立 DATA_DIR + 重置事件库连接（与既有 v1.22 用例一致）。"""
    from services import monitor as _mon
    data = tmp_path / "data"
    monkeypatch.setattr(cfg, "DATA_DIR", data)
    monkeypatch.setattr(history, "DATA_DIR", data)
    monkeypatch.setattr(_mon, "DATA_DIR", data)
    eventdb.reset()
    yield
    eventdb.reset()


def _inst(tmp_path):
    return {"id": "m1", "name": "m1", "container": "m1", "dir": str(tmp_path)}


# ================= 1. 解析 =================

def test_parse_log_extracts_core_metrics():
    oc = outcome.parse_log(SAMPLE_LOG)
    assert oc["stamina"] == 109
    assert oc["stamina_total"] == 300
    assert oc["challenges"] == 2
    assert oc["dungeon"] == 2           # 取最大次数，而不是相加
    assert oc["score"] == 100           # 实训得分累计
    assert oc["daily_done"] == 1
    assert oc["daily_todo"] == 2
    assert oc["modes"] == ["货币战争"]   # 「未开启」的不算完成
    assert oc["lines"] == 11


def test_parse_log_ignores_non_task_noise():
    """系统类「完成」行不应被误判成实训条目。"""
    oc = outcome.parse_log("\n".join([
        "2026-10-03 04:57:00,000 | INFO | smtp 通知发送完成",
        "2026-10-03 04:57:01,000 | INFO | 战斗完成",
        "2026-10-03 04:57:02,000 | INFO | 当前界面：无名勋礼-奖励",
    ]))
    assert oc["daily_done"] == 0
    assert oc["daily_todo"] == 0
    assert oc["notes"] == []


def test_parse_log_tolerates_garbage_and_empty():
    for text in ("", "   \n\n", "not-a-log-line", "2026-13-99 99:99:99 | INFO | x"):
        oc = outcome.parse_log(text)
        assert oc["stamina"] is None
        assert oc["dungeon"] == 0
        assert oc["score"] == 0


def test_parse_log_takes_last_snapshot_for_stamina():
    """开拓力是快照值：后出现的应覆盖先出现的。"""
    oc = outcome.parse_log("\n".join([
        "2026-10-03 00:10:00,000 | INFO | 开拓力: 300/300",
        "2026-10-03 08:10:00,000 | INFO | 开拓力: 40/300",
    ]))
    assert oc["stamina"] == 40


def test_parse_log_collects_notes_dedup_and_capped():
    lines = ["2026-10-03 08:0%d:00,000 | INFO | 领取巡星之礼奖励完成" % i for i in range(6)]
    oc = outcome.parse_log("\n".join(lines))
    assert oc["notes"].count("领取巡星之礼奖励完成") == 1   # 去重
    assert len(oc["notes"]) <= 8                            # 有上限


# ================= 2. 摘要 =================

def test_outcome_summary_text():
    oc = outcome.parse_log(SAMPLE_LOG)
    s = history.outcome_summary(oc)
    assert "副本 2 次" in s
    assert "实训 +100 分" in s
    assert "货币战争" in s


def test_outcome_summary_empty_when_nothing_parsed():
    assert history.outcome_summary({"lines": 3}) == ""
    assert history.outcome_summary(None) == ""


# ================= 3. 日报聚合 =================

def _seed(inst_id: str, ts: int, payload: dict) -> None:
    eventdb.insert_event(ts, "outcome", inst_id, payload)


def test_outcome_daily_buckets_by_day_and_sums():
    now = int(time.time())
    day = 86400
    today0 = outcome._day_start(now)
    # 今天两次任务
    _seed("m1", today0 + 3600, {"task_key": "daily", "task_label": "日常",
                                "outcome": {"dungeon": 2, "score": 100, "stamina": 150,
                                            "stamina_total": 300, "modes": ["货币战争"],
                                            "notes": ["领取巡星之礼奖励完成"]}})
    _seed("m1", today0 + 7200, {"task_key": "power", "task_label": "清体力",
                                "outcome": {"dungeon": 3, "score": 200, "stamina": 60,
                                            "stamina_total": 300, "modes": [], "notes": []}})
    # 昨天一次
    _seed("m1", today0 - day + 3600, {"task_key": "daily", "task_label": "日常",
                                      "outcome": {"dungeon": 1, "score": 50}})
    # 别的实例，不应混入
    _seed("other", today0 + 3600, {"task_key": "daily", "task_label": "日常",
                                   "outcome": {"dungeon": 99, "score": 999}})

    d = outcome.outcome_daily(_inst(Path("/tmp")), days=7)
    assert d["total_tasks"] == 3
    assert d["range_days"] == 7
    assert len(d["list"]) == 7

    today = d["today"]
    assert today["tasks"] == 2
    assert today["dungeon"] == 5            # 2 + 3
    assert today["score"] == 300            # 100 + 200
    assert today["stamina"] == 60           # 当天最后一次快照
    assert today["stamina_total"] == 300
    assert today["modes"] == ["货币战争"]

    yday = d["list"][-2]
    assert yday["tasks"] == 1
    assert yday["dungeon"] == 1


def test_outcome_daily_empty_is_safe():
    d = outcome.outcome_daily(_inst(Path("/tmp")), days=7)
    assert d["total_tasks"] == 0
    assert d["has_data"] is False
    assert d["today"]["tasks"] == 0
    assert len(d["list"]) == 7


def test_outcome_daily_clamps_days():
    assert len(outcome.outcome_daily(_inst(Path("/tmp")), days=0)["list"]) == 1
    assert len(outcome.outcome_daily(_inst(Path("/tmp")), days=999)["list"]) == MAX_DAYS_CAP


# ================= 4. 日志窗口读取（monkeypatch run_cmd，不碰真实文件） =================

def test_extract_for_task_reads_only_window(monkeypatch):
    log = Path("/tmp/m1_fake.log")
    log.write_text("\n".join([
        "2026-10-03 07:00:00,000 | INFO | 第9次副本完成",   # 窗口之前
        "2026-10-03 08:01:00,000 | INFO | 第1次副本完成",   # 窗口内
        "2026-10-03 08:01:01,000 | INFO | 登录游戏: 已完成 +  (+100分)",
        "2026-10-03 09:30:00,000 | INFO | 第8次副本完成",   # 窗口之后
    ]), encoding="utf-8")
    monkeypatch.setattr(outcome, "latest_log_path", lambda inst: str(log))
    monkeypatch.setattr(outcome, "run_cmd",
                        lambda cmd, timeout=60: {"out": log.read_text(encoding="utf-8"), "code": 0})
    start = int(time.mktime((2026, 10, 3, 8, 0, 0, 0, 0, -1)))
    end = int(time.mktime((2026, 10, 3, 9, 0, 0, 0, 0, -1)))
    oc = outcome.extract_for_task(Path("/tmp"), {"start_ts": start, "end_ts": end})
    assert oc["dungeon"] == 1        # 只统计窗口内
    assert oc["score"] == 100


def test_extract_for_task_never_raises(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("run_cmd 挂了")
    monkeypatch.setattr(outcome, "latest_log_path", lambda inst: "/nope/x.log")
    monkeypatch.setattr(outcome, "run_cmd", _boom)
    assert outcome.extract_for_task(Path("/tmp"), {"start_ts": 1, "end_ts": 2}) == {}
    assert outcome.extract_for_task(Path("/tmp"), None) == {}
