"""绞杀者网关：把未迁移/未登录的请求原样转发回原 PHP 站点（nginx :9999）。

X-M7A-Fwd 头用于 nginx if 排除，防止 nginx→8787→nginx 的转发循环。
M1 阶段：只读 5 接口由 Python 处理，其余全部经此转发；PHP 全程兜底。
"""
from __future__ import annotations

import httpx
from fastapi.responses import JSONResponse, Response

from config import FWD_HEADER, FWD_HEADER_VALUE, PHP_UPSTREAM

_HOP_BY_HOP = {
    "connection", "keep-alive", "transfer-encoding", "te", "trailer",
    "proxy-authenticate", "proxy-authorization", "upgrade", "content-length", "host",
}


async def forward(request) -> Response:
    url = PHP_UPSTREAM + request.url.path
    if request.url.query:
        url += "?" + request.url.query

    headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in _HOP_BY_HOP
    }
    headers[FWD_HEADER] = FWD_HEADER_VALUE
    # 强制上游不压缩：httpx 解压后转发会与 content-encoding 头不一致（浏览器白屏）
    headers["accept-encoding"] = "identity"

    body = await request.body()
    try:
        # trust_env=False：内部转发不走环境代理（否则 localhost 被 http_proxy 吞掉）
        async with httpx.AsyncClient(timeout=60.0, follow_redirects=False, trust_env=False) as client:
            r = await client.request(request.method, url, content=body, headers=headers)
    except httpx.HTTPError as e:
        return JSONResponse(
            {"ok": False, "msg": f"PHP upstream 不可达：{e}"},
            status_code=502,
        )

    resp_headers = {
        k: v for k, v in r.headers.items()
        if k.lower() not in _HOP_BY_HOP and k.lower() != "content-encoding"
    }
    return Response(
        content=r.content,
        status_code=r.status_code,
        headers=resp_headers,
    )
