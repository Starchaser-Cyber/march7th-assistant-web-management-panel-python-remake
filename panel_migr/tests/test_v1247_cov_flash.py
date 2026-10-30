"""V1.24.7 覆盖率补测：`services/flash.py`（写操作页面级回执）。

重点回归 v1.24.4 的「双重转义」：`write.py` 6 处 handler 曾先 `h()` 一次、
`flash._banner()` 里又 `h()` 一次 → 实例名 `Test & Demo` 显示成
`Test &amp; Demo`。约定是：**调用方传原始值，`_banner` 统一转义一次**。
本文件把「单次转义」钉死在 `_banner` 这一层。

全部离线：`fetch_php_page` / `page_response` 用 monkeypatch 替身，不联网。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from services import flash
from services.configops import h


# ================= _banner：单次转义（核心回归）=================

def test_banner_escapes_amp_once():
    """`&` 只能变成 `&amp;`，绝不能 `&amp;amp;`。"""
    b = flash._banner(msg="Test & Demo")
    assert "Test &amp; Demo" in b
    assert "&amp;amp;" not in b, f"双重转义：{b}"


def test_banner_escapes_all_special_chars_once():
    raw = '<script>"x" & \'y\'</script>'
    b = flash._banner(msg=raw)
    # 单次转义后的形态
    assert "&lt;script&gt;" in b
    assert "&quot;" in b
    assert "&#x27;" in b
    assert "&amp;" in b
    # 不得出现任何二次转义痕迹
    for bad in ("&amp;lt;", "&amp;gt;", "&amp;quot;", "&amp;amp;", "&amp;#x27;"):
        assert bad not in b, f"双重转义痕迹 {bad}：{b}"


def test_banner_msg_and_err_both():
    b = flash._banner(msg="ok-msg", err="bad-err")
    assert 'class="msg ok"' in b and "ok-msg" in b
    assert 'class="msg err"' in b and "bad-err" in b
    # ok 在前、err 在后
    assert b.index("ok-msg") < b.index("bad-err")


def test_banner_empty_returns_empty_string():
    """msg / err 都为空 → 不产生横幅（inject 据此原样返回）。"""
    assert flash._banner() == ""
    assert flash._banner(None, None) == ""
    assert flash._banner("", "") == ""
    assert flash._banner(msg="", err="") == ""


def test_banner_falsy_but_not_none():
    """0 / False 等假值不产生横幅（PHP 的 `if ($msg)` 语义）。"""
    assert flash._banner(msg=0) == ""
    assert flash._banner(err=False) == ""


def test_banner_non_str_coerced():
    """非字符串入参先 str() 再转义，不能抛。"""
    b = flash._banner(msg=12345)
    assert "12345" in b
    b2 = flash._banner(err=3.14)
    assert "3.14" in b2


def test_banner_unicode_not_escaped():
    """中文/emoji 原样保留（html.escape 不碰非 ASCII）。"""
    b = flash._banner(msg="实例「测试」已添加 🎉")
    assert "实例「测试」已添加 🎉" in b


def test_banner_agrees_with_h():
    """_banner 的转义结果必须与 configops.h 逐字一致（同一个转义函数）。"""
    raw = '<a href="x">&\'</a>'
    assert h(raw) in flash._banner(msg=raw)


# ================= inject：横幅插入位置 =================

def test_inject_at_main_anchor():
    html = f"<body><div>head</div>{flash.MAIN_ANCHOR}<div>tail</div></body>"
    out = flash.inject(html, msg="hello")
    assert out.index('class="msg ok"') < out.index(flash.MAIN_ANCHOR)
    assert "hello" in out


def test_inject_no_banner_returns_html_unchanged():
    html = "<body><div>x</div></body>"
    assert flash.inject(html) == html
    assert flash.inject(html, msg="", err="") == html


def test_inject_auth_card_before_form():
    """登录/初始化页：横幅插在 auth-card 内第一张 form 之前。"""
    html = ('<body><div class="auth-card login">'
            '<p>title</p><form method="post"><input></form></div></body>')
    out = flash.inject(html, err="密码错误")
    assert out.index('class="msg err"') < out.index("<form")
    # 登录页的 err 横幅没有 ❌ 前缀（与 PHP 一致）
    assert "❌" not in out
    assert "密码错误" in out


def test_inject_auth_card_both_msg_and_err_keeps_prefix():
    """msg + err 同时存在时走通用 banner（带 ✅/❌ 前缀）。"""
    html = ('<body><div class="auth-card"><form></form></div></body>')
    out = flash.inject(html, msg="done", err="oops")
    assert "✅" in out and "❌" in out


def test_inject_before_body_close():
    """既无主锚点也无 auth-card → 插在 </body> 前。"""
    html = "<html><body><div>only</div></body></html>"
    out = flash.inject(html, msg="x")
    assert out.index('class="msg ok"') < out.index("</body>")


def test_inject_appends_when_no_body():
    """连 </body> 都没有 → 直接追加到末尾（不能丢消息）。"""
    out = flash.inject("<div>fragment</div>", msg="x")
    assert out.endswith(flash._banner(msg="x"))


def test_inject_notify_check_with_anchor(monkeypatch):
    """notify_check 盒子插到 cfg-group 之前。"""
    from services import configops

    monkeypatch.setattr(configops, "notify_check_html",
                        lambda nc: '<div class="cfg-group notify">NC</div>')
    html = ('<body><div class="cfg-group" id="cfgGroup-notify">'
            "<p>x</p></div></body>")
    out = flash.inject(html, notify_check={"ok": True})
    assert "NC" in out
    assert out.index("NC") < out.index("cfgGroup-notify")


def test_inject_notify_check_without_anchor_appends(monkeypatch):
    from services import configops

    monkeypatch.setattr(configops, "notify_check_html", lambda nc: "<div>NC2</div>")
    out = flash.inject("<body></body>", notify_check={"ok": True})
    assert "NC2" in out


# ================= fallback_page =================

def test_fallback_page_contains_banner_and_refresh():
    page = flash.fallback_page(err="boom")
    assert "boom" in page
    assert 'class="msg err"' in page
    assert "http-equiv=\"refresh\"" in page
    assert "<!doctype html>" in page.lower()


def test_fallback_page_no_banner_when_empty():
    page = flash.fallback_page()
    assert 'class="msg ok"' not in page
    assert 'class="msg err"' not in page
    # 仍是完整页面（有 refresh 回跳）
    assert "refresh" in page


def test_fallback_page_single_escape():
    page = flash.fallback_page(msg="a & b")
    assert "a &amp; b" in page
    assert "&amp;amp;" not in page


# ================= fetch_php_page =================

class _FakeResp:
    def __init__(self, status=200, text="PAGE", content=b"PAGE"):
        self.status_code = status
        self.text = text
        self.content = content


class _FakeAsyncClient:
    """替换 httpx.AsyncClient：记录 URL/headers，返回预置响应或抛异常。"""

    last = {}

    def __init__(self, resp=None, exc=None, **kw):
        self._resp = resp
        self._exc = exc
        _FakeAsyncClient.last["kw"] = kw

    def __call__(self, *a, **kw):     # 让实例本身可当类用（不必要，保留兼容）
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, headers=None):
        _FakeAsyncClient.last["url"] = url
        _FakeAsyncClient.last["headers"] = headers
        if self._exc:
            raise self._exc
        return self._resp


class _FakeRequest:
    def __init__(self, path="/config", cookies=None):
        self.url = type("U", (), {"path": path})()
        self.cookies = cookies or {}


def _patch_client(monkeypatch, resp=None, exc=None):
    def _factory(**kw):
        return _FakeAsyncClient(resp=resp, exc=exc, **kw)

    monkeypatch.setattr(flash.httpx, "AsyncClient", _factory)


@pytest.mark.anyio
async def test_fetch_php_page_ok(monkeypatch):
    _patch_client(monkeypatch, resp=_FakeResp(200, "PHPBODY", b"PHPBODY"))
    out = await flash.fetch_php_page(_FakeRequest("/config"))
    assert out == "PHPBODY"
    # 同路径 + cookie 透传，且**不带查询串**（避免命中 ajax 路由）
    assert _FakeAsyncClient.last["url"].endswith("/config")
    assert "?" not in _FakeAsyncClient.last["url"]


@pytest.mark.anyio
async def test_fetch_php_page_passes_cookies(monkeypatch):
    _patch_client(monkeypatch, resp=_FakeResp(200, "X", b"X"))
    await flash.fetch_php_page(_FakeRequest("/", {"PHPSESSID": "abc", "k": "v"}))
    hdr = _FakeAsyncClient.last["headers"]["cookie"]
    assert "PHPSESSID=abc" in hdr and "k=v" in hdr


@pytest.mark.anyio
async def test_fetch_php_page_non_200_returns_none(monkeypatch):
    _patch_client(monkeypatch, resp=_FakeResp(404, "NF", b"NF"))
    assert await flash.fetch_php_page(_FakeRequest()) is None


@pytest.mark.anyio
async def test_fetch_php_page_empty_content_returns_none(monkeypatch):
    _patch_client(monkeypatch, resp=_FakeResp(200, "", b""))
    assert await flash.fetch_php_page(_FakeRequest()) is None


@pytest.mark.anyio
async def test_fetch_php_page_http_error_returns_none(monkeypatch):
    _patch_client(monkeypatch, exc=flash.httpx.ConnectError("boom"))
    assert await flash.fetch_php_page(_FakeRequest()) is None


# ================= flash()：正常路径 / 降级路径 =================

class _Req:
    def __init__(self, path="/config"):
        self.url = type("U", (), {"path": path})()
        self.cookies = {}


@pytest.mark.anyio
async def test_flash_uses_page_response_when_ok(monkeypatch):
    """正常路径：走 page_response（Python 自渲染），不回源。"""
    from services import pagectx

    called = {}

    def _fake_page_response(request, *, msg=None, err=None, notify_check=None):
        called["args"] = (msg, err, notify_check)
        from fastapi.responses import HTMLResponse
        return HTMLResponse("PY-PAGE")

    monkeypatch.setattr(pagectx, "page_response", _fake_page_response)
    r = await flash.flash(_Req(), msg="m1", err="e1", notify_check={"x": 1})
    assert r.body == b"PY-PAGE"
    assert called["args"] == ("m1", "e1", {"x": 1})


@pytest.mark.anyio
async def test_flash_falls_back_to_php_when_page_response_raises(monkeypatch):
    """page_response 抛异常 → 降级回 PHP 回源 + 注入横幅。"""
    from services import pagectx

    def _boom(*a, **kw):
        raise RuntimeError("template broken")

    monkeypatch.setattr(pagectx, "page_response", _boom)
    _patch_client(monkeypatch, resp=_FakeResp(200, "PHP PAGE", b"PHP PAGE"))
    r = await flash.flash(_Req("/config"), msg="hello")
    assert "PHP PAGE" in r.body.decode()
    assert "hello" in r.body.decode()


@pytest.mark.anyio
async def test_flash_falls_back_to_static_page_when_php_down(monkeypatch):
    """page_response 抛 + 取不到 PHP 页 → 自绘降级页（不抛给上层）。"""
    from services import pagectx

    monkeypatch.setattr(pagectx, "page_response",
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("x")))
    _patch_client(monkeypatch, exc=flash.httpx.ConnectError("no php"))
    r = await flash.flash(_Req(), err="失败了")
    body = r.body.decode()
    assert "失败了" in body
    assert "refresh" in body           # 2 秒自动回面板
    assert r.status_code == 200


@pytest.mark.anyio
async def test_flash_fallback_single_escape(monkeypatch):
    """降级页同样只转义一次（曾在此链双重转义）。"""
    from services import pagectx

    monkeypatch.setattr(pagectx, "page_response",
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("x")))
    _patch_client(monkeypatch, exc=flash.httpx.ConnectError("no php"))
    r = await flash.flash(_Req(), msg="A & B")
    body = r.body.decode()
    assert "A &amp; B" in body
    assert "&amp;amp;" not in body


@pytest.mark.anyio
async def test_flash_returns_response_type(monkeypatch):
    """返回值必须是 Response（可被 FastAPI 直接返回，不能是协程/None）。"""
    from starlette.responses import Response

    from services import pagectx

    monkeypatch.setattr(pagectx, "page_response",
                        lambda *a, **kw: __import__(
                            "fastapi.responses", fromlist=["HTMLResponse"]
                        ).HTMLResponse("x"))
    r = await flash.flash(_Req())
    assert isinstance(r, Response)
