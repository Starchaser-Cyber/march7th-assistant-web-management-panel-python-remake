"""v1.24.7 首屏性能回归（方向A）。

覆盖：
1. GZipMiddleware 已注册且 minimum_size=1000
2. 静态资源带强缓存 Cache-Control: ...immutable
3. >1000 B 的响应在带 Accept-Encoding: gzip 时被真正压缩（按 ASGI 线上字节验证）
4. <1000 B 的响应不压缩
5. 模板渲染出的 HTML 里 panel.css / panel.js 带 ?v=<PANEL_VERSION>
6. panel.js 初始化阶段不再无条件 loadECharts（echarts 懒加载）
7. 工程配置文件（pyproject.toml / ruff.toml / ci.yml）存在且可解析
"""
from __future__ import annotations

import asyncio
import sys
import uuid
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config as cfg
from fastapi.testclient import TestClient
from starlette.middleware.gzip import GZipMiddleware

from main import app

ROOT = Path(__file__).resolve().parent.parent
REPO = ROOT.parent
STATIC = ROOT / "static"

client = TestClient(app)


# ---------- 工具 ----------

def new_session(authed: bool = True) -> dict:
    """造一个 PHP session 文件并返回 cookie（与 tests/test_m2_get.py 同法）。"""
    sid = "pf" + uuid.uuid4().hex[:10]
    parts = "m7a_panel_auth|b:1;" if authed else ""
    p = Path(cfg.SESSION_SAVE_PATH)
    p.mkdir(parents=True, exist_ok=True)
    (p / f"sess_{sid}").write_text(parts, encoding="utf-8")
    return {"PHPSESSID": sid}


def asgi_get(path: str, headers: dict | None = None, cookies: dict | None = None):
    """直接走 ASGI，返回 (线上 body 字节, 响应头 dict)。

    TestClient 的 r.content 是**解压后**的，看不到真实传输量，所以压缩比必须
    在这一层量。
    """
    parts = urlsplit(path)
    raw_headers = [(b"host", b"testserver")]
    for k, v in (headers or {}).items():
        raw_headers.append((k.lower().encode("latin-1"), str(v).encode("latin-1")))
    if cookies:
        raw_headers.append(
            (b"cookie", "; ".join(f"{k}={v}" for k, v in cookies.items()).encode("latin-1"))
        )
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": parts.path or "/",
        "raw_path": (parts.path or "/").encode("latin-1"),
        "query_string": parts.query.encode("latin-1"),
        "root_path": "",
        "headers": raw_headers,
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 80),
    }
    messages: list[dict] = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        messages.append(message)

    asyncio.run(app(scope, receive, send))

    body = b"".join(
        m.get("body", b"") for m in messages if m["type"] == "http.response.body"
    )
    hdrs: dict[str, str] = {}
    for m in messages:
        if m["type"] == "http.response.start":
            hdrs = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in m["headers"]}
    return body, hdrs


# ---------- 1. GZip 中间件已注册 ----------

def test_gzip_middleware_registered():
    gz = [m for m in app.user_middleware if m.cls is GZipMiddleware]
    assert gz, "GZipMiddleware 未注册"
    assert gz[0].kwargs.get("minimum_size") == 1000


# ---------- 2. 静态资源强缓存 ----------

def test_static_assets_have_immutable_cache_control():
    for name in ("panel.js", "panel.css"):
        r = client.get(f"/static/{name}")
        assert r.status_code == 200, name
        cc = r.headers.get("cache-control", "")
        assert "immutable" in cc, f"{name} 缺 immutable：{cc!r}"
        assert "max-age=31536000" in cc, f"{name} max-age 不对：{cc!r}"


def test_static_cache_control_survives_304():
    """条件请求命中 304 时也必须带 Cache-Control，否则浏览器下次还会发请求。"""
    first = client.get("/static/panel.js")
    etag = first.headers.get("etag")
    assert etag, "静态资源应有 ETag"
    second = client.get("/static/panel.js", headers={"If-None-Match": etag})
    assert second.status_code == 304
    assert "immutable" in second.headers.get("cache-control", "")


# ---------- 3. 大响应被压缩（按线上字节） ----------

def test_large_static_asset_is_actually_compressed_on_wire():
    raw_size = (STATIC / "panel.js").stat().st_size
    assert raw_size > 1000, "panel.js 应远大于 minimum_size"

    wire, hdrs = asgi_get("/static/panel.js", headers={"accept-encoding": "gzip"})
    assert hdrs.get("content-encoding") == "gzip", hdrs
    assert len(wire) < raw_size * 0.6, (
        f"压缩后 {len(wire)} B 相对原始 {raw_size} B 没有明显下降"
    )

    plain, hdrs2 = asgi_get("/static/panel.js")
    assert hdrs2.get("content-encoding") is None, "未声明 gzip 时不应压缩"
    assert len(plain) == raw_size


def test_large_html_is_compressed():
    wire, hdrs = asgi_get("/", headers={"accept-encoding": "gzip"}, cookies=new_session())
    assert hdrs.get("content-encoding") == "gzip"
    # 首屏 HTML 未压缩约 190 KB
    assert len(wire) < 60_000, f"HTML 压缩后仍有 {len(wire)} B"


# ---------- 4. 小响应不压缩 ----------

def test_small_response_not_compressed():
    cookies = new_session()
    r = client.get("/?ajax=status", headers={"Accept-Encoding": "gzip"}, cookies=cookies)
    assert r.status_code == 200
    assert r.headers.get("content-encoding") is None, "小于 minimum_size 的响应不应压缩"

    wire, hdrs = asgi_get("/?ajax=status", headers={"accept-encoding": "gzip"}, cookies=cookies)
    assert hdrs.get("content-encoding") is None
    assert len(wire) < 1000


def test_identity_encoding_not_compressed():
    wire, hdrs = asgi_get("/static/panel.js", headers={"accept-encoding": "identity"})
    assert hdrs.get("content-encoding") is None
    assert len(wire) == (STATIC / "panel.js").stat().st_size


# ---------- 5. 模板里的版本串 ----------

def test_rendered_html_has_versioned_asset_urls():
    r = client.get("/", cookies=new_session())
    assert r.status_code == 200
    html = r.text
    assert f'/static/panel.css?v={cfg.PANEL_VERSION}' in html, "panel.css 缺 ?v= 版本串"
    assert f'/static/panel.js?v={cfg.PANEL_VERSION}' in html, "panel.js 缺 ?v= 版本串"
    # 版本串必须来自模板上下文，前端 loadECharts 靠它给 echarts 加 ?v=
    assert "window.PANEL_VERSION" in html, "模板应注入 window.PANEL_VERSION"


def test_panel_js_uses_version_query_for_echarts():
    src = (STATIC / "panel.js").read_text(encoding="utf-8")
    assert "_ecVer" in src, "panel.js 应有 _ecVer() 拼版本串"
    assert "window.PANEL_VERSION" in src
    # 本地优先 + CDN 回退是 v1.24.3 M7A-07 的安全要求，不能被性能改动弄丢
    assert "/static/vendor/echarts.min.js" in src
    assert "cdn.jsdelivr.net" in src


# ---------- 6. echarts 懒加载 ----------

def test_panel_js_init_does_not_load_echarts_unconditionally():
    src = (STATIC / "panel.js").read_text(encoding="utf-8")
    lines = src.splitlines()
    # init 区以注释 "// Init" 起头，到文件末尾的独立 IIFE 之前
    start = next(i for i, ln in enumerate(lines) if ln.strip() == "// Init")
    end = next(
        (i for i in range(start + 1, len(lines)) if lines[i].lstrip().startswith("(function()")),
        len(lines),
    )
    init = "\n".join(lines[start:end])
    assert "loadECharts(" not in init, "初始化区仍在无条件 loadECharts，首屏会拉 1 MB"
    assert "startMonitor()" in init
    assert "_watchMonitorCard()" in init


def test_panel_js_has_lazy_echarts_plumbing():
    src = (STATIC / "panel.js").read_text(encoding="utf-8")
    for fn in (
        "function ensureMonitorChart()",
        "function _monCardVisible()",
        "function _watchMonitorCard()",
        "IntersectionObserver",
    ):
        assert fn in src, f"panel.js 缺 {fn}"
    # 概览页签切回来时也要补加载
    assert "ensureMonitorChart" in src


# ---------- 7. 工程配置 ----------

def test_pyproject_toml_is_valid():
    import tomllib

    data = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    proj = data["project"]
    assert proj["requires-python"].startswith(">=3.11")
    assert proj["name"]
    assert data["tool"]["pytest"]["ini_options"]["testpaths"] == ["panel_migr/tests"]


def test_ruff_toml_is_valid():
    import tomllib

    data = tomllib.loads((REPO / "ruff.toml").read_text(encoding="utf-8"))
    assert data["target-version"] == "py311"
    assert "E" in data["lint"]["select"]
    assert "panel_migr/tests/_tmp" in data["exclude"]


def test_ci_workflow_exists_and_pins_events_bg():
    import yaml

    wf = yaml.safe_load((REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))
    jobs = wf["jobs"]
    assert "test" in jobs and "lint" in jobs
    test_job = jobs["test"]
    matrix = test_job["strategy"]["matrix"]["python-version"]
    assert "3.11" in matrix and "3.12" in matrix
    # M7A_EVENTS_BG=0 可以在 job 级 env，也可以挂在实际跑 pytest 的那一步上
    step_envs = [s.get("env") or {} for s in test_job["steps"]]
    step_envs.append(test_job.get("env") or {})
    assert any(e.get("M7A_EVENTS_BG") == "0" for e in step_envs), (
        "CI 必须设 M7A_EVENTS_BG=0"
    )
    runs = "\n".join(s.get("run", "") for s in test_job["steps"])
    assert "pytest" in runs
