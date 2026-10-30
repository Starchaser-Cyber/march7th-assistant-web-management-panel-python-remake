"""PHP session 文件解析 —— 与 PHP 面板共享登录态。

PHP files handler 序列化格式（php 样式）：`key|<serialized>` 依次连接。
支持 b:/i:/d:/s:/N;/a:（递归）。解析失败时返回已解析部分或空 dict，
效果 = 未登录 → 网关转发给 PHP 返回登录页，行为与原版一致（安全兜底）。
"""
from __future__ import annotations

import logging
import os
import re
import secrets
import stat
import string
import threading
from pathlib import Path

from config import SESSION_FILE_MODE, SESSION_SAVE_PATH, SKEY

log = logging.getLogger("m7a.phpsess")

_SID_RE = re.compile(r"[A-Za-z0-9,\-]{1,128}")


def _parse_value(s: str, i: int):
    """从 s[i:] 解析一个 PHP 序列化值，返回 (value, 下一起始下标)。"""
    if s.startswith("b:", i):
        j = s.index(";", i)
        return (s[i + 2:j] == "1"), j + 1
    if s.startswith("i:", i):
        j = s.index(";", i)
        return int(s[i + 2:j]), j + 1
    if s.startswith("d:", i):
        j = s.index(";", i)
        return float(s[i + 2:j]), j + 1
    if s.startswith("N;", i):
        return None, i + 2
    if s.startswith("s:", i):
        len_end = s.index(":", i + 2)      # s:<len>:  的第二个冒号
        ln = int(s[i + 2:len_end])          # ln 是 UTF-8 字节长度（php 语义）
        start = len_end + 2                # 跳过  `:"`
        # 按字节取值再解码；此前直接按字符数切，含中文时会把尾部 `";`
        # 一并吞进 value（序列化按字节计数、解析按字符），v1.21.1 修正
        val = s[start:].encode("utf-8")[:ln].decode("utf-8", errors="replace")
        return val, start + len(val) + 2   # 跳过尾部 `";`
    if s.startswith("a:", i):
        brace = s.index("{", i)
        n = int(s[i + 2:brace - 1])       # a:<len>:{  的第二个冒号前结束
        p = brace + 1
        out = {}
        for _ in range(n):
            k, p = _parse_value(s, p)
            v, p = _parse_value(s, p)
            out[k] = v
        return out, p + 1              # 跳过 `}`
    raise ValueError(f"unsupported token at {i}: {s[i:i + 20]!r}")


def parse_session(text: str) -> dict:
    """解析 php 样式 session 文本；遇到损坏保留已解析部分。"""
    out: dict = {}
    p, n = 0, len(text)
    try:
        while p < n:
            bar = text.find("|", p)
            if bar <= p:
                break
            key = text[p:bar]
            val, p = _parse_value(text, bar + 1)
            out[key] = val
    except Exception:
        pass
    return out


def php_empty(v) -> bool:
    """PHP empty() 语义（含 '0'、0、''、[]、None、False）。"""
    return v is None or v is False or v == 0 or v == "" or v == "0" or v == [] or v == {}


def load_session(request) -> dict:
    """按 cookie PHPSESSID 读取 /tmp/sess_<sid>。无效/缺失 → {}。"""
    sid = request.cookies.get("PHPSESSID", "")
    if not sid or not _SID_RE.fullmatch(sid):
        return {}
    try:
        text = (Path(SESSION_SAVE_PATH) / f"sess_{sid}").read_text("utf-8", errors="replace")
    except OSError:
        return {}
    return parse_session(text)


def is_auth(session: dict) -> bool:
    """等价 PHP：function is_auth() { return !empty($_SESSION[SKEY]); }"""
    return not php_empty(session.get(SKEY))


# ===== session 写入（M2：login/logout/instance_switch 需要回写共享 session）=====

def php_serialize(value) -> str:
    """Python 值 → PHP 序列化片段（不含键），仅覆盖本面板用到的类型。"""
    if value is True:
        return "b:1;"
    if value is False:
        return "b:0;"
    if value is None:
        return "N;"
    if isinstance(value, int):
        return f"i:{value};"
    if isinstance(value, float):
        return f"d:{value};"
    if isinstance(value, str):
        raw = value.encode("utf-8")
        return f's:{len(raw)}:"{value}";'
    if isinstance(value, dict):
        body = "".join(f"{php_serialize(k)}{php_serialize(v)}" for k, v in value.items())
        return f"a:{len(value)}:{{{body}}}"
    if isinstance(value, (list, tuple)):
        body = "".join(f"i:{i};{php_serialize(v)}" for i, v in enumerate(value))
        return f"a:{len(value)}:{{{body}}}"
    raise TypeError(f"unsupported php type: {type(value)!r}")


def _iter_entries(text: str):
    """按顺序产出 (key, 值起始, 值结束)；解析失败即停（保留已扫到的部分）。"""
    p, n = 0, len(text)
    try:
        while p < n:
            bar = text.find("|", p)
            if bar <= p:
                break
            key = text[p:bar]
            _, end = _parse_value(text, bar + 1)
            yield key, bar + 1, end
            p = end
    except Exception:
        return


_LOCK = threading.RLock()   # 进程内串行化「读-改-写」全程（P1-11）


def _session_path(sid: str) -> Path | None:
    if not sid or not _SID_RE.fullmatch(sid):
        return None
    return Path(SESSION_SAVE_PATH) / f"sess_{sid}"


def _is_symlink(path: Path) -> bool:
    """目标是否已是符号链接（P1-12：绝不跟随链接写/改权限）。"""
    try:
        return stat.S_ISLNK(os.lstat(path).st_mode)
    except OSError:
        return False


def _read_all(fd: int) -> str:
    os.lseek(fd, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    while True:
        b = os.read(fd, 65536)
        if not b:
            break
        chunks.append(b)
    return b"".join(chunks).decode("utf-8", "replace")


def _update_session_file(path: Path, mutate, *, create: bool = True) -> bool:
    """在**排他锁内**完成 session 文件的读-改-写，返回是否成功。

    v1.24.4 修复（P1-11 / 审计 B-03）：旧实现是「read_text 整个文件 → 改 →
    O_TRUNC 写回」，全程无锁。并发下两个请求各读一份旧文本、各自写回，后写者
    把先写者的改动整体覆盖 —— 实测「登出后 auth 键复活」（登出请求删掉 auth，
    并发的另一请求用旧快照把它写回来 → 已登出的会话重新变成管理员）。

    现在：
      1) 模块级 threading.RLock 把「读-改-写」整段串起来（同进程内并发）；
      2) 对同一 fd 用 os.lockf(LOCK_EX) 与 PHP files handler 的 flock 互斥
         （跨进程，Python 与 PHP 面板共享同一批 session 文件）；
      3) 读取与写入都在锁内、都走同一个 fd，不再有「读一份旧快照」的窗口。

    为什么不改成「写临时文件 + os.replace」：M2-R 修复要求已存在的 session 文件
    必须**保留原 inode/属主/权限**（/tmp 下 PHP 建的 www 属主文件，用 O_CREAT
    方式打开会被内核 fs.protected_regular=2 直接 EACCES → 登录死循环）。
    os.replace 会换掉 inode 与属主，等于回退 M2-R 的根因修复；故仍用「无 O_CREAT
    打开 + 原地截断写」，原子性由锁保证。

    P1-12（审计 B-04）：所有打开都带 os.O_NOFOLLOW。旧实现写会话文件时未带该
    标志，若攻击者能在 session 目录里放一个指向 /etc/... 的符号链接（目录可写
    或 PHP 侧可控），写入会穿透链接以 root 身份改写任意文件 → 本地提权。现在
    链接目标 open 直接 ELOOP 失败，且 chmod 前用 lstat 再校验一次不是链接。
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError:
        return False
    flags = os.O_RDWR | os.O_NOFOLLOW
    with _LOCK:
        if _is_symlink(path):        # 快速失败：不跟随链接
            return False
        created = False
        try:
            try:
                fd = os.open(path, flags)
            except FileNotFoundError:
                if not create:
                    return True      # 无文件可改（session_remove 的 noop 语义）
                try:
                    fd = os.open(path, flags | os.O_CREAT | os.O_EXCL,
                                 SESSION_FILE_MODE)
                    created = True
                except FileExistsError:      # 并发创建竞态 → 按已存在打开
                    fd = os.open(path, flags)
            except OSError:                  # ELOOP（链接）/ EACCES / EACCES-protected
                return False
        except OSError:
            return False
        try:
            try:
                import fcntl
                fcntl.lockf(fd, fcntl.LOCK_EX, 0)   # 与 PHP flock 互斥（跨进程）
            except (ImportError, OSError, AttributeError):
                pass                          # 不支持锁的文件系统 → 仍有 RLock
            text = "" if created else _read_all(fd)
            new_text = mutate(text)
            if new_text is None:
                return True
            data = new_text.encode("utf-8")
            os.lseek(fd, 0, os.SEEK_SET)
            os.ftruncate(fd, 0)
            os.write(fd, data)
            if created:
                st = os.lstat(path)
                if stat.S_ISREG(st.st_mode):   # 只在确认是普通文件后才 chmod
                    os.chmod(path, SESSION_FILE_MODE)
            return True
        except OSError:
            return False
        finally:
            # 清理类：fd 已经用完了，关不掉（EBADF/EIO）不影响返回结果，
            # 但 debug 留痕便于发现 fd 泄漏。
            try:
                os.close(fd)
            except OSError:
                log.debug("phpsess: 关闭 session fd 失败 %s", path, exc_info=True)


def new_sid() -> str:
    """生成 PHP 8 风格 session id（32 位 [a-zA-Z0-9-]）。

    v1.24.5（Q-07）：**不再使用逗号**。PHP 8 的字母表是 [a-zA-Z0-9,-]，但 RFC 6265
    的 cookie-octet 不允许 `,`（0x2C），Starlette 只能把它转义成 `\\054` 并给整个值
    加双引号下发：`PHPSESSID="xxx\\054yyy"`。这在浏览器/curl 上碰巧能用，但严格按
    RFC 实现的客户端会拒绝或截断，部分反代/WAF 也会误判。去掉逗号后字母表仍是
    PHP 8 字符集的子集，会话 id 依旧兼容 PHP 侧 sess_ 文件命名。

    `_SID_RE` 保留逗号，以便继续接受升级前下发的历史会话（那些 id 含逗号）。
    """
    alphabet = string.ascii_letters + string.digits + "-"
    return "".join(secrets.choice(alphabet) for _ in range(32))


def _mutate_set(text: str, key: str, entry: str) -> str:
    for k, s, e in _iter_entries(text):
        if k == key:
            # s 指向值起点，键起点 = s - len(key) - 1（含尾部 |）
            return text[:s - len(key) - 1] + entry + text[e:]
    # PHP 格式：条目直接相接（key|ser; 无额外分隔）
    return text + entry


def _mutate_remove(text: str, key: str):
    changed = False
    for k, s, e in list(_iter_entries(text)):
        if k == key:
            text = text[:s - len(key) - 1] + text[e:]
            changed = True
            break
    return text if changed else None      # None = 无需写盘


def session_set(sid: str, key: str, value) -> bool:
    """写/替换一个 session 键（外科式改写原文，不动其它键）。返回是否成功。

    v1.24.4：读-改-写在 _update_session_file 的排他锁内一次完成（P1-11），
    不再出现「读旧快照 → 覆盖别人改动」的窗口。
    """
    path = _session_path(sid)
    if path is None:
        return False
    entry = f"{key}|{php_serialize(value)}"
    return _update_session_file(path, lambda text: _mutate_set(text, key, entry))


def session_remove(sid: str, key: str) -> bool:
    """删除一个 session 键（等价 unset($_SESSION[key])）。"""
    path = _session_path(sid)
    if path is None:
        return False
    return _update_session_file(path, lambda text: _mutate_remove(text, key),
                                create=False)


def session_id(request) -> str:
    """取请求里的 PHPSESSID（无正则校验的裸值；写入路径会再校验一次）。"""
    return request.cookies.get("PHPSESSID", "") or ""


def sid_valid(sid: str) -> bool:
    """cookie 里的 sid 是否为合法 PHP 会话 id（页面会话引导用）。"""
    return bool(sid) and bool(_SID_RE.fullmatch(sid))
