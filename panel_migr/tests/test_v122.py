"""v1.22 后端能力测试：告警重试/退避/去重、监控自定义时段查询、
告警历史筛选 + 清空。全部离线，不依赖真实网络/服务器。

- 告警：monkeypatch alert_send 伪造失败/成功 + 可控时钟（不 sleep）。
- monitor_range：向 SQLite（DATA_DIR 指向 tmp）灌入样本/分钟数据后区间查询。
- HTTP 级：TestClient + 自制 session（登录态 + csrf）。
"""
import json
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config as cfg
import pytest
from fastapi.testclient import TestClient

from main import app
from services import alerts as al
from services import eventdb
from services import events

client = TestClient(app)


# ---------- 公共工具 ----------

class _Clock:
    """可控时钟：只提供 time()，不 sleep。"""

    def __init__(self, t=1_700_000_000.0):
        self.t = float(t)

    def time(self):
        return self.t


def _inst(tmp_path):
    return {"id": "v122", "name": "v122", "container": "v122", "dir": str(tmp_path)}


def _authed_cookies(csrf=None):
    sid = "v122" + uuid.uuid4().hex[:10]
    parts = ["m7a_panel_auth|b:1;"]
    if csrf:
        parts.append(f'm7a_panel_csrf|s:{len(csrf)}:"{csrf}";')
    p = Path(cfg.SESSION_SAVE_PATH)
    p.mkdir(parents=True, exist_ok=True)
    (p / f"sess_{sid}").write_text("".join(parts), encoding="utf-8")
    return {"PHPSESSID": sid}


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    """每个用例独立 DATA_DIR + 清空事件总线/DB 连接。

    history/monitor 的 DATA_DIR 是模块级绑定（`from config import DATA_DIR`），
    必须一并 patch，否则 alert_state.json 会落到共享目录、跨用例串状态。
    """
    from services import history as _hist
    from services import monitor as _mon
    events.reset_state()
    data = tmp_path / "data"
    monkeypatch.setattr(cfg, "DATA_DIR", data)
    monkeypatch.setattr(_hist, "DATA_DIR", data)
    monkeypatch.setattr(_mon, "DATA_DIR", data)
    eventdb.reset()
    yield
    events.reset_state()
    eventdb.reset()


def _alert_events():
    return [e["payload"] for e in events.events_since(0) if e["type"] == "alert"]


# ================= 1. 告警状态机：重试 =================

def test_alert_retry_30_60_120_then_give_up(monkeypatch, tmp_path):
    inst = _inst(tmp_path)
    clk = _Clock()
    monkeypatch.setattr(al, "time", clk)
    runs = iter([True, False, False, False, False])
    monkeypatch.setattr(al, "container_is_running", lambda i: next(runs))
    monkeypatch.setattr(al, "alert_cfg",
                        lambda i: {"enable": True, "channel": "bark", "target": "tk"})
    fails = []

    def _send(title, body, override=None):
        fails.append(title)
        return {"ok": False, "msg": "HTTP 500"}

    monkeypatch.setattr(al, "alert_send", _send)

    r1 = al.alert_tick(inst, force=True)          # prev=None→True：无事件
    assert r1["pushed"] is False
    r2 = al.alert_tick(inst, force=True)          # True→False：down，推送失败
    assert r2["pushed"] is False
    s = al.alert_state_load(inst)
    assert [p["kind"] for p in s["pending"]] == ["down"]
    assert s["retry_count"] == 0
    assert s["retry_at"] == int(clk.time()) + cfg.ALERT_RETRY_DELAYS[0]

    # 第 1 次重试（+30s）
    clk.t += 30
    al.alert_tick(inst, force=True)
    s = al.alert_state_load(inst)
    assert s["retry_count"] == 1
    assert s["retry_at"] == int(clk.time()) + cfg.ALERT_RETRY_DELAYS[1]

    # 第 2 次重试（+60s）
    clk.t += 60
    al.alert_tick(inst, force=True)
    s = al.alert_state_load(inst)
    assert s["retry_count"] == 2
    assert s["retry_at"] == int(clk.time()) + cfg.ALERT_RETRY_DELAYS[2]

    # 第 3 次重试（+120s）→ 用尽后放弃
    clk.t += 120
    al.alert_tick(inst, force=True)
    s = al.alert_state_load(inst)
    assert s["pending"] == [] and s["retry_at"] == 0

    # 首次失败 + 3 次重试 = 4 次发送尝试
    assert len(fails) == 4
    # 事件 payload 带 retry_count，供前端展示重试次数
    rc = [e.get("retry_count") for e in _alert_events() if e.get("kind") == "down"]
    assert 0 in rc and 1 in rc and 2 in rc
    assert any("用尽" in str(e.get("msg") or "") for e in _alert_events())


def test_alert_retry_success_clears_pending(monkeypatch, tmp_path):
    inst = _inst(tmp_path)
    clk = _Clock()
    monkeypatch.setattr(al, "time", clk)
    runs = iter([True, False, False])
    monkeypatch.setattr(al, "container_is_running", lambda i: next(runs))
    monkeypatch.setattr(al, "alert_cfg",
                        lambda i: {"enable": True, "channel": "bark", "target": "tk"})
    state = {"n": 0}

    def _send(title, body, override=None):
        state["n"] += 1
        return {"ok": False, "msg": "boom"} if state["n"] == 1 else {"ok": True, "msg": "ok"}

    monkeypatch.setattr(al, "alert_send", _send)

    al.alert_tick(inst, force=True)               # prev None→True
    al.alert_tick(inst, force=True)               # down，失败 → pending
    assert al.alert_state_load(inst)["pending"]
    clk.t += 30
    al.alert_tick(inst, force=True)               # 重试成功 → 清空
    s = al.alert_state_load(inst)
    assert s["pending"] == [] and s["retry_count"] == 0
    assert s["push_fail_streak"] == 0
    assert any(e.get("pushed") and e.get("retry_count") == 1
               for e in _alert_events() if e.get("kind") == "down")


# ================= 1b. 告警状态机：退避 =================

def test_alert_backoff_suppresses_new_push(monkeypatch, tmp_path):
    inst = _inst(tmp_path)
    clk = _Clock()
    monkeypatch.setattr(al, "time", clk)
    runs = iter([True, False, False, False])
    monkeypatch.setattr(al, "container_is_running", lambda i: next(runs))
    monkeypatch.setattr(al, "alert_cfg",
                        lambda i: {"enable": True, "channel": "bark", "target": "tk"})
    sent = []

    def _send(title, body, override=None):
        sent.append(title)
        return {"ok": False, "msg": "down"}

    monkeypatch.setattr(al, "alert_send", _send)

    # 预置连续失败 2 次，下一次新推送失败即达 3 → 进入冷静期
    s = al.alert_state_load(inst)
    s["push_fail_streak"] = 2
    al.alert_state_save(inst, s)

    al.alert_tick(inst, force=True)               # prev None→True
    al.alert_tick(inst, force=True)               # down，失败 → streak=3 → 冷静期
    s = al.alert_state_load(inst)
    assert s["push_backoff_until"] > int(clk.time())
    assert s["push_fail_streak"] >= cfg.ALERT_BACKOFF_STREAK

    # 冷静期内：新的告警（recovered）只记事件、不推送
    before = len(sent)
    s["prev"] = False
    s["alerted_down"] = True
    s["last_tick"] = 0
    al.alert_state_save(inst, s)
    runs2 = iter([True])
    monkeypatch.setattr(al, "container_is_running", lambda i: next(runs2))
    al.alert_tick(inst, force=True)               # False→True：recovered，但冷静期
    assert len(sent) == before                    # 未新增推送
    rec = [e for e in _alert_events() if e.get("kind") == "recovered"]
    assert rec and rec[-1]["pushed"] is False
    assert "冷静期" in str(rec[-1].get("msg") or "")


# ================= 1c. 告警状态机：去重 =================

def test_alert_dedup_same_kind_within_window(monkeypatch, tmp_path):
    inst = _inst(tmp_path)
    clk = _Clock()
    monkeypatch.setattr(al, "time", clk)
    runs = iter([True, False, True, False])       # down → recovered → down(重复)
    monkeypatch.setattr(al, "container_is_running", lambda i: next(runs))
    monkeypatch.setattr(al, "alert_cfg",
                        lambda i: {"enable": True, "channel": "bark", "target": "tk"})
    sent = []
    monkeypatch.setattr(al, "alert_send",
                        lambda t, b, override=None: (sent.append(b), {"ok": True, "msg": "ok"})[1])

    al.alert_tick(inst, force=True)               # prev None→True
    al.alert_tick(inst, force=True)               # down → 已推送
    al.alert_tick(inst, force=True)               # recovered → 已推送
    al.alert_tick(inst, force=True)               # down（同 kind，窗口内）→ 去重
    assert len(sent) == 2                          # 只推了 down + recovered 各一次
    downs = [e for e in _alert_events() if e.get("kind") == "down"]
    assert len(downs) == 2                         # 两次 down 都发布事件
    assert downs[0]["pushed"] is True
    assert downs[-1]["pushed"] is False
    assert "去重" in str(downs[-1].get("msg") or "")


def test_alert_state_backward_compatible_load(tmp_path):
    """旧 alert_state.json（无新字段）能加载，缺字段用默认补齐。"""
    inst = _inst(tmp_path)
    f = al.alert_state_file(inst)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({"prev": True, "alerted_down": True,
                             "quiet_until": 0, "last_tick": 0,
                             "aborted_seen": [1, 2]}), "utf-8")
    s = al.alert_state_load(inst)
    assert s["prev"] is True and s["aborted_seen"] == [1, 2]
    assert s["pending"] == [] and s["retry_count"] == 0
    assert isinstance(s["last_push_kind_at"], dict)
    assert s["push_fail_streak"] == 0


# ================= 2. 监控自定义时段查询 =================

def _seed_samples(inst, base, n, step=20):
    for i in range(n):
        eventdb.insert_sample(
            {"t": base + i * step, "cpu": float(i % 50), "mem": 40.0 + i,
             "disk": 60, "netIn": 1.5 * i, "netOut": 2.5 * i,
             "load": 0.5, "uptime": 100 + i}, True, inst=inst)


def test_query_range_raw_bucket(tmp_path):
    base = 1_700_000_000
    inst = "v122r"
    _seed_samples(inst, base, 10, step=20)          # 覆盖 180 秒 → ≤2h → raw
    bucket, pts = eventdb.query_range(inst, base, base + 200)
    assert bucket == "raw"
    assert len(pts) == 10
    p0 = pts[0]
    for k in ("t", "cpu", "mem", "disk", "netIn", "netOut", "load", "uptime", "running"):
        assert k in p0
    assert p0["t"] == base and pts[-1]["t"] == base + 180


def test_query_range_minute_bucket(tmp_path):
    base = 1_700_000_000
    inst = "v122m"
    for i in range(5):
        eventdb.insert_minute(base + i * 3600, 10.0 + i, 20.0 + i, 30.0 + i, inst=inst)
    # 区间 > 2h → 自动切到分钟聚合
    bucket, pts = eventdb.query_range(inst, base, base + 5 * 3600)
    assert bucket == "minute"
    assert len(pts) == 5
    assert set(pts[0].keys()) == {"t", "cpu", "mem", "disk"}
    assert pts[0]["cpu"] == 10.0


def test_query_range_empty_and_reversed(tmp_path):
    bucket, pts = eventdb.query_range("none", 1_700_000_100, 1_700_000_000)
    assert pts == []                                 # 空区间不报错
    assert bucket == "raw"                           # frm/to 自动纠正顺序


def test_query_range_downsample(tmp_path):
    base = 1_700_000_000
    inst = "v122d"
    _seed_samples(inst, base, 30, step=20)
    bucket, pts = eventdb.query_range(inst, base, base + 1000, max_points=10)
    assert bucket == "raw"
    assert len(pts) == 10                            # 等间隔降采样
    assert pts[0]["t"] == base and pts[-1]["t"] == base + 20 * 29


def test_monitor_range_endpoint(monkeypatch, tmp_path):
    base = 1_700_000_000
    from services.instances import instance_current
    inst = instance_current({})
    iid = str(inst.get("id") or inst.get("container") or "")
    _seed_samples(iid, base, 6, step=20)
    ck = _authed_cookies()
    r = client.get(f"/?ajax=monitor_range&from={base}&to={base + 200}", cookies=ck)
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True and d["bucket"] == "raw"
    assert d["from"] == base and d["to"] == base + 200 and d["count"] == 6
    # 非法参数 → ok=false
    r2 = client.get("/?ajax=monitor_range&from=abc", cookies=ck)
    assert r2.json()["ok"] is False
    # 空区间 → 空 points，不报错
    r3 = client.get(f"/?ajax=monitor_range&from={base + 90000}&to={base + 90100}",
                    cookies=ck)
    assert r3.json()["ok"] is True and r3.json()["points"] == []


# ================= 3. 告警历史筛选 + 清空 =================

def test_alert_history_filter_and_limit():
    base = int(time.time())
    events.publish("alert", {"kind": "down", "title": "d1", "pushed": True}, inst="v122")
    events.publish("alert", {"kind": "recovered", "title": "r1", "pushed": True}, inst="v122")
    events.publish("alert", {"kind": "down", "title": "d2", "pushed": False}, inst="v122")
    ck = _authed_cookies()

    r = client.get("/?ajax=alert_history", cookies=ck)
    assert r.json()["ok"] and len(r.json()["items"]) == 3      # 无参向后兼容

    r2 = client.get("/?ajax=alert_history&kind=down", cookies=ck)
    kinds = [i["kind"] for i in r2.json()["items"]]
    assert kinds == ["down", "down"]

    r3 = client.get("/?ajax=alert_history&limit=1", cookies=ck)
    assert len(r3.json()["items"]) == 1

    r4 = client.get(f"/?ajax=alert_history&from={base + 3600}&to={base + 7200}", cookies=ck)
    assert r4.json()["items"] == []                            # 时间范围外为空

    r5 = client.get(f"/?ajax=alert_history&from={base - 60}&to={base + 60}&kind=down",
                    cookies=ck)
    assert len(r5.json()["items"]) == 2


def test_clear_events_service():
    events.publish("alert", {"kind": "down"}, inst="v122")
    events.publish("history", {"id": 1, "status": "done"}, inst="v122")
    assert eventdb.clear_events("alert") == 1
    assert eventdb.list_events("alert", 10) == []
    assert len(eventdb.list_events("history", 10)) == 1        # 其它类型不受影响


def test_alert_clear_endpoint_requires_csrf_and_clears():
    events.publish("alert", {"kind": "down"}, inst="v122")
    # 无 csrf → 被拦截（PRG 303，跟随跳转后页面出现错误横幅）
    ck_bad = _authed_cookies(csrf="tok")
    r_bad = client.post("/", data={"action": "alert_clear"},
                        cookies=ck_bad, follow_redirects=False)
    assert r_bad.status_code == 303
    page = client.get(r_bad.headers["location"], cookies=ck_bad)
    assert "请求验证失败" in page.text
    assert eventdb.list_events("alert", 10) != []              # 未清空
    # 带正确 csrf → 清空
    ck = _authed_cookies(csrf="tok")
    r = client.post("/", data={"action": "alert_clear", "csrf": "tok"},
                    cookies=ck, follow_redirects=False)
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True
    assert eventdb.list_events("alert", 10) == []


def test_alert_clear_requires_login():
    r = client.post("/", data={"action": "alert_clear", "csrf": "tok"},
                    follow_redirects=False)
    # 未登录 → 渲染登录页（非 JSON 成功）
    assert "auth-card" in r.text
