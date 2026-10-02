"""异常告警 + 巡检心跳 —— 等价 PHP alert_*/heartbeat_*（v1.18）。

状态机文件 data/alert_state.json、心跳文件 data/heartbeat_<which>.txt 与 PHP 共享。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import config as cfg
from services.containers import container_is_running
from services.history import history_sync, history_data_file
from services.instances import panel_config
from services.updateops import http_get, http_post

_ALERT_DEFAULT = {
    "prev": None,
    "alerted_down": False,
    "quiet_until": 0,
    "last_tick": 0,
    "aborted_seen": [],
}


def _data_dir(inst: dict) -> Path:
    return history_data_file(inst).parent


def alert_state_file(inst: dict) -> Path:
    return _data_dir(inst) / "alert_state.json"


def alert_state_load(inst: dict) -> dict:
    s = dict(_ALERT_DEFAULT)
    s["aborted_seen"] = []
    try:
        raw = alert_state_file(inst).read_text("utf-8")
    except OSError:
        return s
    if raw.strip():
        try:
            d = json.loads(raw)
        except ValueError:
            d = None
        if isinstance(d, dict):
            for k, v in d.items():
                s[k] = v
    return s


def alert_state_save(inst: dict, s: dict) -> None:
    try:
        p = alert_state_file(inst)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(json.dumps(s, ensure_ascii=False), "utf-8")
        import os
        os.replace(tmp, p)
    except OSError:
        pass


def alert_quiet(inst: dict, sec: int) -> None:
    """用户主动容器操作 → 静默 sec 秒，避免把人为操作当异常推送。"""
    s = alert_state_load(inst)
    s["quiet_until"] = int(time.time()) + int(sec)
    alert_state_save(inst, s)


def alert_cfg(inst: dict) -> dict:
    c = panel_config()
    return {
        "enable": str(c.get("alert_enable") or "") == "1",
        "channel": str(c.get("alert_channel") or "bark"),
        "target": str(c.get("alert_target") or "").strip(),
    }


def alert_send(title: str, body: str, override: dict | None = None) -> dict:
    cfgv = override if override is not None else alert_cfg({})
    if not cfgv.get("enable"):
        return {"ok": False, "msg": "告警未启用"}
    if not cfgv.get("target"):
        return {"ok": False, "msg": "未配置接收地址"}
    ch = cfgv.get("channel") or "bark"
    target = str(cfgv["target"])
    if ch == "bark":
        from urllib.parse import quote
        url = f"https://api.day.app/{quote(target, safe='')}/{quote(title, safe='')}/{quote(body, safe='')}"
        r = http_get(url, 8)
        code = int(r["code"])
        return {"ok": 200 <= code < 300, "msg": f"Bark 返回 HTTP {code}"}
    if ch == "serverchan":
        from urllib.parse import quote
        r = http_post(f"https://sctapi.ftqq.com/{quote(target, safe='')}.send",
                      {"title": title, "desp": body}, 8, False)
        code = int(r["code"])
        return {"ok": 200 <= code < 300, "msg": f"Server酱 返回 HTTP {code}"}
    r = http_post(target, {"title": title, "body": body,
                           "time": time.strftime("%Y-%m-%d %H:%M:%S")}, 8, True)
    code = int(r["code"])
    return {"ok": 200 <= code < 300, "msg": f"Webhook 返回 HTTP {code}"}


def alert_tick(inst: dict, force: bool = False) -> dict:
    """巡检状态机：up→down 推故障、down→up 推恢复、aborted 记录推任务中断。
    M4：每次真正产生告警时发布 alert 事件（WS 实时推送 + SQLite 留档）。"""
    s = alert_state_load(inst)
    now = int(time.time())
    if not force and now - int(s.get("last_tick") or 0) < 45:
        return {"ok": True, "throttled": True, "pushed": False}
    s["last_tick"] = now
    run = container_is_running(inst)
    prev = s.get("prev")
    s["prev"] = run
    cfgv = alert_cfg(inst)
    msgs: list[str] = []
    events_out: list[tuple[str, str, str]] = []   # (kind, title, body)
    if cfgv["enable"]:
        quiet = now < int(s.get("quiet_until") or 0)
        if prev is True and run is False and not s.get("alerted_down") and not quiet:
            body = "🛑 容器意外停止\n面板检测到容器已不在运行，任务可能中断。"
            msgs.append(body)
            events_out.append(("down", "容器意外停止", body))
            s["alerted_down"] = True
        elif prev is False and run is True and s.get("alerted_down"):
            body = "✅ 容器已恢复\n容器已重新运行，可以正常跑任务了。"
            msgs.append(body)
            events_out.append(("recovered", "容器已恢复", body))
            s["alerted_down"] = False
        hd = history_sync(inst)
        aborted_seen = list(s.get("aborted_seen") or [])
        for it in hd["items"]:
            if it.get("status") != "aborted":
                continue
            try:
                iid = int(it.get("id") or 0)
            except (TypeError, ValueError):
                iid = 0
            if iid <= 0 or iid in aborted_seen:
                continue
            aborted_seen.append(iid)
            if quiet:
                continue
            label = str(it.get("task_label") or "任务")
            body = f"⚠️ 任务中断：{label}\n请到面板「日志」页查看原因。"
            msgs.append(body)
            events_out.append(("aborted", f"任务中断：{label}", body))
        if len(aborted_seen) > 40:
            aborted_seen = aborted_seen[-40:]
        s["aborted_seen"] = aborted_seen
        if msgs:
            r = alert_send("M7A 面板告警", "\n\n".join(msgs))
            pushed = bool(r.get("ok"))
            alert_state_save(inst, s)
            from services import events as _ev
            _sid = str(inst.get("id") or inst.get("container") or "")
            for kind, title, body in events_out:
                _ev.publish("alert", {
                    "kind": kind, "title": title, "body": body,
                    "pushed": pushed, "msg": str(r.get("msg") or ""),
                }, inst=_sid)
            return {"ok": True, "pushed": pushed, "run": run,
                    "msg": str(r.get("msg") or "")}
    alert_state_save(inst, s)
    return {"ok": True, "pushed": False, "run": run}


# ===== 巡检心跳 =====

def heartbeat_touch(inst: dict, which: str) -> None:
    try:
        d = _data_dir(inst)
        d.mkdir(parents=True, exist_ok=True)
        (d / f"heartbeat_{which}.txt").write_text(str(int(time.time())), "ascii")
    except OSError:
        pass


def heartbeat_read(inst: dict) -> dict:
    out: dict = {}
    d = _data_dir(inst)
    for w in ("scheduler", "monitor", "alerter"):
        f = d / f"heartbeat_{w}.txt"
        if f.is_file():
            try:
                out[w] = int(f.read_text("ascii").strip() or 0)
            except (OSError, ValueError):
                out[w] = 0
    return out
