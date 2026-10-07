"""M2 POST 分发级测试：36 个 action 每个至少一条用例（W1-W5）。

约定：所有路径从 config 模块读取（兼容与 test_dispatch 同进程运行）；
会话/实例数据落在测试 tmp 目录；危险命令一律在 routers.write 层打桩。
"""
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config as cfg
from fastapi.testclient import TestClient

from main import app
from services.instances import instances_write

client = TestClient(app)

# ===== 测试实例：目录落在 tests/_tmp 下，config 写这里 =====
INST_DIR = Path(__file__).resolve().parent / "_tmp" / "m2inst"
(INST_DIR / "logs").mkdir(parents=True, exist_ok=True)
(INST_DIR / "data").mkdir(parents=True, exist_ok=True)
(INST_DIR / "config.yaml").write_text(
    "locales:\n  zh_CN: 简体中文\npower_enable: true\n", "utf-8")
# 清掉上一轮残留状态，保证用例独立（计划任务落在 config.DATA_DIR）
(cfg.DATA_DIR / "schedule.json").unlink(missing_ok=True)
(INST_DIR / "data" / "schedule.json").unlink(missing_ok=True)
TEST_INST = {"id": "m7a", "name": "测试实例", "container": "m7a-test",
             "dir": str(INST_DIR), "default": True}
instances_write([TEST_INST])

TOKEN = "m2token"


def new_session(authed=True):
    sid = "m2" + uuid.uuid4().hex[:10]
    parts = []
    if authed:
        parts.append("m7a_panel_auth|b:1;")
    parts.append(f'm7a_panel_csrf|s:{len(TOKEN)}:"{TOKEN}";')
    p = Path(cfg.SESSION_SAVE_PATH)
    p.mkdir(parents=True, exist_ok=True)
    (p / f"sess_{sid}").write_text("".join(parts), encoding="utf-8")
    return {"PHPSESSID": sid}


def _hop(r, cookies):
    """手动跟随 3xx（TestClient 自动跟随时会丢 per-request cookies）。"""
    if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
        return client.get(r.headers["location"], cookies=cookies or {})
    return r


def post(action, data=None, cookies=None, follow=True):
    # v1.21.1：表单提交走 PRG（303→GET），默认手动跟随回到消息页；
    # 断言 302 的用例传 follow=False 取中间响应。
    payload = {"action": action, "csrf": TOKEN}
    payload.update(data or {})
    ck = cookies or new_session()
    r = client.post("/", data=payload, cookies=ck, follow_redirects=False)
    return _hop(r, ck) if follow else r


def session_text(cookies) -> str:
    p = Path(cfg.SESSION_SAVE_PATH) / f"sess_{cookies['PHPSESSID']}"
    return p.read_text(encoding="utf-8") if p.is_file() else ""


# ---------- 注册表 ----------

def test_registry_covers_all_36_actions():
    from routers.write import POST_HANDLERS

    expected = {
        "setup_pass", "login", "logout", "restore_config",
        "instance_switch", "instance_save", "instance_delete",
        "restart", "update", "update_image", "stop_task", "stop_loop",
        "stop", "do_update", "set_after_finish", "alert_save", "alert_test",
        "set_update_mode", "set_monitor_interval", "save_config_form",
        "save_config_text", "backup_rollback", "history_clear", "alert_clear",
        "schedule_add", "schedule_del", "schedule_toggle",
        "schedule_conflict", "schedule_now",
        "main", "daily", "power", "notify", "divergentloop",
        "universe", "currencywars", "currencywarsloop",
    }
    assert set(POST_HANDLERS) == expected
    assert len(expected) == 37


# ---------- W1：认证 4 + CSRF + 危险静默 ----------

def test_setup_pass_short_password():
    r = post("setup_pass", {"pass1": "123", "pass2": "123"})
    assert r.status_code == 200
    assert "密码至少 6 位" in r.text


def test_setup_pass_mismatch():
    r = post("setup_pass", {"pass1": "abcdef", "pass2": "abcdex"})
    assert "两次输入的密码不一致" in r.text


def test_setup_pass_ok(monkeypatch):
    from services import passwd

    called = {}
    monkeypatch.setattr(passwd, "set_pass", lambda pw: called.update(pw=pw) or True)
    cookies = new_session(authed=False)
    r = post("setup_pass", {"pass1": "secret6", "pass2": "secret6"}, cookies,
            follow=False)
    assert r.status_code == 302
    assert called["pw"] == "secret6"
    # M5 轮换：设置成功后登录态写入新 sid（响应 Set-Cookie），旧 sid 注销
    from http.cookies import SimpleCookie
    jar = SimpleCookie()
    jar.load(r.headers.get("set-cookie", ""))
    assert "PHPSESSID" in jar
    sid = jar["PHPSESSID"].value
    p = Path(cfg.SESSION_SAVE_PATH)
    assert "m7a_panel_auth" in (p / f"sess_{sid}").read_text(encoding="utf-8")
    (p / f"sess_{sid}").unlink(missing_ok=True)          # 测试自产临时文件


def test_login_wrong_password():
    r = post("login", {"pass": "nope"}, new_session(authed=False))
    assert r.status_code == 200
    assert "密码错误" in r.text


def test_login_ok(monkeypatch):
    from services import passwd

    monkeypatch.setattr(passwd, "check_pass", lambda pw: pw == "right")
    cookies = new_session(authed=True)                 # 旧会话已有登录态
    old_sid = cookies["PHPSESSID"]
    r = post("login", {"pass": "right"}, cookies, follow=False)
    assert r.status_code == 302
    # M5 轮换：登录成功总是换新 sid（会话固定防御），旧会话被注销
    from http.cookies import SimpleCookie
    jar = SimpleCookie()
    jar.load(r.headers.get("set-cookie", ""))
    assert "PHPSESSID" in jar
    sid = jar["PHPSESSID"].value
    assert sid != old_sid
    p = Path(cfg.SESSION_SAVE_PATH)
    assert "m7a_panel_auth" in (p / f"sess_{sid}").read_text(encoding="utf-8")
    assert "m7a_panel_auth" not in session_text(cookies)
    (p / f"sess_{sid}").unlink(missing_ok=True)         # 测试自产临时文件


def test_logout_clears_session():
    cookies = new_session(authed=True)
    r = post("logout", {}, cookies, follow=False)
    assert r.status_code == 302
    assert "m7a_panel_auth" not in session_text(cookies)


def test_login_no_cookie_creates_session(monkeypatch):
    """无 PHPSESSID 的登录（全新浏览器/清 cookie）：生成新 sid 并下发 Set-Cookie。"""
    from services import passwd

    monkeypatch.setattr(passwd, "check_pass", lambda pw: pw == "right")
    r = client.post("/", data={"action": "login", "pass": "right"},
                    cookies={}, follow_redirects=False)
    assert r.status_code == 302
    sc = r.headers.get("set-cookie", "")
    assert "PHPSESSID=" in sc
    # 6-bit 字符集 [a-zA-Z0-9,-] 的 sid 有 ~40% 概率含逗号，标准库 Set-Cookie 编码会
    # 转义成 \054（客户端自动反转义）；用 SimpleCookie 解析拿到原始 32 位 sid。
    from http.cookies import SimpleCookie
    jar = SimpleCookie()
    jar.load(sc)
    assert "PHPSESSID" in jar
    sid = jar["PHPSESSID"].value
    assert len(sid) == 32
    p = Path(cfg.SESSION_SAVE_PATH) / f"sess_{sid}"
    assert p.is_file() and "m7a_panel_auth|b:1;" in p.read_text(encoding="utf-8")
    p.unlink(missing_ok=True)                              # 测试自产临时文件


def test_login_write_failure_shows_error(monkeypatch):
    """会话写入失败必须可见（修复前的静默 False → 登录假成功死循环）。"""
    from services import passwd
    import routers.write as W

    monkeypatch.setattr(passwd, "check_pass", lambda pw: True)
    monkeypatch.setattr(W, "session_set", lambda sid, k, v: False)
    cookies = new_session(authed=False)
    r = post("login", {"pass": "x"}, cookies)
    assert r.status_code == 200
    assert "保存登录状态失败" in r.text
    assert "m7a_panel_auth" not in session_text(cookies)  # 不留假登录态


def test_logout_write_failure_keeps_login(monkeypatch):
    """退出写入失败 → 维持登录态并报错（不假退出）。"""
    import routers.write as W

    monkeypatch.setattr(W, "session_remove", lambda sid, k: False)
    cookies = new_session(authed=True)
    r = post("logout", {}, cookies)
    assert r.status_code == 200
    assert "退出失败" in r.text
    assert "m7a_panel_auth" in session_text(cookies)


def test_unauth_post_renders_login():
    r = post("restart", {}, new_session(authed=False))
    assert r.status_code == 200
    assert "auth-card" in r.text              # M5-B：未登录 POST → Python 登录页


def test_csrf_missing_and_invalid():
    cookies = new_session()
    r1 = client.post("/", data={"action": "restart"}, cookies=cookies,
                     follow_redirects=False)
    assert r1.status_code == 303
    assert "请求验证失败" in _hop(r1, cookies).text
    r2 = client.post("/", data={"action": "restart", "csrf": "wrong"},
                     cookies=cookies, follow_redirects=False)
    assert r2.status_code == 303
    assert "请求验证失败" in _hop(r2, cookies).text


def test_dangerous_action_alerts_quiet(monkeypatch):
    import routers.write as W

    calls = []
    monkeypatch.setattr(W, "alert_quiet", lambda inst, sec: calls.append((inst["id"], sec)))
    monkeypatch.setattr(W, "compose", lambda inst, a: {"code": 0, "out": ""})
    r = post("restart")
    assert "容器已重启" in r.text
    assert calls == [("m7a", 600)]


def test_non_dangerous_action_not_quiet(monkeypatch):
    import routers.write as W

    calls = []
    monkeypatch.setattr(W, "alert_quiet", lambda inst, sec: calls.append(1))
    monkeypatch.setattr(W, "ensure_running", lambda inst, timeout=15.0: {"started": False})
    monkeypatch.setattr(W, "task_start", lambda inst, sub: {"code": 0, "out": ""})
    monkeypatch.setattr(W, "history_add", lambda inst, k, l: None)
    post("daily")
    assert calls == []


def test_unknown_action_local_render():
    # v1.22 M6：Python 唯一后端，未知 action 本地渲染面板页兜底，不回源
    r = post("not_migrated_action")
    assert r.status_code == 200
    assert "<!-- ===== 概览 ===== -->" in r.text


# ---------- W1：恢复配置 ----------

def test_restore_config_bad_ext():
    files = {"cfg_file": ("x.txt", b"hello world: 12345", "text/plain")}
    ck = new_session()
    r = client.post("/", data={"action": "restore_config", "csrf": TOKEN},
                    files=files, cookies=ck, follow_redirects=False)
    r = _hop(r, ck)
    assert "文件格式错误" in r.text


def test_restore_config_ok(monkeypatch):
    from services import configops

    monkeypatch.setattr(configops, "config_backup", lambda inst: "/no/backup")
    # write.py 按名引入：同样替换其绑定
    import routers.write as W
    monkeypatch.setattr(W, "config_backup", lambda inst: "/no/backup")
    files = {"cfg_file": ("good.yaml", b"locales:\n  zh_CN: x\n", "text/plain")}
    ck = new_session()
    r = client.post("/", data={"action": "restore_config", "csrf": TOKEN},
                    files=files, cookies=ck, follow_redirects=False)
    assert r.status_code == 303
    assert "配置已恢复" in _hop(r, ck).text
    assert (INST_DIR / "config.yaml").read_text(encoding="utf-8") == "locales:\n  zh_CN: x\n"


# ---------- W5：多实例 3 ----------

def test_instance_switch_ok():
    r = post("instance_switch", {"instance_id": "m7a"})
    assert "已切换到实例：测试实例" in r.text


def test_instance_switch_missing():
    r = post("instance_switch", {"instance_id": "nope"})
    assert "切换失败：实例不存在" in r.text


def test_instance_save_validation():
    r = post("instance_save", {"inst_id": "", "inst_name": "",
                               "inst_container": "", "inst_dir": ""})
    assert "名称、容器名、项目目录均不能为空" in r.text
    r2 = post("instance_save", {"inst_id": "", "inst_name": "x",
                                "inst_container": "bad name!", "inst_dir": "/a"})
    assert "容器名只能包含字母" in r2.text


def test_instance_save_new(monkeypatch):
    import routers.write as W

    monkeypatch.setattr(W, "instances_load", lambda: [])
    monkeypatch.setattr(W, "instances_write", lambda items: True)
    monkeypatch.setattr(W, "instance_switch_to", lambda s, sid, t: True)
    r = post("instance_save", {"inst_id": "", "inst_name": "副实例",
                               "inst_container": "second", "inst_dir": "/tmp/second"})
    assert "已添加并切换" in r.text


def test_instance_delete_confirm_mismatch():
    r = post("instance_delete", {"inst_id": "m7a", "inst_confirm": "别的名字"})
    assert "确认失败" in r.text


# ---------- W3：任务 8 ----------

def test_all_8_task_actions(monkeypatch):
    import routers.write as W

    started, added = [], []
    monkeypatch.setattr(W, "ensure_running", lambda inst, timeout=15.0: {"started": False})
    monkeypatch.setattr(W, "task_start", lambda inst, sub: started.append(sub) or {"code": 0, "out": ""})
    monkeypatch.setattr(W, "history_add", lambda inst, k, l: added.append(k))
    for key, label in cfg.TASKS.items():
        r = post(key)
        assert r.status_code == 200
        assert f"任务「{label}」已后台启动" in r.text, key
    assert started == list(cfg.TASKS)
    assert added == list(cfg.TASKS)


def test_task_start_failure(monkeypatch):
    import routers.write as W

    monkeypatch.setattr(W, "ensure_running", lambda inst, timeout=15.0: {"started": False})
    monkeypatch.setattr(W, "task_start", lambda inst, sub: {"code": 1, "out": "boom"})
    r = post("power")
    assert "任务启动失败：boom" in r.text


# ---------- W3：容器 7 ----------

def test_restart_and_stop_and_stop_task(monkeypatch):
    import routers.write as W

    monkeypatch.setattr(W, "compose", lambda inst, a: {"code": 0, "out": ""})
    assert "容器已重启" in post("restart").text
    assert "任务已停止" in post("stop_task").text
    assert "容器已停止" in post("stop").text


def test_restart_failure(monkeypatch):
    import routers.write as W

    monkeypatch.setattr(W, "compose", lambda inst, a: {"code": 1, "out": "denied"})
    assert "重启失败：denied" in post("restart").text


def test_update_ok_and_pull_fail(monkeypatch):
    import routers.write as W

    results = [{"code": 0, "out": ""}, {"code": 0, "out": ""}]
    monkeypatch.setattr(W, "compose", lambda inst, a: results.pop(0))
    assert "镜像已更新，容器已重建" in post("update").text

    monkeypatch.setattr(W, "compose", lambda inst, a: {"code": 1, "out": "net err"})
    assert "拉取镜像失败：net err" in post("update").text


def test_stop_loop(monkeypatch):
    import routers.write as W

    monkeypatch.setattr(W, "run_cmd", lambda cmd, timeout=120: {"code": 0, "out": ""})
    assert "货币战争循环已停止" in post("stop_loop").text
    monkeypatch.setattr(W, "run_cmd", lambda cmd, timeout=120: {"code": 1, "out": "no proc"})
    assert "循环停止失败" in post("stop_loop").text


def test_update_image_json(monkeypatch):
    import routers.write as W

    monkeypatch.setattr(W, "image_update", lambda inst: {"ok": True, "msg": "已是最新"})
    r = post("update_image")
    assert r.status_code == 200
    assert r.json() == {"ok": True, "msg": "已是最新"}


def test_do_update_json(monkeypatch):
    import routers.write as W

    monkeypatch.setattr(W, "do_update", lambda: {"ok": True, "msg": "更新完成"})
    r = post("do_update")
    assert r.json() == {"ok": True, "msg": "更新完成"}


# ---------- W4：配置 7 ----------

def test_set_after_finish(monkeypatch):
    import routers.write as W

    saved = {}
    monkeypatch.setattr(W, "config_backup", lambda inst: "/no/bak")
    monkeypatch.setattr(W, "config_save_form",
                        lambda inst, u: saved.update(u) or {"ok": True, "msg": "已更新 1 项配置"})
    r = post("set_after_finish", {"value": "Exit"})
    d = r.json()
    assert d["ok"] is True and "跑完自动退出游戏" in d["msg"]
    assert saved.get("after_finish") is not None
    r2 = post("set_after_finish", {"value": "None"})
    assert "跑完保持界面" in r2.json()["msg"]


def test_alert_save_roundtrip():
    r = post("alert_save", {"alert_enable": "1", "alert_channel": "serverchan",
                            "alert_target": "target-1"})
    assert r.json() == {"ok": True, "msg": "告警设置已保存"}
    from services.instances import panel_config

    cfgv = panel_config()
    assert cfgv.get("alert_enable") == "1"
    assert cfgv.get("alert_channel") == "serverchan"


def test_alert_test(monkeypatch):
    import routers.write as W

    monkeypatch.setattr(W, "alert_send",
                        lambda t, b, ov=None: {"ok": True, "msg": "已发送"})
    r = post("alert_test", {"alert_channel": "bark", "alert_target": "tk"})
    assert r.json() == {"ok": True, "msg": "已发送"}


def test_set_update_mode_and_monitor_interval():
    r = post("set_update_mode", {"mode": "manual"})
    assert r.json()["ok"] is True and "手动更新" in r.json()["msg"]
    r2 = post("set_monitor_interval", {"interval": "99999"})
    assert r2.json()["ok"] is True and "3600 秒" in r2.json()["msg"]


def test_save_config_form_restart_branch(monkeypatch):
    import routers.write as W

    monkeypatch.setattr(W, "config_backup", lambda inst: "/no/bak")
    monkeypatch.setattr(W, "config_save_form",
                        lambda inst, u: {"ok": True, "msg": "已更新 1 项配置"})
    monkeypatch.setattr(W, "compose", lambda inst, a: {"code": 0, "out": ""})
    r = post("save_config_form", {"then_restart": "1"})
    assert "已更新 1 项配置" in r.text and "容器已重启，配置已生效" in r.text
    r2 = post("save_config_form", {})
    assert "需重启容器生效" in r2.text


def test_save_config_text_ok_and_bad():
    r = post("save_config_text",
             {"yaml_content": "locales:\n  zh_CN: 简体中文\npower_enable: true\n"})
    assert "配置已保存" in r.text
    r2 = post("save_config_text", {"yaml_content": "不是配置的纯文本"})
    assert "配置" in r2.text and r2.status_code == 200


# ---------- W5：回滚 / 历史 ----------

def test_backup_rollback_json(monkeypatch):
    import routers.write as W

    monkeypatch.setattr(W, "backup_rollback",
                        lambda f: {"ok": True, "msg": f"已回滚 {f}"})
    r = post("backup_rollback", {"file": "bak-v1.18.zip"})
    assert r.json() == {"ok": True, "msg": "已回滚 bak-v1.18.zip"}


def test_history_clear_json():
    r = post("history_clear")
    assert r.json() == {"ok": True, "msg": "任务执行历史已清空"}


# ---------- W5：计划任务 5 ----------

def _sched_post(name="晨跑", t="07:30", days=("1", "3"), args="daily"):
    return post("schedule_add", {"sched_name": name, "sched_time": t,
                                 "sched_days": list(days), "sched_args": args})


def test_schedule_add_toggle_del():
    r = _sched_post()
    assert "计划任务「晨跑」已保存" in r.text
    assert "每日" not in r.text or "执行" in r.text

    # 取出 id 再启停/删除
    import json
    from services.schedule import schedule_load

    d = schedule_load(TEST_INST)
    assert len(d["tasks"]) == 1
    tid = d["tasks"][0]["id"]
    assert d["tasks"][0]["enabled"] is True

    r2 = post("schedule_toggle", {"sched_id": tid})
    assert "已停用" in r2.text
    r3 = post("schedule_toggle", {"sched_id": tid})
    assert "已启用" in r3.text
    r4 = post("schedule_del", {"sched_id": tid})
    assert "已删除" in r4.text
    r5 = post("schedule_del", {"sched_id": tid})
    assert "没有找到该计划任务" in r5.text
    assert json.dumps(d, ensure_ascii=False)  # 读取正常


def test_schedule_add_validation_error():
    r = post("schedule_add", {"sched_name": "x", "sched_time": "bad",
                              "sched_days": ["1"], "sched_args": "daily"})
    assert "计划任务保存失败" in r.text


def test_schedule_conflict():
    r = post("schedule_conflict", {"conflict": "stop"})
    assert "停掉当前任务再执行" in r.text
    r2 = post("schedule_conflict", {"conflict": "skip"})
    assert "跳过本次" in r2.text


def test_schedule_now_no_due(monkeypatch):
    import routers.write as W

    monkeypatch.setattr(W, "schedule_run_due",
                        lambda inst, now=None: {"ok": True, "time": "12:00",
                                                "ran": [], "skipped": []})
    r = post("schedule_now")
    assert "触发 0 个" in r.text
