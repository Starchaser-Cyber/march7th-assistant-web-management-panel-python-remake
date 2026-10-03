"""POST 写操作分发（W1-W5）—— action → 处理器，行为对齐 PHP index.php POST 段。

返回值约定（处理器同步执行，由 render() 异步转成 Response）：
  ("json", payload)          → JSONResponse（PHP: echo json_encode(...); exit）
  ("redirect", None)         → 302 index.php（PHP: header('Location: index.php')）
  ("flash", {...})           → 整页重渲染 + 横幅（PHP: $msg/$err 后继续渲染页面）
  ("raw", Response)          → 直接返回（下载等）
"""
from __future__ import annotations

import random
import re
from hmac import compare_digest as _hash_equals
from pathlib import Path

from fastapi.responses import JSONResponse, RedirectResponse, Response

import config as cfg
from phpsess import session_id, session_set, session_remove, new_sid
from services import passwd
from services import loginguard as lg
from services.alerts import alert_send, alert_quiet
from services.configops import (
    config_backup,
    config_save_form,
    config_save_text,
    notify_health_check,
    yaml_format_val,
    yaml_read_simple,
    h,
)
from services.containers import compose, container_is_running, ensure_running, task_start
from services.flash import flash
from services.history import history_add, history_write
from services.instances import (
    instance_config,
    instance_container,
    instance_current,
    instance_dir,
    instance_name,
    instance_switch_to,
    instances_load,
    instances_write,
    panel_config_save,
)
from services.schedule import (
    schedule_days_label,
    schedule_load,
    schedule_run_due,
    schedule_save,
    schedule_validate,
)
from services.updateops import backup_rollback, do_update, image_update
from services.shell import run_cmd

# ===== 返回值小工具 =====


def J(payload: dict):
    return ("json", payload)


def RD(set_sid: str | None = None):
    # set_sid：无 cookie 登录/首次设密时新建的会话 id，302 响应顺带下发 Set-Cookie
    return ("redirect", {"set_sid": set_sid} if set_sid else None)


def F(msg=None, err=None, notify_check=None):
    return ("flash", {"msg": msg, "err": err, "notify_check": notify_check})


def RAW(resp: Response):
    return ("raw", resp)


async def render(request, result) -> Response:
    kind, data = result
    if kind == "json":
        return JSONResponse(data)
    if kind == "redirect":
        resp = RedirectResponse("index.php", 302)
        if data and data.get("set_sid"):
            resp.set_cookie("PHPSESSID", str(data["set_sid"]), path="/",
                            httponly=True, samesite="lax")
        return resp
    if kind == "flash":
        return await flash(request, **data)
    return data


def _client_ip(request) -> str:
    """限速来源：优先 X-Forwarded-For 首段（nginx 追加真实来源），否则直连地址。"""
    xff = str(request.headers.get("x-forwarded-for") or "")
    if xff:
        return xff.split(",")[0].strip() or "-"
    return request.client.host if request.client else "-"


def csrf_check(form, session: dict) -> bool:
    """等价 PHP csrf_check()：POST csrf 字段与 session token 常量时间比对。"""
    token = form.get("csrf")
    if token is None:
        return False
    return _hash_equals(str(session.get(cfg.CSRF_KEY) or ""), str(token))


# ===== 认证 4（W1）=====


def h_setup_pass(request, form, session, sid):
    p1 = str(form.get("pass1") or "")
    p2 = str(form.get("pass2") or "")
    if len(p1) < 6:
        return F(err="密码至少 6 位")
    if p1 != p2:
        return F(err="两次输入的密码不一致")
    if passwd.set_pass(p1):
        old_sid = sid
        sid = new_sid()                      # 总是轮换（M5：会话固定防御）
        session[cfg.SKEY] = True
        if not session_set(sid, cfg.SKEY, True):
            session.pop(cfg.SKEY, None)
            return F(err="保存登录状态失败，请重试")
        if old_sid and old_sid != sid:
            session_remove(old_sid, cfg.SKEY)  # 旧会话注销（尽力而为）
        return RD(set_sid=sid)
    return F(err="密码文件写入失败，请检查面板目录权限")


def h_login(request, form, session, sid):
    p = str(form.get("pass") or "")
    ip = _client_ip(request)
    if not lg.check(ip):
        return F(err=f"尝试过于频繁，请 {lg.remaining(ip)} 秒后再试")
    if passwd.check_pass(p):
        lg.ok(ip)
        old_sid = sid
        sid = new_sid()                      # 总是轮换（M5：会话固定防御）
        session[cfg.SKEY] = True
        if not session_set(sid, cfg.SKEY, True):
            session.pop(cfg.SKEY, None)
            return F(err="保存登录状态失败，请重试")
        if old_sid and old_sid != sid:
            session_remove(old_sid, cfg.SKEY)  # 旧会话注销（尽力而为）
        return RD(set_sid=sid)
    lg.fail(ip)
    return F(err="密码错误")


def h_logout(request, form, session, sid):
    session.pop(cfg.SKEY, None)
    if sid and not session_remove(sid, cfg.SKEY):
        session[cfg.SKEY] = True       # 文件写失败 → 还原本地状态，避免假退出
        return F(err="退出失败，请重试")
    return RD()


def h_restore_config(request, form, session, sid):
    up = form.get("cfg_file")
    if up is None or getattr(up, "filename", "") in (None, ""):
        return F(err="未收到上传文件或上传失败")
    fname = str(up.filename)
    ext = fname.rsplit(".", 1)[-1].lower() if "." in fname else ""
    if ext not in ("yaml", "yml"):
        return F(err="文件格式错误，请上传 .yaml / .yml 文件")
    content = up.file.read()
    if len(content) == 0 or len(content) > 2 * 1024 * 1024:
        return F(err="文件为空或过大（最大 2MB）")
    text = content.decode("utf-8", "replace") if isinstance(content, bytes) else str(content)
    if len(text.strip()) < 10 or ":" not in text:
        return F(err="文件内容异常，未识别到有效 YAML 配置")
    inst = instance_current(session)
    bak = config_backup(inst)
    try:
        Path(instance_config(inst)).write_text(text, "utf-8")
    except OSError:
        return F(err=f"写入失败，请检查文件权限：chmod 666 {instance_config(inst)}")
    bakname = Path(bak).name if bak else "无"
    return F(msg=f"配置已恢复（{h(fname)}），原配置备份：{bakname}。需重启容器生效。")


# ===== 多实例 3（W5）=====


def h_instance_switch(request, form, session, sid):
    target = str(form.get("instance_id") or "").strip()
    if target and instance_switch_to(session, sid, target):
        inst = instance_current(session)
        return F(msg=f"已切换到实例：{h(instance_name(inst))}（{h(instance_container(inst))}）")
    return F(err="切换失败：实例不存在")


def h_instance_save(request, form, session, sid):
    _id = str(form.get("inst_id") or "").strip()
    name = str(form.get("inst_name") or "").strip()
    container = str(form.get("inst_container") or "").strip()
    _dir = str(form.get("inst_dir") or "").strip().rstrip("/")
    is_default = bool(form.get("inst_default"))
    if name == "" or container == "" or _dir == "":
        return F(err="名称、容器名、项目目录均不能为空")
    if not re.match(r"^[A-Za-z0-9_-]+$", container):
        return F(err="容器名只能包含字母、数字、下划线、短横线")

    lst = instances_load()
    is_new = True
    used: list[str] = []
    for i, item in enumerate(lst):
        if (item["id"] == _id and _id != "") or (_id == "" and item["container"] == container):
            lst[i] = dict(item, name=name, container=container, dir=_dir, default=is_default)
            is_new = False
        elif item["container"] == container:
            used.append(item["name"])
    err = ""
    if used:
        err = "容器名已被实例「" + "、".join(used) + "」使用"
    elif is_new:
        lst.append({"id": container, "name": name, "container": container,
                    "dir": _dir, "default": is_default})
    if err:
        return F(err=err)

    # 保证只有一个默认实例
    any_default = False
    self_id = container if is_new else _id
    for i, item in enumerate(lst):
        if is_default and item["id"] == self_id:
            lst[i]["default"] = True
            any_default = True
        elif is_default and item.get("default"):
            lst[i]["default"] = False
        elif not is_default and item.get("default"):
            any_default = True
    if not any_default and lst:
        lst[0]["default"] = True

    if instances_write(lst):
        if is_new:
            instance_switch_to(session, sid, container)
        msg = f"实例「{h(name)}」已添加并切换" if is_new else f"实例「{h(name)}」已更新"
        return F(msg=msg)
    return F(err=f"写入 {h(Path(cfg.INSTANCES_FILE).name)} 失败，请检查面板目录权限")


def h_instance_delete(request, form, session, sid):
    _id = str(form.get("inst_id") or "").strip()
    confirm = str(form.get("inst_confirm") or "").strip()
    lst = instances_load()
    target = next((it for it in lst if it["id"] == _id), None)
    if not target:
        return F(err="实例不存在")
    if confirm != target["name"]:
        return F(err=f"确认失败：请输入实例名称「{h(target['name'])}」以删除")
    if len(lst) <= 1:
        return F(err="至少保留一个实例，无法删除")
    new_list = [it for it in lst if it["id"] != _id]
    if not instances_write(new_list):
        return F(err="写入失败，实例未删除")
    if instance_current(session)["id"] == _id:
        session.pop("m7a_instance", None)
        session_remove(sid, "m7a_instance")
        nc = instance_current({})
        session["m7a_instance"] = nc["id"]
        session_set(sid, "m7a_instance", nc["id"])
    return F(msg=f"实例「{h(target['name'])}」已删除")


# ===== 任务 8（W3）=====


def _task_action(request, form, session, sid, key: str):
    label = cfg.TASKS[key]
    inst = instance_current(session)
    ck = ensure_running(inst)                      # v1.21：小助手没跑先自动拉起再执行任务
    if ck.get("err"):
        return F(err=ck["err"])
    r = task_start(inst, key)
    if r["code"] == 0:
        history_add(inst, key, label)
        if ck.get("started"):
            return F(msg=f"小助手已自动启动，任务「{label}」已后台开始")
        return F(msg=f"任务「{label}」已后台启动")
    return F(msg="任务启动失败：" + r["out"])


# ===== 容器操作 7（W3）=====


def h_restart(request, form, session, sid):
    r = compose(instance_current(session), "restart")
    return F(msg="容器已重启" if r["code"] == 0 else "重启失败：" + r["out"])


def h_update(request, form, session, sid):
    inst = instance_current(session)
    r1 = compose(inst, "pull")
    if r1["code"] != 0:
        return F(err="拉取镜像失败：" + r1["out"])
    r2 = compose(inst, "up -d")
    return F(msg="镜像已更新，容器已重建" if r2["code"] == 0 else "更新失败：" + r2["out"])


def h_update_image(request, form, session, sid):
    return J(image_update(instance_current(session)))


def h_stop_task(request, form, session, sid):
    r = compose(instance_current(session), "restart")
    if r["code"] == 0:
        return F(msg="⏹️ 任务已停止（容器已重启），如需继续请重新启动任务")
    return F(err="容器重启失败：" + r["out"])


def h_stop_loop(request, form, session, sid):
    from shlex import quote
    inst = instance_current(session)
    r = run_cmd(
        'docker exec ' + quote(instance_container(inst))
        + ' pkill -15 -f "main.py currencywarsloop"'
    )
    if r["code"] == 0:
        return F(msg="🛑 货币战争循环已停止（仅结束循环子进程，容器未重启）")
    return F(err="循环停止失败（可能循环未在运行）：" + r["out"])


def h_stop(request, form, session, sid):
    r = compose(instance_current(session), "stop")
    if r["code"] == 0:
        return F(msg="⏸️ 容器已停止，想再运行请点「重启容器」恢复")
    return F(err="容器停止失败：" + r["out"])


def h_do_update(request, form, session, sid):
    return J(do_update())


# ===== 配置 7（W4）=====


def h_set_after_finish(request, form, session, sid):
    val = "Exit" if str(form.get("value") or "") == "Exit" else "None"
    inst = instance_current(session)
    bak = config_backup(inst)
    r = config_save_form(inst, {"after_finish": yaml_format_val(val, "str")})
    if r["ok"]:
        if val == "Exit":
            tip = "已开启「跑完自动退出游戏」，任务跑完自动退出，下次进入从主界面开始"
        else:
            tip = "已切换为「跑完保持界面」，任务跑完停在最后界面"
        msg = tip + "（备份：" + (Path(bak).name if bak else "无") + "）。下次运行任务时生效。"
        return J({"ok": True, "msg": msg})
    return J({"ok": False, "msg": r["msg"]})


def h_alert_save(request, form, session, sid):
    ch_map = ("bark", "serverchan", "webhook")
    en = "1" if form.get("alert_enable") else "0"
    ch = str(form.get("alert_channel") or "")
    ch = ch if ch in ch_map else "bark"
    tg = str(form.get("alert_target") or "").strip()
    if panel_config_save({"alert_enable": en, "alert_channel": ch, "alert_target": tg}):
        return J({"ok": True, "msg": "告警设置已保存"})
    return J({"ok": False, "msg": "写入失败，请检查 .panel_config.php 权限"})


def h_alert_test(request, form, session, sid):
    ch_map = ("bark", "serverchan", "webhook")
    ch = str(form.get("alert_channel") or "")
    ov = {
        "enable": True,
        "channel": ch if ch in ch_map else "bark",
        "target": str(form.get("alert_target") or "").strip(),
    }
    r = alert_send("M7A 面板测试告警", "看到这条消息说明告警通道配置正确 ✅（来自面板测试）", ov)
    return J({"ok": bool(r.get("ok")), "msg": r.get("msg", "")})


def h_set_update_mode(request, form, session, sid):
    mode = "manual" if str(form.get("mode") or "") == "manual" else "auto"
    if panel_config_save({"update_mode": mode}):
        label = "手动更新" if mode == "manual" else "自动检查"
        return J({"ok": True, "msg": f"更新模式已切换为「{label}」"})
    return J({"ok": False, "msg": "写入面板配置失败，请检查 .panel_config.php 权限"})


def h_set_monitor_interval(request, form, session, sid):
    try:
        iv = int(str(form.get("interval") or 1))
    except ValueError:
        iv = 1
    iv = max(1, min(3600, iv))
    if panel_config_save({"monitor_interval": iv}):
        return J({"ok": True, "msg": f"采样间隔已设置为 {iv} 秒，曲线按新频率刷新"})
    return J({"ok": False, "msg": "写入面板配置失败，请检查 .panel_config.php 权限"})


def _form_updates(form) -> dict:
    """把表单里的 cfg_* 字段按 CONFIG_GROUPS 类型转成 config.yaml 更新值。"""
    from services.configops import config_field_index

    updates: dict[str, str] = {}
    for key, f in config_field_index().items():
        post_key = "cfg_" + key
        if f.get("type") == "bool":
            updates[key] = "true" if form.get(post_key) else "false"
        elif f.get("type") == "password":
            v = str(form.get(post_key) or "").strip()
            if v != "":
                updates[key] = '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'
        else:
            v = str(form.get(post_key) or "").strip()
            updates[key] = yaml_format_val(v, f.get("type"))
    return updates


def h_save_config_form(request, form, session, sid):
    inst = instance_current(session)
    bak = config_backup(inst)
    r = config_save_form(inst, _form_updates(form))
    if not r["ok"]:
        return F(err=r["msg"])
    bakname = Path(bak).name if bak else "无"
    msg = f"{r['msg']}（备份：{bakname}）"
    notify_check = None
    if form.get("then_restart"):
        rr = compose(inst, "restart")
        msg += "。容器已重启，配置已生效。" if rr["code"] == 0 else "。但重启失败：" + rr["out"]
    elif form.get("then_notify_check"):
        notify_check = notify_health_check(yaml_read_simple(inst))
        msg += "。🔍 已完成推送配置体检，结果见「消息推送」分组上方的体检提示。"
    elif form.get("then_notify_test"):
        if container_is_running(inst):
            nr = task_start(inst, "notify")
            if nr["code"] == 0:
                history_add(inst, "notify", cfg.TASKS["notify"])
                msg += "。🔔 已发送测试推送，请在手机上确认（结果见任务历史/日志）。"
            else:
                msg += "。但测试推送发送失败：" + nr["out"].strip()
        else:
            msg += "。容器未运行，配置已保存但未发送测试。"
    else:
        msg += "。需重启容器生效。"
    return F(msg=msg, notify_check=notify_check)


def h_save_config_text(request, form, session, sid):
    inst = instance_current(session)
    bak = config_backup(inst)
    content = str(form.get("yaml_content") or "")
    r = config_save_text(inst, content)
    if r["ok"]:
        bakname = Path(bak).name if bak else "无"
        return F(msg=f"{r['msg']}（备份：{bakname}）。需重启容器生效。")
    return F(err=r["msg"])


# ===== 回滚 / 历史（W5）=====


def h_backup_rollback(request, form, session, sid):
    return J(backup_rollback(str(form.get("file") or "")))


def h_history_clear(request, form, session, sid):
    if history_write(instance_current(session), {"items": []}):
        return J({"ok": True, "msg": "任务执行历史已清空"})
    return J({"ok": False, "msg": "清空失败，请检查 data 目录写权限"})


# ===== 计划任务 5（W5）=====


def h_schedule_add(request, form, session, sid):
    inst = instance_current(session)
    err, clean = schedule_validate({
        "name": str(form.get("sched_name") or ""),
        "time": str(form.get("sched_time") or ""),
        "days": form.getlist("sched_days"),
        "args": str(form.get("sched_args") or ""),
    })
    if err:
        return F(err="计划任务保存失败：" + err)
    assert clean is not None

    _id = str(form.get("sched_id") or "").strip()
    if _id == "" or not re.match(r"^[A-Za-z0-9]{1,32}$", _id):
        _id = random.randbytes(4).hex()
    d = schedule_load(inst)
    found = False
    for i, t in enumerate(d["tasks"]):
        if t["id"] == _id:
            d["tasks"][i].update({
                "name": clean["name"], "time": clean["time"],
                "days": clean["days"], "args": clean["args"],
            })
            found = True
            break
    if not found:
        d["tasks"].append({
            "id": _id, "name": clean["name"], "time": clean["time"],
            "days": clean["days"], "args": clean["args"],
            "enabled": True, "last_run": "", "last_result": "",
        })
    if schedule_save(inst, d):
        label = cfg.TASKS[clean["args"]]
        msg = (f"计划任务「{clean['name']}」已保存："
               f"{schedule_days_label(clean['days'])} {clean['time']} 执行 {label}"
               "（宿主机计划任务每分钟调用面板即可触发）")
        return F(msg=msg)
    return F(err="计划任务保存失败：无法写入 data/schedule.json，请检查 data 目录写权限")


def h_schedule_del(request, form, session, sid):
    inst = instance_current(session)
    _id = str(form.get("sched_id") or "").strip()
    d = schedule_load(inst)
    kept = []
    removed = ""
    for t in d["tasks"]:
        if _id != "" and t["id"] == _id:
            removed = t["name"] if t["name"] != "" else t["id"]
            continue
        kept.append(t)
    if removed == "":
        return F(err="删除失败：没有找到该计划任务")
    d["tasks"] = kept
    if schedule_save(inst, d):
        return F(msg=f"计划任务「{removed}」已删除")
    return F(err="删除失败：无法写入 data/schedule.json")


def h_schedule_toggle(request, form, session, sid):
    inst = instance_current(session)
    _id = str(form.get("sched_id") or "").strip()
    d = schedule_load(inst)
    target = ""
    for i, t in enumerate(d["tasks"]):
        if t["id"] == _id:
            d["tasks"][i]["enabled"] = not bool(t.get("enabled"))
            name = t["name"] if t["name"] != "" else t["id"]
            target = f"{name}（{'已启用' if d['tasks'][i]['enabled'] else '已停用'}）"
            break
    if target == "":
        return F(err="操作失败：没有找到该计划任务")
    if schedule_save(inst, d):
        return F(msg="计划任务 " + target)
    return F(err="操作失败：无法写入 data/schedule.json")


def h_schedule_conflict(request, form, session, sid):
    inst = instance_current(session)
    mode = "stop" if str(form.get("conflict") or "") == "stop" else "skip"
    d = schedule_load(inst)
    d["conflict"] = mode
    if schedule_save(inst, d):
        return F(msg="冲突策略已设为：" + ("停掉当前任务再执行" if mode == "stop" else "跳过本次（保留当前任务）"))
    return F(err="保存失败：无法写入 data/schedule.json")


def h_schedule_now(request, form, session, sid):
    inst = instance_current(session)
    r = schedule_run_due(inst)
    msg = f"计划任务检查完成（{r['time']}）：触发 {len(r['ran'])} 个"
    if r["ran"]:
        msg += "（" + "、".join(r["ran"]) + "）"
    if r["skipped"]:
        msg += f"，跳过 {len(r['skipped'])} 个（" + "、".join(r["skipped"]) + "）"
    if not r["ran"] and not r["skipped"]:
        msg += "。没有命中任何到点的计划任务（到点后 30 分钟内仍会补跑）。"
    return F(msg=msg)


# ===== 注册表 =====

POST_HANDLERS = {
    # 认证
    "setup_pass": h_setup_pass,
    "login": h_login,
    "logout": h_logout,
    "restore_config": h_restore_config,
    # 多实例
    "instance_switch": h_instance_switch,
    "instance_save": h_instance_save,
    "instance_delete": h_instance_delete,
    # 任务（$TASKS 键即 action）
    **{k: (lambda request, form, session, sid, _k=k: _task_action(request, form, session, sid, _k))
       for k in cfg.TASKS},
    # 容器
    "restart": h_restart,
    "update": h_update,
    "update_image": h_update_image,
    "stop_task": h_stop_task,
    "stop_loop": h_stop_loop,
    "stop": h_stop,
    "do_update": h_do_update,
    # 配置
    "set_after_finish": h_set_after_finish,
    "alert_save": h_alert_save,
    "alert_test": h_alert_test,
    "set_update_mode": h_set_update_mode,
    "set_monitor_interval": h_set_monitor_interval,
    "save_config_form": h_save_config_form,
    "save_config_text": h_save_config_text,
    # 回滚 / 历史
    "backup_rollback": h_backup_rollback,
    "history_clear": h_history_clear,
    # 计划任务
    "schedule_add": h_schedule_add,
    "schedule_del": h_schedule_del,
    "schedule_toggle": h_schedule_toggle,
    "schedule_conflict": h_schedule_conflict,
    "schedule_now": h_schedule_now,
}


async def handle_post(request) -> Response:
    """POST 统一入口：W1 前置（认证 / CSRF / 危险操作静默）+ action 分发。"""
    # 先缓存 body：form 解析会消耗请求流，未迁移 action 转发时还要复用
    await request.body()
    form = await request.form()
    action = str(form.get("action") or "")
    session = request.state.session
    sid = session_id(request)

    # 前置：首次设置密码（无 pass 文件时免 CSRF）
    if action == "setup_pass" and not cfg.PASS_FILE.is_file():
        result = h_setup_pass(request, form, session, sid)
        return await render(request, result)
    # 前置：登录（免认证、免 CSRF，与 PHP 一致）
    if action == "login":
        result = h_login(request, form, session, sid)
        return await render(request, result)

    from phpsess import is_auth
    if not is_auth(session):
        return await _forward_unauth_page(request)
    if not csrf_check(form, session):
        return await render(request, F(err="请求验证失败，请重试"))

    # v1.18：用户主动容器操作 → 告警静默 10 分钟
    if action in cfg.DANGEROUS_ACTIONS:
        alert_quiet(instance_current(session), 600)

    handler = POST_HANDLERS.get(action)
    if handler is None:
        # 绞杀者：未迁移的 action 仍回 PHP 执行
        from gateway import forward
        return await forward(request)

    import anyio.to_thread as to_thread
    result = await to_thread.run_sync(handler, request, form, session, sid)
    return await render(request, result)


async def _forward_unauth_page(request):
    """未登录且非 login/setup_pass：与 PHP 一致渲染登录页（M5-B 起 Python 渲染）。"""
    from services.pagectx import page_response

    return page_response(request)
