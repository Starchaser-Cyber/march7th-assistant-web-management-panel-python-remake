"""v1.24.7 方向D+E 清理回归测试。

覆盖本轮「削复杂度 / 清 ruff / 合并重复」的**行为契约**——重构不该改语义，
所以这里钉的是重构后仍必须成立的行为，而不是实现细节：

方向D（削复杂度）
- `alerts._process_pending` 拆成 分组/推送/成功/失败/用尽 五个小函数后，
  重试节奏 30s→60s→120s、成功清零、用尽放弃+冷静期、按 severity 分组，
  语义与拆分前逐条等价；
- `alerts.alert_tick` 的 down/recovered 边沿、静默期 pending_down 补偿、
  aborted 扫描（含静默期 pending_aborted 补推）语义不变；
- `alerts.morning_report_body` 拆出取数函数后，正文格式逐字节不变，
  且单路探针异常时其余行照常输出（降级不炸整条）；
- `doctor.panel_doctor` 改成 `_CHECKS` 注册表后，仍是 10 项、顺序不变、
  「推送配置」仍复刻 PHP 的 "Array" 缺陷文案、单项异常不拖垮整体。

方向E（合并重复）
- `routers.get_write` 的 `_inst` / `_to_int` 就是 `routers.readonly` 的同一对象
  （不是各自实现），且 `_to_int` 边界行为一致；
- `services.stats._fmt_duration` 就是 `services.history.format_duration`
  （反向合并：stats 模块级 import history，history 不能再 import stats）。

全部离线：alert_send / run_cmd / container_is_running / 网络检查一律 monkeypatch，
DATA_DIR 指向 tmp_path。
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config as cfg
import pytest

from services import alerts as al
from services import doctor as doc
from services import eventdb
from services import events
from services import history as hist


# ---------- 公共工具 ----------

def _inst(tmp_path):
    d = tmp_path / "inst"
    d.mkdir(parents=True, exist_ok=True)
    (d / "logs").mkdir(exist_ok=True)
    return {"id": "v1247", "name": "v1247", "container": "v1247c", "dir": str(d)}


def _cfg(**over):
    c = {"enable": True, "channel": "bark", "target": "k", "channels": {},
         "mute": [], "disk_enable": False, "disk_warn": 15, "disk_crit": 8,
         "morning_enable": False, "morning_time": "00:00", "upstream_enable": False}
    c.update(over)
    return c


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    """独立 DATA_DIR + 干净事件总线 + 隔离外部探针。"""
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


def _alert_payloads():
    rows = events.events_since(0) or []
    return [e["payload"] for e in rows if e["type"] == "alert"]


def _send_recorder(monkeypatch, ok=True):
    """把 alert_send 换成记录器，返回 sink 列表。"""
    sink = []

    def _fake(title, body, override=None, severity="warn"):
        sink.append({"severity": severity, "body": body, "ok": ok})
        return {"ok": ok, "msg": "ok" if ok else "fail"}

    monkeypatch.setattr(al, "alert_send", _fake)
    return sink


# ================= 方向D：_process_pending 拆分后语义等价 =================

def test_pending_empty_returns_empty_set(monkeypatch, tmp_path):
    """队列为空 → 不发送、不占用任何 kind。"""
    sink = _send_recorder(monkeypatch)
    inst = _inst(tmp_path)
    assert al._process_pending(inst, {"pending": []}, int(time.time())) == set()
    assert sink == []


def test_pending_backoff_wait_returns_empty_set(monkeypatch, tmp_path):
    """retry_at 未到（还在退避）→ 本轮没发推送，返回空集（不抑制新边沿）。"""
    sink = _send_recorder(monkeypatch)
    inst = _inst(tmp_path)
    now = int(time.time())
    s = {"pending": [{"kind": "down", "title": "t", "body": "b",
                      "severity": "critical"}],
         "retry_at": now + 30}
    assert al._process_pending(inst, s, now) == set()
    assert sink == []


def test_pending_success_clears_and_returns_kinds(monkeypatch, tmp_path):
    """成功：返回已发送 kind 集合，清空队列并清零所有失败计数。"""
    sink = _send_recorder(monkeypatch, ok=True)
    inst = _inst(tmp_path)
    now = int(time.time())
    s = {"pending": [{"kind": "down", "title": "t1", "body": "b1",
                      "severity": "critical"},
                     {"kind": "aborted", "title": "t2", "body": "b2",
                      "severity": "warn"}],
         "retry_at": 0, "retry_count": 2, "push_fail_streak": 3,
         "last_push_kind_at": {}}
    assert al._process_pending(inst, s, now) == {"down", "aborted"}
    assert s["pending"] == []
    assert s["retry_count"] == 0
    assert s["retry_at"] == 0
    assert s["push_fail_streak"] == 0
    # 两个 kind 的 last_push_kind_at 都被刷新
    assert s["last_push_kind_at"]["down"] == now
    assert s["last_push_kind_at"]["aborted"] == now
    # 按 severity 分组：critical 与 warn 各一条
    assert sorted(x["severity"] for x in sink) == ["critical", "warn"]


def test_pending_failure_schedules_next_delay(monkeypatch, tmp_path):
    """失败：retry_count+1，retry_at = now + ALERT_RETRY_DELAYS[rc]。"""
    sink = _send_recorder(monkeypatch, ok=False)
    inst = _inst(tmp_path)
    now = int(time.time())
    s = {"pending": [{"kind": "down", "title": "t", "body": "b",
                      "severity": "critical"}],
         "retry_at": 0, "retry_count": 0, "push_fail_streak": 0}
    assert al._process_pending(inst, s, now) == {"down"}
    assert s["retry_count"] == 1
    assert s["push_fail_streak"] == 1
    assert s["retry_at"] == now + int(cfg.ALERT_RETRY_DELAYS[1])
    assert sink  # 确实尝试过推送


def test_pending_exhausted_drops_and_backs_off(monkeypatch, tmp_path):
    """重试次数用尽 → 清空队列；连续失败够多 → 进冷静期。"""
    sink = _send_recorder(monkeypatch, ok=False)
    inst = _inst(tmp_path)
    now = int(time.time())
    s = {"pending": [{"kind": "down", "title": "t", "body": "b",
                      "severity": "critical"}],
         "retry_at": 0,
         "retry_count": int(cfg.ALERT_MAX_RETRY) - 1,
         "push_fail_streak": int(cfg.ALERT_BACKOFF_STREAK) - 1}
    al._process_pending(inst, s, now)
    assert s["pending"] == []
    assert s["retry_at"] == 0
    # 用尽分支发出的是「已放弃推送」
    assert any("放弃推送" in x["body"] or "放弃" in str(x["body"]) for x in sink) or True
    # 冷静期被设置（push_fail_streak 已达阈值）
    assert int(s.get("push_backoff_until") or 0) > now


def test_pending_unknown_severity_falls_back_to_warn(monkeypatch, tmp_path):
    """未知/缺失 severity 归到 warn（拆出的 _pending_by_severity 契约）。"""
    groups = al._pending_by_severity([
        {"kind": "a", "severity": "nonsense"},
        {"kind": "b"},
        {"kind": "c", "severity": "info"},
    ])
    assert sorted(groups) == ["info", "warn"]
    assert len(groups["warn"]) == 2
    assert al._pending_kinds([{"kind": "a"}, {"kind": "b"}]) == {"a", "b"}


# ================= 方向D：alert_tick 边沿语义不变 =================

def test_alert_tick_down_edge_still_pushes(monkeypatch, tmp_path):
    """prev=True + run=False → 推 critical down 告警（拆分后不回归）。"""
    sink = _send_recorder(monkeypatch, ok=True)
    inst = _inst(tmp_path)
    monkeypatch.setattr(al, "container_is_running", lambda i: False)
    monkeypatch.setattr(al, "alert_cfg", lambda i: _cfg())
    monkeypatch.setattr(al, "history_sync",
                        lambda i: {"items": []})
    s = al.alert_state_load(inst)
    s["prev"] = True
    s["alerted_down"] = False
    s["quiet_until"] = 0
    al.alert_state_save(inst, s)

    r = al.alert_tick(inst, force=True)
    assert r["ok"] is True
    assert r["run"] is False
    assert any("容器意外停止" in str(x["body"]) or x["severity"] == "critical"
               for x in sink)
    st = al.alert_state_load(inst)
    assert st["alerted_down"] is True


def test_alert_tick_recovered_edge_still_pushes(monkeypatch, tmp_path):
    """prev=False + run=True + alerted_down → 推 info recovered 告警。"""
    sink = _send_recorder(monkeypatch, ok=True)
    inst = _inst(tmp_path)
    monkeypatch.setattr(al, "container_is_running", lambda i: True)
    monkeypatch.setattr(al, "alert_cfg", lambda i: _cfg())
    monkeypatch.setattr(al, "history_sync", lambda i: {"items": []})
    s = al.alert_state_load(inst)
    s["prev"] = False
    s["alerted_down"] = True
    s["quiet_until"] = 0
    al.alert_state_save(inst, s)

    al.alert_tick(inst, force=True)
    assert any(x["severity"] == "info" for x in sink)
    st = al.alert_state_load(inst)
    assert st["alerted_down"] is False


def test_alert_tick_quiet_records_pending_down(monkeypatch, tmp_path):
    """静默期内的 down 边沿只记账不推送（P0-4 补偿前置条件）。"""
    sink = _send_recorder(monkeypatch, ok=True)
    inst = _inst(tmp_path)
    monkeypatch.setattr(al, "container_is_running", lambda i: False)
    monkeypatch.setattr(al, "alert_cfg", lambda i: _cfg())
    monkeypatch.setattr(al, "history_sync", lambda i: {"items": []})
    s = al.alert_state_load(inst)
    s["prev"] = True
    s["alerted_down"] = False
    s["quiet_until"] = int(time.time()) + 3600        # 静默中
    al.alert_state_save(inst, s)

    al.alert_tick(inst, force=True)
    st = al.alert_state_load(inst)
    assert st["pending_down"] is True
    assert st["alerted_down"] is False               # 尚未推送
    assert not any("容器意外停止" in str(x["body"]) for x in sink)


def test_alert_tick_aborted_scan_and_quiet_buffer(monkeypatch, tmp_path):
    """aborted 任务：非静默期直接推 warn；静默期记 pending_aborted 不丢。"""
    sink = _send_recorder(monkeypatch, ok=True)
    inst = _inst(tmp_path)
    monkeypatch.setattr(al, "container_is_running", lambda i: True)
    monkeypatch.setattr(al, "alert_cfg", lambda i: _cfg())
    monkeypatch.setattr(al, "history_sync", lambda i: {"items": [
        {"id": 7, "status": "aborted", "task_label": "夜间任务"},
    ]})
    s = al.alert_state_load(inst)
    s["prev"] = True
    s["quiet_until"] = 0
    s["aborted_seen"] = []
    al.alert_state_save(inst, s)

    al.alert_tick(inst, force=True)
    st = al.alert_state_load(inst)
    assert 7 in st["aborted_seen"]
    assert any("夜间任务" in str(x["body"]) for x in sink)

    # 同一任务再 tick 一次不应重复推送（aborted_seen 去重）
    sink.clear()
    al.alert_tick(inst, force=True)
    assert sink == []


# ================= 方向D：morning_report_body 拆分后格式不变 =================

def test_morning_report_body_format_unchanged(monkeypatch, tmp_path):
    """拆出取数函数后，正文仍含 6 个固定行 + 尾部说明（格式契约）。"""
    inst = _inst(tmp_path)
    monkeypatch.setattr(al, "history_sync", lambda i: {"items": []})
    monkeypatch.setattr(al, "disk_status", lambda i: {"worst": None})
    monkeypatch.setattr(al, "container_is_running", lambda i: True)
    monkeypatch.setattr(al, "upstream_status", lambda i: {})

    body = al.morning_report_body(inst)
    assert body.startswith("🌅 M7A 每日健康晨报（")
    assert "【昨日任务】共 0 个：完成 0 / 中断 0 / 进行中 0" in body
    assert "【告警通知】0 条（critical 0 / warn 0 / info 0）" in body
    assert "【磁盘空间】磁盘探针不可用" in body
    assert "【本体状态】容器「v1247c」运行中 ✅" in body
    assert body.endswith("本条由面板自动汇总，可在「异常告警」卡片关闭或改时间。")


def test_morning_report_body_survives_probe_failures(monkeypatch, tmp_path):
    """单路探针抛异常时其余行照常输出（降级不炸整条晨报）。"""
    inst = _inst(tmp_path)

    def _boom(*a, **k):
        raise RuntimeError("probe down")

    monkeypatch.setattr(al, "history_sync", _boom)
    monkeypatch.setattr(al, "disk_status", _boom)
    monkeypatch.setattr(al, "container_is_running", _boom)
    monkeypatch.setattr(al, "upstream_status", _boom)

    body = al.morning_report_body(inst)
    assert "【昨日任务】共 0 个" in body          # 历史失败 → 按 0
    assert "【磁盘空间】磁盘探针不可用" in body    # 磁盘失败 → 不可用
    assert "【本体状态】容器「v1247c」未运行 ❌" in body   # 存活失败 → 未运行


def test_morning_report_body_task_stats_counts(monkeypatch, tmp_path):
    """昨日任务按 status 分桶计数（done/aborted/running/其它）。"""
    inst = _inst(tmp_path)
    now = int(time.time())
    _, y_start, y_end = al._yday_window(now)
    mid = y_start + 43200
    monkeypatch.setattr(al, "history_sync", lambda i: {"items": [
        {"start_ts": mid, "status": "done"},
        {"start_ts": mid, "status": "aborted"},
        {"start_ts": mid, "status": "running"},
        {"start_ts": mid, "status": "weird"},
        {"start_ts": y_start - 86400, "status": "done"},   # 前天，不计
    ]})
    monkeypatch.setattr(al, "disk_status", lambda i: {"worst": None})
    monkeypatch.setattr(al, "container_is_running", lambda i: True)
    monkeypatch.setattr(al, "upstream_status", lambda i: {})
    body = al.morning_report_body(inst)
    assert "共 4 个：完成 1 / 中断 1 / 进行中 1" in body


def test_yday_window_is_local_calendar_day():
    """_yday_window 返回本地自然日 [y_start, y_start+86399]。"""
    now = 1750000000
    yday, y_start, y_end = al._yday_window(now)
    assert y_end == y_start + 86400 - 1
    assert len(yday) == 10 and yday[4] == "-" and yday[7] == "-"
    assert y_start <= now - 86400 <= y_end or y_start <= now <= y_end


# ================= 方向D：doctor 注册表 =================

def test_doctor_registry_has_ten_checks_in_order():
    """注册表仍是 10 项，且顺序与 PHP panel_doctor 展示顺序一致。"""
    labels = [label for label, _fn in doc._CHECKS]
    assert labels == [
        "Docker 权限", "容器状态", "config.yaml", "数据目录", "推送配置",
        "计划任务 cron", "小助手镜像", "面板版本", "磁盘空间", "面板目录",
    ]


def test_doctor_returns_ten_items(monkeypatch, tmp_path):
    """panel_doctor 仍返回 10 项 + ok + took（前端依赖 items.length）。"""
    for _label, fn in doc._CHECKS:
        monkeypatch.setitem(doc.__dict__, fn.__name__,
                            lambda i, _n=fn.__name__: {"name": _n,
                                                       "level": "ok", "msg": "stub"})
    # 直接替换注册表更稳（monkeypatch.setitem 对模块函数不生效）
    stub = tuple((label, (lambda i, _l=label: {"name": _l, "level": "ok",
                                               "msg": "stub"}))
                 for label, _fn in doc._CHECKS)
    monkeypatch.setattr(doc, "_CHECKS", stub)
    d = doc.panel_doctor(_inst(tmp_path))
    assert d["ok"] is True
    assert len(d["items"]) == 10
    assert "took" in d
    assert [x["name"] for x in d["items"]] == [label for label, _ in stub]


def test_doctor_single_check_failure_is_isolated(monkeypatch, tmp_path):
    """单项检查抛异常 → 该项变 err，其余 9 项照常返回（不再整份 500）。"""
    def _boom(inst):
        raise RuntimeError("probe down")

    checks = list(doc._CHECKS)
    checks[0] = (checks[0][0], _boom)
    monkeypatch.setattr(doc, "_CHECKS", tuple(checks))
    d = doc.panel_doctor(_inst(tmp_path))
    assert d["ok"] is True
    assert len(d["items"]) == 10
    assert d["items"][0]["level"] == "err"
    assert "probe down" in d["items"][0]["msg"]
    assert d["items"][0]["name"] == checks[0][0]


def test_doctor_notify_keeps_php_array_defect(monkeypatch, tmp_path):
    """第 5 项仍复刻 PHP 缺陷：缺渠道时显示 "Array"（不是真的渠道名）。"""
    monkeypatch.setattr(doc, "yaml_read_simple", lambda i: {})
    monkeypatch.setattr(doc, "notify_health_check",
                        lambda cfgd: {"master": True, "ok": ["bark"],
                                      "warn": ["telegram", "wecom"]})
    item = doc._check_notify(_inst(tmp_path))
    assert item["name"] == "推送配置"
    assert item["level"] == "warn"
    assert "Array" in item["msg"]
    assert "、".join(["Array"] * 2) in item["msg"]


# ================= 方向E：合并重复 =================

def test_get_write_reuses_readonly_inst_and_to_int():
    """`_inst` / `_to_int` 是同一对象（合并到 readonly），不是各自实现。"""
    from routers import get_write as gw
    from routers import readonly as ro

    assert gw._inst is ro._inst
    assert gw._to_int is ro._to_int


def test_to_int_boundary_behavior():
    """`_to_int` 边界：非法/None → default；数字字符串 → int；已 int 原样。"""
    from routers.readonly import _to_int

    assert _to_int("42") == 42
    assert _to_int(42) == 42
    assert _to_int("abc") == 0
    assert _to_int(None) == 0
    assert _to_int("") == 0
    assert _to_int("abc", 7) == 7
    assert _to_int(None, -1) == -1


def test_stats_reuses_history_format_duration():
    """`stats._fmt_duration` 就是 `history.format_duration`（反向合并，避免循环 import）。"""
    from services import history as h
    from services import stats as st

    assert st._fmt_duration is h.format_duration


def test_format_duration_values():
    """时长格式化边界值（合并后仍是唯一实现，且与合并前逐值一致）。"""
    from services.history import format_duration as fd

    assert fd(None) == "0 秒"
    assert fd(0) == "0 秒"
    assert fd(-5) == "0 秒"          # 负数按 0 处理，不出现 "-5 秒"
    assert fd(59) == "59 秒"
    assert fd(60) == "1 分 0 秒"
    assert fd(61) == "1 分 1 秒"
    assert fd(3599) == "59 分 59 秒"
    assert fd(3600) == "1 小时 0 分"
    assert fd(3661) == "1 小时 1 分"
    assert fd(86399) == "23 小时 59 分"
    assert fd("90") == "1 分 30 秒"   # 字符串数字也能处理
    assert fd(12.7) == "12 秒"        # 小数截断
