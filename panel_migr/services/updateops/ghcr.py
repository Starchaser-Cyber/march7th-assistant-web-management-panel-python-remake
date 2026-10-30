"""镜像更新：GHCR 鉴权、tag 列表、镜像拉取与切换。"""
from __future__ import annotations

import json


import config as cfg
from services.instances import instance_container, instance_dir

from . import _rt
import contextlib


IMAGE_CACHE = cfg.BASE_DIR / ".image_check_cache.json"


IMAGE_REPO = "moesnow/march7thassistant"


GHCR_PREFIXES = ("ghcr.io/", "ghcr.nju.edu.cn/", "ghcr.m.daocloud.io/", "ghcr.dockerproxy.com/")


def _ghcr_auth(token: str) -> str:
    """GHCR 鉴权头的**唯一**构造点（v1.24.4 P1-6）。

    此前 ghcr_latest_created 里先拼好 `auth = "Bearer " + token`，再调用
    ghcr_config_created("Authorization: " + auth, ...)，而后者内部又写成
    headers={"Authorization": auth} → 实际发出
    `Authorization: Authorization: Bearer ey...`，blob 请求必然 401，
    镜像构建时间恒为空。现在统一在这里加一次前缀，调用方只传裸 token。
    """
    t = (token or "").strip()
    if t.lower().startswith("authorization:"):
        t = t.split(":", 1)[1].strip()
    if t.lower().startswith("bearer "):
        t = t[7:].strip()
    return "Bearer " + t


def ghcr_config_created(auth: str, base: str, digest: str, timeout: int = 10) -> str:
    """取镜像 config blob 的 created 时间；auth 只传裸 token（前缀由 _ghcr_auth 加）。"""
    try:
        r = _rt.httpx.get(
            base + "/blobs/" + digest,
            headers={"Authorization": _rt._ghcr_auth(auth),
                     "Accept": "application/vnd.oci.image.config.v1+json"},
            timeout=_rt.httpx.Timeout(timeout, connect=5), follow_redirects=True,
        )
        j = r.json() if r.status_code == 200 else None
        return str(j.get("created") or "") if isinstance(j, dict) else ""
    except Exception:
        return ""


def ghcr_latest_created(timeout: int = 10) -> str:
    """查 GHCR latest 镜像构建时间（token → manifest → config blob）。"""
    repo = _rt.IMAGE_REPO
    base = f"https://ghcr.io/v2/{repo}"
    hdr = {"Accept": "application/json", "User-Agent": _rt.UA}
    try:
        r = _rt.httpx.get(
            f"https://ghcr.io/token?scope=repository:{repo}:pull",
            headers=hdr, timeout=_rt.httpx.Timeout(timeout, connect=5),
        )
        token = str((r.json() or {}).get("token") or "")
        if token == "":
            return ""
        ah = dict(hdr)
        ah["Authorization"] = _rt._ghcr_auth(token)   # v1.24.4 P1-6：唯一构造点
        ah["Accept"] = ",".join([
            "application/vnd.oci.image.index.v1+json",
            "application/vnd.docker.distribution.manifest.list.v2+json",
            "application/vnd.oci.image.manifest.v1+json",
            "application/vnd.docker.distribution.manifest.v2+json",
        ])
        r2 = _rt.httpx.get(base + "/manifests/latest", headers=ah,
                       timeout=_rt.httpx.Timeout(timeout, connect=5))
        if r2.status_code != 200:
            return ""
        j = r2.json()
        if not isinstance(j, dict):
            return ""
        if isinstance(j.get("config"), dict) and j["config"].get("digest"):
            return _rt.ghcr_config_created(token, base, j["config"].get("digest"), timeout)
        manifests = j.get("manifests") or []
        digest = manifests[0].get("digest", "") if manifests else ""
        if digest == "":
            return ""
        ah2 = dict(hdr)
        ah2["Authorization"] = _rt._ghcr_auth(token)  # v1.24.4 P1-6：唯一构造点
        ah2["Accept"] = ",".join([
            "application/vnd.oci.image.index.v1+json",
            "application/vnd.docker.distribution.manifest.list.v2+json",
            "application/vnd.oci.image.manifest.v1+json",
            "application/vnd.docker.distribution.manifest.v2+json",
        ])
        r3 = _rt.httpx.get(base + "/manifests/" + digest, headers=ah2,
                       timeout=_rt.httpx.Timeout(timeout, connect=5))
        if r3.status_code != 200:
            return ""
        j3 = r3.json()
        if not isinstance(j3, dict):
            return ""
        cfgdg = ((j3 or {}).get("config") or {}).get("digest", "")
        if not cfgdg:
            return ""
        return _rt.ghcr_config_created(token, base, cfgdg, timeout)
    except Exception:
        return ""


def assistant_image_candidates(image: str) -> list[str]:
    rest = ""
    low = image.lower()
    for pre in _rt.GHCR_PREFIXES:
        if low.startswith(pre):
            rest = image[len(pre):]
            break
    if rest == "":
        return [image]
    out = []
    for pre in _rt.GHCR_PREFIXES:
        cand = pre + rest
        if cand not in out:
            out.append(cand)
    return out


def image_update(inst: dict) -> dict:
    """依次官方源 + 加速镜像拉取小助手镜像 → tag 回原名 → 重建容器。"""
    from shlex import quote
    r1 = _rt.run_cmd(f'docker inspect --format "{{{{.Config.Image}}}}" {quote(instance_container(inst))}')
    image = r1["out"].strip()
    low = image.lower()
    if r1["code"] != 0 or image == "" or "error" in low or "not found" in low:
        return {"ok": False, "err": "无法获取当前容器镜像（容器未运行？）：" + image}
    cands = _rt.assistant_image_candidates(image)
    used = ""
    detail = []
    for i, cand in enumerate(cands):
        r = _rt.run_cmd(f"docker pull {quote(cand)}", timeout=1800)
        if r["code"] == 0:
            used = cand
            break
        detail.append(("官方源" if i == 0 else f"加速镜像{i}") + "失败：" + r["out"].strip())
    if used == "":
        return {"ok": False, "err": "所有镜像源拉取失败：" + " | ".join(detail)}
    if used != image:
        _rt.run_cmd(f"docker tag {quote(used)} {quote(image)}")
    r2 = _rt._compose(inst, "up -d")
    if r2["code"] != 0:
        return {"ok": False, "err": "镜像已拉取但容器重建失败：" + r2["out"].strip()}
    with contextlib.suppress(OSError):
        _rt.IMAGE_CACHE.unlink()
    src_name = "镜像源"
    labels = {
        "ghcr.io/": "官方源", "ghcr.nju.edu.cn/": "南大镜像",
        "ghcr.m.daocloud.io/": "DaoCloud", "ghcr.dockerproxy.com/": "dockerproxy",
    }
    ulow = used.lower()
    for pre, label in labels.items():
        if ulow.startswith(pre):
            src_name = label
            break
    return {"ok": True, "src": src_name, "msg": f"镜像已更新（{src_name}），容器已重建"}


def image_check(inst: dict, force: bool = False) -> dict:
    if not force and _rt.IMAGE_CACHE.is_file():
        try:
            c = json.loads(_rt.IMAGE_CACHE.read_text("utf-8"))
        except (OSError, ValueError):
            c = None
        if isinstance(c, dict) and int(c.get("ts") or 0) and int(_rt.time.time()) - int(c["ts"]) < 21600:
            return c
    from shlex import quote as _q
    r1 = _rt.run_cmd(f'docker inspect --format "{{{{.Config.Image}}}}" {_q(instance_container(inst))}')
    image = r1["out"].strip()
    low = image.lower()
    if r1["code"] != 0 or image == "" or "error" in low or "not found" in low:
        err = image
        data = {
            "ok": False,
            "err": "无法获取容器镜像" + (f"：{err}" if err != "" else "（容器未运行？）"),
            "ts": int(_rt.time.time()),
        }
        _rt._write_cache(data)
        return data
    r2 = _rt.run_cmd(f'docker image inspect --format "{{{{.Created}}}}" {_q(image)}')
    local = r2["out"].strip()
    remote = _rt.ghcr_latest_created(10)
    has_update = False
    lt = _rt._to_ts(local)
    rt = _rt._to_ts(remote)
    if lt and rt and rt > lt + 3600:
        has_update = True
    data = {
        "ok": True, "image": image, "local": local, "remote": remote,
        "remote_unknown": remote == "", "has_update": has_update, "ts": int(_rt.time.time()),
    }
    _rt._write_cache(data)
    return data


def _write_cache(data: dict) -> None:
    with contextlib.suppress(OSError):
        _rt.IMAGE_CACHE.write_text(json.dumps(data, ensure_ascii=False), "utf-8")


def _compose(inst: dict, args: str) -> dict:
    from shlex import quote
    return _rt.run_cmd(f"cd {quote(instance_dir(inst))} && docker compose {args}")
