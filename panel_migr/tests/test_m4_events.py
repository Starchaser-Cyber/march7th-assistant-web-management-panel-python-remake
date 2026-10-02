"""M4 事件化测试：事件总线 / WS 断线补发与 resync / 日志流式 / 采样落库 /
历史退出码 / 告警状态机事件 / alert_history 接口 / runner 开关。

约定：DATA_DIR（history/monitor/SQLite）全部 monkeypatch 到 tmp_path；
runner 默认关闭（conftest M7A_EVENTS_BG=0）；run_cmd 打桩避开本机 docker。
"""
import json
import os
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config as cfg
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from main import app
from services import alerts as al
from services import eventdb
from services import events
from services import history as hist
from services import logstream
from services import monitor as mon
from services import sampler

client = TestClient(app)


def _authed_cookies():
    sid = "m4" + uuid.uuid4().hex[:10]
    p = Path(cfg.SESSION_SAVE_PATH)
    p.mkdir(parents=True, exist_ok=True)
    (p / f"sess_{sid}").write_text("m7a_panel_auth|b:1;", encoding="utf-8")
    return {"PHPSESSID": sid}


def _ws_headers(cookies):
    return {"cookie": f"PHPSESSID={cookies['PHPSESSID']}"}


def _inst(tmp_path):
    return {"id": "t4", "name": "测试", "container": "t4", "dir": str(tmp_path)}


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    events.reset_state()
    logstream.reset_state()
    data = tmp_path / "data"
    monkeypatch.setattr(cfg, "DATA_DIR", data)
    monkeypatch.setattr(hist, "DATA_DIR", data)
    monkeypatch.setattr(mon, "DATA_DIR", data)
    yield
    events.reset_state()
    logstream.reset_state()


# ---------- 事件总线 ----------


def test_publish_replay_and_seq():
    events.publish("system", {"i": 1})
    events.publish("system", {"i": 2})
    events.publish("system", {"i": 3})
    rows = events.events_since(0)
    assert [e["seq"] for e in rows] == [1, 2, 3]
    assert events.current_seq() == 3
    assert events.events_since(3) == []          # 追平


def test_buffer_hole_triggers_resync():
    # monitor 类型不落库，纯内存灌缓冲
    for i in range(events.BUFFER + 50):
        events.publish("monitor", {"i": i})
    cur = events.current_seq()
    assert cur == events.BUFFER + 50
    assert events.events_since(1) is None        # seq1 已被挤出 → 有洞
    oldest = events.dump_last(events.BUFFER)[0]["seq"]
    rows = events.events_since(oldest - 1)       # 缓冲内完整段可补
    assert rows is not None and len(rows) == events.BUFFER


def test_wait_new_timeout_and_notify():
    rows, timed_out = events.wait_new(0, 0.05)
    assert rows == [] and timed_out is True
    events.publish("monitor", {"k": "v"})
    rows2, timed_out2 = events.wait_new(0, 0.05)
    assert timed_out2 is False
    assert [e["payload"]["k"] for e in rows2] == ["v"]


def test_persisted_types_written_to_sqlite():
    events.publish("monitor", {"a": 1})          # 不落库
    events.publish("history", {"id": 9, "status": "done"})
    rows = eventdb.list_events("history", 10)
    assert len(rows) == 1 and rows[0]["id"] == 9
    assert eventdb.list_events("monitor", 10) == []


def test_eventdb_trim_drops_old_samples(tmp_path):
    now = int(time.time())
    for ts in (now - 40 * 86400, now - 100, now):
        eventdb.insert_sample({"t": ts, "cpu": 1.0}, True, inst="t4")
    # 节流按秒粒度：三条时间戳相隔足够远，均入库
    assert eventdb.trim(now) is True
    import sqlite3
    conn = sqlite3.connect(cfg.DATA_DIR / "panel_events.db")
    n = conn.execute("SELECT COUNT(*) FROM monitor_samples").fetchone()[0]
    conn.close()
    assert n == 2                                     # 40 天前的被裁掉，近两条保留


# ---------- WS：鉴权 / 推送 / 断线补发 ----------


def test_ws_requires_auth():
    with pytest.raises(WebSocketDisconnect) as ei:
        with client.websocket_connect("/m7a-events") as ws:
            ws.receive_text()
    assert ei.value.code == 1008


def test_ws_hello_and_live_push():
    hdr = _ws_headers(_authed_cookies())
    with client.websocket_connect("/m7a-events", headers=hdr) as ws:
        hello = json.loads(ws.receive_text())
        assert hello["type"] == "hello" and hello["seq"] == 0
        events.publish("system", {"n": 1})
        evt = json.loads(ws.receive_text())
        assert evt["seq"] == 1
        assert evt["type"] == "system" and evt["payload"] == {"n": 1}


def test_ws_reconnect_since_backfill():
    """断线期间产生的事件，重连带 since 补齐（M4 验收用例）。"""
    hdr = _ws_headers(_authed_cookies())
    with client.websocket_connect("/m7a-events", headers=hdr) as ws:
        hello = json.loads(ws.receive_text())
        assert hello["seq"] == 0
        events.publish("monitor", {"t": 1})
        first = json.loads(ws.receive_text())
        assert first["seq"] == 1
        last_seen = first["seq"]
    # —— 断线期间新产生两条 ——
    events.publish("monitor", {"t": 2})
    events.publish("alert", {"kind": "down", "title": "容器意外停止"})
    with client.websocket_connect(f"/m7a-events?since={last_seen}",
                                  headers=hdr) as ws:
        hello2 = json.loads(ws.receive_text())
        assert hello2["seq"] == 3
        e2 = json.loads(ws.receive_text())
        e3 = json.loads(ws.receive_text())
        assert (e2["seq"], e3["seq"]) == (2, 3)
        assert e3["type"] == "alert"


def test_ws_resync_when_since_too_old():
    hdr = _ws_headers(_authed_cookies())
    for i in range(events.BUFFER + 20):
        events.publish("monitor", {"i": i})
    with client.websocket_connect("/m7a-events?since=1", headers=hdr) as ws:
        hello = json.loads(ws.receive_text())
        r = json.loads(ws.receive_text())
        assert r["type"] == "resync"
        assert r["seq"] == hello["seq"] == events.current_seq()


# ---------- 监控采样：WS 推送 + SQLite 归档 ----------


def test_monitor_sample_publishes_and_archives(monkeypatch, tmp_path):
    def _fake_run(cmd, timeout=120):
        if cmd.startswith("df "):
            return {"code": 0, "out": (
                "Filesystem 1B-blocks Used Available Use% Mounted on\n"
                "/dev/sda1 1000000000 400000000 600000000 40% /\n")}
        return {"code": 1, "out": ""}             # docker 不可用 → running=False

    monkeypatch.setattr(mon, "run_cmd", _fake_run)
    inst = _inst(tmp_path)
    r = mon.monitor_sample(inst, 5)
    assert r["sampled"] is True
    mons = [e for e in events.events_since(0) if e["type"] == "monitor"]
    assert len(mons) == 1 and "cpu" in mons[0]["payload"]

    import sqlite3
    conn = sqlite3.connect(cfg.DATA_DIR / "panel_events.db")
    n = conn.execute("SELECT COUNT(*) FROM monitor_samples").fetchone()[0]
    conn.close()
    assert n == 1

    r2 = mon.monitor_sample(inst, 5)               # 节流窗口内 → 不重复
    assert r2["sampled"] is False
    assert len([e for e in events.events_since(0)
                if e["type"] == "monitor"]) == 1


# ---------- 历史：退出码 + 事件 ----------


def test_history_sync_exit_done(monkeypatch, tmp_path):
    inst = _inst(tmp_path)
    monkeypatch.setattr(hist, "container_is_running", lambda i: True)
    logs = tmp_path / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    lf = logs / "a.log"
    lf.write_text("task start\n", encoding="utf-8")
    old = time.time() - 200                        # 静默 200s > 90s，但 < 270s
    os.utime(lf, (old, old))
    hist.history_write(inst, {"items": [{
        "id": 1, "task_key": "k", "task_label": "任务", "instance": "t4",
        "start_ts": int(time.time() - 400), "end_ts": 0, "status": "running",
    }]})
    d = hist.history_sync(inst)
    it = d["items"][0]
    assert it["status"] == "done"
    assert it["exit"] == 0
    evs = [e for e in events.events_since(0) if e["type"] == "history"]
    assert len(evs) == 1
    assert evs[0]["payload"]["exit"] == 0
    assert evs[0]["payload"]["task_label"] == "任务"
    # 再次 sync：无 running 残留 → 不重复发布
    hist.history_sync(inst)
    assert len([e for e in events.events_since(0)
                if e["type"] == "history"]) == 1


def test_history_sync_exit_aborted_no_log(monkeypatch, tmp_path):
    inst = _inst(tmp_path)                         # 无 logs 目录
    hist.history_write(inst, {"items": [{
        "id": 7, "task_key": "k", "task_label": "任务", "instance": "t4",
        "start_ts": int(time.time() - 400), "end_ts": 0, "status": "running",
    }]})
    d = hist.history_sync(inst)
    it = d["items"][0]
    assert it["status"] == "aborted"
    assert it["exit"] == -1
    evs = [e for e in events.events_since(0) if e["type"] == "history"]
    assert len(evs) == 1 and evs[0]["payload"]["exit"] == -1


def test_history_view_exit_field_only_when_present():
    now = int(time.time())
    out = hist.history_view([
        {"id": 1, "task_label": "a", "start_ts": now - 10, "end_ts": now,
         "status": "done", "exit": 0},
        {"id": 2, "task_label": "b", "start_ts": now - 5,
         "status": "running"},
    ])
    by_id = {r["id"]: r for r in out}
    assert by_id[1]["exit"] == 0                   # Python 判定过的带退出码
    assert "exit" not in by_id[2]                  # 老记录不出现该键


# ---------- 告警状态机：事件发布 + 留档 ----------


def test_alert_state_machine_publishes_and_archives(monkeypatch, tmp_path):
    inst = _inst(tmp_path)
    runs = iter([True, False, True])
    monkeypatch.setattr(al, "container_is_running", lambda i: next(runs))
    monkeypatch.setattr(al, "alert_cfg",
                        lambda i: {"enable": True, "channel": "bark",
                                   "target": "tk"})
    sent = []

    def _send(title, body, override=None):
        sent.append(title)
        return {"ok": True, "msg": "ok"}

    monkeypatch.setattr(al, "alert_send", _send)

    r1 = al.alert_tick(inst, force=True)           # prev=None → True：无事件
    assert r1["pushed"] is False and sent == []
    r2 = al.alert_tick(inst, force=True)           # True → False：down
    assert r2["pushed"] is True and len(sent) == 1
    r3 = al.alert_tick(inst, force=True)           # False → True：recovered
    assert r3["pushed"] is True and len(sent) == 2

    live = [e["payload"]["kind"] for e in events.events_since(0)
            if e["type"] == "alert"]
    assert live == ["down", "recovered"]
    archived = eventdb.list_events("alert", 10)    # seq 倒序
    assert [a["kind"] for a in archived] == ["recovered", "down"]
    assert archived[0]["pushed"] is True
    assert archived[0]["msg"] == "ok"


def test_alert_history_endpoint():
    events.publish("alert", {"kind": "down", "title": "容器意外停止",
                             "body": "b", "pushed": True, "msg": "ok"},
                   inst="t4")
    r = client.get("/?ajax=alert_history", cookies=_authed_cookies())
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] and len(d["items"]) == 1
    assert d["items"][0]["kind"] == "down"
    assert d["items"][0]["pushed"] is True
    # 未登录 → Python 渲染登录页（M5-B 不再依赖 PHP）
    r2 = client.get("/?ajax=alert_history")
    assert "auth-card" in r2.text


# ---------- 日志流式 ----------


def test_logstream_incremental_and_truncate(tmp_path):
    inst = _inst(tmp_path)
    logs = tmp_path / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    f = logs / "t.log"
    f.write_text("line1\n", encoding="utf-8")
    assert logstream.tail_tick(inst) == 0          # 首次跟踪：从尾部起
    with open(f, "a", encoding="utf-8") as h:
        h.write("line2\nline3\n")
    assert logstream.tail_tick(inst) == 1
    evt = events.dump_last(1)[0]
    assert evt["type"] == "log"
    assert evt["payload"]["file"] == "t.log"
    assert evt["payload"]["lines"] == ["line2", "line3"]
    assert logstream.tail_tick(inst) == 0          # 无新增
    f.write_text("fresh\n", encoding="utf-8")      # 截断/轮转 → 从头读
    assert logstream.tail_tick(inst) == 1
    assert events.dump_last(1)[0]["payload"]["lines"] == ["fresh"]


def test_logstream_rotates_to_new_file(tmp_path):
    inst = _inst(tmp_path)
    logs = tmp_path / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    a = logs / "a.log"
    a.write_text("old1\nold2\n", encoding="utf-8")
    past = time.time() - 100
    os.utime(a, (past, past))
    assert logstream.tail_tick(inst) == 0          # 首次跟踪 a 尾部
    b = logs / "b.log"
    b.write_text("new task\n", encoding="utf-8")   # mtime 最新 → 切换
    assert logstream.tail_tick(inst) == 1
    evt = events.dump_last(1)[0]
    assert evt["payload"]["file"] == "b.log"       # 新文件从头全文推
    assert evt["payload"]["lines"] == ["new task"]


def test_logstream_no_files_is_noop(tmp_path):
    inst = _inst(tmp_path)                         # 无 logs 目录
    assert logstream.tail_tick(inst) == 0
    assert events.current_seq() == 0


# ---------- sampler：保底间隔与开关 ----------


def test_sample_iv_floor(monkeypatch):
    monkeypatch.setattr(cfg, "SAMPLE_IV", 30)
    monkeypatch.setattr(mon, "monitor_interval", lambda: 1)
    assert sampler.sample_iv() == 30               # 面板配置 1s → 抬到保底 30
    monkeypatch.setattr(mon, "monitor_interval", lambda: 60)
    assert sampler.sample_iv() == 60               # 配置更高 → 尊重配置


def test_sample_once_passes_iv(monkeypatch, tmp_path):
    captured = {}

    def _fake(inst, iv=1):
        captured["iv"] = iv
        return {"sampled": True}

    monkeypatch.setattr(mon, "monitor_sample", _fake)
    monkeypatch.setattr(cfg, "SAMPLE_IV", 7)
    monkeypatch.setattr(mon, "monitor_interval", lambda: 1)
    sampler.sample_once(_inst(tmp_path))
    assert captured["iv"] == 7


def test_runner_disabled_when_env_off(monkeypatch):
    monkeypatch.setattr(cfg, "EVENTS_BG", False)
    sampler.stop_runner()
    sampler.start_runner()
    assert sampler.runner_task() is None
