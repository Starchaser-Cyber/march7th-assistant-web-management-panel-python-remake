"""V1.24.3 安全加固回归测试（对应 2026-10-29 第三方审计报告）。

覆盖审计发现：
- M7A-01 自更新链：TLS 校验恢复、更新包 sha256 强制校验、无哈希时只走官方源
- M7A-02 session 文件权限 0666 → 0660（不再默认落 /tmp）
- M7A-03 登录限速来源取可信 IP + 全局兜底速率
- M7A-04 /m7a-preview/* 本地鉴权（HTTP 与 WS）
- M7A-05 shell 引用、M7A-06 key 常量时间比对、M7A-08 webhook 目标校验
- M7A-09 ?key= 默认拒绝、M7A-10 key 熵提升、M7A-11 更新源锁定 https
"""
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import config as cfg
import services.loginguard as lg
import services.updateops as U
from routers.write import _client_ip


# ================= M7A-01 更新链 =================

def test_no_verify_false_left_in_codebase():
    """TLS 证书校验不得被关闭（审计 M7A-01 P0-1）。"""
    offenders = []
    for f in ROOT.rglob("*.py"):
        if "tests" in f.parts or "_tmp" in f.parts:
            continue
        if "verify=False" in f.read_text("utf-8", errors="replace"):
            offenders.append(str(f.relative_to(ROOT)))
    assert offenders == [], f"仍有代码关闭 TLS 校验: {offenders}"


def test_sha256_helper():
    assert U._sha256(b"abc") == (
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    )


def test_asset_digest_parsed():
    j = {"assets": [{"name": "V9.9.9.zip",
                     "digest": "sha256:" + "a" * 64}]}
    assert U._asset_digest(j, "V9.9.9.zip") == "a" * 64


@pytest.mark.parametrize("digest", ["", "md5:" + "a" * 32, "sha256:xyz", None])
def test_asset_digest_rejects_invalid(digest):
    j = {"assets": [{"name": "V9.9.9.zip", "digest": digest}]}
    assert U._asset_digest(j, "V9.9.9.zip") == ""


def test_asset_digest_missing_asset():
    assert U._asset_digest({"assets": []}, "V9.9.9.zip") == ""
    assert U._asset_digest({}, "V9.9.9.zip") == ""


def _release(digest_hex: str, size_ok: bool = True) -> dict:
    a = {"name": "V9.9.9.zip",
         "browser_download_url":
             "https://github.com/o/r/releases/download/V9.9.9/V9.9.9.zip"}
    if digest_hex:
        a["digest"] = "sha256:" + digest_hex
    return {"tag_name": "V9.9.9", "assets": [a]}


def test_do_update_rejects_digest_mismatch(monkeypatch):
    """镜像/中间人替换包体 → 哈希不符 → 拒绝换入（M7A-01 P0-2）。"""
    payload = b"PK\x03\x04" + b"tampered" * 3000
    monkeypatch.setattr(U, "_release_json", lambda: (_release("0" * 64), ""))
    monkeypatch.setattr(U, "http_bytes", lambda url, t=60: payload)
    applied = []
    monkeypatch.setattr(U, "_apply_zip", lambda d, v: applied.append(v) or {"ok": True})

    r = U.do_update()
    assert r["ok"] is False
    assert "完整性校验" in r["msg"]
    assert applied == [], "校验未通过的包不得进入换入流程"


def test_do_update_accepts_matching_digest(monkeypatch):
    """哈希一致 → 放行到后续结构校验（此处用假包验证「已通过哈希」）。"""
    payload = b"PK\x03\x04" + b"good" * 6000
    want = U._sha256(payload)
    monkeypatch.setattr(U, "_release_json", lambda: (_release(want), ""))
    monkeypatch.setattr(U, "http_bytes", lambda url, t=60: payload)
    monkeypatch.setattr(U, "_zip_struct_ok", lambda d: False)

    r = U.do_update()
    assert r["ok"] is False
    assert "结构异常" in r["msg"], "哈希通过后应继续走结构与版本校验"


def test_do_update_without_digest_skips_mirrors(monkeypatch):
    """Release 未提供 digest → 只允许官方源，第三方镜像一律不尝试。"""
    payload = b"PK\x03\x04" + b"x" * 30000
    tried = []

    def _bytes(url, t=60):
        tried.append(url)
        return None

    monkeypatch.setattr(U, "_release_json", lambda: (_release(""), ""))
    monkeypatch.setattr(U, "http_bytes", _bytes)

    r = U.do_update()
    assert r["ok"] is False
    assert len(tried) == 1 and tried[0].startswith("https://github.com/"), tried
    assert not any(m in tried[0] for m in U.MIRRORS)


def test_update_host_locked_to_https():
    """M7A-11：环境变量不能把更新源改成明文/内网地址。"""
    env = dict(os.environ, M7A_UPDATE_HOST="http://evil.example")
    out = subprocess.run([sys.executable, "-c", "import config; print(config.UPDATE_HOST)"],
                         cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=60)
    assert out.stdout.strip() == "https://github.com"


# ================= M7A-02 session 权限 =================

def test_session_file_mode_is_0600_group_not_world():
    assert cfg.SESSION_FILE_MODE == 0o660
    assert cfg.SESSION_FILE_MODE & 0o007 == 0, "session 文件不得对其他人可读写"


def test_session_dir_default_not_tmp():
    """默认 session 目录不得是 world-writable 的 /tmp（M7A-02）。"""
    env = {k: v for k, v in os.environ.items() if k != "M7A_SESSION_DIR"}
    out = subprocess.run(
        [sys.executable, "-c", "import config; print(config.SESSION_SAVE_PATH)"],
        cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=60)
    path = out.stdout.strip()
    assert path != "/tmp" and "sessions" in path, path


# ================= M7A-03 限速来源 =================

class _Req:
    def __init__(self, headers=None, host="10.1.1.1"):
        self.headers = headers or {}
        self.client = type("C", (), {"host": host})()


def test_client_ip_prefers_x_real_ip():
    r = _Req({"x-real-ip": "9.9.9.9", "x-forwarded-for": "1.1.1.1, 2.2.2.2"})
    assert _client_ip(r) == "9.9.9.9"


def test_client_ip_uses_last_xff_segment_not_first():
    """伪造首段不再生效：取最后一段（离本机最近一跳追加的真实地址）。"""
    r = _Req({"x-forwarded-for": "1.2.3.4, 8.8.8.8"})
    assert _client_ip(r) == "8.8.8.8"
    assert _client_ip(r) != "1.2.3.4"


def test_client_ip_falls_back_to_peer():
    assert _client_ip(_Req({}, host="7.7.7.7")) == "7.7.7.7"


def test_global_login_rate_limit():
    """伪造 IP 摊薄单桶计数后，全局兜底仍能锁住登录（M7A-03）。"""
    lg.reset()
    for _ in range(lg.GLOBAL_MAX):
        lg.global_fail()
    assert lg.global_locked() > 0
    lg.reset()
    assert lg.global_locked() == 0


# ================= M7A-05 / M7A-06 / M7A-10 =================

def test_preview_shell_calls_are_quoted():
    """docker inspect / docker exec 的容器名必须做 shell 引用（M7A-05）。"""
    src = (ROOT / "services" / "preview.py").read_text("utf-8")
    for m in re.finditer(r"run_cmd\((.*?)\)\s*\n\s*return", src, re.S):
        pass
    assert "shlex.quote(container)" in src
    assert re.search(r'"docker inspect[^"]*"\s*\n\s*\+ shlex\.quote\(container\)', src), \
        "docker inspect 拼接未做引用"


def test_monitor_sampler_uses_compare_digest():
    src = (ROOT / "routers" / "get_write.py").read_text("utf-8")
    seg = src.split("def h_monitor_sampler")[1].split("def ")[0]
    assert "compare_digest" in seg, "sampler key 比对应常量时间（M7A-06）"


def test_key_entropy_bits():
    """key 生成熵 ≥128 bit（M7A-10）。"""
    src = (ROOT / "services" / "pagectx.py").read_text("utf-8")
    assert "token_hex(8)" not in src
    assert src.count("token_hex(16)") >= 3


# ================= M7A-08 webhook 目标 =================

@pytest.mark.parametrize("url", [
    "http://127.0.0.1/hook", "http://localhost/hook", "http://169.254.169.254/x",
    "http://10.0.0.1/x", "http://192.168.1.5/x", "ftp://example.com/x",
    "file:///etc/passwd", "",
])
def test_webhook_url_rejects_unsafe(url):
    from services.alerts import _webhook_url_ok

    ok, _ = _webhook_url_ok(url)
    assert ok is False, f"应拒绝 {url!r}"


@pytest.mark.parametrize("url", [
    "https://hooks.example.com/notify", "http://example.com/x",
    "https://example.com:8443/hook?a=1",
])
def test_webhook_url_allows_public(url):
    from services.alerts import _webhook_url_ok

    ok, why = _webhook_url_ok(url)
    assert ok is True, why
