"""M5 页面渲染：PHP index.php 页面段（L3188-6227）的 Python/Jinja2 等价实现。

- 模板 templates/panel.html.j2 由 scripts/php2jinja.py 从 index.php 机械转换
- PHP 页面段的 12 个前置计算块（monitor/scheduler/alerter key 惰性生成、history/
  schedule 预处理、after_finish 归一、状态与最近日志）在 _panel_context 等价供给
- Jinja globals 注入 PHP 函数等价物（闭包 session/inst/sid）；返回 HTML 的两个函数
  用 Markup 包裹（内部已按 PHP h() 语义转义），其余经 autoescape 转义
- 会话引导：无有效 PHPSESSID 的访客在页面渲染时发号（镜像 PHP session_start），
  csrf_token 惰性生成后经 session_set 持久化到会话文件（镜像 PHP 自动落盘）
"""
from __future__ import annotations

import json
import os
import secrets
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, Undefined
from markupsafe import Markup

import config as cfg
from phpsess import is_auth, new_sid, session_id, session_remove, session_set, sid_valid
from services.configops import config_read_raw, notify_check_html, yaml_read_simple
from services.containers import container_status
from services.history import history_sync, history_today_stats, history_view
from services.instances import (
    instance_config,
    instance_container,
    instance_current,
    instance_dir,
    instance_name,
    instances_load,
    panel_config,
    panel_config_save,
)
from services.logs import latest_log_path
from services.paneldefs import CONFIG_GROUPS, OPS, TASKS
from services.schedule import (
    config_scheduled_tasks_count,
    schedule_days_label,
    schedule_load,
    schedule_normalize_days,
)
from services.updateops import backups_list, panel_update_mode

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
_ENV: Environment | None = None


def _env() -> Environment:
    global _ENV
    if _ENV is None:
        _ENV = Environment(
            loader=FileSystemLoader(str(TEMPLATES_DIR)),
            autoescape=True,
        )
    return _ENV


def format_size(n) -> str:
    """等价 PHP format_size()（L1242）：GB/MB/KB/B，整数值不带小数点。"""
    b = float(n or 0)
    if b >= 1024 * 1024 * 1024:
        v = round(b / 1024 / 1024 / 1024, 1)
        return f"{int(v)} GB" if v == int(v) else f"{v} GB"
    if b >= 1024 * 1024:
        v = round(b / 1024 / 1024, 1)
        return f"{int(v)} MB" if v == int(v) else f"{v} MB"
    if b >= 1024:
        v = round(b / 1024, 1)
        return f"{int(v)} KB" if v == int(v) else f"{v} KB"
    return f"{int(b)} B"


def _cron_base(request, path: str) -> str:
    """等价 PHP `scheme://HTTP_HOST . rtrim(dirname(SCRIPT_NAME), '/\\')`。"""
    scheme = request.url.scheme or "http"
    host = request.url.netloc or "localhost"
    d = os.path.dirname(path or "/").rstrip("/")
    return f"{scheme}://{host}{d}"


def _panel_context(request, inst: dict, notify_check) -> dict:
    """PHP 页面段 12 个前置计算块 + 页面数据常量的等价供给。"""
    base = _cron_base(request, request.url.path)

    # [1] after_finish 归一（PHP：isset?trim 双引号:'' → 空则 'None'）
    cfg_vals = yaml_read_simple(inst)
    af_val = str(cfg_vals.get("after_finish") or "").strip("\"'") or "None"

    # [2] 容器状态（'' → 权限提示）
    st = container_status(inst)
    st_status = st if st else "(无法获取，请检查 www 用户 docker 权限)"

    # [3] monitor key 惰性生成 + cron 行 + 间隔
    mon = panel_config()
    mon_key = str(mon.get("monitor_key") or "")
    if not mon_key:
        mon_key = secrets.token_hex(8)
        panel_config_save({"monitor_key": mon_key})
    mon_iv = max(1, int(mon.get("monitor_interval") or 1))
    mon_cron = f'curl -s "{base}/action?monitor_sampler=1&key={mon_key}" >/dev/null 2>&1'

    # [4] alerter key 惰性生成 + cron 行
    al_key = str(mon.get("alerter_key") or "")
    if not al_key:
        al_key = secrets.token_hex(8)
        panel_config_save({"alerter_key": al_key})
    al_cron = f'curl -s "{base}/action?alerter=1&key={al_key}" >/dev/null 2>&1'

    # [5] 最近日志名
    lp = latest_log_path(inst)
    latest_log_name = os.path.basename(str(lp)) if lp else "暂无"

    # [6] 任务历史
    hist = history_sync(inst)
    items = hist.get("items") or []
    hist_items = history_view(items)
    hist_today = history_today_stats(items)

    # [7] 计划任务 + scheduler key + cron 行 + 星期名表
    sched = schedule_load(inst)
    sched_cfg = panel_config()
    sch_key = str(sched_cfg.get("scheduler_key") or "")
    if not sch_key:
        sch_key = secrets.token_hex(8)
        panel_config_save({"scheduler_key": sch_key})
    sch_cron = f'curl -s "{base}/action?scheduler=1&key={sch_key}" >/dev/null 2>&1'
    sch_builtin = config_scheduled_tasks_count(inst)
    sched_day_name = {1: "一", 2: "二", 3: "三", 4: "四", 5: "五", 6: "六", 7: "日"}

    # [8]+[9] 启用计数 + 每行任务标签/星期归一（PHP else: foreach 块）
    tasks = sched.get("tasks") or []
    sched_enabled = sum(1 for t in tasks if t.get("enabled"))
    for t in tasks:
        args = str(t.get("args") or "")
        t["_label"] = (TASKS.get(args) or {}).get("label") or f"{args}（已失效）"
        t["_days"] = schedule_normalize_days(t.get("days"))
        t["_daytext"] = schedule_days_label(t["_days"])

    # [12] 原始配置文本（null → 空串，渲染时判断）
    raw_cfg = config_read_raw(inst)

    return {
        "cfgVals": cfg_vals,
        "afVal": af_val,
        "st_status": st_status,
        "_monIv": mon_iv,
        "_monCron": mon_cron,
        "_alCron": al_cron,
        "latest_log_name": latest_log_name,
        "histItems": hist_items,
        "histToday": hist_today,
        "schedData": sched,
        "_schedCron": sch_cron,
        "_schedBuiltin": sch_builtin,
        "_schedDayName": sched_day_name,
        "schedEnabled": sched_enabled,
        "raw_cfg": raw_cfg,
        "notifyCheck": notify_check,
        "TASKS": TASKS,
        "OPS": OPS,
        "CONFIG_GROUPS": CONFIG_GROUPS,
        "BACKUP_KEEP": cfg.BACKUP_KEEP,
        "HISTORY_IDLE_SECONDS": cfg.HISTORY_IDLE_SECONDS,
        "HISTORY_KEEP": cfg.HISTORY_KEEP,
        "PANEL_VERSION": cfg.PANEL_VERSION,
        "UPDATE_TYPE": cfg.UPDATE_TYPE,
        "UPDATE_OWNER": cfg.UPDATE_OWNER,
        "UPDATE_REPO": cfg.UPDATE_REPO,
    }


def _json_hex(o, flags=0) -> str:
    """json_encode 等价（镜像 PHP JSON_UNESCAPED_UNICODE | JSON_HEX_TAG）：
    <>& 转义为 \u003C\u003E\u0026，防内联 <script> 注入闭合标签。"""
    s = json.dumps(o, ensure_ascii=False)
    return s.replace("&", "\\u0026").replace("<", "\\u003C").replace(">", "\\u003E")


def _template_globals(session: dict, inst: dict | None, sid: str) -> dict:
    """Jinja globals：PHP 模板里调用的函数等价物（inst/session/sid 闭包）。"""

    def csrf_field() -> Markup:
        tok = str(session.get(cfg.CSRF_KEY) or "")
        if not tok:
            tok = secrets.token_hex(16)  # 等价 PHP csrf_token()：32 hex
            session[cfg.CSRF_KEY] = tok
            session_set(sid, cfg.CSRF_KEY, tok)  # 镜像 PHP 会话自动落盘
        return Markup(f'<input type="hidden" name="csrf" value="{tok}">')

    def trim(s, chars=None) -> str:
        return str(s if s is not None else "").strip(chars)

    return {
        "csrf_field": csrf_field,
        "notify_check_html": lambda res: Markup(notify_check_html(res) or ""),
        "instances_load": instances_load,
        "instance_current": lambda: instance_current(session),
        "instance_name": lambda: instance_name(inst) if inst else "",
        "instance_container": lambda: instance_container(inst) if inst else "",
        "instance_dir": lambda: instance_dir(inst) if inst else "",
        "instance_config": lambda: instance_config(inst) if inst else "",
        "container_status": lambda: container_status(inst) if inst else "",
        "yaml_read_simple": lambda: yaml_read_simple(inst) if inst else {},
        "config_read_raw": lambda: config_read_raw(inst) if inst else None,
        "backups_list": backups_list,
        "panel_update_mode": panel_update_mode,
        "format_size": format_size,
        "latest_log_path": lambda: latest_log_path(inst) if inst else None,
        "history_sync": lambda: history_sync(inst) if inst else {"items": []},
        "history_view": history_view,
        "history_today_stats": history_today_stats,
        "schedule_load": lambda: schedule_load(inst) if inst else {"tasks": []},
        "schedule_normalize_days": schedule_normalize_days,
        "schedule_days_label": schedule_days_label,
        "config_scheduled_tasks_count": lambda: config_scheduled_tasks_count(inst) if inst else 0,
        # PHP 标量函数
        "count": lambda x: len(x or []),
        "empty": lambda v: not v,
        "isset": lambda v: (not isinstance(v, Undefined)) and v is not None,
        "in_array": lambda v, seq: v in (seq or []),
        "is_file": os.path.isfile,
        "basename": os.path.basename,
        "strtoupper": lambda s: str(s).upper(),
        "trim": trim,
        "json_encode": _json_hex,
        "JSON_UNESCAPED_UNICODE": 0,
        "h": lambda s: str(s if s is not None else ""),  # autoescape 兜底，与 PHP h() 同向
    }


def render_page(request, *, msg=None, err=None, notify_check=None) -> str:
    """渲染面板页面（未登录 → 登录/初始化页，与 PHP 同模板分支）。"""
    session = request.state.session
    # 会话引导：无/非法 PHPSESSID → 发号（响应端由 page_response 下发 cookie）
    sid = session_id(request)
    if sid_valid(sid):
        request.state.new_sid = None
    else:
        sid = new_sid()
        request.state.new_sid = sid
    authed = is_auth(session)
    # v1.21.1：消费 PRG（303 跳回）暂存的一次性消息——渲染一次即从会话移除
    bag_raw = session.pop("_flash", None)
    if isinstance(bag_raw, str) and bag_raw:
        session_remove(sid, "_flash")
        try:
            _bag = json.loads(bag_raw)
            msg = msg or _bag.get("msg") or ""
            err = err or _bag.get("err") or ""
            if notify_check is None and _bag.get("notify_check") is not None:
                notify_check = _bag.get("notify_check")
        except Exception:
            pass

    ctx: dict = {
        "isAuth": authed,
        "needSetup": not cfg.PASS_FILE.is_file(),
        "msg": msg or "",
        "err": err or "",
        "PANEL_VERSION": cfg.PANEL_VERSION,
        "UPDATE_TYPE": cfg.UPDATE_TYPE,
        "UPDATE_OWNER": cfg.UPDATE_OWNER,
        "UPDATE_REPO": cfg.UPDATE_REPO,
        "BACKUP_KEEP": cfg.BACKUP_KEEP,
        "HISTORY_IDLE_SECONDS": cfg.HISTORY_IDLE_SECONDS,
        "HISTORY_KEEP": cfg.HISTORY_KEEP,
        "TASKS": TASKS,
        "OPS": OPS,
        "CONFIG_GROUPS": CONFIG_GROUPS,
    }
    if authed:
        inst = instance_current(session)
        ctx.update(_panel_context(request, inst, notify_check))
        if request.method == "GET":
            from services.alerts import alert_tick

            alert_tick(inst, False)  # PHP：GET 打开页面兜底巡检
        g = _template_globals(session, inst, sid)
    else:
        g = _template_globals(session, None, sid)
    return _env().get_template("panel.html.j2").render(**ctx, **g)


def page_response(request, *, msg=None, err=None, notify_check=None):
    """渲染 + 会话引导下发：新访客发 PHPSESSID（镜像 PHP session_start）。"""
    from fastapi.responses import HTMLResponse

    resp = HTMLResponse(
        render_page(request, msg=msg, err=err, notify_check=notify_check)
    )
    newsid = getattr(request.state, "new_sid", None)
    if newsid:
        resp.set_cookie("PHPSESSID", newsid, path="/", httponly=True, samesite="lax")
    return resp
