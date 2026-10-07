"""异常告警 + 巡检心跳 —— 等价 PHP alert_*/heartbeat_*（v1.18）。

状态机文件 data/alert_state.json、心跳文件 data/heartbeat_<which>.txt 与 PHP 共享。

v1.22 增强（重试 / 退避 / 去重）：
- 推送失败按 ALERT_RETRY_DELAYS（30/60/120s）指数退避重试，最多 ALERT_MAX_RETRY 次；
  alert_tick 每次先处理 pending 队列，成功即清除。
- 连续失败达 ALERT_BACKOFF_STREAK 次进入 ALERT_BACKOFF_SECONDS 冷静期，期内不推送只记事件。
- 同一 kind 告警在 ALERT_DEDUP_WINDOW 秒内不重复推送（仍发布事件供前端展示）。
- 事件 payload 增加 retry_count 字段，供前端展示「重试中/已重试 N 次」。
状态文件仍原子写（tmp + os.replace）；缺字段用默认值补齐，兼容旧 alert_state.json。
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
    # v1.22 状态机增强字段
    "retry_at": 0,              # 下次重试时间（epoch 秒，0=无待重试）
    "retry_count": 0,           # 当前待重试项已重试次数
    "pending": [],              # 待重试项列表：[{kind,title,body}]
    "last_push_kind_at": {},    # kind → 上次成功推送时间戳（去重窗口依据）
    "push_fail_streak": 0,      # 连续推送失败次数（触发冷静期）
    "push_backoff_until": 0,    # 冷静期截止时间（epoch 秒）
}


def _data_dir(inst: dict) -> Path:
    return history_data_file(inst).parent


def alert_state_file(inst: dict) -> Path:
    return _data_dir(inst) / "alert_state.json"


def alert_state_load(inst: dict) -> dict:
    s = dict(_ALERT_DEFAULT)
    # 可变默认值必须逐次重置（浅拷贝会共享同一 list/dict 对象）
    s["aborted_seen"] = []
    s["pending"] = []
    s["last_push_kind_at"] = {}
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
    # 缺字段/类型异常用默认值补齐（兼容旧 alert_state.json）
    if not isinstance(s.get("aborted_seen"), list):
        s["aborted_seen"] = []
    if not isinstance(s.get("pending"), list):
        s["pending"] = []
    if not isinstance(s.get("last_push_kind_at"), dict):
        s["last_push_kind_at"] = {}
    for k in ("retry_at", "retry_count", "push_fail_streak", "push_backoff_until"):
        try:
            s[k] = int(s.get(k) or 0)
        except (TypeError, ValueError):
            s[k] = 0
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


def _publish_alert(inst: dict, kind: str, title: str, body: str,
                   pushed: bool, retry_count: int, msg: str) -> None:
    from services import events as _ev
    _sid = str(inst.get("id") or inst.get("container") or "")
    _ev.publish("alert", {
        "kind": kind, "title": title, "body": body,
        "pushed": pushed, "retry_count": int(retry_count), "msg": msg,
    }, inst=_sid)


def _push_events(inst: dict, s: dict, events_out: list, now: int) -> bool:
    """按去重/冷静期规则推送事件；失败入 pending 队列。返回是否成功推送。

    events_out: [(kind, title, body)]。无论是否推送都会发布事件（供前端展示），
    未推送的附带 retry_count / 说明 msg。
    """
    dedup = int(cfg.ALERT_DEDUP_WINDOW)
    lpk = s.get("last_push_kind_at") or {}
    sendable: list = []
    for kind, title, body in events_out:
        if dedup > 0 and now - int(lpk.get(kind) or 0) < dedup:
            _publish_alert(inst, kind, title, body, False, 0,
                           "同类型告警在去重窗口内，未重复推送")
            continue
        sendable.append((kind, title, body))
    if not sendable:
        return False
    if now < int(s.get("push_backoff_until") or 0):
        for kind, title, body in sendable:
            _publish_alert(inst, kind, title, body, False, 0,
                           "推送连续失败，冷静期内暂不推送")
        return False

    r = alert_send("M7A 面板告警", "\n\n".join(b for _, _, b in sendable))
    if r.get("ok"):
        for kind, _, _ in sendable:
            lpk[kind] = now
        s["last_push_kind_at"] = lpk
        s["push_fail_streak"] = 0
        for kind, title, body in sendable:
            _publish_alert(inst, kind, title, body, True, 0,
                           str(r.get("msg") or "已推送"))
        return True

    # —— 失败：入 pending、计数、必要时进入冷静期 ——
    msg = str(r.get("msg") or "")
    s["push_fail_streak"] = int(s.get("push_fail_streak") or 0) + 1
    pend = list(s.get("pending") or [])
    have = {(p.get("kind"), p.get("body")) for p in pend if isinstance(p, dict)}
    for kind, title, body in sendable:
        if (kind, body) not in have:
            pend.append({"kind": kind, "title": title, "body": body})
            have.add((kind, body))
    s["pending"] = pend[-20:]
    s["retry_count"] = 0
    s["retry_at"] = now + int(cfg.ALERT_RETRY_DELAYS[0])
    if int(s.get("push_fail_streak") or 0) >= int(cfg.ALERT_BACKOFF_STREAK):
        s["push_backoff_until"] = now + int(cfg.ALERT_BACKOFF_SECONDS)
    for kind, title, body in sendable:
        _publish_alert(inst, kind, title, body, False, 0,
                       (msg or "推送失败") + "，将自动重试")
    return False


def _process_pending(inst: dict, s: dict, now: int) -> bool:
    """处理待重试队列。返回 True 表示有 pending 存在（本轮不另起新推送）。

    重试节奏：30s → 60s → 120s，最多 ALERT_MAX_RETRY 次；成功即清除并清零失败计数。
    """
    pend = [p for p in (s.get("pending") or []) if isinstance(p, dict)]
    if not pend:
        return False
    if now < int(s.get("retry_at") or 0):
        return True
    # 说明：重试队列不受冷静期阻断——重试本身已被 30/60/120 限次约束；
    # 冷静期只用于抑制「新的」告警推送（见 _push_events）。

    r = alert_send("M7A 面板告警", "\n\n".join(str(p.get("body") or "") for p in pend))
    if r.get("ok"):
        # 本次成功的这次即为第 (rc+1) 次重试，前端显示「已重试 N 次」
        rc = int(s.get("retry_count") or 0) + 1
        lpk = s.get("last_push_kind_at") or {}
        for p in pend:
            lpk[str(p.get("kind") or "")] = now
        s["last_push_kind_at"] = lpk
        s["pending"] = []
        s["retry_count"] = 0
        s["retry_at"] = 0
        s["push_fail_streak"] = 0
        for p in pend:
            _publish_alert(inst, str(p.get("kind") or ""), str(p.get("title") or ""),
                           str(p.get("body") or ""), True, rc, "重试成功")
        return True

    rc = int(s.get("retry_count") or 0) + 1
    s["retry_count"] = rc
    s["push_fail_streak"] = int(s.get("push_fail_streak") or 0) + 1
    delays = list(cfg.ALERT_RETRY_DELAYS)
    if rc >= int(cfg.ALERT_MAX_RETRY) or rc >= len(delays):
        # 重试次数用尽 → 放弃该批
        s["pending"] = []
        s["retry_at"] = 0
        if int(s.get("push_fail_streak") or 0) >= int(cfg.ALERT_BACKOFF_STREAK):
            s["push_backoff_until"] = now + int(cfg.ALERT_BACKOFF_SECONDS)
        for p in pend:
            _publish_alert(inst, str(p.get("kind") or ""), str(p.get("title") or ""),
                           str(p.get("body") or ""), False, rc,
                           "重试次数用尽，已放弃推送")
        return True

    nxt = now + int(delays[rc])
    s["retry_at"] = nxt
    for p in pend:
        _publish_alert(inst, str(p.get("kind") or ""), str(p.get("title") or ""),
                       str(p.get("body") or ""), False, rc,
                       f"推送失败，{int(delays[rc])} 秒后第 {rc} 次重试")
    return True


def alert_tick(inst: dict, force: bool = False) -> dict:
    """巡检状态机：up→down 推故障、down→up 推恢复、aborted 记录推任务中断。
    M4：每次真正产生告警时发布 alert 事件（WS 实时推送 + SQLite 留档）。
    v1.22：每次 tick 先处理待重试队列，再走状态机检测（推送含去重/退避）。"""
    s = alert_state_load(inst)
    now = int(time.time())
    if not force and now - int(s.get("last_tick") or 0) < 45:
        return {"ok": True, "throttled": True, "pushed": False}
    s["last_tick"] = now

    # 1) 先处理待重试队列（失败告警的退避重试）
    _process_pending(inst, s, now)

    run = container_is_running(inst)
    prev = s.get("prev")
    s["prev"] = run
    cfgv = alert_cfg(inst)
    pushed = False
    if cfgv["enable"]:
        quiet = now < int(s.get("quiet_until") or 0)
        events_out: list = []   # (kind, title, body)
        if prev is True and run is False and not s.get("alerted_down") and not quiet:
            body = "🛑 容器意外停止\n面板检测到容器已不在运行，任务可能中断。"
            events_out.append(("down", "容器意外停止", body))
            s["alerted_down"] = True
        elif prev is False and run is True and s.get("alerted_down"):
            body = "✅ 容器已恢复\n容器已重新运行，可以正常跑任务了。"
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
            events_out.append(("aborted", f"任务中断：{label}", body))
        if len(aborted_seen) > 40:
            aborted_seen = aborted_seen[-40:]
        s["aborted_seen"] = aborted_seen
        if events_out:
            pushed = _push_events(inst, s, events_out, now)
    alert_state_save(inst, s)
    return {"ok": True, "pushed": pushed, "run": run,
            "msg": "" if not pushed else "已推送",
            "pending": len(s.get("pending") or []),
            "retry_count": int(s.get("retry_count") or 0)}


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
