"""V1.24.5 回归测试：Q-07 —— session id 字母表不含逗号（RFC 6265 cookie-octet）。

背景：PHP 8 的 session id 字母表是 [a-zA-Z0-9,-]，但 RFC 6265 的 cookie-octet
明确排除 `,`（0x2C）。Starlette 的 set_cookie 只能把逗号转义成 `\\054` 并给整个
值加双引号，实际下发：

    set-cookie: PHPSESSID="mX1c6VWk...SXz47\\0543m"; HttpOnly; Path=/; SameSite=lax

浏览器与 curl 能往返，但严格按 RFC 的客户端会拒绝，部分反代/WAF 也会误判。
修复：new_sid() 的字母表去掉逗号；_SID_RE 保留逗号以继续接受历史会话。
"""

from __future__ import annotations

import re

import phpsess

# RFC 6265 cookie-octet（不含 DQUOTE / 逗号 / 分号 / 反斜杠 / 空白）
_RFC6265_COOKIE_OCTET = re.compile(r"[\x21\x23-\x2B\x2D-\x3A\x3C-\x5B\x5D-\x7E]*")


def test_new_sid_has_no_comma() -> None:
    """Q-07 核心断言：任何一次生成的 sid 都不含逗号（进而不会被加引号转义）。"""
    for _ in range(200):
        sid = phpsess.new_sid()
        assert "," not in sid, f"sid 含逗号：{sid!r}"


def test_new_sid_is_valid_cookie_octets() -> None:
    """整串 sid 必须完全落在 RFC 6265 的 cookie-octet 字符集内。"""
    for _ in range(200):
        sid = phpsess.new_sid()
        assert _RFC6265_COOKIE_OCTET.fullmatch(sid), f"sid 含非 cookie-octet 字符：{sid!r}"


def test_new_sid_shape() -> None:
    """长度 32、仅 [A-Za-z0-9-]，且仍然是合法 PHP 会话 id。"""
    sid = phpsess.new_sid()
    assert len(sid) == 32
    assert re.fullmatch(r"[A-Za-z0-9-]{32}", sid), sid
    assert phpsess.sid_valid(sid)


def test_new_sid_still_accepted_by_sid_re() -> None:
    """生成的 sid 仍要能通过 _SID_RE（页面会话引导路径依赖它）。"""
    for _ in range(50):
        sid = phpsess.new_sid()
        assert phpsess._SID_RE.fullmatch(sid), sid


def test_legacy_sid_with_comma_still_valid() -> None:
    """向后兼容：升级前下发的含逗号 sid 仍要通过校验（否则老会话全部 401）。"""
    legacy = "mX1c6VWkNoDSCUNOxcbarpSrSXz47,3m"
    assert phpsess._SID_RE.fullmatch(legacy)
    assert phpsess.sid_valid(legacy)


def test_new_sid_uniqueness() -> None:
    """32 位随机，200 次抽样不应碰撞（极低概率事件，作为退化保护）。"""
    sids = {phpsess.new_sid() for _ in range(200)}
    assert len(sids) == 200


def test_set_cookie_has_no_quotes_for_new_sid() -> None:
    """端到端：用新 sid 走 Starlette set_cookie，Set-Cookie 头不应出现双引号或 \\054。"""
    from starlette.responses import Response

    sid = phpsess.new_sid()
    resp = Response()
    resp.set_cookie("PHPSESSID", sid, path="/", httponly=True, samesite="lax")
    header = resp.headers.get("set-cookie") or ""
    assert sid in header
    assert "\\054" not in header, header
    assert '"' not in header.split(";")[0], header
