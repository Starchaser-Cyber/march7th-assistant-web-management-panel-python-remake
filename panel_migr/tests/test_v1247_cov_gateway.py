"""v1.24.7 优化（方向 B）：补齐 `gateway.py` 的测试盲区。

`gateway.forward()` 是 M1 绞杀者阶段的 PHP 回源链路，默认由
`config.FWD_ENABLE=0` 关闭，但它是**极端情况的回滚开关**（`M7A_FWD_ENABLE=1`
即可整体切回 PHP 转发）—— 一条平时不走、出事时才走的路，却没有测试，
优化前覆盖率仅 30%。

本文件覆盖：
  1. URL 拼接（path / 带 query / 不带 query）
  2. 请求头过滤（hop-by-hop 一律剔除、强制 `X-M7A-Fwd`、强制 `accept-encoding: identity`）
  3. 上游正常响应（状态码透传、content-encoding 剔除、hop-by-hop 剔除）
  4. 上游不可达 → 502 + JSON 错误体
  5. `trust_env=False`（内部转发不走环境代理）
  6. 各种 `httpx.HTTPError` 子类都归到 502
"""
from __future__ import annotations

import httpx
import pytest
from starlette.requests import Request

import config
from gateway import _HOP_BY_HOP, forward


def _req(method="GET", path="/index.php", query=b"", headers=None):
    """构造一个最小 Starlette Request（只填 forward() 用得到的字段）。"""
    raw = [(b"host", b"panel.local")]
    for k, v in (headers or {}).items():
        raw.append((k.lower().encode(), v.encode()))
    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "query_string": query,
        "headers": raw,
        "scheme": "http",
        "server": ("panel.local", 80),
        "client": ("127.0.0.1", 12345),
        "http_version": "1.1",
        "root_path": "",
    }

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    return Request(scope, receive)


class _FakeResp:
    def __init__(self, status_code=200, content=b"ok", headers=None):
        self.status_code = status_code
        self.content = content
        self.headers = httpx.Headers(headers or {})


class _FakeClient:
    """替身 AsyncClient：记录调用参数，返回预置响应或抛预置异常。"""

    last = None

    def __init__(self, resp=None, exc=None, **kwargs):
        self._resp = resp or _FakeResp()
        self._exc = exc
        self._kwargs = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def request(self, method, url, content=None, headers=None):
        type(self).last = {
            "method": method, "url": url, "content": content,
            "headers": headers, "kwargs": self._kwargs,
        }
        if self._exc is not None:
            raise self._exc
        return self._resp


@pytest.fixture()
def fake(monkeypatch):
    """把 gateway 里的 httpx.AsyncClient 换成替身，返回可配置的工厂。"""

    def install(resp=None, exc=None):
        _FakeClient.last = None
        monkeypatch.setattr(
            "gateway.httpx.AsyncClient",
            lambda **kw: _FakeClient(resp=resp, exc=exc, **kw),
        )
        return _FakeClient

    return install


# ===================== URL 拼接 =====================

@pytest.mark.asyncio
async def test_forward_url_without_query(fake):
    fc = fake()
    await forward(_req(path="/index.php"))
    assert fc.last["url"] == config.PHP_UPSTREAM + "/index.php"


@pytest.mark.asyncio
async def test_forward_url_keeps_query_string(fake):
    fc = fake()
    await forward(_req(path="/index.php", query=b"ajax=status&x=1"))
    assert fc.last["url"] == config.PHP_UPSTREAM + "/index.php?ajax=status&x=1"


@pytest.mark.asyncio
async def test_forward_url_nested_path(fake):
    fc = fake()
    await forward(_req(path="/a/b/c.php"))
    assert fc.last["url"].endswith("/a/b/c.php")


# ===================== 请求头处理 =====================

@pytest.mark.asyncio
async def test_forward_strips_all_hop_by_hop_headers(fake):
    fc = fake()
    headers = {h: "x" for h in _HOP_BY_HOP}
    headers["x-keep-me"] = "yes"
    await forward(_req(headers=headers))
    sent = {k.lower() for k in fc.last["headers"]}
    for h in _HOP_BY_HOP:
        assert h not in sent, f"{h} 不该转发给上游"
    assert "x-keep-me" in sent


@pytest.mark.asyncio
async def test_forward_sets_fwd_header(fake):
    fc = fake()
    await forward(_req())
    assert fc.last["headers"][config.FWD_HEADER] == config.FWD_HEADER_VALUE


@pytest.mark.asyncio
async def test_forward_forces_identity_encoding(fake):
    """上游若压缩，httpx 解压后 content-encoding 头会与 body 不符 → 浏览器白屏。"""
    fc = fake()
    await forward(_req(headers={"accept-encoding": "gzip, br"}))
    assert fc.last["headers"]["accept-encoding"] == "identity"


@pytest.mark.asyncio
async def test_forward_keeps_custom_headers(fake):
    fc = fake()
    await forward(_req(headers={"cookie": "PHPSESSID=abc", "x-real-ip": "1.2.3.4"}))
    sent = {k.lower(): v for k, v in fc.last["headers"].items()}
    assert sent["cookie"] == "PHPSESSID=abc"
    assert sent["x-real-ip"] == "1.2.3.4"


# ===================== 客户端构造 =====================

@pytest.mark.asyncio
async def test_forward_client_trust_env_false(fake):
    """内部转发不走环境代理，否则 localhost 会被 http_proxy 吞掉。"""
    fc = fake()
    await forward(_req())
    assert fc.last["kwargs"]["trust_env"] is False
    assert fc.last["kwargs"]["follow_redirects"] is False
    assert fc.last["kwargs"]["timeout"] == 60.0


@pytest.mark.asyncio
async def test_forward_passes_method_and_body(fake, monkeypatch):
    fc = fake()
    req = _req(method="POST")

    async def body():
        return b"payload=1"

    monkeypatch.setattr(req, "body", body)
    await forward(req)
    assert fc.last["method"] == "POST"
    assert fc.last["content"] == b"payload=1"


# ===================== 响应处理 =====================

@pytest.mark.asyncio
async def test_forward_passes_status_code(fake):
    fake(resp=_FakeResp(status_code=404, content=b"not found"))
    r = await forward(_req())
    assert r.status_code == 404
    assert r.body == b"not found"


@pytest.mark.asyncio
async def test_forward_strips_content_encoding_from_response(fake):
    fake(resp=_FakeResp(headers={"content-encoding": "gzip", "x-up": "1"}))
    r = await forward(_req())
    got = {k.lower() for k in r.headers}
    assert "content-encoding" not in got
    assert "x-up" in got


@pytest.mark.asyncio
async def test_forward_strips_hop_by_hop_from_response(fake):
    fake(resp=_FakeResp(headers={h: "x" for h in _HOP_BY_HOP} | {"x-ok": "1"}))
    r = await forward(_req())
    got = {k.lower() for k in r.headers}
    for h in _HOP_BY_HOP:
        if h == "content-length":
            continue          # Starlette Response 会按实际 body 重新生成，不算「透传」
        assert h not in got, f"{h} 不该透传给浏览器"
    assert "x-ok" in got


@pytest.mark.asyncio
async def test_forward_empty_body(fake):
    fake(resp=_FakeResp(status_code=204, content=b""))
    r = await forward(_req())
    assert r.status_code == 204
    assert r.body == b""


# ===================== 上游不可达 =====================

@pytest.mark.asyncio
@pytest.mark.parametrize("exc", [
    httpx.ConnectError("refused"),
    httpx.ConnectTimeout("timeout"),
    httpx.ReadTimeout("read timeout"),
    httpx.HTTPError("generic"),
])
async def test_forward_upstream_error_returns_502(fake, exc):
    fake(exc=exc)
    r = await forward(_req())
    assert r.status_code == 502
    assert b'"ok":false' in r.body or b'"ok": false' in r.body
    assert "PHP upstream 不可达" in r.body.decode("utf-8")


@pytest.mark.asyncio
async def test_forward_502_is_json(fake):
    fake(exc=httpx.ConnectError("refused"))
    r = await forward(_req())
    assert r.media_type == "application/json"


# ===================== 配置前提 =====================

def test_fwd_default_off():
    """默认 FWD_ENABLE=0：正常部署走 Python 渲染，转发只是回滚开关。"""
    import os

    if "M7A_FWD_ENABLE" not in os.environ:
        assert config.FWD_ENABLE is False


def test_hop_by_hop_set_is_lowercase():
    """过滤用的是 k.lower()，集合本身必须全小写，否则漏过滤。"""
    assert all(h == h.lower() for h in _HOP_BY_HOP)


def test_hop_by_hop_covers_rfc7230():
    for h in ("connection", "keep-alive", "transfer-encoding", "te",
              "trailer", "upgrade", "proxy-authenticate",
              "proxy-authorization"):
        assert h in _HOP_BY_HOP


def test_hop_by_hop_also_drops_host_and_length():
    """host 必须重算（上游是 PHP 站点不是面板）；content-length 由 httpx 重算。"""
    assert "host" in _HOP_BY_HOP
    assert "content-length" in _HOP_BY_HOP
