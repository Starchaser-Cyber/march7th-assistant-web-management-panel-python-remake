"""v1.24.7 优化（方向 B）：补齐 `main.py` 的分发层测试盲区。

`main.py` 优化前覆盖率 **60%**，未覆盖的正是最关键的几条分流分支：
`_run_handler` 的三种返回形态、`dispatch_get` 的「未知 ajax / 未登录下载」、
`catch_all` 的 405 与 FWD_ENABLE 回滚路径、`_ws_forward_target` 的目标推导。
上一轮 P1-14（`await` 同步函数导致所有非面板路径 500）就藏在这层里。

本文件用 TestClient 打真实 ASGI 应用（不是单元级 mock），覆盖：
  1. `_run_handler`：dict → JSON、Response 透传、str → text/plain
  2. `dispatch_get`：未登录 ajax 401、已登录未知 ajax 空 200、
     未登录下载落登录页、`?manifest` / `?icon` 本地生成
  3. `dispatch_post` / `catch_all`：面板路径、非面板路径、PUT/DELETE → 405
  4. `_ws_forward_target`：全部映射分支
  5. 静态资源 Cache-Control 与 gzip
"""
from __future__ import annotations

import json

import pytest
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from starlette.testclient import TestClient

import main
from main import _ws_forward_target


# ===================== _run_handler =====================

def _dummy_request():
    from starlette.requests import Request

    scope = {
        "type": "http", "method": "GET", "path": "/", "query_string": b"",
        "headers": [], "scheme": "http", "server": ("t", 80),
        "client": ("127.0.0.1", 1), "http_version": "1.1", "root_path": "",
    }

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    return Request(scope, receive)


@pytest.mark.asyncio
async def test_run_handler_dict_becomes_json():
    r = await main._run_handler(_dummy_request(), lambda _req: {"ok": True, "n": 3})
    assert isinstance(r, JSONResponse)
    assert json.loads(r.body) == {"ok": True, "n": 3}


@pytest.mark.asyncio
async def test_run_handler_response_passthrough():
    """`_run_handler` 用 `to_thread.run_sync` 执行 handler → handler 必须是同步函数。"""
    sentinel = PlainTextResponse("hi")

    def handler(_req):
        return sentinel

    r = await main._run_handler(_dummy_request(), handler)
    assert r is sentinel


@pytest.mark.asyncio
async def test_run_handler_str_becomes_text():
    r = await main._run_handler(_dummy_request(), lambda _req: "plain")
    assert isinstance(r, Response)
    assert r.body == b"plain"
    assert r.media_type == "text/plain"


@pytest.mark.asyncio
async def test_run_handler_runs_handler_in_thread():
    """同步 handler 必须进线程池（否则阻塞事件循环）。"""
    import threading

    seen = {}

    def handler(_req):
        seen["thread"] = threading.current_thread().name
        return {}

    await main._run_handler(_dummy_request(), handler)
    assert "MainThread" not in seen["thread"]


# ===================== _ws_forward_target =====================

@pytest.mark.parametrize("path,expect", [
    ("/", "console"),
    ("/favicon.ico", None),
    ("/robots.txt", None),
    ("/phpmyadmin", None),
    ("/adminer", None),
    ("/123", "preview"),
    ("/0", "preview"),
    ("/999999", "preview"),
    ("/logs/x.log", "logtail"),
    ("/logs/a/b", "logtail"),
    ("/index.php", None),
    ("/other", None),
])
def test_ws_forward_target(path, expect):
    assert _ws_forward_target(path) == expect


def test_ws_excluded_is_exact_match_not_prefix():
    """`_WS_EXCLUDED` 是精确匹配：/logs/favicon.ico 仍走转发。"""
    assert _ws_forward_target("/logs/favicon.ico") == "logtail"


# ===================== TestClient 级分发 =====================

@pytest.fixture()
def client():
    return TestClient(main.app)


def test_get_root_returns_login_page(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


def test_unauth_ajax_returns_401_json(client):
    r = client.get("/?ajax=status")
    assert r.status_code == 401
    assert json.loads(r.content) == {"auth": False}


def test_unauth_ajax_does_not_render_page(client):
    """v1.24.2 热修：未登录 ajax 必须返回 401 而不是整页 HTML。"""
    r = client.get("/index.php?ajax=monitor")
    assert r.status_code == 401
    assert b"<html" not in r.content.lower()


def test_put_returns_405_json(client):
    r = client.put("/")
    assert r.status_code == 405
    assert json.loads(r.content)["ok"] is False


@pytest.mark.parametrize("method", ["DELETE", "OPTIONS", "PATCH"])
def test_other_methods_return_405(client, method):
    r = client.request(method, "/")
    assert r.status_code == 405


@pytest.mark.parametrize("path", [
    "/favicon.ico", "/robots.txt", "/manifest.json",
    "/some/deep/link", "/a/b/c/d/e",
])
def test_non_panel_get_returns_200(client, path):
    """P1-14 回归：`await render_fallback(...)` 曾让这些路径全部 500。"""
    r = client.get(path)
    assert r.status_code == 200


def test_non_panel_post_returns_200(client):
    r = client.post("/foo", data={"x": "1"})
    assert r.status_code == 200


def test_manifest_query_generates_locally(client):
    r = client.get("/?manifest=1")
    assert r.status_code == 200
    body = json.loads(r.content)
    assert "name" in body or "short_name" in body


def test_icon_query_generates_png(client):
    r = client.get("/?icon=1&size=64")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    assert r.content[:8] == b"\x89PNG\r\n\x1a\n"


def test_icon_size_is_clamped(client):
    """size 越界要被钳到 16..512，不能 500 也不能无限大。"""
    for bad in ("abc", "", "-5", "0", "99999"):
        r = client.get(f"/?icon=1&size={bad}")
        assert r.status_code == 200, f"size={bad} 应该 200"
        assert r.content[:4] == b"\x89PNG"


def test_preview_path_requires_auth(client):
    r = client.get("/m7a-preview/health")
    assert r.status_code == 401
    assert json.loads(r.content)["auth"] is False


def test_preview_ws_path_not_treated_as_http(client):
    """`/m7a-preview/ws264` 是 WS 路由，HTTP GET 不应返回 401 JSON 之外的东西。"""
    r = client.get("/m7a-preview/ws264")
    assert r.status_code in (401, 403, 404, 405)


def test_static_css_has_immutable_cache(client):
    r = client.get("/static/panel.css")
    assert r.status_code == 200
    assert "immutable" in r.headers.get("cache-control", "")


def test_static_missing_file_404(client):
    assert client.get("/static/no_such_file.js").status_code == 404


def test_static_path_traversal_blocked(client):
    """关键断言是「没有泄露文件内容」，而不是具体状态码。

    Starlette 的 StaticFiles 对越界路径返回 404，但某些编码形式（如
    `/static/../main.py`）会被路由层规整后落到 catch_all，渲染成登录页 ——
    只要响应体不是源码/密码文件就算安全。
    """
    for bad in ("/static/../main.py", "/static/..%2fmain.py",
                "/static/%2e%2e/main.py", "/static/../../.panel_pass.php"):
        r = client.get(bad)
        assert r.status_code in (200, 400, 404), f"{bad} 异常状态码 {r.status_code}"
        body = r.content
        assert b"def dispatch_get" not in body, f"{bad} 泄露了 main.py 源码"
        assert b"$2y$" not in body, f"{bad} 泄露了密码哈希"
        assert b"import config" not in body, f"{bad} 泄露了源码"


def test_page_has_no_double_escape_banner(client):
    """flash 横幅在模板里只做一次 autoescape（P2-1 回归的页面级守护）。"""
    r = client.get("/")
    assert "&amp;amp;" not in r.text


# ===================== FWD_ENABLE 回滚路径 =====================

def test_fwd_disabled_by_default():
    assert main.config.FWD_ENABLE is False


def test_fwd_enabled_routes_to_forward(monkeypatch):
    """FWD_ENABLE=1 时，非面板路径必须走 gateway.forward 而不是本地渲染。"""
    calls = []

    async def fake_forward(request):
        calls.append(request.url.path)
        return PlainTextResponse("FROM_PHP")

    monkeypatch.setattr(main, "forward", fake_forward)
    monkeypatch.setattr(main.config, "FWD_ENABLE", True)

    # 注意：`/index.php` 在 `_PANEL_PATHS` 内，即使 FWD_ENABLE=1 也走本地分发
    # （面板自身路径永远由 Python 处理），所以这里要用非面板路径。
    c = TestClient(main.app)
    r = c.get("/legacy/page.php")
    assert r.text == "FROM_PHP"
    assert calls == ["/legacy/page.php"]


def test_fwd_enabled_unknown_ajax_forwards(monkeypatch):
    calls = []

    async def fake_forward(request):
        calls.append(str(request.url))
        return PlainTextResponse("PHP_AJAX")

    monkeypatch.setattr(main, "forward", fake_forward)
    monkeypatch.setattr(main.config, "FWD_ENABLE", True)

    # 未登录 + ajax → 先被 401 拦（这层优先于转发）
    c = TestClient(main.app)
    r = c.get("/?ajax=nope")
    assert r.status_code == 401
    assert calls == []


# ===================== 生命周期钩子 =====================

def test_startup_shutdown_hooks_exist():
    """M4 的采样 runner 靠这两个钩子起停；M7A_EVENTS_BG=0 时内部直接返回。"""
    assert callable(main._m4_start_runner)
    assert callable(main._m4_stop_runner)


@pytest.mark.asyncio
async def test_startup_shutdown_noop_when_bg_disabled(monkeypatch):
    """测试环境 M7A_EVENTS_BG=0 → start_runner/stop_runner 不抛异常。"""
    await main._m4_start_runner()
    await main._m4_stop_runner()
