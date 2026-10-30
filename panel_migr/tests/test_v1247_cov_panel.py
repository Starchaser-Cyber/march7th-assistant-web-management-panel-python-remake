"""V1.24.7 覆盖率补测：`routers/panel.py`（本地渲染兜底 + PWA 附属资源）。

重点是把 v1.24.4 的 P1-14 钉死：`render_fallback` 是**同步函数**，`main.py`
曾对它 `await` → `TypeError: object HTMLResponse can't be used in 'await'
expression` → 所有非面板路径（/favicon.ico、/robots.txt、/manifest.json、
任意深链接、非面板 POST）全部 500。

本文件用真实 FastAPI TestClient 做端到端断言，这五条路径此前零覆盖。
全部离线：不联网、不碰 docker。
"""
import json
import struct
import sys
import uuid
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import config as cfg
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient

import main as main_mod
from routers import panel

client = TestClient(main_mod.app)


def _session(authed=False) -> dict:
    """造一个会话文件（与 test_v1244_fix_handlers 同风格）。"""
    sid = "v47p" + uuid.uuid4().hex[:10]
    p = Path(cfg.SESSION_SAVE_PATH)
    p.mkdir(parents=True, exist_ok=True)
    parts = "m7a_panel_auth|b:1;" if authed else ""
    (p / f"sess_{sid}").write_text(parts, encoding="utf-8")
    return {"PHPSESSID": sid}


# ================= 任务 7：P1-14 端到端回归（最重要）=================

@pytest.mark.parametrize("path", [
    "/favicon.ico",
    "/robots.txt",
    "/manifest.json",
    "/some/deep/link",
    "/a/b/c/d/e",
])
def test_p14_non_panel_get_returns_200(path):
    """非面板 GET 必须 200。

    v1.24.4 之前这里 500：`main.catch_all` 对同步的 `render_fallback` 用了
    `await`，直接抛 TypeError。这五条路径是那次修复的验收目标。
    """
    r = client.get(path, cookies=_session(), follow_redirects=False)
    assert r.status_code == 200, f"{path} 返回 {r.status_code}，P1-14 回归"
    assert r.headers["content-type"].startswith("text/html")


def test_p14_non_panel_post_returns_200():
    """非面板 POST（未登录、非面板 action）同样走 render_fallback，必须 200。"""
    r = client.post("/foo", data={"a": "b"}, cookies=_session(),
                    follow_redirects=False)
    assert r.status_code == 200, f"/foo POST 返回 {r.status_code}，P1-14 回归"
    assert r.headers["content-type"].startswith("text/html")


def test_p14_authed_non_panel_get_returns_200():
    """已登录用户的非面板路径也不能 500（走同一条兜底分支）。"""
    r = client.get("/favicon.ico", cookies=_session(authed=True),
                   follow_redirects=False)
    assert r.status_code == 200


def test_p14_render_fallback_is_sync():
    """根因钉死：`render_fallback` 必须是同步函数。

    它内部调用的 `page_response` 也是同步的；一旦有人把它改成 `async def`
    而 `main.py` 仍同步调用，就会返回协程对象 → 500。反之若有人改回
    `await`，`test_p14_no_await_in_main` 会失败。两条一起锁死。
    """
    import inspect

    assert not inspect.iscoroutinefunction(panel.render_fallback), (
        "render_fallback 变成了 async：main.py 的同步调用会拿到协程对象 → 500"
    )
    assert not inspect.iscoroutinefunction(panel.icon_response)
    assert not inspect.iscoroutinefunction(panel.manifest_response)


def test_p14_no_await_in_main():
    """`main.py` 里不得再出现 `await render_fallback(` / `await icon_response(`。"""
    src = (Path(__file__).resolve().parent.parent / "main.py").read_text("utf-8")
    for fn in ("render_fallback", "icon_response", "manifest_response"):
        assert f"await {fn}(" not in src, f"main.py 又 await 了同步函数 {fn}"


def test_p14_fallback_body_is_panel_page():
    """兜底返回的确实是面板页面（登录页），不是空 body 或错误页。"""
    r = client.get("/some/deep/link", cookies=_session(), follow_redirects=False)
    assert r.status_code == 200
    body = r.text
    assert "<html" in body.lower()
    assert len(body) > 200, "兜底页面疑似空壳"


# ================= render_fallback 单元契约 =================

@pytest.fixture()
def _mini_client():
    """最小 app：只为了拿到一个「已注入 state.session」的真实 Request。

    main.py 的 catch_all 在调用 render_fallback 前会 `request.state.session =
    load_session(request)`，这里复刻该前置条件，验证 render_fallback 自身的契约。
    """
    mini = FastAPI()

    @mini.get("/probe")
    def _probe(request: Request):          # noqa: ANN001
        request.state.session = {}
        return panel.render_fallback(request)

    return TestClient(mini)


def test_render_fallback_returns_html_response_200(_mini_client):
    r = _mini_client.get("/probe", follow_redirects=False)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")


def test_render_fallback_returns_html_response_type():
    """返回值类型必须是 HTMLResponse（不是协程、不是 JSON）。"""
    from starlette.requests import Request as SRequest

    scope = {
        "type": "http", "method": "GET", "path": "/x", "root_path": "",
        "query_string": b"", "headers": [], "scheme": "http",
        "server": ("testserver", 80), "client": ("127.0.0.1", 1),
    }
    req = SRequest(scope)
    req.state.session = {}
    resp = panel.render_fallback(req)
    assert isinstance(resp, HTMLResponse)
    assert resp.status_code == 200


# ================= _png_solid：合法 PNG =================

def _png_wh(data: bytes) -> tuple:
    """从 IHDR 读宽高（PNG 前 8 字节 magic，随后 4 字节长度 + 'IHDR' + w/h）。"""
    assert data[:8] == b"\x89PNG\r\n\x1a\n", "PNG magic 不对"
    assert data[12:16] == b"IHDR"
    return struct.unpack(">II", data[16:24])


@pytest.mark.parametrize("size", [16, 32, 192, 512])
def test_png_solid_valid(size):
    data = panel._png_solid(size, (236, 72, 153))
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    w, h = _png_wh(data)
    assert (w, h) == (size, size)
    # 结构完整：IHDR + IDAT + IEND 三个 chunk 的 CRC 都能算通
    i = 8
    chunks = []
    while i < len(data):
        ln = struct.unpack(">I", data[i:i + 4])[0]
        typ = data[i + 4:i + 8]
        body = data[i + 8:i + 8 + ln]
        crc = struct.unpack(">I", data[i + 8 + ln:i + 12 + ln])[0]
        assert crc == zlib.crc32(typ + body) & 0xFFFFFFFF, f"{typ} CRC 错"
        chunks.append(typ)
        i += 12 + ln
    assert chunks == [b"IHDR", b"IDAT", b"IEND"]
    assert i == len(data), "尾部有多余字节"


def test_png_solid_pixel_is_solid_color():
    """解压 IDAT 应得到每行 filter=0 + 纯色像素。"""
    data = panel._png_solid(2, (1, 2, 3))
    i = 8
    idat = b""
    while i < len(data):
        ln = struct.unpack(">I", data[i:i + 4])[0]
        typ = data[i + 4:i + 8]
        if typ == b"IDAT":
            idat = data[i + 8:i + 8 + ln]
        i += 12 + ln
    raw = zlib.decompress(idat)
    assert raw == (b"\x00" + bytes((1, 2, 3)) * 2) * 2


# ================= icon_response：size 钳制 =================

@pytest.mark.parametrize("qs,expect", [
    ("size=abc", 192),      # 非法 → 回落 192
    ("", 192),              # 缺省 → 192
    ("size=0", 16),         # 下钳
    ("size=-5", 16),
    ("size=1", 16),
    ("size=99999", 512),    # 上钳
    ("size=513", 512),
    ("size=192", 192),
    ("size=16", 16),
    ("size=512", 512),
])
def test_icon_size_clamped(qs, expect):
    r = client.get(f"/?icon=1&{qs}", follow_redirects=False)
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    w, h = _png_wh(r.content)
    assert (w, h) == (expect, expect)
    assert 16 <= w <= 512


def test_icon_cache_control_header():
    r = client.get("/?icon=1&size=64", follow_redirects=False)
    assert r.headers.get("cache-control") == "public, max-age=86400"


# ================= manifest_response：合法 JSON =================

def test_manifest_valid_json():
    r = client.get("/?manifest=1", follow_redirects=False)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/manifest+json")
    data = json.loads(r.content)
    assert isinstance(data, dict)
    assert data["name"]
    assert data["short_name"]
    assert isinstance(data["icons"], list) and len(data["icons"]) >= 1
    for ic in data["icons"]:
        assert ic["src"] and ic["sizes"] and ic["type"] == "image/png"


def test_manifest_icon_sizes_match_declared():
    """清单里声明的 sizes 与 ?icon 实际能生成的尺寸一致（避免装了图标是空的）。"""
    r = client.get("/?manifest=1", follow_redirects=False)
    data = json.loads(r.content)
    for ic in data["icons"]:
        px = int(ic["sizes"].split("x")[0])
        ri = client.get(f"/?icon=1&size={px}", follow_redirects=False)
        w, _ = _png_wh(ri.content)
        assert w == px


def test_manifest_theme_color_matches_constant():
    r = client.get("/?manifest=1", follow_redirects=False)
    assert json.loads(r.content)["theme_color"] == panel._THEME_COLOR


# ================= 路由优先级：manifest/icon 不被 catch_all 抢走 =================

def test_manifest_not_shadowed_by_catch_all():
    """/manifest.json 是真实路径，必须仍落到 catch_all 的兜底（HTML），
    而 `?manifest=1` 走 PWA 清单（JSON）——两者不能互相干扰。"""
    r1 = client.get("/manifest.json", cookies=_session(), follow_redirects=False)
    assert r1.headers["content-type"].startswith("text/html")
    r2 = client.get("/?manifest=1", follow_redirects=False)
    assert r2.headers["content-type"].startswith("application/manifest+json")


def test_unsupported_method_returns_405():
    """PUT/DELETE 等面板不使用的方法：FWD_ENABLE=0 时本地 405（不是 500）。"""
    for m in ("PUT", "DELETE", "OPTIONS", "PATCH"):
        r = client.request(m, "/whatever", cookies=_session(),
                           follow_redirects=False)
        assert r.status_code == 405, f"{m} 返回 {r.status_code}"
        assert r.json()["ok"] is False


def test_fwd_enable_off_by_default():
    """P1-14 的端到端断言依赖 FWD_ENABLE=0（默认）。"""
    assert cfg.FWD_ENABLE is False
