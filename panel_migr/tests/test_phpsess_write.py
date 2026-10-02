"""M2-R 会话写入根治回归：fs.protected_regular 环境下的真实写入测试。

背景（2026-10-01 服务器实锤）：/tmp 是 world-writable sticky 目录，PHP 创建的
sess_* 属主为 www；Python 若用 O_CREAT 方式（Path.write_text / open(path,"w")）
打开覆写，内核 fs.protected_regular=2 直接 EACCES——即使 uid=0 拥有全部
capabilities——导致登录态写不进（登录 302 但 auth 未落盘 → 跳回登录页死循环）。

本文件在本地复现同一内核条件（本地 fs.protected_regular 同为 2）：
- 参照组证明旧写法确实被拦（根因复现，防止回退）；
- 实验组证明修复写法（无 O_CREAT 打开 / O_EXCL 新建）绕开拦截。
"""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import phpsess
from phpsess import (
    is_auth,
    load_session,
    new_sid,
    parse_session,
    session_remove,
    session_set,
)

OTHER_UID = 1000


def _protected() -> bool:
    try:
        return Path("/proc/sys/fs/protected_regular").read_text().strip() == "2"
    except OSError:
        return False


@pytest.fixture()
def sess_dir(tmp_path, monkeypatch):
    """普通目录（非 world-writable）——不触发内核保护的常规读写路径。"""
    monkeypatch.setattr(phpsess, "SESSION_SAVE_PATH", str(tmp_path))
    return tmp_path


@pytest.fixture()
def sticky_dir(tmp_path, monkeypatch):
    """world-writable sticky 目录——复刻服务器 /tmp 的内核保护条件。"""
    d = tmp_path / "sdir"
    d.mkdir()
    d.chmod(0o1777)
    monkeypatch.setattr(phpsess, "SESSION_SAVE_PATH", str(d))
    return d


def _foreign_file(d: Path, sid: str, text: str, mode: int) -> Path:
    """在 sticky 目录里造一个「非目录属主」的会话文件（等价 PHP 建的 www 文件）。"""
    f = d / f"sess_{sid}"
    f.write_text(text, encoding="utf-8")
    try:
        os.chown(f, OTHER_UID, OTHER_UID)
    except (PermissionError, OSError):
        pytest.skip("需要 root chown 才能复现他人属主文件")
    f.chmod(mode)
    return f


# ---------- 1. 基础：新建 / 改写 / 删除 ----------

def test_new_session_file_created_666(sess_dir):
    sid = "writeok01"
    assert session_set(sid, "m7a_panel_auth", True) is True
    f = sess_dir / f"sess_{sid}"
    assert f.is_file()
    assert (f.stat().st_mode & 0o777) == 0o666          # PHP(www) 需要 O_RDWR
    assert parse_session(f.read_text("utf-8"))["m7a_panel_auth"] is True


def test_replace_and_remove_roundtrip(sess_dir):
    sid = "writeok02"
    assert session_set(sid, "k", "v1") is True
    assert session_set(sid, "k", "v2") is True           # 替换不追加重复键
    assert session_set(sid, "other", 1) is True
    d = parse_session((sess_dir / f"sess_{sid}").read_text("utf-8"))
    assert d == {"k": "v2", "other": 1}
    assert session_remove(sid, "k") is True
    d2 = parse_session((sess_dir / f"sess_{sid}").read_text("utf-8"))
    assert "k" not in d2 and d2["other"] == 1


def test_invalid_sid_rejected(sess_dir):
    assert session_set("../evil", "k", 1) is False
    assert session_set("", "k", 1) is False
    assert session_remove("../evil", "k") is False
    assert not list(sess_dir.iterdir())                  # 未创建任何文件


def test_remove_nonexistent_file_is_noop(sess_dir):
    assert session_remove("ghostsid99", "k") is True


# ---------- 2. 内核保护复现与绕过（M2-R 核心）----------

def test_kernel_guard_blocks_ocreate_reference(sticky_dir):
    """参照组：旧写法（write_text = O_CREAT）在此环境确实被内核拒绝。
    这是 2026-10-01 登录故障的根因复现；若此断言失败，说明环境不再复现保护，
    下面的绕过测试将失去对照意义（会显式暴露，不会静默通过）。"""
    if not _protected():
        pytest.skip("fs.protected_regular != 2，本环境不复现内核保护")
    f = _foreign_file(sticky_dir, "guardref", "m7a_panel_auth|b:1;", 0o600)
    with pytest.raises(PermissionError):
        f.write_text("x", "utf-8")                       # O_CREAT|O_TRUNC → EACCES
    assert f.read_text("utf-8") == "m7a_panel_auth|b:1;"  # 文件未被改动


def test_session_set_bypasses_guard(sticky_dir):
    """根治验证：对「他人属主、600、sticky 目录」的会话文件写入成功。"""
    if not _protected():
        pytest.skip("fs.protected_regular != 2，本环境不复现内核保护")
    f = _foreign_file(sticky_dir, "bypass1", "m7a_panel_auth|b:1;", 0o600)
    assert session_set("bypass1", "m7a_instance", "m7a") is True
    d = parse_session(f.read_text("utf-8"))
    assert d["m7a_panel_auth"] is True                    # 原键保留
    assert d["m7a_instance"] == "m7a"                     # 新键写入
    st = f.stat()
    assert st.st_uid == OTHER_UID                         # inode/属主未被替换
    assert (st.st_mode & 0o777) == 0o600                  # 权限未被改动


def test_session_remove_bypasses_guard(sticky_dir):
    if not _protected():
        pytest.skip("fs.protected_regular != 2，本环境不复现内核保护")
    f = _foreign_file(sticky_dir, "bypass2", "a|b:1;m7a_panel_auth|b:1;", 0o666)
    assert session_remove("bypass2", "a") is True
    d = parse_session(f.read_text("utf-8"))
    assert "a" not in d and d["m7a_panel_auth"] is True


# ---------- 3. 登录全链路（PHP 序列化格式 / PHP 视角可读）----------

def test_login_auth_write_and_readback(sess_dir):
    sid = new_sid()
    assert len(sid) == 32 and phpsess._SID_RE.fullmatch(sid)
    assert session_set(sid, "m7a_panel_auth", True) is True
    raw = (sess_dir / f"sess_{sid}").read_text("utf-8")
    assert raw == "m7a_panel_auth|b:1;"                  # PHP files handler 原生格式

    class Req:
        def __init__(self, s):
            self.cookies = {"PHPSESSID": s}

    assert is_auth(load_session(Req(sid))) is True       # 下个请求读回 → 已登录
    assert session_remove(sid, "m7a_panel_auth") is True
    assert is_auth(load_session(Req(sid))) is False      # 退出 → 未登录


# ---------- 4. 打开策略断言（不依赖内核保护/chown 的逻辑级验证）----------

def test_existing_file_opened_without_ocreate(sess_dir, monkeypatch):
    """已存在的会话文件必须用「无 O_CREAT」方式打开——内核保护的绕开点。"""
    import os as _os

    sid = "openstr1"
    assert session_set(sid, "k", 1) is True              # 先建文件
    calls = []
    real_open = _os.open

    def spy(path, flags, *args, **kwargs):
        calls.append(flags)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(phpsess.os, "open", spy)
    assert session_set(sid, "k", 2) is True
    assert calls, "未捕获到 os.open 调用"
    assert all(not (f & _os.O_CREAT) for f in calls), \
        f"打开已存在文件时出现了 O_CREAT（会触发内核 protected_regular 拦截）: {calls}"
    assert parse_session((sess_dir / f"sess_{sid}").read_text("utf-8"))["k"] == 2


def test_missing_file_created_excl_with_666(sess_dir, monkeypatch):
    """文件不存在 → 先无 CREAT 探测（FNFE）→ O_CREAT|O_EXCL 新建 mode 0666。"""
    import os as _os

    calls = []
    real_open = _os.open

    def spy(path, flags, *args, **kwargs):
        calls.append((flags, args))
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(phpsess.os, "open", spy)
    sid = "openstr2"
    assert session_set(sid, "k", True) is True
    assert len(calls) == 2, f"预期两段式打开（探测+新建），实际: {calls}"
    flags1, _ = calls[0]
    assert not (flags1 & _os.O_CREAT)                    # 探测：无 O_CREAT
    flags2, args2 = calls[1]
    assert flags2 & _os.O_CREAT and flags2 & _os.O_EXCL  # 新建：O_EXCL 防并发覆盖
    assert args2 and args2[0] == 0o666                   # mode 参数 0666
    f = sess_dir / f"sess_{sid}"
    assert (f.stat().st_mode & 0o777) == 0o666           # chmod 覆盖 umask 生效
