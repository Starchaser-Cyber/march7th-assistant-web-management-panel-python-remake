"""读取/写出 PHP 配置文件（.instances.php / .panel_config.php / .panel_pass.php）。

这些文件是 `<?php return array(...);` 形式，Python 无法 include。
读取策略：优先 `php -r include + json_encode`（100% 语义一致），按 mtime 缓存；
php-cli 不可用时（本地开发/测试）退回纯 Python 解析器，只支持 var_export 产物
（array()/[]、单引号字符串、true/false/NULL、整数浮点）——与本模块 dump_php_value
写出的格式互为闭环，保证 PHP 与 Python 读写互见。
"""
from __future__ import annotations

import json
import os
import re
import subprocess

_cache: dict[str, tuple[float, object]] = {}


def invalidate_php_cache(path=None):
    """写入 PHP 数据文件后立刻失效缓存（否则同秒内读到旧值）。"""
    if path is None:
        _cache.clear()
    else:
        _cache.pop(str(path), None)


def load_php_file(path, default=None):
    """include 一个返回值的 PHP 文件，返回 json 化后的 Python 对象。"""
    path = str(path)
    try:
        st = os.stat(path)
        # mtime_ns + size：同秒重写不会命中旧缓存
        mt = (st.st_mtime_ns, st.st_size)
    except OSError:
        return default
    hit = _cache.get(path)
    if hit is not None and hit[0] == mt:
        return hit[1]

    value = default
    try:
        r = subprocess.run(
            ["php", "-r", f'$v=@include {json.dumps(path)}; echo json_encode($v, JSON_UNESCAPED_UNICODE);'],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode == 0 and r.stdout:
            value = json.loads(r.stdout)
        elif r.returncode != 0:
            value = parse_php_file(path, default)
    except Exception:
        # php-cli 缺失 / 超时 / include 失败 → 退回纯 Python 解析
        value = parse_php_file(path, default)

    # 只缓存成功解析的结果：None（读失败/瞬时半截文件）不进缓存，下次重读
    if value is not None:
        _cache[path] = (mt, value)
    return value


def atomic_write_text(path, text: str, encoding: str = "utf-8", mode: int | None = None):
    """临时文件 + os.replace 原子替换：并发读者永远看不到半截文件。"""
    import os as _os
    import tempfile as _tempfile

    path = str(path)
    d = _os.path.dirname(path) or "."
    fd, tmp = _tempfile.mkstemp(prefix=".tmp_", dir=d)
    try:
        with _os.fdopen(fd, "w", encoding=encoding) as f:
            f.write(text)
        _os.replace(tmp, path)
        if mode is not None:
            _os.chmod(path, mode)
    except OSError:
        try:
            _os.unlink(tmp)
        except OSError:
            pass
        raise


# ===== 纯 Python 回退解析 / 序列化（var_export 格式）=====

_TOKEN_RE = re.compile(
    r"\s*(?:"
    r"'(?P<squote>(?:[^'\\]|\\.)*)'"
    r'|"(?P<dquote>(?:[^"\\]|\\.)*)"'
    r"|(?P<true>true)\b|(?P<false>false)\b|(?P<null>NULL)\b"
    r"|(?P<int>-?\d+)\b|(?P<float>-?\d+\.\d+)"
    r"|(?P<arr>array\s*\(|\[)"
    r"|(?P<key>=>)"
    r"|(?P<comma>,)"
    r"|(?P<lparen>\()|(?P<rparen>\))"
    r")"
)


class _PhpParser:
    """极简 var_export 解析器：只认 dump_php_value / PHP var_export 的产物。"""

    def __init__(self, text: str):
        self.t = text
        self.i = 0
        self.n = len(text)

    def skip_gap(self):
        """跳过空白与 PHP 注释（//、#、/* ... */）。"""
        while self.i < self.n:
            c = self.t[self.i]
            if c in " \t\r\n":
                self.i += 1
            elif c == "#" or self.t.startswith("//", self.i):
                nl = self.t.find("\n", self.i)
                self.i = self.n if nl < 0 else nl
            elif self.t.startswith("/*", self.i):
                e = self.t.find("*/", self.i + 2)
                self.i = self.n if e < 0 else e + 2
            else:
                return

    def _peek(self):
        self.skip_gap()
        m = _TOKEN_RE.match(self.t, self.i)
        if not m:
            # 跳过数组闭合符（可能是 ] 或 ）在 token 中已含）——容错终止
            return None, None
        return m.lastgroup, m

    def parse_value(self):
        self.skip_gap()
        if self.i < self.n and self.t[self.i] == ";":
            self.i += 1
        self.skip_gap()
        if self.t.startswith("<?php", self.i):
            self.i += 5
            return self.parse_value()
        if self.t.startswith("return", self.i):
            self.i += 6
            return self.parse_value()
        kind, m = self._peek()
        if m is None:
            raise ValueError(f"bad token at {self.i}")
        self.i = m.end()
        if kind == "squote":
            return m.group("squote").replace("\\'", "'").replace("\\\\", "\\")
        if kind == "dquote":
            return m.group("dquote").replace('\\"', '"').replace("\\\\", "\\").replace("\\n", "\n")
        if kind == "true":
            return True
        if kind == "false":
            return False
        if kind == "null":
            return None
        if kind == "int":
            return int(m.group("int"))
        if kind == "float":
            return float(m.group("float"))
        if kind == "arr":
            return self.parse_array()
        raise ValueError(f"unexpected token {kind} at {self.i}")

    def parse_array(self):
        out = {}
        idx = 0
        while True:
            self.skip_gap()
            if self.i < self.n and self.t[self.i] == ",":
                self.i += 1
                continue
            if self.i >= self.n:
                break
            if self.t[self.i] in ")]":
                self.i += 1
                break
            if self.t[self.i] == ";":
                break
            first = self.parse_value()
            kind, m = self._peek()
            if kind == "key":
                self.i = m.end()
                key = first
                val = self.parse_value()
            elif kind in ("comma", "rparen", "lparen", "key") or kind is None:
                # PHP 源码里省略键的写法：array('a', 'b') → 顺序键 0,1,2...
                key, val = idx, first
            else:
                raise ValueError(f"expect => at {self.i}")
            if key is None:
                key = idx
            out[key] = val
            if isinstance(key, int):
                idx = key + 1
        # PHP 顺序数组（键 0..n-1）→ Python list，与 PHP 内存语义一致
        if out and list(out.keys()) == list(range(len(out))):
            return [out[k] for k in range(len(out))]
        return out


def parse_php_value(text: str, default=None):
    """解析 `<?php return ...;` 形式的 PHP 字面量；失败返回 default。"""
    try:
        return _PhpParser(text).parse_value()
    except Exception:
        return default


def parse_php_file(path, default=None):
    try:
        return parse_php_value(open(path, encoding="utf-8", errors="replace").read(), default)
    except OSError:
        return default


def _export_scalar(v) -> str:
    if v is True:
        return "true"
    if v is False:
        return "false"
    if v is None:
        return "NULL"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return repr(v)
    if isinstance(v, str):
        return "'" + v.replace("\\", "\\\\").replace("'", "\\'") + "'"
    raise TypeError(f"unsupported: {type(v)!r}")


def dump_php_value(v, indent: int = 0) -> str:
    """Python 值 → var_export 风格 PHP 字面量（array ( ... ) 老式格式，PHP 全版本可读）。"""
    pad = "  " * indent
    if isinstance(v, dict):
        if not v:
            return "array ()"
        items = []
        for k, val in v.items():
            items.append(f"{pad}  {_export_scalar(k)} => {dump_php_value(val, indent + 1)},")
        return "array (\n" + "\n".join(items) + f"\n{pad})"
    if isinstance(v, (list, tuple)):
        if not v:
            return "array ()"
        items = []
        for i, val in enumerate(v):
            items.append(f"{pad}  {i} => {dump_php_value(val, indent + 1)},")
        return "array (\n" + "\n".join(items) + f"\n{pad})"
    return _export_scalar(v)


def dump_php_return(v) -> str:
    """写成 `<?php ... return <value>;` 文件内容（供面板其它 PHP 文件 include）。"""
    return "<?php\nreturn " + dump_php_value(v) + ";\n"


def dump_php_return_comment(v, comment: str) -> str:
    return "<?php\n// " + comment + "\nreturn " + dump_php_value(v) + ";\n"
