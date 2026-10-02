"""monitor 竞态保护：PHP file_put_contents 非原子写入期间，Python 读到截断文件时
必须返回 None 哨兵且绝不能用空基线覆盖全量历史数据。"""
import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services import monitor as mon

INST = {"container": "m7a"}


@pytest.fixture(autouse=True)
def _isolated_data_dir():
    """每个用例独立 DATA_DIR：避免与其他测试模块（dispatch 的 monitor 用例）互相污染。"""
    mon.DATA_DIR = Path(tempfile.mkdtemp(prefix="m7a_mon_test_"))
    yield


def test_read_missing_file_returns_empty():
    d = mon.monitor_read(INST)
    assert d["points"] == []
    assert d["meta"]["lastSample"] == 0


def test_read_truncated_returns_none():
    f = mon.monitor_data_file(INST)
    f.write_text('{"points": [{"t": 123}], "meta": {"lastSamp', "utf-8")
    assert mon.monitor_read(INST) is None


def test_sample_never_overwrites_on_corrupt():
    f = mon.monitor_data_file(INST)
    bad = '{"points": [{"t": 1}], "meta": {"last'
    f.write_text(bad, "utf-8")
    r = mon.monitor_sample(INST, 1)
    assert r["sampled"] is False
    assert r["data"]["points"] == []
    # 关键断言：损坏文件未被采样流程覆盖
    assert f.read_text("utf-8") == bad


def test_valid_file_roundtrip():
    f = mon.monitor_data_file(INST)
    good = {"points": [{"t": 5, "cpu": 1.0}], "minutes": [],
            "meta": {"host": None, "hostTs": 0, "lastSample": 9999999999,
                     "lastMin": 0, "lastNet": None}}
    f.write_text(__import__("json").dumps(good), "utf-8")
    d = mon.monitor_read(INST)
    assert d is not None and d["points"][0]["t"] == 5
    # lastSample 是未来值（9999999999）→ 节流命中，不采样不写
    r = mon.monitor_sample(INST, 1)
    assert r["sampled"] is False
    assert r["data"]["points"][0]["t"] == 5
