"""v1.21 新增能力测试：快捷操作链式启动（小助手自动运行）、随时停止入口、
CPU 归一为 0-100 语义与历史数据一次性迁移。"""
import json
import os
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config as cfg
from fastapi.testclient import TestClient

from main import app
from services import containers as ct
from services import monitor as mon
from services.instances import instances_write

client = TestClient(app)

# 与 test_m2_get / test_m2_post 同一测试实例（收集期导入，取值保持一致才互不干扰）
INST_DIR = Path(__file__).resolve().parent / "_tmp" / "m2inst"
(INST_DIR / "logs").mkdir(parents=True, exist_ok=True)
(INST_DIR / "data").mkdir(parents=True, exist_ok=True)
TEST_INST = {"id": "m7a", "name": "测试实例", "container": "m7a-test",
             "dir": str(INST_DIR), "default": True}
instances_write([TEST_INST])

TOKEN = "v121token"


def new_session(authed=True):
    sid = "v21" + uuid.uuid4().hex[:10]
    parts = []
    if authed:
        parts.append("m7a_panel_auth|b:1;")
    parts.append(f'm7a_panel_csrf|s:{len(TOKEN)}:"{TOKEN}";')
    p = Path(cfg.SESSION_SAVE_PATH)
    p.mkdir(parents=True, exist_ok=True)
    (p / f"sess_{sid}").write_text("".join(parts), encoding="utf-8")
    return {"PHPSESSID": sid}


def post(action, data=None, follow=True):
    payload = {"action": action, "csrf": TOKEN}
    payload.update(data or {})
    ck = new_session()
    r = client.post("/", data=payload, cookies=ck, follow_redirects=False)
    if follow and r.status_code in (301, 302, 303, 307, 308):
        r = client.get(r.headers["location"], cookies=ck)
    return r


# ---------- A. 链式启动 ensure_running ----------

class _Clock:
    """可控时钟：time() 每次自增 0.5s、sleep 空转，超时用例瞬间跑完。"""

    def __init__(self):
        self.t = 0.0

    def time(self):
        self.t += 0.5
        return self.t

    def sleep(self, s):
        pass


def test_ensure_running_already_running(monkeypatch):
    monkeypatch.setattr(ct, "container_is_running", lambda inst: True)
    calls = []
    monkeypatch.setattr(ct, "compose",
                        lambda inst, args: calls.append(args) or {"code": 0, "out": ""})
    assert ct.ensure_running({"container": "x"}) == {"started": False}
    assert calls == []                    # 在跑就不该碰 compose


def test_ensure_running_chain_start(monkeypatch):
    flags = iter([False, True])           # 初始检查没跑 → up -d → 轮询已就绪
    monkeypatch.setattr(ct, "container_is_running", lambda inst: next(flags))
    calls = []
    monkeypatch.setattr(ct, "compose",
                        lambda inst, args: calls.append(args) or {"code": 0, "out": ""})
    monkeypatch.setattr(ct, "time", _Clock())
    assert ct.ensure_running({"container": "x"}) == {"started": True}
    assert calls == ["up -d"]


def test_ensure_running_up_fails(monkeypatch):
    monkeypatch.setattr(ct, "container_is_running", lambda inst: False)
    monkeypatch.setattr(ct, "compose",
                        lambda inst, args: {"code": 1, "out": "denied"})
    r = ct.ensure_running({"container": "x"})
    assert r.get("err") and "自动启动失败" in r["err"] and "denied" in r["err"]


def test_ensure_running_timeout(monkeypatch):
    monkeypatch.setattr(ct, "container_is_running", lambda inst: False)
    monkeypatch.setattr(ct, "compose", lambda inst, args: {"code": 0, "out": ""})
    monkeypatch.setattr(ct, "time", _Clock())
    r = ct.ensure_running({"container": "x"}, timeout=1)
    assert r.get("err") and "超时" in r["err"]


# ---------- A2. 快捷操作入口链式执行（HTTP 级） ----------

def _stub_task(monkeypatch, ensure_ret, task_ret):
    import routers.write as W
    monkeypatch.setattr(W, "ensure_running", lambda inst, timeout=15.0: ensure_ret)
    monkeypatch.setattr(W, "task_start", lambda inst, sub: task_ret)
    monkeypatch.setattr(W, "history_add", lambda inst, k, l: None)


def test_post_task_autostart_chain(monkeypatch):
    """容器没跑：点快捷操作 → 自动拉起小助手并接着执行任务，一次点击完成。"""
    _stub_task(monkeypatch, {"started": True}, {"code": 0, "out": ""})
    r = post("daily")
    assert "小助手已自动启动" in r.text and "日常任务" in r.text


def test_post_task_when_already_running(monkeypatch):
    _stub_task(monkeypatch, {"started": False}, {"code": 0, "out": ""})
    r = post("daily")
    assert "任务「日常任务」已后台启动" in r.text


def test_post_task_autostart_fail(monkeypatch):
    """拉起失败必须显式报错，不吞。"""
    _stub_task(monkeypatch, {"err": "小助手自动启动失败：denied"},
               {"code": 0, "out": ""})
    r = post("daily")
    assert "自动启动失败" in r.text


# ---------- B. 随时停止入口（模板 / 前端静态回归） ----------

def test_template_stop_buttons_in_quick_actions():
    tpl = (Path(__file__).resolve().parent.parent
           / "templates" / "panel.html.j2").read_text("utf-8")
    assert "stopCurrentTask" in tpl and "停止任务" in tpl
    assert "stopAssistant" in tpl and "停止小助手" in tpl


def test_panel_js_no_dead_end_restart():
    """回归：手机端快捷任务确认后只重启容器、任务没跑的缺陷必须已移除。"""
    js = (Path(__file__).resolve().parent.parent
          / "static" / "panel.js").read_text("utf-8")
    assert "submitPanelAction('restart'); return" not in js
    assert "stopCurrentTask" in js and "stopAssistant" in js
    assert "自动启动并执行" in js


# ---------- C. CPU 归一 0-100 ----------

def test_cpu_norm_percent_formula():
    assert mon.cpu_norm_percent(50, 100, 4) == 12.5       # 4 核下占 12.5%
    assert mon.cpu_norm_percent(400, 100, 4) == 100.0     # 打满整机 = 100%
    assert mon.cpu_norm_percent(9999, 100, 4) == 100.0    # 钳上限
    assert mon.cpu_norm_percent(-5, 100, 4) == 0.0        # 钳下限
    assert mon.cpu_norm_percent(10, 100, 0) == 10.0       # 核数兜底为 1


def _mk(points, minutes, **meta):
    d = {"points": [dict(p) for p in points],
         "minutes": [dict(m) for m in minutes],
         "meta": {"host": None, "hostTs": 0, "lastSample": 0, "lastMin": 0,
                  "lastNet": None, "sanitized": 0, "cpuBase": None, "cpuNorm": 0}}
    d["meta"].update(meta)
    return d


def test_cpu_history_normalize_migration():
    """历史点位一次性 ÷核数 并钳 100（按当前核数构造，任何机器上结果确定）。"""
    cores = max(1, os.cpu_count() or 1)
    d = _mk([{"t": 1, "cpu": float(cores) * 100}, {"t": 2, "cpu": float(cores) * 40},
             {"t": 3, "cpu": 50.0}, {"t": 4, "cpu": 0.0}],
            [{"t": 1, "cpu": float(cores) * 75}])
    mon._normalize_cpu(d)
    assert [p["cpu"] for p in d["points"]] == [100.0, 40.0, round(50.0 / cores, 1), 0.0]
    assert [m["cpu"] for m in d["minutes"]] == [75.0]
    assert d["meta"]["cpuNorm"] == 1


def test_cpu_normalize_runs_once():
    d = _mk([{"t": 1, "cpu": 50.0}], [], cpuNorm=1)
    mon._normalize_cpu(d)
    assert d["points"][0]["cpu"] == 50.0                  # 已归一 → 不再除核数


def test_sanitize_before_normalize_order():
    """顺序必须先剔脏点再归一：物理不可能的点若先归一会被钳成 100 混进来。"""
    cores = os.cpu_count() or 4
    d = _mk([{"t": 1, "cpu": float(cores) * 100 + 500},
             {"t": 2, "cpu": float(cores) * 100}], [])
    mon._sanitize_cpu(d)
    mon._normalize_cpu(d)
    assert len(d["points"]) == 1
    assert d["points"][0]["cpu"] == 100.0


def test_monitor_read_applies_normalize(tmp_path, monkeypatch):
    f = tmp_path / "monitor.json"
    cores = max(1, os.cpu_count() or 1)
    f.write_text(json.dumps(_mk([{"t": 1, "cpu": 100.0}], [])), "utf-8")
    monkeypatch.setattr(mon, "monitor_data_file", lambda inst: f)
    d = mon.monitor_read({"container": "v121"})
    assert d["meta"]["cpuNorm"] == 1
    assert d["points"][0]["cpu"] == round(100.0 / cores, 1)


# ---------- D. 版本 ----------

def test_panel_version_is_121():
    assert cfg.PANEL_VERSION == "1.21.1"
