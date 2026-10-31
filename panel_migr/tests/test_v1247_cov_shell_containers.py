"""v1.24.7 优化（方向 B）：补齐 `services/shell.py` 与 `services/containers.py` 的测试盲区。

这两个模块是**所有 docker 操作的唯一出口**（`shell.run_cmd` 是全仓唯一
`shell=True` 的 subprocess 调用点），优化前覆盖率分别只有 55% 与 54%。
它们没有测试意味着：命令拼错、超时处理、返回码映射全都靠线上验证。

本文件覆盖：
  `shell.run_cmd`
    1. 成功路径（返回码 0、stderr 合并进 out、去掉尾部换行）
    2. 非 0 返回码原样透传
    3. `TimeoutExpired` → code 124 + out 追加 "[timeout]"
    4. `TimeoutExpired` 的 stdout 是 bytes 时能解码
    5. 命令拼接格式（`cmd + " 2>&1"`、`shell=True`、`timeout` 透传）

  `containers.*`
    6. `compose` / `task_start` 的命令拼装与 `shlex.quote` 防注入
    7. `container_is_running` 的 true/false/异常输出
    8. `ensure_running` 的四条路径（已在跑 / 拉起成功 / 拉起失败 / 超时）
    9. 五个 `op_*` 的成功与失败文案
   10. `op_task` 失败走 `msg` 而非 `err`（PHP 语义）
   11. `history_clear` 的成功/失败
"""
from __future__ import annotations

import subprocess
import time

import pytest

import config as cfg
from services import containers as C
from services import shell as S


# ===================== shell.run_cmd =====================

class _FakeCompleted:
    def __init__(self, returncode=0, stdout=""):
        self.returncode = returncode
        self.stdout = stdout


@pytest.fixture()
def fake_run(monkeypatch):
    """替换 subprocess.run，记录调用参数。"""
    calls = []

    def install(returncode=0, stdout="", exc=None):
        def _run(cmd, **kwargs):
            calls.append({"cmd": cmd, **kwargs})
            if exc is not None:
                raise exc
            return _FakeCompleted(returncode, stdout)

        monkeypatch.setattr(S.subprocess, "run", _run)
        return calls

    return install


def test_run_cmd_success(fake_run):
    fake_run(returncode=0, stdout="hello\n")
    assert S.run_cmd("echo hello") == {"code": 0, "out": "hello"}


def test_run_cmd_strips_only_trailing_newlines(fake_run):
    fake_run(stdout="a\nb\n\n")
    assert S.run_cmd("x")["out"] == "a\nb"


def test_run_cmd_preserves_leading_whitespace(fake_run):
    fake_run(stdout="  indented\n")
    assert S.run_cmd("x")["out"] == "  indented"


def test_run_cmd_empty_output(fake_run):
    fake_run(stdout="")
    assert S.run_cmd("x") == {"code": 0, "out": ""}


def test_run_cmd_nonzero_code_passthrough(fake_run):
    fake_run(returncode=2, stdout="bad\n")
    assert S.run_cmd("x") == {"code": 2, "out": "bad"}


def test_run_cmd_appends_stderr_redirect(fake_run):
    calls = fake_run()
    S.run_cmd("docker ps")
    assert calls[0]["cmd"] == "docker ps 2>&1"


def test_run_cmd_uses_shell_and_text(fake_run):
    calls = fake_run()
    S.run_cmd("x", timeout=7)
    k = calls[0]
    assert k["shell"] is True
    assert k["capture_output"] is True
    assert k["text"] is True
    assert k["timeout"] == 7
    assert k["errors"] == "replace"


def test_run_cmd_default_timeout(fake_run):
    calls = fake_run()
    S.run_cmd("x")
    assert calls[0]["timeout"] == 120


def test_run_cmd_timeout_returns_124(fake_run):
    exc = subprocess.TimeoutExpired(cmd="x", timeout=1, output="partial")
    fake_run(exc=exc)
    r = S.run_cmd("x", timeout=1)
    assert r["code"] == 124
    assert r["out"].endswith("[timeout]")
    assert "partial" in r["out"]


def test_run_cmd_timeout_without_output(fake_run):
    fake_run(exc=subprocess.TimeoutExpired(cmd="x", timeout=1))
    r = S.run_cmd("x", timeout=1)
    assert r["code"] == 124
    assert "[timeout]" in r["out"]


def test_run_cmd_timeout_decodes_bytes_stdout(fake_run):
    exc = subprocess.TimeoutExpired(cmd="x", timeout=1, output="中文".encode())
    fake_run(exc=exc)
    r = S.run_cmd("x", timeout=1)
    assert "中文" in r["out"]


def test_run_cmd_timeout_none_stdout(fake_run):
    exc = subprocess.TimeoutExpired(cmd="x", timeout=1, output=None)
    fake_run(exc=exc)
    r = S.run_cmd("x", timeout=1)
    assert r["out"] == "\n[timeout]"


# ===================== containers：命令拼装 =====================

@pytest.fixture()
def cmds(monkeypatch):
    """捕获 containers 发出的所有命令字符串（不真的执行）。"""
    sent = []

    def _run_cmd(cmd, timeout=120):
        sent.append(cmd)
        return {"code": 0, "out": ""}

    monkeypatch.setattr(C, "run_cmd", _run_cmd)
    monkeypatch.setattr(C, "instance_dir", lambda _i: "/srv/m7a")
    monkeypatch.setattr(C, "instance_container", lambda _i: "m7a")
    return sent


def test_compose_builds_expected_command(cmds):
    C.compose({"id": "x"}, "ps")
    assert cmds == ["cd /srv/m7a && docker compose ps"]


def test_compose_quotes_dir(cmds, monkeypatch):
    monkeypatch.setattr(C, "instance_dir", lambda _i: "/srv/with space")
    C.compose({"id": "x"}, "ps")
    assert cmds[0] == "cd '/srv/with space' && docker compose ps"


def test_compose_dir_with_shell_metachar_is_quoted(cmds, monkeypatch):
    """`shlex.quote` 用单引号包住整段 → 里面的 $() 不会被 shell 展开。"""
    monkeypatch.setattr(C, "instance_dir", lambda _i: "/srv/$(rm -rf /)")
    C.compose({"id": "x"}, "ps")
    # 关键：危险内容必须整体落在单引号内（单引号内 shell 不做任何展开）
    assert cmds[0].startswith("cd '/srv/$(rm -rf /)' && docker compose ps")


def test_task_start_delegates_to_run_ledger(monkeypatch):
    """v1.26：task_start 不再直接 docker exec -d，而是交给 runs.start_run 建账本。"""
    seen = {}

    def fake_start(inst, sub, label="", after_finish="", invoker="panel"):
        seen.update({"sub": sub, "label": label, "after_finish": after_finish,
                     "invoker": invoker})
        return {"ok": True, "run_id": "20260101-000000-abcd", "start_ts": 123}

    monkeypatch.setattr(C.runs, "start_run", fake_start)
    r = C.task_start({"id": "x"}, "daily", "日常任务")
    assert r["code"] == 0
    assert r["run_id"] == "20260101-000000-abcd"
    assert seen["sub"] == "daily" and seen["label"] == "日常任务"
    assert seen["invoker"] == "panel"


def test_task_start_failure_surfaces_reason(monkeypatch):
    monkeypatch.setattr(C.runs, "start_run", lambda *_a, **_k: {"err": "容器没跑"})
    r = C.task_start({"id": "x"}, "daily")
    assert r["code"] == 1 and "容器没跑" in r["out"]


# ===================== container_is_running =====================

def test_container_is_running_true(monkeypatch):
    monkeypatch.setattr(C, "run_cmd", lambda *_a, **_k: {"code": 0, "out": "true"})
    assert C.container_is_running({"id": "x"}) is True


def test_container_is_running_true_with_whitespace(monkeypatch):
    monkeypatch.setattr(C, "run_cmd", lambda *_a, **_k: {"code": 0, "out": "  true  "})
    assert C.container_is_running({"id": "x"}) is True


def test_container_is_running_false(monkeypatch):
    monkeypatch.setattr(C, "run_cmd", lambda *_a, **_k: {"code": 0, "out": "false"})
    assert C.container_is_running({"id": "x"}) is False


def test_container_is_running_error_text(monkeypatch):
    monkeypatch.setattr(C, "run_cmd",
                        lambda *_a, **_k: {"code": 1, "out": "Error: No such object: m7a"})
    assert C.container_is_running({"id": "x"}) is False


def test_container_is_running_empty(monkeypatch):
    monkeypatch.setattr(C, "run_cmd", lambda *_a, **_k: {"code": 0, "out": ""})
    assert C.container_is_running({"id": "x"}) is False


# ===================== ensure_running =====================

def test_ensure_running_already_running(monkeypatch):
    monkeypatch.setattr(C, "container_is_running", lambda _i: True)
    called = []
    monkeypatch.setattr(C, "compose", lambda *_a: called.append(1) or {"code": 0, "out": ""})
    assert C.ensure_running({"id": "x"}) == {"started": False}
    assert called == []


def test_ensure_running_starts_successfully(monkeypatch):
    seq = [False, True]
    monkeypatch.setattr(C, "container_is_running", lambda _i: seq.pop(0) if seq else True)
    monkeypatch.setattr(C, "compose", lambda _i, a: {"code": 0, "out": ""})
    monkeypatch.setattr(C.time, "sleep", lambda _s: None)
    assert C.ensure_running({"id": "x"}) == {"started": True}


def test_ensure_running_compose_fails(monkeypatch):
    monkeypatch.setattr(C, "container_is_running", lambda _i: False)
    monkeypatch.setattr(C, "compose", lambda _i, a: {"code": 1, "out": "no image"})
    r = C.ensure_running({"id": "x"})
    assert "err" in r
    assert "小助手自动启动失败" in r["err"]
    assert "no image" in r["err"]


def test_ensure_running_times_out(monkeypatch):
    monkeypatch.setattr(C, "container_is_running", lambda _i: False)
    monkeypatch.setattr(C, "compose", lambda _i, a: {"code": 0, "out": ""})
    clock = {"t": 0.0}

    def fake_time():
        clock["t"] += 10.0
        return clock["t"]

    monkeypatch.setattr(C.time, "time", fake_time)
    monkeypatch.setattr(C.time, "sleep", lambda _s: None)
    r = C.ensure_running({"id": "x"}, timeout=1.0)
    assert "err" in r
    assert "启动超时" in r["err"]


def test_ensure_running_timeout_floor_is_one_second(monkeypatch):
    """timeout 传 0 或负数时，max(1.0, ...) 保证至少等 1 秒，文案也不出现 0。"""
    monkeypatch.setattr(C, "container_is_running", lambda _i: False)
    monkeypatch.setattr(C, "compose", lambda _i, a: {"code": 0, "out": ""})
    monkeypatch.setattr(C.time, "time", lambda: 1e18)   # 立刻超时
    monkeypatch.setattr(C.time, "sleep", lambda _s: None)
    r = C.ensure_running({"id": "x"}, timeout=0)
    assert "1 秒内" in r["err"]


# ===================== op_* =====================

def test_op_restart_ok(monkeypatch):
    monkeypatch.setattr(C, "compose", lambda *_a: {"code": 0, "out": ""})
    assert C.op_restart({"id": "x"}) == {"msg": "容器已重启"}


def test_op_restart_fail(monkeypatch):
    monkeypatch.setattr(C, "compose", lambda *_a: {"code": 1, "out": "denied"})
    assert C.op_restart({"id": "x"}) == {"err": "重启失败：denied"}


def test_op_update_ok(monkeypatch):
    monkeypatch.setattr(C, "compose", lambda _i, a: {"code": 0, "out": ""})
    assert C.op_update({"id": "x"}) == {"msg": "镜像已更新，容器已重建"}


def test_op_update_pull_fails(monkeypatch):
    monkeypatch.setattr(C, "compose", lambda _i, a: {"code": 1, "out": "net down"})
    assert C.op_update({"id": "x"}) == {"err": "拉取镜像失败：net down"}


def test_op_update_up_fails(monkeypatch):
    calls = []

    def fake(_i, a):
        calls.append(a)
        return {"code": 0, "out": ""} if a == "pull" else {"code": 1, "out": "boom"}

    monkeypatch.setattr(C, "compose", fake)
    assert C.op_update({"id": "x"}) == {"err": "更新失败：boom"}
    assert calls == ["pull", "up -d"]


def test_op_stop_task_ok(monkeypatch):
    monkeypatch.setattr(C, "compose", lambda *_a: {"code": 0, "out": ""})
    assert "任务已停止" in C.op_stop_task({"id": "x"})["msg"]


def test_op_stop_task_fail(monkeypatch):
    monkeypatch.setattr(C, "compose", lambda *_a: {"code": 1, "out": "x"})
    assert C.op_stop_task({"id": "x"}) == {"err": "容器重启失败：x"}


def test_op_stop_loop_ok(cmds):
    assert "货币战争循环已停止" in C.op_stop_loop({"id": "x"})["msg"]
    assert 'pkill -15 -f "main.py currencywarsloop"' in cmds[0]


def test_op_stop_loop_fail(monkeypatch):
    monkeypatch.setattr(C, "run_cmd", lambda *_a, **_k: {"code": 1, "out": "no process"})
    r = C.op_stop_loop({"id": "x"})
    assert "循环停止失败" in r["err"]


def test_op_stop_ok(monkeypatch):
    monkeypatch.setattr(C, "compose", lambda *_a: {"code": 0, "out": ""})
    assert "容器已停止" in C.op_stop({"id": "x"})["msg"]


def test_op_stop_fail(monkeypatch):
    monkeypatch.setattr(C, "compose", lambda *_a: {"code": 1, "out": "x"})
    assert C.op_stop({"id": "x"}) == {"err": "容器停止失败：x"}


# ===================== op_task =====================

def test_op_task_ok_records_history(monkeypatch):
    monkeypatch.setattr(C, "task_start", lambda *_a, **_k: {"code": 0, "out": "",
                                                             "run_id": "r1"})
    added = []
    monkeypatch.setattr(C, "history_add",
                        lambda i, a, label, run_id="": added.append((a, label)) or True)
    r = C.op_task({"id": "x"}, "daily")
    assert "已后台启动" in r["msg"]
    assert added and added[0][0] == "daily"
    assert added[0][1] == cfg.TASKS.get("daily", "daily")


def test_op_task_unknown_action_uses_action_as_label(monkeypatch):
    monkeypatch.setattr(C, "task_start", lambda *_a: {"code": 0, "out": ""})
    monkeypatch.setattr(C, "history_add", lambda *_a, **_k: True)
    r = C.op_task({"id": "x"}, "no_such")
    assert "no_such" in r["msg"]


def test_op_task_failure_uses_msg_not_err(monkeypatch):
    """PHP 的 $TASKS 分支把失败放 $msg（绿色横幅），这里刻意复刻该语义。"""
    monkeypatch.setattr(C, "task_start", lambda *_a: {"code": 1, "out": "docker down"})
    r = C.op_task({"id": "x"}, "daily")
    assert "msg" in r and "err" not in r
    assert "任务启动失败" in r["msg"]
    assert "docker down" in r["msg"]


def test_op_task_failure_does_not_record_history(monkeypatch):
    monkeypatch.setattr(C, "task_start", lambda *_a: {"code": 1, "out": "x"})
    added = []
    monkeypatch.setattr(C, "history_add", lambda *_a, **_k: added.append(1))
    C.op_task({"id": "x"}, "daily")
    assert added == []


# ===================== history_clear =====================

def test_history_clear_ok(monkeypatch):
    monkeypatch.setattr(C, "history_write", lambda *_a: True)
    r = C.history_clear({"id": "x"})
    assert r == {"ok": True, "msg": "任务执行历史已清空"}


def test_history_clear_fail(monkeypatch):
    monkeypatch.setattr(C, "history_write", lambda *_a: False)
    r = C.history_clear({"id": "x"})
    assert r["ok"] is False
    assert "清空失败" in r["msg"]


def test_history_clear_writes_empty_items(monkeypatch):
    got = {}
    monkeypatch.setattr(C, "history_write",
                        lambda i, d: got.update(d) or True)
    C.history_clear({"id": "x"})
    assert got == {"items": []}


# ===================== __all__ 契约 =====================

def test_all_exports_exist():
    for name in C.__all__:
        assert hasattr(C, name), f"__all__ 里的 {name} 不存在"
