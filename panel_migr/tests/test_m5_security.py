"""M5 安全专项：会话轮换、登录限速、key 迁 X-M7A-Key 头、preview_token 端点。"""
import hashlib
import hmac as hmac_mod
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import config as cfg
from fastapi.testclient import TestClient

from main import app

client = TestClient(app)

TOKEN = "m5token"


@pytest.fixture(autouse=True)
def _clean():
    """每测试前后清空登录限速状态（TestClient 同源 IP，跨用例会互相影响）。"""
    from services import loginguard
    loginguard.reset()
    yield
    loginguard.reset()


def new_session(authed=True):
    sid = "m5" + uuid.uuid4().hex[:10]
    parts = []
    if authed:
        parts.append("m7a_panel_auth|b:1;")
    parts.append(f'm7a_panel_csrf|s:{len(TOKEN)}:"{TOKEN}";')
    p = Path(cfg.SESSION_SAVE_PATH)
    p.mkdir(parents=True, exist_ok=True)
    (p / f"sess_{sid}").write_text("".join(parts), encoding="utf-8")
    return {"PHPSESSID": sid}


def post(action, data=None, cookies=None, follow=False):
    payload = {"action": action, "csrf": TOKEN}
    payload.update(data or {})
    ck = cookies or new_session()
    r = client.post("/", data=payload, cookies=ck, follow_redirects=False)
    if follow and r.status_code in (301, 302, 303, 307, 308):
        r = client.get(r.headers["location"], cookies=ck)
    return r


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


# ---------- 会话轮换（防会话固定） ----------

def test_login_rotates_sid_and_kills_old(monkeypatch):
    """登录成功总是换新 sid：新会话拿到登录态，旧会话被注销。"""
    from services import passwd
    monkeypatch.setattr(passwd, "check_pass", lambda pw: pw == "right")
    cookies = new_session(authed=True)
    old = cookies["PHPSESSID"]
    r = post("login", {"pass": "right"}, cookies)
    assert r.status_code == 302
    new = _resp_sid(r)
    assert new and new != old
    assert "m7a_panel_auth" in _sess_text(new)
    assert "m7a_panel_auth" not in _sess_text(old)
    _drop_sess(new)


def test_login_failure_does_not_rotate(monkeypatch):
    """密码错误：不下发新 cookie，旧会话登录态不动。"""
    from services import passwd
    monkeypatch.setattr(passwd, "check_pass", lambda pw: False)
    cookies = new_session(authed=True)
    old = cookies["PHPSESSID"]
    r = post("login", {"pass": "x"}, cookies, follow=True)
    assert r.status_code == 200
    assert _resp_sid(r) == ""
    assert "m7a_panel_auth" in _sess_text(old)


def test_setup_pass_rotates_sid(monkeypatch):
    from services import passwd
    monkeypatch.setattr(passwd, "set_pass", lambda pw: True)
    cookies = new_session(authed=False)
    old = cookies["PHPSESSID"]
    r = post("setup_pass", {"pass1": "secret6", "pass2": "secret6"}, cookies)
    assert r.status_code == 302
    new = _resp_sid(r)
    assert new and new != old
    assert "m7a_panel_auth" in _sess_text(new)
    _drop_sess(new)


# ---------- 登录限速 ----------

def _fail_login(n: int) -> None:
    for _ in range(n):
        post("login", {"pass": "x"}, new_session(authed=False))


def test_login_rate_limited_after_5_fails(monkeypatch):
    from services import passwd
    monkeypatch.setattr(passwd, "check_pass", lambda pw: False)
    _fail_login(5)
    # 锁定后即使密码正确也拒绝
    monkeypatch.setattr(passwd, "check_pass", lambda pw: True)
    r = post("login", {"pass": "right"}, new_session(authed=False),
             follow=True)
    assert r.status_code == 200
    assert "尝试过于频繁" in r.text


def test_login_rate_limit_window_expires(monkeypatch):
    from services import loginguard as lg
    from services import passwd
    monkeypatch.setattr(passwd, "check_pass", lambda pw: False)
    _fail_login(5)
    assert not lg.check("testclient")
    base = time.time()
    monkeypatch.setattr(lg, "_now", lambda: base + 601)   # 锁与统计窗口均已过期
    assert lg.check("testclient")


def test_login_success_clears_fail_count(monkeypatch):
    from services import passwd
    monkeypatch.setattr(passwd, "check_pass", lambda pw: False)
    _fail_login(4)
    monkeypatch.setattr(passwd, "check_pass", lambda pw: True)
    r = post("login", {"pass": "right"}, new_session(authed=False))
    assert r.status_code == 302
    _drop_sess(_resp_sid(r))
    # 成功清零后：再来 4 次失败仍未到锁定阈值，正确密码照常通过
    monkeypatch.setattr(passwd, "check_pass", lambda pw: False)
    _fail_login(4)
    monkeypatch.setattr(passwd, "check_pass", lambda pw: True)
    r2 = post("login", {"pass": "right"}, new_session(authed=False))
    assert r2.status_code == 302
    _drop_sess(_resp_sid(r2))


# ---------- key 迁 X-M7A-Key 头（query 兼容由 test_m2_get 既有用例守住） ----------

def test_monitor_sampler_key_via_header(monkeypatch):
    import routers.get_write as G
    from services.instances import panel_config_save

    panel_config_save({"monitor_key": "mk-123"})
    monkeypatch.setattr(G, "services_monitor_sample",
                        lambda inst, iv: {"sampled": True, "running": False,
                                          "data": {"points": [1]}})
    monkeypatch.setattr(G, "alert_tick", lambda inst, force=False: {"pushed": False})
    r = client.get("/?monitor_sampler=1", headers={"X-M7A-Key": "mk-123"},
                   cookies={}, follow_redirects=False)
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_key_header_beats_wrong_query(monkeypatch):
    """header 正确、query 错误 → header 优先，照常通过（防日志里的旧值干扰）。"""
    import routers.get_write as G
    from services.instances import panel_config_save

    panel_config_save({"monitor_key": "mk-123"})
    monkeypatch.setattr(G, "services_monitor_sample",
                        lambda inst, iv: {"sampled": True, "running": False,
                                          "data": {"points": []}})
    monkeypatch.setattr(G, "alert_tick", lambda inst, force=False: {"pushed": False})
    r = client.get("/?monitor_sampler=1&key=wrong",
                   headers={"X-M7A-Key": "mk-123"},
                   cookies={}, follow_redirects=False)
    assert r.json()["ok"] is True


def test_scheduler_key_via_header(monkeypatch):
    """_keyed 共用路径（scheduler/alerter）也认 header。"""
    import routers.get_write as G
    from services.instances import panel_config_save

    panel_config_save({"scheduler_key": "sk-1"})
    monkeypatch.setattr(G, "schedule_run_due",
                        lambda inst, now=None: {"ok": True, "ran": []})
    monkeypatch.setattr(G, "alert_tick", lambda inst, force=False: {"pushed": False})
    r = client.get("/?scheduler=1", headers={"X-M7A-Key": "sk-1"},
                   cookies={}, follow_redirects=False)
    assert r.status_code == 200
    assert r.json()["ok"] is True


# ---------- preview_token（M3 已迁后端，M5 验收登记） ----------

def test_preview_token_registered():
    from routers import GET_HANDLERS
    assert "preview_token" in GET_HANDLERS


def test_preview_token_ok(monkeypatch):
    import services.preview as pv
    monkeypatch.setattr(pv, "preview_secret", lambda: "s3cret")
    r = client.get("/?ajax=preview_token", cookies=new_session(authed=True),
                   follow_redirects=False)
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True
    day = time.strftime("%Y%m%d")
    want = hmac_mod.new(b"s3cret", day.encode(), hashlib.sha256).hexdigest()[:32]
    assert d["token"] == want and len(d["token"]) == 32


def test_preview_token_missing_secret(monkeypatch):
    import services.preview as pv
    monkeypatch.setattr(pv, "preview_secret", lambda: "")
    r = client.get("/?ajax=preview_token", cookies=new_session(authed=True),
                   follow_redirects=False)
    assert r.json() == {"ok": False, "msg": "预览服务未部署（服务器缺少 preview_secret）"}


def test_preview_token_requires_auth():
    r = client.get("/?ajax=preview_token", cookies={}, follow_redirects=False)
    assert b"auth-card" in r.content   # M5-B：未登录 → Python 渲染登录页
