"""V1.28.2：容器 healthcheck 状态接入面板告警。

背景：面板原先只看 `State.Running`，于是「容器在跑但内部任务持续失败 /
内存快被打满」这类故障对用户完全不可见。2026-11-10 那台机器上容器
Running=True 挂了十几个小时，实际每轮任务都失败、云游戏时长被烧光，
面板一个告警都没发。

覆盖：
1. sysmetrics.container_info 解析 Health 字段（含 FailingStreak / OOMKilled）
2. monitor.container_health 快照字段
3. _observe_unhealthy_edge：unhealthy 边沿推送 + 恢复推送 + 静默期记账
4. run=False 时不管 unhealthy（归 down 边沿管，避免同一故障推两条）
5. 没有 healthcheck（health=''）时保持历史行为，不评估
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from services import alerts, monitor, sysmetrics  # noqa: E402

_INST = {"container": "m7a"}


# ---------- sysmetrics.container_info ----------

@pytest.fixture()
def fake_sock(monkeypatch):
    """把 Engine API 换成可编程的桩，返回 (setter, state)。"""
    state = {"payload": None}

    def _sock_json(path, timeout=3.0):
        if state["payload"] is None:
            return 404, None
        return 200, state["payload"]

    monkeypatch.setattr(sysmetrics, "sock_json", _sock_json)
    # 绕开 1 秒缓存，避免用例间串味
    monkeypatch.setattr(sysmetrics, "_INFO_CACHE", {})
    return state


def test_container_info_parses_health(fake_sock):
    fake_sock["payload"] = {
        "Id": "abc123",
        "State": {
            "Running": True,
            "StartedAt": "2026-11-11T12:00:00.000Z",
            "Health": {
                "Status": "unhealthy",
                "FailingStreak": 4,
                "Log": [
                    {"Start": "2026-11-11T12:05:00.000Z", "ExitCode": 0},
                    {"Start": "2026-11-11T12:10:00.000Z", "ExitCode": 1},
                ],
            },
            "OOMKilled": True,
        },
    }
    info = sysmetrics.container_info("m7a", refresh=True)
    assert info["running"] is True
    assert info["health"] == "unhealthy"
    assert info["health_fails"] == 4
    assert info["oom_killed"] is True
    assert info["health_since"] > 0          # 取最后一条 Log.Start


def test_container_info_without_healthcheck(fake_sock):
    """旧版 compose 没配 healthcheck：字段为空，不报错。"""
    fake_sock["payload"] = {"Id": "abc", "State": {"Running": True,
                                                  "StartedAt": "2026-11-11T12:00:00.000Z"}}
    info = sysmetrics.container_info("m7a", refresh=True)
    assert info["health"] == ""
    assert info["health_fails"] == 0
    assert info["oom_killed"] is False


def test_container_info_stopped(fake_sock):
    fake_sock["payload"] = {"Id": "abc", "State": {"Running": False,
                                                  "StartedAt": "2026-11-11T12:00:00.000Z"}}
    info = sysmetrics.container_info("m7a", refresh=True)
    assert info["running"] is False
    assert info["health"] == ""


# ---------- monitor.container_health ----------

def test_container_health_snapshot(monkeypatch):
    monkeypatch.setattr(monitor.sm, "container_info",
                        lambda name, refresh=False: {
                            "id": "x", "running": True, "started": 1,
                            "health": "unhealthy", "health_since": 99,
                            "health_fails": 3, "restarting": False,
                            "oom_killed": True,
                        })
    h = monitor.container_health(_INST)
    assert h == {"health": "unhealthy", "failing": 3, "since": 99,
                 "oom_killed": True, "restarting": False}


def test_container_health_cleared_when_stopped(monkeypatch):
    """容器已退出时 Docker 会留下最后一次探测结论（通常 unhealthy）。
    直接透传会让页面显示「容器活着但病了」，实际是「根本没在跑」= down 语义。
    这里必须归一成 ''，由 Running 单独表达。"""
    monkeypatch.setattr(monitor.sm, "container_info",
                        lambda name, refresh=False: {
                            "id": "x", "running": False, "started": 1,
                            "health": "unhealthy", "health_since": 99,
                            "health_fails": 7, "restarting": False,
                            "oom_killed": False,
                        })
    h = monitor.container_health(_INST)
    assert h["health"] == ""
    assert h["failing"] == 0


def test_container_health_missing_fields(monkeypatch):
    """容器信息字段缺失时全部兜底为安全默认值。"""
    monkeypatch.setattr(monitor.sm, "container_info", lambda name, refresh=False: {})
    h = monitor.container_health(_INST)
    assert h["health"] == ""
    assert h["failing"] == 0
    assert h["oom_killed"] is False


# ---------- alerts._observe_unhealthy_edge ----------

def _health(health="unhealthy", failing=3, oom=False, restarting=False):
    return {"health": health, "failing": failing, "since": 0,
            "oom_killed": oom, "restarting": restarting}


def test_unhealthy_edge_pushes(monkeypatch):
    monkeypatch.setattr(monitor, "container_health", lambda inst: _health())
    s: dict = {}
    ev: list = []
    alerts._observe_unhealthy_edge(_INST, s, True, False, ev)
    assert s["alerted_unhealthy"] is True
    assert s["prev_unhealthy"] is True
    assert len(ev) == 1
    kind, title, body, severity = ev[0]
    assert kind == "unhealthy"
    assert severity == "critical"
    assert "健康检查失败" in body
    assert "连续失败次数：3" in body


def test_unhealthy_edge_deduped(monkeypatch):
    """同一段 unhealthy 期内不重复推送。"""
    monkeypatch.setattr(monitor, "container_health", lambda inst: _health())
    s: dict = {}
    alerts._observe_unhealthy_edge(_INST, s, True, False, [])
    ev: list = []
    alerts._observe_unhealthy_edge(_INST, s, True, False, ev)
    assert ev == []


def test_unhealthy_recovers(monkeypatch):
    monkeypatch.setattr(monitor, "container_health", lambda inst: _health("healthy", 0))
    s = {"alerted_unhealthy": True, "prev_unhealthy": True}
    ev: list = []
    alerts._observe_unhealthy_edge(_INST, s, True, False, ev)
    assert s["alerted_unhealthy"] is False
    assert len(ev) == 1
    assert ev[0][0] == "unhealthy_recovered"
    assert ev[0][3] == "info"


def test_unhealthy_not_observed_when_stopped(monkeypatch):
    """容器没在跑：归 down 边沿管，这里不评估也不推 unhealthy。"""
    called = {"n": 0}

    def _boom(inst):
        called["n"] += 1
        return _health()

    monkeypatch.setattr(monitor, "container_health", _boom)
    s: dict = {}
    ev: list = []
    alerts._observe_unhealthy_edge(_INST, s, False, False, ev)
    assert ev == []
    assert called["n"] == 0
    assert "alerted_unhealthy" not in s


def test_unhealthy_ignored_without_healthcheck(monkeypatch):
    """health=''（旧 compose 没配探针）：保持历史行为，什么都不推。"""
    monkeypatch.setattr(monitor, "container_health", lambda inst: _health("", 0))
    s: dict = {}
    ev: list = []
    alerts._observe_unhealthy_edge(_INST, s, True, False, ev)
    assert ev == []
    assert "alerted_unhealthy" not in s


def test_unhealthy_quiet_period_defers(monkeypatch):
    """静默期：边沿先记账 pending，不立即推。"""
    monkeypatch.setattr(monitor, "container_health", lambda inst: _health())
    s: dict = {}
    ev: list = []
    alerts._observe_unhealthy_edge(_INST, s, True, True, ev)
    assert ev == []
    assert s["pending_unhealthy"] is True
    assert s.get("alerted_unhealthy") is None


def test_unhealthy_body_mentions_oom(monkeypatch):
    monkeypatch.setattr(monitor, "container_health",
                        lambda inst: _health(oom=True, restarting=True))
    s: dict = {}
    ev: list = []
    alerts._observe_unhealthy_edge(_INST, s, True, False, ev)
    body = ev[0][2]
    assert "OOM" in body
    assert "restarting" in body


def test_unhealthy_survives_health_exception(monkeypatch):
    """探测本身抛异常时不能把整个告警链路拖垮。"""
    def _boom(inst):
        raise RuntimeError("engine down")

    monkeypatch.setattr(monitor, "container_health", _boom)
    s: dict = {}
    ev: list = []
    alerts._observe_unhealthy_edge(_INST, s, True, False, ev)
    assert ev == []


# ---------- alert_health 暴露容器健康 ----------

def test_alert_health_exposes_container_state(monkeypatch):
    monkeypatch.setattr(alerts, "panel_config",
                        lambda: {"alert_enable": "0"})
    monkeypatch.setattr(monitor, "container_health",
                        lambda inst: _health(failing=5, oom=True))
    out = alerts.alert_health(_INST)
    assert out["enabled"] is False
    assert out["container_health"] == "unhealthy"
    assert out["container_fails"] == 5
    assert out["container_oom"] is True
