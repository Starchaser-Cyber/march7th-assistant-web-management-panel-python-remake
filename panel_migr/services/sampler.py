"""进程内采样/巡检/日志流 runner（M4）——面板常驻后台循环。

定位：给「没人开页面、cron 又被 M6 改道」的场景兜底，同时让 WS 订阅者
不依赖前端轮询也能持续收到 monitor/log 事件。与 PHP cron 端点并存期间：
- 采样：monitor_sample 内部按 iv 节流，两侧共享 lastSample 字段，谁先谁采，不会重复；
- 告警：alert_tick 有状态机去重（alerted_down/aborted_seen），重复调用安全；
- 心跳：**不碰** heartbeat_* 文件——那是「外部 cron 活着」的体检依据，留给 cron。
"""
from __future__ import annotations

import asyncio
import logging
import time

import anyio.to_thread

import config

log = logging.getLogger("m7a.sampler")

_runner_task: asyncio.Task | None = None


def sample_iv() -> int:
    """保底采样间隔：跟随面板配置，但不低于 SAMPLE_IV 秒。"""
    from services.monitor import monitor_interval
    try:
        cfg_iv = monitor_interval()
    except Exception:
        cfg_iv = 1
    return max(cfg_iv, int(config.SAMPLE_IV))


def sample_once(inst: dict) -> dict:
    from services.monitor import monitor_sample
    return monitor_sample(inst, sample_iv())


def alert_once(inst: dict) -> dict:
    from services.alerts import alert_tick
    return alert_tick(inst, force=True)


async def runner() -> None:
    """1 秒 tick：采样（节流在 monitor_sample 内）/ 日志流 1.5s / 告警 60s / 裁剪 1h。"""
    from services.instances import instance_current
    from services.logstream import tail_tick

    try:
        inst = instance_current({})
    except Exception:
        inst = None

    next_log = 0.0
    next_alert = 0.0
    next_trim = 0.0
    while True:
        try:
            await asyncio.sleep(1)
            if inst is None:
                try:
                    inst = instance_current({})
                except Exception:
                    continue
            now = time.monotonic()
            try:
                await anyio.to_thread.run_sync(sample_once, inst)
            except Exception:
                log.exception("sample_once failed")
            if now >= next_log:
                next_log = now + 1.5
                try:
                    await anyio.to_thread.run_sync(tail_tick, inst)
                except Exception:
                    log.exception("log tail failed")
            if now >= next_alert:
                next_alert = now + 60
                try:
                    await anyio.to_thread.run_sync(alert_once, inst)
                except Exception:
                    log.exception("alert_tick failed")
            if now >= next_trim:
                next_trim = now + 3600
                try:
                    from services import eventdb
                    await anyio.to_thread.run_sync(eventdb.trim)
                except Exception:
                    log.exception("eventdb trim failed")
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("runner tick failed")


def start_runner() -> None:
    global _runner_task
    if not config.EVENTS_BG or _runner_task is not None:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    _runner_task = loop.create_task(runner())


def stop_runner() -> None:
    global _runner_task
    if _runner_task is not None:
        _runner_task.cancel()
        _runner_task = None


def runner_task() -> asyncio.Task | None:
    return _runner_task
