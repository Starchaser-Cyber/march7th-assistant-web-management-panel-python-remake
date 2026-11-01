"""V1.27 测试：错误循环预警（loop_patrol）+ 企微通道（wecom）+ 配置保存。

覆盖：
- loop_patrol：默认关闭 / 节流 / 无日志 / 计数不足 / 超阈值触发一次 /
  去重窗口内不重复 / 状态落盘 / 强制绕过节流
- alert_send wecom 分支：正确请求体、URL 校验复用（拒内网）、未启用分支
- h_alert_save：收集 loop_alert_* 三键并 roundtrip 保存
- _alert_channels：白名单含 wecom
- 模板/JS：错误循环预警行元素 + 企微 option + _afillAlertForm 三个 append
"""
from __future__ import annotations

import datetime
from pathlib import Path

import config as cfg
from services import alerts


def _inst(tmp_path):
    d = tmp_path / "inst"
    (d / "logs").mkdir(parents=True, exist_ok=True)
    return {"id": "m2", "name": "M2面板", "container": "m2c", "dir": str(d)}


def _cfg(**over):
    v = {
        "alert_enable": "1",
        "alert_channel": "bark",
        "alert_target": "test-key",
        "loop_alert_enable": "1",
        "loop_alert_window": "5",
        "loop_alert_min_count": "2",
    }
    v.update(over)
    return v


def _seed_log(inst, n, minutes_ago=0):
    """在实例 logs/ 下种 n 行「重复错误」日志（带时间戳，间隔 1 秒）。"""
    ts = datetime.datetime.now() - datetime.timedelta(minutes=minutes_ago)
    lines = []
    for i in range(n):
        stamp = (ts + datetime.timedelta(seconds=i)).strftime("%Y-%m-%d %H:%M:%S")
        lines.append(f"{stamp} ERROR | 截图失败：没有找到游戏窗口（第{i}次）")
    p = f"{inst['dir']}/logs/2026-11-01.log"
    with open(p, "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return p


def _reset(monkeypatch, tmp_path, over=None):
    inst = _inst(tmp_path)
    monkeypatch.setattr(alerts, "panel_config", lambda *a, **k: _cfg(**(over or {})))
    return inst


# ===== loop_patrol 行为 =====

def test_loop_patrol_default_off(monkeypatch, tmp_path):
    inst = _reset(monkeypatch, tmp_path, over={"loop_alert_enable": "0"})
    out = alerts.loop_patrol(inst, force=True)
    assert out.get("ok") is True
    assert out.get("enabled") is False


def test_loop_patrol_disabled_by_default(monkeypatch, tmp_path):
    """未配置 loop_alert_enable 时默认关闭（不产生误报）。"""
    inst = _reset(monkeypatch, tmp_path, over={"loop_alert_enable": ""})
    out = alerts.loop_patrol(inst, force=True)
    assert out.get("enabled") is False


def test_loop_patrol_no_logs(monkeypatch, tmp_path):
    inst = _reset(monkeypatch, tmp_path)
    out = alerts.loop_patrol(inst, force=True)
    assert out.get("ok") is True
    assert out.get("checked") is False
    assert out.get("count") == 0


def test_loop_patrol_below_threshold(monkeypatch, tmp_path):
    inst = _reset(monkeypatch, tmp_path, over={"loop_alert_min_count": "5"})
    _seed_log(inst, 3)
    out = alerts.loop_patrol(inst, force=True)
    assert out.get("count") == 3
    assert out.get("alerted") is False


def test_loop_patrol_above_threshold_pushes(monkeypatch, tmp_path):
    inst = _reset(monkeypatch, tmp_path, over={"loop_alert_min_count": "2"})
    _seed_log(inst, 4)

    calls = []
    monkeypatch.setattr(alerts, "_push_events",
                        lambda i, s, ev, now, busy_kinds=None: (calls.append((ev, now)) or True))
    out = alerts.loop_patrol(inst, force=True)
    assert out.get("count") == 4
    assert out.get("alerted") is True
    assert calls
    kind, title, body, sev = calls[0][0][0]
    assert kind == "error_loop"
    assert title == "M7A 本体疑似异常循环"
    assert "4 条重复错误" in body
    assert sev == "warn"


def test_loop_patrol_dedup_window(monkeypatch, tmp_path):
    """去重窗口内（ALERT_DEDUP_WINDOW=120s）不重复推送。"""
    inst = _reset(monkeypatch, tmp_path, over={"loop_alert_min_count": "2"})
    _seed_log(inst, 4)

    pushes = []

    def fake_push(i, s, ev, now, busy_kinds=None):
        pushes.append(1)
        # 真实 _push_events 成功后会记 last_push_kind_at，这里等价模拟
        s.setdefault("last_push_kind_at", {})["error_loop"] = now
        return True

    monkeypatch.setattr(alerts, "_push_events", fake_push)
    assert alerts.loop_patrol(inst, force=True).get("alerted") is True
    assert len(pushes) == 1
    assert alerts.loop_patrol(inst, force=True).get("alerted") is False
    assert len(pushes) == 1  # 去重窗口内未再推送


def test_loop_patrol_throttle_without_force(monkeypatch, tmp_path):
    """非 force 且距上次检查 < LOOP_ALERT_CHECK_IV 时被节流。"""
    inst = _reset(monkeypatch, tmp_path, over={"loop_alert_min_count": "2"})
    _seed_log(inst, 4, minutes_ago=1)
    alerts.loop_patrol(inst, force=True)      # 首次 force 跑
    out = alerts.loop_patrol(inst, force=False)  # 立刻再来 → 节流
    assert out.get("throttled") is True


def test_loop_patrol_saves_state(monkeypatch, tmp_path):
    inst = _reset(monkeypatch, tmp_path)
    _seed_log(inst, 2)
    alerts.loop_patrol(inst, force=True)
    st = alerts.alert_state_load(inst)
    assert st.get("loop_last_check", 0) > 0
    assert st.get("loop_last_count") == 2


# ===== wecom 通道 =====

def test_alert_send_wecom_uses_webhook_url(monkeypatch):
    calls = []

    def fake_post(url, data, timeout, json_mode):
        calls.append((url, data))
        return {"code": 200}

    monkeypatch.setattr(alerts, "http_post", fake_post)
    ov = {"enable": True, "channel": "wecom",
          "target": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc123"}
    r = alerts.alert_send("标题", "正文", ov, severity="warn")
    assert r.get("ok") is True
    assert calls[0][0] == "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc123"
    assert calls[0][1]["msgtype"] == "text"
    assert calls[0][1]["text"]["content"] == "标题\n正文"


def test_alert_send_wecom_rejects_private_url():
    ov = {"enable": True, "channel": "wecom", "target": "http://127.0.0.1:8080/webhook"}
    r = alerts.alert_send("t", "b", ov)
    assert r.get("ok") is False
    assert "内网" in r.get("msg", "")


def test_alert_send_wecom_requires_enabled():
    ov = {"enable": False, "channel": "wecom", "target": "https://qyapi.weixin.qq.com/x"}
    assert alerts.alert_send("t", "b", ov).get("ok") is False


# ===== 配置保存 =====

def test_alert_save_collects_loop_keys(monkeypatch, tmp_path):
    import routers.write as write_mod

    saved = {}
    monkeypatch.setattr(write_mod, "panel_config_save", lambda upd: saved.update(upd) or True)
    inst = _inst(tmp_path)

    class F(dict):
        def get(self, k, d=None):
            return dict.get(self, k, d)

    form = F({
        "alert_enable": "1", "alert_channel": "bark", "alert_target": "x",
        "loop_alert_enable": "1", "loop_alert_window": "7", "loop_alert_min_count": "3",
    })
    r = write_mod.h_alert_save(None, form, {}, "sid")
    assert r[0] == "json"
    assert r[1].get("ok") is True
    assert saved["loop_alert_enable"] == "1"
    assert saved["loop_alert_window"] == 7
    assert saved["loop_alert_min_count"] == 3


def test_alert_channels_whitelist_has_wecom():
    from routers.write import _alert_channels

    out = _alert_channels({"alert_channel_info": "wecom"})
    assert out["alert_channel_info"] == "wecom"
    out2 = _alert_channels({"alert_channel_warn": "evil"})
    assert out2["alert_channel_warn"] == ""


# ===== 前端静态断言 =====

_REPO = Path(__file__).resolve().parent.parent


def test_template_has_loop_alert_row():
    tpl = (_REPO / "templates" / "panel.html.j2").read_text("utf-8")
    assert "错误循环预警" in tpl
    assert 'id="loopAlertEnable"' in tpl
    assert 'id="loopAlertWindow"' in tpl
    assert 'id="loopAlertMinCount"' in tpl


def test_template_has_wecom_options():
    tpl = (_REPO / "templates" / "panel.html.j2").read_text("utf-8")
    assert 'value="wecom"' in tpl
    assert "企业微信" in tpl


def test_js_afill_loop_keys():
    js = (_REPO / "static" / "panel.js").read_text("utf-8")
    assert "loop_alert_enable" in js
    assert "loopAlertWindow" in js
    assert "loopAlertMinCount" in js
