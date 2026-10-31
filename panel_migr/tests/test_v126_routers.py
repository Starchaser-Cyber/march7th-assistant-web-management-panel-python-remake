"""V1.26 新接口：?ajax=runs / run_log / tasks 与 action=stop_run。

这些接口是「运行账本」露给前端的唯一出口，所以既要测正常路径，也要测
参数校验（run_id 非法、lines 越界）和 handler 注册表的一致性。
"""
import json

import pytest

import config as cfg
import routers.readonly as RO
import routers.write as W
from services import runs


def _body(resp):
    """JSONResponse → dict。"""
    return json.loads(resp.body.decode("utf-8"))


@pytest.fixture()
def inst(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "DATA_DIR", tmp_path / "data")
    (tmp_path / "data").mkdir()
    d = tmp_path / "inst"
    d.mkdir()
    return {"id": "t1", "container": "c1", "dir": str(d)}


# ===================== handler 注册一致性 =====================

def test_get_handlers_include_v126():
    for k in ("runs", "run_log", "tasks"):
        assert k in RO.GET_HANDLERS


def test_post_handlers_include_stop_run():
    assert "stop_run" in W.POST_HANDLERS


# ===================== h_runs =====================

class _Req:
    def __init__(self, qp=None):
        self.query_params = qp or {}
        self.state = type("S", (), {"session": {}})()


def _patch_inst(monkeypatch, inst):
    monkeypatch.setattr(RO, "_inst", lambda _r: inst)


def test_h_runs_empty(inst, monkeypatch):
    _patch_inst(monkeypatch, inst)
    d = _body(RO.h_runs(_Req()))
    assert d["ok"] is True
    assert d["items"] == []


def test_h_runs_lists_ledger(inst, monkeypatch):
    _patch_inst(monkeypatch, inst)
    rid = "20261031-120000-abcd"
    runs.runs_dir(inst).mkdir(parents=True, exist_ok=True)
    runs.run_meta_path(inst, rid).write_text(
        '{"run_id":"%s","task_key":"daily","task_label":"每日实训","start_ts":100,"status":"running"}' % rid,
        "utf-8")
    monkeypatch.setattr(runs, "_pid_alive", lambda *_a: False)
    d = _body(RO.h_runs(_Req()))
    assert len(d["items"]) == 1
    assert d["items"][0]["run_id"] == rid


def test_h_runs_limit_clamped(inst, monkeypatch):
    _patch_inst(monkeypatch, inst)
    for bad, expect in (("0", 1), ("-5", 1), ("abc", 30), ("99999", 100)):
        d = _body(RO.h_runs(_Req({"limit": bad})))
        assert d["ok"] is True
        assert isinstance(d["items"], list)


# ===================== h_run_log =====================

def test_h_run_log_rejects_bad_run_id(inst, monkeypatch):
    _patch_inst(monkeypatch, inst)
    for bad in ("", "../../etc/passwd", "2026", "20261031-120000-ZZZZ"):
        d = _body(RO.h_run_log(_Req({"run_id": bad})))
        assert d["ok"] is False


def test_h_run_log_accepts_valid(inst, monkeypatch):
    _patch_inst(monkeypatch, inst)
    rid = "20261031-120000-abcd"
    runs.runs_dir(inst).mkdir(parents=True, exist_ok=True)
    runs.run_meta_path(inst, rid).write_text(
        '{"run_id":"%s","task_key":"daily","start_ts":100,"status":"running"}' % rid, "utf-8")
    runs.run_log_path(inst, rid).write_text("hello\n", "utf-8")
    monkeypatch.setattr(runs, "run_cmd", lambda *_a, **_k: {"code": 0, "out": "hello"})
    d = _body(RO.h_run_log(_Req({"run_id": rid})))
    assert d["ok"] is True
    assert d["run_id"] == rid


def test_h_run_log_lines_clamped(inst, monkeypatch):
    _patch_inst(monkeypatch, inst)
    rid = "20261031-120000-abcd"
    runs.runs_dir(inst).mkdir(parents=True, exist_ok=True)
    runs.run_meta_path(inst, rid).write_text('{"run_id":"%s","start_ts":1}' % rid, "utf-8")
    runs.run_log_path(inst, rid).write_text("x\n", "utf-8")
    seen = []

    def fake(cmd, timeout=120):
        seen.append(cmd)
        return {"code": 0, "out": ""}
    monkeypatch.setattr(runs, "run_cmd", fake)
    for bad in ("0", "-1", "abc", "99999"):
        d = _body(RO.h_run_log(_Req({"run_id": rid, "lines": bad})))
        assert d["ok"] is True
    assert seen, "应当调用过 run_cmd"


# ===================== h_tasks =====================

def test_h_tasks_shape(inst, monkeypatch):
    _patch_inst(monkeypatch, inst)
    one = [{"key": "main", "label": "完整运行", "group": "core",
            "group_label": "常用", "pausable": True}]
    monkeypatch.setattr(RO.tasklist, "grouped",
                        lambda _i, force=False: [{"key": "core", "label": "常用",
                                                  "items": one}])
    monkeypatch.setattr(RO.tasklist, "tasks", lambda _i, force=False: one)
    d = _body(RO.h_tasks(_Req()))
    assert d["ok"] is True
    assert d["count"] == 1
    assert d["groups"][0]["key"] == "core"


def test_h_tasks_empty_ok(inst, monkeypatch):
    _patch_inst(monkeypatch, inst)
    monkeypatch.setattr(RO.tasklist, "grouped", lambda _i, force=False: [])
    monkeypatch.setattr(RO.tasklist, "tasks", lambda _i, force=False: [])
    d = _body(RO.h_tasks(_Req()))
    assert d["ok"] is True and d["count"] == 0


# ===================== stop_run =====================

def test_h_stop_run_requires_run_id(inst, monkeypatch):
    monkeypatch.setattr(W, "instance_current", lambda _s: inst)
    kind, data = W.h_stop_run(_Req(), {"run_id": ""}, {}, "sid")
    assert kind == "flash" and data["err"]


def test_h_stop_run_rejects_bad_run_id(inst, monkeypatch):
    monkeypatch.setattr(W, "instance_current", lambda _s: inst)
    kind, data = W.h_stop_run(_Req(), {"run_id": "../../x"}, {}, "sid")
    assert kind == "flash" and data["err"]


def test_h_stop_run_reports_ledger_error(inst, monkeypatch):
    monkeypatch.setattr(W, "instance_current", lambda _s: inst)
    monkeypatch.setattr(W.runs, "stop_run", lambda _i, _r: {"err": "该运行已结束"})
    kind, data = W.h_stop_run(_Req(), {"run_id": "20261031-120000-abcd"}, {}, "sid")
    assert kind == "flash"
    assert "已结束" in data["err"]


def test_h_stop_run_success(inst, monkeypatch):
    monkeypatch.setattr(W, "instance_current", lambda _s: inst)
    monkeypatch.setattr(W.runs, "stop_run",
                        lambda _i, _r: {"ok": True, "msg": "已发送停止信号"})
    kind, data = W.h_stop_run(_Req(), {"run_id": "20261031-120000-abcd"}, {}, "sid")
    assert kind == "flash"
    assert "停止" in data["msg"]
