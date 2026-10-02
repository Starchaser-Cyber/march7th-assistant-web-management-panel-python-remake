"""phpsess 单测：PHP 序列化格式解析 + empty 语义。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from phpsess import parse_session, php_empty


def test_basic_types():
    text = 'auth|b:1;name|s:5:"hello";n|i:42;d|d:1.5;nothing|N;'
    d = parse_session(text)
    assert d["auth"] is True
    assert d["name"] == "hello"
    assert d["n"] == 42
    assert d["d"] == 1.5
    assert d["nothing"] is None


def test_nested_array():
    text = 'cfg|a:2:{s:3:"foo";s:3:"bar";i:0;b:0;}'
    d = parse_session(text)
    assert d["cfg"]["foo"] == "bar"
    assert d["cfg"][0] is False


def test_instance_key():
    text = 'm7a_instance|s:3:"m7a";m7a_panel_auth|b:1;'
    d = parse_session(text)
    assert d["m7a_instance"] == "m7a"
    assert d["m7a_panel_auth"] is True


def test_corrupted_keeps_parsed_prefix():
    text = 'm7a_panel_auth|b:1;broken|???'
    d = parse_session(text)
    assert d["m7a_panel_auth"] is True   # 损坏前的键保留


def test_php_empty_semantics():
    assert php_empty(None) and php_empty(False)
    assert php_empty(0) and php_empty("") and php_empty("0")
    assert php_empty([]) and php_empty({})
    assert not php_empty(True) and not php_empty("x") and not php_empty(1)
