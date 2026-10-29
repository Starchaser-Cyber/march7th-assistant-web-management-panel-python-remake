"""POST 写操作分发（W1-W5）—— action → 处理器，行为对齐 PHP index.php POST 段。

返回值约定（处理器同步执行，由 render() 异步转成 Response）：
  ("json", payload)          → JSONResponse（PHP: echo json_encode(...); exit）
  ("redirect", None)         → 302 index.php（PHP: header('Location: index.php')）
  ("flash", {...})           → 整页重渲染 + 横幅（PHP: $msg/$err 后继续渲染页面）
  ("raw", Response)          → 直接返回（下载等）
"""
from __future__ import annotations

import json
import os
import random
import re
import secrets
import time
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
        # v1.21.1：POST-Redirect-GET。表单提交后 303 跳回 GET /，一次性消息经 session 携带；
        # 此前直接返回 200 整页，地址栏停在 POST 响应上，浏览器刷新会原样重放提交
        # （表现为每次刷新都重复触发一次任务），PRG 后刷新只是普通 GET。
        msg = data.get("msg") or ""
        err = data.get("err") or ""
        nchk = data.get("notify_check")
        if msg or err or nchk:
            bag = json.dumps({"msg": msg, "err": err, "notify_check": nchk},
                             ensure_ascii=False, default=str)
            sid0 = session_id(request)
            request.state.session["_flash"] = bag
            if not session_set(sid0, "_flash", bag):
                # 消息带不走（会话写失败）→ 退回整页渲染，保证错误仍可见
                return await flash(request, **data)
        return RedirectResponse("/", 303)
    return data


def _client_ip(request) -> str:
    """限速来源判定（v1.24.3 安全加固 M7A-03）。

    旧实现取 X-Forwarded-For **首段**：nginx 的 $proxy_add_x_forwarded_for 把
    客户端自带的 XFF 拼在最前面，攻击者每次请求换一个假 IP 就落进全新计数桶，
    5 次阈值永远凑不满 → 限速形同虚设（只剩 bcrypt 成本，弱密码可被在线爆破）。

    改为按可信度取：
    1) X-Real-IP（nginx 用 $remote_addr 显式覆盖，客户端无法注入）；
    2) XFF **最后一段**（离本机最近一跳追加的地址，伪造值会被真实 IP 顶掉）；
    3) TCP 直连地址。
    另在 loginguard 增加不分来源的全局兜底速率，使伪造 IP 的收益归零。
    """
    real = str(request.headers.get("x-real-ip") or "").strip()
    if real:
        return real
    xff = str(request.headers.get("x-forwarded-for") or "")
    if xff:
        parts = [p.strip() for p in xff.split(",") if p.strip()]
        if parts:
            return parts[-1]
    return request.client.host if request.client else "-"


def csrf_check(form, session: dict) -> bool:
    """等价 PHP csrf_check()：POST csrf 字段与 session token 常量时间比对。

    v1.24.4 修复（P1-9 / 审计 B-01）：旧实现只判 `token is None`，空串会走进
    `compare_digest("", "")` 并返回 True。而 csrf token 只在**渲染页面**时惰性
    生成，`h_login` 成功路径只写 auth 键 → 登录后到首次 GET 之间的窗口里
    session 无 csrf 键，此时 `action=setup_pass&csrf=`（不需要旧密码）即可改密
    接管面板。现在三条件缺一不可：
      1) session 里存在**非空** csrf 值；
      2) 提交的 csrf **非空**；
      3) 两者常量时间相等。
    """
    stored = str(session.get(cfg.CSRF_KEY) or "")
    token = str(form.get("csrf") or "")
    if not stored or not token:
        return False
    return _hash_equals(stored, token)


def _issue_csrf(session: dict, sid: str) -> str:
    """登录/设密成功时立即生成并落盘 csrf token（P1-9 的另一半）。

    页面渲染期的惰性生成（services/pagectx.py csrf_field）保留不动：这里只是把
    「session 里没有 csrf 键」的窗口从登录后一直存在的长度缩短到 0，使空 token
    绕过连理论窗口都不存在。写盘失败不影响登录本身（下次渲染会再补）。
    """
    tok = str(session.get(cfg.CSRF_KEY) or "")
    if not tok:
        tok = secrets.token_hex(16)
        session[cfg.CSRF_KEY] = tok
        try:
            session_set(sid, cfg.CSRF_KEY, tok)
        except OSError:
            pass
    return tok


# ===== 上传/目录参数校验（v1.24.4 修 P1-23 / P1-24）=====

# inst_dir 黑名单：这些目录做「实例项目目录」会直接把面板的配置写入/备份/删除
# 操作引到系统关键路径上（config.yaml 会被写到 /etc/config.yaml 之类）。
_SYS_DIRS = frozenset({
    "/", "/etc", "/usr", "/usr/local", "/usr/bin", "/usr/sbin", "/usr/lib",
    "/bin", "/sbin", "/lib", "/lib64", "/boot", "/dev", "/proc", "/sys",
    "/root", "/home", "/var", "/tmp", "/run", "/opt", "/srv", "/mnt", "/media",
})


def validate_inst_dir(raw: str) -> str:
    """校验实例项目目录，返回错误文案（空串=通过）。P1-24（审计 D-02）。

    旧实现只 `.strip().rstrip("/")` 后直接拼给 instances.py →
    `instance_config()` 变成 `inst_dir + "/config.yaml"`，于是
      - `inst_dir=/etc`      → 配置写入/备份/回滚全部落在 /etc；
      - `inst_dir=../..`     → 相对路径按面板进程 cwd 解析，可逃出面板目录；
      - `inst_dir=/`         → 拼出 `//config.yaml`。
    面板以 root 运行，等价于任意文件写入/删除。

    规则：必须是绝对路径、不含 `..` 段、规范化后不得命中系统关键目录黑名单。
    """
    d = str(raw or "").strip()
    if not d:
        return "项目目录不能为空"
    if not d.startswith("/"):
        return "项目目录必须是绝对路径"
    # 逐段检查 `..`（规范化会吃掉它，所以必须先查原始段）
    for seg in d.replace("\\", "/").split("/"):
        if seg == "..":
            return "项目目录不能包含 .. 路径段"
    norm = os.path.normpath(d)
    if not norm.startswith("/"):
        return "项目目录必须是绝对路径"
    if norm in _SYS_DIRS:
        return f"项目目录不能是系统目录：{norm}"
    return ""


def validate_yaml_text(text: str) -> str:
    """轻量 YAML 结构校验，返回错误文案（空串=通过）。P1-23（审计 D-01）。

    旧实现只查「扩展名 + ≤2MB + 含 `:`」就整文件覆盖 config.yaml → 上传
    `server:\n  port: [8080\n  bad: {{{` 这类明显损坏的 YAML 也会成功，
    容器下次启动直接读不到配置（面板失联，且备份文件名带随机后缀不好找）。

    不引入新依赖（pyyaml 不在 requirements 里），只做「结构自洽」检查：
      1) 逐行校验：tab 缩进、行尾未闭合引号、括号/花括号/方括号配平；
      2) 至少一行 `key:` 形式的映射键（纯文本/二进制被挡掉）；
      3) 整体括号深度最终归零。
    这是保守校验（宁可放过合法怪写法，也不误杀），但足以挡住截断/乱码/注入式
    的损坏内容 —— 审计里实测可写入的那几种都会被 1) 拦下。
    """
    if not text.strip():
        return "文件内容为空"
    depth = 0
    stack: list[str] = []
    pairs = {")": "(", "]": "[", "}": "{"}
    key_lines = 0
    in_block: str | None = None      # 处于 `|` / `>` 块标量内时不做括号/引号检查
    block_indent = 0
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" \t"))
        if in_block is not None:
            if indent > block_indent:
                continue             # 块标量正文，原样放行
            in_block = None
        if "\t" in line[:indent + 1] and line[:indent].count(" ") != indent:
            return f"第 {lineno} 行：缩进不能使用 Tab，请改用空格"
        if stripped.startswith("\t"):
            return f"第 {lineno} 行：缩进不能使用 Tab，请改用空格"
        # 块标量起始（`key: |` / `key: >` / `- |`）
        m_block = re.match(r"^(?:-\s+)?[^#]*:\s*[|>][+-]?\s*(?:#.*)?$", stripped)
        if m_block:
            in_block, block_indent = "|", indent
            continue
        if re.match(r"^[A-Za-z_][\w.-]*\s*:", stripped) or re.match(r"^-\s", stripped):
            if re.match(r"^[A-Za-z_][\w.-]*\s*:", stripped):
                key_lines += 1
        # 括号配平 + 引号闭合（跳过注释部分）
        i, quote = 0, None
        while i < len(stripped):
            ch = stripped[i]
            if quote:
                if quote == '"' and ch == "\\":
                    i += 2
                    continue
                if ch == quote:
                    quote = None
            elif ch in "\"'":
                quote = ch
            elif ch == "#" and (i == 0 or stripped[i - 1] in " \t"):
                break                # 行内注释，其后不参与配平
            elif ch in "([{":
                stack.append(ch)
                depth += 1
            elif ch in ")]}":
                if not stack or stack[-1] != pairs[ch]:
                    return f"第 {lineno} 行：括号不匹配（多余的 {ch}）"
                stack.pop()
                depth -= 1
            i += 1
        if quote:
            return f"第 {lineno} 行：引号未闭合"
    if in_block is not None:
        pass                          # 块标量到文件尾是合法的
    if stack:
        return "文件内容异常：括号/引号未闭合，疑似配置被截断"
    if key_lines == 0:
        return "文件内容异常，未识别到有效的 YAML 映射键"
    return ""



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
        _issue_csrf(session, sid)            # P1-9：登录即发 csrf，不留空窗
        if old_sid and old_sid != sid:
            session_remove(old_sid, cfg.SKEY)  # 旧会话注销（尽力而为）
        return RD(set_sid=sid)
    return F(err="密码文件写入失败，请检查面板目录权限")


def h_login(request, form, session, sid):
    """登录：单 IP 锁定 + 全局失败计数兜底（v1.24.4 修 P1-10 自伤式 DoS）。

    旧实现在**校验密码之前**就用 global_locked() 硬拒绝，而成功登录是清零全局桶
    的唯一途径 → 一旦被人打满 20 次/分钟，锁定期内成功路径永远走不到，所有人都
    永久无法登录。现在改为「先验密码、成功即放行并清零」：
      - 单 IP 锁定（5 次/5 分钟）仍在密码校验前生效（这是有意义的防爆破）；
      - 全局桶只影响**失败**路径的文案与记录，不再拒绝正确的密码；
      - 任一次成功登录 → lg.ok() 整体清零全局桶。
    这样攻击者付出 20 次/分钟的成本，收益从「锁死所有人」降为「无收益」。
    """
    p = str(form.get("pass") or "")
    ip = _client_ip(request)
    if not lg.check(ip):
        return F(err=f"尝试过于频繁，请 {lg.remaining(ip)} 秒后再试")
    if passwd.check_pass(p):
        lg.ok(ip)                            # 清单 IP 桶 + 整体清全局桶
        old_sid = sid
        sid = new_sid()                      # 总是轮换（M5：会话固定防御）
        session[cfg.SKEY] = True
        if not session_set(sid, cfg.SKEY, True):
            session.pop(cfg.SKEY, None)
            return F(err="保存登录状态失败，请重试")
        _issue_csrf(session, sid)            # P1-9：登录即发 csrf，不留空窗
        if old_sid and old_sid != sid:
            session_remove(old_sid, cfg.SKEY)  # 旧会话注销（尽力而为）
        return RD(set_sid=sid)
    lg.fail(ip)
    lg.global_fail(ip)
    gl = lg.global_locked()
    if gl > 0:
        # 全局失败风暴中：失败者看到限速文案，合法管理员不受影响。
        # 只有「不同来源数 ≥ GLOBAL_SOURCES_MIN」才判定为分布式爆破并明说，
        # 单人反复失败（来源数≈1）不升级文案，避免误导运维。
        if lg.global_sources() >= lg.GLOBAL_SOURCES_MIN:
            return F(err=f"登录尝试过多（疑似分布式爆破），请 {gl} 秒后再试")
        return F(err=f"登录尝试过多，请 {gl} 秒后再试")
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
    # v1.24.4（P1-23）：结构校验。旧实现只查「扩展名 + 大小 + 含冒号」，
    # 截断/乱码的 YAML 也能整文件覆盖 config.yaml → 容器下次启动读不到配置。
    verr = validate_yaml_text(text)
    if verr:
        return F(err=verr)
    inst = instance_current(session)
    bak = config_backup(inst)
    if not bak:
        # 旧实现备份失败也照样覆盖（备份名 "无"）→ 写坏就没有回滚点。
        return F(err="原配置备份失败，已取消写入（避免无法回滚）")
    try:
        Path(instance_config(inst)).write_text(text, "utf-8")
    except OSError:
        return F(err=f"写入失败，请检查文件权限：chmod 666 {instance_config(inst)}")
    bakname = Path(bak).name
    return F(msg=f"配置已恢复（{fname}），原配置备份：{bakname}。需重启容器生效。")


# ===== 多实例 3（W5）=====


def h_instance_switch(request, form, session, sid):
    target = str(form.get("instance_id") or "").strip()
    if target and instance_switch_to(session, sid, target):
        inst = instance_current(session)
        return F(msg=f"已切换到实例：{instance_name(inst)}（{instance_container(inst)}）")
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
    # v1.24.4（P1-24）：inst_dir 此前完全不校验，直接拼成 `<dir>/config.yaml`
    # 交给写盘/备份/回滚 → 可设 /etc、/ 或 ../.. 实现任意路径写入（面板以 root 跑）。
    derr = validate_inst_dir(_dir)
    if derr:
        return F(err=derr)

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
        msg = f"实例「{name}」已添加并切换" if is_new else f"实例「{name}」已更新"
        return F(msg=msg)
    return F(err=f"写入 {Path(cfg.INSTANCES_FILE).name} 失败，请检查面板目录权限")


def h_instance_delete(request, form, session, sid):
    _id = str(form.get("inst_id") or "").strip()
    confirm = str(form.get("inst_confirm") or "").strip()
    lst = instances_load()
    target = next((it for it in lst if it["id"] == _id), None)
    if not target:
        return F(err="实例不存在")
    if confirm != target["name"]:
        return F(err=f"确认失败：请输入实例名称「{target['name']}」以删除")
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
    return F(msg=f"实例「{target['name']}」已删除")


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


def _alert_channels(form) -> dict:
    """v1.24：分级通道（留空 = 继承主通道）→ 面板配置键。"""
    out = {}
    for sev in ("info", "warn", "critical"):
        v = str(form.get("alert_channel_" + sev) or "").strip()
        out["alert_channel_" + sev] = v if v in ("bark", "serverchan", "webhook") else ""
    return out


def h_alert_save(request, form, session, sid):
    ch_map = ("bark", "serverchan", "webhook")
    en = "1" if form.get("alert_enable") else "0"
    ch = str(form.get("alert_channel") or "")
    ch = ch if ch in ch_map else "bark"
    tg = str(form.get("alert_target") or "").strip()

    def _int(key, dft, lo, hi):
        try:
            n = int(str(form.get(key) or "").strip())
        except (TypeError, ValueError):
            n = int(dft)
        return max(int(lo), min(int(hi), n))

    # v1.24 M2-6：磁盘阈值（critical 必须比 warn 更严格，否则自动收一格）
    dw = _int("disk_warn_percent", cfg.DISK_WARN_PERCENT, 1, 99)
    dc = _int("disk_crit_percent", cfg.DISK_CRIT_PERCENT, 1, 99)
    if dc >= dw:
        dc = max(1, dw - 1)

    # v1.24 M2-7：晨报时间（HH:MM，非法回落 08:00）
    try:
        hh, mm = [int(x) for x in str(form.get("morning_report_time") or "").split(":")[:2]]
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            raise ValueError
    except (TypeError, ValueError):
        hh, mm = 8, 0

    mute = ",".join([s for s in ("info", "warn", "critical")
                     if form.get("alert_mute_" + s)])
    upd = {
        "alert_enable": en,
        "alert_channel": ch,
        "alert_target": tg,
        # v1.24 M2-5：分级静音（勾选的级别只记录不推送）
        "alert_mute_levels": mute,
        # v1.24 M2-6：磁盘预警
        "disk_alert_enable": "1" if form.get("disk_alert_enable") else "0",
        "disk_warn_percent": dw,
        "disk_crit_percent": dc,
        # v1.24 M2-7：每日晨报
        "morning_report_enable": "1" if form.get("morning_report_enable") else "0",
        "morning_report_time": f"{hh:02d}:{mm:02d}",
        # v1.24 M2-8：上游本体更新提醒
        "upstream_check_enable": "1" if form.get("upstream_check_enable") else "0",
    }
    upd.update(_alert_channels(form))
    if panel_config_save(upd):
        return J({"ok": True, "msg": "告警设置已保存"})
    return J({"ok": False, "msg": "写入失败，请检查 .panel_config.php 权限"})


def h_alert_test(request, form, session, sid):
    """v1.24：支持按级别测试（severity=info/warn/critical）。"""
    ch_map = ("bark", "serverchan", "webhook")
    ch = str(form.get("alert_channel") or "")
    sev = str(form.get("severity") or "warn")
    if sev not in ("info", "warn", "critical"):
        sev = "warn"
    name = {"info": "提示", "warn": "警告", "critical": "严重"}[sev]
    ov = {
        "enable": True,
        "channel": ch if ch in ch_map else "bark",
        "target": str(form.get("alert_target") or "").strip(),
    }
    r = alert_send(f"M7A 面板测试告警（{name}）",
                   f"看到这条消息说明「{name}」级告警通道配置正确 ✅（来自面板测试）",
                   ov, severity=sev)
    return J({"ok": bool(r.get("ok")), "msg": r.get("msg", "")})


def h_morning_report_now(request, form, session, sid):
    """v1.24 M2-7：立即发一份晨报（忽略时间/当日已发限制）。"""
    from services.alerts import morning_report
    r = morning_report(instance_current(session), force=True)
    return J({"ok": bool(r.get("ok")), "msg": r.get("msg", "")})


def h_upstream_check_now(request, form, session, sid):
    """v1.24 M2-8：立即检查一次上游本体版本（有新版会推 warn 级提醒）。"""
    from services.alerts import upstream_check
    r = upstream_check(instance_current(session), force=True)
    if r.get("error"):
        msg = f"检查失败：{r['error']}"
    elif r.get("has_update"):
        msg = f"发现新版本 v{r.get('latest')}（当前 v{r.get('local') or '未知'}）"
    elif r.get("latest"):
        msg = f"已是最新（v{r.get('latest')}）"
    else:
        msg = "未取到上游版本信息"
    return J({"ok": bool(r.get("ok")), "msg": msg, "has_update": bool(r.get("has_update"))})


def h_disk_check_now(request, form, session, sid):
    """v1.24 M2-6：立即跑一次磁盘探针（不改变告警级别状态时不会重复推送）。"""
    from services.alerts import disk_check, disk_line
    r = disk_check(instance_current(session), force=True)
    w = r.get("worst") or {}
    if r.get("ok") and w:
        msg = "磁盘检查完成：" + disk_line(w)
    else:
        msg = str(r.get("msg") or "磁盘探针不可用")
    return J({"ok": bool(r.get("ok")), "msg": msg, "level": r.get("level", "")})


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
        elif f.get("type") == "textarea":
            # v1.24.4 修复（交接自 fix-handlers）：textarea（webhook headers/body、
            # 自定义 JSON/图片）是用户手工编排的内容，首尾空白与末尾换行有语义，
            # 不能 strip。仅把 CRLF 归一化为 LF，其余原样保留。
            v = str(form.get(post_key) or "").replace("\r\n", "\n").replace("\r", "\n")
            updates[key] = yaml_format_val(v, f.get("type"))
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


def h_alert_clear(request, form, session, sid):
    """POST action=alert_clear → 清空告警历史（SQLite events type=alert）。

    v1.22：与其它写操作一致走 CSRF 校验；失败不抛，返回结构化结果。
    """
    from services import eventdb
    n = eventdb.clear_events("alert")
    if n < 0:
        return J({"ok": False, "msg": "清空失败，请检查 data 目录写权限"})
    return J({"ok": True, "msg": f"告警历史已清空（{n} 条）"})


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
    "morning_report_now": h_morning_report_now,
    "upstream_check_now": h_upstream_check_now,
    "disk_check_now": h_disk_check_now,
    "set_update_mode": h_set_update_mode,
    "set_monitor_interval": h_set_monitor_interval,
    "save_config_form": h_save_config_form,
    "save_config_text": h_save_config_text,
    # 回滚 / 历史
    "backup_rollback": h_backup_rollback,
    "history_clear": h_history_clear,
    "alert_clear": h_alert_clear,
    # 计划任务
    "schedule_add": h_schedule_add,
    "schedule_del": h_schedule_del,
    "schedule_toggle": h_schedule_toggle,
    "schedule_conflict": h_schedule_conflict,
    "schedule_now": h_schedule_now,
}


_TASK_COOLDOWN: dict[str, float] = {}
TASK_COOLDOWN_SECONDS = 15.0


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

    # v1.21.1：任务类 action 短窗口幂等——刷新重放/连点 15 秒内同会话同任务只执行一次
    if action in cfg.TASKS:
        now = time.monotonic()
        tkey = f"{sid}|{action}"
        prev = _TASK_COOLDOWN.get(tkey)
        if prev is not None and now - prev < TASK_COOLDOWN_SECONDS:
            return await render(
                request,
                F(msg=f"任务「{cfg.TASKS[action]}」刚刚已启动，重复提交已忽略"))
        _TASK_COOLDOWN[tkey] = now
        if len(_TASK_COOLDOWN) > 256:
            for k in [k for k, v in _TASK_COOLDOWN.items()
                      if now - v >= TASK_COOLDOWN_SECONDS]:
                _TASK_COOLDOWN.pop(k, None)

    # v1.18：用户主动容器操作 → 告警静默 10 分钟
    if action in cfg.DANGEROUS_ACTIONS:
        alert_quiet(instance_current(session), 600)

    handler = POST_HANDLERS.get(action)
    if handler is None:
        # v1.22 M6：Python 为唯一后端；未知 action 不再回源，本地渲染面板页兜底。
        # （回滚开关打开时仍走网关转发）
        if cfg.FWD_ENABLE:
            from gateway import forward
            return await forward(request)
        from routers.panel import render_fallback
        return render_fallback(request)

    import anyio.to_thread as to_thread
    result = await to_thread.run_sync(handler, request, form, session, sid)
    return await render(request, result)


async def _forward_unauth_page(request):
    """未登录且非 login/setup_pass：与 PHP 一致渲染登录页（M5-B 起 Python 渲染）。"""
    from services.pagectx import page_response

    return page_response(request)
