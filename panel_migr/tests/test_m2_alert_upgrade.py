"""v1.24（M2）告警升级测试：分级通道 / 按级别静音 / 磁盘空间预警 /
每日健康晨报 / 上游本体更新提醒。

全部离线：网络请求（http_get / http_post）与命令执行（run_cmd / df）
一律 monkeypatch，不触真实 GitHub、不真跑 df。
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config as cfg
import pytest

from services import alerts as al
from services import eventdb
from services import events


# ---------- 公共工具 ----------

def _inst(tmp_path):
    d = tmp_path / "inst"
    d.mkdir(parents=True, exist_ok=True)
    return {"id": "m2", "name": "M2面板", "container": "m2c", "dir": str(d)}


def _cfg(**over):
    """默认全开的告警配置，按需覆盖。"""
    c = {
        "enable": True, "channel": "bark", "target": "k",
        "channels": {}, "mute": [],
        "disk_enable": True, "disk_warn": 15, "disk_crit": 8,
        "morning_enable": True, "morning_time": "00:00",
        "upstream_enable": True,
    }
    c.update(over)
    return c


def _recorder():
    """记录 alert_send 的级别调用，返回 (sink, fake)。"""
    sink = []

    def _fake(title, body, override=None, severity="warn"):
        sink.append(severity)
        return {"ok": True, "msg": "ok"}

    return sink, _fake


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    """每个用例独立 DATA_DIR + 独立面板配置路径，避免跨用例串状态。"""
    from services import history as _hist
    from services import monitor as _mon

    events.reset_state()
    data = tmp_path / "data"
    data.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(cfg, "DATA_DIR", data)
    monkeypatch.setattr(_hist, "DATA_DIR", data)
    monkeypatch.setattr(_mon, "DATA_DIR", data)
    monkeypatch.setattr(cfg, "PANEL_CONFIG_FILE", tmp_path / "panel_config.php")
    eventdb.reset()
    yield
    events.reset_state()
    eventdb.reset()


def _alert_events():
    return [e["payload"] for e in events.events_since(0) if e["type"] == "alert"]


# ================= 1. 版本比较公开包装 =================

def test_version_gt_public_wrapper():
    from services.updateops import version_eq, version_gt

    assert version_gt("2026.10.3", "2026.9.30") is True
    assert version_gt("2026.9.30", "2026.10.3") is False
    assert version_gt("v2026.10.3", "2026.10.3") is False
    assert version_eq("2026.10.3", "2026.10.3") is True


def test_norm_ver_extracts_digits():
    assert al._norm_ver("v2026.10.3") == "2026.10.3"
    assert al._norm_ver("release 1.2.3-beta\n") == "1.2.3"
    assert al._norm_ver("没有版本号") == ""


# ================= 2. 分级通道 / 按级别静音配置解析 =================

def test_alert_cfg_level_channels_and_mute(monkeypatch):
    monkeypatch.setattr(al, "panel_config", lambda: {
        "alert_enable": "1",
        "alert_channel": "bark",
        "alert_target": "  key-abc  ",
        "alert_channel_critical": "serverchan",
        "alert_channel_info": "nonsense",          # 非法值 → 继承主通道
        "alert_mute_levels": "info,bogus",         # 只认合法级别
        "disk_warn_percent": "20",
        "disk_crit_percent": "10",
        "morning_report_enable": "0",
        "morning_report_time": "7:30",
        "upstream_check_enable": "0",
    })
    c = al.alert_cfg({})

    assert c["enable"] is True
    assert c["target"] == "key-abc"
    assert c["channels"] == {"info": "bark", "warn": "bark", "critical": "serverchan"}
    assert c["mute"] == ["info"]
    assert c["disk_enable"] is True
    assert c["disk_warn"] == 20 and c["disk_crit"] == 10
    assert c["morning_enable"] is False
    assert c["morning_time"] == "07:30"        # 补前导零
    assert c["upstream_enable"] is False


def test_alert_cfg_defaults_when_keys_missing(monkeypatch):
    monkeypatch.setattr(al, "panel_config", lambda: {})
    c = al.alert_cfg({})

    assert c["enable"] is False
    assert c["channels"] == {"info": "bark", "warn": "bark", "critical": "bark"}
    assert c["mute"] == []
    assert c["disk_enable"] is True
    assert c["disk_warn"] == int(cfg.DISK_WARN_PERCENT)
    assert c["disk_crit"] == int(cfg.DISK_CRIT_PERCENT)
    assert c["morning_enable"] is bool(cfg.MORNING_REPORT_ENABLE)
    assert c["morning_time"] == cfg.MORNING_REPORT_TIME
    assert c["upstream_enable"] is True


def test_alert_cfg_invalid_hhmm_falls_back(monkeypatch):
    monkeypatch.setattr(al, "panel_config", lambda: {"morning_report_time": "25:99"})
    assert al.alert_cfg({})["morning_time"] == al._cfg_hhmm({}, "morning_report_time",
                                                            str(cfg.MORNING_REPORT_TIME))


# ================= 3. alert_send 按级别路由 =================

def test_alert_send_routes_by_severity(monkeypatch):
    calls = []

    def _get(url, timeout=8):
        calls.append(url)
        return {"code": 200, "body": "ok"}

    def _post(url, fields=None, timeout=8, as_json=False):
        calls.append(url)
        return {"code": 200, "body": "ok"}

    monkeypatch.setattr(al, "http_get", _get)
    monkeypatch.setattr(al, "http_post", _post)

    ov = {
        "enable": True, "channel": "bark", "target": "K1",
        "channels": {"info": "webhook", "warn": "bark", "critical": "serverchan"},
    }

    al.alert_send("标题", "正文", ov, severity="critical")
    assert "sctapi.ftqq.com/K1.send" in calls[-1]

    # webhook 通道：v1.24.3 起目标必须是合法 http(s) 公网地址（M7A-08 加固）
    hook = dict(ov, target="https://hooks.example.com/notify")
    al.alert_send("标题", "正文", hook, severity="info")
    assert calls[-1] == "https://hooks.example.com/notify"

    al.alert_send("标题", "正文", ov, severity="warn")
    assert calls[-1].startswith("https://api.day.app/K1/")

    al.alert_send("标题", "正文", ov)             # 缺省 = warn，向后兼容
    assert calls[-1].startswith("https://api.day.app/K1/")


def test_webhook_rejects_internal_and_bad_scheme(monkeypatch):
    """v1.24.3（M7A-08）：webhook 目标拒绝内网/回环/保留地址与非 http(s) 协议。"""
    calls = []
    monkeypatch.setattr(al, "http_post",
                        lambda url, *a, **k: (calls.append(url), {"code": 200, "body": "ok"})[1])

    bad = ["http://127.0.0.1:8080/hook", "http://169.254.169.254/latest/meta-data",
           "http://10.0.0.5/notify", "http://localhost/hook", "file:///etc/passwd", ""]
    for target in bad:
        ov = {"enable": True, "channel": "webhook", "target": target}
        r = al.alert_send("标题", "正文", ov, severity="info")
        assert r["ok"] is False, f"应拒绝 {target!r}"
    assert calls == [], "被拒绝的目标不应发生任何外发请求"

    # 公网地址正常放行
    ok_ov = {"enable": True, "channel": "webhook", "target": "https://hooks.example.com/x"}
    r = al.alert_send("标题", "正文", ok_ov, severity="info")
    assert r["ok"] is True and calls[-1] == "https://hooks.example.com/x"


def test_alert_send_level_channel_falls_back_to_main(monkeypatch):
    calls = []
    monkeypatch.setattr(al, "http_get",
                        lambda url, timeout=8: (calls.append(url) or {"code": 200, "body": "ok"}))
    ov = {"enable": True, "channel": "bark", "target": "K2", "channels": {"critical": "bark"}}
    al.alert_send("T", "B", ov, severity="info")   # info 未配置 → 继承主通道 bark
    assert calls[-1].startswith("https://api.day.app/K2/")


# ================= 4. 静音：只记录不推送 =================

def test_muted_level_records_event_without_pushing(monkeypatch, tmp_path):
    inst = _inst(tmp_path)
    monkeypatch.setattr(al, "alert_cfg", lambda i: _cfg(mute=["warn"]))
    monkeypatch.setattr(al, "history_sync", lambda i: {"items": []})
    monkeypatch.setattr(al, "health_tick", lambda i: {})
    sink, fake = _recorder()
    monkeypatch.setattr(al, "alert_send", fake)

    alive = {"v": True}
    monkeypatch.setattr(al, "container_is_running", lambda i: alive["v"])

    al.alert_tick(inst, force=True)          # prev None → True
    alive["v"] = False
    al.alert_tick(inst, force=True)          # True → False：down = critical

    assert sink[-1] == "critical"            # critical 未被静音 → 推送
    downs = [p for p in _alert_events() if p.get("kind") == "down"]
    assert downs and downs[-1]["severity"] == "critical"
    assert downs[-1]["pushed"] is True


def test_muted_critical_is_recorded_but_not_pushed(monkeypatch, tmp_path):
    inst = _inst(tmp_path)
    monkeypatch.setattr(al, "alert_cfg", lambda i: _cfg(mute=["critical", "warn"]))
    monkeypatch.setattr(al, "history_sync", lambda i: {"items": []})
    monkeypatch.setattr(al, "health_tick", lambda i: {})
    sink, fake = _recorder()
    monkeypatch.setattr(al, "alert_send", fake)

    alive = {"v": True}
    monkeypatch.setattr(al, "container_is_running", lambda i: alive["v"])

    al.alert_tick(inst, force=True)
    alive["v"] = False
    al.alert_tick(inst, force=True)

    assert sink == []                        # 被静音 → 一条都不推
    downs = [p for p in _alert_events() if p.get("kind") == "down"]
    assert downs and downs[-1]["pushed"] is False
    assert "静音" in downs[-1]["msg"]


# ================= 5. 磁盘探针 =================

def test_disk_probe_parses_gnu_df(monkeypatch):
    from services import monitor as mon

    out = ("文件系统          1B-块        已用        可用 已用% 挂载点\n"
           "/dev/sdb1 116438282240 86167027712 24309264384   78% /\n")

    def _run(cmd, timeout=120):
        assert cmd.startswith("df -B1 ")
        return {"code": 0, "out": out}

    monkeypatch.setattr(mon, "run_cmd", _run)
    r = mon.disk_probe("/")

    assert r["ok"] is True
    assert r["path"] == "/"
    assert r["total"] == 116438282240
    assert r["avail"] == 24309264384
    assert r["percent"] == 20                # 剩余百分比（向下取整）


def test_disk_probe_degrades_on_failure(monkeypatch):
    from services import monitor as mon

    monkeypatch.setattr(mon, "run_cmd", lambda cmd, timeout=120: {"code": 1, "out": "df: bad"})
    r = mon.disk_probe("/nope")
    assert r["ok"] is False and r["percent"] == 0

    monkeypatch.setattr(mon, "run_cmd", lambda cmd, timeout=120: {"code": 0, "out": "只有一列"})
    assert mon.disk_probe("/nope")["ok"] is False


# ================= 6. 磁盘预警级别跃迁 =================

def test_disk_check_warn_critical_recovery(monkeypatch, tmp_path):
    inst = _inst(tmp_path)
    holder = {"pct": 30}

    def _status(i):
        p = holder["pct"]
        return {
            "ok": True, "probes": [],
            "worst": {"ok": True, "path": "/", "total": 100, "used": 100 - p,
                      "avail": p, "percent": p},
        }

    monkeypatch.setattr(al, "disk_status", _status)
    monkeypatch.setattr(al, "alert_cfg", lambda i: _cfg())
    sink, fake = _recorder()
    monkeypatch.setattr(al, "alert_send", fake)

    assert al.disk_check(inst)["level"] == ""          # 30% 正常，不打扰
    assert sink == []

    holder["pct"] = 10
    r = al.disk_check(inst)
    assert r["level"] == "warn" and sink[-1] == "warn"
    assert al.alert_state_load(inst)["disk_level"] == "warn"

    holder["pct"] = 5
    r = al.disk_check(inst)                            # 跨级切换不被去重窗口吞掉
    assert r["level"] == "critical" and sink[-1] == "critical"
    assert al.alert_state_load(inst)["disk_level"] == "critical"

    holder["pct"] = 40
    r = al.disk_check(inst)
    assert r["level"] == "" and sink[-1] == "info"     # 恢复通知
    assert al.alert_state_load(inst)["disk_level"] == ""


def test_disk_check_respects_disable_switch(monkeypatch, tmp_path):
    inst = _inst(tmp_path)
    monkeypatch.setattr(al, "alert_cfg", lambda i: _cfg(disk_enable=False))
    monkeypatch.setattr(al, "disk_status", lambda i: {
        "ok": True, "probes": [],
        "worst": {"ok": True, "path": "/", "total": 100, "used": 95, "avail": 5, "percent": 5},
    })
    sink, fake = _recorder()
    monkeypatch.setattr(al, "alert_send", fake)

    assert al.disk_check(inst)["level"] == "critical"   # 级别照常记录
    assert sink == []                                   # 但绝不推送


def test_disk_check_custom_thresholds(monkeypatch, tmp_path):
    inst = _inst(tmp_path)
    monkeypatch.setattr(al, "alert_cfg", lambda i: _cfg(disk_warn=50, disk_crit=40))
    monkeypatch.setattr(al, "disk_status", lambda i: {
        "ok": True, "probes": [],
        "worst": {"ok": True, "path": "/", "total": 100, "used": 55, "avail": 45, "percent": 45},
    })
    sink, fake = _recorder()
    monkeypatch.setattr(al, "alert_send", fake)
    assert al.disk_check(inst)["level"] == "warn"      # 45% < 自定义 50%


# ================= 7. 每日健康晨报 =================

def _yesterday_start() -> int:
    lt = time.localtime(time.time() - 86400)
    return int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1)))


def test_morning_report_body_aggregates(monkeypatch, tmp_path):
    inst = _inst(tmp_path)
    y = _yesterday_start()

    monkeypatch.setattr(al, "history_sync", lambda i: {"items": [
        {"id": 1, "start_ts": y + 3600, "status": "done", "task_label": "日常任务"},
        {"id": 2, "start_ts": y + 7200, "status": "done", "task_label": "清体力"},
        {"id": 3, "start_ts": y + 10800, "status": "aborted", "task_label": "领取奖励"},
    ]})
    monkeypatch.setattr(al, "disk_status", lambda i: {"ok": True, "probes": [], "worst": {
        "ok": True, "path": "/", "total": 100, "used": 80, "avail": 20, "percent": 20}})
    monkeypatch.setattr(al, "container_is_running", lambda i: True)
    monkeypatch.setattr(al, "upstream_status", lambda i: {
        "local": "2026.9.30", "latest": "2026.10.3", "has_update": True, "url": "https://x/y"})

    body = al.morning_report_body(inst)

    assert "每日健康晨报" in body
    assert "m2c" in body                  # 容器名
    assert "完成 2" in body and "中断 1" in body
    assert "剩余 20%" in body
    assert "运行中" in body
    assert "2026.10.3" in body


def test_morning_report_body_handles_container_down(monkeypatch, tmp_path):
    inst = _inst(tmp_path)
    monkeypatch.setattr(al, "history_sync", lambda i: {"items": []})
    monkeypatch.setattr(al, "disk_status", lambda i: {"ok": False, "probes": [], "worst": {"ok": False}})
    monkeypatch.setattr(al, "container_is_running", lambda i: False)
    monkeypatch.setattr(al, "upstream_status", lambda i: {})

    body = al.morning_report_body(inst)
    assert "未运行" in body
    assert "磁盘" in body                          # 探不到也要给一行，不能抛异常


def test_morning_report_once_per_local_day(monkeypatch, tmp_path):
    inst = _inst(tmp_path)
    monkeypatch.setattr(al, "alert_cfg", lambda i: _cfg(morning_time="00:00"))
    monkeypatch.setattr(al, "morning_report_body", lambda i: "晨报正文")
    sink, fake = _recorder()
    monkeypatch.setattr(al, "alert_send", fake)

    r = al.morning_report(inst, force=True)
    assert r["ok"] is True and sink == ["info"]     # 晨报走 info 级

    r2 = al.morning_report(inst)                    # 同一天非 force → 不重复打扰
    assert r2["ok"] is False and "已发送" in r2["msg"]
    assert sink == ["info"]


def test_morning_report_skips_before_scheduled_time(monkeypatch, tmp_path):
    inst = _inst(tmp_path)
    monkeypatch.setattr(al, "alert_cfg", lambda i: _cfg(morning_time="23:59"))
    monkeypatch.setattr(al, "morning_report_body", lambda i: "晨报正文")
    sink, fake = _recorder()
    monkeypatch.setattr(al, "alert_send", fake)

    s = al.alert_state_load(inst)
    s["morning_last"] = ""                          # 今天还没发过
    al.alert_state_save(inst, s)

    now_hm = time.strftime("%H:%M")
    if now_hm == "23:59":                            # 极窄边界：跳过以免偶发失败
        pytest.skip("刚好落在 23:59")

    r = al.morning_report(inst)
    assert r["ok"] is False
    assert sink == []


def test_morning_report_can_be_disabled(monkeypatch, tmp_path):
    inst = _inst(tmp_path)
    monkeypatch.setattr(al, "alert_cfg", lambda i: _cfg(morning_enable=False))
    monkeypatch.setattr(al, "morning_report_body",
                        lambda i: (_ for _ in ()).throw(AssertionError("关闭后不该组装晨报")))
    r = al.morning_report(inst)
    assert r["ok"] is False and "关闭" in r["msg"]


# ================= 8. 上游本体更新提醒 =================

def test_local_assistant_version_from_instance_dir(monkeypatch, tmp_path):
    inst = _inst(tmp_path)
    (Path(inst["dir"]) / "version.txt").write_text("v2026.9.30\n", encoding="utf-8")
    assert al.local_assistant_version(inst) == "2026.9.30"


def test_local_assistant_version_falls_back_to_container(monkeypatch, tmp_path):
    from services import shell as _shell

    inst = _inst(tmp_path)                          # 实例目录里没有版本文件
    seen = []

    def _run(cmd, timeout=120):
        seen.append(cmd)
        return {"code": 0, "out": "v2026.10.3"}

    monkeypatch.setattr(_shell, "run_cmd", _run)
    assert al.local_assistant_version(inst) == "2026.10.3"
    assert "docker exec m2c" in seen[-1]


def test_local_assistant_version_never_lies_on_failure(monkeypatch, tmp_path):
    from services import shell as _shell

    inst = _inst(tmp_path)
    monkeypatch.setattr(_shell, "run_cmd",
                        lambda cmd, timeout=120: {"code": 1, "out": "Error: No such container"})
    assert al.local_assistant_version(inst) == ""   # 取不到就留空，绝不误报更新


def test_upstream_check_detects_update_and_notifies_once(monkeypatch, tmp_path):
    inst = _inst(tmp_path)
    monkeypatch.setattr(al, "alert_cfg", lambda i: _cfg())
    monkeypatch.setattr(al, "local_assistant_version", lambda i: "2026.9.30")
    monkeypatch.setattr(al, "http_get", lambda url, timeout=8: {
        "code": 200,
        "body": json.dumps({"tag_name": "v2026.10.3",
                            "html_url": "https://github.com/moesnow/March7thAssistant/releases/tag/v2026.10.3"}),
    })
    sink, fake = _recorder()
    monkeypatch.setattr(al, "alert_send", fake)

    r = al.upstream_check(inst, force=True)
    assert r["has_update"] is True
    assert r["latest"] == "2026.10.3"
    assert r["local"] == "2026.9.30"
    assert sink == ["warn"]                          # 有新版 → warn 级提醒
    assert al.alert_state_load(inst)["upstream_notified"] == "2026.10.3"

    r2 = al.upstream_check(inst)                     # 6 小时内走缓存，不再打网络
    assert r2.get("cached") is True
    assert sink == ["warn"]


def test_upstream_check_no_false_positive_when_local_newer(monkeypatch, tmp_path):
    inst = _inst(tmp_path)
    monkeypatch.setattr(al, "alert_cfg", lambda i: _cfg())
    monkeypatch.setattr(al, "local_assistant_version", lambda i: "2026.10.3")
    monkeypatch.setattr(al, "http_get", lambda url, timeout=8: {
        "code": 200, "body": json.dumps({"tag_name": "v2026.10.3"})})
    sink, fake = _recorder()
    monkeypatch.setattr(al, "alert_send", fake)

    r = al.upstream_check(inst, force=True)
    assert r["has_update"] is False
    assert sink == []
    assert al.alert_state_load(inst)["upstream_latest"] == "2026.10.3"


def test_upstream_check_tolerates_api_failure(monkeypatch, tmp_path):
    inst = _inst(tmp_path)
    monkeypatch.setattr(al, "alert_cfg", lambda i: _cfg())
    monkeypatch.setattr(al, "local_assistant_version", lambda i: "2026.9.30")
    monkeypatch.setattr(al, "http_get", lambda url, timeout=8: {"code": 403, "body": "rate limited"})
    sink, fake = _recorder()
    monkeypatch.setattr(al, "alert_send", fake)

    r = al.upstream_check(inst, force=True)
    assert r["ok"] is False
    assert r["has_update"] is False
    assert sink == []                                # 接口挂了不误报
    assert al.alert_state_load(inst)["upstream_error"]


def test_upstream_check_respects_disable_switch(monkeypatch, tmp_path):
    inst = _inst(tmp_path)
    monkeypatch.setattr(al, "alert_cfg", lambda i: _cfg(upstream_enable=False))
    monkeypatch.setattr(al, "http_get",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("关闭后不该请求 GitHub")))
    monkeypatch.setattr(al, "local_assistant_version", lambda i: "2026.9.30")
    r = al.upstream_check(inst, force=True)
    assert r["ok"] is False and "关闭" in r["msg"]


def test_upstream_status_is_cache_only(monkeypatch, tmp_path):
    """面板每次渲染都要读它 —— 必须零网络、零命令。"""
    inst = _inst(tmp_path)
    monkeypatch.setattr(al, "http_get",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("不该发网络请求")))
    monkeypatch.setattr(al, "local_assistant_version",
                        lambda i: (_ for _ in ()).throw(AssertionError("不该跑命令")))

    s = al.alert_state_load(inst)
    s.update({"upstream_local": "2026.9.30", "upstream_latest": "2026.10.3"})
    al.alert_state_save(inst, s)

    st = al.upstream_status(inst)
    assert st["local"] == "2026.9.30"
    assert st["latest"] == "2026.10.3"
    assert st["has_update"] is True


# ================= 9. health_tick 统一入口节流 =================

def test_health_tick_throttles_disk_probe(monkeypatch, tmp_path):
    inst = _inst(tmp_path)
    n = {"calls": 0}

    def _status(i):
        n["calls"] += 1
        return {"ok": True, "probes": [], "worst": {
            "ok": True, "path": "/", "total": 100, "used": 20, "avail": 80, "percent": 80}}

    monkeypatch.setattr(al, "alert_cfg", lambda i: _cfg())
    monkeypatch.setattr(al, "disk_status", _status)
    monkeypatch.setattr(al, "alert_send", lambda *a, **k: {"ok": True, "msg": "ok"})
    monkeypatch.setattr(al, "morning_report", lambda i, force=False: {"ok": False, "msg": "skip"})
    monkeypatch.setattr(al, "upstream_check", lambda i, force=False: {"ok": True})

    al.health_tick(inst)
    al.health_tick(inst)
    assert n["calls"] == 1                            # 6 分钟节流


def test_health_tick_survives_broken_substep(monkeypatch, tmp_path):
    inst = _inst(tmp_path)
    monkeypatch.setattr(al, "alert_cfg", lambda i: _cfg())
    monkeypatch.setattr(al, "disk_status",
                        lambda i: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(al, "morning_report", lambda i, force=False: {"ok": False, "msg": "skip"})
    monkeypatch.setattr(al, "upstream_check", lambda i, force=False: {"ok": True})

    r = al.health_tick(inst)                           # 单项炸掉不能拖垮整轮
    assert isinstance(r, dict)
    assert "morning" in r and "upstream" in r
