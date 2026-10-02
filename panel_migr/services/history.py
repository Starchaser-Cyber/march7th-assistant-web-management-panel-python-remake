"""任务执行历史 —— 等价 PHP history_* 全套（共享同一 data/history_<container>.json）。"""
from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timedelta
from pathlib import Path

from config import DATA_DIR, HISTORY_IDLE_SECONDS, HISTORY_KEEP
from services.logs import latest_log_path
from services.monitor import container_is_running


def history_data_file(inst: dict) -> Path:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", inst["container"])
    return DATA_DIR / f"history_{safe}.json"


def history_read(inst: dict) -> dict:
    f = history_data_file(inst)
    try:
        d = json.loads(f.read_text("utf-8"))
    except Exception:
        return {"items": []}
    if not isinstance(d, dict) or not isinstance(d.get("items"), list):
        return {"items": []}
    return {"items": d["items"]}


def history_write(inst: dict, d: dict) -> bool:
    f = history_data_file(inst)
    try:
        tmp = f.with_name(f.name + ".tmp")
        tmp.write_text(json.dumps(d, ensure_ascii=False), "utf-8")
        os.replace(tmp, f)
        return True
    except OSError:
        return False


def history_sync(inst: dict) -> dict:
    """结束判定：日志静默 90 秒 → done/aborted（照抄 PHP history_sync）。

    M4：判定结束时补记推断退出码（done→0，aborted→-1；PHP 读不到该字段时
    忽略、回写原样保留），并向事件总线发布 history 事件。
    """
    d = history_read(inst)
    now = int(time.time())
    log_path = latest_log_path(inst)
    mtime = int(os.path.getmtime(log_path)) if (log_path and os.path.isfile(log_path)) else 0
    changed = False
    container_running = None
    finished: list[dict] = []

    def _finish(i: int, end: int, status: str) -> None:
        d["items"][i]["end_ts"] = end
        d["items"][i]["status"] = status
        d["items"][i]["exit"] = 0 if status == "done" else -1
        finished.append(d["items"][i])

    for i, it in enumerate(d["items"]):
        if not isinstance(it, dict) or it.get("status") != "running":
            continue
        start = int(it.get("start_ts") or 0)
        if mtime == 0:
            # 没有日志文件：超过两倍静默时间仍无日志 → 中断，避免永远挂在「运行中」
            if now - start > HISTORY_IDLE_SECONDS * 2:
                _finish(i, now, "aborted")
                changed = True
            continue
        if now - mtime <= HISTORY_IDLE_SECONDS:
            continue                      # 日志还在更新 → 仍在跑
        if now - start <= HISTORY_IDLE_SECONDS:
            continue                      # 刚启动不足静默时长，先不判定
        if container_running is None:
            container_running = container_is_running(inst)
        end = mtime if mtime > start else start
        st = (
            "aborted" if (not container_running and (now - mtime) > HISTORY_IDLE_SECONDS * 3)
            else "done"
        )
        _finish(i, end, st)
        changed = True

    if changed:
        history_write(inst, d)
        try:
            from services import events as _ev
            _sid = str(inst.get("id") or inst.get("container") or "")
            for it in finished:
                _ev.publish("history", {
                    "id": int(it.get("id") or 0),
                    "task_key": str(it.get("task_key") or ""),
                    "task_label": str(it.get("task_label") or ""),
                    "status": str(it.get("status") or ""),
                    "exit": int(it.get("exit") or 0),
                    "end_ts": int(it.get("end_ts") or 0),
                }, inst=_sid)
        except Exception:
            pass
    return d


def format_duration(seconds) -> str:
    s = max(0, int(seconds or 0))
    if s >= 3600:
        return f"{s // 3600} 小时 {s % 3600 // 60} 分"
    if s >= 60:
        return f"{s // 60} 分 {s % 60} 秒"
    return f"{s} 秒"


_STATUS_MAP = {
    "running": {"label": "运行中", "color": "var(--blue,#3b82f6)"},
    "done": {"label": "已完成", "color": "var(--green,#10b981)"},
    "aborted": {"label": "已中断", "color": "var(--red,#ef4444)"},
}


def history_view(items: list) -> list:
    now = int(time.time())
    out = []
    for it in reversed(items):
        if not isinstance(it, dict):
            continue
        start = int(it.get("start_ts") or 0)
        end = int(it.get("end_ts") or 0)
        st = str(it.get("status") or "running")
        if st == "running":
            dur = "进行中 " + format_duration(now - start)
        else:
            dur = format_duration((end if end > 0 else start) - start)
        smap = _STATUS_MAP.get(st, {"label": st, "color": "var(--muted,#9ca3af)"})
        out.append({
            "id": int(it.get("id") or 0),
            "task_label": str(it.get("task_label") or ""),
            "start_str": datetime.fromtimestamp(start).strftime("%m-%d %H:%M:%S") if start > 0 else "--",
            "duration_str": dur,
            "status": st,
            "status_label": smap["label"],
            "status_color": smap["color"],
            **({"exit": it["exit"]} if "exit" in it else {}),
        })
    return out


def _today_start() -> float:
    now = datetime.now()
    return now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def history_today_stats(items: list) -> dict:
    today_start = _today_start()
    count = ok = 0
    for it in items:
        if not isinstance(it, dict):
            continue
        if int(it.get("start_ts") or 0) < today_start:
            continue
        count += 1
        if it.get("status") == "done":
            ok += 1
    return {"count": count, "ok": ok}


_WD = ["日", "一", "二", "三", "四", "五", "六"]


def history_week_stats(items: list) -> list:
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    buckets: dict[str, dict] = {}
    for i in range(6, -1, -1):
        day = today - timedelta(days=i)
        key = day.strftime("%Y-%m-%d")
        buckets[key] = {
            "date": day.strftime("%m-%d"),
            "wd": _WD[day.isoweekday() % 7],   # 等价 PHP date('w')：0=日..6=六
            "today": i == 0,
            "count": 0,
            "ok": 0,
        }
    for it in items:
        if not isinstance(it, dict):
            continue
        start = int(it.get("start_ts") or 0)
        if start <= 0:
            continue
        key = datetime.fromtimestamp(start).strftime("%Y-%m-%d")
        if key not in buckets:
            continue
        buckets[key]["count"] += 1
        if it.get("status") == "done":
            buckets[key]["ok"] += 1
    return list(buckets.values())


def history_add(inst: dict, task_key: str, task_label: str) -> bool:
    """等价 PHP history_add()：插一条 running 记录，超 HISTORY_KEEP 只留最近。"""
    from services.instances import instance_container

    d = history_read(inst)
    max_id = 0
    for it in d["items"]:
        try:
            max_id = max(max_id, int(it.get("id") or 0))
        except (TypeError, ValueError):
            pass
    d["items"].append({
        "id": max_id + 1,
        "task_key": str(task_key),
        "task_label": str(task_label),
        "instance": instance_container(inst),
        "start_ts": int(time.time()),
        "end_ts": 0,
        "status": "running",
    })
    if len(d["items"]) > HISTORY_KEEP:
        d["items"] = d["items"][-HISTORY_KEEP:]
    return history_write(inst, d)
