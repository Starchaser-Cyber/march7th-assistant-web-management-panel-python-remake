"""M3 预览路由：/m7a-preview/ HTTP/WS 反代 + ?ajax=preview_token + ws264 极致档。

main.py catch_all/ws_proxy 识别 m7a-preview 前缀后转入本模块；
HTTP 语义（health / stream MJPEG / ws token 校验）全部由容器内 preview_server
原样决定，本模块只做动态寻址、自愈与字节透传。
"""
from __future__ import annotations

import asyncio
import json

import httpx
from fastapi.responses import JSONResponse, Response, StreamingResponse

import config
from services import preview as pv
from services.instances import instance_current, instance_container

# 浏览器与 preview_server 之间的 hop-by-hop 头（不透传）
_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "content-length", "host",
    "content-encoding",
}

# ajax 注册表项：GET ?ajax=preview_token（与 PHP preview_token() 分支对齐）
PREVIEW_GET_HANDLERS = {"preview_token": lambda request: pv.preview_token()}


def _container(request_or_ws) -> str:
    session = getattr(request_or_ws.state, "session", None)
    return instance_container(instance_current(session or {}))


def _upstream_path(path: str) -> str:
    """nginx /m7a-preview/ location 剥前缀后的上游路径（/m7a-preview/health → /health）。"""
    p = path[len("/m7a-preview"):] if path.startswith("/m7a-preview") else path
    return p or "/"


def _upstream_url(base: str, request) -> str:
    url = base + _upstream_path(request.url.path)
    if request.url.query:
        url += "?" + request.url.query
    return url


async def proxy_http(request) -> Response:
    """GET /m7a-preview/* → 容器 preview_server 透传（支持 MJPEG 长流）。"""
    container = _container(request)
    base, err = await pv.get_upstream(container)
    if base is None:
        return JSONResponse({"ok": False, "msg": err}, status_code=503)

    url = _upstream_url(base, request)
    timeout = httpx.Timeout(connect=3.0, read=300.0, write=10.0, pool=3.0)
    try:
        client = httpx.AsyncClient(timeout=timeout, trust_env=False)
        upstream_req = client.build_request("GET", url)
        r = await client.send(upstream_req, stream=True)
    except httpx.HTTPError as e:
        return JSONResponse(
            {"ok": False, "msg": f"preview_server 连接失败：{e}"}, status_code=502
        )

    headers = {
        k: v for k, v in r.headers.items()
        if k.lower() not in _HOP and k.lower() != "content-type"
    }
    if "content-type" in r.headers:
        headers["content-type"] = r.headers["content-type"]

    async def body():
        try:
            async for chunk in r.aiter_raw():
                yield chunk
        finally:
            await r.aclose()
            await client.aclose()

    return StreamingResponse(
        body(), status_code=r.status_code, headers=headers, media_type=None
    )


# ===== WebSocket 反代（/m7a-preview/ws，token 由上游 preview_server 校验）=====

async def ws_preview(websocket) -> None:
    """main.py 已 accept 并加载 session；透传到容器 preview_server /ws。"""
    container = _container(websocket)
    base, err = await pv.get_upstream(container)
    if base is None:
        await _close(websocket, 1013, err)
        return

    ws_url = base.replace("http://", "ws://", 1) + _upstream_path(websocket.url.path)
    if websocket.url.query:
        ws_url += "?" + websocket.url.query
    await _relay(websocket, ws_url)


async def ws_h264(websocket) -> None:
    """H.264 极致档：上游 JPEG 帧 → ffmpeg → fMP4 → 浏览器（MSE 播放）。

    本地先做 token 校验（省一次无效上游连接），失败按 close code 1008 拒绝。
    """
    if not pv.ffmpeg_available():
        await _close(websocket, 1013, "服务器未安装 ffmpeg，H.264 档不可用")
        return

    secret = pv.preview_secret()
    token = websocket.url.query.split("token=")[-1].split("&")[0] if "token=" in websocket.url.query else ""
    if not secret or not token or token != pv.day_token(secret):
        await _close(websocket, 1008, "invalid token")
        return

    container = _container(websocket)
    base, err = await pv.get_upstream(container)
    if base is None:
        await _close(websocket, 1013, err)
        return

    ws_url = base.replace("http://", "ws://", 1) + "/ws"
    keep = [k for k in ("token", "res", "quality") if k + "=" in websocket.url.query]
    if keep:
        # 保留 key=value 形式拼接（丢键名会让上游 parse_qs 取不到 token → 403）
        qs = "&".join(
            k + "=" + websocket.url.query.split(k + "=", 1)[1].split("&")[0]
            for k in keep
        )
        ws_url += "?" + qs

    try:
        import websockets

        upstream = await websockets.connect(ws_url, open_timeout=5, max_size=None)
    except Exception as e:
        await _close(websocket, 1013, f"上游连接失败：{e}")
        return

    proc = await asyncio.create_subprocess_exec(
        config.FFMPEG_BIN, *pv.FFMPEG_ARGS,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    tasks = [
        asyncio.create_task(_jpeg_to_ffmpeg(upstream, proc)),
        asyncio.create_task(_fmp4_to_client(proc, websocket)),
        asyncio.create_task(_watch_client(websocket)),
    ]
    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()
        for t in pending:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
    finally:
        for t in tasks:
            if not t.done():
                t.cancel()
        try:
            await upstream.close()
        except Exception:
            pass
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        await _close(websocket, 1000, "")


async def _jpeg_to_ffmpeg(upstream, proc) -> None:
    """上游 preview_server JPEG 帧 → ffmpeg stdin；上游断开 → 关 stdin 结束编码。"""
    try:
        async for msg in upstream:
            if isinstance(msg, (bytes, bytearray)) and proc.stdin:
                proc.stdin.write(msg)
                await proc.stdin.drain()
    except Exception:
        pass
    finally:
        try:
            if proc.stdin:
                proc.stdin.close()
        except Exception:
            pass


async def _fmp4_to_client(proc, websocket) -> None:
    """ffmpeg fMP4 输出 → 浏览器 WebSocket；EOF/异常结束（触发整体清理）。"""
    while True:
        chunk = await proc.stdout.read(65536)
        if not chunk:
            break
        await websocket.send_bytes(chunk)


async def _watch_client(websocket) -> None:
    """客户端断开检测：收到 disconnect 即结束（触发整体清理）。"""
    from fastapi import WebSocketDisconnect

    try:
        while True:
            await websocket.receive()
    except WebSocketDisconnect:
        pass
    except Exception:
        pass


async def _relay(websocket, ws_url: str) -> None:
    """双向字节泵：浏览器 WebSocket ↔ 容器 preview_server。"""
    from starlette.websockets import WebSocketState

    try:
        import websockets

        async with websockets.connect(ws_url, open_timeout=5, max_size=None) as upstream:

            async def up_to_down():
                async for msg in upstream:
                    if isinstance(msg, str):
                        await websocket.send_text(msg)
                    else:
                        await websocket.send_bytes(msg)

            async def down_to_up():
                while True:
                    m = await websocket.receive()
                    if m["type"] == "websocket.disconnect":
                        return
                    if "bytes" in m and m["bytes"] is not None:
                        await upstream.send(m["bytes"])
                    elif "text" in m and m["text"] is not None:
                        await upstream.send(m["text"])

            tasks = [
                asyncio.create_task(up_to_down()),
                asyncio.create_task(down_to_up()),
            ]
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in tasks:
                t.cancel()
    except Exception as e:
        await _close(websocket, 1011, f"上游连接失败：{e}")


async def _close(websocket, code: int, reason: str) -> None:
    from starlette.websockets import WebSocketState

    try:
        if websocket.client_state == WebSocketState.CONNECTED:
            if reason and code != 1000:
                try:
                    await websocket.send_text(json.dumps({"error": reason}, ensure_ascii=False))
                except Exception:
                    pass
            await websocket.close(code=code, reason=reason[:120] or None)
    except Exception:
        pass
