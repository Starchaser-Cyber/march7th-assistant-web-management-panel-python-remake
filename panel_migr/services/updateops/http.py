"""HTTP 抓取（httpx 同步）+ 连通性探测。

失败语义与旧 PHP curl 版一致：code=0 表示连接失败，body='' 表示无内容。
"""
from __future__ import annotations

import json


import config as cfg

from . import _rt


UA = f"M7A-Panel/{cfg.PANEL_VERSION}"


_MAX_DOWNLOAD = 64 * 1024 * 1024         # 下载包体上限 64MB


def http_get(url: str, timeout: int = 8) -> dict:
    try:
        r = _rt.httpx.get(
            url, timeout=_rt.httpx.Timeout(timeout, connect=5), follow_redirects=True,
            headers={"User-Agent": _rt.UA},
        )
        return {"code": r.status_code, "body": r.text}
    except Exception:
        return {"code": 0, "body": ""}


def http_bytes(url: str, timeout: int = 60) -> bytes | None:
    """下载二进制（更新包）；失败 / 超过 _MAX_DOWNLOAD → None。

    v1.24.4（P1-8）：改流式读取 + 边读边累计上限。此前用 httpx.get 把整包读进
    内存（无 Content-Length 上限），投毒包可直接把 8G 机器打爆。
    """
    try:
        with _rt.httpx.stream(
            "GET", url, timeout=_rt.httpx.Timeout(timeout, connect=10),
            follow_redirects=True, headers={"User-Agent": _rt.UA},
        ) as r:
            if r.status_code != 200:
                return None
            # 有 Content-Length 且明显超限：不读包体，直接放弃
            try:
                cl = int(r.headers.get("content-length") or 0)
            except (TypeError, ValueError):
                cl = 0
            if cl > _rt._MAX_DOWNLOAD:
                return None
            buf = bytearray()
            for chunk in r.iter_bytes():
                buf.extend(chunk)
                if len(buf) > _rt._MAX_DOWNLOAD:
                    return None
            return bytes(buf) if buf else None
    except Exception:
        return None


def http_post(url: str, fields: dict, timeout: int = 8, as_json: bool = False) -> dict:
    try:
        if as_json:
            r = _rt.httpx.post(url, content=json.dumps(fields, ensure_ascii=False),
                           headers={"Content-Type": "application/json"},
                           timeout=_rt.httpx.Timeout(timeout, connect=5), follow_redirects=True)
        else:
            r = _rt.httpx.post(url, data=fields,
                           headers={"Content-Type": "application/x-www-form-urlencoded"},
                           timeout=_rt.httpx.Timeout(timeout, connect=5), follow_redirects=True)
        return {"code": r.status_code, "body": r.text}
    except Exception:
        return {"code": 0, "body": "request failed"}


def _probe(url: str, timeout: int = 8) -> dict:
    """轻量下载探测（Range 头，尽量只取首字节）→ {code, ok}。"""
    try:
        r = _rt.httpx.get(
            url, headers={"User-Agent": _rt.UA, "Range": "bytes=0-0"},
            timeout=_rt.httpx.Timeout(timeout, connect=5), follow_redirects=True,
        )
        return {"code": r.status_code, "ok": r.status_code in (200, 206)}
    except Exception:
        return {"code": 0, "ok": False}
