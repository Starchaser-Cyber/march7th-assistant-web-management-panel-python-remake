"""资源监控 —— 等价 PHP monitor_* 全套（共享同一 data/monitor_<container>.json）。

v1.20 重写采样主流程（详见 services/sysmetrics.py 模块说明）：
- CPU 差分取自 cgroup usage_usec，杜绝旧版「docker stats 短窗口值 × 墙钟」爆表 bug
- 零子进程、零 sleep；采样加线程锁，避免 sampler 线程与 HTTP 线程并发读改写竞态
- 节流仍以文件 meta.lastSample 为准（兼容 PHP 时代数据与既有测试）
- 读文件时做一次性历史清洗（meta.sanitized）：剔除旧算法产生的 >核数×100% 脏点
- 事件发布（WS 实时）与 SQLite 归档逻辑保持不变
写文件仍使用 tmp+rename 原子替换。
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path

from config import DATA_DIR
from services import sysmetrics as sm
from services.instances import panel_config
from services.shell import run_cmd  # noqa: F401 —— 兼容既有调用方 monkeypatch（如 tests/test_m4_events）

# 网络速率刷新间隔（秒）。Engine stats 单次约 1.3s，必须低频；期间沿用上次速率，
# 刷新后把本窗口速率回填到已采点，曲线平滑无锯齿。
NET_IV = max(1, int(os.environ.get("M7A_NET_IV", "5") or 5))

_SAMPLE_LOCK = threading.Lock()


# ---------- 数据文件 ----------

def monitor_data_file(inst: dict) -> Path:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", inst["container"])
    return DATA_DIR / f"monitor_{safe}.json"


def _empty() -> dict:
    return {
        "points": [], "minutes": [],
        "meta": {"host": None, "hostTs": 0, "lastSample": 0, "lastMin": 0, "lastNet": None,
                 "sanitized": 0, "cpuBase": None},
    }


def _sanitize_cpu(d: dict) -> None:
    """一次性历史清洗：剔除旧算法爆出的 CPU 脏点（> 核数×100% 即物理上不可能）。
    由 monitor_read 触发，随下一次 monitor_write 落盘并打 meta.sanitized 标记。"""
    if int(d["meta"].get("sanitized") or 0) == 1:
        return
    cap = (os.cpu_count() or 4) * 100 + 5
    pts = d.get("points") or []
    mins = d.get("minutes") or []
    d["points"] = [p for p in pts if abs(float(p.get("cpu") or 0)) <= cap]
    d["minutes"] = [m for m in mins if abs(float(m.get("cpu") or 0)) <= cap]
    d["meta"]["sanitized"] = 1


def monitor_read(inst: dict) -> dict:
    """读采样文件。文件正被 PHP(file_put_contents 非原子) 截断写入时会瞬时解析失败，
    重试一次；仍失败且文件存在 → 返回 None 哨兵（禁止调用方拿空基线去覆盖历史数据）。"""
    f = monitor_data_file(inst)
    for attempt in (0, 1):
        try:
            d = json.loads(f.read_text("utf-8"))
        except Exception:
            if attempt == 0 and f.exists():
                time.sleep(0.15)
                continue
            return _empty() if not f.exists() else None
        if not isinstance(d, dict) or "points" not in d:
            return _empty()
        meta = _empty()["meta"]
        got = d.get("meta")
        if isinstance(got, dict):
            meta.update(got)
        d["meta"] = meta
        if not isinstance(d.get("minutes"), list):
            d["minutes"] = []
        _sanitize_cpu(d)
        return d
    return None


def monitor_write(inst: dict, d: dict) -> bool:
    f = monitor_data_file(inst)
    try:
        tmp = f.with_name(f.name + f".tmp.{time.time_ns()}")
        tmp.write_text(json.dumps(d, ensure_ascii=False), "utf-8")
        os.replace(tmp, f)
        return True
    except OSError:
        return False


# ---------- 采集 ----------

def parse_bytes(s: str) -> float:
    s = (s or "").strip().lower()
    if not s:
        return 0.0
    m = re.match(r"^([\d.]+)\s*([kmgt]?i?b)?$", s)
    if not m:
        return 0.0
    v = float(m.group(1))
    u = m.group(2) or ""
    factors = {
        "": 1, "b": 1,
        "kb": 1024, "kib": 1024,
        "mb": 1024 ** 2, "mib": 1024 ** 2,
        "gb": 1024 ** 3, "gib": 1024 ** 3,
        "tb": 1024 ** 4, "tib": 1024 ** 4,
    }
    return v * factors.get(u, 1)


def container_is_running(inst: dict) -> bool:
    """容器是否在运行（v1.20：Engine API + 1s 缓存，失败回退 docker CLI）。"""
    return bool(sm.container_info(inst["container"]).get("running"))


def container_started(inst: dict) -> int:
    """容器 StartedAt → epoch 秒；失败/未启动 → 0。"""
    return int(sm.container_info(inst["container"]).get("started") or 0)


def monitor_interval() -> int:
    cfg = panel_config()
    try:
        return max(1, int(cfg.get("monitor_interval", 1)))
    except (TypeError, ValueError):
        return 1


def monitor_host_info(inst: dict) -> dict:
    """主机信息（600 秒缓存于 meta.host；v1.20 起零 fork 采集）。"""
    with _SAMPLE_LOCK:
        d = monitor_read(inst)
        meta = (d or {}).get("meta") or {}
        host = meta.get("host")
        host_ts = int(meta.get("hostTs") or 0)
        if host and time.time() - host_ts < 600:
            return host

        host = sm.host_info()

        if d is not None:
            d["meta"]["host"] = host
            d["meta"]["hostTs"] = int(time.time())
            monitor_write(inst, d)
        return host


def _net_rate(d: dict, cid: str, now: int) -> tuple:
    """网络速率（B/s）。NET_IV 秒才调一次 Engine stats（约 1.3s）；期间沿用上次速率；
    刷新时用累计计数差分，并把本窗口速率回填到刷新间隔内已采的点。"""
    meta = d["meta"]
    last = meta.get("lastNet")
    last = last if isinstance(last, dict) else None
    r_in = float((last or {}).get("rateIn") or 0)
    r_out = float((last or {}).get("rateOut") or 0)
    nts = int((last or {}).get("ts") or 0)
    if now - nts < NET_IV:
        return round(r_in, 1), round(r_out, 1)          # 沿用上次速率（平滑）

    ctr = sm.net_counters(cid)
    if ctr is None:
        return round(r_in, 1), round(r_out, 1)
    dt = now - nts
    new_in = new_out = 0.0
    if last and isinstance(last.get("in"), (int, float)) and 0 < dt <= 300:
        # 计数器回退（容器网络命名空间重建）→ 本窗口记 0，以新值为基线
        if ctr["in"] >= float(last["in"]):
            new_in = (ctr["in"] - float(last["in"])) / dt
        if ctr["out"] >= float(last["out"]):
            new_out = (ctr["out"] - float(last["out"])) / dt
    if nts and (new_in or new_out):                     # 回填本窗口历史点
        for pp in d["points"]:
            if nts < int(pp.get("t") or 0) <= now:
                pp["netIn"] = round(new_in, 1)
                pp["netOut"] = round(new_out, 1)
    meta["lastNet"] = {"in": ctr["in"], "out": ctr["out"], "ts": now,
                       "rateIn": round(new_in, 1), "rateOut": round(new_out, 1)}
    return round(new_in, 1), round(new_out, 1)


def monitor_sample(inst: dict, interval: int = 1) -> dict:
    """一次采样（节流 iv 秒）。v1.20：cgroup 直读，零子进程、零 sleep，全程持锁。"""
    with _SAMPLE_LOCK:
        d = monitor_read(inst)
        if d is None:
            # 文件正被写入（瞬时截断）→ 不采样不写，避免空基线覆盖全量历史
            return {"sampled": False, "data": _empty(), "running": container_is_running(inst)}
        now = int(time.time())
        iv = max(1, int(interval or 1))

        if now - int(d["meta"].get("lastSample") or 0) < iv:
            return {"sampled": False, "data": d, "running": container_is_running(inst)}

        info = sm.container_info(inst["container"])
        running = bool(info.get("running"))
        started = int(info.get("started") or 0)
        cores = os.cpu_count() or 1
        p = {"t": now, "cpu": 0.0, "mem": 0.0, "disk": sm.disk_percent(),
             "netIn": 0.0, "netOut": 0.0, "load": sm.loadavg(), "uptime": 0}

        if running:
            cid = info.get("id") or ""
            # CPU：cgroup usage_usec 差分 ÷ 墙钟差 = 单核百分比的物理定义，
            # 上限钳在 [0, 核数×100%]（旧版这里会算出几万~几十万 %）
            u = sm.cpu_usec(cid)
            w = time.time_ns()
            if u is not None:
                base = d["meta"].get("cpuBase")
                if isinstance(base, dict):
                    try:
                        du = u - int(base.get("u") or 0)
                        dw = (w - int(base.get("w") or 0)) / 1000.0    # 墙钟差（µs）
                        if du >= 0 and 0 < dw <= 60_000_000:           # 最多回看 60s（跨重启/卡顿作废）
                            p["cpu"] = round(min(max(du / dw * 100.0, 0.0), cores * 100.0), 1)
                    except (TypeError, ValueError):
                        pass
                d["meta"]["cpuBase"] = {"u": u, "w": w}

            used, limit = sm.mem(cid)
            if used:
                lim = limit or sm.host_mem_total()
                if lim > 0:
                    p["mem"] = round(min(max(used / lim * 100.0, 0.0), 100.0), 1)
            p["uptime"] = max(0, now - started) if started > 0 else 0
            p["netIn"], p["netOut"] = _net_rate(d, cid, now)
        else:
            d["meta"]["lastNet"] = None

        d["points"].append(p)
        if len(d["points"]) > 3600:
            d["points"] = d["points"][-3600:]

        # 分钟级聚合：跨分钟时取最近 60 秒平均值，保留 1440 点（24 小时）
        minute = now // 60
        if minute != int(d["meta"].get("lastMin") or 0):
            cut = now - 60
            sc = sd = ss = 0.0
            cnt = 0
            for pp in d["points"]:
                if pp.get("t", 0) >= cut:
                    sc += pp.get("cpu", 0.0)
                    sd += pp.get("mem", 0.0)
                    ss += pp.get("disk", 0)
                    cnt += 1
            if cnt > 0:
                minute_row = {
                    "t": minute * 60,
                    "cpu": round(sc / cnt, 1),
                    "mem": round(sd / cnt, 1),
                    "disk": round(ss / cnt, 1),
                }
                d["minutes"].append(minute_row)
                if len(d["minutes"]) > 1440:
                    d["minutes"] = d["minutes"][-1440:]
                try:
                    from services import eventdb as _edb2
                    _edb2.insert_minute(
                        minute_row["t"], minute_row["cpu"], minute_row["mem"],
                        minute_row["disk"],
                        str(inst.get("id") or inst.get("container") or ""))
                except Exception:
                    pass
            d["meta"]["lastMin"] = minute

        d["meta"]["lastSample"] = now
        monitor_write(inst, d)
        # M4 事件化：WS 订阅者实时收点 + SQLite 长期归档（失败不影响采样主流程）
        try:
            from services import events as _ev
            from services import eventdb as _edb
            _sid = str(inst.get("id") or inst.get("container") or "")
            _ev.publish("monitor", dict(p), inst=_sid)
            _edb.insert_sample(p, running, inst=_sid)
        except Exception:
            pass
        return {"sampled": True, "data": d, "running": running}
