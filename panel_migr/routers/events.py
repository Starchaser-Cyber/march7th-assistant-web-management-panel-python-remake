"""M4 事件化：WS 事件流端点 + 告警历史查询。

- WS /m7a-events：登录态校验 → hello(seq) → since 补发（有洞回 resync）→
  持续推送（monitor/log/history/alert）+ 15 秒 ping 保活（防 nginx 超时）。
- GET ?ajax=alert_history：读 SQLite events 表 type=alert（状态机事件留档）。
"""
from __future__ import annotations

import asyncio
import json
import time

from fastapi import WebSocket
from starlette.websockets import WebSocketState

from phpsess import is_auth, load_session
from services import eventdb, events
from services.instances import instance_current


def h_alert_history(request) -> dict:
    """GET ?ajax=alert_history → 告警事件（最新在前，limit 1..200）。

    v1.22：新增可选筛选 kind（down/recovered/aborted）、from/to（unix 秒区间）。
    无参数时行为与旧版一致（向后兼容）。
    """
    qp = request.query_params

    def _int(name, default=None):
        v = qp.get(name)
        if v is None or str(v).strip() == "":
            return default
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return default

    limit = _int("limit", 50)
    limit = max(1, min(200, limit or 50))
    kind = (qp.get("kind") or "").strip() or None
    frm = _int("from")
    to = _int("to")
    return {"ok": True,
            "items": eventdb.list_events("alert", limit, kind=kind, frm=frm, to=to)}


EVENTS_GET_HANDLERS = {"alert_history": h_alert_history}


async def ws_events(websocket: WebSocket) -> None:
    """事件流 WS：未登录 1008；hello 之后先补 since，再进入推送循环。"""
    sess = load_session(websocket)
    if not is_auth(sess):
        await websocket.close(code=1008, reason="unauthorized")
        return
    inst = instance_current(sess)
    try:
        since = int(websocket.query_params.get("since") or 0)
    except (TypeError, ValueError):
        since = 0

    async def _send(obj: dict) -> None:
        await websocket.send_text(json.dumps(obj, ensure_ascii=False))

    cur = events.current_seq()
    await _send({
        "type": "hello", "seq": cur, "ts": int(time.time()),
        "inst": str(inst.get("id") or inst.get("container") or ""),
    })
    last = cur
    if since > 0:
        rows = events.events_since(since)
        if rows is None:
            await _send({"type": "resync", "seq": cur})
        else:
            for e in rows:
                await _send(e)
                last = e["seq"]

    async def _sender() -> None:
        nonlocal last
        while True:
            rows2, timed_out = await asyncio.to_thread(events.wait_new, last, 15.0)
            if rows2 is None:
                cur2 = events.current_seq()
                await _send({"type": "resync", "seq": cur2})
                last = cur2
                continue
            for e in rows2:
                await _send(e)
                last = e["seq"]
            if timed_out and not rows2:
                await _send({"type": "ping", "seq": last})

    async def _receiver() -> None:
        # 只为感知断开；客户端发来的任何消息都忽略
        while True:
            msg = await websocket.receive()
            if msg.get("type") == "websocket.disconnect":
                return

    sender = asyncio.create_task(_sender())
    receiver = asyncio.create_task(_receiver())
    try:
        await asyncio.wait({sender, receiver},
                           return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in (sender, receiver):
            if not t.done():
                t.cancel()
        if websocket.client_state == WebSocketState.CONNECTED:
            try:
                await websocket.close(code=1000)
            except Exception:
                pass
