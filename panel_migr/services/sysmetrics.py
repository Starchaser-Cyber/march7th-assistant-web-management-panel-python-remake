"""资源采集（v1.20 重写）—— cgroup 直读 + Docker Engine unix socket API，零 shell 子进程。

旧实现（v1.19 及更早）每秒 fork 多次 docker/df/free 命令、sleep(0.4) 做两次
docker stats 快照，并把 docker stats 的「短窗口瞬时百分比」当「开机以来平均值」
乘墙钟差分反推，导致 CPU 使用率爆到几万~几十万 %（服务器实测 max=567471%）。

新实现采集路径（全部为文件读取或本地 unix socket HTTP，无 sleep、无 fork）：
- CPU   : cgroup cpu.stat usage_usec（v2）/ cpuacct.usage（v1）差分 ÷ 墙钟差 ×100
          —— 即 CPU 使用率的物理定义（单核百分比），上限天然是 核数×100%
- 内存  : memory.current / memory.max（v2），v1 回退 usage_in_bytes / limit_in_bytes
- 网络  : Engine API /containers/{id}/stats?stream=false 累计计数差分（约 1.3s/次，低频）
- 状态  : Engine API /containers/{name}/json（约 14ms），socket 不可用回退 docker CLI
- 磁盘  : os.statvfs；负载 /proc/loadavg
- 主机  : /proc/meminfo、/etc/os-release、os.statvfs、Engine /version（回退 CLI）
"""
from __future__ import annotations

import json
import os
import re
import socket
import time
from datetime import datetime, timezone
from pathlib import Path

DOCKER_SOCK = os.environ.get("M7A_DOCKER_SOCK", "/var/run/docker.sock")

_CGROUP_CACHE: dict = {}   # cid -> cgroup 目录（命中后仍校验存在，容器重启后自动重解析）
_INFO_CACHE: dict = {}     # container -> {ts, info}，1 秒缓存（sampler 线程与 HTTP 线程共享）
_HOST_MEM = {"ts": 0.0, "val": 0}


# ---------- Engine API（unix socket 裸 HTTP） ----------

def sock_json(path: str, timeout: float = 3.0):
    """HTTP GET over unix socket → (status, json|None)；连接/读取失败 → (-1, None)。"""
    buf = b""
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect(DOCKER_SOCK)
        s.sendall(f"GET {path} HTTP/1.1\r\nHost: docker\r\nConnection: close\r\n\r\n".encode("utf-8"))
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
        s.close()
    except OSError:
        return -1, None
    head, sep, body = buf.partition(b"\r\n\r\n")
    if not sep:
        return -1, None
    try:
        status = int(head.split(b" ", 2)[1])
    except (IndexError, ValueError):
        return -1, None
    try:
        return status, json.loads(body.decode("utf-8", "replace"))
    except ValueError:
        return status, None


def _epoch(iso) -> int:
    """ISO 时间（如 StartedAt=2026-10-02T06:01:07.527Z，UTC）→ epoch 秒；失败 → 0。"""
    m = re.match(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})", str(iso or ""))
    if not m:
        return 0
    try:
        ts = int(datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S")
                  .replace(tzinfo=timezone.utc).timestamp())
    except ValueError:
        return 0
    return ts if ts > 0 else 0


def container_info(name: str, refresh: bool = False) -> dict:
    """{id, running, started}；Engine API 约 14ms，1 秒缓存，socket 不可用回退 docker CLI。"""
    now = time.monotonic()
    hit = _INFO_CACHE.get(name)
    if hit is not None and not refresh and now - hit["ts"] < 1.0:
        return hit["info"]
    info = {"id": "", "running": False, "started": 0}
    st, j = sock_json(f"/containers/{name}/json", 3.0)
    if st == 200 and isinstance(j, dict):
        state = j.get("State") or {}
        info = {"id": str(j.get("Id") or ""),
                "running": bool(state.get("Running")),
                "started": _epoch(state.get("StartedAt"))}
    else:
        info = _cli_info(name)
    _INFO_CACHE[name] = {"ts": now, "info": info}
    return info


def _cli_info(name: str) -> dict:
    from services.shell import run_cmd
    from shlex import quote
    r = run_cmd("docker inspect -f '{{.Id}} {{.State.Running}} {{.State.StartedAt}}' " + quote(name))
    parts = (r.get("out") or "").split()
    if r.get("code") == 0 and len(parts) >= 3:
        return {"id": parts[0], "running": parts[1].lower() == "true", "started": _epoch(parts[2])}
    return {"id": "", "running": False, "started": 0}


# ---------- cgroup ----------

def cgroup_dir(cid: str):
    """容器 cgroup 目录（v2 统一挂载优先，v1 cpuacct 兜底）；找不到 → None。"""
    if not cid:
        return None
    cached = _CGROUP_CACHE.get(cid)
    if cached and Path(cached).is_dir():
        return Path(cached)
    for c in (f"/sys/fs/cgroup/system.slice/docker-{cid}.scope",   # v2 systemd
              f"/sys/fs/cgroup/docker/{cid}.scope",                # v2 cgroupfs
              f"/sys/fs/cgroup/docker/{cid}",                      # v1
              f"/sys/fs/cgroup/cpuacct/docker/{cid}",              # v1 cpuacct 单独挂载
              f"/sys/fs/cgroup/cpu,cpuacct/docker/{cid}",
              f"/sys/fs/cgroup/{cid}"):
        p = Path(c)
        if p.is_dir() and ((p / "cpu.stat").is_file() or (p / "cpuacct.usage").is_file()):
            _CGROUP_CACHE[cid] = c
            return p
    return None


def cpu_usec(cid: str):
    """容器累计 CPU 时间（µs）。v2 cpu.stat usage_usec；v1 cpuacct.usage(ns)÷1000。"""
    d = cgroup_dir(cid)
    if d is None:
        return None
    f = d / "cpu.stat"
    if f.is_file():
        try:
            for line in f.read_text().splitlines():
                if line.startswith("usage_usec"):
                    return int(line.split()[1])
        except (OSError, ValueError, IndexError):
            return None
    f = d / "cpuacct.usage"
    if f.is_file():
        try:
            return int(f.read_text().strip()) // 1000
        except (OSError, ValueError):
            return None
    return None


def mem(cid: str):
    """→ (used_bytes, limit_bytes|None)。v2 memory.current/memory.max；v1 回退。"""
    d = cgroup_dir(cid)
    if d is not None and (d / "memory.current").is_file():
        try:
            used = int((d / "memory.current").read_text().strip())
            lim = None
            try:
                raw = (d / "memory.max").read_text().strip()
                if raw != "max":
                    lim = int(raw)
            except (OSError, ValueError):
                pass
            return used, lim
        except (OSError, ValueError):
            pass
    # cgroup v1：memory 控制器独立挂载
    for c in (f"/sys/fs/cgroup/memory/docker/{cid}", f"/sys/fs/cgroup/memory/{cid}"):
        dd = Path(c)
        if dd.is_dir():
            try:
                used = int((dd / "memory.usage_in_bytes").read_text().strip())
                lim = int((dd / "limit_in_bytes").read_text().strip())
                if lim <= 0 or lim >= (1 << 62):
                    lim = None
                return used, lim
            except (OSError, ValueError):
                pass
    return 0, None


def net_counters(cid: str):
    """Engine API 累计收发字节（stream=false 一次约 1.3s，按 NET_IV 低频调用）。"""
    if not cid:
        return None
    st, j = sock_json(f"/containers/{cid}/stats?stream=false", 8.0)
    if st != 200 or not isinstance(j, dict):
        return None
    nets = j.get("networks") or {}
    try:
        rx = sum(int(v.get("rx_bytes") or 0) for v in nets.values())
        tx = sum(int(v.get("tx_bytes") or 0) for v in nets.values())
    except (TypeError, ValueError):
        return None
    return {"in": rx, "out": tx}


# ---------- 主机 ----------

def host_mem_total() -> int:
    """主机内存总量（字节，600s 缓存）；内存限额为 max 时用它换算百分比。"""
    if time.time() - _HOST_MEM["ts"] < 600 and _HOST_MEM["val"]:
        return _HOST_MEM["val"]
    val = 0
    try:
        with open("/proc/meminfo", encoding="utf-8") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    val = int(line.split()[1]) * 1024
                    break
    except (OSError, ValueError, IndexError):
        pass
    _HOST_MEM.update(ts=time.time(), val=val)
    return val


def disk_percent(path: str = "/") -> int:
    try:
        st = os.statvfs(path)
        if st.f_blocks <= 0:
            return 0
        return int(round((st.f_blocks - st.f_bfree) / st.f_blocks * 100))
    except OSError:
        return 0


def loadavg() -> float:
    try:
        with open("/proc/loadavg", encoding="utf-8") as f:
            return float(f.read(64).split()[0])
    except (OSError, ValueError, IndexError):
        return 0.0


def host_info() -> dict:
    """主机信息（字段与 PHP 旧版一致，零 fork；docker 版本走 /version，失败回退 CLI）。"""
    host = {"name": socket.gethostname(), "os": "", "docker": "", "cores": 0,
            "memTotal": 0, "memAvail": 0, "diskTotal": 0, "diskUsed": 0}
    try:
        with open("/proc/meminfo", encoding="utf-8") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    host["memTotal"] = int(line.split()[1]) * 1024
                elif line.startswith("MemAvailable:"):
                    host["memAvail"] = int(line.split()[1]) * 1024
                if host["memTotal"] and host["memAvail"]:
                    break
    except (OSError, ValueError, IndexError):
        pass
    try:
        st = os.statvfs("/")
        host["diskTotal"] = st.f_blocks * st.f_frsize
        host["diskUsed"] = (st.f_blocks - st.f_bfree) * st.f_frsize
    except OSError:
        pass
    try:
        with open("/etc/os-release", encoding="utf-8") as f:
            for line in f:
                if line.startswith("PRETTY_NAME="):
                    host["os"] = line.split("=", 1)[1].strip().strip('"')
                    break
    except OSError:
        pass
    st, j = sock_json("/version", 3.0)
    if st == 200 and isinstance(j, dict) and j.get("Version"):
        host["docker"] = str(j["Version"])
    else:
        from services.shell import run_cmd
        r = run_cmd('docker version --format "{{.Server.Version}}"')
        host["docker"] = (r.get("out") or "").strip()
    host["cores"] = os.cpu_count() or 0
    return host
