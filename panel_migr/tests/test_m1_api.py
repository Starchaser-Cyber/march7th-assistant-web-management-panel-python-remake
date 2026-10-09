"""v1.23 M1：HTTP 级接口测试（收益日报 / 成功率统计 / 数据导出）。

全部离线：DATA_DIR 指向 tmp，事件直接灌 SQLite，不碰真实服务器与日志。
"""
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config as cfg
import pytest
from fastapi.testclient import TestClient

from main import app
from services import eventdb
from services import history as hist
from services.instances import instances_write

client = TestClient(app)

INST_DIR = Path(__file__).resolve().parent / "_tmp" / "m1inst"
(INST_DIR / "logs").mkdir(parents=True, exist_ok=True)
(INST_DIR / "config.yaml").write_text("power_enable: true\n", "utf-8")
TEST_INST = {"id": "m1x", "name": "M1 测试实例", "container": "m1x-test",
             "dir": str(INST_DIR), "default": True}

instances_write([TEST_INST])


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    """独立 DATA_DIR + 事件库复位。

    实例配置写在同一份 .instances.php 上，其它测试模块会在 import 期写自己的实例，
    导致「当前实例」与用例灌数据的 id 对不上；这里接管后再原样还原，避免污染。
    """
    from pathlib import Path as _P

    from services import monitor as _mon

    ipath = _P(cfg.INSTANCES_FILE)
    backup = ipath.read_bytes() if ipath.exists() else None
    instances_write([TEST_INST])

    data = tmp_path / "data"
    monkeypatch.setattr(cfg, "DATA_DIR", data)
    monkeypatch.setattr(hist, "DATA_DIR", data)
    monkeypatch.setattr(_mon, "DATA_DIR", data)
    eventdb.reset()
    yield
    eventdb.reset()
    if backup is None:
        ipath.unlink(missing_ok=True)
    else:
        ipath.write_bytes(backup)


def _session(authed=True):
    sid = "m1" + uuid.uuid4().hex[:10]
    parts = ["m7a_panel_auth|b:1;"] if authed else []
    p = Path(cfg.SESSION_SAVE_PATH)
    p.mkdir(parents=True, exist_ok=True)
    (p / f"sess_{sid}").write_text("".join(parts), encoding="utf-8")
    return {"PHPSESSID": sid}


def _get(qs, authed=True):
    return client.get("/?" + qs, cookies=_session(authed), follow_redirects=False)


def _seed_history(ts: int, key: str, label: str, status: str, dur: int = 300) -> None:
    eventdb.insert_event(ts, "history", TEST_INST["id"], {
        "id": ts % 100000, "task_key": key, "task_label": label,
        "status": status, "exit": 0 if status == "done" else -1,
        "start_ts": ts - dur, "end_ts": ts,
    })


def _seed_outcome(ts: int, key: str, label: str, oc: dict) -> None:
    eventdb.insert_event(ts, "outcome", TEST_INST["id"], {
        "id": ts % 100000, "task_key": key, "task_label": label,
        "start_ts": ts - 600, "outcome": oc,
    })


# ================= 1. 收益日报接口 =================

def test_outcome_daily_endpoint_ok():
    now = int(time.time())
    _seed_outcome(now - 60, "daily", "日常", {"dungeon": 2, "score": 100, "stamina": 120,
                                              "stamina_total": 300, "modes": ["货币战争"],
                                              "notes": ["领取巡星之礼奖励完成"]})
    r = _get("ajax=outcome_daily&days=7")
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True
    assert d["total_tasks"] == 1
    assert d["today"]["dungeon"] == 2
    assert d["today"]["score"] == 100
    assert len(d["list"]) == 7


def test_outcome_daily_days_clamped():
    assert _get("ajax=outcome_daily&days=999").json()["range_days"] == 30
    assert _get("ajax=outcome_daily&days=abc").json()["range_days"] == 7
    assert _get("ajax=outcome_daily").json()["range_days"] == 7


def test_outcome_daily_requires_login():
    r = _get("ajax=outcome_daily", authed=False)
    assert r.status_code in (200, 302, 401, 403)
    if r.status_code == 200:
        assert "text/html" in r.headers.get("content-type", "")


# ================= 2. 成功率统计接口 =================

def test_task_stats_endpoint_ok_and_rate():
    now = int(time.time())
    for i in range(8):
        _seed_history(now - 3600 - i * 60, "daily", "日常", "done")
    for i in range(2):
        _seed_history(now - 7200 - i * 60, "daily", "日常", "aborted")
    _seed_history(now - 1000, "power", "清体力", "done")

    d = _get("ajax=task_stats&days=30").json()
    assert d["ok"] is True
    assert d["total"] == 11
    assert d["done"] == 9
    assert d["aborted"] == 2
    assert round(d["rate"], 1) == 81.8

    by_key = {it["task_key"]: it for it in d["items"]}
    assert by_key["daily"]["total"] == 10
    assert by_key["daily"]["done"] == 8
    assert by_key["daily"]["aborted"] == 2
    assert round(by_key["daily"]["rate"], 1) == 80.0
    assert by_key["power"]["rate"] == 100.0
    assert by_key["daily"]["avg_duration"] == 300


def test_task_stats_window_filters_old_events():
    now = int(time.time())
    _seed_history(now - 3600, "daily", "日常", "done")
    _seed_history(now - 40 * 86400, "daily", "日常", "aborted")   # 40 天前，窗口外
    d = _get("ajax=task_stats&days=30").json()
    assert d["total"] == 1
    assert d["aborted"] == 0


def test_task_stats_empty_is_safe():
    d = _get("ajax=task_stats&days=30").json()
    assert d["ok"] is True
    assert d["total"] == 0
    assert d["items"] == []
    assert d["rate"] == 0


def test_task_stats_falls_back_to_history_json(monkeypatch):
    """事件库没数据（老版本升级上来）时，回退读 history JSON，不让页面空白。"""
    hist.history_add(TEST_INST, "daily", "日常")
    items = hist.history_read(TEST_INST)["items"]
    items[-1]["status"] = "aborted"
    items[-1]["start_ts"] = int(time.time()) - 120
    items[-1]["end_ts"] = int(time.time())
    hist.history_write(TEST_INST, {"items": items})

    d = _get("ajax=task_stats&days=30").json()
    assert d["ok"] is True
    assert d["total"] == 1
    assert d["aborted"] == 1
    assert d["source"] == "history.json"


# ================= 3. 数据导出 =================

def test_export_history_csv():
    now = int(time.time())
    _seed_history(now - 600, "daily", "日常", "done")
    r = _get("export=history&days=90")
    assert r.status_code == 200
    assert "text/csv" in r.headers.get("content-type", "")
    assert "attachment" in r.headers.get("content-disposition", "")
    assert "m7a_tasks_" in r.headers.get("content-disposition", "")
    assert r.text.startswith("\ufeff")            # BOM，Excel 不乱码
    assert "任务" in r.text
    assert "日常" in r.text


def test_export_history_json_mode():
    now = int(time.time())
    _seed_history(now - 600, "daily", "日常", "done")
    d = _get("export=history&days=90&format=json").json()
    assert d["ok"] is True
    assert d["count"] == 1
    assert d["rows"][0][2] == "日常"
    assert d["rows"][0][5] == "已完成"


def test_export_alerts_csv():
    now = int(time.time())
    eventdb.insert_event(now - 300, "alert", TEST_INST["id"], {
        "kind": "down", "title": "容器离线", "body": "m7a 未运行",
        "pushed": True, "msg": "ok"})
    d = _get("export=alerts&days=30&format=json").json()
    assert d["ok"] is True
    assert d["count"] == 1
    assert d["rows"][0][1] == "离线告警"
    assert d["rows"][0][3] == "容器离线"


def test_export_monitor_csv_has_rows():
    """7 天窗口 → 走分钟聚合表（与图表口径一致）。"""
    now = int(time.time())
    for i in range(5):
        eventdb.insert_minute(now - i * 300, 10 + i, 20.0, 30, inst=TEST_INST["id"])
    d = _get("export=monitor&days=7&format=json").json()
    assert d["ok"] is True
    assert d["count"] >= 1
    assert "CPU(%)" in d["header"]


def test_export_requires_login():
    for qs in ("export=history", "export=alerts", "export=monitor"):
        r = _get(qs, authed=False)
        assert "attachment" not in r.headers.get("content-disposition", "")


def test_export_unknown_value_not_hijacked():
    """?export=xxx 未注册，应走普通页面渲染而不是被当成导出。"""
    r = _get("export=xxx")
    assert "attachment" not in r.headers.get("content-disposition", "")
