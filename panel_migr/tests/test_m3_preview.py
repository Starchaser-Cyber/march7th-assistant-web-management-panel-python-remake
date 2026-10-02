"""M3 预览收编测试：token 一致性 / 动态寻址与自愈 / HTTP 透传 / WS 反代 / H.264 管线。

约定：路径与会话全部从 config 模块读取（tests/_tmp）；
容器寻址通过 monkeypatch services.preview.run_cmd 打桩（本地无 docker）；
health 探测默认 bypass，自愈用例单独走真探测路径。
"""
import asyncio
import json
import subprocess
import sys
import tempfile
import threading
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config as cfg
import http.server
import pytest
from fastapi.testclient import TestClient

import services.preview as pv
from main import app

client = TestClient(app)

# ===== 模块级缓存隔离（其他测试文件可能触碰 preview 状态）=====
pv.reset_state()

_secret_file = Path(tempfile.gettempdir()) / f"m3_secret_{uuid.uuid4().hex[:8]}"


def _write_secret(text: str) -> None:
    _secret_file.write_text(text, encoding="utf-8")


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    pv.reset_state()
    monkeypatch.setattr(cfg, "PREVIEW_SECRET_FILE", str(_secret_file))
    yield
    pv.reset_state()


@pytest.fixture
def fake_run_cmd(monkeypatch):
    """docker inspect → 127.0.0.1；docker exec -d → 记录并成功。"""
    exec_calls: list[str] = []

    def _run(cmd, timeout=120):
        exec_calls.append(cmd)
        if cmd.startswith("docker inspect"):
            return {"code": 0, "out": "127.0.0.1"}
        return {"code": 0, "out": ""}

    monkeypatch.setattr(pv, "run_cmd", _run)
    return exec_calls


@pytest.fixture
def bypass_health(monkeypatch):
    async def _ok(container, base):
        return True

    monkeypatch.setattr(pv, "health_ok", _ok)


class _MockPreview(http.server.BaseHTTPRequestHandler):
    """容器 preview_server 的 HTTP 假上游：/health 与 /stream。"""

    def do_GET(self):
        if self.path.startswith("/health"):
            body = json.dumps({"ok": True, "cdp": True, "clients": 0}).encode()
            ctype = "application/json"
            code = 200
        elif self.path.startswith("/stream"):
            body = (b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: 3\r\n\r\n"
                    b"\xff\xd8\xff\r\n--frame\r\n")
            ctype = "multipart/x-mixed-replace; boundary=frame"
            code = 200
        else:
            body = b'{"error": "not found"}'
            ctype = "application/json"
            code = 404
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@pytest.fixture
def http_upstream(monkeypatch):
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _MockPreview)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setattr(cfg, "PREVIEW_UPSTREAM_PORT", srv.server_address[1])
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def _make_jpeg() -> bytes:
    out = Path(tempfile.gettempdir()) / f"m3_{uuid.uuid4().hex[:8]}.jpg"
    try:
        subprocess.run(
            [cfg.FFMPEG_BIN, "-hide_banner", "-loglevel", "error", "-y",
             "-f", "lavfi", "-i", "color=c=red:s=64x64", "-frames:v", "1", str(out)],
            check=True, timeout=20,
        )
        return out.read_bytes()
    except Exception:
        pytest.skip("ffmpeg 不可用，无法生成测试 JPEG")
    finally:
        out.unlink(missing_ok=True)


def _start_ws_upstream(jpeg: bytes):
    """后台线程起 websockets 服务：持续推 JPEG 帧（复用于转发与 H.264 用例）。"""
    import websockets

    box: dict = {}
    ev = threading.Event()

    async def handler(ws):
        try:
            while True:
                await ws.send(jpeg)
                await asyncio.sleep(0.05)
        except Exception:
            pass

    def run():
        async def main():
            srv = await websockets.serve(handler, "127.0.0.1", 0)
            box["port"] = srv.sockets[0].getsockname()[1]
            ev.set()
            await asyncio.Future()

        try:
            asyncio.run(main())
        except Exception:
            ev.set()

    threading.Thread(target=run, daemon=True).start()
    assert ev.wait(5), "WS 上游启动超时"
    assert "port" in box, "WS 上游启动失败"
    return box["port"]


def _authed_cookies():
    sid = "m3" + uuid.uuid4().hex[:10]
    p = Path(cfg.SESSION_SAVE_PATH)
    p.mkdir(parents=True, exist_ok=True)
    (p / f"sess_{sid}").write_text("m7a_panel_auth|b:1;", encoding="utf-8")
    return {"PHPSESSID": sid}


# ---------- token ----------

def test_day_token_matches_reference_algorithm():
    got = pv.day_token("s3cret", "20261001")
    import hashlib
    import hmac as h
    ref = h.new(b"s3cret", b"20261001", hashlib.sha256).hexdigest()[:32]
    assert got == ref
    assert len(got) == 32 and all(c in "0123456789abcdef" for c in got)


def test_day_token_defaults_to_local_today():
    import time
    got = pv.day_token("x")
    ref = pv.day_token("x", time.strftime("%Y%m%d"))
    assert got == ref


def test_preview_token_ok(monkeypatch):
    _write_secret("abcd" * 8)
    d = pv.preview_token()
    assert d["ok"] is True
    assert d["token"] == pv.day_token("abcd" * 8)


def test_preview_token_missing_secret():
    _write_secret("")
    d = pv.preview_token()
    assert d["ok"] is False
    assert "preview_secret" in d["msg"]


def test_preview_token_ajax_endpoint():
    """GET ?ajax=preview_token 走面板登录鉴权 + Python 分发（不再依赖 PHP）。"""
    _write_secret("deadbeef" * 4)
    r = client.get("/?ajax=preview_token", cookies=_authed_cookies())
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] and d["token"] == pv.day_token("deadbeef" * 4)
    # 未登录 → Python 渲染登录页（M5-B 不再依赖 PHP）
    r2 = client.get("/?ajax=preview_token")
    assert "auth-card" in r2.text


# ---------- 动态寻址与自愈 ----------

def test_resolve_upstream_caches_per_container(fake_run_cmd):
    assert pv.resolve_upstream("m7a") == "127.0.0.1"
    assert pv.resolve_upstream("m7a") == "127.0.0.1"
    assert len(fake_run_cmd) == 1  # 60s 缓存，不重复 docker inspect


def test_resolve_upstream_rejects_garbage(monkeypatch):
    monkeypatch.setattr(pv, "run_cmd", lambda c, timeout=120: {"code": 1, "out": "err"})
    assert pv.resolve_upstream("m7a") is None
    assert pv.resolve_upstream("nope") is None


def test_get_upstream_selfheal_then_503(fake_run_cmd, monkeypatch):
    """真探测死端口 → docker exec 拉起（节流记录）→ 复检失败 → 明确错误。"""
    monkeypatch.setattr(cfg, "PREVIEW_UPSTREAM_PORT", 1)  # 必然拒绝的端口
    base, err = asyncio.run(pv.get_upstream("m7a"))
    assert base is None and "不可达" in err
    assert any(c.startswith("docker exec -d m7a python3") for c in fake_run_cmd)
    n = len(fake_run_cmd)
    # 60s 节流：立即再次 get_upstream 不重复拉起
    base2, _ = asyncio.run(pv.get_upstream("m7a"))
    assert base2 is None
    assert len(fake_run_cmd) == n


def test_get_upstream_healthy_returns_base(fake_run_cmd, bypass_health):
    base, err = asyncio.run(pv.get_upstream("m7a"))
    assert err == "" and base.startswith("http://127.0.0.1:")


# ---------- HTTP 透传 ----------

def test_health_proxy_passthrough(fake_run_cmd, bypass_health, http_upstream):
    r = client.get("/m7a-preview/health")
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True and d["cdp"] is True


def test_stream_proxy_mjpeg_passthrough(fake_run_cmd, bypass_health, http_upstream):
    r = client.get("/m7a-preview/stream?token=x&res=720p")
    assert r.status_code == 200
    assert "multipart/x-mixed-replace" in r.headers.get("content-type", "")
    assert b"\xff\xd8\xff" in r.content


def test_proxy_503_when_upstream_down(monkeypatch, fake_run_cmd):
    """上游不可达（探测+自愈均失败）→ 503 + 中文错误（不再误转发给 PHP）。"""
    monkeypatch.setattr(cfg, "PREVIEW_UPSTREAM_PORT", 1)
    r = client.get("/m7a-preview/health")
    assert r.status_code == 503
    assert r.json()["ok"] is False


# ---------- WebSocket 反代 ----------

def test_ws_preview_relays_upstream_frames(fake_run_cmd, bypass_health, monkeypatch):
    jpeg = _make_jpeg()
    port = _start_ws_upstream(jpeg)
    monkeypatch.setattr(cfg, "PREVIEW_UPSTREAM_PORT", port)
    with client.websocket_connect("/m7a-preview/ws?token=t&res=720p") as ws:
        data = ws.receive_bytes()
    assert data[:2] == b"\xff\xd8"  # JPEG SOI：上游帧原样透传


def test_ws_preview_upstream_down_closes_with_error(fake_run_cmd, monkeypatch):
    monkeypatch.setattr(cfg, "PREVIEW_UPSTREAM_PORT", 1)

    async def _never(container, base):
        return False

    monkeypatch.setattr(pv, "health_ok", _never)
    with client.websocket_connect("/m7a-preview/ws?token=t") as ws:
        msg = ws.receive_text()
    assert "error" in msg and "不可达" in msg


# ---------- H.264 极致档 ----------

def test_ws264_rejects_when_ffmpeg_missing(fake_run_cmd, bypass_health, monkeypatch):
    monkeypatch.setattr(pv.shutil, "which", lambda x: None)
    _write_secret("s" * 48)
    with client.websocket_connect(
        "/m7a-preview/ws264?token=" + pv.day_token("s" * 48)
    ) as ws:
        msg = ws.receive_text()
    assert "ffmpeg" in msg


def test_ws264_rejects_bad_token(fake_run_cmd, bypass_health):
    _write_secret("s" * 48)
    with client.websocket_connect("/m7a-preview/ws264?token=wrong") as ws:
        msg = ws.receive_text()
    assert "token" in msg


def test_ws264_pipeline_emits_fmp4(fake_run_cmd, bypass_health, monkeypatch):
    """真 ffmpeg：JPEG 帧 → H.264 fMP4 字节流（ftyp/moof box 特征）。"""
    jpeg = _make_jpeg()
    port = _start_ws_upstream(jpeg)
    monkeypatch.setattr(cfg, "PREVIEW_UPSTREAM_PORT", port)
    _write_secret("s" * 48)
    tok = pv.day_token("s" * 48)
    buf = b""
    with client.websocket_connect(f"/m7a-preview/ws264?token={tok}&res=480p") as ws:
        for _ in range(40):
            buf += ws.receive_bytes()
            if b"ftyp" in buf or b"moof" in buf:
                break
    assert b"ftyp" in buf or b"moof" in buf, "H.264 输出缺少 MP4 box 特征"
