"""写操作的页面级回执 —— 等价 PHP 写操作后整页重渲染（$msg/$err 横幅）。

M2 阶段前端仍在 PHP 手里：Python 执行完写操作后，取一份 PHP 渲染好的页面，
把 PHP 本该输出的横幅（.msg.ok / .msg.err）注入进去，行为与 PHP 整页重渲染
一致；取页面失败时降级为自绘横幅 + 自动跳回页面。
"""
from __future__ import annotations

from fastapi.responses import HTMLResponse, Response
import httpx

import config as cfg
from services.configops import h

MAIN_ANCHOR = "<!-- ===== 概览 ===== -->"


async def fetch_php_page(request) -> str | None:
    """GET 一份 PHP 渲染的面板页面（同路径、同 Cookie，去掉查询串避免 ajax 路由）。"""
    url = cfg.PHP_UPSTREAM + (request.url.path or "/")
    headers = {"cookie": "; ".join(f"{k}={v}" for k, v in request.cookies.items())}
    try:
        async with httpx.AsyncClient(
            timeout=60.0, follow_redirects=True, trust_env=False
        ) as client:
            r = await client.get(url, headers=headers)
        if r.status_code != 200 or not r.content:
            return None
        return r.text
    except httpx.HTTPError:
        return None


def _banner(msg=None, err=None) -> str:
    parts = []
    if msg:
        parts.append(f'<div class="msg ok">✅ {h(str(msg))}</div>')
    if err:
        parts.append(f'<div class="msg err">❌ {h(str(err))}</div>')
    return "".join(parts)


def inject(html: str, msg=None, err=None, notify_check=None) -> str:
    """把 PHP 本应输出的横幅插到它在页面里的原位置。"""
    if notify_check:
        from services.configops import notify_check_html

        box = notify_check_html(notify_check)
        anchor = 'id="cfgGroup-notify"'
        pos = html.find(anchor)
        if pos >= 0:
            start = html.rfind('<div class="cfg-group', 0, pos)
            if start >= 0:
                html = html[:start] + box + html[start:]
        else:
            html += box
    banner = _banner(msg, err)
    if not banner:
        return html
    pos = html.find(MAIN_ANCHOR)
    if pos >= 0:
        return html[:pos] + banner + html[pos:]
    # 登录/初始化页：插在 auth-card 里第一张表单前（PHP：`<?php if ($err): ?>` 原位）
    card = html.find('<div class="auth-card')
    if card >= 0:
        form = html.find("<form", card)
        if form >= 0:
            # PHP 登录/初始化页的横幅没有 ❌ 前缀
            style = f'<div class="msg err">{h(str(err))}</div>' if (err and not msg) else banner
            return html[:form] + style + html[form:]
    body = html.find("</body>")
    if body >= 0:
        return html[:body] + banner + html[body:]
    return html + banner


def fallback_page(msg=None, err=None) -> str:
    """取不到 PHP 页面时的降级：横幅 + 2 秒后自动回面板页。"""
    return (
        "<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
        "<meta http-equiv=\"refresh\" content=\"2;url=./\">"
        "<title>M7A</title></head><body>"
        + _banner(msg, err)
        + "</body></html>"
    )


async def flash(request, *, msg=None, err=None, notify_check=None) -> Response:
    """M5-B：页面归 Python 后直接重渲染带横幅页面；失败时降级回 PHP 注入链。"""
    from services.pagectx import page_response

    try:
        return page_response(request, msg=msg, err=err, notify_check=notify_check)
    except Exception:
        html = await fetch_php_page(request)
        if html is None:
            return HTMLResponse(fallback_page(msg, err))
        return HTMLResponse(inject(html, msg=msg, err=err, notify_check=notify_check))
