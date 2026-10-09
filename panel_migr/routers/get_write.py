"""M2 读型/带副作用 GET：7 个写型 ajax + 2 个下载 + 3 个 key 回调（W2/W6）。

PHP 对照（index.php）：
  ajax=config_raw/preview_token/doctor/alert_check/image_check/check_update/test_update_source
  ?download=config / ?export_log=1            （需登录）
  ?monitor_sampler=1 / ?scheduler=1 / ?alerter=1 （key 回调，免登录）
"""
from __future__ import annotations

import hashlib
import hmac
import time
from pathlib import Path

from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse, Response

import config as cfg
from phpsess import is_auth
from services import updateops
from services.alerts import alert_tick, heartbeat_touch
from services.configops import config_read_raw
from services.doctor import panel_doctor
from services.instances import instance_current, panel_config_save
from services.logs import filter_log, log_files
from services.schedule import schedule_run_due
from services.shell import run_cmd


def _inst(request):
    return instance_current(request.state.session)


# ===== 7 个写型 GET（ajax）=====


def h_config_raw(request) -> Response:
    raw = config_read_raw(_inst(request))
    return PlainTextResponse(raw if raw is not None else "(无法读取 config.yaml)")


def h_preview_token(request) -> dict:
    s = Path(cfg.DEFAULT_DIR) / "logs" / "preview_secret"
    try:
        secret = s.read_text("utf-8").strip()
    except OSError:
        secret = ""
    if secret == "":
        return {"ok": False, "msg": "预览服务未部署（服务器缺少 preview_secret）"}
    # 与 preview_server.py 一致：本地日期 + HMAC-SHA256 前 32 位，按天轮换
    day = time.strftime("%Y%m%d")
    token = hmac.new(secret.encode("utf-8"), day.encode("ascii"), hashlib.sha256).hexdigest()[:32]
    return {"ok": True, "token": token}


def h_doctor(request) -> dict:
    return panel_doctor(_inst(request))


def h_alert_check(request) -> dict:
    return alert_tick(_inst(request), False)


def h_image_check(request) -> dict:
    return updateops.image_check(_inst(request), bool(request.query_params.get("force")))


def h_check_update(request) -> dict:
    return updateops.check_update()


def h_test_update_source(request) -> dict:
    return updateops.test_update_source()


# ===== 2 个下载（需登录）=====


def h_download_config(request) -> Response:
    inst = _inst(request)
    p = Path(inst_dir_config(inst))
    if not p.is_file():
        return RedirectResponse("index.php", 302)
    ts = time.strftime("%Y%m%d%H%M%S")
    return Response(
        content=p.read_bytes(),
        media_type="application/octet-stream",
        headers={
            "content-disposition": f'attachment; filename="config.yaml.bak.{ts}.yaml"',
            "content-length": str(p.stat().st_size),
        },
    )


def inst_dir_config(inst: dict) -> str:
    from services.instances import instance_config

    return instance_config(inst)


def h_export_log(request) -> Response:
    inst = _inst(request)
    files = log_files(inst)
    base_names = [Path(f).name for f in files]
    want = str(request.query_params.get("file") or "").strip()
    idx = base_names.index(want) if want in base_names else 0
    path = files[idx] if files else None
    if path is None:
        return Response(content="", media_type="text/plain")
    opts = {
        "keyword": str(request.query_params.get("keyword") or ""),
        "level": str(request.query_params.get("level") or ""),
        "hours": int(request.query_params.get("hours") or 0),
        "lines": 5000,
    }
    r = filter_log(path, opts)
    ts = time.strftime("%Y%m_%H%M%S")
    return Response(
        content="\n".join(r["lines"]),
        media_type="text/plain; charset=utf-8",
        headers={"content-disposition": f'attachment; filename="m7a_log_{ts}.log"'},
    )


# ===== 3 个 key 回调（免登录、靠 key 比对）=====


def _panel_cfg() -> dict:
    from services.instances import panel_config

    return panel_config()


def _req_key(request) -> str:
    """key 读取：优先 X-M7A-Key 头（不进访问日志/Referer），兼容 ?key= 过渡（M6 改 cron 后收口）。"""
    return str(
        request.headers.get("x-m7a-key")
        or request.query_params.get("key")
        or ""
    )


def h_monitor_sampler(request) -> Response:
    cfgv = _panel_cfg()
    key = str(cfgv.get("monitor_key") or "")
    if key == "":
        import binascii
        import os

        key = binascii.hexlify(os.urandom(8)).decode("ascii")
        panel_config_save({"monitor_key": key})
    if _req_key(request) != key:
        return JSONResponse({"ok": False, "msg": "invalid key"})
    inst = _inst(request)
    r = services_monitor_sample(inst, 60)
    heartbeat_touch(inst, "monitor")
    al = alert_tick(inst, True)
    return JSONResponse({
        "ok": True,
        "sampled": r["sampled"],
        "running": r["running"],
        "points": len(r["data"].get("points") or []),
        "alert": bool(al.get("pushed")),
    })


def services_monitor_sample(inst: dict, interval: int) -> dict:
    from services.monitor import monitor_sample

    return monitor_sample(inst, interval)


def _keyed(request, cfg_key: str) -> tuple[bool, str]:
    """key 回调共用比对：未配置 / 空 key / 不一致 → 拒绝。"""
    cfgv = _panel_cfg()
    want = str(cfgv.get(cfg_key) or "")
    got = _req_key(request)
    if want == "" or got == "" or not hmac.compare_digest(want, got):
        return False, ""
    return True, got


def h_scheduler(request) -> Response:
    ok, _ = _keyed(request, "scheduler_key")
    if not ok:
        return JSONResponse({"ok": False, "msg": "forbidden"}, status_code=403)
    inst = _inst(request)
    heartbeat_touch(inst, "scheduler")
    res = schedule_run_due(inst)
    al = alert_tick(inst, True)
    res["alert"] = bool(al.get("pushed"))
    return JSONResponse(res)


def h_alerter(request) -> Response:
    ok, _ = _keyed(request, "alerter_key")
    if not ok:
        return JSONResponse({"ok": False, "msg": "forbidden"}, status_code=403)
    inst = _inst(request)
    heartbeat_touch(inst, "alerter")
    return JSONResponse(alert_tick(inst, True))


# ===== v1.23 M1-4：数据导出（CSV / JSON，需登录）=====


def _export_days(request, default: int = 30, cap: int = 180) -> int:
    try:
        d = int(request.query_params.get("days") or default)
    except (TypeError, ValueError):
        d = default
    return max(1, min(d, cap))


def _wants_json(request) -> bool:
    return str(request.query_params.get("format") or "").strip().lower() == "json"


def _csv_text(header: list, rows: list) -> str:
    """生成 CSV 文本（带 UTF-8 BOM，Excel 直接双击不乱码）。"""
    def _cell(v) -> str:
        s = "" if v is None else str(v)
        if any(ch in s for ch in (",", '"', "\n", "\r")):
            return '"' + s.replace('"', '""') + '"'
        return s

    lines = [",".join(_cell(c) for c in header)]
    for r in rows:
        lines.append(",".join(_cell(c) for c in r))
    return "\ufeff" + "\r\n".join(lines) + "\r\n"


def _export_response(request, header: list, rows: list, base: str, payload: dict):
    ts = time.strftime("%Y%m%d_%H%M%S")
    if _wants_json(request):
        return JSONResponse({"ok": True, "header": header, "rows": rows, **payload})
    return Response(
        content=_csv_text(header, rows),
        media_type="text/csv; charset=utf-8",
        headers={"content-disposition": f'attachment; filename="{base}_{ts}.csv"'},
    )


def h_export_history(request) -> Response:
    """?export=history&days=90[&format=json] → 任务历史 CSV（含状态、耗时与收益摘要）。

    数据源优先 SQLite 历史事件（覆盖天数/条数都更大），无事件时回退 history JSON，
    保证老版本升级上来也能导出。
    """
    from services import eventdb
    from services.history import history_read, outcome_summary
    from services.instances import instance_container

    inst = _inst(request)
    days = _export_days(request, 90, 365)
    now = int(time.time())
    frm = now - days * 86400
    sid = str(inst.get("id") or inst.get("container") or "")

    items, source = [], "events"
    for e in eventdb.list_events("history", 50000, frm=frm, to=now):
        if str(e.get("inst") or "") != sid:
            continue
        items.append(e)
    if not items:
        source = "history.json"
        for it in (history_read(inst).get("items") or []):
            if isinstance(it, dict) and int(it.get("start_ts") or 0) >= frm:
                items.append(it)
    items.sort(key=lambda x: int(x.get("start_ts") or x.get("ts") or 0))

    st_map = {"done": "已完成", "aborted": "已中断", "running": "运行中"}
    header = ["开始时间", "结束时间", "任务", "任务标识", "实例", "状态", "耗时(秒)", "退出码", "收益摘要"]
    rows = []
    for it in items:
        start = int(it.get("start_ts") or it.get("ts") or 0)
        end = int(it.get("end_ts") or 0)
        if not start:
            continue
        st = str(it.get("status") or "running")
        rows.append([
            time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(start)),
            time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(end)) if end else "",
            str(it.get("task_label") or ""),
            str(it.get("task_key") or ""),
            str(it.get("instance") or instance_container(inst)),
            st_map.get(st, st),
            max(0, (end or now) - start),
            it.get("exit", ""),
            str(it.get("outcome_summary") or outcome_summary(it.get("outcome")) or ""),
        ])
    return _export_response(request, header, rows, "m7a_tasks",
                            {"count": len(rows), "days": days, "source": source})


def h_export_alerts(request) -> Response:
    """?export=alerts&days=30[&format=json] → 告警事件 CSV。"""
    from services import eventdb

    inst = _inst(request)
    days = _export_days(request, 30, 365)
    now = int(time.time())
    frm = now - days * 86400
    sid = str(inst.get("id") or inst.get("container") or "")
    _kind = {"down": "离线告警", "recovered": "已恢复", "aborted": "任务中断"}
    rows = []
    for e in eventdb.list_events("alert", 20000, frm=frm, to=now):
        if str(e.get("inst") or "") not in (sid, ""):
            continue
        tsv = int(e.get("ts") or 0)
        rows.append([
            time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(tsv)) if tsv else "",
            _kind.get(str(e.get("kind") or ""), str(e.get("kind") or "")),
            str(e.get("inst") or ""),
            str(e.get("title") or e.get("text") or ""),
            str(e.get("body") or e.get("msg") or ""),
        ])
    return _export_response(request, ["时间", "类型", "实例", "标题", "内容"], rows,
                            "m7a_alerts", {"count": len(rows), "days": days})


def h_export_monitor(request) -> Response:
    """?export=monitor&days=7[&format=json] → 资源监控归档 CSV。

    区间 ≤2 小时导原始采样点，>2 小时导分钟聚合（与图表口径一致）。
    """
    from services import eventdb

    inst = _inst(request)
    days = _export_days(request, 7, 90)
    now = int(time.time())
    frm = now - days * 86400
    sid = str(inst.get("id") or inst.get("container") or "")
    bucket, pts = eventdb.query_range(sid, frm, now, max_points=200000)
    raw = bucket == "raw"
    header = (["时间", "CPU(%)", "内存(%)", "磁盘(%)", "下载(B/s)", "上传(B/s)", "负载", "容器运行"]
              if raw else ["时间", "CPU(%)", "内存(%)", "磁盘(%)"])
    rows = []
    for p in pts:
        tsv = int(p.get("t") or 0)
        ts_txt = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(tsv)) if tsv else ""
        if raw:
            rows.append([ts_txt, p.get("cpu"), p.get("mem"), p.get("disk"),
                         p.get("netIn"), p.get("netOut"), p.get("load"),
                         "运行中" if p.get("running") else "已停止"])
        else:
            rows.append([ts_txt, p.get("cpu"), p.get("mem"), p.get("disk")])
    return _export_response(request, header, rows, "m7a_monitor",
                            {"count": len(rows), "days": days, "bucket": bucket})


# ===== 注册表 =====

# ajax 名 → 同步处理器（request）→ Response | dict（dict 自动包 JSON）
WRITE_GET_HANDLERS = {
    "config_raw": h_config_raw,
    "doctor": h_doctor,
    "alert_check": h_alert_check,
    "image_check": h_image_check,
    "check_update": h_check_update,
    "test_update_source": h_test_update_source,
}

# (查询参数, 期望值, 需要登录, 处理器)
SPECIAL_GETS = [
    ("monitor_sampler", "1", False, h_monitor_sampler),
    ("scheduler", "1", False, h_scheduler),
    ("alerter", "1", False, h_alerter),
    ("download", "config", True, h_download_config),
    ("export_log", "1", True, h_export_log),
    ("export", "history", True, h_export_history),
    ("export", "alerts", True, h_export_alerts),
    ("export", "monitor", True, h_export_monitor),
]
