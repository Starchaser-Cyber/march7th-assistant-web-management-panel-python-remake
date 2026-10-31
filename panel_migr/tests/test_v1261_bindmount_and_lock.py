"""V1.26.1：两个 P0 修复的回归测试。

1. **单文件 bind mount 断链**（P0）
   `config.yaml` 是以 `./config.yaml:/m7a/config.yaml` 单文件 bind mount 挂进
   容器的。旧实现 `phpfile.atomic_write_text` 用 `os.replace` 落盘，会把 inode
   换掉；容器里的挂载点仍钉在旧 inode 上（mountinfo 里出现 `<src>//deleted`），
   面板写进去的配置容器**永远读不到**，直到容器重启。
   实测：2026-10-31 12:53 之后面板改了三次配置，容器内 config.yaml 一直停在
   12:53 之前的版本，`cloud_game_use_paid_time` 宿主为 False、容器读出来是 True。

   修复：`atomic_write_text(..., inplace=True)` 走「临时文件 + fsync → 原地
   覆盖 + ftruncate」，inode 不变。

2. **任务互斥锁**（P0）
   面板旧实现没有「已有任务在跑」检查，连点两次就起两个 `python main.py`。
   小助手本体的 `module/game/cloud.py::close_all_m7a_browser()` 按 cmdline
   匹配同 tag 的浏览器并 terminate，两个实例会轮流关掉对方的浏览器、抢
   9222/9224 端口，最终 `tab crashed` 雪崩（10-31 实测 174 次）。
"""
import json
import os

import pytest

import config as cfg
import phpfile
from services import runs

INST = {"id": "t1", "container": "c1", "dir": "/srv/m7a"}


@pytest.fixture()
def inst(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "DATA_DIR", tmp_path / "data")
    d = tmp_path / "inst"
    d.mkdir()
    return {"id": "t1", "container": "c1", "dir": str(d)}


# ===================== 1. bind mount 不断链 =====================


def test_inplace_write_keeps_inode(tmp_path):
    """核心回归：原地覆盖后 inode 必须不变，否则容器 bind mount 断链。"""
    p = tmp_path / "config.yaml"
    p.write_text("a: 1\n", encoding="utf-8")
    ino = os.stat(p).st_ino

    phpfile.atomic_write_text(p, "a: 2\nb: 3\n", inplace=True)

    assert os.stat(p).st_ino == ino, "inode 变了 → 容器 bind mount 会断链"
    assert p.read_text(encoding="utf-8") == "a: 2\nb: 3\n"


def test_replace_write_changes_inode(tmp_path):
    """对照组：默认（非 inplace）行为仍是 os.replace，inode 会变。

    这条用来证明上一条测的是真差异，而不是文件系统恰好复用 inode。
    """
    p = tmp_path / "config.yaml"
    p.write_text("a: 1\n", encoding="utf-8")
    ino = os.stat(p).st_ino

    phpfile.atomic_write_text(p, "a: 2\n")

    assert os.stat(p).st_ino != ino


def test_inplace_write_truncates_tail(tmp_path):
    """新内容更短时必须 ftruncate，不能留旧文件尾巴（否则 YAML 尾部残留）。"""
    p = tmp_path / "config.yaml"
    p.write_text("a: 1\n" + "b: " + "x" * 500 + "\n", encoding="utf-8")

    phpfile.atomic_write_text(p, "a: 2\n", inplace=True)

    assert p.read_text(encoding="utf-8") == "a: 2\n"
    assert p.stat().st_size == len("a: 2\n")


def test_inplace_write_preserves_mode(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("a: 1\n", encoding="utf-8")
    os.chmod(p, 0o644)

    phpfile.atomic_write_text(p, "a: 2\n", mode=0o644, inplace=True)

    assert (os.stat(p).st_mode & 0o777) == 0o644


def test_inplace_write_creates_when_missing(tmp_path):
    """目标不存在时（首次部署）不能走原地覆盖，必须能正常建文件。"""
    p = tmp_path / "config.yaml"

    phpfile.atomic_write_text(p, "a: 1\n", inplace=True)

    assert p.read_text(encoding="utf-8") == "a: 1\n"


def test_inplace_write_cleans_tmp(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("a: 1\n", encoding="utf-8")

    phpfile.atomic_write_text(p, "a: 2\n", inplace=True)

    assert not list(tmp_path.glob(".tmp_*")), "临时文件必须清理干净"


def test_inplace_write_utf8(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("a: 1\n", encoding="utf-8")

    phpfile.atomic_write_text(p, "名字: 三月七\n", inplace=True)

    assert p.read_text(encoding="utf-8") == "名字: 三月七\n"


def test_configops_uses_inplace(tmp_path, monkeypatch):
    """configops._atomic_write_text 必须传 inplace=True。

    这是真正打到 config.yaml 的那一层；只改 phpfile 而忘了这里等于没修。
    """
    from services import configops

    p = tmp_path / "config.yaml"
    p.write_text("a: 1\n", encoding="utf-8")
    ino = os.stat(p).st_ino

    configops._atomic_write_text(p, "a: 2\n")

    assert os.stat(p).st_ino == ino
    assert p.read_text(encoding="utf-8") == "a: 2\n"


def test_configops_source_passes_inplace():
    """源码级断言：防止后续重构把 inplace=True 丢了。"""
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "services" / "configops.py").read_text("utf-8")
    assert "inplace=True" in src


# ===================== 2. 任务互斥 =====================


def _fake_run_cmd(monkeypatch, exit_codes=None):
    """让 run_cmd 假装容器一直在跑、进程一直活着。"""
    calls = []

    def fake(cmd, timeout=120):
        calls.append(cmd)
        return {"code": 0, "out": ""}

    monkeypatch.setattr(runs, "run_cmd", fake)
    return calls


def test_start_run_rejects_when_another_running(inst, monkeypatch):
    _fake_run_cmd(monkeypatch)
    first = runs.start_run(inst, "daily", "每日实训")
    assert first.get("ok") is True

    second = runs.start_run(inst, "power", "清体力")
    assert "err" in second, "已有任务在跑时必须拒绝"
    assert "已有任务在运行" in second["err"]
    assert second["busy"]["run_id"] == first["run_id"]


def test_start_run_allows_after_finished(inst, monkeypatch):
    """上一次跑完了（有 .exit）就该允许再启动，不能锁死。"""
    _fake_run_cmd(monkeypatch)
    first = runs.start_run(inst, "daily", "每日实训")
    rid = first["run_id"]
    runs.run_exit_path(inst, rid).write_text("0\n", "utf-8")
    runs.run_pid_path(inst, rid).write_text("999999\n", "utf-8")
    runs._PID_CACHE.clear()

    second = runs.start_run(inst, "power", "清体力")
    assert second.get("ok") is True, second


def test_start_run_allows_when_pid_dead(inst, monkeypatch):
    """进程已死但 .exit 还没落盘（容器被重启）→ 视为已结束，允许新启动。"""
    calls = _fake_run_cmd(monkeypatch)
    first = runs.start_run(inst, "daily", "每日实训")
    rid = first["run_id"]
    runs.run_pid_path(inst, rid).write_text("4242\n", "utf-8")

    def dead(cmd, timeout=120):
        calls.append(cmd)
        if "test -d /proc/4242" in cmd:
            return {"code": 1, "out": ""}
        return {"code": 0, "out": ""}

    monkeypatch.setattr(runs, "run_cmd", dead)
    runs._PID_CACHE.clear()

    second = runs.start_run(inst, "power", "清体力")
    assert second.get("ok") is True, second


def test_active_run_none_when_empty(inst, monkeypatch):
    _fake_run_cmd(monkeypatch)
    assert runs.active_run(inst) is None


def test_active_run_reports_running(inst, monkeypatch):
    _fake_run_cmd(monkeypatch)
    r = runs.start_run(inst, "daily", "每日实训")
    a = runs.active_run(inst)
    assert a and a["run_id"] == r["run_id"]
    assert a["status"] == "running"


def test_active_run_ignores_finished(inst, monkeypatch):
    _fake_run_cmd(monkeypatch)
    r = runs.start_run(inst, "daily", "每日实训")
    runs.run_exit_path(inst, r["run_id"]).write_text("0\n", "utf-8")
    runs.run_pid_path(inst, r["run_id"]).write_text("999999\n", "utf-8")
    runs._PID_CACHE.clear()
    assert runs.active_run(inst) is None


def test_task_start_propagates_busy(monkeypatch):
    """containers.task_start 必须把 busy 标记透出来，供路由层给友好提示。"""
    from services import containers as C

    monkeypatch.setattr(C.runs, "start_run",
                        lambda *_a, **_k: {"err": "已有任务在运行（每日实训 · x）", "busy": {"run_id": "x"}})
    r = C.task_start({"id": "x"}, "power")
    assert r["code"] == 1
    assert r.get("busy"), "busy 必须透传"


def test_task_action_shows_busy_as_error(monkeypatch):
    """路由层：busy 要走 err 横幅（红），不能混进「已后台启动」。"""
    import routers.write as W

    monkeypatch.setattr(W, "ensure_running", lambda inst, timeout=15.0: {"started": False})
    monkeypatch.setattr(W, "task_start",
                        lambda inst, sub, label="", **kw: {"code": 1, "out": "已有任务在运行（每日实训 · x）",
                                                           "busy": {"run_id": "x"}})
    monkeypatch.setattr(W, "history_add", lambda *a, **k: None)
    kind, payload = W._task_action(None, {}, {"m7a_instance": "m7a"}, "sid", "power")
    assert kind == "flash"
    assert "err" in payload and "已有任务在运行" in payload["err"]


def test_busy_message_is_actionable(inst, monkeypatch):
    """拒绝文案要告诉用户「怎么办」，不只是「失败了」。"""
    _fake_run_cmd(monkeypatch)
    runs.start_run(inst, "daily", "每日实训")
    r = runs.start_run(inst, "power")
    assert "停止" in r["err"] or "结束" in r["err"]


def test_start_run_bad_task_key_still_rejected_first(inst, monkeypatch):
    """参数校验必须在锁之前：非法任务名不该被「已有任务在跑」掩盖。"""
    _fake_run_cmd(monkeypatch)
    runs.start_run(inst, "daily", "每日实训")
    r = runs.start_run(inst, "x;rm -rf /")
    assert "非法任务名" in r["err"]
