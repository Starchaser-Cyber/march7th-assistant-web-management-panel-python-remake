"""资源监控 —— 等价 PHP monitor_* 全套（共享同一 data/monitor_<container>.json）。

采样策略、瞬时 CPU 换算、分钟聚合、3600/1440 截断均照抄 PHP 实现；
写文件使用 tmp+rename 原子替换（比 PHP file_put_contents 更抗并发截断）。
"""
from __future__ import annotations

import json
import os
import re
import shlex
import socket
import time
from datetime import datetime, timezone
from pathlib import Path

from config import DATA_DIR
from services.instances import panel_config
from services.shell import run_cmd


# ---------- 数据文件 ----------

def monitor_data_file(inst: dict) -> Path:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", inst["container"])
    return DATA_DIR / f"monitor_{safe}.json"


def _empty() -> dict:
    return {
        "points": [], "minutes": [],
        "meta": {"host": None, "hostTs": 0, "lastSample": 0, "lastMin": 0, "lastNet": None},
    }


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


def docker_stats(inst: dict) -> dict | None:
    cmd = ('docker stats --no-stream --format "{{.CPUPerc}}|{{.MemUsage}}|{{.MemPerc}}|{{.NetIO}}" '
           + shlex.quote(inst["container"]))
    r = run_cmd(cmd)
    if r["code"] != 0:
        return None
    parts = r["out"].strip().split("|")
    if len(parts) < 4:
        return None
    mem = parts[1].split("/")
    net = parts[3].split("/")
    def pct(x): return float(x.replace("%", "").strip() or 0.0)
    return {
        "cpu": pct(parts[0]),
        "memUsed": parse_bytes(mem[0] if mem else "0"),
        "memTotal": parse_bytes(mem[1] if len(mem) > 1 else "0"),
        "memPct": pct(parts[2]),
        "netIn": parse_bytes(net[0] if net else "0"),
        "netOut": parse_bytes(net[1] if len(net) > 1 else "0"),
    }


def container_is_running(inst: dict) -> bool:
    r = run_cmd('docker inspect -f "{{.State.Running}}" ' + shlex.quote(inst["container"]))
    return r["out"].strip() == "true"


def container_started(inst: dict) -> int:
    """docker inspect StartedAt → epoch 秒；失败/未启动 → 0。"""
    r = run_cmd('docker inspect -f "{{.State.StartedAt}}" ' + shlex.quote(inst["container"]))
    s = r["out"].strip()
    m = re.match(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?", s)
    if not m:
        return 0
    try:
        # StartedAt 为 UTC（带 Z）；按绝对时间换算 epoch，与 PHP strtotime 语义一致
        dt = datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
        ts = int(dt.timestamp())
    except ValueError:
        return 0
    return ts if ts > 0 else 0


def monitor_interval() -> int:
    cfg = panel_config()
    try:
        return max(1, int(cfg.get("monitor_interval", 1)))
    except (TypeError, ValueError):
        return 1


def monitor_host_info(inst: dict) -> dict:
    """主机信息（600 秒缓存于 meta.host）。"""
    d = monitor_read(inst)
    meta = (d or {}).get("meta") or {}
    host = meta.get("host")
    host_ts = int(meta.get("hostTs") or 0)
    if host and time.time() - host_ts < 600:
        return host

    host = {"name": socket.gethostname(), "os": "", "docker": "", "cores": 0,
            "memTotal": 0, "memAvail": 0, "diskTotal": 0, "diskUsed": 0}

    r = run_cmd("free -b")
    for line in r["out"].split("\n"):
        m = re.match(r"^Mem:\s+(\d+)\s+\d+\s+\d+\s+\d+\s+\d+\s+(\d+)", line)
        if m:
            host["memTotal"] = int(m.group(1))
            host["memAvail"] = int(m.group(2))

    r = run_cmd("df -B1 /")
    lines = [x for x in r["out"].strip().split("\n")]
    if len(lines) >= 2:
        m = re.match(r"^\S+\s+(\d+)\s+(\d+)\s+\d+\s+\d+%", lines[1].strip())
        if m:
            host["diskTotal"] = int(m.group(1))
            host["diskUsed"] = int(m.group(2))

    r = run_cmd("cat /etc/os-release")
    for line in r["out"].split("\n"):
        m = re.match(r'^PRETTY_NAME="?(.*?)"?$', line.strip())
        if m:
            host["os"] = m.group(1)
            break

    r = run_cmd('docker version --format "{{.Server.Version}}"')
    host["docker"] = r["out"].strip()

    r = run_cmd("nproc")
    try:
        host["cores"] = int(r["out"].strip())
    except ValueError:
        host["cores"] = 0

    if d is not None:
        d["meta"]["host"] = host
        d["meta"]["hostTs"] = int(time.time())
        monitor_write(inst, d)
    return host


def monitor_sample(inst: dict, interval: int = 1) -> dict:
    """一次采样（节流 iv 秒）—— 照抄 PHP monitor_sample()。"""
    d = monitor_read(inst)
    if d is None:
        # 文件正被写入（瞬时截断）→ 不采样不写，避免空基线覆盖全量历史
        return {"sampled": False, "data": _empty(), "running": container_is_running(inst)}
    now = int(time.time())
    iv = max(1, int(interval or 1))

    if now - int(d["meta"].get("lastSample") or 0) < iv:
        return {"sampled": False, "data": d, "running": container_is_running(inst)}

    running = container_is_running(inst)
    started = container_started(inst) if running else 0
    p = {"t": now, "cpu": 0.0, "mem": 0.0, "disk": 0, "netIn": 0.0, "netOut": 0.0,
         "load": 0.0, "uptime": 0}

    if running:
        t0 = time.time()
        s1 = docker_stats(inst)
        if s1 is not None:
            time.sleep(0.4)
        s2 = docker_stats(inst)
        t1 = time.time()
        if s1 is not None and s2 is not None:
            # docker stats 单次输出是启动以来平均值 → 两次快照差值换算瞬时 CPU
            wall1 = (t0 - started) if started > 0 else 0
            wall2 = (t1 - started) if started > 0 else 0
            if wall1 > 1 and wall2 > wall1:
                cpu1 = s1["cpu"] / 100.0 * wall1
                cpu2 = s2["cpu"] / 100.0 * wall2
                p["cpu"] = max(0.0, (cpu2 - cpu1) / (wall2 - wall1) * 100.0)
            else:
                p["cpu"] = s2["cpu"]
            p["mem"] = s2["memPct"]
            p["uptime"] = max(0, int(t1 - started)) if started > 0 else 0
            last_net = d["meta"].get("lastNet")
            dt = now - int(d["meta"].get("lastSample") or 0)
            if isinstance(last_net, dict) and dt > 0:
                p["netIn"] = max(0.0, (s2["netIn"] - last_net.get("in", 0)) / dt)
                p["netOut"] = max(0.0, (s2["netOut"] - last_net.get("out", 0)) / dt)
            d["meta"]["lastNet"] = {"in": s2["netIn"], "out": s2["netOut"]}
        else:
            d["meta"]["lastNet"] = None
    else:
        d["meta"]["lastNet"] = None

    try:
        with open("/proc/loadavg") as f:
            la = f.read(64)
        m = re.match(r"^([\d.]+)\s+([\d.]+)\s+([\d.]+)", la)
        if m:
            p["load"] = float(m.group(1))
    except OSError:
        pass

    r = run_cmd("df -B1 /")
    lines = r["out"].strip().split("\n")
    if len(lines) >= 2:
        m = re.match(r"^\S+\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)%", lines[1].strip())
        if m:
            p["disk"] = int(m.group(4))

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
