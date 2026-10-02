"""PHP session 文件解析 —— 与 PHP 面板共享登录态。

PHP files handler 序列化格式（php 样式）：`key|<serialized>` 依次连接。
支持 b:/i:/d:/s:/N;/a:（递归）。解析失败时返回已解析部分或空 dict，
效果 = 未登录 → 网关转发给 PHP 返回登录页，行为与原版一致（安全兜底）。
"""
from __future__ import annotations

import os
import re
import secrets
import string
from pathlib import Path

from config import SESSION_SAVE_PATH, SKEY

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
        ln = int(s[i + 2:len_end])
        start = len_end + 2                # 跳过  `:"`
        return s[start:start + ln], start + ln + 2   # 跳过尾部 `";`
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


def _session_path(sid: str) -> Path | None:
    if not sid or not _SID_RE.fullmatch(sid):
        return None
    return Path(SESSION_SAVE_PATH) / f"sess_{sid}"


def _write_session_file(path: Path, text: str) -> None:
    """写回 session 文件（M2-R：绕开内核 fs.protected_regular）。

    在 world-writable sticky 目录（/tmp）中，对「非目录属主」的已存在普通文件用
    O_CREAT 方式（open(path,"w") / Path.write_text）打开，会被内核以 EACCES 拒绝，
    即使 uid=0 且具备全部 capabilities（服务器实测：登录态写不进 → 登录死循环）。
    故：
    - 文件已存在 → O_WRONLY|O_TRUNC（无 O_CREAT）打开，保留原 inode/属主/权限，
      PHP(www) 之后按原权限 O_RDWR 打开不受影响；
    - 文件不存在 → O_CREAT|O_EXCL 新建（新文件属主 = 目录属主，保护不触发），
      并 chmod 0666 覆盖 umask，保证 PHP-fpm(www) 可读写。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    data = text.encode("utf-8")
    created = False
    try:
        fd = os.open(path, os.O_WRONLY | os.O_TRUNC)
    except FileNotFoundError:
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
            created = True
        except FileExistsError:          # 并发创建竞态 → 按已存在打开
            fd = os.open(path, os.O_WRONLY | os.O_TRUNC)
    with os.fdopen(fd, "wb") as f:
        if created:
            os.chmod(path, 0o666)
        f.write(data)


def new_sid() -> str:
    """生成 PHP 8 风格 session id（32 位 [a-zA-Z0-9,-]），与 _SID_RE 兼容。"""
    alphabet = string.ascii_letters + string.digits + ",-"
    return "".join(secrets.choice(alphabet) for _ in range(32))


def session_set(sid: str, key: str, value) -> bool:
    """写/替换一个 session 键（外科式改写原文，不动其它键）。返回是否成功。"""
    path = _session_path(sid)
    if path is None:
        return False
    entry = f"{key}|{php_serialize(value)}"
    try:
        try:
            text = path.read_text("utf-8", errors="replace")
        except OSError:
            text = ""
        for k, s, e in _iter_entries(text):
            if k == key:
                # s 指向值起点，键起点 = s - len(key) - 1（含尾部 |）
                text = text[:s - len(key) - 1] + entry + text[e:]
                break
        else:
            text += entry   # PHP 格式：条目直接相接（key|ser; 无额外分隔）
        _write_session_file(path, text)
        return True
    except OSError:
        return False


def session_remove(sid: str, key: str) -> bool:
    """删除一个 session 键（等价 unset($_SESSION[key])）。"""
    path = _session_path(sid)
    if path is None:
        return False
    try:
        text = path.read_text("utf-8", errors="replace")
    except OSError:
        return True
    changed = False
    for k, s, e in list(_iter_entries(text)):
        if k == key:
            text = text[:s - len(key) - 1] + text[e:]
            changed = True
            break
    if not changed:
        return True
    try:
        _write_session_file(path, text)
        return True
    except OSError:
        return False


def session_id(request) -> str:
    """取请求里的 PHPSESSID（无正则校验的裸值；写入路径会再校验一次）。"""
    return request.cookies.get("PHPSESSID", "") or ""


def sid_valid(sid: str) -> bool:
    """cookie 里的 sid 是否为合法 PHP 会话 id（页面会话引导用）。"""
    return bool(sid) and bool(_SID_RE.fullmatch(sid))
