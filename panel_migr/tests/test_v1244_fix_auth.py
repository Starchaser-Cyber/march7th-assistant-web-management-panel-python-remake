"""V1.24.4 修复回归测试 A（认证 / 会话 / CSRF / 上传校验 / 依赖声明）。

对应第三方审计 B_auth.md 与 D_handlers.md 的 P1 项：
- P1-9  CSRF 空 token 绕过（登录后 session 无 csrf 键 → `csrf=` 空串即通过）
- P1-10 全局登录锁自伤式 DoS（成功登录不清零 + 校验密码前就拒绝）
- P1-11 session 整文件读改写无锁（并发下登出后 auth 键复活）
- P1-12 session 写缺 O_NOFOLLOW（符号链接写穿 → 以 root 改写任意文件）
- P1-13 文档仍写 M7A_SESSION_DIR 默认 /tmp（按文档配置即复活 M7A-02）
- P1-23 h_restore_config 无 YAML 结构校验 / 备份失败仍覆盖
- P1-24 inst_dir 无任何校验（可设 /etc、../..）
- P1-25 websockets 依赖未声明
- 顺带：write.py 6 处 h() + _banner() 双重转义

全部离线（monkeypatch / tmp_path），不联网、不碰本机 docker。
"""
import io
import os
import stat
import sys
import threading
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import config as cfg
from fastapi.testclient import TestClient

from main import app
import phpsess
from routers import write as W
from services import loginguard as lg
from services.flash import _banner

client = TestClient(app)

ROOT = Path(__file__).resolve().parent.parent      # panel_migr/
REPO = ROOT.parent                                 # 仓库根

TOKEN = "v44authtoken"


# ================= 通用 helper =================

def new_session(authed=True, csrf=True):
    """在测试 session 目录造一个会话文件（与 test_m5_security 同风格）。"""
    sid = "a44" + uuid.uuid4().hex[:10]
    parts = []
    if authed:
        parts.append("m7a_panel_auth|b:1;")
    if csrf:
        parts.append(f'm7a_panel_csrf|s:{len(TOKEN)}:"{TOKEN}";')
    p = Path(cfg.SESSION_SAVE_PATH)
    p.mkdir(parents=True, exist_ok=True)
    (p / f"sess_{sid}").write_text("".join(parts), encoding="utf-8")
    return {"PHPSESSID": sid}


def _hop(r, cookies):
    if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
        return client.get(r.headers["location"], cookies=cookies or {})
    return r


def post(action, data=None, cookies=None, follow=True):
    payload = {"action": action, "csrf": TOKEN}
    payload.update(data or {})
    ck = cookies or new_session()
    r = client.post("/", data=payload, cookies=ck, follow_redirects=False)
    return _hop(r, ck) if follow else r


def _resp_sid(resp) -> str:
    from http.cookies import SimpleCookie

    jar = SimpleCookie()
    jar.load(resp.headers.get("set-cookie", ""))
    return jar["PHPSESSID"].value if "PHPSESSID" in jar else ""


def _sess_text(sid: str) -> str:
    p = Path(cfg.SESSION_SAVE_PATH) / f"sess_{sid}"
    return p.read_text(encoding="utf-8") if p.is_file() else ""


def _drop_sess(sid: str) -> None:
    if sid:
        (Path(cfg.SESSION_SAVE_PATH) / f"sess_{sid}").unlink(missing_ok=True)


# ================= P1-9：CSRF 空 token 绕过 =================

def test_csrf_check_rejects_empty_token_when_session_has_none():
    """回归：session 无 csrf 键时，提交空 csrf 必须被拒（旧实现 compare_digest("","")→True）。"""
    assert W.csrf_check({"csrf": ""}, {}) is False
    assert W.csrf_check({"csrf": ""}, {cfg.CSRF_KEY: ""}) is False
    assert W.csrf_check({"csrf": None}, {}) is False
    # 缺字段（form 里根本没有 csrf）
    assert W.csrf_check({}, {cfg.CSRF_KEY: TOKEN}) is False


def test_csrf_check_rejects_when_session_token_missing():
    """session 有 auth 但无 csrf（登录后立即 POST 的窗口）→ 任何 token 都不通过。"""
    session = {cfg.SKEY: True}
    assert W.csrf_check({"csrf": ""}, session) is False
    assert W.csrf_check({"csrf": "guess"}, session) is False
    assert W.csrf_check({"csrf": TOKEN}, session) is False


def test_csrf_check_accepts_only_equal_nonempty():
    session = {cfg.CSRF_KEY: TOKEN}
    assert W.csrf_check({"csrf": TOKEN}, session) is True
    assert W.csrf_check({"csrf": TOKEN + "x"}, session) is False
    assert W.csrf_check({"csrf": ""}, session) is False


def test_csrf_empty_token_setup_pass_blocked_http(tmp_path, monkeypatch):
    """端到端回归：已登录 + session 无 csrf → `setup_pass&csrf=`（不带旧密码）被拒。

    审计实测链路：受害登录（h_login 只写 auth）→ 立刻 POST action=setup_pass&csrf=
    → 改密成功 → 面板被接管。这里用真实 HTTP 链路复现并断言被拦。
    """
    # 让「首次设密免 CSRF」前置分支不生效（模拟已有密码的面板）
    pass_file = tmp_path / ".panel_pass.php"
    pass_file.write_text("<?php return 'x';", encoding="utf-8")
    monkeypatch.setattr(cfg, "PASS_FILE", pass_file)

    called = []
    monkeypatch.setattr(W.passwd, "set_pass", lambda pw: called.append(pw) or True)

    ck = new_session(authed=True, csrf=False)      # 登录后、渲染前的窗口
    r = client.post(
        "/",
        data={"action": "setup_pass", "csrf": "", "pass1": "hacked6",
              "pass2": "hacked6"},
        cookies=ck, follow_redirects=False,
    )
    r = _hop(r, ck)
    assert "请求验证失败" in r.text
    assert called == [], "空 csrf 不得进入改密逻辑"
    assert pass_file.read_text(encoding="utf-8") == "<?php return 'x';"


def test_login_success_issues_csrf_token(monkeypatch):
    """P1-9 另一半：h_login 成功即生成并落盘 csrf token，不留空窗。"""
    monkeypatch.setattr(W.passwd, "check_pass", lambda pw: pw == "right")
    ck = new_session(authed=False, csrf=False)
    r = post("login", {"pass": "right"}, ck, follow=False)
    assert r.status_code == 302
    new = _resp_sid(r)
    assert new and new != ck["PHPSESSID"]
    text = _sess_text(new)
    assert "m7a_panel_auth" in text
    assert cfg.CSRF_KEY in text, f"登录后 session 缺 csrf 键：{text!r}"
    # 落盘的 token 是 32 位 hex（secrets.token_hex(16)）
    tok = str(phpsess.parse_session(text).get(cfg.CSRF_KEY) or "")
    assert len(tok) == 32 and all(c in "0123456789abcdef" for c in tok)
    _drop_sess(new)


def test_setup_pass_success_issues_csrf_token(monkeypatch):
    monkeypatch.setattr(W.passwd, "set_pass", lambda pw: True)
    ck = new_session(authed=False, csrf=False)
    r = post("setup_pass", {"pass1": "secret6", "pass2": "secret6"}, ck,
             follow=False)
    assert r.status_code == 302
    new = _resp_sid(r)
    assert new and cfg.CSRF_KEY in _sess_text(new)
    _drop_sess(new)


def test_pagectx_lazy_csrf_still_works():
    """不得破坏 pagectx.csrf_field 的惰性生成逻辑（源码级守护）。"""
    src = (ROOT / "services" / "pagectx.py").read_text("utf-8")
    assert "def csrf_field()" in src
    assert "session_set(sid, cfg.CSRF_KEY, tok)" in src


# ================= P1-10：全局登录锁自伤式 DoS =================

def test_global_fail_same_ip_still_locks():
    """保留 M7A-03 原语义：同一来源反复失败也计入全局（test_v1243 依赖此行为）。"""
    lg.reset()
    for _ in range(lg.GLOBAL_MAX):
        lg.global_fail("10.0.0.1")
    assert lg.global_locked() > 0


def test_success_login_clears_global_bucket():
    """任一来源成功登录 → 全局桶整体清零（P1-10 核心：可恢复，不再永久锁死）。"""
    lg.reset()
    for _ in range(lg.GLOBAL_MAX):
        lg.global_fail("10.0.0.9")
    assert lg.global_locked() > 0
    lg.ok("192.168.1.50")                 # 另一个来源成功登录
    assert lg.global_locked() == 0
    assert lg.global_sources() == 0


def test_global_sources_counts_distinct_ips():
    lg.reset()
    for ip in ("1.1.1.1", "1.1.1.1", "1.1.1.1"):
        lg.global_fail(ip)
    assert lg.global_sources() == 1
    for ip in ("2.2.2.2", "3.3.3.3"):
        lg.global_fail(ip)
    assert lg.global_sources() == 3


def test_global_bucket_slides_out_of_window(monkeypatch):
    lg.reset()
    base = time.time()
    monkeypatch.setattr(lg, "_now", lambda: base)
    for _ in range(lg.GLOBAL_MAX):
        lg.global_fail("10.0.0.1")
    assert lg.global_locked() > 0
    monkeypatch.setattr(lg, "_now", lambda: base + lg.GLOBAL_WINDOW + 1)
    assert lg.global_locked() == 0


def test_correct_password_not_locked_out_during_global_storm(monkeypatch):
    """全局风暴期间合法管理员仍能登录（旧实现在校验密码前就硬拒绝 → 永久锁死）。"""
    lg.reset()
    for i in range(lg.GLOBAL_MAX):
        lg.global_fail(f"203.0.113.{i}")
    assert lg.global_locked() > 0
    monkeypatch.setattr(W.passwd, "check_pass", lambda pw: pw == "right")
    ck = new_session(authed=False, csrf=False)
    r = post("login", {"pass": "right"}, ck, follow=False)
    assert r.status_code == 302, "全局锁定期内正确密码必须放行"
    new = _resp_sid(r)
    assert "m7a_panel_auth" in _sess_text(new)
    assert lg.global_locked() == 0, "成功登录应清零全局桶"
    _drop_sess(new)


def test_wrong_password_still_rate_limited_during_global_storm(monkeypatch):
    lg.reset()
    for i in range(lg.GLOBAL_MAX):
        lg.global_fail(f"203.0.113.{i}")
    monkeypatch.setattr(W.passwd, "check_pass", lambda pw: False)
    r = post("login", {"pass": "nope"}, new_session(authed=False, csrf=False))
    assert "登录尝试过多" in r.text or "密码错误" in r.text


def test_single_ip_lock_still_before_password_check(monkeypatch):
    """单 IP 5 次锁定仍在验密前生效（有意义的防爆破，不因 P1-10 被削弱）。"""
    lg.reset()
    monkeypatch.setattr(W.passwd, "check_pass", lambda pw: False)
    for _ in range(lg.MAX_FAIL):
        post("login", {"pass": "x"}, new_session(authed=False, csrf=False))
    assert not lg.check("testclient")
    monkeypatch.setattr(W.passwd, "check_pass", lambda pw: True)
    r = post("login", {"pass": "right"}, new_session(authed=False, csrf=False))
    assert "尝试过于频繁" in r.text


# ================= P1-11：session 读-改-写加锁 =================

@pytest.fixture()
def sess_tmp(tmp_path, monkeypatch):
    d = tmp_path / "sessions"
    d.mkdir()
    monkeypatch.setattr(phpsess, "SESSION_SAVE_PATH", str(d))
    return d


def test_session_set_serialized_no_lost_update(sess_tmp):
    """并发写不同键：所有键都必须留在文件里（旧实现后写者覆盖先写者）。"""
    sid = phpsess.new_sid()
    assert phpsess.session_set(sid, "base", True)
    keys = [f"k{i}" for i in range(8)]
    barrier = threading.Barrier(len(keys))
    errors = []

    def worker(k):
        try:
            barrier.wait(timeout=10)
            for _ in range(5):
                if not phpsess.session_set(sid, k, True):
                    errors.append(f"set {k} failed")
        except Exception as exc:                     # noqa: BLE001
            errors.append(repr(exc))

    threads = [threading.Thread(target=worker, args=(k,)) for k in keys]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not errors, errors
    data = phpsess.parse_session((sess_tmp / f"sess_{sid}").read_text("utf-8"))
    assert data.get("base") is True
    for k in keys:
        assert data.get(k) is True, f"并发写丢失键 {k}：{data!r}"


def test_logout_not_resurrected_by_concurrent_writes(sess_tmp):
    """登出（删 auth）后，并发的其它键写入不得把 auth 用旧快照写回来。"""
    sid = phpsess.new_sid()
    assert phpsess.session_set(sid, cfg.SKEY, True)
    assert phpsess.session_set(sid, "keep", True)
    barrier = threading.Barrier(5)
    errors = []

    def remover():
        try:
            barrier.wait(timeout=10)
            assert phpsess.session_remove(sid, cfg.SKEY)
        except Exception as exc:                     # noqa: BLE001
            errors.append(repr(exc))

    def writer(i):
        try:
            barrier.wait(timeout=10)
            for _ in range(20):
                phpsess.session_set(sid, f"w{i}", True)
        except Exception as exc:                     # noqa: BLE001
            errors.append(repr(exc))

    threads = [threading.Thread(target=remover)]
    threads += [threading.Thread(target=writer, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not errors, errors
    data = phpsess.parse_session((sess_tmp / f"sess_{sid}").read_text("utf-8"))
    assert cfg.SKEY not in data, f"登出后 auth 键复活：{data!r}"
    assert data.get("keep") is True


def test_session_write_keeps_inode_and_mode(sess_tmp):
    """M2-R 约束：已存在文件必须原地改写（保留 inode/属主），权限 0660。"""
    sid = phpsess.new_sid()
    assert phpsess.session_set(sid, "a", 1)
    path = sess_tmp / f"sess_{sid}"
    ino = os.lstat(path).st_ino
    assert phpsess.session_set(sid, "b", 2)
    st = os.lstat(path)
    assert st.st_ino == ino, "不得用 os.replace 换 inode（会丢属主/权限 → M2-R 回归）"
    assert stat.S_IMODE(st.st_mode) == 0o660


def test_session_set_has_module_lock():
    """源码级守护：读-改-写必须整体处在锁内。"""
    src = (ROOT / "phpsess.py").read_text("utf-8")
    assert "threading.RLock()" in src
    assert "with _LOCK:" in src
    assert "os.O_NOFOLLOW" in src
    assert "fcntl.lockf(fd, fcntl.LOCK_EX, 0)" in src


# ================= P1-12：O_NOFOLLOW 防 symlink 写穿 =================

def test_session_set_refuses_symlink(sess_tmp, tmp_path):
    target = tmp_path / "victim.txt"
    target.write_text("ORIGINAL", encoding="utf-8")
    target.chmod(0o644)
    sid = phpsess.new_sid()
    link = sess_tmp / f"sess_{sid}"
    link.symlink_to(target)

    assert phpsess.session_set(sid, cfg.SKEY, True) is False
    assert target.read_text(encoding="utf-8") == "ORIGINAL"
    # chmod 也不能穿过链接（旧实现会在写完 chmod 目标文件）
    assert stat.S_IMODE(os.lstat(target).st_mode) == 0o644
    assert link.is_symlink(), "链接本身不应被替换/删除"


def test_session_remove_refuses_symlink(sess_tmp, tmp_path):
    target = tmp_path / "victim2.txt"
    target.write_text("m7a_panel_auth|b:1;", encoding="utf-8")
    sid = phpsess.new_sid()
    (sess_tmp / f"sess_{sid}").symlink_to(target)
    assert phpsess.session_remove(sid, cfg.SKEY) is False
    assert target.read_text(encoding="utf-8") == "m7a_panel_auth|b:1;"


def test_session_set_no_follow_flag_in_open():
    """源码级守护：所有 session 文件 open 都必须带 O_NOFOLLOW。"""
    src = (ROOT / "phpsess.py").read_text("utf-8")
    seg = src.split("def _update_session_file")[1]
    assert "flags = os.O_RDWR | os.O_NOFOLLOW" in seg
    assert seg.count("os.O_NOFOLLOW") >= 1
    assert "os.lstat(path)" in seg, "chmod 前必须 lstat 校验不是链接"


# ================= P1-13：文档默认目录 =================

def test_readme_session_dir_default_is_not_tmp():
    readme = (REPO / "README.md").read_text("utf-8")
    rows = [l for l in readme.splitlines()
            if l.startswith("|") and "`M7A_SESSION_DIR`" in l]
    assert rows, "README 缺少 M7A_SESSION_DIR 说明行"
    for row in rows:
        assert "`/tmp`" not in row, f"README 仍把 /tmp 写成默认值：{row}"
    assert any("sessions" in row for row in rows)
    assert "M7A_SESSION_MODE" in readme
    assert "全局可写" in readme, "README 缺少 /tmp 等全局可写目录的安全提示"


def test_migration_notes_session_dir_default_is_not_tmp():
    notes = (REPO / "docs" / "MIGRATION_NOTES.md").read_text("utf-8")
    rows = [l for l in notes.splitlines()
            if l.startswith("|") and "`M7A_SESSION_DIR`" in l]
    assert rows, "MIGRATION_NOTES 缺少 M7A_SESSION_DIR 说明行"
    for row in rows:
        assert "`/tmp`" not in row, f"MIGRATION_NOTES 仍写 /tmp 默认值：{row}"
    assert any("sessions" in row for row in rows)


def test_config_session_default_is_base_dir_sessions():
    """代码事实与文档一致：默认 SESSION_SAVE_PATH = BASE_DIR/sessions。"""
    assert str(cfg.SESSION_SAVE_PATH) == os.environ.get(
        "M7A_SESSION_DIR", str(cfg.BASE_DIR / "sessions"))


# ================= P1-23：restore_config YAML 校验 =================

AUDIT_BAD_YAML = "server:\n  port: [8080\n  bad: {{{\n"
GOOD_YAML = (
    "locales:\n  zh_CN: 简体中文\n"
    "power_enable: true\n"
    "log_retention_days: 30\n"
    "notify_webhook_headers: \"Content-Type: application/json\"\n"
    "notify_webhook_body: '{\"text\": \"hi\"}'\n"
    "notify_custom_image: \"ghcr.io/x/y:latest\"\n"
)


def test_validate_yaml_rejects_audit_payload():
    """审计实测可写入的损坏 YAML 必须被拒。"""
    assert W.validate_yaml_text(AUDIT_BAD_YAML) != ""
    assert W.validate_yaml_text("server:\n  port: [8080\n") != ""       # 截断
    assert W.validate_yaml_text("a:\n\tb: 1\n") != ""                   # Tab 缩进
    assert W.validate_yaml_text("key: \"unclosed\n") != ""              # 引号未闭合
    assert W.validate_yaml_text("just some text\nno keys\n") != ""      # 无映射键
    assert W.validate_yaml_text("   \n\n") != ""                        # 空


def test_validate_yaml_accepts_panel_own_config():
    """面板自己写出的 config.yaml 必须通过（防止校验过严误杀合法配置）。"""
    assert W.validate_yaml_text(GOOD_YAML) == ""
    assert W.validate_yaml_text("a: 1\nb: |\n  line1\n  line2\nc: 2\n") == ""
    assert W.validate_yaml_text("list:\n  - x\n  - y\n") == ""


class _Upload:
    """模拟 FastAPI UploadFile 的最小接口（.filename / .file.read()）。"""

    def __init__(self, name: str, data: bytes):
        self.filename = name
        self.file = io.BytesIO(data)


def test_restore_config_rejects_bad_yaml_http(tmp_path, monkeypatch):
    """端到端：损坏 YAML 不写盘（config.yaml 内容不变）。"""
    inst_dir = tmp_path / "inst"
    inst_dir.mkdir()
    cfg_path = inst_dir / "config.yaml"
    cfg_path.write_text(GOOD_YAML, encoding="utf-8")
    inst = {"id": "i1", "name": "i1", "container": "c1",
            "dir": str(inst_dir), "default": True}
    monkeypatch.setattr(W, "instance_current", lambda session: inst)
    monkeypatch.setattr(W, "instance_config", lambda i: str(cfg_path))
    monkeypatch.setattr(W, "config_backup",
                        lambda i: str(tmp_path / "config.yaml.bak"))

    ck = new_session(authed=True)
    r = client.post(
        "/",
        data={"action": "restore_config", "csrf": TOKEN},
        files={"cfg_file": ("bad.yaml", AUDIT_BAD_YAML.encode(), "text/plain")},
        cookies=ck, follow_redirects=False,
    )
    r = _hop(r, ck)
    assert "请求验证失败" not in r.text
    assert ("括号" in r.text or "引号" in r.text or "异常" in r.text), r.text[:400]
    assert cfg_path.read_text(encoding="utf-8") == GOOD_YAML, "校验失败不得写盘"


def test_restore_config_blocks_when_backup_fails(tmp_path, monkeypatch):
    """config_backup 返回 None → 阻断写入（旧实现照样覆盖，写坏无法回滚）。"""
    inst_dir = tmp_path / "inst2"
    inst_dir.mkdir()
    cfg_path = inst_dir / "config.yaml"
    cfg_path.write_text(GOOD_YAML, encoding="utf-8")
    inst = {"id": "i2", "name": "i2", "container": "c2",
            "dir": str(inst_dir), "default": True}
    monkeypatch.setattr(W, "instance_current", lambda session: inst)
    monkeypatch.setattr(W, "instance_config", lambda i: str(cfg_path))
    monkeypatch.setattr(W, "config_backup", lambda i: None)

    form = {"cfg_file": _Upload("good.yaml", GOOD_YAML.encode("utf-8"))}
    kind, data = W.h_restore_config(None, form, {}, "sid")
    assert kind == "flash"
    assert "备份失败" in (data.get("err") or "")
    assert cfg_path.read_text(encoding="utf-8") == GOOD_YAML


def test_restore_config_accepts_good_yaml(tmp_path, monkeypatch):
    """合法 YAML 仍能恢复（防止修 P1-23 把功能改死）。"""
    inst_dir = tmp_path / "inst3"
    inst_dir.mkdir()
    cfg_path = inst_dir / "config.yaml"
    cfg_path.write_text("old: 1\n", encoding="utf-8")
    inst = {"id": "i3", "name": "i3", "container": "c3",
            "dir": str(inst_dir), "default": True}
    monkeypatch.setattr(W, "instance_current", lambda session: inst)
    monkeypatch.setattr(W, "instance_config", lambda i: str(cfg_path))
    monkeypatch.setattr(W, "config_backup",
                        lambda i: str(tmp_path / "config.yaml.bak"))
    form = {"cfg_file": _Upload("good.yaml", GOOD_YAML.encode("utf-8"))}
    kind, data = W.h_restore_config(None, form, {}, "sid")
    assert kind == "flash" and not data.get("err")
    assert "配置已恢复" in data["msg"]
    assert cfg_path.read_text(encoding="utf-8") == GOOD_YAML


# ================= P1-24：inst_dir 校验 =================

@pytest.mark.parametrize("bad", [
    "", "  ", "relative/dir", "../..", "..", "/etc", "/usr", "/usr/bin",
    "/bin", "/boot", "/dev", "/proc", "/sys", "/root", "/", "/tmp",
    "/home/user/../etc", "/var/../etc", "/etc/",
])
def test_validate_inst_dir_rejects(bad):
    assert W.validate_inst_dir(bad) != "", f"应当拒绝 inst_dir={bad!r}"


@pytest.mark.parametrize("good", [
    "/home/march7thassistant",
    "/home/demo/m7a",
    "/opt/instances/m7a",
    "/srv/m7a-inst",
    "/home/demo/m7a/",          # 尾部斜杠
])
def test_validate_inst_dir_accepts(good):
    assert W.validate_inst_dir(good) == "", f"不应拒绝 inst_dir={good!r}"


def test_instance_save_rejects_system_dir_http(monkeypatch):
    """端到端：inst_dir=/etc 被拒，且不写 .instances.php。"""
    writes = []
    monkeypatch.setattr(W, "instances_load", lambda: [])
    monkeypatch.setattr(W, "instances_write", lambda items: writes.append(items) or True)
    monkeypatch.setattr(W, "instance_switch_to", lambda s, sid, t: True)
    r = post("instance_save", {"inst_id": "", "inst_name": "evil",
                               "inst_container": "evil", "inst_dir": "/etc"})
    assert "系统目录" in r.text
    assert writes == [], "校验失败不得写实例表"


def test_instance_save_rejects_dotdot_http(monkeypatch):
    writes = []
    monkeypatch.setattr(W, "instances_load", lambda: [])
    monkeypatch.setattr(W, "instances_write", lambda items: writes.append(items) or True)
    monkeypatch.setattr(W, "instance_switch_to", lambda s, sid, t: True)
    r = post("instance_save", {"inst_id": "", "inst_name": "evil",
                               "inst_container": "evil", "inst_dir": "../../etc"})
    assert "绝对路径" in r.text or ".." in r.text
    assert writes == []


def test_instance_save_accepts_valid_dir(monkeypatch):
    writes = []
    monkeypatch.setattr(W, "instances_load", lambda: [])
    monkeypatch.setattr(W, "instances_write", lambda items: writes.append(items) or True)
    monkeypatch.setattr(W, "instance_switch_to", lambda s, sid, t: True)
    r = post("instance_save", {"inst_id": "", "inst_name": "ok",
                               "inst_container": "ok1",
                               "inst_dir": "/home/demo/m7a"})
    assert "已添加并切换" in r.text
    assert writes and writes[0][0]["dir"] == "/home/demo/m7a"


# ================= 顺带：flash 双重转义 =================

def test_banner_escapes_exactly_once():
    """_banner 统一负责转义：传原始值 → 只转一次。"""
    banner = _banner(msg="实例「Test & Demo」已添加")
    assert "Test &amp; Demo" in banner
    assert "&amp;amp;" not in banner, f"双重转义：{banner}"


def test_instance_save_flash_single_escape_http(monkeypatch):
    """实测缺陷：实例名 Test & Demo 曾显示成 Test &amp; Demo。"""
    monkeypatch.setattr(W, "instances_load", lambda: [])
    monkeypatch.setattr(W, "instances_write", lambda items: True)
    monkeypatch.setattr(W, "instance_switch_to", lambda s, sid, t: True)
    r = post("instance_save", {"inst_id": "", "inst_name": "Test & Demo",
                               "inst_container": "demo1",
                               "inst_dir": "/home/demo/m7a"})
    assert "Test &amp; Demo" in r.text
    assert "Test &amp;amp; Demo" not in r.text, "实例名被双重转义"


def test_write_handlers_do_not_pre_escape_flash():
    """源码级守护：F(msg=/err=) 里不得再包 h()（flash._banner 统一转义）。"""
    import re

    src = (ROOT / "routers" / "write.py").read_text("utf-8")
    # 只匹配独立的 h(...) 调用（排除 Path( / .h( / 标识符后缀）
    call = re.compile(r"(?<![\w.])h\s*\(")
    bad = [ln.strip() for ln in src.splitlines()
           if ("F(msg=" in ln or "F(err=" in ln) and call.search(ln)]
    assert not bad, f"仍有预转义：{bad}"


# ================= P1-25：websockets 依赖声明 =================

def test_requirements_txt_declares_websockets():
    lines = [l.strip() for l in
             (ROOT / "requirements.txt").read_text("utf-8").splitlines()]
    deps = [l for l in lines if l and not l.startswith("#")]
    assert any(l.split(">=")[0].split("==")[0].strip() == "websockets" for l in deps), \
        f"requirements.txt 未声明 websockets：{deps}"


def test_requirements_lock_pins_websockets():
    text = (ROOT / "requirements.lock").read_text("utf-8")
    pins = [l.strip() for l in text.splitlines()
            if l.strip().startswith("websockets==")]
    assert pins, "requirements.lock 未锁定 websockets"
    ver = pins[0].split("==", 1)[1]
    assert ver and ver[0].isdigit(), f"版本号异常：{pins[0]}"


def test_websockets_import_sites_are_declared():
    """用到 websockets 的两个模块必须存在，依赖声明才有意义。"""
    main_src = (ROOT / "main.py").read_text("utf-8")
    prev_src = (ROOT / "routers" / "preview.py").read_text("utf-8")
    assert "import websockets" in main_src
    assert "websockets.connect" in prev_src


# ================= 顺带（交接自 fix-handlers）：textarea 值不得被 strip =================

def test_form_updates_keeps_textarea_whitespace():
    """webhook headers/body、自定义 JSON 的首尾空白与末尾换行有语义，不能 strip。"""
    from routers.write import _form_updates

    body = '  {\n    "a": 1\n  }\n\n'
    u = _form_updates({
        "cfg_notify_webhook_body": body,
        "cfg_notify_lark_keyword": "  hi  ",      # 普通 str 仍 strip
    })
    # yaml_format_val 用双引号包裹并转义换行 → 去掉外层引号后应还原原文
    got = u["notify_webhook_body"]
    assert got.startswith('"') and got.endswith('"')
    assert got[1:-1].replace('\\n', '\n').replace('\\"', '"') == body
    assert u["notify_lark_keyword"] == '"hi"'


def test_form_updates_normalizes_textarea_crlf():
    from routers.write import _form_updates

    u = _form_updates({"cfg_notify_custom_data": "a\r\nb\rc"})
    got = u["notify_custom_data"]
    assert "\r" not in got
    assert got[1:-1] == "a\\nb\\nc"


def test_form_updates_password_still_stripped():
    from routers.write import _form_updates

    u = _form_updates({"cfg_notify_smtp_password": "  pw  "})
    assert u["notify_smtp_password"] == '"pw"'


def test_distributed_storm_message_mentions_distributed(monkeypatch):
    """不同来源数达阈值才升级为「疑似分布式爆破」文案（单人打满不升级）。"""
    lg.reset()
    monkeypatch.setattr(W.passwd, "check_pass", lambda pw: False)
    for i in range(lg.GLOBAL_MAX):
        lg.global_fail(f"198.51.100.{i % 12}")
    assert lg.global_sources() >= lg.GLOBAL_SOURCES_MIN
    r = post("login", {"pass": "nope"}, new_session(authed=False, csrf=False))
    assert "分布式爆破" in r.text


def test_single_ip_storm_message_not_distributed(monkeypatch):
    lg.reset()
    monkeypatch.setattr(W.passwd, "check_pass", lambda pw: False)
    for _ in range(lg.GLOBAL_MAX):
        lg.global_fail("198.51.100.7")
    assert lg.global_sources() < lg.GLOBAL_SOURCES_MIN
    r = post("login", {"pass": "nope"}, new_session(authed=False, csrf=False))
    assert "分布式爆破" not in r.text
    assert "登录尝试过多" in r.text
