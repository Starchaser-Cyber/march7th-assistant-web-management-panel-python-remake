"""v1.24.5 告警状态机缺陷回归测试（Q-04 + Q-08）。

覆盖：
- Q-04 容器「从来没运行过」则永不告警
    * 首次观测即 down（prev is None, run is False）→ 必须告警
    * 首次观测即 up（prev is None, run is True）→ 不告警，只写 prev=True
    * 首次即 down 恰好落在静默期 → 记 pending_down，静默期结束后补推
    * 正常 up→down / down→up 边沿不受影响（防回归）
- Q-08 `alert_tick` 忽略 `_process_pending` 返回值导致重复推送
    * 本轮真的重试了 pending 的 down → 同轮新观测到的 down 边沿不再重复推送
    * 仍在退避等待（retry_at 未到）→ 本轮没发过推送，边沿照常推送（不误抑制）
    * pending 重试的 kind 与边沿 kind 不同 → 不抑制（只按 kind 抑制，不整轮跳过）

全部离线：alert_send / container_is_running / history_sync / health_tick 一律
monkeypatch，DATA_DIR 指向 tmp_path。
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config as cfg
import pytest

from services import alerts as al
from services import eventdb
from services import events
from services import history as hist


# ---------- 公共工具 ----------

def _inst(tmp_path):
    d = tmp_path / "inst"
    d.mkdir(parents=True, exist_ok=True)
    (d / "logs").mkdir(exist_ok=True)
    return {"id": "v1245", "name": "v1245", "container": "v1245c", "dir": str(d)}


def _cfg(**over):
    c = {"enable": True, "channel": "bark", "target": "k", "channels": {},
         "mute": [], "disk_enable": False, "disk_warn": 15, "disk_crit": 8,
         "morning_enable": False, "morning_time": "00:00", "upstream_enable": False}
    c.update(over)
    return c


def _recorder():
    """记录 alert_send 调用；返回 (sink, fake)。sink 元素为 (severity, body)。"""
    sink = []

    def _fake(title, body, override=None, severity="warn"):
        sink.append((severity, body))
        return {"ok": True, "msg": "ok"}

    return sink, _fake


def _alert_events():
    rows = events.events_since(0) or []
    return [e["payload"] for e in rows if e["type"] == "alert"]


def _kinds(kind):
    return [p for p in _alert_events() if p.get("kind") == kind]


def _set_quiet_expired(inst):
    s = al.alert_state_load(inst)
    s["quiet_until"] = int(time.time()) - 1
    assert al.alert_state_save(inst, s) is True


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    """独立 DATA_DIR + 干净事件总线；health_tick 默认隔离（不跑 df/网络）。"""
    from services import monitor as _mon

    events.reset_state()
    data = tmp_path / "data"
    data.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(cfg, "DATA_DIR", data)
    monkeypatch.setattr(hist, "DATA_DIR", data)
    monkeypatch.setattr(_mon, "DATA_DIR", data)
    monkeypatch.setattr(al, "health_tick", lambda i: {})
    eventdb.reset()
    yield
    events.reset_state()
    eventdb.reset()


def _arm(monkeypatch, sink_fake, running, items=None, **cfg_over):
    monkeypatch.setattr(al, "alert_cfg", lambda i: _cfg(**cfg_over))
    monkeypatch.setattr(al, "alert_send", sink_fake)
    monkeypatch.setattr(al, "history_sync", lambda i: {"items": list(items or [])})
    monkeypatch.setattr(al, "container_is_running", lambda i: running["v"])


# ================= Q-04：首次观测即 down 必须告警 =================

def test_q04_first_observation_down_alerts(monkeypatch, tmp_path):
    """面板首次接管时容器就没起来：prev=None + run=False 也要告警。"""
    inst = _inst(tmp_path)
    sink, fake = _recorder()
    running = {"v": False}
    _arm(monkeypatch, fake, running)

    assert al.alert_state_load(inst)["prev"] is None      # 前置：干净状态
    r = al.alert_tick(inst, force=True)

    assert r["pushed"] is True
    assert len(sink) == 1
    sev, body = sink[0]
    assert sev == "critical"                              # down 是 critical
    assert "容器意外停止" in body
    # 文案要说明这不是「从运行中掉下去」，而是首次检测
    assert "首次检测" in body

    st = al.alert_state_load(inst)
    assert st["prev"] is False
    assert st["alerted_down"] is True                     # 只告警一次，不每轮刷屏

    # 第二轮仍是 down：不应重复推送
    al.alert_tick(inst, force=True)
    assert len(sink) == 1

    downs = _kinds("down")
    assert len(downs) == 1 and downs[0]["pushed"] is True


def test_q04_first_observation_up_does_not_alert(monkeypatch, tmp_path):
    """首次观测即正常（prev=None + run=True）→ 不告警，只记录 prev=True。"""
    inst = _inst(tmp_path)
    sink, fake = _recorder()
    running = {"v": True}
    _arm(monkeypatch, fake, running)

    r = al.alert_tick(inst, force=True)

    assert r["pushed"] is False
    assert sink == []
    st = al.alert_state_load(inst)
    assert st["prev"] is True and st["alerted_down"] is False
    assert _alert_events() == []


def test_q04_first_down_during_quiet_is_backfilled(monkeypatch, tmp_path):
    """首次即 down 但正好在静默期：先记 pending_down，结束后补推（带首次文案）。"""
    inst = _inst(tmp_path)
    sink, fake = _recorder()
    running = {"v": False}
    _arm(monkeypatch, fake, running)
    al.alert_quiet(inst, 600)

    r1 = al.alert_tick(inst, force=True)
    assert r1["pushed"] is False and sink == []           # 静默期不推
    st = al.alert_state_load(inst)
    assert st["pending_down"] is True
    assert st["pending_down_first"] is True               # 记住「首次即 down」
    assert st["alerted_down"] is False

    _set_quiet_expired(inst)
    r2 = al.alert_tick(inst, force=True)

    assert r2["pushed"] is True
    assert len(sink) == 1
    body = sink[0][1]
    assert "容器意外停止" in body
    assert "首次检测" in body                              # 首次语义保留
    assert "静默期内" in body                              # 补发说明
    st = al.alert_state_load(inst)
    assert st["alerted_down"] is True
    assert st["pending_down"] is False and st["pending_down_first"] is False


def test_q04_normal_up_down_edge_unchanged(monkeypatch, tmp_path):
    """防回归：正常 up→down→up→down 边沿行为与 v1.24.4 一致（无「首次检测」字样）。"""
    inst = _inst(tmp_path)
    # 关掉同 kind 去重窗口：本用例验证的是边沿检测本身，不是去重（真实时钟
    # 下多轮 tick 都落在同一秒内，会被 ALERT_DEDUP_WINDOW=120 掩盖）。
    monkeypatch.setattr(cfg, "ALERT_DEDUP_WINDOW", 0)
    sink, fake = _recorder()
    running = {"v": True}
    _arm(monkeypatch, fake, running)

    al.alert_tick(inst, force=True)                       # prev None→True
    assert sink == []

    running["v"] = False
    al.alert_tick(inst, force=True)                       # True→False：down
    assert len(sink) == 1
    assert "首次检测" not in sink[0][1]                    # 正常掉线不带首次文案

    running["v"] = True
    al.alert_tick(inst, force=True)                       # False→True：recovered
    assert len(sink) == 2
    assert "已恢复" in sink[1][1]

    running["v"] = False
    al.alert_tick(inst, force=True)                       # 再次 down
    assert len(sink) == 3
    assert "首次检测" not in sink[2][1]


# ================= Q-08：pending 重试与边沿不重复推送 =================

def _seed_pending_down(inst, retry_at=0):
    s = al.alert_state_load(inst)
    s["pending"] = [{"kind": "down", "title": "容器意外停止",
                     "body": "PENDING-RETRY-DOWN", "severity": "critical"}]
    s["retry_at"] = int(retry_at)
    s["retry_count"] = 0
    assert al.alert_state_save(inst, s) is True


def test_q08_pending_retry_and_edge_same_tick_pushes_once(monkeypatch, tmp_path):
    """去重窗口为 0 时，同轮「重试 pending」+「新 down 边沿」只能推送一次。"""
    inst = _inst(tmp_path)
    monkeypatch.setattr(cfg, "ALERT_DEDUP_WINDOW", 0)     # 关键：去掉去重窗口的掩盖
    sink, fake = _recorder()
    running = {"v": False}
    _arm(monkeypatch, fake, running)

    # 预置：有 pending down 待重试（retry_at 已到），且容器刚掉线
    s = al.alert_state_load(inst)
    s["prev"] = True
    s["alerted_down"] = False
    s["pending"] = [{"kind": "down", "title": "容器意外停止",
                     "body": "PENDING-RETRY-DOWN", "severity": "critical"}]
    s["retry_at"] = 0
    assert al.alert_state_save(inst, s) is True

    al.alert_tick(inst, force=True)

    assert len(sink) == 1                                  # 只推了一次
    assert "PENDING-RETRY-DOWN" in sink[0][1]              # 是 pending 重试那条

    # 边沿仍被检测到（alerted_down 置位，不会下一轮再来一次），但事件标未推送
    st = al.alert_state_load(inst)
    assert st["alerted_down"] is True
    downs = _kinds("down")
    assert len(downs) == 2                                 # 重试事件 + 边沿事件
    assert any(d["pushed"] is True for d in downs)
    edge = [d for d in downs if d["pushed"] is False]
    assert edge and "重试队列" in str(edge[-1].get("msg") or "")


def test_q08_pending_waiting_backoff_does_not_suppress_edge(monkeypatch, tmp_path):
    """pending 还在退避等待（retry_at 未到）：本轮没发推送 → 边沿照常推送。"""
    inst = _inst(tmp_path)
    monkeypatch.setattr(cfg, "ALERT_DEDUP_WINDOW", 0)
    sink, fake = _recorder()
    running = {"v": False}
    _arm(monkeypatch, fake, running)

    s = al.alert_state_load(inst)
    s["prev"] = True
    s["alerted_down"] = False
    s["pending"] = [{"kind": "down", "title": "容器意外停止",
                     "body": "PENDING-RETRY-DOWN", "severity": "critical"}]
    s["retry_at"] = int(time.time()) + 600                # 远未到重试时间
    assert al.alert_state_save(inst, s) is True

    al.alert_tick(inst, force=True)

    assert len(sink) == 1
    assert "PENDING-RETRY-DOWN" not in sink[0][1]          # 推的是新边沿
    assert "容器意外停止" in sink[0][1]
    st = al.alert_state_load(inst)
    assert st["pending"]                                      # pending 仍在队列里
    assert st["alerted_down"] is True


def test_q08_different_kind_is_not_suppressed(monkeypatch, tmp_path):
    """pending 重试的是 aborted，同轮观测到 down 边沿 → 两者都要推（按 kind 抑制）。"""
    inst = _inst(tmp_path)
    monkeypatch.setattr(cfg, "ALERT_DEDUP_WINDOW", 0)
    sink, fake = _recorder()
    running = {"v": False}
    _arm(monkeypatch, fake, running)

    s = al.alert_state_load(inst)
    s["prev"] = True
    s["alerted_down"] = False
    s["pending"] = [{"kind": "aborted", "title": "任务中断",
                     "body": "PENDING-RETRY-ABORTED", "severity": "warn"}]
    s["retry_at"] = 0
    assert al.alert_state_save(inst, s) is True

    al.alert_tick(inst, force=True)

    bodies = [b for _, b in sink]
    assert len(bodies) == 2
    assert any("PENDING-RETRY-ABORTED" in b for b in bodies)
    assert any("容器意外停止" in b for b in bodies)


def test_q08_process_pending_returns_kind_set(monkeypatch, tmp_path):
    """_process_pending 的返回值语义：空集 / 已发送 kind 集合，而不是 bool。"""
    inst = _inst(tmp_path)
    sink, fake = _recorder()
    monkeypatch.setattr(al, "alert_send", fake)
    s = al.alert_state_load(inst)

    # 无 pending → 空集
    assert al._process_pending(inst, s, int(time.time())) == set()

    # 有 pending 但未到重试时间 → 空集（本轮没发推送）
    s["pending"] = [{"kind": "down", "title": "t", "body": "b",
                     "severity": "critical"}]
    s["retry_at"] = int(time.time()) + 600
    assert al._process_pending(inst, s, int(time.time())) == set()
    assert sink == []

    # 到期 → 返回实际发送过的 kind 集合
    s["retry_at"] = 0
    got = al._process_pending(inst, s, int(time.time()))
    assert got == {"down"}
    assert len(sink) == 1
