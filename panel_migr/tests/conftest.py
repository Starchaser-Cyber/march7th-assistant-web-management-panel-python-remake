"""共享测试环境：在导入 app 前设置环境变量，并起一个 mock PHP 上游。

- 独立运行任何测试文件时：env 生效，面板/会话数据落在 tests/_tmp/ 下
- 与 test_dispatch.py 一起跑时：它会在 config 首次导入前用自己的 tmp 覆盖 env，
  本文件的 env 被忽略，行为仍一致（各测试一律从 config 模块读取运行时路径）。
"""
import http.server
import os
import threading
from pathlib import Path

import pytest

_TMP = Path(__file__).resolve().parent / "_tmp"
_SESSION_DIR = _TMP / "sessions"
_PANEL_DIR = _TMP / "panel"
_SESSION_DIR.mkdir(parents=True, exist_ok=True)
_PANEL_DIR.mkdir(parents=True, exist_ok=True)

MOCK_BODY = b"MOCK_PHP_PAGE"


class _MockPHP(http.server.BaseHTTPRequestHandler):
    def _handle(self):
        body = MOCK_BODY
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST = _handle

    def log_message(self, *args):
        pass


def _ensure_mock():
    try:
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _MockPHP)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server.server_address[1]
    except OSError:
        return None


_MOCK_PORT = _ensure_mock()
if _MOCK_PORT:
    os.environ["M7A_PHP_UPSTREAM"] = f"http://127.0.0.1:{_MOCK_PORT}"
os.environ.setdefault("M7A_SESSION_DIR", str(_SESSION_DIR))
os.environ.setdefault("M7A_PANEL_DIR", str(_PANEL_DIR))
os.environ.setdefault("M7A_EVENTS_BG", "0")   # M4：测试不起后台 runner


@pytest.fixture(autouse=True)
def _reset_login_guard():
    """全局：每测试前后清空登录限速状态（TestClient 同源 IP 会共享失败计数）。"""
    from services import loginguard
    loginguard.reset()
    yield
    loginguard.reset()
