"""V1.24.7 覆盖率补测：`services/sampler.py`（面板常驻后台 runner）。

注意：任务书提到的「实例切换后仍打旧实例」在本版**已经修过**——源码里
`runner()` 每 10 秒重读一次 `instance_current`（注释标 `审计 C-P2-8`）。
本文件固化的是**当前**行为：10 秒重读一次，而不是「拿到就永久缓存」。
"""
import asyncio
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import config as cfg
import services.sampler as sampler
from services import alerts, instances, logstream, monitor


@pytest.fixture(autouse=True)
def _clean_runner():
    """每个用例前后把模块级 _runner_task 清干净，避免跨用例泄漏。"""
    sampler.stop_runner()
    yield
    sampler.stop_runner()


# ================= sample_iv =================

def test_sample_iv_respects_floor(monkeypatch):
    monkeypatch.setattr(cfg, "SAMPLE_IV", 30)
    monkeypatch.setattr(monitor, "monitor_interval", lambda: 1)
    assert sampler.sample_iv() == 30


def test_sample_iv_respects_higher_config(monkeypatch):
    monkeypatch.setattr(cfg, "SAMPLE_IV", 5)
    monkeypatch.setattr(monitor, "monitor_interval", lambda: 60)
    assert sampler.sample_iv() == 60


def test_sample_iv_monitor_interval_raises_falls_back(monkeypatch):
    """配置读不出来 → 退回 1，再与保底取 max（不能让 runner 崩）。"""
    monkeypatch.setattr(cfg, "SAMPLE_IV", 9)

    def _boom():
        raise RuntimeError("config unreadable")

    monkeypatch.setattr(monitor, "monitor_interval", _boom)
    assert sampler.sample_iv() == 9


def test_sample_iv_floor_at_least_one(monkeypatch):
    monkeypatch.setattr(cfg, "SAMPLE_IV", 1)
    monkeypatch.setattr(monitor, "monitor_interval", lambda: 0)
    assert sampler.sample_iv() == 1


# ================= 单次 tick 委托 =================

def test_sample_once_delegates_with_iv(monkeypatch):
    captured = {}

    def _fake(inst, iv=1):
        captured["inst"] = inst
        captured["iv"] = iv
        return {"sampled": True}

    monkeypatch.setattr(monitor, "monitor_sample", _fake)
    monkeypatch.setattr(cfg, "SAMPLE_IV", 7)
    monkeypatch.setattr(monitor, "monitor_interval", lambda: 1)
    out = sampler.sample_once({"id": "demo"})
    assert out == {"sampled": True}
    assert captured["iv"] == 7
    assert captured["inst"] == {"id": "demo"}


def test_alert_once_forces_tick(monkeypatch):
    captured = {}

    def _fake(inst, force=False):
        captured["inst"] = inst
        captured["force"] = force
        return {"alerted": []}

    monkeypatch.setattr(alerts, "alert_tick", _fake)
    out = sampler.alert_once({"id": "demo"})
    assert out == {"alerted": []}
    assert captured["force"] is True, "后台 runner 必须 force=True 才能越过节流"
    assert captured["inst"] == {"id": "demo"}


def test_health_once_delegates(monkeypatch):
    captured = {}

    def _fake(inst):
        captured["inst"] = inst
        return {"disk": "ok"}

    monkeypatch.setattr(alerts, "health_tick", _fake)
    assert sampler.health_once({"id": "demo"}) == {"disk": "ok"}
    assert captured["inst"] == {"id": "demo"}


# ================= runner 驱动脚手架 =================

class _Clock:
    """可控时钟 + 可控 sleep：每睡一次前进 step 秒，超过 limit 次就抛取消。"""

    def __init__(self, start=1000.0, step=1.0, limit=0):
        self.now = start
        self.step = step
        self.limit = limit
        self.sleeps = 0
        self.sleep_args = []

    def monotonic(self):
        return self.now

    async def sleep(self, d):
        self.sleeps += 1
        self.sleep_args.append(d)
        if self.sleeps > self.limit:
            raise asyncio.CancelledError()
        self.now += self.step


def _install_clock(monkeypatch, clock):
    """只替换 sampler 命名空间里的 asyncio/time，不动真正的 asyncio 模块。"""
    aio_stub = types.SimpleNamespace(
        sleep=clock.sleep,
        CancelledError=asyncio.CancelledError,
        Task=asyncio.Task,
        get_running_loop=asyncio.get_running_loop,
    )
    monkeypatch.setattr(sampler, "asyncio", aio_stub)
    monkeypatch.setattr(sampler, "time", clock)


def _install_work(monkeypatch, *, inst_calls=None, sample=None, tail=None,
                  alert=None, trim=None, inst_raises=False):
    """替换 runner 内部 lazy import 的目标，记录调用。"""
    calls = {"inst": inst_calls if inst_calls is not None else [],
             "sample": [], "tail": [], "alert": [], "trim": []}

    def _instance_current(_):
        calls["inst"].append(True)
        if inst_raises:
            raise RuntimeError("no instance")
        return {"id": "inst%d" % len(calls["inst"])}

    def _sample(inst):
        calls["sample"].append(inst)
        if sample is not None:
            sample(inst)

    def _tail(inst):
        calls["tail"].append(inst)
        if tail is not None:
            tail(inst)

    def _alert(inst):
        calls["alert"].append(inst)
        if alert is not None:
            alert(inst)

    def _trim():
        calls["trim"].append(True)
        if trim is not None:
            trim()

    monkeypatch.setattr(instances, "instance_current", _instance_current)
    monkeypatch.setattr(sampler, "sample_once", _sample)
    monkeypatch.setattr(logstream, "tail_tick", _tail)
    monkeypatch.setattr(sampler, "alert_once", _alert)
    # runner 内部是 `from services import eventdb`（真 import），
    # 所以必须打在 eventdb 模块上，打在 sampler 命名空间上无效。
    from services import eventdb as _eventdb
    monkeypatch.setattr(_eventdb, "trim", _trim)
    return calls


@pytest.mark.anyio
async def test_runner_ticks_and_cancels(monkeypatch):
    """1 秒 tick：采样每轮跑，日志流约 1.5s、告警 60s、裁剪 1h。"""
    clock = _Clock(step=1.0, limit=5)
    _install_clock(monkeypatch, clock)
    calls = _install_work(monkeypatch)

    with pytest.raises(asyncio.CancelledError):
        await sampler.runner()

    assert clock.sleep_args == [1, 1, 1, 1, 1, 1]      # 每轮 sleep(1)
    assert len(calls["sample"]) == 5, "采样必须每轮都跑（节流在 monitor_sample 内）"
    assert len(calls["tail"]) >= 2, "日志流按 1.5s 节流，5 轮至少 2 次"
    assert len(calls["alert"]) == 1, "告警按 60s 节流，5 轮只该 1 次"
    assert len(calls["trim"]) == 1, "裁剪按 1h 节流，5 轮只该 1 次"


@pytest.mark.anyio
async def test_runner_rereads_instance_every_10s(monkeypatch):
    """当前行为（审计 C-P2-8 已修）：每 10 秒重读默认实例，不是永久缓存。"""
    clock = _Clock(step=1.0, limit=25)
    _install_clock(monkeypatch, clock)
    calls = _install_work(monkeypatch)

    with pytest.raises(asyncio.CancelledError):
        await sampler.runner()

    # 进循环前 1 次 + now=1001/1011/1021 各 1 次
    assert len(calls["inst"]) == 4, f"实例重读次数异常：{len(calls['inst'])}"
    # 采样拿到的实例随重读更新（证明没有永久缓存旧实例）
    sampled_ids = [c["id"] for c in calls["sample"] if c]
    assert len(set(sampled_ids)) >= 2, "25 轮内实例应至少被替换一次"


@pytest.mark.anyio
async def test_runner_instance_none_skips_work(monkeypatch):
    """读不到实例 → continue，本轮不做采样/告警（不拿 None 去调用）。"""
    clock = _Clock(step=1.0, limit=3)
    _install_clock(monkeypatch, clock)
    calls = _install_work(monkeypatch, inst_raises=True)

    with pytest.raises(asyncio.CancelledError):
        await sampler.runner()

    assert len(calls["inst"]) == 4, "实例为 None 时每轮都要重试读取"
    assert calls["sample"] == [] and calls["alert"] == [] and calls["trim"] == []


@pytest.mark.anyio
async def test_runner_survives_tick_exceptions(monkeypatch):
    """任一 tick 抛异常都不能打死 runner：记日志后继续下一轮。"""
    clock = _Clock(step=1.0, limit=4)
    _install_clock(monkeypatch, clock)

    def _boom(_inst):
        raise RuntimeError("sample exploded")

    calls = _install_work(monkeypatch, sample=_boom)

    with pytest.raises(asyncio.CancelledError):
        await sampler.runner()

    assert len(calls["sample"]) == 4, "采样异常后仍要继续跑满 4 轮"
    assert len(calls["tail"]) >= 1 and len(calls["alert"]) == 1


@pytest.mark.anyio
async def test_runner_survives_tail_and_alert_exceptions(monkeypatch):
    clock = _Clock(step=1.0, limit=4)
    _install_clock(monkeypatch, clock)

    def _boom(_inst):
        raise RuntimeError("boom")

    calls = _install_work(monkeypatch, tail=_boom, alert=_boom)

    with pytest.raises(asyncio.CancelledError):
        await sampler.runner()

    assert len(calls["sample"]) == 4
    assert len(calls["alert"]) == 1


@pytest.mark.anyio
async def test_runner_survives_trim_exception(monkeypatch):
    clock = _Clock(step=1.0, limit=3)
    _install_clock(monkeypatch, clock)

    def _boom():
        raise RuntimeError("trim boom")

    calls = _install_work(monkeypatch, trim=_boom)

    with pytest.raises(asyncio.CancelledError):
        await sampler.runner()

    assert len(calls["sample"]) == 3


@pytest.mark.anyio
async def test_runner_propagates_cancelled_error(monkeypatch):
    """CancelledError 必须穿透（否则 stop_runner 停不掉）。"""
    clock = _Clock(step=1.0, limit=0)      # 第一次 sleep 就抛
    _install_clock(monkeypatch, clock)
    _install_work(monkeypatch)

    with pytest.raises(asyncio.CancelledError):
        await sampler.runner()
    assert clock.sleeps == 1


# ================= start_runner / stop_runner =================

def test_start_runner_disabled_by_env(monkeypatch):
    monkeypatch.setattr(cfg, "EVENTS_BG", False)
    sampler.start_runner()
    assert sampler.runner_task() is None


def test_start_runner_outside_loop_is_noop(monkeypatch):
    """没有运行中的事件循环（例如同步上下文）→ 静默返回，不抛。"""
    monkeypatch.setattr(cfg, "EVENTS_BG", True)
    sampler.start_runner()
    assert sampler.runner_task() is None


@pytest.mark.anyio
async def test_start_runner_creates_task(monkeypatch):
    monkeypatch.setattr(cfg, "EVENTS_BG", True)

    async def _fake_runner():
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            raise

    monkeypatch.setattr(sampler, "runner", _fake_runner)
    sampler.start_runner()
    task = sampler.runner_task()
    assert task is not None and isinstance(task, asyncio.Task)
    assert not task.done()


@pytest.mark.anyio
async def test_start_runner_is_idempotent(monkeypatch):
    """重复调用不能起第二个 runner（否则采样/告警双跑）。"""
    monkeypatch.setattr(cfg, "EVENTS_BG", True)
    created = []

    async def _fake_runner():
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            raise

    monkeypatch.setattr(sampler, "runner", _fake_runner)
    sampler.start_runner()
    first = sampler.runner_task()
    sampler.start_runner()
    assert sampler.runner_task() is first
    created.append(first)
    assert len(created) == 1


@pytest.mark.anyio
async def test_stop_runner_cancels_and_clears(monkeypatch):
    monkeypatch.setattr(cfg, "EVENTS_BG", True)

    async def _fake_runner():
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            raise

    monkeypatch.setattr(sampler, "runner", _fake_runner)
    sampler.start_runner()
    task = sampler.runner_task()
    sampler.stop_runner()
    assert sampler.runner_task() is None
    assert task.cancelled() or task.cancelling()


def test_stop_runner_without_task_is_noop():
    sampler.stop_runner()
    assert sampler.runner_task() is None


@pytest.mark.anyio
async def test_runner_task_accessor(monkeypatch):
    assert sampler.runner_task() is None
    monkeypatch.setattr(cfg, "EVENTS_BG", True)

    async def _fake_runner():
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            raise

    monkeypatch.setattr(sampler, "runner", _fake_runner)
    sampler.start_runner()
    assert sampler.runner_task() is not None
