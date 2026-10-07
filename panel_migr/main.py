"""m7a_panel 迁移版入口。

HTTP 部分按 path 分流到各 handler（不依赖任何 Web 框架路由），
便于机械对照 index.php 的 GET/POST 分支。未迁移部分原样转发回 PHP。
M2 起：POST 全量（写操作）+ 写型 GET/key 回调由 Python 处理。
"""
from __future__ import annotations

import re

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, Response
from starlette.websockets import WebSocketState

import config
from gateway import forward
from phpsess import is_auth, load_session
from routers import GET_HANDLERS, SPECIAL_GETS
from routers.panel import render_fallback
from routers.write import handle_post

app = FastAPI(title="m7a_panel (python)", version=config.PANEL_VERSION)

# M5-B：面板静态资产由 Python 直接服务（须注册在 catch_all 通配之前）
from pathlib import Path as _Path

from starlette.staticfiles import StaticFiles as _StaticFiles

app.mount(
    "/static",
    _StaticFiles(directory=_Path(__file__).resolve().parent / "static"),
    name="static",
)


@app.on_event("startup")
async def _m4_start_runner() -> None:
    # M4：进程内采样/日志流/告警 runner（M7A_EVENTS_BG=0 关闭；测试默认关闭）
    from services.sampler import start_runner

    start_runner()


@app.on_event("shutdown")
async def _m4_stop_runner() -> None:
    from services.sampler import stop_runner

    stop_runner()

# 这些 path_info 不做 WebSocket 代理（M3 再统一梳理）
_WS_EXCLUDED = {"phpmyadmin", "adminer", "favicon.ico", "robots.txt"}
_PANEL_PATHS = ("", "/", "/action", "/index.php")


async def _run_handler(request: Request, handler) -> Response:
    """同步处理器在线程池执行；返回 dict 时自动包 JSON。"""
    import anyio.to_thread as to_thread

    result = await to_thread.run_sync(handler, request)
    if isinstance(result, dict):
        return JSONResponse(result)
    if isinstance(result, Response):
        return result
    return Response(content=str(result), media_type="text/plain")


async def dispatch_get(request: Request) -> Response:
    """GET 分发，顺序对齐 index.php：authed+ajax → key 回调 → 下载 → 页面。"""
    qp = request.query_params
    authed = is_auth(request.state.session)

    # 1) 登录态 + ajax（对齐 PHP `isset($_GET['ajax']) && is_auth()`）
    if authed and qp.get("ajax"):
        handler = GET_HANDLERS.get(qp["ajax"])
        if handler is None:
            # 未知 ajax：旧行为是回源（PHP 对未知 ajax 是 exit 空响应）。
            # v1.22 M6：Python 为唯一后端，本地直接返回空响应（与旧站点行为一致）。
            if config.FWD_ENABLE:
                return await forward(request)
            return Response(content="", media_type="text/plain")
        return await _run_handler(request, handler)

    # 2) key 回调（免登录）与下载（需登录）
    for param, expected, need_auth, handler in SPECIAL_GETS:
        if qp.get(param) == expected:
            if need_auth and not authed:
                break  # 未登录的下载请求 → 渲染登录页
            return await _run_handler(request, handler)

    # 3) 页面（Python/Jinja 渲染）
    # PWA 附属资源：v1.22 起由 Python 本地生成，不再回源（回滚时仍走转发）
    if "manifest" in qp or "icon" in qp:
        if config.FWD_ENABLE:
            return await forward(request)
        from routers.panel import icon_response, manifest_response

        return icon_response(request) if "icon" in qp else manifest_response(request)

    from services.pagectx import page_response

    return page_response(request)


async def dispatch_post(request: Request) -> Response:
    """POST：action 分发（认证/CSRF/危险静默前置见 routers.write.handle_post）。"""
    return await handle_post(request)


@app.api_route(
    "/{full_path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "OPTIONS", "PATCH"],
    include_in_schema=False,
)
async def catch_all(request: Request, full_path: str):
    request.state.session = load_session(request)
    path = "/" + full_path
    if request.method == "POST":
        if path in _PANEL_PATHS:
            return await dispatch_post(request)
        if config.FWD_ENABLE:
            return await forward(request)
        return await render_fallback(request)
    if request.method == "GET":
        if full_path.startswith("m7a-preview/"):
            # M3：/m7a-preview/* 反代容器内 preview_server（动态 IP + 自愈）
            from routers.preview import proxy_http

            return await proxy_http(request)
        if path in _PANEL_PATHS:
            return await dispatch_get(request)
        if config.FWD_ENABLE:
            return await forward(request)
        return await render_fallback(request)
    # PUT/DELETE/OPTIONS/PATCH：面板不使用；回滚时转发，否则本地 405
    if config.FWD_ENABLE:
        return await forward(request)
    return JSONResponse({"ok": False, "msg": "不支持的请求方法"}, status_code=405)


# ===== WebSocket：M1 阶段原样转发回 PHP（M3 替换）=====


def _ws_forward_target(path_info: str):
    p = path_info.lstrip("/")
    if p in _WS_EXCLUDED:
        return None
    if re.fullmatch(r"[0-9]+", p):
        return "preview"
    if p == "":
        return "console"
    if re.match(r"^logs/", p):
        return "logtail"
    return None


@app.websocket("/{full_path:path}")
async def ws_proxy(websocket: WebSocket, full_path: str):
    await websocket.accept()
    if full_path == "m7a-events":
        # M4：事件流（monitor/log/history/alert 推送 + since 断线补发）
        from routers.events import ws_events

        await ws_events(websocket)
        return
    if full_path.startswith("m7a-preview/"):
        # M3：预览 WebSocket 由面板反代容器 preview_server（token 由上游校验）
        from routers.preview import ws_preview, ws_h264

        websocket.state.session = load_session(websocket)
        if full_path == "m7a-preview/ws264":
            await ws_h264(websocket)
        else:
            await ws_preview(websocket)
        return
    target = _ws_forward_target("/" + full_path)
    if target is None:
        await websocket.close(code=1008)
        return
    url = config.PHP_UPSTREAM.replace("http://", "ws://", 1).replace(
        "https://", "wss://", 1
    )
    url += f"?ws={target}"
    if websocket.url.query:
        url += "&" + websocket.url.query
    try:
        import websockets

        headers = []
        if "cookie" in websocket.headers:
            headers.append(("cookie", websocket.headers["cookie"]))
        try:
            async with websockets.connect(
                url, additional_headers=headers, open_timeout=15, max_size=None
            ) as upstream:
                async def _client_to_upstream():
                    try:
                        while True:
                            data = await websocket.receive_bytes()
                            await upstream.send(data)
                    except WebSocketDisconnect:
                        pass
                    except Exception:
                        pass

                async def _upstream_to_client():
                    try:
                        async for message in upstream:
                            if isinstance(message, str):
                                await websocket.send_text(message)
                            else:
                                await websocket.send_bytes(message)
                    except Exception:
                        pass

                import anyio

                async with anyio.create_task_group() as tg:
                    tg.start_soon(_client_to_upstream)
                    tg.start_soon(_upstream_to_client)
        except Exception:
            if websocket.client_state == WebSocketState.CONNECTED:
                await websocket.close(code=1011)
    except WebSocketDisconnect:
        pass
    except Exception:
        if websocket.client_state == WebSocketState.CONNECTED:
            await websocket.close(code=1011)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=config.LISTEN_HOST, port=config.LISTEN_PORT)
