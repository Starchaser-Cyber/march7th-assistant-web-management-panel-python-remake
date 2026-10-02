"""端到端分发测试：登录分流到 Python handler，未登录/未迁移转发 mock PHP 上游。

环境（导入 app 前设置）：
- M7A_SESSION_DIR → 测试 session 目录
- M7A_PHP_UPSTREAM → 本地 mock PHP（http.server，记录收到的请求）
- M7A_PANEL_DIR    → 测试面板目录（data/ 落这里）
"""
import http.server
import json
import os
import sys
import tempfile
import threading
from pathlib import Path

# ===== 导入 app 前设置环境 =====
_TMP = Path(tempfile.mkdtemp(prefix="m7a_migr_test_"))
_SESSION_DIR = _TMP / "sessions"
_PANEL_DIR = _TMP / "panel"
_SESSION_DIR.mkdir()
_PANEL_DIR.mkdir()

RECEIVED = []          # mock 上游收到的 (path, headers)


class _MockPHP(http.server.BaseHTTPRequestHandler):
    def _handle(self):
        RECEIVED.append((self.path, dict(self.headers)))
        body = b"MOCK_PHP_PAGE"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST = _handle

    def log_message(self, *args):
        pass


_server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _MockPHP)
_UPSTREAM_PORT = _server.server_address[1]
threading.Thread(target=_server.serve_forever, daemon=True).start()

os.environ["M7A_SESSION_DIR"] = str(_SESSION_DIR)
os.environ["M7A_PHP_UPSTREAM"] = f"http://127.0.0.1:{_UPSTREAM_PORT}"
os.environ["M7A_PANEL_DIR"] = str(_PANEL_DIR)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient   # noqa: E402
from main import app                          # noqa: E402

client = TestClient(app)

# 已登录 session 文件
(_SESSION_DIR / "sess_authed").write_text("m7a_panel_auth|b:1;", encoding="utf-8")
(_SESSION_DIR / "sess_csrf").write_text(
    'm7a_panel_auth|b:1;m7a_panel_csrf|s:3:"abc";', encoding="utf-8")
AUTH_COOKIE = {"PHPSESSID": "authed"}
CSRF_COOKIE = {"PHPSESSID": "csrf"}
UNAUTH = {}


def _last_forward():
    """最后一次被转发到 mock 上游的请求。"""
    return RECEIVED[-1]


# ---------- 登录态 → Python 处理 ----------

def test_status_handled_by_python():
    RECEIVED.clear()
    r = client.get("/?ajax=status", cookies=AUTH_COOKIE)
    assert r.status_code == 200
    assert "text/plain" in r.headers["content-type"]
    assert not RECEIVED                       # 没有转发
    assert r.text != "MOCK_PHP_PAGE"


def test_log_json():
    r = client.get("/?ajax=log", cookies=AUTH_COOKIE)
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True
    assert d["files"] == []                   # 测试目录无日志
    assert d["lines"] == "(暂无日志文件，任务运行后会生成)"


def test_running_json():
    r = client.get("/?ajax=running", cookies=AUTH_COOKIE)
    d = r.json()
    assert r.status_code == 200
    assert d["running"] is False              # 本地无 docker
    assert d["task"] == ""
    assert isinstance(d["now"], int)


def test_history_json():
    r = client.get("/?ajax=history", cookies=AUTH_COOKIE)
    d = r.json()
    assert d["ok"] is True
    assert d["items"] == []
    assert d["today_count"] == 0
    assert len(d["week"]) == 7


def test_monitor_json():
    r = client.get("/?ajax=monitor&iv=60", cookies=AUTH_COOKIE)
    d = r.json()
    assert d["ok"] is True
    assert d["running"] is False
    assert isinstance(d["points"], list)
    assert d["interval"] == 60
    assert "host" in d


# ---------- 未登录 / 未迁移 → 转发 ----------

def test_unauth_renders_login_page():
    """M5-B：未登录请求与 PHP 一致 → Python 渲染登录页（不再转发）。"""
    RECEIVED.clear()
    r = client.get("/?ajax=status", cookies=UNAUTH)
    assert r.status_code == 200
    assert "auth-card" in r.text
    assert not RECEIVED


def test_unknown_ajax_forwards():
    RECEIVED.clear()
    r = client.get("/?ajax=not_migrated_yet", cookies=AUTH_COOKIE)   # 未迁移接口
    assert r.text == "MOCK_PHP_PAGE"
    assert _last_forward()[1].get("X-M7A-Fwd") == "1"


def test_m2_ajax_handled_by_python():
    """M2 起 doctor 不再转发：由 Python 返回 JSON。"""
    RECEIVED.clear()
    r = client.get("/?ajax=doctor", cookies=AUTH_COOKIE)
    assert r.status_code == 200
    assert not RECEIVED
    d = r.json()
    assert d["ok"] is True and len(d["items"]) == 10


def test_page_render_by_python():
    """M5-B：面板主页面由 Python/Jinja 渲染。"""
    RECEIVED.clear()
    r = client.get("/", cookies=AUTH_COOKIE)
    assert r.status_code == 200
    assert "<!-- ===== 概览 ===== -->" in r.text
    assert "/static/panel.css" in r.text
    assert not RECEIVED


def test_static_served_by_python():
    """M5-B：/static/* 由 Python 静态服务直接响应，不再转发。"""
    RECEIVED.clear()
    r = client.get("/static/panel.css", cookies=AUTH_COOKIE)
    assert r.status_code == 200
    assert "text/css" in r.headers["content-type"]
    assert not RECEIVED
    r404 = client.get("/static/app.css", cookies=AUTH_COOKIE)   # 不存在 → 404，不转发
    assert r404.status_code == 404
    assert not RECEIVED


def test_post_forwards():
    """未实现的 action 仍走绞杀者转发回 PHP。"""
    RECEIVED.clear()
    r = client.post("/", data={"action": "action_not_migrated", "csrf": "abc"},
                    cookies=CSRF_COOKIE)
    assert r.text == "MOCK_PHP_PAGE"
    assert RECEIVED[0][0] == "/"


def test_post_login_handled_by_python():
    """M2 起 login 由 Python 处理：错密码 → 整页重渲染 + 横幅。"""
    RECEIVED.clear()
    r = client.post("/", data={"action": "login", "pass": "x"}, cookies=AUTH_COOKIE)
    assert r.status_code == 200
    assert "密码错误" in r.text
    assert "<!-- ===== 概览 ===== -->" in r.text  # 已登录会话 → Python 渲染面板页+横幅
