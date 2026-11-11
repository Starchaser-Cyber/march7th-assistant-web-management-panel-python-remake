"""V1.28.2：sock_json 必须能解 Docker 的 chunked 响应。

Bug 现场：`socket` 直连 /var/run/docker.sock 时，Docker Engine API 一律回
`Transfer-Encoding: chunked`（无 Content-Length）。原实现直接把 chunked 帧
（`1e9c\\r\\n{json}\\r\\n0\\r\\n\\r\\n`）交给 `json.loads`，必然抛 ValueError，
于是返回 `(200, None)`。调用方 `container_info` 看到的是「status 是 200 但 j 不是
dict」→ 走 `_cli_info` 回退。功能不出错，但：
  1. 每条采样多 fork 一个 docker 进程；
  2. 新加的 Health 字段只能靠 CLI 的 format 串取，HTTP 主路径形同虚设。

发现方式：`curl --unix-socket /var/run/docker.sock -D - /containers/x/json`
的响应头里有 `Transfer-Encoding: chunked`。
"""
from __future__ import annotations

import json
import socket
import sys
import threading
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from services import sysmetrics  # noqa: E402

_PAYLOAD = {"Id": "abc", "State": {"Running": True,
                                  "Health": {"Status": "healthy"}}}


def _chunk(data: bytes, size: int = 4096) -> bytes:
    """按给定大小切成 chunked 帧（含结尾 0 帧）。"""
    out = b""
    for i in range(0, len(data), size):
        part = data[i:i + size]
        out += b"%x\r\n" % len(part) + part + b"\r\n"
    return out + b"0\r\n\r\n"


def _serve(monkeypatch, body_frame: bytes, headers: bytes):
    """起一个 AF_UNIX 假 socket，返回一次响应后关闭。"""
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind("\0m7a-test-sock")          # 抽象命名空间，不落盘
    srv.listen(1)

    def _run():
        try:
            conn, _ = srv.accept()
            with conn:
                conn.recv(65536)
                conn.sendall(b"HTTP/1.1 200 OK\r\n" + headers +
                             b"\r\n" + body_frame)
        finally:
            srv.close()

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    monkeypatch.setattr(sysmetrics, "DOCKER_SOCK", "\0m7a-test-sock")
    return t


def test_sock_json_decodes_chunked(monkeypatch):
    raw = json.dumps(_PAYLOAD).encode()
    _serve(monkeypatch, _chunk(raw, 64),
           b"Content-Type: application/json\r\nTransfer-Encoding: chunked\r\n")
    st, j = sysmetrics.sock_json("/containers/x/json", 3.0)
    assert st == 200
    assert j == _PAYLOAD


def test_sock_json_plain_body_still_works(monkeypatch):
    """没有 Transfer-Encoding 的普通响应不能被 _dechunk 破坏。"""
    raw = json.dumps(_PAYLOAD).encode()
    _serve(monkeypatch, raw,
           b"Content-Type: application/json\r\nContent-Length: %d\r\n" % len(raw))
    st, j = sysmetrics.sock_json("/containers/x/json", 3.0)
    assert st == 200
    assert j == _PAYLOAD


def test_dechunk_direct():
    raw = b'{"a":1}'
    hdr = b"Transfer-Encoding: chunked\r\n"
    # 手工构造标准 chunked 帧：'{"a:' + '1}' 两段（共 8 字节）
    frame = b'4\r\n{\"a\"\r\n3\r\n:1}\r\n0\r\n\r\n'
    assert sysmetrics._dechunk(hdr, frame) == raw
    # helper 生成 vs 实现解析，互相验证
    assert sysmetrics._dechunk(hdr, _chunk(raw, 1)) == raw
    # 没有 chunked 头时原样返回（不误伤普通响应）
    assert sysmetrics._dechunk(b"", frame) == frame


def test_dechunk_non_chunked_passthrough():
    raw = b'{"a":1}'
    assert sysmetrics._dechunk(b"Content-Type: application/json\r\n", raw) == raw


def test_dechunk_malformed_returns_original():
    bad = b"zz\r\nnot-hex\r\n"
    assert sysmetrics._dechunk(b"Transfer-Encoding: chunked\r\n", bad) == bad


def test_container_info_uses_http_path(monkeypatch):
    """端到端：走 chunked socket 时 container_info 应拿到 health，且不再调 CLI。"""
    payload = {"Id": "abc", "State": {
        "Running": True, "StartedAt": "2026-11-11T12:00:00.000Z",
        "Health": {"Status": "unhealthy", "FailingStreak": 3,
                   "Log": [{"Start": "2026-11-11T12:05:00.000Z"}]},
    }}
    raw = json.dumps(payload).encode()
    _serve(monkeypatch, _chunk(raw),
           b"Content-Type: application/json\r\nTransfer-Encoding: chunked\r\n")
    monkeypatch.setattr(sysmetrics, "_INFO_CACHE", {})
    monkeypatch.setattr(sysmetrics, "_cli_info",
                        lambda name: (_ for _ in ()).throw(
                            AssertionError("不该回退 CLI")))
    info = sysmetrics.container_info("x", refresh=True)
    assert info["health"] == "unhealthy"
    assert info["health_fails"] == 3
