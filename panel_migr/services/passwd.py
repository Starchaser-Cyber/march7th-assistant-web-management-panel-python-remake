"""面板密码（.panel_pass.php）——等价 PHP check_pass() / set_pass()。

hash 由 password_hash(PASSWORD_DEFAULT) 生成 = bcrypt，Python 侧用 bcrypt 库校验。
写回格式与 PHP 逐字符一致：`<?php return '$2y$...';`（$2b$ → $2y$ 等价改写，PHP 可校验）。
"""
from __future__ import annotations

import os

import bcrypt

import config as cfg
from phpfile import load_php_file


def _pass_hash() -> str | None:
    h = load_php_file(str(cfg.PASS_FILE), None)
    return h if isinstance(h, str) and h else None


def check_pass(pw) -> bool:
    """等价 password_verify()。无密码文件 → False。"""
    h = _pass_hash()
    if not h:
        return False
    try:
        return bcrypt.checkpw(str(pw).encode("utf-8"), h.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def set_pass(pw) -> bool:
    """等价 password_hash()+file_put_contents(PASS_FILE) + chmod 600。"""
    digest = bcrypt.hashpw(str(pw).encode("utf-8"), bcrypt.gensalt(rounds=10))
    if digest[:4] == b"$2b$":
        digest = b"$2y$" + digest[4:]
    content = "<?php return '" + digest.decode("ascii") + "';"
    try:
        from phpfile import atomic_write_text

        atomic_write_text(cfg.PASS_FILE, content, encoding="ascii", mode=0o600)
        return True
    except OSError:
        return False
