"""任务成功率统计（v1.23 M1-2）——按任务维度聚合近 N 天执行情况。

数据源优先级：
1. SQLite events(type="history")：v1.22 起任务结束会落库，保留 5 万条，覆盖长周期；
2. 回退 history_<容器>.json（最多 200 条）：新装面板尚无事件时的兜底。

输出按任务分组：执行次数、成功、失败/中断、成功率、平均耗时、最近一次时间。
统计只读，任何失败都返回空结构，不影响页面渲染。
"""
from __future__ import annotations

import time

from services import eventdb, history

MAX_DAYS = 30
DEFAULT_DAYS = 30


def _fmt_duration(seconds) -> str:
    s = max(0, int(seconds or 0))
    if s >= 3600:
        return f"{s // 3600} 小时 {s % 3600 // 60} 分"
    if s >= 60:
        return f"{s // 60} 分 {s % 60} 秒"
    return f"{s} 秒"


def _clamp_days(days) -> int:
    try:
        return max(1, min(int(days or DEFAULT_DAYS), MAX_DAYS))
    except (TypeError, ValueError):
        return DEFAULT_DAYS


def _collect_events(inst: dict, frm: int, to: int) -> tuple:
    """返回 (rows, source)；source 取值 events / history.json。"""
    sid = str(inst.get("id") or inst.get("container") or "")
    rows = eventdb.list_events("history", 20000, frm=frm, to=to)
    out = [e for e in rows if str(e.get("inst") or "") == sid
           and str(e.get("status") or "") in ("done", "aborted")]
    if out:
        return out, "events"
    # 回退：JSON 历史（无事件库时的兜底，窗口内过滤）
    d = history.history_read(inst)
    fb = []
    for it in d.get("items") or []:
        if not isinstance(it, dict):
            continue
        start = int(it.get("start_ts") or 0)
        if start < frm or start > to:
            continue
        st = str(it.get("status") or "")
        if st not in ("done", "aborted"):
            continue
        fb.append({
            "ts": int(it.get("end_ts") or start or 0),
            "inst": sid,
            "task_key": str(it.get("task_key") or ""),
            "task_label": str(it.get("task_label") or ""),
            "status": st,
            "start_ts": start,
            "end_ts": int(it.get("end_ts") or 0),
        })
    return fb, "history.json"


def task_success_stats(inst: dict, days=DEFAULT_DAYS, now: int | None = None) -> dict:
    """按任务聚合成功率。返回 {ok, days, total, done, aborted, rate, items[]}。"""
    days = _clamp_days(days)
    now = int(now if now is not None else time.time())
    frm = now - days * 86400

    groups: dict = {}
    try:
        rows, source = _collect_events(inst, frm, now)
    except Exception:
        rows, source = [], "none"

    for e in rows:
        key = str(e.get("task_key") or "unknown")
        g = groups.setdefault(key, {
            "task_key": key,
            "task_label": str(e.get("task_label") or key),
            "total": 0, "done": 0, "aborted": 0,
            "dur_sum": 0, "dur_n": 0, "last_ts": 0,
        })
        if e.get("task_label"):
            g["task_label"] = str(e.get("task_label"))
        st = str(e.get("status") or "")
        if st not in ("done", "aborted"):
            continue
        g["total"] += 1
        if st == "done":
            g["done"] += 1
        else:
            g["aborted"] += 1
        start = int(e.get("start_ts") or 0)
        end = int(e.get("end_ts") or e.get("ts") or 0)
        if start > 0 and end > start:
            g["dur_sum"] += end - start
            g["dur_n"] += 1
        g["last_ts"] = max(g["last_ts"], int(e.get("ts") or end or 0))

    items = []
    for g in groups.values():
        total = g["total"]
        avg = int(g["dur_sum"] / g["dur_n"]) if g["dur_n"] else 0
        items.append({
            "task_key": g["task_key"],
            "task_label": g["task_label"],
            "total": total,
            "done": g["done"],
            "aborted": g["aborted"],
            "rate": round(g["done"] * 100.0 / total, 1) if total else 0.0,
            "avg_duration": avg,
            "avg_duration_str": _fmt_duration(avg) if avg else "--",
            "last_ts": g["last_ts"],
            "last_str": (time.strftime("%m-%d %H:%M", time.localtime(g["last_ts"]))
                         if g["last_ts"] else "--"),
        })
    items.sort(key=lambda x: (-x["total"], x["task_label"]))

    total = sum(i["total"] for i in items)
    done = sum(i["done"] for i in items)
    return {
        "ok": True,
        "days": days,
        "from": frm,
        "to": now,
        "total": total,
        "done": done,
        "aborted": total - done,
        "rate": round(done * 100.0 / total, 1) if total else 0.0,
        "items": items,
        "has_data": total > 0,
        "source": source,
    }
