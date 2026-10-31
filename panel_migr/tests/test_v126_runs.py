"""V1.26 RunLedger：运行账本（services/runs.py）单元测试。

账本的价值在于「退出码是权威结论」，所以这里重点覆盖：
- run_id 格式与校验
- 启动脚本的 shell 正确性（`cmd &` 后必须换行，不能接 `;`）
- 退出码 → 状态 / 人话的映射
- 账本四件套缺失、进程已死、prune 掉元数据等各种退化路径
- pause/resume 控制文件协议
"""
import json
import os
import time

import pytest

import config as cfg
from services import runs

INST = {"id": "t1", "container": "c1", "dir": "/srv/m7a"}


@pytest.fixture()
def inst(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "DATA_DIR", tmp_path / "data")
    d = tmp_path / "inst"
    d.mkdir()
    return {"id": "t1", "container": "c1", "dir": str(d)}


# ===================== run_id =====================

def test_new_run_id_matches_upstream_pattern():
    rid = runs.new_run_id()
    assert runs.RUN_ID_RE.match(rid), rid
    assert len(rid) == 20                        # 8+1+6+1+4


def test_new_run_id_unique():
    ids = {runs.new_run_id() for _ in range(50)}
    assert len(ids) == 50


@pytest.mark.parametrize("rid,ok", [
    ("20261031-120000-abcd", True),
    ("20261031-120000-AB12", False),             # 大写 hex 不接受
    ("2026-10-31-120000-abcd", False),
    ("20261031-120000-abcdx", False),            # 多一位
    ("", False),
    ("../../etc/passwd", False),
    ("20261031-120000-abcd\n", False),
])
def test_valid_run_id(rid, ok):
    assert runs.valid_run_id(rid) is ok


# ===================== 退出码 =====================

def test_exit_label_known_codes():
    assert runs.exit_label(0) == "成功"
    assert runs.exit_label(2) == "参数错误"
    assert runs.exit_label(4) == "已有任务在跑"


def test_exit_label_stop_signals():
    """130/137/143 都是被信号终止，文案统一。"""
    for c in (130, 137, 143):
        assert "被终止" in runs.exit_label(c), c


def test_exit_label_unknown_code():
    assert "99" in runs.exit_label(99)


def test_exit_label_none():
    assert runs.exit_label(None) == ""


# ===================== 启动脚本 =====================

def test_build_script_uses_newline_after_background():
    """`cmd &` 后必须换行；接 `;` 会让 sh 报语法错误（这是踩过的坑）。"""
    s = runs._build_script(INST, "20261031-120000-abcd", "daily")
    assert " 2>&1 &\n" in s
    assert "& ;" not in s and "&;" not in s
    assert "wait " in s and "echo $?" in s


def test_build_script_sets_control_file():
    s = runs._build_script(INST, "20261031-120000-abcd", "daily")
    assert "MARCH7TH_CONTROL_FILE=" in s
    assert "20261031-120000-abcd.control" in s


def test_build_script_task_key_is_validated_not_quoted():
    """task_key 走 _TASK_RE 白名单校验，不含 shell 元字符，所以脚本里直接内联。"""
    s = runs._build_script(INST, "20261031-120000-abcd", "daily")
    assert "python main.py daily " in s
    assert ";" not in s.split("\n")[2]


# ===================== 路径 =====================

def test_paths_live_under_logs_runs(inst):
    rid = "20261031-120000-abcd"
    assert str(runs.runs_dir(inst)).endswith("/logs/runs")
    assert str(runs.run_meta_path(inst, rid)).endswith(f"/logs/runs/{rid}.json")
    assert str(runs.run_log_path(inst, rid)).endswith(f"/logs/runs/{rid}.log")
    assert str(runs.run_exit_path(inst, rid)).endswith(f"/logs/runs/{rid}.exit")
    assert str(runs.run_pid_path(inst, rid)).endswith(f"/logs/runs/{rid}.pid")
    assert str(runs.run_control_path(inst, rid)).endswith(f"/logs/runs/{rid}.control")


# ===================== start_run =====================

def _ok_start(monkeypatch, calls=None):
    def fake(cmd, timeout=120):
        if calls is not None:
            calls.append(cmd)
        return {"code": 0, "out": ""}
    monkeypatch.setattr(runs, "run_cmd", fake)


def test_start_run_writes_meta_and_returns_id(inst, monkeypatch):
    calls = []
    _ok_start(monkeypatch, calls)
    r = runs.start_run(inst, "daily", "每日实训")
    assert r["ok"] is True
    assert runs.RUN_ID_RE.match(r["run_id"])
    meta = json.loads(runs.run_meta_path(inst, r["run_id"]).read_text("utf-8"))
    assert meta["task_key"] == "daily"
    assert meta["task_label"] == "每日实训"
    assert meta["status"] == "running"
    assert meta["schema"] == 1
    assert calls and "docker exec -d" in calls[0]


def test_start_run_rejects_bad_task_key(inst, monkeypatch):
    _ok_start(monkeypatch)
    for bad in ("", "a b", "x;rm -rf /", "-" * 3, "任务"):
        r = runs.start_run(inst, bad)
        assert "err" in r, bad


def test_start_run_fails_when_exec_fails(inst, monkeypatch):
    monkeypatch.setattr(runs, "run_cmd", lambda *_a, **_k: {"code": 1, "out": "no container"})
    r = runs.start_run(inst, "daily")
    assert "err" in r and "no container" in r["err"]


def test_start_run_after_finish_env(inst, monkeypatch):
    calls = []
    _ok_start(monkeypatch, calls)
    runs.start_run(inst, "daily", after_finish="None")
    assert "MARCH7TH_AFTER_FINISH=None" in calls[0]


def test_start_run_ignores_unknown_after_finish(inst, monkeypatch):
    calls = []
    _ok_start(monkeypatch, calls)
    runs.start_run(inst, "daily", after_finish="rm -rf /")
    assert "MARCH7TH_AFTER_FINISH" not in calls[0]


# ===================== run_status =====================

def _write_meta(inst, rid, **kw):
    runs.runs_dir(inst).mkdir(parents=True, exist_ok=True)
    meta = {"run_id": rid, "task_key": "daily", "task_label": "每日实训",
            "start_ts": int(time.time()) - 60, "end_ts": 0,
            "status": "running", "schema": 1}
    meta.update(kw)
    runs.run_meta_path(inst, rid).write_text(json.dumps(meta), "utf-8")
    return meta


def test_run_status_success_from_exit_file(inst, monkeypatch):
    rid = "20261031-120000-abcd"
    _write_meta(inst, rid)
    runs.run_exit_path(inst, rid).write_text("0\n")
    st = runs.run_status(inst, rid)
    assert st["status"] == "success" and st["exit_code"] == 0


def test_run_status_failed_from_exit_file(inst, monkeypatch):
    rid = "20261031-120000-abcd"
    _write_meta(inst, rid)
    runs.run_exit_path(inst, rid).write_text("1\n")
    st = runs.run_status(inst, rid)
    assert st["status"] == "failed" and st["exit_code"] == 1


def test_run_status_stopped_from_sigterm(inst, monkeypatch):
    rid = "20261031-120000-abcd"
    _write_meta(inst, rid)
    runs.run_exit_path(inst, rid).write_text("143\n")
    assert runs.run_status(inst, rid)["status"] == "stopped"


def test_run_status_running_when_pid_alive(inst, monkeypatch):
    rid = "20261031-120000-abcd"
    _write_meta(inst, rid)
    runs.run_pid_path(inst, rid).write_text("4242\n")
    monkeypatch.setattr(runs, "_pid_alive", lambda *_a: True)
    assert runs.run_status(inst, rid)["status"] == "running"


def test_run_status_paused_via_control_state(inst, monkeypatch):
    rid = "20261031-120000-abcd"
    _write_meta(inst, rid)
    runs.run_pid_path(inst, rid).write_text("4242\n")
    runs.run_control_path(inst, rid).with_suffix(".control.state").write_text('{"state":"paused"}')
    monkeypatch.setattr(runs, "_pid_alive", lambda *_a: True)
    assert runs.run_status(inst, rid)["status"] == "paused"


def test_run_status_aborted_when_pid_dead_no_exit(inst, monkeypatch):
    """进程已死、无 .exit、日志也早已停写 → 中断。"""
    rid = "20261031-120000-abcd"
    _write_meta(inst, rid, start_ts=int(time.time()) - 600)
    runs.run_pid_path(inst, rid).write_text("4242\n")
    lp = runs.run_log_path(inst, rid)
    lp.write_text("x")
    os.utime(lp, (time.time() - 600, time.time() - 600))
    monkeypatch.setattr(runs, "_pid_alive", lambda *_a: False)
    assert runs.run_status(inst, rid)["status"] == "aborted"


def test_run_status_not_aborted_during_exit_race(inst, monkeypatch):
    """进程刚退出但 .exit 还没落盘（日志刚停写）→ 仍算 running，不能误报中断。

    这是实跑 notify 任务时踩到的真竞态：`wait` 返回后 `echo $? > .exit`
    之间有几秒窗口，若此刻判 aborted，成功任务会被记成中断。
    """
    rid = "20261031-120000-abcd"
    _write_meta(inst, rid, start_ts=int(time.time()) - 10)
    runs.run_pid_path(inst, rid).write_text("4242\n")
    runs.run_log_path(inst, rid).write_text("done\n")
    monkeypatch.setattr(runs, "_pid_alive", lambda *_a: False)
    assert runs.run_status(inst, rid)["status"] == "running"


def test_run_status_unknown_when_meta_missing(inst):
    """账本元数据不存在（被 prune / 非本面板启动）→ unknown，交回 mtime 启发式。"""
    st = runs.run_status(inst, "20200101-000000-dead")
    assert st["status"] == "unknown"
    assert st["start_ts"] == 0


def test_run_status_no_pid_yet_is_running(inst, monkeypatch):
    rid = "20261031-120000-abcd"
    _write_meta(inst, rid, start_ts=int(time.time()) - 5)
    assert runs.run_status(inst, rid)["status"] == "running"


def test_run_status_no_pid_long_ago_is_failed(inst, monkeypatch):
    rid = "20261031-120000-abcd"
    _write_meta(inst, rid, start_ts=int(time.time()) - 600)
    assert runs.run_status(inst, rid)["status"] == "failed"


# ===================== list / latest / prune =====================

def test_list_runs_newest_first(inst, monkeypatch):
    for rid in ("20260101-000000-aaaa", "20260102-000000-bbbb"):
        _write_meta(inst, rid)
    items = runs.list_runs(inst)
    assert [i["run_id"] for i in items][0] == "20260102-000000-bbbb"


def test_latest_run_empty(inst):
    assert runs.latest_run(inst) in (None, {})


def test_prune_runs_keeps_newest(inst, monkeypatch):
    for i in range(6):
        rid = f"2026010{i}-000000-aaaa"
        _write_meta(inst, rid)
        runs.run_log_path(inst, rid).write_text("x")
    runs.prune_runs(inst, keep=3)
    left = sorted(p.name for p in runs.runs_dir(inst).glob("*.json"))
    assert len(left) == 3
    assert left[-1].startswith("20260105")


# ===================== stop / control =====================

def test_stop_run_without_pid_reports_error(inst, monkeypatch):
    rid = "20261031-120000-abcd"
    _write_meta(inst, rid)
    r = runs.stop_run(inst, rid)
    assert "err" in r and "进程号" in r["err"]


def test_stop_run_sends_sigterm(inst, monkeypatch):
    rid = "20261031-120000-abcd"
    _write_meta(inst, rid)
    runs.run_pid_path(inst, rid).write_text("4242\n")
    calls = []
    monkeypatch.setattr(runs, "_pid_alive", lambda *_a: True)
    monkeypatch.setattr(runs, "run_cmd", lambda c, timeout=120: calls.append(c) or {"code": 0, "out": ""})
    monkeypatch.setattr(runs.time, "sleep", lambda _s: None)
    r = runs.stop_run(inst, rid)
    assert "msg" in r
    assert any("kill -15 4242" in c for c in calls)


def test_request_control_only_accepts_pause_resume(inst):
    rid = "20261031-120000-abcd"
    assert "err" in runs.request_control(inst, rid, "rm -rf /")
    assert "ok" in runs.request_control(inst, rid, "pause")


def test_control_state_reads_receipt(inst):
    rid = "20261031-120000-abcd"
    runs.runs_dir(inst).mkdir(parents=True, exist_ok=True)
    runs.run_control_path(inst, rid).with_suffix(".control.state").write_text('{"state":"pausing"}')
    assert runs.control_state(inst, rid) == "pausing"


# ===================== 日志 =====================

def test_clean_ansi_strips_color_codes():
    assert runs.clean_ansi("\x1b[31mred\x1b[0m") == "red"


def test_clean_ansi_keeps_plain_text():
    assert runs.clean_ansi("普通日志") == "普通日志"


def test_read_log_missing_file(inst):
    d = runs.read_log(inst, "20261031-120000-abcd")
    assert d["lines"] == "" or "不存在" in str(d.get("lines", ""))


def test_read_log_returns_content(inst, monkeypatch):
    rid = "20261031-120000-abcd"
    _write_meta(inst, rid)
    runs.run_log_path(inst, rid).write_text("line1\nline2\n", "utf-8")
    monkeypatch.setattr(runs, "run_cmd",
                        lambda *_a, **_k: {"code": 0, "out": "line1\nline2"})
    d = runs.read_log(inst, rid)
    assert "line1" in d["lines"]
