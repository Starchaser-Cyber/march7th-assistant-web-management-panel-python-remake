"""更新源：Release 元数据、资产解析、镜像地址、sha256 锚点。"""
from __future__ import annotations

import hashlib
import json
import re
from urllib.parse import quote as rawurlencode



from . import _rt


MIRRORS = ("https://ghfast.top/", "https://gh-proxy.com/", "https://ghproxy.net/", "https://ghps.cc/")


def update_api_url() -> str:
    owner, repo = rawurlencode(_rt.cfg.UPDATE_OWNER), rawurlencode(_rt.cfg.UPDATE_REPO)
    if _rt.cfg.UPDATE_TYPE == "github":
        return f"https://api.github.com/repos/{owner}/{repo}/releases/latest"
    return _rt.cfg.UPDATE_HOST.rstrip("/") + f"/api/v1/repos/{owner}/{repo}/releases/latest"


def update_raw_url() -> str:
    owner, repo = rawurlencode(_rt.cfg.UPDATE_OWNER), rawurlencode(_rt.cfg.UPDATE_REPO)
    if _rt.cfg.UPDATE_TYPE == "github":
        return f"https://raw.githubusercontent.com/{owner}/{repo}/{_rt.cfg.UPDATE_BRANCH}/index.php"
    return _rt.cfg.UPDATE_HOST.rstrip("/") + f"/{owner}/{repo}/raw/branch/{_rt.cfg.UPDATE_BRANCH}/index.php"


def update_raw_urls() -> list[str]:
    if _rt.cfg.UPDATE_TYPE != "github":
        return [_rt.update_raw_url()]
    official = _rt.update_raw_url()
    mirrors = [official] + [m + official for m in _rt.MIRRORS]
    ts = "?t=" + str(int(_rt.time.time()))
    return [u + ts for u in mirrors]


def panel_update_mode() -> str:
    from services.instances import panel_config
    c = panel_config()
    return "manual" if str(c.get("update_mode") or "") == "manual" else "auto"


def _release_json() -> tuple[dict | None, str]:
    """→ (release JSON, 错误说明)。"""
    r = _rt.http_get(_rt.update_api_url(), 10)
    if r["code"] != 200 or not r["body"]:
        return None, f"HTTP {r['code']}"
    try:
        j = json.loads(r["body"])
    except ValueError:
        return None, "返回数据无法解析"
    if not isinstance(j, dict) or not j.get("tag_name"):
        return None, "无可用 release"
    return j, ""


# 发布包命名约定：`V{版本}.zip`（如 V1.24.6.zip）。Release 里还会挂审计包、
# 交接包等附加资产，它们的名字**也**以版本号开头（V1.24.6-audit.zip），且带外层
# 目录 → `_zip_struct_ok` 必然 False。旧判据 `^[Vv]\d` 对两者都成立，于是按 GitHub
# API 的返回顺序误选审计包，在线更新 100% 失败（Q-10）。
_VER_ZIP_RE = re.compile(r"^[Vv]\d+(?:\.\d+)*\.zip$")


# 兜底：名字里带这些词的 zip 不是发布包
_AUX_ZIP_RE = re.compile(r"audit|report|handoff|source|src", re.I)


def _zip_asset(j: dict) -> tuple[str, str]:
    """→ (zip 下载 URL, 资产名)。

    优先级：① 严格形如 `V{版本}.zip` 的发布包 ② 名字不含 audit/report/handoff
    等附加包标记的 zip（兼容旧命名）③ 以版本号开头的 zip ④ 第一个 zip（历史兜底）。
    """
    if not isinstance(j, dict):
        return "", ""
    assets = j.get("assets") or []
    zips = [a for a in assets
            if isinstance(a, dict) and str(a.get("name") or "").lower().endswith(".zip")]
    if not zips:
        return "", ""

    def _name(a: dict) -> str:
        return str(a.get("name") or "")

    exact = [a for a in zips if _rt._VER_ZIP_RE.match(_name(a))]
    if not exact:
        exact = [a for a in zips if not _rt._AUX_ZIP_RE.search(_name(a))]
    if not exact:
        exact = [a for a in zips if re.match(r"^[Vv]\d", _name(a))]
    a = (exact or zips)[0]
    return str(a.get("browser_download_url") or ""), _name(a)


def _zip_urls(zip_url: str) -> list[tuple[str, str]]:
    """官方源 + 加速镜像的下载地址列表。"""
    if _rt.cfg.UPDATE_TYPE != "github":
        return [("官方源", zip_url)]
    out = [("官方源", zip_url)]
    out += [(f"加速镜像{i}", m + zip_url) for i, m in enumerate(_rt.MIRRORS, 1)]
    return out


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _asset_digest(j: dict, asset_name: str) -> str:
    """从 Release JSON 取资产 sha256 digest。

    v1.24.3 安全加固（M7A-01）：GitHub Release API 自 2024 年起为每个资产返回
    `digest: "sha256:<64hex>"`，该值经 TLS 校验的 API 通道取得，可作为更新包
    的真实性锚点。第三方加速镜像可替换包体但无法替换该值 —— 下载后比对，
    不符即丢弃该来源。

    返回 64 位小写 hex；无该字段（旧 API / gitea / 自建源）→ ""。
    """
    if not isinstance(j, dict) or asset_name == "":
        return ""
    for a in j.get("assets") or []:
        if not isinstance(a, dict):
            continue
        if str(a.get("name") or "") != asset_name:
            continue
        d = str(a.get("digest") or "").strip().lower()
        if d.startswith("sha256:") and re.fullmatch(r"[0-9a-f]{64}", d[7:]):
            return d[7:]
    return ""


def check_update() -> dict:
    if not _rt.cfg.UPDATE_ENABLED:
        return {"ok": True, "enabled": False}
    r = _rt.http_get(_rt.update_api_url(), 8)
    if r["code"] != 200 or not r["body"]:
        return {"ok": False, "err": f"更新源连接失败（HTTP {r['code']}）"}
    try:
        j = json.loads(r["body"])
    except ValueError:
        j = None
    if not isinstance(j, dict) or not j.get("tag_name"):
        return {"ok": False, "err": "更新源返回数据异常"}
    latest = str(j["tag_name"]).lstrip("vV")
    note = str(j.get("body") or "").strip()
    _, asset = _rt._zip_asset(j)
    return {
        "ok": True,
        "enabled": True,
        "has_update": _rt._version_gt(latest, _rt.cfg.PANEL_VERSION),
        "latest": latest,
        "current": _rt.cfg.PANEL_VERSION,
        "note": note,
        "asset": asset,
        "digest_ok": _rt._asset_digest(j, asset) != "",
        "update_mode": _rt.panel_update_mode(),
    }


def test_update_source() -> dict:
    """连通性测试：release API + 更新包下载源（v1.20 为 ZIP 资产；
    旧版 release 无 zip 时回退 index.php raw 探测）。响应形状保持前端兼容。"""
    api = _rt.update_api_url()
    ar = _rt.http_get(api, 8)
    j = None
    if ar["code"] == 200:
        try:
            j = json.loads(ar["body"])
        except ValueError:
            j = None
    if ar["code"] == 200 and "tag_name" in ar["body"].lower():
        api_state = "ok_release"
    elif ar["code"] == 200:
        api_state = "ok_no_release"
    else:
        api_state = "fail"

    mirrors = []
    raw_ok = False
    raw_code = 0
    raw_url = _rt.update_raw_url()

    zip_url, _asset = _rt._zip_asset(j) if isinstance(j, dict) else ("", "")
    if zip_url:
        raw_url = zip_url
        urls = [(name, url) for name, url in _rt._zip_urls(zip_url)]
        for i, (name, url) in enumerate(urls):
            pr = _rt._probe(url)
            mirrors.append({"name": name, "url": url, "code": pr["code"], "ok": pr["ok"]})
            if i == 0:
                raw_ok, raw_code = pr["ok"], pr["code"]
    else:
        for i, url in enumerate(_rt.update_raw_urls()):
            rr = _rt.http_get(url, 8)
            ok = rr["code"] == 200 and (rr["body"].lstrip()[:5].lower() == "<?php")
            mirrors.append({
                "name": "官方源" if i == 0 else f"镜像{i}",
                "url": url,
                "code": rr["code"],
                "ok": ok,
            })
            if i == 0:
                raw_ok, raw_code = ok, rr["code"]

    return {
        "ok": api_state != "fail" and raw_ok,
        "api": {"url": api, "code": ar["code"], "state": api_state},
        "raw": {"url": raw_url, "code": raw_code, "ok": raw_ok},
        "mirrors": mirrors,
    }
