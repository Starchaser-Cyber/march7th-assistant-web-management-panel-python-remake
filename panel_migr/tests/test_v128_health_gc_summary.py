"""V1.28 测试：告警通道健康看板 + 会话 GC + 日志错误聚合。

覆盖：
- alert_health：off / setup / crit（停推、从未成功、持续失败）/ warn / ok 六个分支，
  以及 alert_state 里四个 push_last_* 字段的落盘与 _STR_FIELDS 清洗
- session_gc：新鲜保留 / 超龄删除 / 非 sess_ 前缀跳过 / 符号链接跳过 /
  非法 sid 跳过 / 目录不可读返回零值 / session_is_expired 边界
- error_summary：空输入 / 无错误行 / 分类分布 / rate 计算 / 首末时间 / top
- h_log 返回体带 summary 字段（既有 ok/files/lines 不回归）
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

import config as cfg
import phpsess
from services import alerts, logs

_REPO = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """alert_state 落在 cfg.DATA_DIR（history_data_file 的父目录），不是 inst["dir"]，
    必须按用例隔离，否则跨用例串状态。"""
    from services import events, eventdb
    from services import history as _hist
    from services import monitor as _mon

    events.reset_state()
    data = tmp_path / "data"
    data.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(cfg, "DATA_DIR", data)
    monkeypatch.setattr(_hist, "DATA_DIR", data)
    monkeypatch.setattr(_mon, "DATA_DIR", data)
    eventdb.reset()
    yield
    events.reset_state()
    eventdb.reset()


def _inst(tmp_path):
    d = tmp_path / "inst"
    (d / "logs").mkdir(parents=True, exist_ok=True)
    return {"id": "m3", "name": "M3面板", "container": "m3c", "dir": str(d)}


def _pcfg(**over):
    v = {"alert_enable": "1", "alert_channel": "bark", "alert_target": "test-key"}
    v.update(over)
    return v


def _health(monkeypatch, inst, pcfg, state=None):
    monkeypatch.setattr(alerts, "panel_config", lambda *a, **k: pcfg)
    if state is not None:
        alerts.alert_state_save(inst, state)
    return alerts.alert_health(inst)


# ===== alert_health =====

def test_health_off_when_disabled(monkeypatch, tmp_path):
    inst = _inst(tmp_path)
    h = _health(monkeypatch, inst, _pcfg(alert_enable="0"))
    assert h["enabled"] is False
    assert h["level"] == "off"
    assert h["color"] == "gray"


def test_health_setup_when_no_target(monkeypatch, tmp_path):
    inst = _inst(tmp_path)
    h = _health(monkeypatch, inst, _pcfg(alert_target=""))
    assert h["enabled"] is True
    assert h["has_target"] is False
    assert h["level"] == "setup"
    assert "接收地址" in h["label"]


def test_health_crit_when_backoff(monkeypatch, tmp_path):
    inst = _inst(tmp_path)
    now = int(time.time())
    h = _health(monkeypatch, inst, _pcfg(), state={"push_backoff_until": now + 300})
    assert h["level"] == "crit"
    assert h["color"] == "red"


def test_health_crit_when_never_succeeded(monkeypatch, tmp_path):
    inst = _inst(tmp_path)
    now = int(time.time())
    h = _health(monkeypatch, inst, _pcfg(),
                state={"push_last_fail_at": now - 10, "push_last_ok_at": 0,
                       "push_last_fail_msg": "Bark 返回 HTTP 400"})
    assert h["level"] == "crit"
    assert "从未" in h["label"]
    assert "HTTP 400" in h["detail"]


def test_health_warn_when_recent_fail(monkeypatch, tmp_path):
    inst = _inst(tmp_path)
    now = int(time.time())
    h = _health(monkeypatch, inst, _pcfg(),
                state={"push_last_fail_at": now - 10, "push_last_ok_at": now - 3600,
                       "push_fail_streak": 1})
    assert h["level"] == "warn"
    assert h["color"] == "orange"


def test_health_ok_when_last_ok_newer(monkeypatch, tmp_path):
    inst = _inst(tmp_path)
    now = int(time.time())
    h = _health(monkeypatch, inst, _pcfg(),
                state={"push_last_ok_at": now - 10, "push_last_fail_at": now - 3600})
    assert h["level"] == "ok"
    assert h["color"] == "green"


def test_health_channel_label_wecom(monkeypatch, tmp_path):
    inst = _inst(tmp_path)
    h = _health(monkeypatch, inst, _pcfg(alert_channel="wecom"))
    assert h["channel_label"] == "企业微信"


def test_alert_state_defaults_have_push_fields(tmp_path):
    inst = _inst(tmp_path)
    st = alerts.alert_state_load(inst)
    for k in ("push_last_ok_at", "push_last_fail_at", "push_last_fail_msg",
              "push_last_fail_kind"):
        assert k in st
    assert st["push_last_ok_at"] == 0
    assert st["push_last_fail_msg"] == ""


def test_alert_state_str_fields_cleaned(tmp_path):
    inst = _inst(tmp_path)
    alerts.alert_state_save(inst, {"push_last_fail_msg": 123,
                                   "push_last_fail_kind": None})
    st = alerts.alert_state_load(inst)
    assert isinstance(st["push_last_fail_msg"], str)
    assert isinstance(st["push_last_fail_kind"], str)


# ===== session_gc =====

def _mk(path, name, age_sec=0):
    p = os.path.join(path, name)
    with open(p, "w", encoding="utf-8") as f:
        f.write("m7a_panel_auth|b:1;")
    if age_sec:
        t = time.time() - age_sec
        os.utime(p, (t, t))
    return p


def test_session_gc_removes_expired(monkeypatch, tmp_path):
    d = str(tmp_path / "sess")
    os.makedirs(d)
    monkeypatch.setattr(phpsess, "SESSION_SAVE_PATH", d)
    fresh = _mk(d, "sess_" + "a" * 32)
    old = _mk(d, "sess_" + "b" * 32, age_sec=40 * 86400)
    r = phpsess.session_gc(ttl=30 * 86400)
    assert r["removed"] == 1
    assert r["kept"] == 1
    assert r["errors"] == 0
    assert os.path.exists(fresh) and not os.path.exists(old)


def test_session_gc_skips_non_sess_files(monkeypatch, tmp_path):
    d = str(tmp_path / "sess")
    os.makedirs(d)
    monkeypatch.setattr(phpsess, "SESSION_SAVE_PATH", d)
    _mk(d, "other.txt", age_sec=99 * 86400)
    r = phpsess.session_gc(ttl=30 * 86400)
    assert r["scanned"] == 0
    assert os.path.exists(os.path.join(d, "other.txt"))


def test_session_gc_skips_symlink(monkeypatch, tmp_path):
    d = str(tmp_path / "sess")
    os.makedirs(d)
    monkeypatch.setattr(phpsess, "SESSION_SAVE_PATH", d)
    link = os.path.join(d, "sess_" + "c" * 32)
    os.symlink("/etc/passwd", link)
    r = phpsess.session_gc(ttl=1)
    assert r["removed"] == 0
    assert os.path.islink(link)


def test_session_gc_skips_bad_sid(monkeypatch, tmp_path):
    d = str(tmp_path / "sess")
    os.makedirs(d)
    monkeypatch.setattr(phpsess, "SESSION_SAVE_PATH", d)
    # _SID_RE = [A-Za-z0-9,\-]{1,128} → 含空格/点的 sid 非法，必须跳过
    _mk(d, "sess_has space", age_sec=99 * 86400)
    _mk(d, "sess_has.dot", age_sec=99 * 86400)
    r = phpsess.session_gc(ttl=30 * 86400)
    assert r["scanned"] == 0
    assert r["removed"] == 0


def test_session_gc_missing_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(phpsess, "SESSION_SAVE_PATH", str(tmp_path / "nope"))
    r = phpsess.session_gc()
    assert r == {"scanned": 0, "removed": 0, "kept": 0, "errors": 0, "ttl": r["ttl"]}


def test_session_age_and_expired(monkeypatch, tmp_path):
    d = str(tmp_path / "sess")
    os.makedirs(d)
    monkeypatch.setattr(phpsess, "SESSION_SAVE_PATH", d)
    p = _mk(d, "sess_" + "d" * 32, age_sec=100)
    assert phpsess.session_age(p) >= 90
    assert phpsess.session_is_expired(p, ttl=50) is True
    assert phpsess.session_is_expired(p, ttl=1000) is False
    assert phpsess.session_age(str(tmp_path / "gone")) == -1.0
    # stat 失败不算过期（避免竞态下的删除污染统计）
    assert phpsess.session_is_expired(str(tmp_path / "gone")) is False


def test_session_ttl_config_default():
    assert int(cfg.SESSION_TTL) >= 3600
    assert int(cfg.SESSION_GC_IV) == 3600


# ===== error_summary =====

def _line(stamp, level, msg):
    return f"{stamp} | {level} | {msg}"


def test_error_summary_empty():
    s = logs.error_summary([])
    assert s["total"] == 0 and s["errors"] == 0
    assert s["rate"] == 0.0
    assert s["buckets"] == [] and s["top"] is None


def test_error_summary_no_errors():
    s = logs.error_summary([_line("2026-11-07 10:00:00", "INFO", "一切正常")])
    assert s["total"] == 1 and s["errors"] == 0
    assert s["buckets"] == []


def test_error_summary_buckets_and_rate():
    lines = [
        _line("2026-11-07 10:00:00", "ERROR", "截图失败：没有找到游戏窗口"),
        _line("2026-11-07 10:00:01", "ERROR", "截图失败：没有找到游戏窗口"),
        _line("2026-11-07 10:00:02", "INFO", "正常"),
        _line("2026-11-07 10:05:00", "CRITICAL", "端口 9222 无法绑定（Errno 98）"),
        _line("2026-11-07 10:06:00", "ERROR", "莫名其妙的问题"),
    ]
    s = logs.error_summary(lines)
    assert s["total"] == 5
    assert s["errors"] == 4          # 2×ERROR(截图) + 1×CRITICAL(端口) + 1×ERROR(其他)
    assert s["rate"] == 80.0
    top = s["top"]
    assert top["key"] == "screenshot" and top["count"] == 2
    assert s["first_ts"] > 0 and s["last_ts"] >= s["first_ts"]
    keys = [b["key"] for b in s["buckets"]]
    assert "screenshot" in keys and "network" in keys and "other" in keys
    # 每个 bucket 带可点击过滤的关键词；「其他错误」没有可匹配串 → kw 为空（前端渲染成不可点）
    by_key = {b["key"]: b for b in s["buckets"]}
    assert by_key["screenshot"]["kw"] == "截图"
    assert by_key["other"]["kw"] == ""


def test_error_summary_ignores_unparsable_timestamp():
    s = logs.error_summary(["not-a-timestamp | ERROR | 截图失败"])
    assert s["errors"] == 1
    assert s["first_ts"] == 0 and s["last_ts"] == 0


def test_classify_error_rules():
    assert logs.classify_error("发生错误 截图超时") == "screenshot"
    assert logs.classify_error("连接 websocket 失败") == "network"
    assert logs.classify_error("未知问题") == "other"


def test_classify_error_real_world_lines():
    """线上真实错误行（2026-11-07 日志）必须能归类，不能全落到「其他」。

    这两条是真实出现过的：进入余烬兑换失败 / 当前界面：未知。
    前者属于任务流程失败，后者属于界面识别失败。
    """
    assert logs.classify_error("2026-11-07 16:29:42,140 | ERROR | 进入余烬兑换失败") == "task"
    assert logs.classify_error("2026-11-07 16:29:55,771 | ERROR | 当前界面：未知") == "ui"
    # 顺序敏感：配置类关键词要先于泛化的「失败」
    assert logs.classify_error("2026-11-07 10:00:00 | ERROR | 配置加载失败") == "config"
    # 规则表里每个 bucket 的关键词都必须能在日志里真实命中（kw 为空只允许 other）
    for key, _label, kw, _needles in logs._ERROR_RULES:
        assert kw, f"bucket {key} 缺少可点击过滤关键词"


# ===== h_log 集成 =====

def test_h_log_returns_summary(monkeypatch, tmp_path):
    from routers import readonly
    inst = _inst(tmp_path)
    logp = os.path.join(inst["dir"], "logs", "2026-11-07.log")
    with open(logp, "w", encoding="utf-8") as f:
        f.write(_line("2026-11-07 10:00:00", "ERROR", "截图失败：没有找到游戏窗口") + "\n")
        f.write(_line("2026-11-07 10:00:01", "INFO", "正常") + "\n")
    with open(logp, encoding="utf-8") as f:
        rows = [line.rstrip("\n") for line in f]
    monkeypatch.setattr(readonly, "_inst", lambda req: inst)
    monkeypatch.setattr(readonly.lg, "filter_log",
                        lambda p, o: {"lines": rows, "total": len(rows)})
    resp = readonly.h_log(_FakeReq({}))
    d = json.loads(resp.body.decode())
    assert d["ok"] is True
    assert "summary" in d
    assert d["summary"]["total"] == 2
    assert d["summary"]["errors"] == 1


def test_h_log_no_file_summary_empty(monkeypatch, tmp_path):
    from routers import readonly
    inst = _inst(tmp_path)
    monkeypatch.setattr(readonly, "_inst", lambda req: inst)
    resp = readonly.h_log(_FakeReq({}))
    d = json.loads(resp.body.decode())
    assert d["files"] == []
    assert isinstance(d["lines"], str)
    assert d["summary"]["total"] == 0


class _FakeReq:
    def __init__(self, q):
        self.query_params = q


# ===== 前端静态断言 =====

def test_template_has_log_summary_and_health_bar():
    tpl = (_REPO / "templates" / "panel.html.j2").read_text(encoding="utf-8")
    assert 'id="logSummary"' in tpl
    assert "alertHealthBar" in tpl
    assert "ah-dot" in tpl
    assert "发送测试告警" in tpl


def test_js_has_summary_renderer():
    js = (_REPO / "static" / "panel.js").read_text(encoding="utf-8")
    assert "function renderLogSummary" in js
    assert "renderLogSummary(d.summary)" in js
    assert "_sumBucketClick" in js
    assert "_logSumKw" in js           # 填真实关键词，不是显示名
    assert "ls-plain" in js            # 无可匹配关键词的类别不可点


def test_css_has_summary_and_health_styles():
    css = (_REPO / "static" / "panel.css").read_text(encoding="utf-8")
    for sel in (".log-summary", ".ls-chip", ".ls-head", ".ahbar", ".ah-dot"):
        assert sel in css


def test_pagectx_exposes_alert_health():
    src = (_REPO / "services" / "pagectx.py").read_text(encoding="utf-8")
    assert "alertHealth" in src
    assert "def _alert_health" in src
