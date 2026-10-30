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
from pathlib import Path

import config as cfg
from services.containers import container_is_running
from services.history import history_sync, history_data_file
from services.instances import panel_config, instance_dir
from services.logs import before_deadline, elapsed_ok
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
}

# 告警分级（顺序即展示/推送优先级）
_SEVERITIES = ("info", "warn", "critical")

# alert_tick 最小间隔（秒）；时钟回拨时 elapsed_ok 会判为到期
_TICK_IV = 45

# 面板里可选的推送通道
_CHANNELS = ("bark", "serverchan", "webhook")

_STR_FIELDS = (
    "disk_level", "disk_path", "morning_last",
    "upstream_local", "upstream_latest", "upstream_notified", "upstream_error",
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
              "disk_last_check", "upstream_last_check"):
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
                pass
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
    }


# ===== 推送 =====

def alert_send(title: str, body: str, override: dict | None = None,
               severity: str = "warn") -> dict:
    """发一条告警。

    severity: info / warn / critical（v1.24 M2-5）。通道解析顺序：
    override["channels"][severity] → override["channel"] → alert_cfg()。
    向后兼容：老调用只传 (title, body, override)，此时 severity 默认 warn。
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
            for kind, title, body, _ in grp:
                _publish_alert(inst, kind, title, body, True, 0,
                               str(r.get("msg") or "已推送"), sev)
        else:
            _enqueue_pending(inst, s, grp, now, str(r.get("msg") or ""))
    s["last_push_kind_at"] = lpk
    return pushed_any


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
    sent_kinds = {str(p.get("kind") or "") for p in pend}

    groups: dict = {}
    for p in pend:
        sev = str(p.get("severity") or "warn")
        if sev not in _SEVERITIES:
            sev = "warn"
        groups.setdefault(sev, []).append(p)

    ok_all = True
    for sev in _SEVERITIES:
        grp = groups.get(sev) or []
        if not grp:
            continue
        r = alert_send("M7A 面板告警",
                       "\n\n".join(str(p.get("body") or "") for p in grp),
                       severity=sev)
        if not r.get("ok"):
            ok_all = False

    if ok_all:
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
                           str(p.get("body") or ""), True, rc, "重试成功",
                           str(p.get("severity") or "warn"))
        return sent_kinds

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
                           "重试次数用尽，已放弃推送",
                           str(p.get("severity") or "warn"))
        return sent_kinds

    nxt = now + int(delays[rc])
    s["retry_at"] = nxt
    for p in pend:
        _publish_alert(inst, str(p.get("kind") or ""), str(p.get("title") or ""),
                       str(p.get("body") or ""), False, rc,
                       f"推送失败，{int(delays[rc])} 秒后第 {rc} 次重试",
                       str(p.get("severity") or "warn"))
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

            # Q-04：down 边沿判据不能只认 prev is True。
            # 面板首次接管时容器就没起来（或 alert_state.json 被清空/重置），
            # prev 初始为 None，第一轮 tick 写入 prev=False 后永远是
            # False → False，down 告警永不触发 —— 用户以为「没消息=一切正常」，
            # 实际容器根本没跑。因此「首次观测即 down」也算一次 down 边沿。
            # 反例不受影响：prev is None and run is True 只是首次正常，不告警。
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

            # 静默期结束：补推静默期内被压下的 down 边沿（P0-4 核心补偿）
            if (not quiet and s.get("pending_down")
                    and run is False and not s.get("alerted_down")):
                events_out.append(("down", "容器意外停止",
                                   _down_body(bool(s.get("pending_down_first")),
                                              delayed=True), "critical"))
                s["alerted_down"] = True
                s["pending_down"] = False
                s["pending_down_first"] = False
            elif not quiet and s.get("pending_down"):
                # 静默期内已经自行恢复（或状态不可判定）→ 边沿失效，丢弃
                s["pending_down"] = False
                s["pending_down_first"] = False

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
                    # P1-16：静默期内不丢弃边沿，先记待补推
                    if not any(int(x.get("id") or 0) == iid for x in pend_ab):
                        pend_ab.append({"id": iid, "label": label})
                    continue
                body = f"⚠️ 任务中断：{label}\n请到面板「日志」页查看原因。"
                events_out.append(("aborted", f"任务中断：{label}", body, "warn"))
            if len(aborted_seen) > 40:
                aborted_seen = aborted_seen[-40:]
            s["aborted_seen"] = aborted_seen
            if not quiet and pend_ab:
                # 静默期结束：补推期间积压的中断通知
                for x in pend_ab:
                    label = str(x.get("label") or "任务")
                    body = (f"⚠️ 任务中断：{label}\n请到面板「日志」页查看原因。"
                            "\n（发生在告警静默期内，现已补发提醒）")
                    events_out.append(("aborted", f"任务中断：{label}", body, "warn"))
                pend_ab = []
            s["pending_aborted"] = pend_ab[-20:]
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
        pass

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

def morning_report_body(inst: dict) -> str:
    """聚合昨日任务完成情况 / 告警数 / 磁盘剩余 / 本体是否存活，纯文本正文。"""
    now = int(time.time())
    lt = time.localtime(now - 86400)
    yday = time.strftime("%Y-%m-%d", lt)
    try:
        y_start = int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday,
                                   0, 0, 0, 0, 0, -1)))
    except (OverflowError, ValueError):
        y_start = now - 86400
    y_end = y_start + 86400 - 1

    # 昨日任务
    done = aborted = running = other = 0
    try:
        hd = history_sync(inst)
        items = list(hd.get("items") or [])
    except Exception:
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
    total = done + aborted + running + other

    # 昨日告警（只统计真正推送出去的）
    cnt = {"info": 0, "warn": 0, "critical": 0}
    al_total = 0
    try:
        from services import eventdb
        evs = eventdb.list_events("alert", limit=500, frm=y_start, to=y_end)
        for e in (evs or []):
            # v1.24.4（P2）：list_events 已把 payload 展开成顶层字段
            # （eventdb.py:150 `**body`），原先取 e["payload"] 恒为 None →
            # 晨报「昨日告警条数」永远是 0。这里直接读展开后的 dict。
            p = e if isinstance(e, dict) else None
            if not isinstance(p, dict):
                continue
            if not p.get("pushed"):
                continue
            al_total += 1
            sev = str(p.get("severity") or "warn")
            if sev in cnt:
                cnt[sev] += 1
    except Exception:
        pass

    # 磁盘
    disk_txt = "磁盘探针不可用"
    try:
        st = disk_status(inst)
        w = st.get("worst")
        if w:
            disk_txt = disk_line(w)
    except Exception:
        pass

    # 本体存活
    try:
        alive = bool(container_is_running(inst))
    except Exception:
        alive = False
    cname = str(inst.get("container") or cfg.DEFAULT_CONTAINER)

    lines = [
        f"🌅 M7A 每日健康晨报（{yday} 汇总）",
        "────────────────",
        f"【昨日任务】共 {total} 个：完成 {done} / 中断 {aborted} / 进行中 {running}",
        f"【告警通知】{al_total} 条（critical {cnt['critical']} / warn {cnt['warn']} / info {cnt['info']}）",
        f"【磁盘空间】{disk_txt}",
        f"【本体状态】容器「{cname}」{'运行中 ✅' if alive else '未运行 ❌'}",
    ]
    try:
        us = upstream_status(inst)
    except Exception:
        us = {}
    if us.get("latest"):
        if us.get("has_update"):
            lines.append(f"【版本提醒】v{us.get('local') or '?'} → 有新版本 v{us.get('latest')}，需要时再更新")
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
    if c["enable"] and c["upstream_enable"] and has_update:
        if force or str(s.get("upstream_notified") or "") != latest:
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
    return out


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
