"""首批只读接口（M1）：status / log / running / monitor / history。

handler 签名统一 `def h(request) -> Response`（同步，由 main.py 载入线程执行，
阻塞型采样不卡事件循环）。返回结构与 PHP 分支逐字段一致。
"""
from __future__ import annotations

import os
import shlex
import time

from fastapi.responses import JSONResponse, PlainTextResponse

from services import history as hist
from services import logs as lg
from services import monitor as mon
from services import eventdb
from services.instances import instance_current
from services.shell import run_cmd


def _inst(request):
    return instance_current(request.state.session)


def h_status(request):
    """GET ?ajax=status → container_status()：docker compose ps 原始文本。"""
    inst = _inst(request)
    r = run_cmd(f"cd {shlex.quote(inst['dir'])} && docker compose ps")
    return PlainTextResponse(r["out"])


def h_log(request):
    """GET ?ajax=log → 日志文件列表 + 过滤结果。"""
    inst = _inst(request)
    qp = request.query_params
    files = lg.log_files(inst)
    base_names = [os.path.basename(f) for f in files]
    file_q = (qp.get("file") or "").strip()
    if file_q and file_q in base_names:
        idx = base_names.index(file_q)
    else:
        idx = 0
    path = files[idx] if idx < len(files) else None

    def to_int(v, default=0):
        try:
            return int(v)
        except (TypeError, ValueError):
            return default

    opts = {
        "keyword": str(qp.get("keyword") or ""),
        "level": str(qp.get("level") or ""),
        "hours": to_int(qp.get("hours"), 0),
        "lines": max(100, min(5000, to_int(qp.get("lines"), 2000))),
    }
    if path:
        r = lg.filter_log(path, opts)
        return JSONResponse({
            "ok": True,
            "file": os.path.basename(path),
            "files": base_names,
            "total": r["total"],
            "lines": r["lines"],
        })
    return JSONResponse({
        "ok": True, "file": None, "files": [], "total": 0,
        "lines": "(暂无日志文件，任务运行后会生成)",
    })


def h_running(request):
    """GET ?ajax=running → 容器运行态 + 当前任务。"""
    inst = _inst(request)
    running = mon.container_is_running(inst)
    hd = hist.history_sync(inst)
    cur = None
    for it in reversed(hd["items"]):
        if isinstance(it, dict) and it.get("status") == "running":
            cur = it
            break
    return JSONResponse({
        "running": running,
        "task": str(cur.get("task_label") or "") if cur else "",
        "start": int(cur.get("start_ts") or 0) if cur else 0,
        "now": int(time.time()),
    })


def h_monitor(request):
    """GET ?ajax=monitor → 采样（节流 iv 秒）+ 历史点位。"""
    inst = _inst(request)
    qp = request.query_params
    if "iv" in qp:
        try:
            iv = max(1, int(qp.get("iv") or 1))
        except (TypeError, ValueError):
            iv = 1
    else:
        iv = mon.monitor_interval()
    r = mon.monitor_sample(inst, iv)
    d = r["data"]
    host = d["meta"].get("host")
    return JSONResponse({
        "ok": True,
        "sampled": r["sampled"],
        "running": r["running"],
        "points": d.get("points") or [],
        "minutes": d.get("minutes") or [],
        "host": host if host is not None else mon.monitor_host_info(inst),
        "lastSample": int(d["meta"].get("lastSample") or 0),
        "interval": iv,
    })


def h_monitor_range(request):
    """GET ?ajax=monitor_range&from=<unix>&to=<unix> → 自定义时段监控点位。

    v1.22：区间 ≤2h 返回原始采样点（bucket=raw），>2h 返回分钟聚合（bucket=minute），
    超 1 万点自动等间隔降采样。参数非法返回 ok=false；空区间返回空 points 不报错。
    """
    inst = _inst(request)
    qp = request.query_params

    def _ts(name):
        v = qp.get(name)
        if v is None or str(v).strip() == "":
            return None
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return None

    frm = _ts("from")
    to = _ts("to")
    if frm is None or to is None:
        return JSONResponse({"ok": False, "msg": "缺少或非法的 from/to 参数（需 unix 秒）"})
    if to < frm:
        frm, to = to, frm
    sid = str(inst.get("id") or inst.get("container") or "")
    bucket, points = eventdb.query_range(sid, frm, to)
    return JSONResponse({
        "ok": True,
        "bucket": bucket,
        "from": int(frm),
        "to": int(to),
        "count": len(points),
        "points": points,
    })


def h_history(request):
    """GET ?ajax=history → 任务历史（返回前先做结束判定）。"""
    inst = _inst(request)
    d = hist.history_sync(inst)
    stats = hist.history_today_stats(d["items"])
    return JSONResponse({
        "ok": True,
        "items": hist.history_view(d["items"]),
        "today_count": stats["count"],
        "today_ok": stats["ok"],
        "week": hist.history_week_stats(d["items"]),
    })


GET_HANDLERS = {
    "status": h_status,
    "log": h_log,
    "running": h_running,
    "monitor": h_monitor,
    "monitor_range": h_monitor_range,
    "history": h_history,
}
