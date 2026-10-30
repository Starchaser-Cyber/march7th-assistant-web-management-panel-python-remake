"""面板密码（.panel_pass.php）——等价 PHP check_pass() / set_pass()。

hash 由 password_hash(PASSWORD_DEFAULT) 生成 = bcrypt，Python 侧用 bcrypt 库校验。
写回格式与 PHP 逐字符一致：`<?php return '$2y$...';`（$2b$ → $2y$ 等价改写，PHP 可校验）。
"""
from __future__ import annotations

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


# bcrypt 算法硬上限：只处理前 72 字节，bcrypt>=5.0 的 hashpw 直接抛 ValueError
# 而不是静默截断。设密码的 UI 在 routers/write.py::h_setup_pass 里有前置拦截
# （给出友好提示），这里再兜一层，防止其它调用点漏掉 → 用户拿到 500。
PASS_MAX_BYTES = 72


def set_pass(pw) -> bool:
    """等价 password_hash()+file_put_contents(PASS_FILE) + chmod 600。

    失败返回 False（不抛异常）：密码超长、写盘失败都算「没设成功」，
    调用方只需判断返回值。超长**绝不静默截断**（截断会让前 72 字节相同的
    两个不同密码互相通过校验）。
    """
    raw = str(pw).encode("utf-8")
    if len(raw) > PASS_MAX_BYTES:
        return False
    try:
        digest = bcrypt.hashpw(raw, bcrypt.gensalt(rounds=10))
    except ValueError:
        # bcrypt 版本策略差异兜底（理论上已被上面的长度校验挡住）
        return False
    if digest[:4] == b"$2b$":
        digest = b"$2y$" + digest[4:]
    content = "<?php return '" + digest.decode("ascii") + "';"
    try:
        from phpfile import atomic_write_text

        atomic_write_text(cfg.PASS_FILE, content, encoding="ascii", mode=0o600)
        return True
    except OSError:
        return False
