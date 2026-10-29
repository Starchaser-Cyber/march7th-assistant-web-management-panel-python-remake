"""v1.24.4 服务层缺陷回归测试（C_services 审计修复）。

覆盖：
- P0-4  静默期内吞掉 down 边沿 → 之后永久不再告警（完整三段回归）
- P0-5  alert_state.json 无锁读改写（并发 alert_tick / alert_quiet 不互相覆盖）
- P1-16 静默期内 aborted 永不补推
- P1-17 alert_state_save 静默吞 OSError（返回 bool + WARNING 日志）
- P1-18 history_sync 无锁整份覆盖（并发 history_add 不丢记录）
- P1-19 每条 WS 占 1 个线程池线程（改为短轮询，不占线程）
- P1-20 时钟回拨后所有节流恒真（elapsed_ok / before_deadline）
- P1-21 logs.getmtime TOCTOU（轮转中的文件被跳过，不抛异常）
- P2    晨报「昨日告警条数」取错字段恒为 0 / eventdb prune 后 VACUUM

全部离线：网络、df、docker 一律 monkeypatch；DATA_DIR 指向 tmp_path。
"""
import inspect
import json
import logging
import os
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config as cfg
import pytest

from services import alerts as al
from services import eventdb
from services import events
from services import history as hist
from services import logs as logs_mod


# ---------- 公共工具 ----------

def _inst(tmp_path):
    d = tmp_path / "inst"
    d.mkdir(parents=True, exist_ok=True)
    (d / "logs").mkdir(exist_ok=True)
    return {"id": "v1244", "name": "v1244", "container": "v1244c", "dir": str(d)}


def _cfg(**over):
    c = {"enable": True, "channel": "bark", "target": "k", "channels": {},
         "mute": [], "disk_enable": False, "disk_warn": 15, "disk_crit": 8,
         "morning_enable": False, "morning_time": "00:00", "upstream_enable": False}
    c.update(over)
    return c


def _recorder():
    """记录 alert_send 调用；返回 (sink, fake)。sink 元素为 (severity, body)。"""
    sink = []

    def _fake(title, body, override=None, severity="warn"):
        sink.append((severity, body))
        return {"ok": True, "msg": "ok"}

    return sink, _fake


def _alert_events():
    rows = events.events_since(0) or []
    return [e["payload"] for e in rows if e["type"] == "alert"]


def _kinds(kind):
    return [p for p in _alert_events() if p.get("kind") == kind]


def _set_quiet_expired(inst):
    s = al.alert_state_load(inst)
    s["quiet_until"] = int(time.time()) - 1
    assert al.alert_state_save(inst, s) is True


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    """独立 DATA_DIR + 干净事件总线；health_tick 默认隔离（不跑 df/网络）。"""
    from services import monitor as _mon

    events.reset_state()
    data = tmp_path / "data"
    data.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(cfg, "DATA_DIR", data)
    monkeypatch.setattr(hist, "DATA_DIR", data)
    monkeypatch.setattr(_mon, "DATA_DIR", data)
    monkeypatch.setattr(al, "health_tick", lambda i: {})
    eventdb.reset()
    yield
    events.reset_state()
    eventdb.reset()


def _arm_alert(monkeypatch, inst, sink_fake, running, items=None):
    monkeypatch.setattr(al, "alert_cfg", lambda i: _cfg())
    monkeypatch.setattr(al, "alert_send", sink_fake)
    monkeypatch.setattr(al, "history_sync", lambda i: {"items": list(items or [])})
    monkeypatch.setattr(al, "container_is_running", lambda i: running["v"])


# ================= P0-4：静默期内的 down 边沿不能丢 =================

def test_p0_4_down_edge_during_quiet_is_backfilled(monkeypatch, tmp_path):
    """静默期前正常 → 静默期内掉线（不推但记 pending）→ 静默期后补推。"""
    inst = _inst(tmp_path)
    sink, fake = _recorder()
    running = {"v": True}
    _arm_alert(monkeypatch, inst, fake, running)

    # ---- 第一段：静默期前，容器正常运行，建立 prev=True ----
    r1 = al.alert_tick(inst, force=True)
    assert r1["run"] is True
    assert _kinds("down") == []
    st = al.alert_state_load(inst)
    assert st["prev"] is True and st["alerted_down"] is False

    # ---- 第二段：进入静默期（点 restart 后的 600s），此时容器掉线 ----
    al.alert_quiet(inst, 600)
    running["v"] = False
    r2 = al.alert_tick(inst, force=True)
    # 静默期内不推送
    assert sink == []
    assert _kinds("down") == []
    st = al.alert_state_load(inst)
    # 但边沿必须记账：pending_down 置位、alerted_down 仍未置位（尚未真正通知）
    assert st["prev"] is False
    assert st["pending_down"] is True
    assert st["alerted_down"] is False
    assert r2["pushed"] is False

    # ---- 第三段：静默期结束，下一轮 tick 必须补推 down ----
    _set_quiet_expired(inst)
    r3 = al.alert_tick(inst, force=True)
    assert r3["pushed"] is True
    assert len(sink) == 1 and sink[0][0] == "critical"
    downs = _kinds("down")
    assert len(downs) == 1
    assert downs[0]["pushed"] is True
    assert downs[0]["severity"] == "critical"
    assert "补发" in downs[0]["body"]          # 文案标明是静默期补发
    st = al.alert_state_load(inst)
    assert st["alerted_down"] is True
    assert st["pending_down"] is False

    # ---- 第四段：不得重复补推，且恢复后状态机仍可用 ----
    r4 = al.alert_tick(inst, force=True)
    assert r4["pushed"] is False and len(_kinds("down")) == 1
    running["v"] = True
    r5 = al.alert_tick(inst, force=True)
    assert r5["pushed"] is True
    assert _kinds("recovered") and _kinds("recovered")[-1]["pushed"] is True
    assert al.alert_state_load(inst)["alerted_down"] is False


def test_p0_4_pending_down_dropped_if_recovered_within_quiet(monkeypatch, tmp_path):
    """静默期内掉线又自行恢复 → 补推没有意义，pending_down 应被清掉。"""
    inst = _inst(tmp_path)
    sink, fake = _recorder()
    running = {"v": True}
    _arm_alert(monkeypatch, inst, fake, running)

    al.alert_tick(inst, force=True)                 # prev=True
    al.alert_quiet(inst, 600)
    running["v"] = False
    al.alert_tick(inst, force=True)                 # 掉线（静默期）
    assert al.alert_state_load(inst)["pending_down"] is True
    running["v"] = True
    al.alert_tick(inst, force=True)                 # 静默期内恢复
    _set_quiet_expired(inst)
    r = al.alert_tick(inst, force=True)

    assert r["pushed"] is False
    assert sink == []                               # 没有迟到的 down 告警
    st = al.alert_state_load(inst)
    assert st["pending_down"] is False and st["alerted_down"] is False


# ================= P0-5：alert_state.json 读改写加锁 =================

def test_p0_5_state_lock_is_rlock():
    assert isinstance(al._STATE_LOCK, type(threading.RLock()))
    # RLock 必须可重入：alert_tick 持锁期间会再进 health_tick / alert_state_save
    assert al._STATE_LOCK.acquire(timeout=0) is True
    assert al._STATE_LOCK.acquire(timeout=0) is True
    al._STATE_LOCK.release()
    al._STATE_LOCK.release()


def test_p0_5_tmp_name_is_unique_per_call(tmp_path):
    p = tmp_path / "alert_state.json"
    n1, n2 = al._tmp_name(p), al._tmp_name(p)
    assert n1 != n2
    assert str(os.getpid()) in n1.name
    assert n1.name.startswith("alert_state.json.") and n1.name.endswith(".tmp")


def test_p0_5_concurrent_tick_and_quiet_do_not_lose_updates(monkeypatch, tmp_path):
    """alert_tick 与 alert_quiet 并发时，两者的写入都必须留在最终文件里。"""
    inst = _inst(tmp_path)
    sink, fake = _recorder()
    running = {"v": True}
    _arm_alert(monkeypatch, inst, fake, running)

    errors = []

    def _tick():
        try:
            al.alert_tick(inst, force=True)
        except Exception as e:                     # pragma: no cover
            errors.append(e)

    def _quiet():
        try:
            al.alert_quiet(inst, 600)
        except Exception as e:                     # pragma: no cover
            errors.append(e)

    for _ in range(15):
        t1 = threading.Thread(target=_tick)
        t2 = threading.Thread(target=_quiet)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

    assert errors == []
    st = al.alert_state_load(inst)                 # 文件必须仍是合法 JSON
    assert st["last_tick"] > 0                     # tick 的写入没被 quiet 覆盖
    assert st["quiet_until"] > int(time.time())    # quiet 的写入没被 tick 覆盖
    assert st["prev"] is True
    # 无残留临时文件（每个 save 都用独立名字并 replace 掉）
    leftover = [p.name for p in (tmp_path / "data").iterdir() if p.name.endswith(".tmp")]
    assert leftover == []


# ================= P1-16：静默期内 aborted 必须补推 =================

def test_p1_16_aborted_during_quiet_is_backfilled(monkeypatch, tmp_path):
    inst = _inst(tmp_path)
    sink, fake = _recorder()
    running = {"v": True}
    items = [{"id": 7, "task_label": "日常任务", "status": "aborted"}]
    _arm_alert(monkeypatch, inst, fake, running, items)

    al.alert_quiet(inst, 600)
    al.alert_tick(inst, force=True)
    assert _kinds("aborted") == []                 # 静默期内不推
    st = al.alert_state_load(inst)
    assert [x["id"] for x in st["pending_aborted"]] == [7]
    assert 7 in st["aborted_seen"]                 # 边沿已记录，不会重复检测

    _set_quiet_expired(inst)
    r = al.alert_tick(inst, force=True)
    assert r["pushed"] is True
    abs_ = _kinds("aborted")
    assert len(abs_) == 1 and abs_[0]["pushed"] is True
    assert "补发" in abs_[0]["body"]
    assert al.alert_state_load(inst)["pending_aborted"] == []

    # 补推后不得重复
    al.alert_tick(inst, force=True)
    assert len(_kinds("aborted")) == 1


# ================= P1-17：save 失败不再静默 =================

def test_p1_17_save_failure_returns_false_and_logs(monkeypatch, tmp_path, caplog):
    inst = _inst(tmp_path)
    bad = Path("/proc/m7a_v1244_no_such_dir/alert_state.json")
    monkeypatch.setattr(al, "alert_state_file", lambda i: bad)

    with caplog.at_level(logging.WARNING, logger="m7a.alerts"):
        ok = al.alert_state_save(inst, {"prev": True})
    assert ok is False                             # 调用方能感知失败
    assert any("写盘失败" in r.getMessage() for r in caplog.records)


def test_p1_17_alert_tick_reports_unsaved_state(monkeypatch, tmp_path):
    """状态落盘失败时 alert_tick 必须回传 state_saved=False，而不是假装成功。"""
    inst = _inst(tmp_path)
    sink, fake = _recorder()
    running = {"v": True}
    _arm_alert(monkeypatch, inst, fake, running)
    monkeypatch.setattr(al, "alert_state_file",
                        lambda i: Path("/proc/m7a_v1244_no_such_dir/alert_state.json"))

    r = al.alert_tick(inst, force=True)
    assert r["state_saved"] is False
    assert r["ok"] is True                         # 巡检本身不因写盘失败而崩


# ================= P1-18：history 读改写加锁 =================

def test_p1_18_concurrent_history_add_loses_no_record(monkeypatch, tmp_path):
    inst = _inst(tmp_path)
    n = 30
    errs = []

    def _add(i):
        try:
            assert hist.history_add(inst, f"k{i}", f"任务{i}") is True
        except Exception as e:                     # pragma: no cover
            errs.append(e)

    ts = [threading.Thread(target=_add, args=(i,)) for i in range(n)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()

    assert errs == []
    items = hist.history_read(inst)["items"]
    assert len(items) == n                         # 无旧快照覆盖导致的丢失
    assert len({it["id"] for it in items}) == n    # id 无重复
    assert {it["task_key"] for it in items} == {f"k{i}" for i in range(n)}


def test_p1_18_history_lock_exists_and_tmp_unique(tmp_path):
    assert isinstance(hist._HISTORY_LOCK, type(threading.RLock()))
    p = tmp_path / "history_x.json"
    assert hist._tmp_name(p) != hist._tmp_name(p)


# ================= P1-19：WS 等待不占线程池 =================

def test_p1_19_ws_sender_does_not_block_thread_pool():
    """WS 推送循环不得再用 asyncio.to_thread 长期占用线程池线程。

    审计实测「100 条 WS 后线程数恒为 10」其实是默认线程池
    （min(32, cpu+4)）被占满的表现：第 13 条起的连接要排队等线程，
    推送与 15s 保活 ping 一起被拖延 → 客户端误判断线。
    """
    from routers import events as ev_router

    src = inspect.getsource(ev_router)
    # 只看代码行，忽略注释（注释里保留了对旧实现的说明）
    code = "\n".join(ln for ln in src.splitlines()
                     if not ln.lstrip().startswith("#"))
    assert "asyncio.to_thread(events.wait_new" not in code
    assert "asyncio.sleep(_POLL_IV)" in code
    # 事件总线侧的阻塞等待仍然保留（供其它同步调用方使用）
    assert hasattr(events, "wait_new")


def test_p1_19_ws_still_pushes_live_events():
    """改为轮询后，事件仍然能及时推给已连接的客户端。"""
    from fastapi.testclient import TestClient
    from main import app
    import uuid

    sid = "v1244" + uuid.uuid4().hex[:10]
    sp = Path(cfg.SESSION_SAVE_PATH)
    sp.mkdir(parents=True, exist_ok=True)
    (sp / f"sess_{sid}").write_text("m7a_panel_auth|b:1;", encoding="utf-8")
    client = TestClient(app)
    with client.websocket_connect("/m7a-events",
                                  headers={"cookie": f"PHPSESSID={sid}"}) as ws:
        hello = json.loads(ws.receive_text())
        assert hello["type"] == "hello"
        events.publish("system", {"n": 42})
        evt = json.loads(ws.receive_text())
        assert evt["type"] == "system" and evt["payload"] == {"n": 42}


# ================= P1-20：时钟回拨 =================

def test_p1_20_elapsed_ok_clock_rollback_counts_as_due():
    now = 1_700_000_000
    # 正常：未到间隔 → False；到点 → True
    assert logs_mod.elapsed_ok(now, now - 10, 45) is False
    assert logs_mod.elapsed_ok(now, now - 45, 45) is True
    assert logs_mod.elapsed_ok(now, now - 1000, 45) is True
    # last 为 0/缺失/不可解析 → 首次执行
    assert logs_mod.elapsed_ok(now, 0, 45) is True
    assert logs_mod.elapsed_ok(now, None, 45) is True
    assert logs_mod.elapsed_ok(now, "bad", 45) is True
    # 时钟回拨：last 落在未来（合理偏差内）→ 视为到期，不再恒真阻塞
    assert logs_mod.elapsed_ok(now, now + 60, 45) is True
    # 远超合理范围的脏数据（如损坏的 lastSample=9999999999）→ 仍按节流处理
    assert logs_mod.elapsed_ok(now, 9_999_999_999, 45) is False


def test_p1_20_before_deadline_clock_rollback():
    now = 1_700_000_000
    assert logs_mod.before_deadline(now, now + 600) is True      # 正常等待期内
    assert logs_mod.before_deadline(now, now - 1) is False       # 已过期
    assert logs_mod.before_deadline(now, 0) is False
    # 回拨遗留的极端未来时间戳 → 视为脏数据，不能永久卡住重试/静默
    assert logs_mod.before_deadline(now, now + logs_mod.MAX_FUTURE_SKEW + 1) is False


def test_p1_20_alert_tick_not_throttled_after_rollback(monkeypatch, tmp_path):
    """last_tick 落在未来（回拨）时，非 force 的 tick 也必须照常执行。"""
    inst = _inst(tmp_path)
    sink, fake = _recorder()
    running = {"v": True}
    _arm_alert(monkeypatch, inst, fake, running)

    s = al.alert_state_load(inst)
    s["last_tick"] = int(time.time()) + 120         # 回拨 2 分钟
    assert al.alert_state_save(inst, s) is True

    r = al.alert_tick(inst)                         # 注意：force=False
    assert r.get("throttled") is not True
    assert al.alert_state_load(inst)["last_tick"] <= int(time.time()) + 1


def test_p1_20_eventdb_sample_throttle_after_rollback(monkeypatch, tmp_path):
    """采样节流 _last_sample_ts 落在未来时不应永久拒绝落库。"""
    now = int(time.time())
    monkeypatch.setitem(eventdb._last_sample_ts, "rb", now + 30)
    assert eventdb.insert_sample({"t": now, "cpu": 1.0}, True, inst="rb") is True


# ================= P1-21：logs getmtime TOCTOU =================

def test_p1_21_log_files_skips_vanished_file(monkeypatch, tmp_path):
    inst = _inst(tmp_path)
    logs_dir = Path(inst["dir"]) / "logs"
    older = logs_dir / "a.log"
    newer = logs_dir / "b.log"
    ghost = logs_dir / "ghost.log"
    older.write_text("old", encoding="utf-8")
    newer.write_text("new", encoding="utf-8")
    os.utime(older, (1_600_000_000, 1_600_000_000))
    os.utime(newer, (1_700_000_000, 1_700_000_000))

    real_glob = logs_mod.glob.glob
    monkeypatch.setattr(logs_mod.glob, "glob",
                        lambda pat: real_glob(pat) + [str(ghost)])

    files = logs_mod.log_files(inst)               # 不抛 FileNotFoundError
    assert files == [str(newer), str(older)]
    assert str(ghost) not in files


def test_p1_21_history_sync_tolerates_rotated_log(monkeypatch, tmp_path):
    """latest_log_path 返回的日志在 stat 前被轮转掉 → history_sync 不抛异常。"""
    inst = _inst(tmp_path)
    log = Path(inst["dir"]) / "logs" / "gone.log"
    log.write_text("x", encoding="utf-8")
    monkeypatch.setattr(hist, "latest_log_path", lambda i: str(log))
    log.unlink()

    d = hist.history_sync(inst)                    # 不抛 FileNotFoundError
    assert d == {"items": []}


# ================= P2：晨报告警条数 / eventdb VACUUM =================

def test_p2_morning_report_counts_pushed_alerts(monkeypatch, tmp_path):
    """list_events 返回的是已展开 dict（eventdb `**body`），晨报不能再取 payload。"""
    inst = _inst(tmp_path)
    now = int(time.time())
    yts = now - 86400                              # 必须落在「昨日」区间内
    monkeypatch.setattr(al, "history_sync", lambda i: {"items": []})
    monkeypatch.setattr(al, "disk_status", lambda i: {"worst": None, "probes": []})
    monkeypatch.setattr(al, "container_is_running", lambda i: True)

    eventdb.insert_event(yts, "alert", "v1244", {
        "kind": "down", "title": "t", "body": "b",
        "severity": "critical", "pushed": True, "retry_count": 0, "msg": "",
    })
    eventdb.insert_event(yts, "alert", "v1244", {
        "kind": "aborted", "title": "t", "body": "b",
        "severity": "warn", "pushed": True, "retry_count": 0, "msg": "",
    })
    eventdb.insert_event(yts, "alert", "v1244", {
        "kind": "down", "title": "t", "body": "b",
        "severity": "critical", "pushed": False, "retry_count": 0, "msg": "",
    })

    body = al.morning_report_body(inst)
    assert "【告警通知】2 条" in body              # 只统计真正推送出去的
    assert "critical 1" in body and "warn 1" in body


def test_p2_eventdb_trim_runs_vacuum_periodically(monkeypatch, tmp_path):
    now = int(time.time())
    eventdb.insert_event(now - 10, "alert", "v", {"kind": "x"})
    monkeypatch.setattr(eventdb, "_trim_count", eventdb.VACUUM_EVERY_N_TRIMS - 1)

    assert eventdb.trim(now) is True
    assert eventdb._trim_count == eventdb.VACUUM_EVERY_N_TRIMS
    # VACUUM 之后连接仍可用（未把连接留在事务里）
    assert eventdb.insert_event(now, "alert", "v", {"kind": "y"}) is True
    assert eventdb.list_events("alert", 10)
