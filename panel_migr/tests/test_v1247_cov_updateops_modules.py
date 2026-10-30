"""v1.24.7 优化（方向 B/C 补充）：补齐 `services/updateops/` 拆包后低覆盖模块。

拆包把 1,894 行拆成 12 个模块后，几个模块的覆盖率掉到了 30~67%：
`backups.py` 29%、`ghcr.py` 30%、`release.py` 46%、`http.py` 67%、`restart.py` 67%。
它们恰好是**在线更新链路**——出事时最难现场调试的一段，所以补齐。

本文件覆盖：
  `http.py`     — 三条抓取函数的成功/失败/超限/超时路径
  `release.py`  — URL 拼装、资产选择（Q-10 回归）、digest 解析、check_update
  `ghcr.py`     — 鉴权头幂等（P1-6 回归）、manifest 三级回退、异常兜底
  `backups.py`  — 列表解析、清理、索引备份、两种回滚
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

import config as cfg
from services import updateops as U
from services.updateops import backups as B
from services.updateops import ghcr as G
from services.updateops import http as H
from services.updateops import release as R


# ====================================================================
# http.py
# ====================================================================

class _Resp:
    def __init__(self, status_code=200, text="", headers=None, content=b"", json_data=None):
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}
        self.content = content
        self._json = json_data

    def json(self):
        if self._json is not None:
            return self._json
        return json.loads(self.text)


class _StreamCtx:
    """模拟 httpx.stream(...) 的上下文管理器。

    `http_bytes` 用 `with httpx.stream(...) as r:` 然后 `r.iter_bytes()`，
    所以这里要让被返回的对象带 `iter_bytes`。
    """

    def __init__(self, resp, chunks):
        self._resp = resp
        self._chunks = chunks

    def __enter__(self):
        self._resp.iter_bytes = lambda: iter(self._chunks)
        return self._resp

    def __exit__(self, *a):
        return False


def _patch_httpx(monkeypatch, **fns):
    for name, fn in fns.items():
        monkeypatch.setattr(U.httpx, name, fn)


def test_http_get_ok(monkeypatch):
    _patch_httpx(monkeypatch, get=lambda *a, **k: _Resp(200, "hello"))
    assert H.http_get("http://x") == {"code": 200, "body": "hello"}


def test_http_get_connection_error(monkeypatch):
    def boom(*a, **k):
        raise OSError("refused")

    _patch_httpx(monkeypatch, get=boom)
    assert H.http_get("http://x") == {"code": 0, "body": ""}


def test_http_get_sends_ua(monkeypatch):
    seen = {}

    def fake(url, **kw):
        seen.update(kw)
        return _Resp(200, "")

    _patch_httpx(monkeypatch, get=fake)
    H.http_get("http://x")
    assert seen["headers"]["User-Agent"].startswith("M7A-Panel/")


def test_http_get_default_timeout_is_8(monkeypatch):
    seen = {}
    _patch_httpx(monkeypatch, get=lambda u, **k: seen.update(k) or _Resp(200, ""))
    H.http_get("http://x")
    assert "timeout" in seen


def test_http_bytes_ok(monkeypatch):
    _patch_httpx(monkeypatch, stream=lambda *a, **k: _StreamCtx(
        _Resp(200, headers={"content-length": "5"}), [b"ab", b"cde"]))
    assert H.http_bytes("http://x") == b"abcde"


def test_http_bytes_non_200(monkeypatch):
    _patch_httpx(monkeypatch, stream=lambda *a, **k: _StreamCtx(_Resp(404), []))
    assert H.http_bytes("http://x") is None


def test_http_bytes_content_length_over_limit(monkeypatch):
    """P1-8：有 Content-Length 且超限时直接放弃，不读包体。"""
    big = str(H._MAX_DOWNLOAD + 1)
    _patch_httpx(monkeypatch, stream=lambda *a, **k: _StreamCtx(
        _Resp(200, headers={"content-length": big}), [b"x"]))
    assert H.http_bytes("http://x") is None


def test_http_bytes_streaming_over_limit(monkeypatch):
    """P1-8：没有 Content-Length 时靠边读边累计兜住。"""
    chunk = b"x" * (H._MAX_DOWNLOAD // 2 + 1)
    _patch_httpx(monkeypatch, stream=lambda *a, **k: _StreamCtx(
        _Resp(200, headers={}), [chunk, chunk]))
    assert H.http_bytes("http://x") is None


def test_http_bytes_bad_content_length(monkeypatch):
    _patch_httpx(monkeypatch, stream=lambda *a, **k: _StreamCtx(
        _Resp(200, headers={"content-length": "abc"}), [b"ok"]))
    assert H.http_bytes("http://x") == b"ok"


def test_http_bytes_empty_body(monkeypatch):
    _patch_httpx(monkeypatch, stream=lambda *a, **k: _StreamCtx(_Resp(200), []))
    assert H.http_bytes("http://x") is None


def test_http_bytes_exception(monkeypatch):
    def boom(*a, **k):
        raise OSError("down")

    _patch_httpx(monkeypatch, stream=boom)
    assert H.http_bytes("http://x") is None


def test_http_post_form(monkeypatch):
    seen = {}
    _patch_httpx(monkeypatch, post=lambda u, **k: seen.update(k) or _Resp(200, "ok"))
    assert H.http_post("http://x", {"a": 1}) == {"code": 200, "body": "ok"}
    assert "data" in seen
    assert seen["headers"]["Content-Type"].startswith("application/x-www-form-urlencoded")


def test_http_post_json(monkeypatch):
    seen = {}
    _patch_httpx(monkeypatch, post=lambda u, **k: seen.update(k) or _Resp(200, "ok"))
    H.http_post("http://x", {"a": 1}, as_json=True)
    assert seen["headers"]["Content-Type"] == "application/json"
    assert json.loads(seen["content"]) == {"a": 1}


def test_http_post_exception(monkeypatch):
    def boom(*a, **k):
        raise OSError("down")

    _patch_httpx(monkeypatch, post=boom)
    assert H.http_post("http://x", {}) == {"code": 0, "body": "request failed"}


def test_probe_ok_200(monkeypatch):
    _patch_httpx(monkeypatch, get=lambda *a, **k: _Resp(200))
    assert H._probe("http://x") == {"code": 200, "ok": True}


def test_probe_ok_206(monkeypatch):
    _patch_httpx(monkeypatch, get=lambda *a, **k: _Resp(206))
    assert H._probe("http://x")["ok"] is True


def test_probe_404(monkeypatch):
    _patch_httpx(monkeypatch, get=lambda *a, **k: _Resp(404))
    assert H._probe("http://x") == {"code": 404, "ok": False}


def test_probe_exception(monkeypatch):
    def boom(*a, **k):
        raise OSError("x")

    _patch_httpx(monkeypatch, get=boom)
    assert H._probe("http://x") == {"code": 0, "ok": False}


def test_probe_uses_range_header(monkeypatch):
    seen = {}
    _patch_httpx(monkeypatch, get=lambda u, **k: seen.update(k) or _Resp(206))
    H._probe("http://x")
    assert seen["headers"]["Range"] == "bytes=0-0"


# ====================================================================
# release.py
# ====================================================================

def test_update_api_url_github(monkeypatch):
    monkeypatch.setattr(U.cfg, "UPDATE_TYPE", "github")
    monkeypatch.setattr(U.cfg, "UPDATE_OWNER", "o")
    monkeypatch.setattr(U.cfg, "UPDATE_REPO", "r")
    assert R.update_api_url() == "https://api.github.com/repos/o/r/releases/latest"


def test_update_api_url_gitea(monkeypatch):
    monkeypatch.setattr(U.cfg, "UPDATE_TYPE", "gitea")
    monkeypatch.setattr(U.cfg, "UPDATE_HOST", "https://gitea.example/")
    monkeypatch.setattr(U.cfg, "UPDATE_OWNER", "o")
    monkeypatch.setattr(U.cfg, "UPDATE_REPO", "r")
    assert R.update_api_url() == "https://gitea.example/api/v1/repos/o/r/releases/latest"


def test_update_raw_url_github(monkeypatch):
    monkeypatch.setattr(U.cfg, "UPDATE_TYPE", "github")
    monkeypatch.setattr(U.cfg, "UPDATE_OWNER", "o")
    monkeypatch.setattr(U.cfg, "UPDATE_REPO", "r")
    monkeypatch.setattr(U.cfg, "UPDATE_BRANCH", "main")
    assert "raw.githubusercontent.com" in R.update_raw_url()
    assert R.update_raw_url().endswith("/main/index.php")


def test_update_raw_url_gitea(monkeypatch):
    monkeypatch.setattr(U.cfg, "UPDATE_TYPE", "gitea")
    monkeypatch.setattr(U.cfg, "UPDATE_HOST", "https://g.example")
    monkeypatch.setattr(U.cfg, "UPDATE_OWNER", "o")
    monkeypatch.setattr(U.cfg, "UPDATE_REPO", "r")
    monkeypatch.setattr(U.cfg, "UPDATE_BRANCH", "dev")
    assert "raw/branch/dev/index.php" in R.update_raw_url()


def test_update_raw_urls_gitea_single(monkeypatch):
    monkeypatch.setattr(U.cfg, "UPDATE_TYPE", "gitea")
    monkeypatch.setattr(U.cfg, "UPDATE_HOST", "https://g.example")
    assert len(R.update_raw_urls()) == 1


def test_update_raw_urls_github_has_mirrors(monkeypatch):
    monkeypatch.setattr(U.cfg, "UPDATE_TYPE", "github")
    urls = R.update_raw_urls()
    assert len(urls) == 1 + len(R.MIRRORS)
    assert all("?t=" in u for u in urls)


# ---- 资产选择：Q-10 回归（最重要）----

def _asset(name, url="http://d/"):
    return {"name": name, "browser_download_url": url + name}


def test_zip_asset_prefers_exact_release_zip():
    """Q-10 回归：同时挂发布包与审计包时，必须选发布包。"""
    j = {"assets": [_asset("V1.24.6-audit.zip"), _asset("V1.24.6.zip")]}
    url, name = R._zip_asset(j)
    assert name == "V1.24.6.zip"
    assert url.endswith("V1.24.6.zip")


def test_zip_asset_ignores_audit_even_when_first():
    j = {"assets": [_asset("V1.24.6-audit.zip"), _asset("m7a-audit-report.zip"),
                    _asset("V1.24.6.zip")]}
    assert R._zip_asset(j)[1] == "V1.24.6.zip"


def test_zip_asset_fallback_when_no_exact():
    """旧命名（没有 V{ver}.zip）时退到「不含 audit/report/handoff」。"""
    j = {"assets": [_asset("panel-audit.zip"), _asset("panel-release.zip")]}
    assert R._zip_asset(j)[1] == "panel-release.zip"


def test_zip_asset_fallback_to_version_prefix():
    j = {"assets": [_asset("audit-report.zip"), _asset("V2.zip")]}
    assert R._zip_asset(j)[1] == "V2.zip"


def test_zip_asset_fallback_to_first_zip():
    j = {"assets": [_asset("handoff.zip"), _asset("notes.txt")]}
    assert R._zip_asset(j)[1] == "handoff.zip"


def test_zip_asset_no_zip():
    assert R._zip_asset({"assets": [_asset("x.tar.gz")]}) == ("", "")


def test_zip_asset_empty():
    assert R._zip_asset({}) == ("", "")
    assert R._zip_asset(None) == ("", "")


def test_zip_asset_skips_non_dict_assets():
    j = {"assets": ["junk", None, _asset("V1.0.zip")]}
    assert R._zip_asset(j)[1] == "V1.0.zip"


def test_ver_zip_regex_accepts():
    for n in ("V1.24.6.zip", "v1.0.zip", "V2.zip", "V1.24.6.1.zip"):
        assert R._VER_ZIP_RE.match(n), n


def test_ver_zip_regex_rejects():
    for n in ("V1.24.6-audit.zip", "V1.24.6-report.zip", "panel_1.0.zip",
              "V1.24.6.zip.sig", "audit-V1.24.6.zip"):
        assert not R._VER_ZIP_RE.match(n), n


# ---- 下载地址与 digest ----

def test_zip_urls_gitea_single(monkeypatch):
    monkeypatch.setattr(U.cfg, "UPDATE_TYPE", "gitea")
    assert R._zip_urls("http://z") == [("官方源", "http://z")]


def test_zip_urls_github_with_mirrors(monkeypatch):
    monkeypatch.setattr(U.cfg, "UPDATE_TYPE", "github")
    out = R._zip_urls("http://z")
    assert out[0] == ("官方源", "http://z")
    assert len(out) == 1 + len(R.MIRRORS)
    assert all(u.endswith("http://z") for _, u in out[1:])
    assert all(u.startswith(tuple(R.MIRRORS)) for _, u in out[1:])


def test_sha256_known_value():
    assert R._sha256(b"") == (
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855")


def test_asset_digest_valid():
    d = "a" * 64
    j = {"assets": [{"name": "V1.zip", "digest": "sha256:" + d}]}
    assert R._asset_digest(j, "V1.zip") == d


def test_asset_digest_uppercase_normalized():
    j = {"assets": [{"name": "V1.zip", "digest": "SHA256:" + "A" * 64}]}
    assert R._asset_digest(j, "V1.zip") == "a" * 64


def test_asset_digest_missing_field():
    assert R._asset_digest({"assets": [{"name": "V1.zip"}]}, "V1.zip") == ""


def test_asset_digest_bad_prefix():
    j = {"assets": [{"name": "V1.zip", "digest": "sha512:" + "a" * 64}]}
    assert R._asset_digest(j, "V1.zip") == ""


def test_asset_digest_bad_hex():
    j = {"assets": [{"name": "V1.zip", "digest": "sha256:zzzz"}]}
    assert R._asset_digest(j, "V1.zip") == ""


def test_asset_digest_wrong_name():
    j = {"assets": [{"name": "V2.zip", "digest": "sha256:" + "a" * 64}]}
    assert R._asset_digest(j, "V1.zip") == ""


def test_asset_digest_empty_name():
    assert R._asset_digest({"assets": []}, "") == ""


def test_asset_digest_not_dict():
    assert R._asset_digest(None, "x") == ""


# ---- check_update ----

def test_check_update_disabled(monkeypatch):
    monkeypatch.setattr(U.cfg, "UPDATE_ENABLED", False)
    assert R.check_update() == {"ok": True, "enabled": False}


def test_check_update_http_fail(monkeypatch):
    monkeypatch.setattr(U.cfg, "UPDATE_ENABLED", True)
    monkeypatch.setattr(U, "http_get", lambda *a, **k: {"code": 0, "body": ""})
    r = R.check_update()
    assert r["ok"] is False and "更新源连接失败" in r["err"]


def test_check_update_bad_json(monkeypatch):
    monkeypatch.setattr(U.cfg, "UPDATE_ENABLED", True)
    monkeypatch.setattr(U, "http_get", lambda *a, **k: {"code": 200, "body": "<html>"})
    r = R.check_update()
    assert r["ok"] is False and "返回数据异常" in r["err"]


def test_check_update_missing_tag(monkeypatch):
    monkeypatch.setattr(U.cfg, "UPDATE_ENABLED", True)
    monkeypatch.setattr(U, "http_get",
                        lambda *a, **k: {"code": 200, "body": json.dumps({"x": 1})})
    assert R.check_update()["ok"] is False


def test_check_update_ok_with_digest(monkeypatch):
    monkeypatch.setattr(U.cfg, "UPDATE_ENABLED", True)
    monkeypatch.setattr(U.cfg, "PANEL_VERSION", "1.0.0")
    j = {"tag_name": "V2.0.0", "body": "notes",
         "assets": [{"name": "V2.0.0.zip", "browser_download_url": "http://d/V2.0.0.zip",
                     "digest": "sha256:" + "b" * 64}]}
    monkeypatch.setattr(U, "http_get", lambda *a, **k: {"code": 200, "body": json.dumps(j)})
    monkeypatch.setattr(U, "panel_update_mode", lambda: "auto")
    r = R.check_update()
    assert r["ok"] is True
    assert r["has_update"] is True
    assert r["latest"] == "2.0.0"
    assert r["current"] == "1.0.0"
    assert r["asset"] == "V2.0.0.zip"
    assert r["digest_ok"] is True


def test_check_update_no_digest(monkeypatch):
    monkeypatch.setattr(U.cfg, "UPDATE_ENABLED", True)
    j = {"tag_name": "V1.24.6", "assets": [{"name": "V1.24.6.zip",
                                            "browser_download_url": "http://d/x"}]}
    monkeypatch.setattr(U, "http_get", lambda *a, **k: {"code": 200, "body": json.dumps(j)})
    monkeypatch.setattr(U, "panel_update_mode", lambda: "manual")
    r = R.check_update()
    assert r["digest_ok"] is False
    assert r["update_mode"] == "manual"


def test_panel_update_mode_manual(monkeypatch):
    monkeypatch.setattr("services.instances.panel_config",
                        lambda: {"update_mode": "manual"})
    assert R.panel_update_mode() == "manual"


def test_panel_update_mode_default_auto(monkeypatch):
    monkeypatch.setattr("services.instances.panel_config", lambda: {})
    assert R.panel_update_mode() == "auto"


# ---- test_update_source ----

def test_update_source_ok(monkeypatch):
    monkeypatch.setattr(U.cfg, "UPDATE_TYPE", "github")
    j = {"tag_name": "V1.0.0", "assets": [
        {"name": "V1.0.0.zip", "browser_download_url": "http://d/V1.0.0.zip"}]}
    monkeypatch.setattr(U, "http_get",
                        lambda *a, **k: {"code": 200, "body": json.dumps(j)})
    monkeypatch.setattr(U, "_probe", lambda u, *a, **k: {"code": 206, "ok": True})
    r = R.test_update_source()
    assert r["api"]["state"] == "ok_release"
    assert r["ok"] is True
    assert r["mirrors"][0]["name"] == "官方源"


def test_update_source_api_fail(monkeypatch):
    monkeypatch.setattr(U.cfg, "UPDATE_TYPE", "github")
    monkeypatch.setattr(U, "http_get", lambda *a, **k: {"code": 500, "body": ""})
    monkeypatch.setattr(U, "_probe", lambda u, *a, **k: {"code": 0, "ok": False})
    r = R.test_update_source()
    assert r["api"]["state"] == "fail"
    assert r["ok"] is False


def test_update_source_no_release_falls_back_to_raw(monkeypatch):
    """旧版 release 没有 zip 资产 → 回退探测 index.php raw。"""
    monkeypatch.setattr(U.cfg, "UPDATE_TYPE", "github")
    monkeypatch.setattr(U, "http_get",
                        lambda *a, **k: {"code": 200, "body": json.dumps({"x": 1})})
    monkeypatch.setattr(U, "_probe", lambda u, *a, **k: {"code": 0, "ok": False})
    r = R.test_update_source()
    assert r["api"]["state"] == "ok_no_release"
    assert r["raw"]["url"].endswith("index.php") or "?t=" in r["raw"]["url"]


# ====================================================================
# ghcr.py
# ====================================================================

def test_ghcr_auth_plain_token():
    assert G._ghcr_auth("abc") == "Bearer abc"


def test_ghcr_auth_idempotent_bearer():
    assert G._ghcr_auth("Bearer abc") == "Bearer abc"


def test_ghcr_auth_strips_authorization_prefix():
    """P1-6 回归：曾经把 `Authorization: Bearer x` 整串当 token 再拼一次前缀。"""
    assert G._ghcr_auth("Authorization: Bearer abc") == "Bearer abc"


def test_ghcr_auth_strips_authorization_no_bearer():
    assert G._ghcr_auth("authorization:abc") == "Bearer abc"


def test_ghcr_auth_empty():
    assert G._ghcr_auth("") == "Bearer "


def test_ghcr_auth_whitespace():
    assert G._ghcr_auth("  Bearer   abc  ") == "Bearer abc"


def test_ghcr_auth_none():
    assert G._ghcr_auth(None) == "Bearer "


def test_ghcr_config_created_ok(monkeypatch):
    _patch_httpx(monkeypatch, get=lambda *a, **k: _Resp(
        200, json_data={"created": "2026-10-16T15:41:59Z"}))
    assert G.ghcr_config_created("tok", "http://b", "sha256:x") == "2026-10-16T15:41:59Z"


def test_ghcr_config_created_non_200(monkeypatch):
    _patch_httpx(monkeypatch, get=lambda *a, **k: _Resp(404))
    assert G.ghcr_config_created("tok", "http://b", "sha256:x") == ""


def test_ghcr_config_created_not_dict(monkeypatch):
    _patch_httpx(monkeypatch, get=lambda *a, **k: _Resp(200, json_data=["a"]))
    assert G.ghcr_config_created("tok", "http://b", "sha256:x") == ""


def test_ghcr_config_created_exception(monkeypatch):
    def boom(*a, **k):
        raise OSError("x")

    _patch_httpx(monkeypatch, get=boom)
    assert G.ghcr_config_created("tok", "http://b", "sha256:x") == ""


def test_ghcr_latest_created_single_manifest(monkeypatch):
    """manifest 直接带 config.digest → 一次 blob 请求。"""
    seq = [
        _Resp(200, json_data={"token": "tok"}),
        _Resp(200, json_data={"config": {"digest": "sha256:cfg"}}),
        _Resp(200, json_data={"created": "2026-01-01T00:00:00Z"}),
    ]
    monkeypatch.setattr(U.httpx, "get", lambda *a, **k: seq.pop(0))
    assert G.ghcr_latest_created() == "2026-01-01T00:00:00Z"


def test_ghcr_latest_created_index_manifest(monkeypatch):
    """多架构 index → 取第一个 manifests[i].digest 再查一层。"""
    seq = [
        _Resp(200, json_data={"token": "tok"}),
        _Resp(200, json_data={"manifests": [{"digest": "sha256:idx"}]}),
        _Resp(200, json_data={"config": {"digest": "sha256:cfg"}}),
        _Resp(200, json_data={"created": "2026-02-02T00:00:00Z"}),
    ]
    monkeypatch.setattr(U.httpx, "get", lambda *a, **k: seq.pop(0))
    assert G.ghcr_latest_created() == "2026-02-02T00:00:00Z"


def test_ghcr_latest_created_no_token(monkeypatch):
    monkeypatch.setattr(U.httpx, "get", lambda *a, **k: _Resp(200, json_data={}))
    assert G.ghcr_latest_created() == ""


def test_ghcr_latest_created_manifest_404(monkeypatch):
    seq = [_Resp(200, json_data={"token": "tok"}), _Resp(404)]
    monkeypatch.setattr(U.httpx, "get", lambda *a, **k: seq.pop(0))
    assert G.ghcr_latest_created() == ""


def test_ghcr_latest_created_manifest_not_dict(monkeypatch):
    seq = [_Resp(200, json_data={"token": "tok"}), _Resp(200, json_data=["x"])]
    monkeypatch.setattr(U.httpx, "get", lambda *a, **k: seq.pop(0))
    assert G.ghcr_latest_created() == ""


def test_ghcr_latest_created_empty_manifests(monkeypatch):
    seq = [_Resp(200, json_data={"token": "tok"}), _Resp(200, json_data={"manifests": []})]
    monkeypatch.setattr(U.httpx, "get", lambda *a, **k: seq.pop(0))
    assert G.ghcr_latest_created() == ""


def test_ghcr_latest_created_exception(monkeypatch):
    def boom(*a, **k):
        raise OSError("x")

    monkeypatch.setattr(U.httpx, "get", boom)
    assert G.ghcr_latest_created() == ""


def test_ghcr_auth_header_never_doubled(monkeypatch):
    """P1-6 核心回归：实际发出的 Authorization 头只能有一个 Bearer 前缀。"""
    seen = []

    def fake_get(url, **kw):
        seen.append(kw.get("headers", {}).get("Authorization", ""))
        if "token" in url:
            return _Resp(200, json_data={"token": "tok"})
        if "manifests" in url:
            return _Resp(200, json_data={"config": {"digest": "sha256:c"}})
        return _Resp(200, json_data={"created": "T"})

    monkeypatch.setattr(U.httpx, "get", fake_get)
    G.ghcr_latest_created()
    auths = [a for a in seen if a]
    assert auths, "至少应发出带鉴权头的请求"
    for a in auths:
        assert a.count("Bearer") == 1, a
        assert "Authorization:" not in a, a


# ====================================================================
# backups.py
# ====================================================================

@pytest.fixture()
def bdir(tmp_path, monkeypatch):
    d = tmp_path / "backups"
    d.mkdir()
    monkeypatch.setattr(U, "BACKUP_DIR", d)
    monkeypatch.setattr(U, "INDEX_FILE", tmp_path / "index.php")
    monkeypatch.setattr(U.cfg, "BACKUP_KEEP", 3)
    return d


def test_backups_dir_creates(bdir):
    import shutil

    shutil.rmtree(bdir)
    assert U.backups_dir() == bdir
    assert bdir.is_dir()


def test_backups_list_empty(bdir):
    assert U.backups_list() == []


def test_backups_list_missing_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(U, "BACKUP_DIR", tmp_path / "nope")
    assert U.backups_list() == []


def test_backups_list_parses_both_kinds(bdir):
    (bdir / "index_1.19_20261001000000.php").write_text("<?php echo 1;" + "x" * 200)
    (bdir / "panel_1.20_20261002000000.zip").write_bytes(b"PK" + b"x" * 300)
    lst = U.backups_list()
    assert len(lst) == 2
    kinds = {i["kind"] for i in lst}
    assert kinds == {"index", "panel"}
    assert lst[0]["time"] >= lst[1]["time"]     # 倒序


def test_backups_list_skips_unmatched_names(bdir):
    (bdir / "notes.txt").write_text("x")
    (bdir / "panel_no_time.zip").write_bytes(b"x")
    assert U.backups_list() == []


def test_backups_list_has_expected_keys(bdir):
    (bdir / "panel_1.0_20260101000000.zip").write_bytes(b"x" * 10)
    item = U.backups_list()[0]
    for k in ("file", "path", "version", "time", "kind", "timeStr", "size"):
        assert k in item


def test_backups_list_bad_time_falls_back_to_mtime(bdir):
    f = bdir / "panel_1.0_99999999999999.zip"
    f.write_bytes(b"x" * 10)
    item = U.backups_list()[0]
    assert item["time"] > 0          # 用 st_mtime 兜住


def test_backups_prune_keeps_n(bdir, monkeypatch):
    for i in range(5):
        (bdir / f"panel_1.{i}_2026010100000{i}.zip").write_bytes(b"x")
    removed = U.backups_prune(keep=2)
    assert removed == 3
    assert len(U.backups_list()) == 2


def test_backups_prune_noop_when_under_limit(bdir):
    (bdir / "panel_1.0_20260101000000.zip").write_bytes(b"x")
    assert U.backups_prune(keep=5) == 0


def test_backups_prune_uses_config_default(bdir, monkeypatch):
    monkeypatch.setattr(U.cfg, "BACKUP_KEEP", 1)
    for i in range(3):
        (bdir / f"panel_1.{i}_2026010100000{i}.zip").write_bytes(b"x")
    assert U.backups_prune() == 2


def test_backups_prune_keep_floor_is_one(bdir):
    for i in range(3):
        (bdir / f"panel_1.{i}_2026010100000{i}.zip").write_bytes(b"x")
    assert U.backups_prune(keep=0) == 2      # max(1, 0) = 1


def test_backups_prune_does_not_delete_other_files(bdir):
    (bdir / "notes.txt").write_text("keep me")
    (bdir / "panel_1.0_20260101000000.zip").write_bytes(b"x")
    U.backups_prune(keep=1)
    assert (bdir / "notes.txt").is_file()


def test_backup_current_index_ok(bdir):
    (bdir.parent / "index.php").write_text("<?php // panel")
    p = U.backup_current_index("1.2.3")
    assert p != ""
    assert Path(p).is_file()
    assert Path(p).name.startswith("index_1.2.3_")


def test_backup_current_index_no_index_file(bdir):
    assert U.backup_current_index("1.2.3") == ""


def test_backup_current_index_sanitizes_version(bdir):
    (bdir.parent / "index.php").write_text("<?php")
    p = U.backup_current_index("1.2.3/../../evil")
    assert "/" not in Path(p).name


def test_backup_current_index_empty_version_uses_panel_version(bdir, monkeypatch):
    monkeypatch.setattr(U.cfg, "PANEL_VERSION", "9.9.9")
    (bdir.parent / "index.php").write_text("<?php")
    assert Path(U.backup_current_index("")).name.startswith("index_9.9.9_")


def test_backup_current_index_sets_mode_644(bdir):
    (bdir.parent / "index.php").write_text("<?php")
    p = Path(U.backup_current_index("1.0"))
    assert oct(p.stat().st_mode & 0o777) == "0o644"


# ---- 回滚 ----

def test_backup_rollback_empty_name():
    assert U.backup_rollback("")["ok"] is False


def test_backup_rollback_illegal_name():
    r = U.backup_rollback("evil.sh")
    assert r["ok"] is False and "不合法" in r["msg"]


def test_backup_rollback_missing_file(bdir):
    r = U.backup_rollback("panel_1.0_20260101000000.zip")
    assert r["ok"] is False and "不存在" in r["msg"]


def test_backup_rollback_index_ok(bdir, monkeypatch):
    (bdir / "index_1.0_20260101000000.php").write_text("<?php " + "x" * 300)
    (bdir.parent / "index.php").write_text("<?php old")
    monkeypatch.setattr(U, "_apply_zip", lambda *a: {"ok": True})
    r = U.backup_rollback("index_1.0_20260101000000.php")
    assert r["ok"] is True
    assert "已回滚到备份" in r["msg"]
    assert (bdir.parent / "index.php").read_text().startswith("<?php x")


def test_backup_rollback_index_too_short(bdir):
    (bdir / "index_1.0_20260101000000.php").write_text("<?php")
    r = U.backup_rollback("index_1.0_20260101000000.php")
    assert r["ok"] is False and "内容异常" in r["msg"]


def test_backup_rollback_index_not_php(bdir):
    (bdir / "index_1.0_20260101000000.php").write_text("x" * 300)
    r = U.backup_rollback("index_1.0_20260101000000.php")
    assert r["ok"] is False and "内容异常" in r["msg"]


def test_backup_rollback_panel_not_zip(bdir):
    (bdir / "panel_1.0_20260101000000.zip").write_bytes(b"not a zip")
    r = U.backup_rollback("panel_1.0_20260101000000.zip")
    assert r["ok"] is False and "内容异常" in r["msg"]


def test_backup_rollback_panel_ok(bdir, monkeypatch):
    import zipfile

    (bdir.parent / "index.php").write_text("<?php old")
    zp = bdir / "panel_1.0_20260101000000.zip"
    with zipfile.ZipFile(zp, "w") as zf:
        zf.writestr("panel_migr/main.py", "print(1)")
        zf.writestr("panel_migr/config.py",
                    'PANEL_VERSION = os.environ.get("M7A_PANEL_VERSION", "1.0")')
        zf.writestr("panel_migr/routers/__init__.py", "")
        zf.writestr("panel_migr/static/panel.js", "")
    monkeypatch.setattr(U, "_backup_panel_zip", lambda v: str(bdir / "panel_safety.zip"))
    monkeypatch.setattr(U, "_apply_zip", lambda *a: {"ok": True})
    monkeypatch.setattr(U, "_schedule_restart", lambda: "，服务即将重启")
    r = U.backup_rollback(zp.name)
    assert r["ok"] is True
    assert "已回滚到备份" in r["msg"]
    assert "服务即将重启" in r["msg"]


def test_backup_rollback_panel_apply_fails(bdir, monkeypatch):
    import zipfile

    zp = bdir / "panel_1.0_20260101000000.zip"
    with zipfile.ZipFile(zp, "w") as zf:
        zf.writestr("panel_migr/main.py", "x")
        zf.writestr("panel_migr/config.py",
                    'PANEL_VERSION = os.environ.get("M7A_PANEL_VERSION", "1.0")')
        zf.writestr("panel_migr/routers/__init__.py", "")
        zf.writestr("panel_migr/static/panel.js", "")
    monkeypatch.setattr(U, "_backup_panel_zip", lambda v: "")
    monkeypatch.setattr(U, "_apply_zip", lambda *a: {"ok": False, "msg": "boom"})
    r = U.backup_rollback(zp.name)
    assert r["ok"] is False and r["msg"] == "boom"


def test_backup_rollback_strips_directory_from_name(bdir):
    """`Path(...).name` 剥掉路径 → 不能借 ../ 跳出 backups/。"""
    r = U.backup_rollback("../../etc/passwd")
    assert r["ok"] is False


# ====================================================================
# ghcr.py —— 镜像候选 / 更新 / 检查
# ====================================================================

def test_image_candidates_non_ghcr():
    assert G.assistant_image_candidates("moesnow/march7thassistant:latest") == [
        "moesnow/march7thassistant:latest"]


def test_image_candidates_expands_all_prefixes():
    out = G.assistant_image_candidates("ghcr.io/moesnow/march7thassistant:latest")
    assert len(out) == len(G.GHCR_PREFIXES)
    assert all(c.endswith("moesnow/march7thassistant:latest") for c in out)
    assert out[0] == "ghcr.io/moesnow/march7thassistant:latest"


def test_image_candidates_case_insensitive_prefix():
    out = G.assistant_image_candidates("GHCR.IO/moesnow/x:1")
    assert len(out) == len(G.GHCR_PREFIXES)


def test_image_candidates_dedup():
    out = G.assistant_image_candidates("ghcr.io/a/b:1")
    assert len(out) == len(set(out))


def _patch_run(monkeypatch, table):
    """table: list of (match_substr, result) —— 按顺序匹配命令串。"""
    calls = []

    def fake(cmd, timeout=120):
        calls.append(cmd)
        for sub, res in table:
            if sub in cmd:
                return res
        return {"code": 0, "out": ""}

    monkeypatch.setattr(U, "run_cmd", fake)
    return calls


_INST = {"id": "m7a", "name": "x", "container": "m7a", "dir": "/srv/m7a"}


def test_image_update_ok_official(monkeypatch):
    _patch_run(monkeypatch, [
        ("docker inspect", {"code": 0, "out": "ghcr.io/moesnow/march7thassistant:latest\n"}),
        ("docker pull", {"code": 0, "out": "ok"}),
        ("docker compose up -d", {"code": 0, "out": "started"}),
    ])
    monkeypatch.setattr(U, "_compose", lambda i, a: {"code": 0, "out": "started"})
    r = G.image_update(_INST)
    assert r["ok"] is True
    assert r["src"] == "官方源"
    assert "镜像已更新" in r["msg"]


def test_image_update_falls_back_to_mirror(monkeypatch):
    state = {"pull": 0}

    def fake(cmd, timeout=120):
        if "docker inspect" in cmd:
            return {"code": 0, "out": "ghcr.io/moesnow/march7thassistant:latest"}
        if "docker pull" in cmd:
            state["pull"] += 1
            return {"code": 0, "out": "ok"} if state["pull"] == 2 else {"code": 1, "out": "403"}
        return {"code": 0, "out": ""}

    monkeypatch.setattr(U, "run_cmd", fake)
    monkeypatch.setattr(U, "_compose", lambda i, a: {"code": 0, "out": ""})
    r = G.image_update(_INST)
    assert r["ok"] is True
    assert state["pull"] == 2


def test_image_update_all_sources_fail(monkeypatch):
    _patch_run(monkeypatch, [
        ("docker inspect", {"code": 0, "out": "ghcr.io/a/b:1"}),
        ("docker pull", {"code": 1, "out": "denied"}),
    ])
    r = G.image_update(_INST)
    assert r["ok"] is False
    assert "所有镜像源拉取失败" in r["err"]


def test_image_update_cannot_inspect(monkeypatch):
    _patch_run(monkeypatch, [("docker inspect", {"code": 1, "out": "Error: no such object"})])
    r = G.image_update(_INST)
    assert r["ok"] is False
    assert "无法获取当前容器镜像" in r["err"]


def test_image_update_compose_fails(monkeypatch):
    _patch_run(monkeypatch, [
        ("docker inspect", {"code": 0, "out": "ghcr.io/a/b:1"}),
        ("docker pull", {"code": 0, "out": ""}),
    ])
    monkeypatch.setattr(U, "_compose", lambda i, a: {"code": 1, "out": "boom"})
    r = G.image_update(_INST)
    assert r["ok"] is False
    assert "容器重建失败" in r["err"]


def test_image_update_tags_when_source_differs(monkeypatch):
    """当前镜像是加速源前缀 → 官方源（不同前缀）拉成功时要 docker tag 回原名。"""
    calls = []

    def fake(cmd, timeout=120):
        calls.append(cmd)
        if "docker inspect" in cmd:
            return {"code": 0, "out": "ghcr.nju.edu.cn/moesnow/x:latest"}
        if "docker pull" in cmd:
            return {"code": 0, "out": ""}
        return {"code": 0, "out": ""}

    monkeypatch.setattr(U, "run_cmd", fake)
    monkeypatch.setattr(U, "_compose", lambda i, a: {"code": 0, "out": ""})
    r = G.image_update(_INST)
    assert r["ok"] is True
    assert any("docker tag" in c for c in calls)
    assert r["src"] == "官方源"


def test_image_check_uses_cache(monkeypatch, tmp_path):
    cache = tmp_path / "c.json"
    cache.write_text(json.dumps({"ok": True, "cached": 1, "ts": int(time.time())}))
    monkeypatch.setattr(U, "IMAGE_CACHE", cache)
    r = G.image_check(_INST)
    assert r.get("cached") == 1


def test_image_check_stale_cache_refetches(monkeypatch, tmp_path):
    cache = tmp_path / "c.json"
    cache.write_text(json.dumps({"ok": True, "ts": int(time.time()) - 999999}))
    monkeypatch.setattr(U, "IMAGE_CACHE", cache)
    _patch_run(monkeypatch, [
        ("docker inspect", {"code": 0, "out": "ghcr.io/a/b:1"}),
        ("docker image inspect", {"code": 0, "out": "2026-01-01T00:00:00Z"}),
    ])
    monkeypatch.setattr(U, "ghcr_latest_created", lambda *a: "2026-06-01T00:00:00Z")
    r = G.image_check(_INST)
    assert r["ok"] is True
    assert r["has_update"] is True


def test_image_check_force_bypasses_cache(monkeypatch, tmp_path):
    cache = tmp_path / "c.json"
    cache.write_text(json.dumps({"ok": True, "cached": 1, "ts": int(time.time())}))
    monkeypatch.setattr(U, "IMAGE_CACHE", cache)
    _patch_run(monkeypatch, [
        ("docker inspect", {"code": 0, "out": "ghcr.io/a/b:1"}),
        ("docker image inspect", {"code": 0, "out": "T"}),
    ])
    monkeypatch.setattr(U, "ghcr_latest_created", lambda *a: "")
    assert G.image_check(_INST, force=True).get("cached") is None


def test_image_check_bad_cache_json(monkeypatch, tmp_path):
    cache = tmp_path / "c.json"
    cache.write_text("{not json")
    monkeypatch.setattr(U, "IMAGE_CACHE", cache)
    _patch_run(monkeypatch, [("docker inspect", {"code": 0, "out": "ghcr.io/a/b:1"})])
    monkeypatch.setattr(U, "ghcr_latest_created", lambda *a: "")
    assert G.image_check(_INST)["ok"] is True


def test_image_check_container_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(U, "IMAGE_CACHE", tmp_path / "c.json")
    _patch_run(monkeypatch, [("docker inspect", {"code": 1, "out": ""})])
    r = G.image_check(_INST)
    assert r["ok"] is False
    assert "无法获取容器镜像" in r["err"]


def test_image_check_remote_unknown(monkeypatch, tmp_path):
    monkeypatch.setattr(U, "IMAGE_CACHE", tmp_path / "c.json")
    _patch_run(monkeypatch, [
        ("docker inspect", {"code": 0, "out": "ghcr.io/a/b:1"}),
        ("docker image inspect", {"code": 0, "out": "2026-01-01T00:00:00Z"}),
    ])
    monkeypatch.setattr(U, "ghcr_latest_created", lambda *a: "")
    r = G.image_check(_INST)
    assert r["remote_unknown"] is True
    assert r["has_update"] is False


def test_compose_quotes_dir(monkeypatch):
    calls = _patch_run(monkeypatch, [])
    G._compose(_INST, "ps")
    assert " && docker compose ps" in calls[0]
    assert calls[0].startswith("cd ")


def test_write_cache_tolerates_oserror(monkeypatch, tmp_path):
    monkeypatch.setattr(U, "IMAGE_CACHE", tmp_path / "no" / "such" / "dir" / "c.json")
    G._write_cache({"a": 1})          # 不应抛


# ====================================================================
# restart.py —— 三种部署形态 + detach
# ====================================================================

from services.updateops import restart as RS  # noqa: E402


def test_schedule_restart_message():
    assert "已重启" in RS._schedule_restart()


def test_self_under_unit_false_when_cgroup_missing(monkeypatch):
    monkeypatch.setattr(RS.Path, "read_text", lambda *a, **k: (_ for _ in ()).throw(OSError()))
    assert RS._self_under_unit() is False


def test_self_under_unit_true(monkeypatch):
    monkeypatch.setattr(RS.Path, "read_text",
                        lambda *a, **k: "0::/system.slice/m7a-panel.service\n")
    assert RS._self_under_unit() is True


def test_self_under_unit_false_other_unit(monkeypatch):
    monkeypatch.setattr(RS.Path, "read_text", lambda *a, **k: "0::/system.slice/nginx.service\n")
    assert RS._self_under_unit() is False


def test_probe_python_returns_existing():
    assert Path(RS._probe_python()).exists()


def test_write_probe_script(monkeypatch, tmp_path):
    target = tmp_path / "probe.py"
    monkeypatch.setattr(U, "_HEALTH_PROBE_SCRIPT", str(target))
    p = RS._write_probe_script()
    assert p == target
    assert p.is_file()
    assert p.read_text().startswith("#!/usr/bin/env python3")


def test_write_probe_script_tolerates_oserror(monkeypatch, tmp_path):
    monkeypatch.setattr(U, "_HEALTH_PROBE_SCRIPT", str(tmp_path / "no" / "d" / "p.py"))
    RS._write_probe_script()          # 不应抛


def test_restart_skipped_when_not_systemd(monkeypatch):
    _patch_run(monkeypatch, [("systemctl is-active", {"code": 3, "out": "inactive"})])
    r = RS._restart_and_health_check(port=8787)
    assert r["skipped"] is True
    assert "手动重启" in r["msg"]


def test_restart_uses_config_port(monkeypatch):
    """P0-6 回归：端口来自 cfg.LISTEN_PORT，不是硬编码 9999。"""
    seen = []

    def fake(cmd, timeout=120):
        seen.append(cmd)
        if "is-active" in cmd:
            return {"code": 0, "out": "active"}
        if "systemctl restart" in cmd:
            return {"code": 0, "out": ""}
        if "journalctl" in cmd:
            return {"code": 0, "out": "log"}
        return {"code": 0, "out": ""}

    monkeypatch.setattr(U, "run_cmd", fake)
    monkeypatch.setattr(U.cfg, "LISTEN_PORT", 8787)
    urls = []
    monkeypatch.setattr(U, "http_get", lambda u, **k: urls.append(u) or {"code": 404, "body": ""})
    monkeypatch.setattr(U.time, "sleep", lambda s: None)
    r = RS._restart_and_health_check(retries=1)
    assert r["ok"] is False
    assert urls == ["http://127.0.0.1:8787/"]


def test_restart_sync_probe_ok(monkeypatch):
    _patch_run(monkeypatch, [
        ("systemctl is-active", {"code": 0, "out": "active"}),
        ("systemctl restart", {"code": 0, "out": ""}),
    ])
    monkeypatch.setattr(U, "http_get", lambda u, **k: {"code": 200, "body": "ok"})
    monkeypatch.setattr(U.time, "sleep", lambda s: None)
    r = RS._restart_and_health_check(port=1234, retries=3)
    assert r["ok"] is True and r["probe_ok"] is True


def test_restart_systemctl_restart_fails(monkeypatch):
    _patch_run(monkeypatch, [
        ("systemctl is-active", {"code": 0, "out": "active"}),
        ("systemctl restart", {"code": 1, "out": "denied"}),
    ])
    r = RS._restart_and_health_check(port=1234)
    assert r["ok"] is False
    assert "systemctl restart 失败" in r["msg"]


def test_restart_probe_fail_includes_journal(monkeypatch):
    _patch_run(monkeypatch, [
        ("systemctl is-active", {"code": 0, "out": "active"}),
        ("systemctl restart", {"code": 0, "out": ""}),
        ("journalctl", {"code": 0, "out": "last 8 lines"}),
    ])
    monkeypatch.setattr(U, "http_get", lambda u, **k: {"code": 500, "body": ""})
    monkeypatch.setattr(U.time, "sleep", lambda s: None)
    r = RS._restart_and_health_check(port=1234, retries=2)
    assert r["ok"] is False
    assert "最近日志" in r["msg"]


def test_restart_probe_exception_treated_as_fail(monkeypatch):
    _patch_run(monkeypatch, [
        ("systemctl is-active", {"code": 0, "out": "active"}),
        ("systemctl restart", {"code": 0, "out": ""}),
    ])
    def boom(u, **k):
        raise OSError("x")

    monkeypatch.setattr(U, "http_get", boom)
    monkeypatch.setattr(U.time, "sleep", lambda s: None)
    assert RS._restart_and_health_check(port=1, retries=1)["ok"] is False


def test_restart_is_active_raises(monkeypatch):
    def boom(cmd, timeout=120):
        raise OSError("x")

    monkeypatch.setattr(U, "run_cmd", boom)
    assert RS._restart_and_health_check(port=1)["skipped"] is True


def test_detach_prefers_systemd_run(monkeypatch, tmp_path):
    monkeypatch.setattr(RS, "_write_probe_script", lambda: tmp_path / "p.py")
    monkeypatch.setattr(U.shutil, "which", lambda n: "/usr/bin/" + n)
    _patch_run(monkeypatch, [("systemd-run", {"code": 0, "out": ""})])
    r = RS._detach_restart_and_probe(8787, 3, 2, tmp_path / "old", tmp_path / "stage")
    assert r["ok"] is True
    assert r["detach_mode"] == "systemd-run"


def test_detach_falls_back_to_setsid(monkeypatch, tmp_path):
    monkeypatch.setattr(RS, "_write_probe_script", lambda: tmp_path / "p.py")
    monkeypatch.setattr(U.shutil, "which", lambda n: "/usr/bin/" + n)
    _patch_run(monkeypatch, [
        ("systemd-run", {"code": 1, "out": "polkit"}),
        ("setsid", {"code": 0, "out": ""}),
    ])
    r = RS._detach_restart_and_probe(8787, 3, 2, tmp_path / "old", tmp_path / "stage")
    assert r["ok"] is True
    assert r["detach_mode"] == "setsid"


def test_detach_systemd_run_raises_falls_through(monkeypatch, tmp_path):
    monkeypatch.setattr(RS, "_write_probe_script", lambda: tmp_path / "p.py")
    monkeypatch.setattr(U.shutil, "which", lambda n: "/usr/bin/" + n)

    def fake(cmd, timeout=120):
        if "systemd-run" in cmd:
            raise OSError("boom")
        return {"code": 0, "out": ""}

    monkeypatch.setattr(U, "run_cmd", fake)
    r = RS._detach_restart_and_probe(8787, 3, 2, tmp_path / "old", tmp_path / "stage")
    assert r["detach_mode"] == "setsid"


def test_detach_setsid_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(RS, "_write_probe_script", lambda: tmp_path / "p.py")
    monkeypatch.setattr(U.shutil, "which", lambda n: None)

    def fake(cmd, timeout=120):
        raise OSError("boom")

    monkeypatch.setattr(U, "run_cmd", fake)
    r = RS._detach_restart_and_probe(8787, 3, 2, tmp_path / "old", tmp_path / "stage")
    assert r["ok"] is False
    assert "无法 detach" in r["msg"]


def test_detach_setsid_nonzero(monkeypatch, tmp_path):
    monkeypatch.setattr(RS, "_write_probe_script", lambda: tmp_path / "p.py")
    monkeypatch.setattr(U.shutil, "which", lambda n: None)
    _patch_run(monkeypatch, [("setsid", {"code": 1, "out": "nope"})])
    r = RS._detach_restart_and_probe(8787, 3, 2, tmp_path / "old", tmp_path / "stage")
    assert r["ok"] is False
    assert "无法 detach" in r["msg"]


def test_restart_detaches_when_self_under_unit(monkeypatch, tmp_path):
    """形态一：本进程在单元内 → 走 detach，绝不原地 restart。"""
    _patch_run(monkeypatch, [("systemctl is-active", {"code": 0, "out": "active"})])
    monkeypatch.setattr(U, "_self_under_unit", lambda: True)
    monkeypatch.setattr(U, "_detach_restart_and_probe",
                        lambda *a, **k: {"ok": True, "detach": True, "detach_mode": "x",
                                         "skipped": False, "probe_ok": False, "msg": "d"})
    r = RS._restart_and_health_check(port=1, old=tmp_path / "old", stage=tmp_path / "st")
    assert r["detach"] is True
