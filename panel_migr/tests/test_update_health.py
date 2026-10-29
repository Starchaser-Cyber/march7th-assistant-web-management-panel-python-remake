"""v1.24.1 加固：更新包结构校验 + 重启健康探测。

对照 services/updateops 的两条新防线：
1) _zip_struct_ok 必须含 routers/__init__.py（否则 main.py 顶层 import 直接炸）
2) _restart_and_health_check：systemd 部署要同步重启+轮询 HTTP 200；非 systemd 部署跳过
"""
from __future__ import annotations

import io
import time
import zipfile

import pytest

import config as cfg
from services import updateops as uo


# ===== helpers =====

def _make_zip(items: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, body in items.items():
            zf.writestr(name, body)
    return buf.getvalue()


_REQUIRED = {
    "panel_migr/main.py": b"# main",
    "panel_migr/config.py": b'M7A_PANEL_VERSION", "1.24.1"',
    "panel_migr/static/panel.js": b"// js",
    "panel_migr/routers/__init__.py": b"from routers.readonly import GET_HANDLERS",
}


def _run_stub(out: str, code: int = 0):
    return lambda cmd, timeout=30: {"code": code, "out": out}


# ===== _zip_struct_ok =====

class TestZipStructOk:

    def test_full_package_passes(self):
        data = _make_zip(_REQUIRED)
        assert uo._zip_struct_ok(data) is True

    def test_missing_init_returns_false(self):
        items = dict(_REQUIRED)
        del items["panel_migr/routers/__init__.py"]
        data = _make_zip(items)
        assert uo._zip_struct_ok(data) is False, "缺 routers/__init__.py 应判为结构异常"

    def test_missing_main_py_returns_false(self):
        items = dict(_REQUIRED)
        del items["panel_migr/main.py"]
        data = _make_zip(items)
        assert uo._zip_struct_ok(data) is False

    def test_bad_zipfile_returns_false(self):
        assert uo._zip_struct_ok(b"not a zip") is False


# ===== _restart_and_health_check =====

def _call_health(monkeypatch, run_out: str, run_code: int,
                 http_code: int = 0, http_calls: int = 0):
    """构造 _restart_and_health_check 调用，返回其结果 + 调用计数。"""
    counts = {"run": 0, "http": 0}

    def _run(cmd, timeout=30):
        counts["run"] += 1
        if "is-active" in cmd:
            return {"code": run_code, "out": run_out}
        # restart
        return {"code": 0, "out": ""}

    def _http(url, timeout=5):
        counts["http"] += 1
        # v1.24.4（P0-6）：必须校验探测端口。此前这个假 _http 忽略 url 参数，
        # 硬编码 9999 的端口 bug 因此从未被测试发现。端口取自 cfg.LISTEN_PORT。
        assert url == f"http://127.0.0.1:{cfg.LISTEN_PORT}/", url
        return {"code": http_code, "body": ""} if counts["http"] <= http_calls else {"code": 0, "body": ""}

    monkeypatch.setattr(uo, "run_cmd", _run)
    monkeypatch.setattr(uo, "http_get", _http)
    monkeypatch.setattr(time, "sleep", lambda *_: None)
    return uo._restart_and_health_check(retries=3, interval=0), counts


class TestRestartAndHealthCheck:

    def test_systemd_active_probe_ok(self, monkeypatch):
        r, c = _call_health(monkeypatch, "active", 0, http_code=200, http_calls=1)
        assert r["ok"] is True
        assert r["skipped"] is False
        assert r["probe_ok"] is True
        assert r["msg"] == ""
        assert c["run"] == 2  # is-active + restart

    def test_systemd_active_probe_fails_returns_reason(self, monkeypatch):
        r, c = _call_health(monkeypatch, "active", 0, http_code=0, http_calls=0)
        assert r["ok"] is False
        assert r["skipped"] is False
        assert r["probe_ok"] is False
        assert "健康探测失败" in r["msg"]
        assert "3 次" in r["msg"]

    def test_non_systemd_skipped(self, monkeypatch):
        # systemctl is-active 返回 inactive + code=4
        r, c = _call_health(monkeypatch, "inactive", 4, http_code=200, http_calls=0)
        assert r["ok"] is True
        assert r["skipped"] is True
        assert r["probe_ok"] is False
        assert "手动" in r["msg"]
        # 不应尝试 restart 或 http 探测
        assert c["run"] == 1  # 仅 is-active
        assert c["http"] == 0

    def test_restart_cmd_failed(self, monkeypatch):
        def _run(cmd, timeout=30):
            if "is-active" in cmd:
                return {"code": 0, "out": "active"}
            return {"code": 1, "out": "Unit m7a-panel.service not found."}

        monkeypatch.setattr(uo, "run_cmd", _run)
        monkeypatch.setattr(uo, "http_get", lambda *_: {"code": 0, "body": ""})
        monkeypatch.setattr(time, "sleep", lambda *_: None)
        r = uo._restart_and_health_check()
        assert r["ok"] is False
        assert "restart" in r["msg"].lower() or "失败" in r["msg"]

    def test_probe_retries_until_ok(self, monkeypatch):
        # 第一次失败，第二次成功
        attempts = {"n": 0}

        def _run(cmd, timeout=30):
            if "is-active" in cmd:
                return {"code": 0, "out": "active"}
            return {"code": 0, "out": ""}

        def _http(url, timeout=5):
            attempts["n"] += 1
            if attempts["n"] == 1:
                return {"code": 0, "body": ""}
            return {"code": 200, "body": ""}

        monkeypatch.setattr(uo, "run_cmd", _run)
        monkeypatch.setattr(uo, "http_get", _http)
        monkeypatch.setattr(time, "sleep", lambda *_: None)
        r = uo._restart_and_health_check(retries=3, interval=0)
        assert r["ok"] is True
        assert r["probe_ok"] is True
        assert attempts["n"] == 2
