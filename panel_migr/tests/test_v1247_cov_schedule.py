"""v1.24.7 优化（方向 B）：补齐 `services/schedule.py` 的测试盲区。

该模块在优化前覆盖率仅 **54%**，且是「计划任务到底跑不跑」的唯一实现 ——
上一轮审计的 P1-22（cron 用 `?key=` 导致三条离线链路静默失效）与
C-P2-3（多任务串跑时先结束的永不判完）都落在这条链路上，却几乎没有测试。

本文件覆盖：
  1. `schedule_load` 的各种坏数据形态（缺失/空文件/非法 JSON/非 dict/脏任务项）
  2. `schedule_save` 原子写与失败路径
  3. `schedule_normalize_days` / `schedule_days_label` 的边界
  4. `schedule_validate` 的全部拒绝分支与规范化
  5. `schedule_due` 的星期过滤、时间窗、防重复 slot
  6. `schedule_run_due` 的四条主路径（未到点 / 容器未运行 / 有任务在跑 / 成功触发）
     与 conflict=stop 分支
  7. `config_scheduled_tasks_count` 的块扫描
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

import config as cfg
from services import schedule as S


# ===================== fixtures =====================

@pytest.fixture()
def inst(tmp_path, monkeypatch):
    """把 schedule.json 落到 tmp 目录（history_data_file 决定路径）。"""
    d = tmp_path / "data"
    d.mkdir()
    monkeypatch.setattr(S, "history_data_file",
                        lambda _i: d / "history_x.json")
    return {"id": "x", "container": "m7a"}


def _at(y, mo, d, h, mi):
    """构造本地时间戳（与 schedule_due 用同一时区）。"""
    return int(time.mktime((y, mo, d, h, mi, 0, 0, 0, -1)))


# ===================== schedule_load =====================

def test_load_missing_file_returns_default(inst):
    assert S.schedule_load(inst) == {"conflict": "skip", "tasks": []}


def test_load_empty_file_returns_default(inst):
    S.schedule_data_file(inst).write_text("   \n", "utf-8")
    assert S.schedule_load(inst) == {"conflict": "skip", "tasks": []}


def test_load_invalid_json_returns_default(inst):
    S.schedule_data_file(inst).write_text("{not json", "utf-8")
    assert S.schedule_load(inst) == {"conflict": "skip", "tasks": []}


def test_load_non_dict_json_returns_default(inst):
    S.schedule_data_file(inst).write_text("[1,2,3]", "utf-8")
    assert S.schedule_load(inst) == {"conflict": "skip", "tasks": []}


def test_load_skips_non_dict_task_items(inst):
    S.schedule_data_file(inst).write_text(
        json.dumps({"tasks": [1, "x", None, {"id": "a"}]}), "utf-8")
    d = S.schedule_load(inst)
    assert len(d["tasks"]) == 1
    assert d["tasks"][0]["id"] == "a"


def test_load_normalizes_every_field_to_str(inst):
    S.schedule_data_file(inst).write_text(json.dumps({"tasks": [{
        "id": 7, "name": None, "time": 4, "days": "1,2",
        "args": 99, "enabled": 1, "last_run": None, "last_result": None,
    }]}), "utf-8")
    t = S.schedule_load(inst)["tasks"][0]
    assert t["id"] == "7"
    assert t["name"] == ""
    assert t["time"] == "4"
    assert t["days"] == []          # 非 list → 空
    assert t["args"] == "99"
    assert t["enabled"] is True
    assert t["last_run"] == ""
    assert t["last_result"] == ""


def test_load_preserves_conflict_stop(inst):
    S.schedule_data_file(inst).write_text(
        json.dumps({"conflict": "stop", "tasks": []}), "utf-8")
    assert S.schedule_load(inst)["conflict"] == "stop"


def test_load_unknown_conflict_falls_back_to_skip(inst):
    S.schedule_data_file(inst).write_text(
        json.dumps({"conflict": "whatever", "tasks": []}), "utf-8")
    assert S.schedule_load(inst)["conflict"] == "skip"


def test_load_unreadable_file_returns_default(inst, monkeypatch):
    S.schedule_data_file(inst).write_text("{}", "utf-8")

    def boom(*_a, **_k):
        raise OSError("permission denied")

    monkeypatch.setattr(Path, "read_text", boom)
    assert S.schedule_load(inst) == {"conflict": "skip", "tasks": []}


# ===================== schedule_save =====================

def test_save_writes_json_and_reload_roundtrip(inst):
    data = {"conflict": "stop", "tasks": [{"id": "a", "name": "n"}]}
    assert S.schedule_save(inst, data) is True
    assert json.loads(S.schedule_data_file(inst).read_text("utf-8")) == data
    assert S.schedule_load(inst)["conflict"] == "stop"


def test_save_rejects_non_dict(inst):
    assert S.schedule_save(inst, ["not", "a", "dict"]) is False


def test_save_returns_false_on_oserror(inst, monkeypatch):
    def boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(Path, "write_text", boom)
    assert S.schedule_save(inst, {"tasks": []}) is False


def test_save_leaves_no_tmp_file(inst):
    S.schedule_save(inst, {"tasks": []})
    assert not list(S.schedule_data_file(inst).parent.glob("*.tmp"))


# ===================== days helpers =====================

@pytest.mark.parametrize("raw,expect", [
    ([], []),
    (None, []),
    ("1,2", []),                      # 非 list 一律空（"每天"）
    ([1, 2, 3], [1, 2, 3]),
    ([3, 1, 2], [1, 2, 3]),           # 升序
    ([1, 1, 1], [1]),                 # 去重
    ([0, 8, 9], []),                  # 越界丢弃
    (["1", " 2 ", "3"], [1, 2, 3]),   # 字符串数字
    (["abc"], []),
    ([1.9, 2.9], []),                 # int("1.9") 抛 ValueError → 丢弃（不截断）
])
def test_normalize_days(raw, expect):
    assert S.schedule_normalize_days(raw) == expect


@pytest.mark.parametrize("raw,expect", [
    ([], "每天"),
    (None, "每天"),
    ([1], "周一"),
    ([1, 3, 5], "周一、周三、周五"),
    ([7], "周日"),
    ([1, 2, 3, 4, 5, 6, 7], "周一、周二、周三、周四、周五、周六、周日"),
])
def test_days_label(raw, expect):
    assert S.schedule_days_label(raw) == expect


# ===================== schedule_validate =====================

def test_validate_ok_normalizes_time_and_days():
    err, d = S.schedule_validate({
        "name": "  日常  ", "time": "4:05", "days": ["3", "1", "3"],
        "args": next(iter(cfg.TASKS)),
    })
    assert err == ""
    assert d["name"] == "日常"
    assert d["time"] == "04:05"
    assert d["days"] == [1, 3]


@pytest.mark.parametrize("inp,needle", [
    ({}, "任务名称不能为空"),
    ({"name": "   "}, "任务名称不能为空"),
    ({"name": "x" * 41}, "任务名称过长"),
    ({"name": "n", "time": ""}, "时间格式不正确"),
    ({"name": "n", "time": "24:00"}, "时间格式不正确"),
    ({"name": "n", "time": "12:60"}, "时间格式不正确"),
    ({"name": "n", "time": "12"}, "时间格式不正确"),
])
def test_validate_rejects(inp, needle):
    err, d = S.schedule_validate(inp)
    assert needle in err
    assert d is None


def test_validate_rejects_bad_days():
    err, d = S.schedule_validate(
        {"name": "n", "time": "04:00", "days": [[1]], "args": "main"})
    assert "星期选择不合法" in err and d is None

    err, d = S.schedule_validate(
        {"name": "n", "time": "04:00", "days": ["9"], "args": "main"})
    assert "星期选择不合法" in err and d is None


def test_validate_rejects_empty_args():
    # days 是必填项（缺省会先报「星期选择不合法」），故显式给合法值以走到 args 校验
    err, d = S.schedule_validate(
        {"name": "n", "time": "04:00", "days": ["1"], "args": ""})
    assert "请选择要执行的任务" in err and d is None


def test_validate_rejects_unknown_args():
    err, d = S.schedule_validate(
        {"name": "n", "time": "04:00", "days": ["1"], "args": "no_such_task"})
    assert "不在可用任务列表中" in err and d is None


def test_validate_days_is_required():
    """days 缺省（None）时先报星期错误 —— args 校验在其后，属既有语义。"""
    err, d = S.schedule_validate({"name": "n", "time": "04:00", "args": "main"})
    assert "星期选择不合法" in err and d is None

    # 显式空数组 = 每天（与 schedule_normalize_days 一致），合法
    err, d = S.schedule_validate(
        {"name": "n", "time": "04:00", "days": [], "args": "main"})
    assert err == "" and d["days"] == []


def test_validate_non_dict_input():
    err, d = S.schedule_validate(None)  # type: ignore[arg-type]
    assert "任务名称不能为空" in err and d is None


# ===================== schedule_due =====================

def test_due_disabled_task_never_fires():
    ok, slot = S.schedule_due({"enabled": False, "time": "04:00"},
                              now=_at(2026, 10, 30, 4, 0))
    assert ok is False and slot == ""


def test_due_bad_time_format():
    ok, slot = S.schedule_due({"enabled": True, "time": "4点"},
                              now=_at(2026, 10, 30, 4, 0))
    assert ok is False


def test_due_wrong_weekday_does_not_fire():
    # 2026-10-30 是周五（tm_wday=4 → 我们的 5）
    now = _at(2026, 10, 30, 4, 0)
    assert time.localtime(now).tm_wday + 1 == 5
    ok, _ = S.schedule_due({"enabled": True, "time": "04:00", "days": [1]}, now=now)
    assert ok is False


def test_due_matching_weekday_fires():
    now = _at(2026, 10, 30, 4, 0)
    ok, slot = S.schedule_due(
        {"enabled": True, "time": "04:00", "days": [5]}, now=now)
    assert ok is True
    assert slot == "20261030-0400"


def test_due_empty_days_means_every_day():
    now = _at(2026, 10, 30, 4, 0)
    ok, _ = S.schedule_due({"enabled": True, "time": "04:00", "days": []}, now=now)
    assert ok is True


def test_due_before_target_time_does_not_fire():
    ok, _ = S.schedule_due({"enabled": True, "time": "04:00"},
                           now=_at(2026, 10, 30, 3, 59))
    assert ok is False


def test_due_beyond_window_does_not_fire():
    # SCHEDULE_WINDOW_SECONDS 默认 300s，5 分钟后就不再补跑
    now = _at(2026, 10, 30, 4, 0) + int(cfg.SCHEDULE_WINDOW_SECONDS) + 1
    ok, _ = S.schedule_due({"enabled": True, "time": "04:00"}, now=now)
    assert ok is False


def test_due_within_window_fires():
    now = _at(2026, 10, 30, 4, 0) + int(cfg.SCHEDULE_WINDOW_SECONDS) - 1
    ok, _ = S.schedule_due({"enabled": True, "time": "04:00"}, now=now)
    assert ok is True


def test_due_same_slot_already_run_does_not_fire_again():
    now = _at(2026, 10, 30, 4, 0)
    ok, _ = S.schedule_due(
        {"enabled": True, "time": "04:00", "last_run": "20261030-0400"}, now=now)
    assert ok is False


def test_due_non_dict_task():
    ok, slot = S.schedule_due("nope", now=_at(2026, 10, 30, 4, 0))  # type: ignore[arg-type]
    assert ok is False and slot == ""


# ===================== schedule_run_due =====================

def _task(**kw):
    t = {"id": "t1", "name": "测试任务", "time": "04:00", "days": [],
         "args": next(iter(cfg.TASKS)), "enabled": True,
         "last_run": "", "last_result": ""}
    t.update(kw)
    return t


def test_run_due_not_due_does_nothing(inst, monkeypatch):
    S.schedule_save(inst, {"conflict": "skip", "tasks": [_task(time="23:59")]})
    called = []
    monkeypatch.setattr(S, "container_is_running", lambda _i: called.append(1) or True)
    r = S.schedule_run_due(inst, now=_at(2026, 10, 30, 4, 0))
    assert r["ran"] == [] and r["skipped"] == []
    assert called == []


def test_run_due_container_not_running_skips(inst, monkeypatch):
    S.schedule_save(inst, {"conflict": "skip", "tasks": [_task()]})
    monkeypatch.setattr(S, "container_is_running", lambda _i: False)
    r = S.schedule_run_due(inst, now=_at(2026, 10, 30, 4, 0))
    assert r["ran"] == []
    assert r["skipped"] == ["测试任务"]
    saved = S.schedule_load(inst)["tasks"][0]
    assert saved["last_result"] == "跳过（容器未运行）"
    assert saved["last_run"] == "20261030-0400"     # 先标记避免每分钟重复判定


def test_run_due_busy_and_conflict_skip(inst, monkeypatch):
    S.schedule_save(inst, {"conflict": "skip", "tasks": [_task()]})
    monkeypatch.setattr(S, "container_is_running", lambda _i: True)
    monkeypatch.setattr(S, "_tasks_busy", lambda _i: True)
    started = []
    monkeypatch.setattr(S, "task_start", lambda _i, _a: started.append(_a) or {"code": 0, "out": ""})
    r = S.schedule_run_due(inst, now=_at(2026, 10, 30, 4, 0))
    assert r["ran"] == []
    assert r["skipped"] == ["测试任务"]
    assert started == []
    assert S.schedule_load(inst)["tasks"][0]["last_result"] == "跳过（有任务在跑）"


def test_run_due_busy_and_conflict_stop_restarts_then_fires(inst, monkeypatch):
    S.schedule_save(inst, {"conflict": "stop", "tasks": [_task()]})
    monkeypatch.setattr(S, "container_is_running", lambda _i: True)
    monkeypatch.setattr(S, "_tasks_busy", lambda _i: True)
    restarted = []
    monkeypatch.setattr(S, "compose",
                        lambda _i, _a: restarted.append(_a) or {"code": 0, "out": ""})
    fired = []
    monkeypatch.setattr(S, "task_start",
                        lambda _i, a: fired.append(a) or {"code": 0, "out": ""})
    monkeypatch.setattr(S, "history_add", lambda *_a, **_k: True)
    r = S.schedule_run_due(inst, now=_at(2026, 10, 30, 4, 0))
    assert restarted == ["restart"]
    assert r["ran"] == ["测试任务"]
    assert S.schedule_load(inst)["tasks"][0]["last_result"] == "已触发"


def test_run_due_conflict_stop_restart_failure(inst, monkeypatch):
    S.schedule_save(inst, {"conflict": "stop", "tasks": [_task()]})
    monkeypatch.setattr(S, "container_is_running", lambda _i: True)
    monkeypatch.setattr(S, "_tasks_busy", lambda _i: True)
    monkeypatch.setattr(S, "compose",
                        lambda _i, _a: {"code": 1, "out": "no such container"})
    r = S.schedule_run_due(inst, now=_at(2026, 10, 30, 4, 0))
    assert r["ran"] == []
    assert "停掉当前任务失败" in S.schedule_load(inst)["tasks"][0]["last_result"]


def test_run_due_fires_task(inst, monkeypatch):
    S.schedule_save(inst, {"conflict": "skip", "tasks": [_task()]})
    monkeypatch.setattr(S, "container_is_running", lambda _i: True)
    monkeypatch.setattr(S, "_tasks_busy", lambda _i: False)
    added = []
    monkeypatch.setattr(S, "task_start", lambda _i, _a: {"code": 0, "out": ""})
    monkeypatch.setattr(S, "history_add",
                        lambda i, a, label: added.append((a, label)) or True)
    r = S.schedule_run_due(inst, now=_at(2026, 10, 30, 4, 0))
    assert r["ran"] == ["测试任务"]
    assert added and added[0][0] == _task()["args"]
    assert S.schedule_load(inst)["tasks"][0]["last_result"] == "已触发"


def test_run_due_task_start_failure(inst, monkeypatch):
    S.schedule_save(inst, {"conflict": "skip", "tasks": [_task()]})
    monkeypatch.setattr(S, "container_is_running", lambda _i: True)
    monkeypatch.setattr(S, "_tasks_busy", lambda _i: False)
    monkeypatch.setattr(S, "task_start", lambda _i, _a: {"code": 1, "out": " docker down "})
    r = S.schedule_run_due(inst, now=_at(2026, 10, 30, 4, 0))
    assert r["ran"] == []
    assert S.schedule_load(inst)["tasks"][0]["last_result"] == "失败：docker down"


def test_run_due_two_due_tasks_second_sees_busy(inst, monkeypatch):
    """同轮两条都到点时，第一条触发后 busy=True，第二条走「有任务在跑」分支。"""
    S.schedule_save(inst, {"conflict": "skip",
                           "tasks": [_task(id="a", name="A"), _task(id="b", name="B")]})
    monkeypatch.setattr(S, "container_is_running", lambda _i: True)
    monkeypatch.setattr(S, "_tasks_busy", lambda _i: False)
    monkeypatch.setattr(S, "task_start", lambda _i, _a: {"code": 0, "out": ""})
    monkeypatch.setattr(S, "history_add", lambda *_a, **_k: True)
    r = S.schedule_run_due(inst, now=_at(2026, 10, 30, 4, 0))
    assert r["ran"] == ["A"]
    assert r["skipped"] == ["B"]


def test_run_due_returns_human_time(inst, monkeypatch):
    S.schedule_save(inst, {"conflict": "skip", "tasks": []})
    r = S.schedule_run_due(inst, now=_at(2026, 10, 30, 4, 0))
    assert r["ok"] is True
    assert r["time"] == "2026-10-30 04:00:00"


def test_run_due_no_due_task_does_not_save(inst, monkeypatch):
    S.schedule_save(inst, {"conflict": "skip", "tasks": [_task(time="23:59")]})
    saved = []
    monkeypatch.setattr(S, "schedule_save", lambda *a, **k: saved.append(1) or True)
    S.schedule_run_due(inst, now=_at(2026, 10, 30, 4, 0))
    assert saved == []


def test_run_due_nameless_task_falls_back_to_id(inst, monkeypatch):
    S.schedule_save(inst, {"conflict": "skip", "tasks": [_task(name="")]})
    monkeypatch.setattr(S, "container_is_running", lambda _i: False)
    r = S.schedule_run_due(inst, now=_at(2026, 10, 30, 4, 0))
    assert r["skipped"] == ["t1"]


# ===================== config_scheduled_tasks_count =====================

def test_count_zero_when_config_missing(inst, monkeypatch):
    monkeypatch.setattr("services.configops.config_read_raw", lambda *a, **k: "")
    assert S.config_scheduled_tasks_count(inst) == 0


def test_count_only_top_level_scheduled_tasks_block(inst, monkeypatch):
    raw = "\n".join([
        "after_finish: None",
        "scheduled_tasks:",
        "  - id: 1",
        "    time: '04:00'",
        "  - id: 2",
        "    time: '05:00'",
        "other_key: 1",
        "  - id: 3",            # 不在 scheduled_tasks 块内，不该计
    ])
    monkeypatch.setattr("services.configops.config_read_raw", lambda *a, **k: raw)
    assert S.config_scheduled_tasks_count(inst) == 2


def test_count_stops_at_next_top_level_key(inst, monkeypatch):
    raw = "\n".join([
        "scheduled_tasks:",
        "  - id: 1",
        "other:",
        "  - id: 2",
    ])
    monkeypatch.setattr("services.configops.config_read_raw", lambda *a, **k: raw)
    assert S.config_scheduled_tasks_count(inst) == 1


def test_count_handles_crlf(inst, monkeypatch):
    raw = "scheduled_tasks:\r\n  - id: 1\r\n  - id: 2\r\n"
    monkeypatch.setattr("services.configops.config_read_raw", lambda *a, **k: raw)
    assert S.config_scheduled_tasks_count(inst) == 2
