"""异常告警 + 巡检心跳 —— 等价 PHP alert_*/heartbeat_*（v1.18）。

状态机文件 data/alert_state.json、心跳文件 data/heartbeat_<which>.txt 与 PHP 共享。

v1.22 增强（重试 / 退避 / 去重）：
- 推送失败按 ALERT_RETRY_DELAYS（30/60/120s）指数退避重试，最多 ALERT_MAX_RETRY 次；
  alert_tick 每次先处理 pending 队列，成功即清除。
- 连续失败达 ALERT_BACKOFF_STREAK 次进入 ALERT_BACKOFF_SECONDS 冷静期，期内不推送只记事件。
- 同一 kind 告警在 ALERT_DEDUP_WINDOW 秒内不重复推送（仍发布事件供前端展示）。
- 事件 payload 增加 retry_count 字段，供前端展示「重试中/已重试 N 次」。

v1.24 增强（M2 告警升级）：
- 告警分级（info / warn / critical）：推送入口增加 severity，按级别可配不同通道
  （alert_channel_info / _warn / _critical，留空继承 alert_channel）；支持按级别静音
  （alert_mute_levels），静音仍写告警历史 + 发事件总线，仅不真正推送。
  容器操作静音 quiet_until（600s）保留且优先级高于分级静音。
- 磁盘空间预警（df -B1 探针）：剩余低于阈值发 warn / critical，恢复正常发 info；
  开关与阈值在面板配置页可调（disk_warn_percent / disk_crit_percent）。
- 每日健康晨报：每天本地 MORNING_REPORT_TIME 聚合昨日任务 / 告警数 / 磁盘剩余 / 本体存活，
  纯文本走三通道推送，可一键关闭；过点 4 小时内仍会补发一次。
- 上游本体更新提醒：定时查 GitHub Release 比对本地本体版本，有新版推 warn 级提醒 +
  面板内角标，仅提醒不自动升级。

状态文件仍原子写（tmp + os.replace）；缺字段用默认值补齐，兼容旧 alert_state.json。
"""
from __future__ import annotations

import json
import logging
import os
import random
import re
import shlex
import threading
import time
from datetime import datetime
from pathlib import Path

import config as cfg
from services.containers import container_is_running
from services.history import history_sync, history_data_file
from services.instances import panel_config, instance_dir
from services.logs import before_deadline, elapsed_ok, filter_log, latest_log_path
from services.updateops import http_get, http_post, version_gt

log = logging.getLogger("m7a.alerts")

# v1.24.4（P0-5）：alert_state.json 的「读 → 改 → 写」全程互斥。
# 6 类调用方并发（sampler 后台 / 4 个 ajax handler / pagectx 页面兜底 /
# health_tick 内部三段巡检），无锁时后写者用旧快照覆盖前者，丢失
# alerted_down / quiet_until / pending / last_tick。用 RLock 是因为
# alert_tick 内部还会调 alert_state_save / health_tick（同线程重入）。
_STATE_LOCK = threading.RLock()

_ALERT_DEFAULT = {
    "prev": None,
    "alerted_down": False,
    "quiet_until": 0,
    "last_tick": 0,
    "aborted_seen": [],
    # v1.24.4（P0-4 / P1-16）：静默期只是「延后推送」，不是「丢弃边沿」。
    # 静默期内观测到的 down 边沿 / aborted 先记在这里，静默期结束后补推。
    "pending_down": False,      # 静默期内检测到 down 边沿，待静默结束后补推
    "pending_down_first": False,  # 上面那条边沿是否为「首次观测即 down」（Q-04 文案用）
    "pending_aborted": [],      # 静默期内被压下的 aborted 项：[{id,label}]
    # v1.22 状态机增强字段
    "retry_at": 0,              # 下次重试时间（epoch 秒，0=无待重试）
    "retry_count": 0,           # 当前待重试项已重试次数
    "pending": [],              # 待重试项列表：[{kind,title,body,severity}]
    "last_push_kind_at": {},    # kind → 上次成功推送时间戳（去重窗口依据）
    "push_fail_streak": 0,      # 连续推送失败次数（触发冷静期）
    "push_backoff_until": 0,    # 冷静期截止时间（epoch 秒）
    # v1.24 M2 新增字段
    "disk_last_check": 0,       # 上次磁盘探针时间
    "disk_level": "",           # 当前磁盘告警级别："" / warn / critical
    "disk_path": "",            # 上次触发告警的探针路径
    "morning_last": "",         # 上次成功发送晨报的本地日期 YYYY-MM-DD
    "upstream_last_check": 0,   # 上次上游版本检查时间
    "upstream_local": "",       # 本地本体版本（数字部分）
    "upstream_latest": "",      # 上游最新版本（数字部分）
    "upstream_notified": "",    # 已提醒过的上游版本（避免重复提醒）
    "upstream_error": "",       # 上次上游检查的失败说明
    # v1.27：错误循环预警（本体日志重复错误检测）
    "loop_last_check": 0,       # 上次错误循环检查时间（epoch 秒）
    "loop_last_count": 0,       # 上次检查命中重复错误的行数
    # v1.28：推送通道健康看板（最近一次推送尝试的结果，供 alert_health 展示）
    "push_last_ok_at": 0,       # 上次推送成功的 epoch 秒
    "push_last_fail_at": 0,     # 上次推送失败的 epoch 秒
    "push_last_fail_msg": "",   # 上次推送失败的说明（如「Bark 返回 HTTP 400」）
    "push_last_fail_kind": "",  # 上次推送失败所属告警类型（error_loop / aborted / down…）
}

# 告警分级（顺序即展示/推送优先级）
_SEVERITIES = ("info", "warn", "critical")

# alert_tick 最小间隔（秒）；时钟回拨时 elapsed_ok 会判为到期
_TICK_IV = 45

# 面板里可选的推送通道（v1.27 新增企微机器人 Webhook）
_CHANNELS = ("bark", "serverchan", "webhook", "wecom")

_STR_FIELDS = (
    "disk_level", "disk_path", "morning_last",
    "upstream_local", "upstream_latest", "upstream_notified", "upstream_error",
    # v1.28：健康看板
    "push_last_fail_msg", "push_last_fail_kind",
)


def _data_dir(inst: dict) -> Path:
    return history_data_file(inst).parent


def alert_state_file(inst: dict) -> Path:
    return _data_dir(inst) / "alert_state.json"


def _tmp_name(p: Path) -> Path:
    """v1.24.4（P0-5）：临时文件名带 pid + 随机后缀，避免并发 save 互踩同一个 .tmp。"""
    return p.with_name(f"{p.name}.{os.getpid()}.{random.getrandbits(32):08x}.tmp")


def alert_state_load(inst: dict) -> dict:
    s = dict(_ALERT_DEFAULT)
    # 可变默认值必须逐次重置（浅拷贝会共享同一 list/dict 对象）
    s["aborted_seen"] = []
    s["pending"] = []
    s["pending_aborted"] = []
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
    if not isinstance(s.get("pending_aborted"), list):
        s["pending_aborted"] = []
    if not isinstance(s.get("last_push_kind_at"), dict):
        s["last_push_kind_at"] = {}
    for k in ("retry_at", "retry_count", "push_fail_streak", "push_backoff_until",
              "disk_last_check", "upstream_last_check",
              "loop_last_check", "loop_last_count",
              "push_last_ok_at", "push_last_fail_at"):
        try:
            s[k] = int(s.get(k) or 0)
        except (TypeError, ValueError):
            s[k] = 0
    for k in _STR_FIELDS:
        if not isinstance(s.get(k), str):
            s[k] = str(s.get(k) or "")
    return s


def alert_state_save(inst: dict, s: dict) -> bool:
    """原子写状态文件；返回是否成功。

    v1.24.4（P1-17）：原先 `except OSError: pass` 静默吞掉写盘失败，磁盘满时
    `prev` 每轮读回 None → down 告警永不触发，而磁盘满恰是最该告警的场景。
    现在失败会写 WARNING 日志并返回 False，调用方可据此不更新内存状态。
    """
    try:
        p = alert_state_file(inst)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = _tmp_name(p)
        try:
            tmp.write_text(json.dumps(s, ensure_ascii=False), "utf-8")
            os.replace(tmp, p)
        except OSError:
            # 别留下半个临时文件（磁盘满时 write_text 可能写了一半）
            try:
                tmp.unlink()
            except OSError:
                # 清理类：写盘已失败，删半截临时文件只是善后（原异常马上 re-raise）。
                log.debug("alerts: 清理半截状态临时文件失败 %s", tmp, exc_info=True)
            raise
        return True
    except OSError as e:
        log.warning("告警状态写盘失败（%s）：%s；本轮内存状态不回滚，"
                    "下一轮将读回旧快照", alert_state_file(inst), e)
        return False


def alert_quiet(inst: dict, sec: int) -> None:
    """用户主动容器操作 → 静默 sec 秒，避免把人为操作当异常推送。"""
    with _STATE_LOCK:
        s = alert_state_load(inst)
        s["quiet_until"] = int(time.time()) + int(sec)
        alert_state_save(inst, s)


# ===== 配置读取（v1.24：分级通道 / 分级静音 / 磁盘阈值 / 晨报 / 上游）=====

def _cfg_flag(c: dict, key: str, default_on: bool) -> bool:
    """面板配置是三态：未写过（缺键/空串）= 用默认值，写过则以 '1' / '0' 为准。"""
    v = c.get(key)
    if v is None or str(v).strip() == "":
        return bool(default_on)
    return str(v).strip() == "1"


def _cfg_int(c: dict, key: str, dft: int, lo: int, hi: int) -> int:
    v = c.get(key)
    try:
        n = int(str(v).strip())
    except (TypeError, ValueError):
        n = int(dft)
    return max(int(lo), min(int(hi), n))


def _cfg_hhmm(c: dict, key: str, dft: str) -> str:
    m = re.match(r"^\s*(\d{1,2})\s*:\s*(\d{2})\s*$", str(c.get(key) or ""))
    if not m:
        return str(dft)
    h, mi = int(m.group(1)), int(m.group(2))
    if not (0 <= h <= 23 and 0 <= mi <= 59):
        return str(dft)
    return f"{h:02d}:{mi:02d}"


def alert_cfg(inst: dict) -> dict:
    """告警配置。

    v1.24 新增：channels 为「按级别解析后的通道」（留空继承 alert_channel），
    mute 为被静音的级别列表；磁盘/晨报/上游的开关与阈值也在此归一。
    """
    c = panel_config()
    base_ch = str(c.get("alert_channel") or "bark").strip() or "bark"
    if base_ch not in _CHANNELS:
        base_ch = "bark"

    channels = {}
    for sev in _SEVERITIES:
        v = str(c.get("alert_channel_" + sev) or "").strip()
        channels[sev] = v if v in _CHANNELS else base_ch

    mute = [x.strip() for x in str(c.get("alert_mute_levels") or "").split(",")
            if x.strip() in _SEVERITIES]

    return {
        "enable": str(c.get("alert_enable") or "") == "1",
        "channel": base_ch,
        "target": str(c.get("alert_target") or "").strip(),
        "channels": channels,
        "mute": mute,
        "disk_enable": _cfg_flag(c, "disk_alert_enable", True),
        "disk_warn": _cfg_int(c, "disk_warn_percent", int(cfg.DISK_WARN_PERCENT), 1, 99),
        "disk_crit": _cfg_int(c, "disk_crit_percent", int(cfg.DISK_CRIT_PERCENT), 1, 99),
        "morning_enable": _cfg_flag(c, "morning_report_enable", bool(cfg.MORNING_REPORT_ENABLE)),
        "morning_time": _cfg_hhmm(c, "morning_report_time", str(cfg.MORNING_REPORT_TIME)),
        "upstream_enable": _cfg_flag(c, "upstream_check_enable", True),
        # v1.27：错误循环预警
        "loop_enable": _cfg_flag(c, "loop_alert_enable", False),
        "loop_window": _cfg_int(c, "loop_alert_window", int(cfg.LOOP_ALERT_WINDOW), 1, 60),
        "loop_min_count": _cfg_int(c, "loop_alert_min_count", int(cfg.LOOP_ALERT_MIN_COUNT), 1, 1000),
    }


# ===== 推送 =====

def alert_send(title: str, body: str, override: dict | None = None,
               severity: str = "warn") -> dict:
    """发一条告警。

    severity: info / warn / critical（v1.24 M2-5）。通道解析顺序：
    override["channels"][severity] → override["channel"] → alert_cfg()。
    向后兼容：老调用只传 (title, body, override)，此时 severity 默认 warn。

    v1.27：新增企微机器人 Webhook 通道（wecom）—— target 填
    https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxxx 完整地址。
    """
    cfgv = override if override is not None else alert_cfg({})
    sev = str(severity or "warn")
    if sev not in _SEVERITIES:
        sev = "warn"
    if not cfgv.get("enable"):
        return {"ok": False, "msg": "告警未启用"}
    if not cfgv.get("target"):
        return {"ok": False, "msg": "未配置接收地址"}
    chs = cfgv.get("channels") or {}
    ch = str(chs.get(sev) or cfgv.get("channel") or "bark")
    if ch not in _CHANNELS:
        ch = "bark"
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
    if ch == "wecom":
        # 企微群机器人：target 是完整 webhook URL，POST JSON 文本消息。
        ok_url, why = _webhook_url_ok(target)
        if not ok_url:
            return {"ok": False, "msg": why}
        r = http_post(target, {"msgtype": "text",
                               "text": {"content": f"{title}\n{body}"}},
                      8, True)
        code = int(r["code"])
        msg = "企微返回 HTTP " + str(code)
        if code == 200:
            msg = "企微已推送"
        return {"ok": 200 <= code < 300, "msg": msg}
    ok_url, why = _webhook_url_ok(target)
    if not ok_url:
        return {"ok": False, "msg": why}
    r = http_post(target, {"title": title, "body": body,
                           "severity": sev,
                           "time": time.strftime("%Y-%m-%d %H:%M:%S")}, 8, True)
    code = int(r["code"])
    return {"ok": 200 <= code < 300, "msg": f"Webhook 返回 HTTP {code}"}


def _webhook_url_ok(url: str) -> tuple[bool, str]:
    """v1.24.3 安全加固（M7A-08）：webhook 目标校验。

    target 来自管理员配置的任意 URL，原先直接 POST —— 一旦管理员会话被劫持，
    即可用它探测内网/云 metadata（SSRF 枢纽）。这里限制为 http(s) 并拒绝
    回环、链路本地（含 169.254.169.254）与 RFC1918 私网字面量地址。
    注意：仅做字面量判断，不做 DNS 解析（不引入解析放大与阻塞风险），
    域名解析到内网仍可能绕过 —— 管理员配置项按可信输入对待。
    """
    from urllib.parse import urlparse

    u = (url or "").strip()
    if u == "":
        return False, "未配置 Webhook 地址"
    try:
        pr = urlparse(u)
    except ValueError:
        return False, "Webhook 地址无法解析"
    if pr.scheme not in ("http", "https"):
        return False, "Webhook 仅支持 http/https"
    host = (pr.hostname or "").lower()
    if host == "":
        return False, "Webhook 地址缺少主机名"
    if host in ("localhost", "localhost.localdomain") or host.endswith(".localhost"):
        return False, "Webhook 不允许指向本机"
    import ipaddress

    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return True, ""                      # 域名：放行（不解析）
    if ip.is_loopback or ip.is_link_local or ip.is_private or ip.is_reserved:
        return False, "Webhook 不允许指向内网或保留地址"
    return True, ""


def _publish_alert(inst: dict, kind: str, title: str, body: str,
                   pushed: bool, retry_count: int, msg: str,
                   severity: str = "warn") -> None:
    from services import events as _ev
    _sid = str(inst.get("id") or inst.get("container") or "")
    sev = str(severity or "warn")
    _ev.publish("alert", {
        "kind": kind, "title": title, "body": body, "severity": sev,
        "pushed": pushed, "retry_count": int(retry_count), "msg": msg,
    }, inst=_sid)


def _ev4(ev) -> tuple:
    """归一事件元组为 (kind, title, body, severity)，兼容旧的 3 元组写法。"""
    if len(ev) >= 4:
        sev = str(ev[3] or "warn")
        return str(ev[0]), str(ev[1]), str(ev[2]), (sev if sev in _SEVERITIES else "warn")
    return str(ev[0]), str(ev[1]), str(ev[2]), "warn"


def _enqueue_pending(inst: dict, s: dict, grp: list, now: int, msg: str) -> None:
    """推送失败的整组事件入待重试队列 + 计数 + 必要时进冷静期（v1.24 带 severity）。"""
    s["push_fail_streak"] = int(s.get("push_fail_streak") or 0) + 1
    pend = list(s.get("pending") or [])
    have = {(p.get("kind"), p.get("body")) for p in pend if isinstance(p, dict)}
    for kind, title, body, sev in grp:
        if (kind, body) not in have:
            pend.append({"kind": kind, "title": title, "body": body, "severity": sev})
            have.add((kind, body))
    s["pending"] = pend[-20:]
    s["retry_count"] = 0
    s["retry_at"] = now + int(cfg.ALERT_RETRY_DELAYS[0])
    if int(s.get("push_fail_streak") or 0) >= int(cfg.ALERT_BACKOFF_STREAK):
        s["push_backoff_until"] = now + int(cfg.ALERT_BACKOFF_SECONDS)
    for kind, title, body, sev in grp:
        _publish_alert(inst, kind, title, body, False, 0,
                       (msg or "推送失败") + "，将自动重试", sev)


def _push_events(inst: dict, s: dict, events_out: list, now: int,
                 busy_kinds=None) -> bool:
    """按静音 / 去重 / 冷静期规则推送；按 severity 分组走各自通道。

    events_out: [(kind, title, body, severity)]（3 元组按 warn 兼容）。
    无论是否推送都会发布事件（供前端展示），未推送的附带 retry_count / 说明 msg。

    busy_kinds：Q-08 —— 本轮 `_process_pending` 已经实际发送过的 kind 集合。
    这些 kind 本轮已占用推送通道（用户刚收到过一条同类告警），此处不再重复
    推送，只发布一条 `pushed=False` 的事件说明「同类型告警本轮已重试推送」。
    只按 kind 抑制而不是整轮丢弃：其它 kind（disk / morning / upstream /
    recovered）与 pending 队列没有竞争，不应被连坐推迟。
    """
    busy = set(busy_kinds or ())
    dedup = int(cfg.ALERT_DEDUP_WINDOW)
    lpk = s.get("last_push_kind_at") or {}
    mute = set(alert_cfg(inst).get("mute") or [])
    sendable: list = []
    for ev in events_out:
        kind, title, body, sev = _ev4(ev)
        if kind in busy:
            # Q-08：本轮已通过重试队列推送过同 kind 告警，避免重复打扰
            _publish_alert(inst, kind, title, body, False, 0,
                           "同类型告警本轮已通过重试队列推送，未重复推送", sev)
            continue
        if sev in mute:
            _publish_alert(inst, kind, title, body, False, 0,
                           f"{sev} 级别已静音，仅记录不推送", sev)
            continue
        if dedup > 0 and not elapsed_ok(now, lpk.get(kind), dedup):
            _publish_alert(inst, kind, title, body, False, 0,
                           "同类型告警在去重窗口内，未重复推送", sev)
            continue
        sendable.append((kind, title, body, sev))
    if not sendable:
        return False
    if before_deadline(now, s.get("push_backoff_until")):
        for kind, title, body, sev in sendable:
            _publish_alert(inst, kind, title, body, False, 0,
                           "推送连续失败，冷静期内暂不推送", sev)
        return False

    pushed_any = False
    for sev in _SEVERITIES:
        grp = [x for x in sendable if x[3] == sev]
        if not grp:
            continue
        r = alert_send("M7A 面板告警", "\n\n".join(b for _, _, b, _ in grp),
                       severity=sev)
        if r.get("ok"):
            pushed_any = True
            ts = int(time.time())
            for kind, _, _, _ in grp:
                lpk[kind] = ts
            s["push_fail_streak"] = 0
            # v1.28：健康看板 —— 记录最近一次成功推送
            s["push_last_ok_at"] = ts
            s["push_last_ok_ms"] = int(time.time() * 1000)   # v1.28.1 同秒排序依据
            for kind, title, body, _ in grp:
                _publish_alert(inst, kind, title, body, True, 0,
                               str(r.get("msg") or "已推送"), sev)
        else:
            # v1.28：健康看板 —— 记录最近一次失败原因（整包推送成功才清空）
            s["push_last_fail_at"] = int(now)
            s["push_last_fail_ms"] = int(time.time() * 1000)  # v1.28.1 同秒排序依据
            s["push_last_fail_msg"] = str(r.get("msg") or "推送失败")
            s["push_last_fail_kind"] = str(grp[0][0] or "")
            _enqueue_pending(inst, s, grp, now, str(r.get("msg") or ""))
    s["last_push_kind_at"] = lpk
    return pushed_any


def _pending_by_severity(pend: list) -> dict:
    """按 severity 分组待重试项；未知/缺失 severity 一律归到 warn。"""
    groups: dict = {}
    for p in pend:
        sev = str(p.get("severity") or "warn")
        if sev not in _SEVERITIES:
            sev = "warn"
        groups.setdefault(sev, []).append(p)
    return groups


def _pending_kinds(pend: list) -> set:
    """本批 pending 覆盖的 kind 集合（无论发送成败都算占用本轮通道）。"""
    return {str(p.get("kind") or "") for p in pend}


def _retry_push(pend: list, groups: dict) -> tuple:
    """按 severity 各发一条合并推送；返回 `(ok_all, fail_msg, fail_kind)`。

    v1.28.1（V28-05）：此前只返回 `bool`，把 `alert_send()` 的 `r["msg"]`
    和 `r["kind"]` 丢掉了 —— 于是 `_retry_failed()` 只能刷新失败**时间**、
    没法刷新失败**原因**，健康看板会一直显示几小时前那次的原因（实测：
    初次失败「Bark 返回 HTTP 400」，之后重试失败原因完全不同，看板却仍显示
    前者，而 `push_last_fail_kind` 变成 None）。

    同一 severity 的多条 pending 合并成一条正文（`\n\n` 分隔），避免刷屏。
    失败原因取**最后一次**失败的通道返回，与「最近一次失败」的语义一致。
    """
    ok_all = True
    fail_msg = ""
    fail_kind = ""
    for sev in _SEVERITIES:
        grp = groups.get(sev) or []
        if not grp:
            continue
        r = alert_send("M7A 面板告警",
                       "\n\n".join(str(p.get("body") or "") for p in grp),
                       severity=sev)
        if not r.get("ok"):
            ok_all = False
            fail_msg = str(r.get("msg") or "")
            fail_kind = str(r.get("kind") or "")
    return ok_all, fail_msg, fail_kind


def _retry_succeeded(inst: dict, s: dict, pend: list, now: int) -> None:
    """重试成功：清空队列、清零失败计数、刷新 last_push_kind_at、留档。"""
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
    s["push_last_ok_at"] = now           # v1.28：重试成功也算通道恢复
    s["push_last_ok_ms"] = int(time.time() * 1000)
    for p in pend:
        _publish_alert(inst, str(p.get("kind") or ""), str(p.get("title") or ""),
                       str(p.get("body") or ""), True, rc, "重试成功",
                       str(p.get("severity") or "warn"))


def _retry_exhausted(inst: dict, s: dict, pend: list, rc: int) -> None:
    """重试次数用尽 → 放弃该批；连续失败够多则进冷静期，避免继续烧推送通道。"""
    s["pending"] = []
    s["retry_at"] = 0
    if int(s.get("push_fail_streak") or 0) >= int(cfg.ALERT_BACKOFF_STREAK):
        s["push_backoff_until"] = int(time.time()) + int(cfg.ALERT_BACKOFF_SECONDS)
    for p in pend:
        _publish_alert(inst, str(p.get("kind") or ""), str(p.get("title") or ""),
                       str(p.get("body") or ""), False, rc,
                       "重试次数用尽，已放弃推送",
                       str(p.get("severity") or "warn"))


def _retry_failed(inst: dict, s: dict, pend: list, now: int,
                  fail_msg: str = "", fail_kind: str = "") -> None:
    """重试失败：累加计数，按 30/60/120 节奏排下一次；用尽则转 _retry_exhausted。

    v1.28.1（V28-05）：一并回填失败**原因/类型**（不只时间），否则健康看板
    展示的「最近失败原因」会停留在很久以前那次。
    """
    rc = int(s.get("retry_count") or 0) + 1
    s["retry_count"] = rc
    s["push_fail_streak"] = int(s.get("push_fail_streak") or 0) + 1
    # v1.28：健康看板 —— 重试失败也更新最近失败时间
    s["push_last_fail_at"] = int(now)
    s["push_last_fail_ms"] = int(time.time() * 1000)
    if fail_msg:
        s["push_last_fail_msg"] = fail_msg
        s["push_last_fail_kind"] = fail_kind
    delays = list(cfg.ALERT_RETRY_DELAYS)
    if rc >= int(cfg.ALERT_MAX_RETRY) or rc >= len(delays):
        _retry_exhausted(inst, s, pend, rc)
        return
    nxt = int(delays[rc])
    s["retry_at"] = now + nxt
    for p in pend:
        _publish_alert(inst, str(p.get("kind") or ""), str(p.get("title") or ""),
                       str(p.get("body") or ""), False, rc,
                       f"推送失败，{nxt} 秒后第 {rc} 次重试",
                       str(p.get("severity") or "warn"))


def _process_pending(inst: dict, s: dict, now: int) -> set:
    """处理待重试队列。返回本轮【实际尝试发送】的 kind 集合。

    Q-08：返回值从 bool 改为 kind 集合，因为「有 pending」与「本轮真的发了推送」
    是两件事：
    - 队列非空但 `retry_at` 未到（还在退避等待）→ 本轮没有发出任何推送，
      返回空集，后续新边沿照常走自己的通道；
    - 本轮真的重试了某些 kind（成功或失败都算发过）→ 这些 kind 已占用本轮
      推送通道，返回给 `_push_events` 做**按 kind 去重**，避免同一轮既重试
      pending、又对刚观测到的同类边沿再推一条（`ALERT_DEDUP_WINDOW=0` 时
      用户会收到两条重复告警）。
    只抑制同 kind、不整轮跳过，是因为 disk/morning/upstream/recovered 等
    其它 kind 与 pending 队列无竞争，整轮跳过会让它们凭空晚一轮（基线用例
    `test_v122.py::test_alert_backoff_suppresses_new_push` 就依赖冷静期内
    recovered 仍被记录）。

    重试节奏：30s → 60s → 120s，最多 ALERT_MAX_RETRY 次；成功即清除并清零失败计数。
    v1.24：按 severity 分组重试，各自走对应级别通道。
    v1.24.7（方向D）：拆成「分组 / 推送 / 成功分支 / 失败分支」四个小函数，
    本函数只保留「该不该发、发完记什么」的流程，语义与拆分前逐行等价。
    """
    pend = [p for p in (s.get("pending") or []) if isinstance(p, dict)]
    if not pend:
        return set()
    if before_deadline(now, s.get("retry_at")):
        # 仍在退避等待期：本轮不发推送，因此不占用任何 kind
        return set()
    # 说明：重试队列不受冷静期阻断——重试本身已被 30/60/120 限次约束；
    # 冷静期只用于抑制「新的」告警推送（见 _push_events）。

    # Q-08：本批实际会发出推送的 kind 集合（无论成败都算占用通道）
    sent_kinds = _pending_kinds(pend)
    ok_push, fail_msg, fail_kind = _retry_push(pend, _pending_by_severity(pend))
    if ok_push:
        _retry_succeeded(inst, s, pend, now)
    else:
        _retry_failed(inst, s, pend, now, fail_msg, fail_kind)
    return sent_kinds


def _down_body(first: bool = False, delayed: bool = False) -> str:
    """容器掉线的推送正文。

    Q-04：`first=True` 表示「首次观测就是 down」（面板刚接管 / 状态文件被重置 /
    容器从未起来过），文案要说明这不是「从运行中掉下去」，否则用户会以为是
    刚发生的中断。`delayed=True` 表示该边沿发生在静默期内、现在补发（P0-4）。
    """
    body = "🛑 容器意外停止\n面板检测到容器已不在运行，任务可能中断。"
    if first:
        body += "\n（首次检测：面板接管时容器即未运行，或告警状态被重置过）"
    if delayed:
        body += "\n（该故障发生在告警静默期内，现已补发提醒）"
    return body


def _observe_unhealthy_edge(inst: dict, s: dict, run: bool, quiet: bool,
                            events_out: list) -> None:
    """检测 healthcheck unhealthy 边沿（v1.28.2）。

    与 `_observe_down_edge` 的 down 边沿互补：容器 `Running=True` 但内部
    healthcheck 连续失败时，面板原先完全无感。典型场景是 m7a 的
    `pgrep -f 'python main.py'` 探测连续 3 次失败（主进程卡死/被内核干掉但
    容器还没退出），或未来换成 HTTP/业务探针后任务链路彻底断掉。

    独立记账字段 `alerted_unhealthy`，与 `alerted_down` 互不干扰：
    - 只有 run=True 时才评估（容器停了这个边沿归 down 管，避免同一故障推两条）
    - unhealthy → healthy 时推一条 recovered
    - 静默期同 down 一致：先记 `pending_unhealthy`，结束后补推

    为什么 event kind 单独用 `unhealthy`/`unhealthy_recovered` 而不是复用
    down/recovered：面板的推送文案、severity 分级、告警历史筛选都按 kind 分派，
    复用会让用户看到「容器意外停止」而实际容器在跑，属误导。
    """
    if not run:
        # 容器没在跑：down 边沿自己会报，这里不重复；同时清掉 unhealthy 记账，
        # 免得容器重启后带着上次的 unhealthy 状态一路误报。
        return
    try:
        from services.monitor import container_health
        h = container_health(inst)
    except Exception:
        return
    health = str(h.get("health") or "")
    if not health:
        # 容器没配 healthcheck（旧版 compose）：不评估，保持历史行为
        return
    unhealthy = health == "unhealthy"
    s["prev_unhealthy"] = unhealthy          # 仅记录，供下次 tick / 排查用
    if unhealthy and not s.get("alerted_unhealthy"):
        if quiet:
            s["pending_unhealthy"] = True
            log.info("静默期内检测到容器 unhealthy（连续失败 %s 次），"
                     "已记 pending_unhealthy", h.get("failing"))
        else:
            events_out.append(("unhealthy", "容器内部异常",
                               _unhealthy_body(h), "critical"))
            s["alerted_unhealthy"] = True
    elif (not unhealthy) and s.get("alerted_unhealthy"):
        events_out.append(("unhealthy_recovered", "容器已恢复健康",
                           "✅ 容器内部健康检查恢复正常\n容器已在运行且探针通过。",
                           "info"))
        s["alerted_unhealthy"] = False


def _unhealthy_body(h: dict) -> str:
    """unhealthy 推送正文：把「容器还活着但内部坏了」讲清楚，避免和 down 混淆。"""
    fails = int(h.get("failing") or 0)
    body = ("⚠️ 容器内部健康检查失败\n"
            "容器进程仍在运行，但健康探针连续失败，任务可能已经卡住或反复报错。\n"
            "建议到面板看日志，必要时重启容器。")
    if fails:
        body += f"\n连续失败次数：{fails}"
    if h.get("oom_killed"):
        body += "\n注意：上一次退出是被 OOM 杀掉的，内存可能仍然不够。"
    if h.get("restarting"):
        body += "\n注意：容器处于 restarting 状态。"
    return body


def _observe_down_edge(s: dict, prev, run, quiet: bool, events_out: list) -> None:
    """检测 up→down 边沿（含 Q-04 的「首次观测即 down」），按静默期决定记账还是推送。

    Q-04：down 边沿判据不能只认 prev is True。面板首次接管时容器就没起来
    （或 alert_state.json 被清空/重置），prev 初始为 None，第一轮 tick 写入
    prev=False 后永远是 False → False，down 告警永不触发 —— 用户以为
    「没消息=一切正常」，实际容器根本没跑。因此「首次观测即 down」也算一次
    down 边沿。反例不受影响：prev is None and run is True 只是首次正常，不告警。
    """
    first_down = (prev is None and run is False)
    down_edge = ((prev is True or first_down) and run is False
                 and not s.get("alerted_down"))
    if down_edge:
        if quiet:
            # P0-4：静默期只是延后推送，边沿先记账，静默期结束补推
            s["pending_down"] = True
            s["pending_down_first"] = bool(first_down)
            log.info("静默期内检测到容器掉线（prev=%r），已记 "
                     "pending_down，静默期结束后补推", prev)
        else:
            events_out.append(("down", "容器意外停止",
                               _down_body(first_down), "critical"))
            s["alerted_down"] = True
    elif prev is False and run is True and s.get("alerted_down"):
        body = "✅ 容器已恢复\n容器已重新运行，可以正常跑任务了。"
        events_out.append(("recovered", "容器已恢复", body, "info"))
        s["alerted_down"] = False


def _flush_pending_down(s: dict, run, quiet: bool, events_out: list) -> None:
    """静默期结束后补推被压下的 down 边沿（P0-4 核心补偿）。

    静默期内已自行恢复（或状态不可判定）→ 边沿失效，直接丢弃。
    """
    if (not quiet and s.get("pending_down")
            and run is False and not s.get("alerted_down")):
        events_out.append(("down", "容器意外停止",
                           _down_body(bool(s.get("pending_down_first")),
                                      delayed=True), "critical"))
        s["alerted_down"] = True
        s["pending_down"] = False
        s["pending_down_first"] = False
    elif not quiet and s.get("pending_down"):
        s["pending_down"] = False
        s["pending_down_first"] = False


def _aborted_body(label: str, delayed: bool = False) -> str:
    body = f"⚠️ 任务中断：{label}\n请到面板「日志」页查看原因。"
    if delayed:
        body += "\n（发生在告警静默期内，现已补发提醒）"
    return body


def _scan_aborted(inst: dict, s: dict, quiet: bool, events_out: list) -> None:
    """扫描历史里的 aborted 任务，去重后推「任务中断」；静默期先记 pending_aborted。

    P1-16：静默期内不丢弃边沿，先记待补推（原先直接 continue → 永久丢失）。
    """
    hd = history_sync(inst)
    aborted_seen = list(s.get("aborted_seen") or [])
    pend_ab = [x for x in (s.get("pending_aborted") or []) if isinstance(x, dict)]
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
        label = str(it.get("task_label") or "任务")
        if quiet:
            if not any(int(x.get("id") or 0) == iid for x in pend_ab):
                pend_ab.append({"id": iid, "label": label})
            continue
        events_out.append(("aborted", f"任务中断：{label}",
                           _aborted_body(label), "warn"))
    if len(aborted_seen) > 40:
        aborted_seen = aborted_seen[-40:]
    s["aborted_seen"] = aborted_seen
    if not quiet and pend_ab:
        # 静默期结束：补推期间积压的中断通知
        for x in pend_ab:
            label = str(x.get("label") or "任务")
            events_out.append(("aborted", f"任务中断：{label}",
                               _aborted_body(label, delayed=True), "warn"))
        pend_ab = []
    s["pending_aborted"] = pend_ab[-20:]


def alert_tick(inst: dict, force: bool = False) -> dict:
    """巡检状态机：up→down 推故障、down→up 推恢复、aborted 记录推任务中断。
    M4：每次真正产生告警时发布 alert 事件（WS 实时推送 + SQLite 留档）。
    v1.22：每次 tick 先处理待重试队列，再走状态机检测（推送含去重/退避）。
    v1.24：告警分级（down=critical / aborted=warn / recovered=info），并把
    磁盘探针、每日晨报、上游版本检查挂到同一条巡检链路上（各自内部节流）。

    v1.24.4 修复：
    - P0-5：整个「load → 状态机 → save」临界区持 `_STATE_LOCK`，避免与
      alert_quiet / disk_check / health_tick / 页面兜底并发时互相覆盖。
    - P0-4：静默期内观测到的 down 边沿不再丢弃，记 `pending_down`，静默期
      结束后补推（原先 `and not quiet` 直接跳过且不置 alerted_down，而 prev
      已写 False → 恢复后 prev is True 永不成立 → 永久不再告警）。
    - P1-16：aborted 同理，静默期内记 `pending_aborted`，结束后补推。
    - P1-20：`last_tick` 节流改用 elapsed_ok，时钟回拨不再永久停摆。

    v1.24.7（方向D）：原 145 行 / CC=45 的单体函数按职责拆成
    `_process_pending`（重试队列）、`_observe_down_edge`（down/recovered 边沿）、
    `_flush_pending_down`（静默期补偿）、`_scan_aborted`（中断扫描）。
    本函数只保留「节流 → 各阶段按序执行 → 落盘」的编排，语义逐行等价。
    """
    with _STATE_LOCK:
        s = alert_state_load(inst)
        now = int(time.time())
        if not force and not elapsed_ok(now, s.get("last_tick"), _TICK_IV):
            return {"ok": True, "throttled": True, "pushed": False}
        s["last_tick"] = now

        # 1) 先处理待重试队列（失败告警的退避重试）
        # Q-08：此前调用处丢弃返回值，于是同一轮既重试了 pending、又对同一轮
        # 观测到的同类边沿另起一次 _push_events —— 用户实测（去重窗口设 0 时）
        # 收到两条重复告警。现在 _process_pending 返回「本轮真的发送过的 kind」，
        # 交给 _push_events 做按 kind 抑制。
        #
        # 为什么是「按 kind 抑制」而不是任务书建议的「整轮跳过」：基线用例
        # test_v122.py::test_alert_backoff_suppresses_new_push 明确要求冷静期
        # （必然伴随 pending）内观测到的 recovered 仍要发布事件（pushed=False
        # + 「冷静期」说明）。整轮跳过会把 recovered / disk / morning /
        # upstream 一起推迟一轮，既破坏该用例，也让这些与 pending 无竞争的
        # 通道无故延迟。因此只抑制「本轮已重试过的同 kind」，其它照常。
        busy_kinds = _process_pending(inst, s, now)

        run = container_is_running(inst)
        prev = s.get("prev")
        cfgv = alert_cfg(inst)
        pushed = False
        s["prev"] = run
        if cfgv["enable"]:
            quiet = before_deadline(now, s.get("quiet_until"))
            events_out: list = []   # (kind, title, body, severity)
            _observe_down_edge(s, prev, run, quiet, events_out)
            # v1.28.2：容器 Running 但 healthcheck unhealthy 也算故障边沿
            _observe_unhealthy_edge(inst, s, run, quiet, events_out)
            _flush_pending_down(s, run, quiet, events_out)
            _scan_aborted(inst, s, quiet, events_out)
            if events_out:
                pushed = _push_events(inst, s, events_out, now,
                                      busy_kinds=busy_kinds)
        saved = alert_state_save(inst, s)
        # P1-17：写盘失败时不能假装成功——内存态与磁盘不一致会让 prev 回退
        # 到旧值，这里明确回传，调用方可据此提示「状态机降级」。
        if not saved:
            log.warning("alert_tick：状态未能落盘，本轮 prev=%r 不会持久化", run)

    # 2) M2 定时巡检：磁盘（6 分钟）/ 晨报（判到点）/ 上游版本（6 小时）
    #    先落盘再调用，避免它们各自的读写覆盖本轮的 last_tick / alerted_down。
    try:
        health_tick(inst)
    except Exception:
        # 业务逻辑（不是清理）：health_tick 的磁盘/晨报/上游三路巡检各有内部兜底，
        # 走到这里说明是兜底之外的意外（如状态文件损坏、第三方库异常）。
        # 它不该让 alert_tick 整体失败，但必须可见 —— 静默吞会让「巡检长期不跑」
        # 完全无迹可查（P1-17 同类问题）。故意保留宽 Exception：这是最后一道
        # 安全网，收窄反而会漏掉未知故障。
        log.warning("alert_tick：health_tick 巡检异常（本轮告警推送不受影响）",
                    exc_info=True)

    return {"ok": True, "pushed": pushed, "run": run,
            "msg": "" if not pushed else "已推送",
            "pending": len(s.get("pending") or []),
            "retry_count": int(s.get("retry_count") or 0),
            "state_saved": saved}


# ===== v1.24 M2-6：磁盘空间预警 =====

def disk_targets(inst: dict) -> list:
    """探针路径：配置的挂载点（默认 /）+ 实例目录，去重后返回。"""
    cands = [str(cfg.DISK_PROBE_PATH or "/")]
    try:
        d = str(instance_dir(inst) or "")
        if d:
            cands.append(d)
    except Exception:
        pass
    out, seen = [], set()
    for p in cands:
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return out


def disk_status(inst: dict) -> dict:
    """跑一遍磁盘探针，返回剩余比例最低的那块盘 + 全部探针结果。"""
    from services.monitor import disk_probe
    probes = []
    for p in disk_targets(inst):
        try:
            r = disk_probe(p)
        except Exception as e:
            r = {"ok": False, "path": p, "msg": str(e)}
        if not isinstance(r, dict):
            r = {"ok": False, "path": p}
        probes.append(r)
    oks = [r for r in probes if r.get("ok")]
    worst = None
    for r in oks:
        try:
            pct = int(r.get("percent"))
        except (TypeError, ValueError):
            continue
        if worst is None or pct < int(worst.get("percent") or 100):
            worst = r
    return {"ok": worst is not None, "worst": worst, "probes": probes}


def disk_fmt(n) -> str:
    try:
        gb = float(n or 0) / (1024 ** 3)
    except (TypeError, ValueError):
        gb = 0.0
    return f"{gb:.1f} GB"


def disk_line(worst: dict) -> str:
    return (f"{worst.get('path')} 剩余 {worst.get('percent')}%"
            f"（可用 {disk_fmt(worst.get('avail'))} / 共 {disk_fmt(worst.get('total'))}）")


def disk_check(inst: dict, force: bool = False) -> dict:
    """磁盘探针 + 跨阈值告警（warn/critical），恢复正常发 info。

    只在「级别发生变化」时推送，避免同一状态反复轰炸。
    """
    with _STATE_LOCK:
        return _disk_check_locked(inst, force)


def _disk_check_locked(inst: dict, force: bool = False) -> dict:
    s = alert_state_load(inst)
    now = int(time.time())
    c = alert_cfg(inst)
    st = disk_status(inst)
    worst = st.get("worst")
    if not worst:
        return {"ok": False, "msg": "磁盘探针不可用（df 读取失败或路径不存在）",
                "worst": None, "level": ""}
    try:
        pct = int(worst.get("percent"))
    except (TypeError, ValueError):
        pct = 100
    if pct < int(c["disk_crit"]):
        lvl = "critical"
    elif pct < int(c["disk_warn"]):
        lvl = "warn"
    else:
        lvl = ""
    prev = str(s.get("disk_level") or "")

    events_out: list = []
    if c["enable"] and c["disk_enable"] and lvl and (force or lvl != prev):
        name = "磁盘空间严重不足" if lvl == "critical" else "磁盘空间不足"
        thr = int(c["disk_crit"] if lvl == "critical" else c["disk_warn"])
        body = (f"💾 {name}\n{disk_line(worst)}\n剩余已低于 {thr}%，"
                f"请清理旧日志 / 备份 / 镜像后再跑任务。")
        # kind 按级别拆分：warn↔critical 跨级切换不被 120s 去重窗口吞掉
        events_out.append(("disk_" + lvl, name, body, lvl))
    elif c["enable"] and prev and not lvl and not force:
        body = f"✅ 磁盘空间已恢复\n{disk_line(worst)}"
        events_out.append(("disk_ok", "磁盘空间已恢复", body, "info"))

    pushed = _push_events(inst, s, events_out, now) if events_out else False
    s["disk_level"] = lvl
    s["disk_path"] = str(worst.get("path") or "")
    alert_state_save(inst, s)
    return {"ok": True, "pushed": pushed, "level": lvl,
            "worst": worst, "probes": st.get("probes")}


# ===== v1.24 M2-7：每日健康晨报 =====

def _yday_window(now: int) -> tuple:
    """返回「昨日」的本地日期字符串与 [y_start, y_end] unix 秒闭区间。

    用 time.localtime/mktime 取本地日历日（不是 now-86400 的近似），
    夏令时切换日也能得到正确的自然日边界；mktime 溢出时退回近似窗口。
    """
    lt = time.localtime(now - 86400)
    yday = time.strftime("%Y-%m-%d", lt)
    try:
        y_start = int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday,
                                   0, 0, 0, 0, 0, -1)))
    except (OverflowError, ValueError):
        y_start = now - 86400
    return yday, y_start, y_start + 86400 - 1


def _yday_task_stats(inst: dict, y_start: int, y_end: int) -> dict:
    """统计昨日任务：完成 / 中断 / 进行中 / 其它。history 不可用时全 0。"""
    done = aborted = running = other = 0
    try:
        items = list(history_sync(inst).get("items") or [])
    except Exception:
        # 业务降级：晨报是「锦上添花」的通知，历史读不出来就按 0 统计，
        # 不能让整条晨报发不出去（否则用户连磁盘/存活信息也收不到）。
        log.debug("morning_report：读取历史失败，任务统计按 0 计", exc_info=True)
        items = []
    for it in items:
        try:
            ts = int(it.get("start_ts") or 0)
        except (TypeError, ValueError):
            ts = 0
        if not (y_start <= ts <= y_end):
            continue
        stt = str(it.get("status") or "")
        if stt == "done":
            done += 1
        elif stt == "aborted":
            aborted += 1
        elif stt == "running":
            running += 1
        else:
            other += 1
    return {"done": done, "aborted": aborted, "running": running,
            "other": other, "total": done + aborted + running + other}


def _yday_alert_stats(y_start: int, y_end: int) -> dict:
    """统计昨日真正推送出去的告警条数与分级。eventdb 不可用时全 0。"""
    cnt = {"info": 0, "warn": 0, "critical": 0}
    al_total = 0
    try:
        from services import eventdb
        evs = eventdb.list_events("alert", limit=500, frm=y_start, to=y_end)
        for e in (evs or []):
            # v1.24.4（P2）：list_events 已把 payload 展开成顶层字段
            # （eventdb.py:150 `**body`），原先取 e["payload"] 恒为 None →
            # 晨报「昨日告警条数」永远是 0。这里直接读展开后的 dict。
            if not isinstance(e, dict) or not e.get("pushed"):
                continue
            al_total += 1
            sev = str(e.get("severity") or "warn")
            if sev in cnt:
                cnt[sev] += 1
    except Exception:
        # 业务降级：事件库读失败只影响「昨日告警」这一行，其余晨报照发。
        log.debug("morning_report：读取告警事件失败，按 0 计", exc_info=True)
    return {"total": al_total, "by_sev": cnt}


def _yday_disk_text(inst: dict) -> str:
    """磁盘探针文本；探针不可用/未到检查时间返回「磁盘探针不可用」。"""
    try:
        st = disk_status(inst)
        w = st.get("worst")
        if w:
            return disk_line(w)
    except Exception:
        log.debug("morning_report：磁盘探针异常", exc_info=True)
    return "磁盘探针不可用"


def morning_report_body(inst: dict) -> str:
    """聚合昨日任务完成情况 / 告警数 / 磁盘剩余 / 本体是否存活，纯文本正文。

    v1.24.7（方向D）：原 96 行 / CC=30，拆出 `_yday_window`（时间窗）、
    `_yday_task_stats`、`_yday_alert_stats`、`_yday_disk_text` 四个取数函数，
    本函数只负责拼装正文。每一路取数都自带降级（失败即按「不可用/0」处理），
    保证晨报不会因为某一路探针异常而整条发不出去。
    """
    now = int(time.time())
    yday, y_start, y_end = _yday_window(now)

    ts = _yday_task_stats(inst, y_start, y_end)
    al = _yday_alert_stats(y_start, y_end)
    cnt = al["by_sev"]
    disk_txt = _yday_disk_text(inst)

    # 本体存活
    try:
        alive = bool(container_is_running(inst))
    except Exception:
        log.debug("morning_report：容器状态探测异常", exc_info=True)
        alive = False
    cname = str(inst.get("container") or cfg.DEFAULT_CONTAINER)

    lines = [
        f"🌅 M7A 每日健康晨报（{yday} 汇总）",
        "────────────────",
        f"【昨日任务】共 {ts['total']} 个：完成 {ts['done']} / 中断 {ts['aborted']}"
        f" / 进行中 {ts['running']}",
        f"【告警通知】{al['total']} 条（critical {cnt['critical']} / warn {cnt['warn']}"
        f" / info {cnt['info']}）",
        f"【磁盘空间】{disk_txt}",
        f"【本体状态】容器「{cname}」{'运行中 ✅' if alive else '未运行 ❌'}",
    ]
    try:
        us = upstream_status(inst)
    except Exception:
        log.debug("morning_report：上游版本查询异常", exc_info=True)
        us = {}
    if us.get("latest"):
        if us.get("has_update"):
            lines.append(f"【版本提醒】v{us.get('local') or '?'} → 有新版本 "
                         f"v{us.get('latest')}，需要时再更新")
        else:
            lines.append(f"【版本提醒】v{us.get('local') or us.get('latest')} 已是最新")
    lines.append("────────────────")
    lines.append("本条由面板自动汇总，可在「异常告警」卡片关闭或改时间。")
    return "\n".join(lines)


def morning_report(inst: dict, force: bool = False) -> dict:
    """到点发一次晨报；同一本地日期只发一次（force=True 供「立即发送」按钮）。"""
    with _STATE_LOCK:
        return _morning_report_locked(inst, force)


def _morning_report_locked(inst: dict, force: bool = False) -> dict:
    s = alert_state_load(inst)
    c = alert_cfg(inst)
    if not c["enable"]:
        return {"ok": False, "msg": "告警未启用"}
    if not c["morning_enable"] and not force:
        return {"ok": False, "msg": "每日晨报已关闭"}
    today = time.strftime("%Y-%m-%d")
    if not force:
        if str(s.get("morning_last") or "") == today:
            return {"ok": False, "msg": "今日晨报已发送"}
        try:
            th, tm = [int(x) for x in str(c["morning_time"]).split(":")[:2]]
        except ValueError:
            th, tm = 8, 0
        cur = int(time.strftime("%H")) * 60 + int(time.strftime("%M"))
        target = th * 60 + tm
        # 允许 4 小时补发窗口（面板夜间没开 / 服务器重启过）
        if not (target <= cur <= target + 240):
            return {"ok": False, "msg": "未到晨报时间"}

    body = morning_report_body(inst)
    r = alert_send("M7A 每日健康晨报", body, severity="info")
    ok = bool(r.get("ok"))
    if ok:
        s["morning_last"] = today
        alert_state_save(inst, s)
    _publish_alert(inst, "morning", "每日健康晨报", body, ok, 0,
                   str(r.get("msg") or ""), "info")
    return {"ok": ok, "msg": r.get("msg", ""), "body": body}


# ===== v1.24 M2-8：上游本体更新提醒 =====

def _norm_ver(txt: str) -> str:
    """从任意文本里抠出版本数字部分：'v2026.10.3' → '2026.10.3'。"""
    m = re.search(r"(\d+(?:\.\d+)+)", str(txt or ""))
    return m.group(1) if m else ""


def local_assistant_version(inst: dict) -> str:
    """读本地本体版本（数字部分）。读不到返回 ""，调用方必须跳过以免误报。"""
    d = ""
    try:
        d = str(instance_dir(inst) or "")
    except Exception:
        d = ""
    if d:
        for name in ("version.txt", "assets/config/version.txt"):
            try:
                p = Path(d) / name
                if p.is_file():
                    v = _norm_ver(p.read_text("utf-8", "ignore"))
                    if v:
                        return v
            except OSError:
                pass
    cname = str(inst.get("container") or "")
    if not cname:
        return ""
    try:
        from services.shell import run_cmd
        r = run_cmd(f"docker exec {shlex.quote(cname)} "
                    f"cat /m7a/assets/config/version.txt", timeout=8)
    except Exception:
        return ""
    if isinstance(r, dict):
        try:
            if int(r.get("code") or 0) != 0:
                return ""
        except (TypeError, ValueError):
            pass
    return _norm_ver(str((r or {}).get("out") or ""))


def upstream_status(inst: dict) -> dict:
    """只读缓存：面板角标与晨报用，绝不触发网络请求。"""
    s = alert_state_load(inst)
    local = str(s.get("upstream_local") or "")
    latest = str(s.get("upstream_latest") or "")
    return {
        "local": local,
        "latest": latest,
        "has_update": bool(local and latest and version_gt(latest, local)),
        "error": str(s.get("upstream_error") or ""),
        "last_check": int(s.get("upstream_last_check") or 0),
        "url": str(cfg.UPSTREAM_RELEASE_URL),
    }


def upstream_check(inst: dict, force: bool = False) -> dict:
    """查上游最新 Release 与本地本体版本比对；有新版推 warn 级提醒（仅提醒不升级）。"""
    with _STATE_LOCK:
        return _upstream_check_locked(inst, force)


def _upstream_check_locked(inst: dict, force: bool = False) -> dict:
    s = alert_state_load(inst)
    now = int(time.time())
    c = alert_cfg(inst)
    if not (c["enable"] and c["upstream_enable"]):
        out = upstream_status(inst)
        out.update({
            "ok": False, "cached": False, "pushed": False,
            "msg": "告警未启用" if not c["enable"] else "本体更新提醒已关闭",
        })
        return out
    if not force and not elapsed_ok(now, s.get("upstream_last_check"),
                                    int(cfg.UPSTREAM_CHECK_IV)):
        out = upstream_status(inst)
        out["cached"] = True
        out["pushed"] = False
        out["ok"] = not out.get("error")
        return out

    local = local_assistant_version(inst)
    latest, err = "", ""
    try:
        r = http_get(str(cfg.UPSTREAM_API), int(cfg.UPSTREAM_HTTP_TIMEOUT))
        code = int(r.get("code") or 0)
        body = str(r.get("body") or "")
        d = json.loads(body) if body.strip().startswith("{") else {}
        latest = _norm_ver(str(d.get("tag_name") or d.get("name") or ""))
        if not latest:
            err = f"上游返回异常（HTTP {code}）"
    except Exception as e:
        err = f"上游请求失败：{e}"

    has_update = bool(local and latest and version_gt(latest, local))
    pushed = False
    # 两个条件合并成一个 if（SIM102）：语义完全不变 —— 上游提醒开关 + 有新版本，
    # 且（强制 或 这个版本还没提醒过）。
    if (c["enable"] and c["upstream_enable"] and has_update
            and (force or str(s.get("upstream_notified") or "") != latest)):
        title = "M7A 本体有新版本"
        body = (f"🆕 M7A 本体有新版本\n当前：v{local}\n最新：v{latest}\n"
                f"仅提醒、不会自动升级；需要时到本体目录执行：\n"
                f"docker compose pull && docker compose up -d\n"
                f"{cfg.UPSTREAM_RELEASE_URL}")
        pushed = _push_events(inst, s, [("upstream", title, body, "warn")], now)
        if pushed:
            s["upstream_notified"] = latest

    s["upstream_last_check"] = now
    s["upstream_local"] = local
    s["upstream_latest"] = latest
    s["upstream_error"] = err
    alert_state_save(inst, s)
    return {"ok": not err, "cached": False, "pushed": pushed, "local": local,
            "latest": latest, "has_update": has_update, "error": err,
            "last_check": now, "url": str(cfg.UPSTREAM_RELEASE_URL)}


# ===== v1.24 M2：统一巡检入口 =====

def health_tick(inst: dict) -> dict:
    """磁盘 / 晨报 / 上游版本的统一巡检入口（各自内部节流）。

    被 sampler.runner（60s）与 alert_tick（cron / 打开页面兜底）共同驱动，
    重复调用安全：磁盘 6 分钟一次、上游 6 小时一次、晨报每天一次。
    """
    # v1.24.4（P0-5）：health_tick 自身也是一次「读→改→写」，必须与
    # alert_tick / disk_check / 页面兜底串行化；RLock 允许 alert_tick 内部重入。
    with _STATE_LOCK:
        return _health_tick_locked(inst)


def _health_tick_locked(inst: dict) -> dict:
    out: dict = {}
    s = alert_state_load(inst)
    now = int(time.time())
    if elapsed_ok(now, s.get("disk_last_check"), int(cfg.DISK_CHECK_IV)):
        s["disk_last_check"] = now
        alert_state_save(inst, s)
        try:
            out["disk"] = disk_check(inst)
        except Exception as e:
            out["disk"] = {"ok": False, "msg": str(e)}
    try:
        out["morning"] = morning_report(inst)
    except Exception as e:
        out["morning"] = {"ok": False, "msg": str(e)}
    try:
        out["upstream"] = upstream_check(inst)
    except Exception as e:
        out["upstream"] = {"ok": False, "msg": str(e)}
    # v1.27：错误循环预警（60s 节流，自身有 force）
    try:
        out["loop"] = loop_patrol(inst)
    except Exception as e:
        out["loop"] = {"ok": False, "msg": str(e)}
    return out


# ===== v1.27：错误循环预警 =====

# 判定「重复错误」的行模式（命中任意一条即计数；窗口内 >= 阈值触发告警）。
# 2026-10-31 事故模板：云游戏卡加载页后每秒刷「截图失败：没有找到游戏窗口」、
# 每 ~23 分钟一次「截图超时」，日志 171k 行全部是这类重复 → 面板静默 11 小时。
LOOP_ERROR_RES = (
    r"截图失败",
    r"没有找到游戏窗口",
    r"截图超时",
    r"当前界面：未知",
    r"获取当前界面超时",
)
_LOOP_ERROR_RE = re.compile("|".join(LOOP_ERROR_RES))


def _loop_body(inst: dict, cnt: int, window_min: int) -> str:
    path = latest_log_path(inst)
    name = os.path.basename(path) if path else "日志"
    return (
        f"最近 {window_min} 分钟内，{name} 出现 {cnt} 条重复错误"
        f"（截图失败 / 超时 / 未识别界面等）。\n"
        f"这通常意味着本体陷入异常循环（如云游戏卡加载页、OCR 全失败）。\n"
        f"建议：打开面板「日志」页查看，或重启容器。"
    )


def loop_patrol(inst: dict, force: bool = False) -> dict:
    """错误循环检测：统计本体日志窗口内的重复错误密度，超阈值推 warn 告警。

    与磁盘/晨报/上游同一套状态管理（_ALERT_DEFAULT 里的 loop_last_check /
    loop_last_count），节流 cfg.LOOP_ALERT_CHECK_IV（60s）由抽查端点与
    alert_tick 共同驱动，重复调用安全。默认关闭（cfg 默认 loop_alert_enable=0）。
    """
    with _STATE_LOCK:
        s = alert_state_load(inst)
        now = int(time.time())
        cfgv = alert_cfg(inst)
        if not cfgv.get("loop_enable"):
            return {"ok": True, "enabled": False}
        window_min = int(cfgv.get("loop_window") or cfg.LOOP_ALERT_WINDOW)
        min_count = int(cfgv.get("loop_min_count") or cfg.LOOP_ALERT_MIN_COUNT)
        if not force and not elapsed_ok(now, s.get("loop_last_check"),
                                        int(cfg.LOOP_ALERT_CHECK_IV)):
            return {"ok": True, "throttled": True, "count": int(s.get("loop_last_count") or 0)}
        s["loop_last_check"] = now

        # 读最新日志尾部（filter_log 内部做 tail，这里只取整行文本）
        path = latest_log_path(inst)
        if not path:
            return {"ok": True, "checked": False, "count": 0}
        r = filter_log(path, {"lines": 2000})
        lines = r.get("lines") or []

        # 只统计窗口时间窗内的行（行首 %Y-%m-%d %H:%M:%S）
        cutoff = now - window_min * 60
        cnt = 0
        for line in lines:
            if not _LOOP_ERROR_RE.search(line):
                continue
            ts = None
            try:
                ts = datetime.strptime(line[:19], "%Y-%m-%d %H:%M:%S").timestamp()
            except ValueError:
                pass
            if ts is None or ts >= cutoff:
                cnt += 1
        s["loop_last_count"] = cnt

        alerted = False
        if cnt >= min_count:
            lpk = s.setdefault("last_push_kind_at", {})
            # 去重窗口：同一 kind 在 ALERT_DEDUP_WINDOW 内不重复推送
            if elapsed_ok(now, lpk.get("error_loop"), int(cfg.ALERT_DEDUP_WINDOW)):
                pushed = _push_events(
                    inst, s,
                    [("error_loop", "M7A 本体疑似异常循环",
                      _loop_body(inst, cnt, window_min), "warn")],
                    now,
                )
                alerted = bool(pushed)
        alert_state_save(inst, s)
        return {"ok": True, "enabled": True, "count": cnt, "alerted": alerted}


# ===== 推送通道健康看板（v1.28）=====

def alert_health(inst: dict) -> dict:
    """汇总告警通道的健康状态，供配置页顶部健康条展示。

    这个函数存在的理由：告警系统失效时**它自己是不会告警的**。线上曾经发生过
    `push_fail_streak=8`、`last_push_kind_at={}`（一次都没成功过）、Bark 一直回
    HTTP 400 却无人知晓的情况——用户以为「没收到告警就是没事」，实际是推送根本
    没发出去。把状态摊到页面上，让「通道坏了」变成一眼可见。

    返回：
      level: ok / warn / crit / off / setup
        - off   告警未启用
        - setup 已启用但没填接收地址（配了也没用）
        - crit  连续失败达到冷静期阈值，或最近一次推送失败且从未成功过
        - warn  最近一次是失败 / 还在重试队列里
        - ok    最近一次成功
      label / color / detail / last_ok / last_fail / streak / pending / channel
    """
    c = panel_config()
    enabled = str(c.get("alert_enable") or "") == "1"
    target = str(c.get("alert_target") or "").strip()
    ch = str(c.get("alert_channel") or "bark").strip() or "bark"
    if ch not in _CHANNELS:
        ch = "bark"
    out = {
        "enabled": enabled,
        "channel": ch,
        "channel_label": _CHANNEL_LABEL.get(ch, ch),
        "has_target": bool(target),
        "last_ok": 0, "last_fail": 0, "fail_msg": "", "fail_kind": "",
        "streak": 0, "pending": 0, "retry_count": 0, "backoff_until": 0,
        "level": "off", "label": "告警未启用", "color": "gray", "detail": "",
        # v1.28.2：容器自身 healthcheck 快照（与通道健康分开呈现，互不覆盖）
        "container_health": "", "container_fails": 0, "container_oom": False,
    }
    # 无论告警是否启用都带上容器健康——用户即使没配推送，也该在页面上看到
    # 「容器还活着但内部探针一直失败」。这也是 v1.28.2 加 healthcheck 的意义。
    try:
        from services.monitor import container_health
        _ch = container_health(inst)
        out["container_health"] = str(_ch.get("health") or "")
        out["container_fails"] = int(_ch.get("failing") or 0)
        out["container_oom"] = bool(_ch.get("oom_killed"))
    except Exception:
        pass
    if not enabled:
        out["detail"] = "勾选「启用告警」后才会评估通道健康"
        return out
    if not target:
        out["level"] = "setup"
        out["label"] = "未配置接收地址"
        out["color"] = "orange"
        out["detail"] = f"通道是「{out['channel_label']}」，但接收地址为空，推送不会发出"
        return out

    s = alert_state_load(inst)
    last_ok = int(s.get("push_last_ok_at") or 0)
    last_fail = int(s.get("push_last_fail_at") or 0)
    streak = int(s.get("push_fail_streak") or 0)
    ppend = s.get("pending") or []
    out.update({
        "last_ok": last_ok,
        "last_fail": last_fail,
        # 毫秒级时间戳：同秒内的先后顺序靠它区分（旧字段保持兼容）
        "last_ok_ms": int(s.get("push_last_ok_ms") or 0),
        "last_fail_ms": int(s.get("push_last_fail_ms") or 0),
        "fail_msg": str(s.get("push_last_fail_msg") or ""),
        "fail_kind": str(s.get("push_last_fail_kind") or ""),
        "streak": streak,
        "pending": len(ppend) if isinstance(ppend, list) else 0,
        "retry_count": int(s.get("retry_count") or 0),
        "backoff_until": int(s.get("push_backoff_until") or 0),
    })

    backoff = int(s.get("push_backoff_until") or 0)
    if backoff > int(time.time()):
        out["level"] = "crit"
        out["label"] = "通道已停推"
        out["color"] = "red"
        out["detail"] = (f"连续失败 {streak} 次，进入冷静期，"
                         f"{int((backoff - time.time()) // 60)} 分钟内不再尝试推送")
        return out
    # v1.28.1（V28-05）：`last_ok` / `last_fail` 是**整数秒**，同一秒内先成功后
    # 失败时二者相等，`last_fail > last_ok` 不成立 → 会落到「通道正常」分支，
    # 把刚发生的失败吞掉。改为 `>=` 且要求 last_fail 非零：同时刻以失败为准
    # （更保守，宁可提示也不要漏报）。
    if last_fail > 0 and last_fail >= last_ok:
        # 最近一次是失败
        if last_ok <= 0:
            out["level"] = "crit"
            out["label"] = "从未推送成功"
            out["color"] = "red"
            out["detail"] = (f"通道「{out['channel_label']}」一次都没成功过——"
                             f"最近失败原因：{out['fail_msg'] or '未知'}。"
                             "请检查接收地址/密钥是否正确")
        elif streak >= int(cfg.ALERT_BACKOFF_STREAK):
            out["level"] = "crit"
            out["label"] = "推送持续失败"
            out["color"] = "red"
            out["detail"] = (f"连续失败 {streak} 次，最近原因：{out['fail_msg'] or '未知'}")
        else:
            out["level"] = "warn"
            out["label"] = "最近推送失败"
            out["color"] = "orange"
            detail = f"最近一次推送失败：{out['fail_msg'] or '未知'}"
            if out["pending"]:
                detail += f"；{out['pending']} 条告警在重试队列中"
            out["detail"] = detail
        return out
    if last_ok > 0:
        out["level"] = "ok"
        out["label"] = "通道正常"
        out["color"] = "green"
        out["detail"] = "最近一次推送成功 · " + _fmt_ts(last_ok)
        return out
    out["level"] = "warn"
    out["label"] = "尚未推送过"
    out["color"] = "orange"
    out["detail"] = "配置已就绪但还没有触发过推送，可点「发送测试告警」验证通道"
    return out


_CHANNEL_LABEL = {
    "bark": "Bark",
    "serverchan": "Server酱",
    "webhook": "Webhook",
    "wecom": "企业微信",
}


def _fmt_ts(ts: int) -> str:
    try:
        return time.strftime("%m-%d %H:%M", time.localtime(int(ts)))
    except (ValueError, OSError):
        return "—"


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
