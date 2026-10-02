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


def list_events(etype: str, limit: int = 50) -> list[dict]:
    """按 seq 倒序取状态机事件（payload 已还原为 dict）。"""
    try:
        with _lock:
            c = _conn()
            rows = c.execute(
                "SELECT ts, type, inst, payload FROM events WHERE type=? "
                "ORDER BY seq DESC LIMIT ?",
                (str(etype), int(limit)),
            ).fetchall()
    except (sqlite3.Error, OSError, ValueError):
        return []
    out = []
    for ts, etype_r, inst, payload in rows:
        try:
            body = json.loads(payload)
        except ValueError:
            body = {}
        out.append({"ts": int(ts), "type": etype_r, "inst": inst, **(body or {})})
    return out


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
