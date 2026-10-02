"""进程内事件总线（M4）——WS 断线补发的数据源。

publish() 任意线程可调：写内存环形缓冲（BUFFER 条，供 since 补发）→
状态机事件（alert/history/system）同步落 SQLite → 唤醒等待中的 WS 发送循环。

seq 为进程内单调递增，重启归零：客户端 since > current_seq 时判为过期，
WS 侧回 resync（客户端转 REST 拉全量快照），不尝试跨重启补发。
"""
from __future__ import annotations

import json
import threading
import time
from collections import deque

from services import eventdb

BUFFER = 512
_PERSISTED = {"alert", "history", "system"}

_cond = threading.Condition()
_buffer: deque[dict] = deque(maxlen=BUFFER)
_seq = 0


def publish(etype: str, payload: dict, inst: str = "") -> int:
    """发布事件；返回 seq。失败不抛（事件化不阻塞业务主流程）。"""
    global _seq
    with _cond:
        _seq += 1
        evt = {"seq": _seq, "ts": int(time.time()), "type": etype,
               "payload": payload if isinstance(payload, dict) else {}}
        _buffer.append(evt)
        _cond.notify_all()
    if etype in _PERSISTED:
        eventdb.insert_event(evt["ts"], etype, inst, evt["payload"])
    return _seq


def current_seq() -> int:
    with _cond:
        return _seq


def events_since(since: int) -> list[dict] | None:
    """since 之后的事件；缓冲不足（有洞）返回 None 表示需要 resync。

    语义：since = 客户端已收到的最大 seq（0 表示尚未收到任何事件）。
    """
    with _cond:
        if since >= _seq:
            return []
        if not _buffer:
            return None                      # 有新事件但缓冲空 → 不可能补
        if since < _buffer[0]["seq"] - 1:
            return None                      # since 之后的最早事件已被挤出 → 有洞
        return [e for e in _buffer if e["seq"] > since]


def wait_new(since: int, timeout: float) -> tuple[list[dict] | None, bool]:
    """阻塞等待 seq>since 的事件；返回 (事件列表或 None=需resync, 是否超时)。

    由 WS 发送循环在线程池中调用（asyncio.to_thread）。
    """
    with _cond:
        _cond.wait_for(lambda: _seq > since, timeout=timeout)
        rows = events_since(since)
        timed_out = _seq <= since
    return rows, timed_out


def reset_state() -> None:
    """测试用：清缓冲、seq 归零、断开 SQLite 连接。"""
    global _seq
    with _cond:
        _buffer.clear()
        _seq = 0
        _cond.notify_all()
    eventdb.reset()


def stats() -> dict:
    with _cond:
        return {"seq": _seq, "buffered": len(_buffer)}


def dump_last(n: int = 1) -> list[dict]:
    """测试辅助：缓冲末尾 n 条。"""
    with _cond:
        return list(_buffer)[-n:]
