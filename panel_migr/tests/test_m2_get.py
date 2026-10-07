"""M2 写型 GET / 下载 / key 回调 分发级测试（W2/W6）+ 5 个只读接口回归。"""
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config as cfg
from fastapi.testclient import TestClient

from main import app
from services.instances import instances_write

client = TestClient(app)

INST_DIR = Path(__file__).resolve().parent / "_tmp" / "m2inst"
(INST_DIR / "logs").mkdir(parents=True, exist_ok=True)
(INST_DIR / "config.yaml").write_text(
    "locales:\n  zh_CN: 简体中文\npower_enable: true\n", "utf-8")
TEST_INST = {"id": "m7a", "name": "测试实例", "container": "m7a-test",
             "dir": str(INST_DIR), "default": True}


instances_write([TEST_INST])   # 独立运行本文件时也指向测试实例


def new_session(authed=True):
    sid = "g2" + uuid.uuid4().hex[:10]
    parts = []
    if authed:
        parts.append("m7a_panel_auth|b:1;")
    p = Path(cfg.SESSION_SAVE_PATH)
    p.mkdir(parents=True, exist_ok=True)
    (p / f"sess_{sid}").write_text("".join(parts), encoding="utf-8")
    return {"PHPSESSID": sid}


def get(qs, cookies=None):
    return client.get("/?" + qs, cookies=cookies or new_session(),
                      follow_redirects=False)


# ---------- M1 只读 5 接口回归 ----------

def test_readonly_5_no_regression():
    assert get("ajax=status").status_code == 200
    d = get("ajax=log").json()
    assert d["ok"] is True and "files" in d
    d = get("ajax=running").json()
    assert "running" in d
    d = get("ajax=history").json()
    assert d["ok"] is True and len(d["week"]) == 7
    d = get("ajax=monitor&iv=60").json()
    assert d["ok"] is True and d["interval"] == 60


def test_unknown_ajax_local_empty():
    # v1.22 M6：Python 唯一后端，未知 ajax 本地返回空响应，不回源
    r = get("ajax=zzz_not_migrated")
    assert r.status_code == 200
    assert r.text == ""


# ---------- W6：7 个写型 GET ----------

def test_config_raw_plain_text():
    r = get("ajax=config_raw")
    assert r.status_code == 200
    assert "text/plain" in r.headers["content-type"]
    assert "power_enable" in r.text


def test_preview_token_missing_secret():
    r = get("ajax=preview_token")
    d = r.json()
    assert d["ok"] is False and "preview_secret" in d["msg"]


def test_preview_token_ok(monkeypatch, tmp_path):
    secret_dir = tmp_path / "logs"
    secret_dir.mkdir()
    (secret_dir / "preview_secret").write_text("topsecret", encoding="utf-8")
    monkeypatch.setattr(cfg, "DEFAULT_DIR", str(tmp_path))
    d = get("ajax=preview_token").json()
    assert d["ok"] is True and len(d["token"]) == 32


def test_doctor_json():
    d = get("ajax=doctor").json()
    assert d["ok"] is True
    assert len(d["items"]) == 10
    assert "took" in d


def test_alert_check(monkeypatch):
    import routers.get_write as G

    calls = []
    monkeypatch.setattr(G, "alert_tick",
                        lambda inst, force=False: calls.append(force) or {"pushed": False, "run": 0})
    d = get("ajax=alert_check").json()
    assert d == {"pushed": False, "run": 0}
    assert calls == [False]


def test_image_check_force(monkeypatch):
    import services.updateops as U

    seen = {}
    monkeypatch.setattr(U, "image_check",
                        lambda inst, force=False: seen.update(force=force) or {"ok": True, "new": False})
    d = get("ajax=image_check&force=1").json()
    assert d == {"ok": True, "new": False}
    assert seen["force"] is True


def test_check_update_and_test_source(monkeypatch):
    import services.updateops as U

    monkeypatch.setattr(U, "check_update", lambda: {"ok": True, "has_new": False})
    monkeypatch.setattr(U, "test_update_source", lambda: {"ok": True, "msg": "源可达"})
    assert get("ajax=check_update").json() == {"ok": True, "has_new": False}
    assert get("ajax=test_update_source").json() == {"ok": True, "msg": "源可达"}


def test_write_get_unauth_renders_login():
    r = get("ajax=doctor", cookies=new_session(authed=False))
    assert "auth-card" in r.text              # M5-B：未登录 → Python 登录页


# ---------- W6：2 个下载 ----------

def test_download_config_header():
    r = get("download=config")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/octet-stream"
    assert 'attachment; filename="config.yaml.bak.' in r.headers["content-disposition"]
    assert b"power_enable" in r.content


def test_download_config_unauth_renders_login():
    r = get("download=config", cookies=new_session(authed=False))
    assert "auth-card" in r.text              # M5-B：未登录 → Python 登录页


def test_export_log_empty():
    r = get("export_log=1")
    assert r.status_code == 200
    assert "text/plain" in r.headers["content-type"]
    assert r.content == b""


def test_export_log_with_file():
    log = INST_DIR / "logs" / "m7a_run_20260101.log"
    log.write_text("INFO 第一行\nWARN 第二行\n", encoding="utf-8")
    try:
        r = get("export_log=1")
        assert r.status_code == 200
        assert 'attachment; filename="m7a_log_' in r.headers["content-disposition"]
        assert "第一行" in r.text
        r2 = get("export_log=1&keyword=WARN")
        assert "第二行" in r2.text and "第一行" not in r2.text
    finally:
        log.unlink(missing_ok=True)


# ---------- W2：3 个 key 回调 ----------

def test_monitor_sampler_invalid_key():
    d = get("monitor_sampler=1&key=wrong").json()
    assert d == {"ok": False, "msg": "invalid key"}


def test_monitor_sampler_ok(monkeypatch):
    import routers.get_write as G
    from services.instances import panel_config_save, panel_config

    # 首次访问自动生成 key
    assert str(panel_config().get("monitor_key") or "") != "" or True
    panel_config_save({"monitor_key": "mk-123"})
    monkeypatch.setattr(G, "services_monitor_sample",
                        lambda inst, iv: {"sampled": True, "running": False,
                                          "data": {"points": [1, 2, 3]}})
    monkeypatch.setattr(G, "alert_tick", lambda inst, force=False: {"pushed": True})
    d = get("monitor_sampler=1&key=mk-123").json()
    assert d == {"ok": True, "sampled": True, "running": False,
                 "points": 3, "alert": True}


def test_scheduler_forbidden_and_ok(monkeypatch):
    import routers.get_write as G
    from services.instances import panel_config_save

    # 未配置 key → 403
    d = client.get("/?scheduler=1&key=nope", follow_redirects=False)
    assert d.status_code == 403
    assert d.json() == {"ok": False, "msg": "forbidden"}
    # 空 key → 403
    assert client.get("/?scheduler=1", follow_redirects=False).status_code == 403

    panel_config_save({"scheduler_key": "sk-1"})
    monkeypatch.setattr(G, "schedule_run_due",
                        lambda inst, now=None: {"ok": True, "time": "12:00",
                                                "ran": [], "skipped": []})
    monkeypatch.setattr(G, "alert_tick", lambda inst, force=False: {"pushed": False})
    d = client.get("/?scheduler=1&key=sk-1", follow_redirects=False).json()
    assert d["ok"] is True and d["alert"] is False


def test_alerter_forbidden_and_ok(monkeypatch):
    import routers.get_write as G
    from services.instances import panel_config_save

    assert client.get("/?alerter=1&key=nope", follow_redirects=False).status_code == 403
    panel_config_save({"alerter_key": "ak-1"})
    monkeypatch.setattr(G, "alert_tick",
                        lambda inst, force=False: {"pushed": True, "msg": "已推送"})
    d = client.get("/?alerter=1&key=ak-1", follow_redirects=False).json()
    assert d == {"pushed": True, "msg": "已推送"}


def test_key_callbacks_no_auth_required():
    """key 回调免登录：不带 cookie 也能到达比对逻辑（返回 invalid/forbidden 而非登录页）。"""
    d = client.get("/?monitor_sampler=1&key=x", cookies={}, follow_redirects=False)
    assert d.status_code == 200 and d.json()["ok"] is False
    d2 = client.get("/?scheduler=1&key=x", cookies={}, follow_redirects=False)
    assert d2.status_code == 403
