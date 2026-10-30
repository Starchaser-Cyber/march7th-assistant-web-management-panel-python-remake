"""V1.24.7 覆盖率补测：`services/passwd.py`（面板密码文件）。

回归保护点：
- 写出的 hash 必须是 PHP 可校验的 `$2y$` 前缀（Python 的 bcrypt 默认给 `$2b$`）；
- 文件内容格式与 PHP `<?php return '...';` 逐字符一致，权限 0600；
- **bcrypt 72 字节上限**：超长密码不能静默截断成「前 72 字节等价」，也不能
  让 `check_pass` 抛异常（必须返回 False）。
"""
import os
import stat
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import config as cfg
import services.passwd as passwd


@pytest.fixture
def pass_file(tmp_path, monkeypatch):
    """把 cfg.PASS_FILE 指到 tmp_path（passwd 内部实时读 cfg.PASS_FILE）。"""
    p = tmp_path / ".panel_pass.php"
    monkeypatch.setattr(cfg, "PASS_FILE", p)
    # load_php_file 有 mtime 缓存，测试间必须清（同一 tmp 目录可能复用 inode）
    import phpfile
    phpfile.invalidate_php_cache()
    yield p
    phpfile.invalidate_php_cache()


# ================= set_pass：写入格式 =================

def test_set_pass_creates_file(pass_file):
    assert passwd.set_pass("secret123") is True
    assert pass_file.exists()


def test_set_pass_php_wrapper_format(pass_file):
    """必须是 `<?php return '<hash>';`，PHP 侧 include 才能拿到字符串。"""
    passwd.set_pass("secret123")
    text = pass_file.read_text(encoding="ascii")
    assert text.startswith("<?php return '")
    assert text.endswith("';")


def test_set_pass_uses_2y_prefix(pass_file):
    """PHP 的 password_verify 认 $2y$；bcrypt 库产出 $2b$，必须改写前缀。"""
    passwd.set_pass("secret123")
    text = pass_file.read_text(encoding="ascii")
    assert "$2y$" in text, f"未改写成 PHP 兼容前缀：{text}"
    assert "$2b$" not in text


def test_set_pass_mode_0600(pass_file):
    """密码哈希不能被同机其他用户读到。"""
    passwd.set_pass("secret123")
    assert stat.S_IMODE(os.stat(pass_file).st_mode) == 0o600


def test_set_pass_replaces_old_hash(pass_file):
    passwd.set_pass("first-pass")
    first = pass_file.read_text(encoding="ascii")
    passwd.set_pass("second-pass")
    second = pass_file.read_text(encoding="ascii")
    assert first != second
    assert passwd.check_pass("second-pass") is True
    assert passwd.check_pass("first-pass") is False


def test_set_pass_returns_false_on_oserror(pass_file, monkeypatch):
    """目录不可写 → 返回 False（调用方据此显示「检查面板目录权限」）。"""
    import phpfile

    def _boom(*a, **kw):
        raise OSError("permission denied")

    monkeypatch.setattr(phpfile, "atomic_write_text", _boom)
    assert passwd.set_pass("secret123") is False


def test_set_pass_coerces_non_str(pass_file):
    """非字符串入参先 str()（与 PHP 弱类型一致），不能抛。"""
    assert passwd.set_pass(123456) is True
    assert passwd.check_pass("123456") is True


# ================= check_pass =================

def test_check_pass_no_file_returns_false(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "PASS_FILE", tmp_path / "nope.php")
    assert passwd.check_pass("anything") is False


def test_check_pass_empty_hash_returns_false(pass_file):
    pass_file.write_text("<?php return '';", encoding="ascii")
    import phpfile
    phpfile.invalidate_php_cache()
    assert passwd.check_pass("x") is False


def test_check_pass_correct(pass_file):
    passwd.set_pass("correct-horse")
    assert passwd.check_pass("correct-horse") is True


def test_check_pass_wrong(pass_file):
    passwd.set_pass("correct-horse")
    assert passwd.check_pass("wrong-horse") is False


def test_check_pass_empty_string(pass_file):
    passwd.set_pass("correct-horse")
    assert passwd.check_pass("") is False


def test_check_pass_non_str(pass_file):
    passwd.set_pass("123456")
    assert passwd.check_pass(123456) is True


def test_check_pass_corrupt_hash_returns_false(pass_file):
    """密码文件被写坏（非 bcrypt 串）→ 返回 False，不能抛 500。"""
    pass_file.write_text("<?php return 'not-a-bcrypt-hash';", encoding="ascii")
    import phpfile
    phpfile.invalidate_php_cache()
    assert passwd.check_pass("x") is False


def test_check_pass_non_string_file_value(pass_file):
    """密码文件里存了数组/整数 → 返回 False（类型守卫）。"""
    pass_file.write_text("<?php return 123;", encoding="ascii")
    import phpfile
    phpfile.invalidate_php_cache()
    assert passwd.check_pass("x") is False


# ================= bcrypt 72 字节上限 =================

def test_check_pass_over_72_bytes_returns_false(pass_file):
    """bcrypt 只处理前 72 字节；超长输入 checkpw 会抛 ValueError → 必须吞成 False。"""
    passwd.set_pass("a" * 72)
    # 73 字节输入不能抛异常
    assert passwd.check_pass("a" * 73) is False
    assert passwd.check_pass("a" * 200) is False


def test_check_pass_exactly_72_bytes_ok(pass_file):
    passwd.set_pass("a" * 72)
    assert passwd.check_pass("a" * 72) is True


def test_check_pass_truncation_boundary(pass_file):
    """bcrypt 只看前 72 字节：前 72 相同、第 73 字节不同的密码会「撞车」。

    这是 bcrypt 的固有语义（PHP 侧同样如此），不是本项目的 bug；
    记录下来防止有人误以为是校验漏洞。真正的防护应在 `set_pass` 拒绝超长密码。
    """
    passwd.set_pass("a" * 72)
    assert passwd.check_pass("a" * 72 + "DIFFERENT-SUFFIX") is False  # 超长 → 直接 False
    # 但 set 侧若接受超长密码，则前缀相同的两个密码互相可登录
    # （本用例只断言 check 侧不误放行，set 侧行为见下一条）


def test_set_pass_over_72_bytes_is_rejected_not_raising(pass_file):
    """v1.24.7 修复：>72 字节密码被 `set_pass` 明确拒绝，返回 False，不再抛 ValueError。

    修复前：bcrypt 5.x 的 `hashpw` 拒绝超长密码（抛 ValueError），而 `set_pass()`
    只 `except OSError`，ValueError 一路上抛到 `routers/write.py::h_setup_pass`
    → 用户设置超长密码时拿到 500 而不是友好提示。

    修复后（两层）：
    1. `h_setup_pass` 前置校验字节数 → 直接返回「密码过长」提示；
    2. `services/passwd.py::set_pass` 自身也判长度并返回 False（兜底其它调用点），
       且**绝不静默截断**（截断会让前 72 字节相同的两个密码互相通过校验）。
    """
    assert passwd.set_pass("a" * 73) is False
    # 72 字节是合法上界
    assert passwd.set_pass("a" * 72) is True
    # 拒绝时不能留下/改坏已有密码文件：仍是上一次成功的那个密码
    assert passwd.check_pass("a" * 72) is True


def test_set_pass_multibyte_password_byte_limit(pass_file):
    """上限是 **字节数** 不是字符数：24 个汉字 = 72 字节（刚好），25 个 = 75 字节（超）。"""
    assert len("密" * 24) == 24 and len(("密" * 24).encode()) == 72
    assert passwd.set_pass("密" * 24) is True
    # 75 字节 > 72 → 拒绝（v1.24.7：返回 False 而不是抛 ValueError）
    assert passwd.set_pass("密" * 25) is False


# ================= 与 PHP 侧互操作（格式级） =================

def test_written_hash_is_parseable_by_phpfile(pass_file):
    """写出的文件必须能被 phpfile 的解析器读回同一个字符串（PHP include 等价）。"""
    import phpfile

    passwd.set_pass("secret123")
    phpfile.invalidate_php_cache()
    value = phpfile.load_php_file(str(pass_file))
    assert isinstance(value, str)
    assert value.startswith("$2y$")
    # 用读回的值也能校验通过
    import bcrypt
    assert bcrypt.checkpw(b"secret123", value.encode("ascii")) is True


def test_hash_roundtrip_via_phpfile_parser(pass_file):
    """纯 Python 解析器路径（php-cli 不可用）也要能读回。"""
    import phpfile

    passwd.set_pass("secret123")
    text = pass_file.read_text(encoding="ascii")
    assert phpfile.parse_php_value(text).startswith("$2y$")
