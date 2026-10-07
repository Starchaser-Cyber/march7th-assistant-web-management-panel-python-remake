"""M6 本地渲染兜底 —— Python 唯一后端时，未匹配请求不再回源。

v1.22 起面板后端全部由 Python 承担（旧站点转发链退役，仅以回滚开关兜底）：
- 未匹配的整站 GET/POST 路径 → 直接渲染面板主页面（等价旧页面渲染分支）
- PWA 附属资源 `?manifest` / `?icon` → 本地生成，不再回源
- 回滚：`M7A_FWD_ENABLE=1` 时由 main.py 改走 gateway.forward（见 config.FWD_ENABLE）

所有函数只做本地渲染，不访问网络，失败也不影响主流程。
"""
from __future__ import annotations

import json
import struct
import zlib

from fastapi.responses import JSONResponse, Response

# 面板品牌色（与模板 theme-color 一致）
_THEME_COLOR = "#ec4899"
_THEME_RGB = (236, 72, 153)


def render_fallback(request) -> Response:
    """未匹配路径兜底：渲染面板主页面（登录页/面板页按会话自动分流）。"""
    from services.pagectx import page_response

    return page_response(request)


# ===== PWA 附属资源（本地生成）=====

def _png_solid(size: int, rgb: tuple) -> bytes:
    """纯 Python 生成纯色 PNG（无第三方依赖，失败可回退）。"""
    w = h = int(size)
    row = b"\x00" + bytes(rgb) * w
    raw = row * h

    def chunk(typ: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + typ + data
                + struct.pack(">I", zlib.crc32(typ + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)   # 8bit truecolor RGB
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw, 9))
            + chunk(b"IEND", b""))


def icon_response(request) -> Response:
    """`?icon=1&size=192` → 面板图标（PNG）。非法 size 回落 192，钳 16..512。"""
    try:
        size = int(request.query_params.get("size") or 192)
    except (TypeError, ValueError):
        size = 192
    size = max(16, min(512, size))
    return Response(
        content=_png_solid(size, _THEME_RGB),
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=86400"},
    )


def manifest_response(request) -> Response:
    """`?manifest=1` → PWA 清单（供添加到主屏）。"""
    manifest = {
        "name": "M7A WebUI · 管理面板",
        "short_name": "M7A WebUI",
        "start_url": ".",
        "scope": ".",
        "display": "standalone",
        "background_color": "#0f1115",
        "theme_color": _THEME_COLOR,
        "icons": [
            {"src": "?icon=1&size=192", "sizes": "192x192",
             "type": "image/png", "purpose": "any maskable"},
            {"src": "?icon=1&size=512", "sizes": "512x512",
             "type": "image/png", "purpose": "any maskable"},
        ],
    }
    return Response(
        content=json.dumps(manifest, ensure_ascii=False),
        media_type="application/manifest+json",
        headers={"Cache-Control": "public, max-age=3600"},
    )
