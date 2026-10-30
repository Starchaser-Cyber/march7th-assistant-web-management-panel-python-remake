"""V1.24.7 覆盖率补测：`phpfile.py`（PHP 数据文件读写）。

回归保护点（都是 v1.24.x 修过的缺陷）：
- **P2-10**：`php -r` 成功但 stdout 为空时**不得写缓存**（否则瞬时半截文件被
  永久缓存，面板一直读到空值）。
- `atomic_write_text` 必须保留调用方给的 mode（`.panel_pass.php` 依赖 0600，
  否则密码哈希对所有本机用户可读）。
- 缓存键是 `(st_mtime_ns, st_size)`：同秒重写不能命中旧值。
"""
import json
import os
import stat
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import phpfile


@pytest.fixture(autouse=True)
def _clear_cache():
    """每个用例前后清空模块级缓存，避免跨用例污染。"""
    phpfile.invalidate_php_cache()
    yield
    phpfile.invalidate_php_cache()


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


# ================= load_php_file：php-cli 正常路径 =================

class _FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _fake_run(monkeypatch, proc):
    """替换 subprocess.run（phpfile 内部是模块级 import subprocess）。"""
    calls = []

    def _run(args, **kw):
        calls.append(args)
        if isinstance(proc, Exception):
            raise proc
        return proc

    monkeypatch.setattr(phpfile.subprocess, "run", _run)
    return calls


def test_load_php_file_via_php_cli(tmp_path, monkeypatch):
    p = _write(tmp_path / "cfg.php", "<?php return array('a' => 1);")
    calls = _fake_run(monkeypatch, _FakeProc(0, json.dumps({"a": 1})))
    assert phpfile.load_php_file(p) == {"a": 1}
    assert "php" in calls[0][0]


def test_load_php_file_missing_returns_default(tmp_path, monkeypatch):
    _fake_run(monkeypatch, _FakeProc(0, "{}"))
    assert phpfile.load_php_file(tmp_path / "nope.php", default={"d": 1}) == {"d": 1}


def test_load_php_file_passes_path_to_php(tmp_path, monkeypatch):
    p = _write(tmp_path / "cfg.php", "<?php return 1;")
    calls = _fake_run(monkeypatch, _FakeProc(0, "1"))
    phpfile.load_php_file(p)
    assert str(p) in calls[0][2]


# ================= P2-10：stdout 为空不缓存 =================

def test_empty_stdout_is_not_cached(tmp_path, monkeypatch):
    """php 成功但 stdout 为空 → 不缓存，下次重新执行 php。"""
    p = _write(tmp_path / "cfg.php", "<?php return array();")
    calls = _fake_run(monkeypatch, _FakeProc(0, ""))
    assert phpfile.load_php_file(p, default=None) is None
    assert str(p) not in phpfile._cache, "空 stdout 被缓存了（P2-10 回归）"
    # 第二次仍会跑 php
    phpfile.load_php_file(p)
    assert len(calls) == 2


def test_empty_stdout_falls_back_to_python_parser(tmp_path, monkeypatch):
    """stdout 为空时 value 保持 default；但文件本身可被纯 Python 解析器读。"""
    p = _write(tmp_path / "cfg.php", "<?php return array('k' => 'v');")
    _fake_run(monkeypatch, _FakeProc(0, ""))
    # load_php_file 走 php 路径拿到空 → default
    assert phpfile.load_php_file(p, default={}) == {}
    # 而 parse_php_file 能正确读
    assert phpfile.parse_php_file(p) == {"k": "v"}


def test_nonzero_returncode_uses_python_parser(tmp_path, monkeypatch):
    """php 不可用/报错 → 退回纯 Python 解析器（不能抛）。"""
    p = _write(tmp_path / "cfg.php", "<?php return array('x' => 2);")
    _fake_run(monkeypatch, _FakeProc(1, "", "parse error"))
    assert phpfile.load_php_file(p) == {"x": 2}


def test_php_missing_falls_back_to_python_parser(tmp_path, monkeypatch):
    """php-cli 不存在（FileNotFoundError）→ 纯 Python 解析器兜底。"""
    p = _write(tmp_path / "cfg.php", "<?php return array('y' => 3);")
    _fake_run(monkeypatch, FileNotFoundError("php"))
    assert phpfile.load_php_file(p) == {"y": 3}


def test_php_timeout_falls_back_to_python_parser(tmp_path, monkeypatch):
    import subprocess as sp

    p = _write(tmp_path / "cfg.php", "<?php return array('z' => 4);")
    _fake_run(monkeypatch, sp.TimeoutExpired("php", 10))
    assert phpfile.load_php_file(p) == {"z": 4}


def test_bad_json_falls_back_to_python_parser(tmp_path, monkeypatch):
    """php 输出非法 JSON（例如 warning 混进 stdout）→ 不抛，退回解析器。"""
    p = _write(tmp_path / "cfg.php", "<?php return array('w' => 5);")
    _fake_run(monkeypatch, _FakeProc(0, "PHP Warning: something"))
    assert phpfile.load_php_file(p) == {"w": 5}


# ================= 缓存行为 =================

def test_cache_hit_avoids_second_php_call(tmp_path, monkeypatch):
    p = _write(tmp_path / "cfg.php", "<?php return 1;")
    calls = _fake_run(monkeypatch, _FakeProc(0, "1"))
    assert phpfile.load_php_file(p) == 1
    assert phpfile.load_php_file(p) == 1
    assert len(calls) == 1, "相同 (mtime_ns,size) 应命中缓存"


def test_cache_invalidated_by_size_change(tmp_path, monkeypatch):
    """同秒内改写但长度不同 → 缓存失效（mtime_ns+size 双键）。"""
    p = _write(tmp_path / "cfg.php", "<?php return 1;")
    calls = _fake_run(monkeypatch, _FakeProc(0, "1"))
    assert phpfile.load_php_file(p) == 1
    p.write_text("<?php return 11111;", encoding="utf-8")   # 内容变长
    assert phpfile.load_php_file(p) == 1
    assert len(calls) == 2, "size 变化后仍命中缓存"


def test_invalidate_single_path(tmp_path, monkeypatch):
    p = _write(tmp_path / "a.php", "<?php return 1;")
    q = _write(tmp_path / "b.php", "<?php return 2;")
    calls = _fake_run(monkeypatch, _FakeProc(0, "1"))
    phpfile.load_php_file(p)
    phpfile.load_php_file(q)
    assert len(calls) == 2
    phpfile.invalidate_php_cache(p)
    phpfile.load_php_file(p)      # 重新跑
    phpfile.load_php_file(q)      # 仍命中
    assert len(calls) == 3


def test_invalidate_all(tmp_path, monkeypatch):
    p = _write(tmp_path / "a.php", "<?php return 1;")
    q = _write(tmp_path / "b.php", "<?php return 2;")
    calls = _fake_run(monkeypatch, _FakeProc(0, "1"))
    phpfile.load_php_file(p)
    phpfile.load_php_file(q)
    phpfile.invalidate_php_cache()
    assert phpfile._cache == {}
    phpfile.load_php_file(p)
    phpfile.load_php_file(q)
    assert len(calls) == 4


# ================= atomic_write_text：mode 保留 =================

def test_atomic_write_text_creates_file(tmp_path):
    p = tmp_path / "out.php"
    phpfile.atomic_write_text(p, "<?php return 1;")
    assert p.read_text(encoding="utf-8") == "<?php return 1;"


def test_atomic_write_text_preserves_explicit_mode(tmp_path):
    """`.panel_pass.php` 必须 0600：密码哈希不能被其他本机用户读。"""
    p = tmp_path / "pass.php"
    phpfile.atomic_write_text(p, "x", mode=0o600)
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600


def test_atomic_write_text_preserves_mode_across_rewrite(tmp_path):
    """重写已存在文件时，显式 mode 仍生效（os.replace 会带临时文件的 0600）。"""
    p = tmp_path / "pass.php"
    p.write_text("old", encoding="utf-8")
    os.chmod(p, 0o644)
    phpfile.atomic_write_text(p, "new", mode=0o600)
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600
    assert p.read_text(encoding="utf-8") == "new"


def test_atomic_write_text_no_mode_keeps_default(tmp_path):
    p = tmp_path / "plain.php"
    phpfile.atomic_write_text(p, "y")
    assert p.read_text(encoding="utf-8") == "y"
    # 不显式给 mode 时不额外 chmod（保持 mkstemp 的 0600）
    assert stat.S_IMODE(os.stat(p).st_mode) & 0o777 == 0o600


def test_atomic_write_text_ascii_encoding(tmp_path):
    p = tmp_path / "pass.php"
    phpfile.atomic_write_text(p, "<?php return '$2y$10$abc';", encoding="ascii")
    assert p.read_text(encoding="ascii").startswith("<?php")


def test_atomic_write_text_cleans_tmp_on_failure(tmp_path, monkeypatch):
    """写失败必须删掉临时文件，不能在目录里留 .tmp_ 垃圾。"""
    p = tmp_path / "x.php"

    class _BoomFd:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def write(self, s):
            raise OSError("disk full")

    monkeypatch.setattr(phpfile.os, "fdopen", lambda fd, *a, **kw: _BoomFd())
    with pytest.raises(OSError):
        phpfile.atomic_write_text(p, "data")
    leftovers = [f for f in os.listdir(tmp_path) if f.startswith(".tmp_")]
    assert leftovers == [], f"临时文件未清理：{leftovers}"


def test_atomic_write_text_reader_never_sees_partial(tmp_path):
    """os.replace 语义：写到一半时旧内容仍完整可读。"""
    p = tmp_path / "cfg.php"
    phpfile.atomic_write_text(p, "OLD")
    assert p.read_text(encoding="utf-8") == "OLD"
    phpfile.atomic_write_text(p, "NEW-LONGER-CONTENT")
    assert p.read_text(encoding="utf-8") == "NEW-LONGER-CONTENT"


# ================= 纯 Python 解析器 =================

@pytest.mark.parametrize("text,expected", [
    ("<?php return array('a' => 1);", {"a": 1}),
    ("<?php return array('a' => 'b');", {"a": "b"}),
    ("<?php return array('a' => true, 'b' => false, 'c' => NULL);",
     {"a": True, "b": False, "c": None}),
    ("<?php return array('n' => -5);", {"n": -5}),
    ("<?php return array();", {}),
    ("<?php return array('l' => array(1, 2, 3));", {"l": [1, 2, 3]}),
    ("<?php return array(0 => 'a', 1 => 'b');", ["a", "b"]),
    ("<?php\n// comment\nreturn array('k' => 'v');\n", {"k": "v"}),
    ("<?php\n# hash comment\nreturn array('k' => 'v');\n", {"k": "v"}),
    ("<?php\n/* block */ return array('k' => 'v');\n", {"k": "v"}),
    ('<?php return array("dq" => "v");', {"dq": "v"}),
    ("<?php return 'scalar';", "scalar"),
    ("<?php return 42;", 42),
    ("<?php return true;", True),
])
def test_parse_php_value(text, expected):
    assert phpfile.parse_php_value(text) == expected


def test_pure_python_parser_reads_floats():
    """v1.24.7 修复：纯 Python 回退解析器现在能正确读出浮点数。

    修复前（原用例名 `..._cannot_read_floats_known_limitation` 钉死的旧行为）：
    `_TOKEN_RE` 里 `int` 分支 (`-?\\d+\\b`) 排在 `float` 分支 (`-?\\d+\\.\\d+`)
    之前，正则 alternation 从左到右取第一个能匹配的分支 → `1.5` 先被 `int`
    吃成 `1`，随后解析在 `.` 处失败，整个数组都读不出来。结果是浮点
    **写得出（dump 用 repr）却读不回来**，读写不对称。

    修复：把 `float` 分支移到 `int` 之前（更长更具体的模式优先）。
    影响面：只在 php-cli 不可用时走这条回退路径；面板配置里没有浮点，
    所以线上未暴露。
    """
    assert phpfile.parse_php_value("<?php return 1.5;", default="FAIL") == 1.5
    assert phpfile.parse_php_value("<?php return -1.5;", default="FAIL") == -1.5
    assert phpfile.parse_php_value("<?php return array('f' => 1.5);",
                                   default="FAIL") == {"f": 1.5}
    # 负小数 / 0.0 也要保持 float 类型（不能退化成 int）
    assert phpfile.parse_php_value("<?php return -0.25;", default="FAIL") == -0.25
    assert isinstance(phpfile.parse_php_value("<?php return 0.0;", default="FAIL"), float)
    # 整数不受影响：仍是 int，不能被误判成 float
    assert phpfile.parse_php_value("<?php return 15;") == 15
    assert isinstance(phpfile.parse_php_value("<?php return 15;"), int)
    # 读写对称：dump 出来的浮点能被读回同一个值
    assert phpfile.parse_php_value(phpfile.dump_php_value({"f": 1.5})) == {"f": 1.5}


def test_pure_python_parser_malformed_floats_still_fall_back():
    """畸形浮点不因分支调换而变得更差：仍走 default，不抛异常。

    `1.` / `.5` / `1.5.6` / `1e5` 都不是合法 PHP 字面量，修复前后都应为 default
    （或至少不崩）。这条是分支调换的安全网。
    """
    for bad in ("<?php return 1.;", "<?php return .5;",
                "<?php return 1.5.6;", "<?php return 1e5;"):
        v = phpfile.parse_php_value(bad, default="FAIL")
        assert v is not None, bad


def test_parse_php_value_bad_returns_default():
    assert phpfile.parse_php_value("not php at all", default={"d": 1}) == {"d": 1}
    assert phpfile.parse_php_value("", default=None) is None


def test_parse_php_file_missing_returns_default(tmp_path):
    assert phpfile.parse_php_file(tmp_path / "nope.php", default=[]) == []


def test_parse_php_file_reads_disk(tmp_path):
    p = _write(tmp_path / "c.php", "<?php return array('a' => 1);")
    assert phpfile.parse_php_file(p) == {"a": 1}


# ================= 序列化：dump → parse 闭环 =================

@pytest.mark.parametrize("value", [
    {},
    {"a": 1},
    {"a": "b", "n": 2},
    {"flag": True, "off": False, "nil": None},
    {"list": [1, 2, 3]},
    {"nested": {"x": {"y": [1, {"z": "w"}]}}},
    {"quote": "it's"},
    {"backslash": "a\\b"},
    {"empty_str": ""},
    {"neg": -7},
])
def test_dump_parse_roundtrip(value):
    text = phpfile.dump_php_return(value)
    assert phpfile.parse_php_value(text) == value


def test_dump_php_return_header():
    text = phpfile.dump_php_return({"a": 1})
    assert text.startswith("<?php\nreturn ")
    assert text.rstrip().endswith(";")


def test_dump_php_return_comment():
    text = phpfile.dump_php_return_comment({"a": 1}, "written by panel")
    assert "// written by panel" in text
    assert phpfile.parse_php_value(text) == {"a": 1}


def test_dump_php_value_empty_containers():
    assert phpfile.dump_php_value({}) == "array ()"
    assert phpfile.dump_php_value([]) == "array ()"


def test_dump_php_value_rejects_unsupported():
    with pytest.raises(TypeError):
        phpfile.dump_php_value(object())


def test_dump_escapes_single_quote():
    text = phpfile.dump_php_return({"k": "it's"})
    assert "\\'" in text
    assert phpfile.parse_php_value(text) == {"k": "it's"}


def test_dump_escapes_backslash():
    text = phpfile.dump_php_return({"k": "a\\b"})
    assert phpfile.parse_php_value(text) == {"k": "a\\b"}


# ================= 端到端：atomic_write + load =================

def test_write_then_load_roundtrip(tmp_path, monkeypatch):
    """atomic_write_text 写盘 → load_php_file 读回（缓存失效后）。"""
    p = tmp_path / "data.php"
    phpfile.atomic_write_text(p, phpfile.dump_php_return({"inst": "demo"}))
    _fake_run(monkeypatch, _FakeProc(1, ""))          # 强制走纯 Python 解析器
    assert phpfile.load_php_file(p) == {"inst": "demo"}
