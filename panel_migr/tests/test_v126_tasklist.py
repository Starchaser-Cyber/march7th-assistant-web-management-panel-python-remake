"""V1.26 动态任务表（services/tasklist.py）单元测试。

任务表的价值在于「面板别替容器做主」：真实支持哪些任务由容器 `main.py --list`
说了算。所以这里重点覆盖解析鲁棒性、缓存、探测失败回落。
"""
import json
import time

import pytest

import config as cfg
from services import tasklist

REAL_OUTPUT = """可用的任务列表:
----------------------------------------
  main                 完整运行
  routine              日常
  daily                每日实训
  power                清体力
  game_update          更新游戏
  divergent            差分宇宙
  fight                锄大地
  universe             模拟宇宙
  forgottenhall        混沌回忆
  currencywarsloop     货币战争循环
  screen_test          界面可切换性测试
----------------------------------------

使用示例:
  python main.py daily
"""

INST = {"id": "t1", "container": "c1", "dir": "/srv/m7a"}


@pytest.fixture()
def inst(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "DATA_DIR", tmp_path / "data")
    (tmp_path / "data").mkdir()
    tasklist._MEM.clear()
    yield {"id": "t1", "container": "c1", "dir": str(tmp_path / "inst")}
    tasklist._MEM.clear()


# ===================== 解析 =====================

def test_parse_real_output():
    items = tasklist.parse_list_output(REAL_OUTPUT)
    assert len(items) == 11
    assert items[0] == {"key": "main", "label": "完整运行"}
    assert items[-1] == {"key": "screen_test", "label": "界面可切换性测试"}


def test_parse_skips_header_and_separators():
    keys = [i["key"] for i in tasklist.parse_list_output(REAL_OUTPUT)]
    assert "可用的任务列表" not in keys
    assert not any(k.startswith("-") for k in keys)


def test_parse_skips_usage_section():
    keys = [i["key"] for i in tasklist.parse_list_output(REAL_OUTPUT)]
    assert "python" not in keys


def test_parse_empty():
    assert tasklist.parse_list_output("") == []
    assert tasklist.parse_list_output("no tasks here") == []


def test_parse_dedupes():
    txt = "  main    完整运行\n  main    完整运行（重复）\n"
    items = tasklist.parse_list_output(txt)
    assert len(items) == 1


def test_parse_handles_wide_label_spacing():
    txt = "  a_task                  带空格的说明文字\n"
    items = tasklist.parse_list_output(txt)
    assert items[0]["key"] == "a_task"
    assert items[0]["label"] == "带空格的说明文字"


# ===================== probe =====================

def test_probe_success(inst, monkeypatch):
    monkeypatch.setattr(tasklist, "run_cmd",
                        lambda *_a, **_k: {"code": 0, "out": REAL_OUTPUT})
    r = tasklist.probe_tasks(inst)
    assert r["source"] == "probe"
    assert len(r["tasks"]) == 11
    assert r["err"] == ""


def test_probe_caches_in_memory(inst, monkeypatch):
    calls = []

    def fake(*_a, **_k):
        calls.append(1)
        return {"code": 0, "out": REAL_OUTPUT}
    monkeypatch.setattr(tasklist, "run_cmd", fake)
    tasklist.probe_tasks(inst)
    tasklist.probe_tasks(inst)
    assert len(calls) == 1


def test_probe_force_bypasses_cache(inst, monkeypatch):
    calls = []

    def fake(*_a, **_k):
        calls.append(1)
        return {"code": 0, "out": REAL_OUTPUT}
    monkeypatch.setattr(tasklist, "run_cmd", fake)
    tasklist.probe_tasks(inst)
    tasklist.probe_tasks(inst, force=True)
    assert len(calls) == 2


def test_probe_falls_back_to_disk_cache(inst, monkeypatch):
    monkeypatch.setattr(tasklist, "run_cmd",
                        lambda *_a, **_k: {"code": 0, "out": REAL_OUTPUT})
    tasklist.probe_tasks(inst)
    tasklist._MEM.clear()                     # 模拟进程重启
    monkeypatch.setattr(tasklist, "run_cmd", lambda *_a, **_k: {"code": 1, "out": "boom"})
    r = tasklist.probe_tasks(inst, force=True)
    assert r["source"] == "cache"
    assert len(r["tasks"]) == 11


def test_probe_failure_without_cache(inst, monkeypatch):
    monkeypatch.setattr(tasklist, "run_cmd", lambda *_a, **_k: {"code": 1, "out": "no such container"})
    r = tasklist.probe_tasks(inst)
    assert r["tasks"] == []
    assert "no such container" in r["err"]


def test_probe_tolerates_garbage_output(inst, monkeypatch):
    monkeypatch.setattr(tasklist, "run_cmd", lambda *_a, **_k: {"code": 0, "out": "\x00\x01garbage"})
    r = tasklist.probe_tasks(inst)
    assert r["tasks"] == []


# ===================== tasks / grouped / resolve =====================

def _patched(inst, monkeypatch, out=REAL_OUTPUT):
    monkeypatch.setattr(tasklist, "run_cmd", lambda *_a, **_k: {"code": 0, "out": out})


def test_tasks_annotates_group_and_pausable(inst, monkeypatch):
    _patched(inst, monkeypatch)
    ts = tasklist.tasks(inst)
    by = {t["key"]: t for t in ts}
    assert by["main"]["group"] == "core"
    assert by["main"]["pausable"] is True
    assert by["game_update"]["pausable"] is False
    assert by["game_update"]["group"] == "update"


def test_tasks_falls_back_to_config_when_probe_empty(inst, monkeypatch):
    monkeypatch.setattr(tasklist, "run_cmd", lambda *_a, **_k: {"code": 1, "out": ""})
    ts = tasklist.tasks(inst)
    assert {t["key"] for t in ts} == set(cfg.TASKS)


def test_grouped_returns_six_groups(inst, monkeypatch):
    _patched(inst, monkeypatch, out=REAL_OUTPUT + "".join(
        f"  {k}    {k}\n" for k in ("game", "redemption", "notify")))
    groups = tasklist.grouped(inst)
    assert 1 <= len(groups) <= 6
    keys = [g["key"] for g in groups]
    assert keys == [g for g in ("core", "weekly", "loop", "native", "update", "misc") if g in keys]


def test_grouped_skips_empty_groups(inst, monkeypatch):
    _patched(inst, monkeypatch, out="  main    完整运行\n")
    groups = tasklist.grouped(inst)
    assert len(groups) == 1 and groups[0]["key"] == "core"


def test_grouped_items_have_label(inst, monkeypatch):
    _patched(inst, monkeypatch)
    for g in tasklist.grouped(inst):
        assert g["label"]
        for it in g["items"]:
            assert it["label"]


def test_resolve_accepts_known(inst, monkeypatch):
    _patched(inst, monkeypatch)
    assert tasklist.resolve(inst, "main") == ""


def test_resolve_rejects_unknown(inst, monkeypatch):
    _patched(inst, monkeypatch)
    err = tasklist.resolve(inst, "nope")
    assert "nope" in err


def test_resolve_falls_back_to_config_whitelist(inst, monkeypatch):
    """容器探测失败时不能把已有任务全禁掉。"""
    monkeypatch.setattr(tasklist, "run_cmd", lambda *_a, **_k: {"code": 1, "out": ""})
    assert tasklist.resolve(inst, "daily") == ""
    assert tasklist.resolve(inst, "nope") != ""


def test_label_of_known(inst, monkeypatch):
    _patched(inst, monkeypatch)
    assert tasklist.label_of(inst, "main") == "完整运行"


def test_label_of_unknown_returns_key(inst, monkeypatch):
    _patched(inst, monkeypatch)
    assert tasklist.label_of(inst, "zzz") == "zzz"


# ===================== 缓存文件 =====================

def test_cache_file_path(inst):
    p = tasklist._cache_file(inst)
    assert p.name.startswith("tasklist_") and p.name.endswith(".json")


def test_cache_file_sanitizes_container(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "DATA_DIR", tmp_path)
    p = tasklist._cache_file({"container": "a/b c", "dir": "/x"})
    assert "/" not in p.name


def test_probe_writes_disk_cache(inst, monkeypatch):
    _patched(inst, monkeypatch)
    tasklist.probe_tasks(inst)
    f = tasklist._cache_file(inst)
    assert f.exists()
    d = json.loads(f.read_text("utf-8"))
    assert len(d["tasks"]) == 11
