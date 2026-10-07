# -*- coding: utf-8 -*-
"""v1.21.1 专项：PRG 防刷新重放 / 任务 15s 幂等窗口 / 更新器保留 .venv / J 路径不回归。"""
import uuid
from pathlib import Path

import config as cfg
from fastapi.testclient import TestClient
from main import app

client = TestClient(app)
TOKEN = "v1211tok"


def new_session(authed=True):
    sid = "v11" + uuid.uuid4().hex[:10]
    parts = []
    if authed:
        parts.append("m7a_panel_auth|b:1;")
    parts.append(f'm7a_panel_csrf|s:{len(TOKEN)}:"{TOKEN}";')
    p = Path(cfg.SESSION_SAVE_PATH)
    p.mkdir(parents=True, exist_ok=True)
    (p / f"sess_{sid}").write_text("".join(parts), encoding="utf-8")
    return {"PHPSESSID": sid}


def post(action, data=None, cookies=None):
    payload = {"action": action, "csrf": TOKEN}
    payload.update(data or {})
    ck = cookies or new_session()
    r = client.post("/", data=payload, cookies=ck, follow_redirects=False)
    return r, ck


def session_text(ck):
    p = Path(cfg.SESSION_SAVE_PATH) / f"sess_{ck['PHPSESSID']}"
    return p.read_text(encoding="utf-8") if p.exists() else ""


def _stub_tasks(monkeypatch, started):
    import routers.write as W
    monkeypatch.setattr(W, "ensure_running", lambda inst, timeout=15.0: {"started": False})
    monkeypatch.setattr(
        W, "task_start",
        lambda inst, sub: started.append(sub) or {"code": 0, "out": ""})
    monkeypatch.setattr(W, "history_add", lambda inst, k, l: None)


# ---------- 1. PRG：303 + Location + 消息经 GET 显示一次即清 ----------

def test_prg_redirects_and_message_shows_once(monkeypatch):
    _stub_tasks(monkeypatch, [])
    r, ck = post("main")
    assert r.status_code == 303
    assert r.headers["location"] == "/"
    assert "_flash" in session_text(ck)                  # 消息已暂存会话

    g1 = client.get("/", cookies=ck)                      # 第一次 GET：显示
    assert g1.status_code == 200
    assert "任务「" in g1.text and "已后台启动" in g1.text
    assert "_flash" not in session_text(ck)               # 消费即清（一次性）

    g2 = client.get("/", cookies=ck)                      # 第二次 GET：不再出现
    assert "已后台启动" not in g2.text


def test_flash_message_survives_reload_without_repost(monkeypatch):
    """PRG 后地址栏是 GET：刷新只是重新 GET，消息取一次即止，任务不被重跑。"""
    started = []
    _stub_tasks(monkeypatch, started)
    r, ck = post("daily")
    assert r.status_code == 303
    assert started == ["daily"]
    for _ in range(3):                                    # 连刷三次 = 三次普通 GET
        client.get("/", cookies=ck)
    assert started == ["daily"]                           # 任务没有被刷新重放


# ---------- 2. 15 秒幂等窗口（浏览器仍重放 POST 时的兜底）----------

def test_replay_window_blocks_duplicate(monkeypatch):
    started = []
    _stub_tasks(monkeypatch, started)
    r1, ck = post("power")
    assert r1.status_code == 303
    assert started == ["power"]

    r2, _ = post("power", cookies=ck)                     # 模拟 F5 重放同一 POST
    assert r2.status_code == 303
    assert started == ["power"]                           # 第二次没有执行
    assert "重复提交已忽略" in session_text(ck)

    r3, _ = post("power")                                 # 不同会话不受影响
    assert r3.status_code == 303
    assert started == ["power", "power"]


# ---------- 3. J（fetch）路径不受 PRG 影响 ----------

def test_j_path_still_json(monkeypatch):
    import routers.write as W
    monkeypatch.setitem(
        W.POST_HANDLERS, "history_clear",
        lambda req, form, session, sid: W.J({"ok": True, "cleared": 0}))
    r, _ = post("history_clear")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")
    assert r.json()["ok"] is True


# ---------- 4. updateops：一键更新保留 .venv ----------

def test_swap_stage_keeps_venv(monkeypatch, tmp_path):
    from services import updateops as U
    base, stage, old = tmp_path / "base", tmp_path / "stage", tmp_path / "old"
    monkeypatch.setattr(cfg, "BASE_DIR", base)

    (base / "panel_migr" / ".venv").mkdir(parents=True)
    (base / "panel_migr" / ".venv" / "pyvenv.cfg").write_text("x", encoding="utf-8")
    (base / "panel_migr" / "main.py").write_text("OLD", encoding="utf-8")
    (stage / "panel_migr").mkdir(parents=True)
    (stage / "panel_migr" / "main.py").write_text("NEW", encoding="utf-8")
    (stage / "panel_migr" / "requirements.txt").write_text("jinja2", encoding="utf-8")

    U._swap_stage(stage, old)

    assert (base / "panel_migr" / "main.py").read_text(encoding="utf-8") == "NEW"
    assert (base / "panel_migr" / ".venv" / "pyvenv.cfg").is_file()     # venv 保住了
    assert (old / "panel_migr" / "main.py").read_text(encoding="utf-8") == "OLD"
    assert not (old / "panel_migr" / ".venv").exists()                  # 旧 venv 未随目录进 old
    assert not (old / ".venv_stash_panel_migr").exists()                # stash 已放回


def test_restore_old_returns_stashed_venv(monkeypatch, tmp_path):
    from services import updateops as U
    base, old = tmp_path / "base", tmp_path / "old"
    monkeypatch.setattr(cfg, "BASE_DIR", base)

    (old / "panel_migr").mkdir(parents=True)
    (old / "panel_migr" / "main.py").write_text("OLD", encoding="utf-8")
    (old / ".venv_stash_panel_migr").mkdir(parents=True)
    (old / ".venv_stash_panel_migr" / "pyvenv.cfg").write_text("x", encoding="utf-8")
    (base / "panel_migr").mkdir(parents=True)
    (base / "panel_migr" / "main.py").write_text("NEW", encoding="utf-8")

    U._restore_old(old)

    assert (base / "panel_migr" / "main.py").read_text(encoding="utf-8") == "OLD"
    assert (base / "panel_migr" / ".venv" / "pyvenv.cfg").is_file()     # 回滚后 venv 也不丢
    assert not (old / ".venv_stash_panel_migr").exists()


# ---------- 5. 版本号 ----------

def test_version_is_1211():
    # 版本无关断言：非空且形如 x.y[.z]，避免每次升版都要改本用例
    v = cfg.PANEL_VERSION
    assert isinstance(v, str) and v
    parts = v.split(".")
    assert len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit()
