"""SQLite 事件库（M4）——长期归档，与共享 JSON 环形缓存互补。

- events:          告警/历史等状态机事件（?ajax=alert_history 的数据源）
- monitor_samples: 采样点归档（JSON points 只留 3600 点，这里留 30 天）
- monitor_minutes: 分钟聚合归档（JSON 留 1440 分钟，这里留 90 天）

线程安全：threading.local 连接 + 写锁；任何失败只返回 False，绝不影响主流程。
路径跟随 config.DATA_DIR（测试可 monkeypatch）。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time

import config

_DDL = """
CREATE TABLE IF NOT EXISTS events (
    seq     INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      INTEGER NOT NULL,
    type    TEXT    NOT NULL,
    inst    TEXT    NOT NULL DEFAULT '',
    payload TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_type_seq ON events(type, seq);
CREATE TABLE IF NOT EXISTS monitor_samples (
    ts      INTEGER NOT NULL,
    inst    TEXT    NOT NULL DEFAULT '',
    cpu     REAL, mem REAL, disk INTEGER,
    net_in  REAL, net_out REAL,
    load    REAL, uptime INTEGER,
    running INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (ts, inst)
);
CREATE TABLE IF NOT EXISTS monitor_minutes (
    t    INTEGER NOT NULL,
    inst TEXT    NOT NULL DEFAULT '',
    cpu  REAL, mem REAL, disk REAL,
    PRIMARY KEY (t, inst)
);
"""

_local = threading.local()
_lock = threading.RLock()          # 写串行化（可重入：insert 持锁时会进 _conn 再加锁）
_conns: list[sqlite3.Connection] = []

_SAMPLE_DB_GAP = 15               # 采样点落库节流秒数（WS 推送不受此限制）
_last_sample_ts: dict[str, int] = {}


def db_path():
    from config import DATA_DIR
    return DATA_DIR / "panel_events.db"


_epoch = 0                          # reset() 递增，各线程据此重建连接


def _conn() -> sqlite3.Connection:
    global _epoch
    cur = getattr(_local, "conn", None)
    if cur is not None and getattr(_local, "epoch", -1) == _epoch:
        return cur
    if cur is not None:              # epoch 已轮换（reset 过）→ 丢弃旧连接
        try:
            cur.close()
        except sqlite3.Error:
            pass
    p = db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(p, timeout=10)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=NORMAL")
    c.executescript(_DDL)
    _local.conn = c
    _local.epoch = _epoch
    with _lock:
        _conns.append(c)
    return c


def reset() -> None:
    """关闭全部连接并轮换 epoch（测试切换 DATA_DIR 时各线程自动重建）。"""
    global _epoch
    _epoch += 1
    with _lock:
        for c in _conns:
            try:
                c.close()
            except sqlite3.Error:
                pass
        _conns.clear()
    _last_sample_ts.clear()


def insert_event(ts: int, etype: str, inst: str, payload: dict) -> bool:
    try:
        with _lock:
            c = _conn()
            c.execute(
                "INSERT INTO events (ts, type, inst, payload) VALUES (?,?,?,?)",
                (int(ts), str(etype), str(inst or ""),
                 json.dumps(payload, ensure_ascii=False)),
            )
            c.commit()
        return True
    except (sqlite3.Error, OSError, ValueError):
        return False


def list_events(etype: str, limit: int = 50, kind: str | None = None,
                frm: int | None = None, to: int | None = None) -> list[dict]:
    """按 seq 倒序取状态机事件（payload 已还原为 dict）。

    v1.22：新增可选过滤 kind（payload.kind，Python 侧过滤）/ frm / to（unix 秒区间）。
    无参数时行为与旧版完全一致（向后兼容）。
    """
    sql = "SELECT ts, type, inst, payload FROM events WHERE type=?"
    args: list = [str(etype)]
    if frm is not None:
        sql += " AND ts >= ?"
        args.append(int(frm))
    if to is not None:
        sql += " AND ts <= ?"
        args.append(int(to))
    sql += " ORDER BY seq DESC"
    if kind:
        sql += " LIMIT 5000"          # kind 在 JSON 内 → 取范围内更宽，Python 过滤后再截断
    else:
        sql += " LIMIT ?"
        args.append(int(limit))
    try:
        with _lock:
            c = _conn()
            rows = c.execute(sql, args).fetchall()
    except (sqlite3.Error, OSError, ValueError):
        return []
    out = []
    for ts, etype_r, inst, payload in rows:
        try:
            body = json.loads(payload)
        except ValueError:
            body = {}
        out.append({"ts": int(ts), "type": etype_r, "inst": inst, **(body or {})})
    if kind:
        out = [e for e in out if str(e.get("kind") or "") == str(kind)]
        out = out[:int(limit)]
    return out


def clear_events(etype: str, inst: str | None = None) -> int:
    """删除该类型（可选按实例）全部事件；返回删除行数，失败返回 -1。"""
    try:
        with _lock:
            c = _conn()
            if inst:
                cur = c.execute("DELETE FROM events WHERE type=? AND inst=?",
                                (str(etype), str(inst)))
            else:
                cur = c.execute("DELETE FROM events WHERE type=?", (str(etype),))
            c.commit()
            return int(cur.rowcount)
    except (sqlite3.Error, OSError, ValueError):
        return -1


# ===== v1.22 监控自定义时段区间查询 =====

RANGE_RAW_MAX = 2 * 3600            # ≤2 小时用 monitor_samples 原始点，否则用分钟聚合
RANGE_MAX_POINTS = 10000            # 超过该点数自动等间隔降采样


def _fnum(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _downsample(points: list, max_points: int) -> list:
    """等间隔降采样到 max_points 个点（保留首尾，避免曲线两端丢失）。"""
    n = len(points)
    if max_points <= 0 or n <= max_points:
        return points
    step = n / float(max_points)
    out = [points[min(n - 1, int(i * step))] for i in range(max_points)]
    if out:
        out[-1] = points[-1]
    return out


def query_range(inst: str, frm: int, to: int,
                max_points: int = RANGE_MAX_POINTS) -> tuple:
    """区间查询监控点位。返回 (bucket, points)。

    自动选粒度：区间 ≤2h → monitor_samples 原始点（bucket="raw"）；
    >2h → monitor_minutes 聚合（bucket="minute"）。结果超 max_points 自动等间隔降采样。
    raw 点位字段与 ?ajax=monitor 的 points 一致（t/cpu/mem/disk/netIn/netOut/load/uptime）；
    minute 点位字段为 t/cpu/mem/disk。任何失败返回 (bucket, [])。
    """
    try:
        frm = int(frm)
        to = int(to)
    except (TypeError, ValueError):
        return "raw", []
    if to < frm:
        frm, to = to, frm
    bucket = "raw" if (to - frm) <= RANGE_RAW_MAX else "minute"
    key = str(inst or "")
    try:
        with _lock:
            c = _conn()
            if bucket == "raw":
                rows = c.execute(
                    "SELECT ts, cpu, mem, disk, net_in, net_out, load, uptime, running "
                    "FROM monitor_samples WHERE inst=? AND ts BETWEEN ? AND ? "
                    "ORDER BY ts ASC", (key, frm, to)).fetchall()
            else:
                rows = c.execute(
                    "SELECT t, cpu, mem, disk FROM monitor_minutes "
                    "WHERE inst=? AND t BETWEEN ? AND ? ORDER BY t ASC",
                    (key, frm, to)).fetchall()
    except (sqlite3.Error, OSError, ValueError):
        return bucket, []
    if bucket == "raw":
        pts = [{"t": int(r[0]), "cpu": round(_fnum(r[1]), 1),
                "mem": round(_fnum(r[2]), 1), "disk": int(r[3] or 0),
                "netIn": round(_fnum(r[4]), 1), "netOut": round(_fnum(r[5]), 1),
                "load": round(_fnum(r[6]), 2), "uptime": int(r[7] or 0),
                "running": int(r[8] or 0)} for r in rows]
    else:
        pts = [{"t": int(r[0]), "cpu": round(_fnum(r[1]), 1),
                "mem": round(_fnum(r[2]), 1), "disk": round(_fnum(r[3]), 1)}
               for r in rows]
    if max_points and len(pts) > int(max_points):
        pts = _downsample(pts, int(max_points))
    return bucket, pts


def insert_sample(p: dict, running: bool, inst: str = "") -> bool:
    """采样点归档（15 秒节流；WS 的 monitor 事件由 events.publish 直发，不受影响）。"""
    ts = int(p.get("t") or time.time())
    key = str(inst or "")
    last = _last_sample_ts.get(key, 0)
    if ts - last < _SAMPLE_DB_GAP:
        return False
    _last_sample_ts[key] = ts
    try:
        with _lock:
            c = _conn()
            c.execute(
                "INSERT OR REPLACE INTO monitor_samples "
                "(ts, inst, cpu, mem, disk, net_in, net_out, load, uptime, running) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (ts, key, float(p.get("cpu") or 0), float(p.get("mem") or 0),
                 int(p.get("disk") or 0), float(p.get("netIn") or 0),
                 float(p.get("netOut") or 0), float(p.get("load") or 0),
                 int(p.get("uptime") or 0), 1 if running else 0),
            )
            c.commit()
        return True
    except (sqlite3.Error, OSError, ValueError):
        return False


def insert_minute(t: int, cpu: float, mem: float, disk: float, inst: str = "") -> bool:
    try:
        with _lock:
            c = _conn()
            c.execute(
                "INSERT OR REPLACE INTO monitor_minutes (t, inst, cpu, mem, disk) "
                "VALUES (?,?,?,?,?)",
                (int(t), str(inst or ""), float(cpu), float(mem), float(disk)),
            )
            c.commit()
        return True
    except (sqlite3.Error, OSError, ValueError):
        return False


def trim(now: int | None = None) -> bool:
    """定期裁剪：events 留 5 万条、采样 30 天、分钟 90 天。"""
    now = int(now if now is not None else time.time())
    try:
        with _lock:
            c = _conn()
            c.execute(
                "DELETE FROM events WHERE seq NOT IN "
                "(SELECT seq FROM events ORDER BY seq DESC LIMIT 50000)")
            c.execute("DELETE FROM monitor_samples WHERE ts < ?", (now - 30 * 86400,))
            c.execute("DELETE FROM monitor_minutes WHERE t < ?", (now - 90 * 86400,))
            c.commit()
        return True
    except (sqlite3.Error, OSError, ValueError):
        return False
