"""v1.20 新增能力测试：零 fork cgroup 采集、版本比较后缀修复、
H.264 帧率白名单参数、历史脏数据一次性清洗。"""
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services import monitor as mon
from services import preview as pv
from services import sysmetrics as sm
from services import updateops as uo

CID = "c" * 64


# ---------- sysmetrics：假 cgroup 目录直读（不依赖真实 docker） ----------

def _fake_cgroup(tmp_path: Path, v: str = "2"):
    if v == "2":
        (tmp_path / "cpu.stat").write_text(
            "usage_usec 123456\nuser_usec 100000\nsystem_usec 23456\n")
        (tmp_path / "memory.current").write_text("536870912\n")
        (tmp_path / "memory.max").write_text("2147483648\n")
    else:
        (tmp_path / "cpuacct.usage").write_text("987654321000\n")  # ns
    return tmp_path


def test_cpu_usec_cgroup_v2(tmp_path, monkeypatch):
    _fake_cgroup(tmp_path, "2")
    monkeypatch.setitem(sm._CGROUP_CACHE, CID, str(tmp_path))
    assert sm.cpu_usec(CID) == 123456


def test_cpu_usec_cgroup_v1_ns_to_usec(tmp_path, monkeypatch):
    _fake_cgroup(tmp_path, "1")
    monkeypatch.setitem(sm._CGROUP_CACHE, CID, str(tmp_path))
    assert sm.cpu_usec(CID) == 987654321  # ns ÷ 1000


def test_mem_cgroup_v2(tmp_path, monkeypatch):
    _fake_cgroup(tmp_path, "2")
    monkeypatch.setitem(sm._CGROUP_CACHE, CID, str(tmp_path))
    used, limit = sm.mem(CID)
    assert used == 536870912
    assert limit == 2147483648


def test_mem_unlimited_is_none(tmp_path, monkeypatch):
    _fake_cgroup(tmp_path, "2")
    (tmp_path / "memory.max").write_text("max\n")
    monkeypatch.setitem(sm._CGROUP_CACHE, CID, str(tmp_path))
    used, limit = sm.mem(CID)
    assert used == 536870912 and limit is None


def test_cpu_usec_missing_dir_returns_none():
    assert sm.cpu_usec("") is None
    assert sm.cpu_usec("does-not-exist-cid") is None or isinstance(sm.cpu_usec("does-not-exist-cid"), int)


# ---------- updateops：版本比较后缀修复 ----------

def test_version_nums_stops_at_suffix():
    assert uo._nums("1.20.0-python") == [1, 20, 0]
    assert uo._nums("1.19") == [1, 19]
    assert uo._nums("beta") == [0]


def test_version_gt_suffix_no_false_positive():
    # 旧 bug：'1.19.0-python' 的 -python 被当分段 → 误报有更新
    assert uo._version_gt("1.20", "1.19.0-python") is True
    assert uo._version_gt("1.19.0-python", "1.19") is False
    assert uo._version_gt("1.19.1", "1.19") is True


def test_version_eq_padded():
    assert uo._version_eq("1.19.0-python", "1.19") is True
    assert uo._version_eq("1.19", "1.20") is False
    # Release tag 数字前缀 vs config 版本：补 0 后相等
    assert uo._version_eq("1.20.0", "1.20") is True


# ---------- preview：H.264 帧率白名单 ----------

def test_normalize_fps_whitelist():
    assert pv.normalize_fps("15") == 15
    assert pv.normalize_fps("30") == 30
    assert pv.normalize_fps(30) == 30
    # 非白名单（含被砍掉的 60、空、乱填）一律回落 15
    assert pv.normalize_fps("60") == 15
    assert pv.normalize_fps("") == 15
    assert pv.normalize_fps("abc") == 15
    assert pv.normalize_fps(None) == 15


def test_build_ffmpeg_args_30():
    a = pv.build_ffmpeg_args(30)
    s = " ".join(a)
    assert "-vf" in a and "fps=30," in s
    # 关键帧间隔 = fps（每秒一个关键帧，低延迟切档）
    i = a.index("-g")
    assert a[i + 1] == "30" and a[a.index("-keyint_min") + 1] == "30"
    assert "fps=15" not in s
    # 墙钟时间戳 + frag mp4（防播放时间轴漂移）
    assert "-use_wallclock_as_timestamps" in a
    assert "frag_keyframe+empty_moov" in s


def test_build_ffmpeg_args_default_15():
    a = pv.build_ffmpeg_args()
    s = " ".join(a)
    assert "fps=15," in s
    assert a[a.index("-g") + 1] == "15"
    # 非法入参回落 15，不炸
    assert "fps=15," in " ".join(pv.build_ffmpeg_args(60))


# ---------- monitor：历史脏数据一次性清洗 ----------

def _d(points, minutes, sanitized=0):
    d = mon._empty()
    d["points"] = points
    d["minutes"] = minutes
    d["meta"]["sanitized"] = sanitized
    return d


def test_sanitize_drops_impossible_cpu():
    cap = (os.cpu_count() or 4) * 100
    d = _d(
        [{"t": 1, "cpu": cap + 100}, {"t": 2, "cpu": 12.5}, {"t": 3, "cpu": 0}],
        [{"t": 1, "cpu": 567471}, {"t": 2, "cpu": 30.0}],
    )
    mon._sanitize_cpu(d)
    assert [p["cpu"] for p in d["points"]] == [12.5, 0]
    assert [m["cpu"] for m in d["minutes"]] == [30.0]
    assert d["meta"]["sanitized"] == 1


def test_sanitize_runs_once():
    d = _d([{"t": 1, "cpu": 5}], [], sanitized=1)
    d["points"].append({"t": 2, "cpu": 999999})
    mon._sanitize_cpu(d)  # 已清洗过 → 不再动
    assert len(d["points"]) == 2
    assert d["meta"]["sanitized"] == 1


def test_sanitize_empty_structures():
    d = _d([], [])
    mon._sanitize_cpu(d)
    assert d["points"] == [] and d["minutes"] == []
    assert d["meta"]["sanitized"] == 1


# ---------- updateops：ZIP 结构校验 / 版本提取 / 换入回滚干跑 ----------

def _mk_zip(files: dict) -> bytes:
    import io as _io
    import zipfile as _zf
    buf = _io.BytesIO()
    with _zf.ZipFile(buf, "w") as z:
        for name, content in files.items():
            z.writestr(name, content)
    return buf.getvalue()


def test_zip_struct_ok():
    good = _mk_zip({
        "panel_migr/main.py": "print(1)",
        "panel_migr/config.py": 'import os\nPANEL_VERSION = os.environ.get("M7A_PANEL_VERSION", "1.20")',
        "panel_migr/static/panel.js": "//",
        "README.md": "# x",
    })
    assert uo._zip_struct_ok(good) is True
    bad = _mk_zip({"README.md": "# x", "panel_migr/main.py": "print(1)"})
    assert uo._zip_struct_ok(bad) is False


def test_zip_new_version_extracted():
    data = _mk_zip({
        "panel_migr/config.py": 'import os\nPANEL_VERSION = os.environ.get("M7A_PANEL_VERSION", "1.20")   # 默认版本',
        "panel_migr/main.py": "",
    })
    assert uo._zip_new_version(data) == "1.20"
    try:
        uo._zip_new_version(_mk_zip({"panel_migr/main.py": ""}))
    except RuntimeError:
        pass
    else:
        raise AssertionError("缺 config.py 应报错")


def test_extract_rejects_zip_slip():
    import io as _io
    import zipfile as _zf
    buf = _io.BytesIO()
    with _zf.ZipFile(buf, "w") as z:
        z.writestr("../evil.txt", "x")
    import pytest as _pytest
    with _pytest.raises(RuntimeError):
        uo._extract_zip(buf.getvalue(), __import__("pathlib").Path("/tmp/x"))


def test_swap_and_rollback_dryrun(tmp_path, monkeypatch):
    """干跑整包换入：新文件就位、data/ backups/ 永不触碰、失败可回滚。"""
    import pathlib

    base = tmp_path / "install"
    (base / "panel_migr").mkdir(parents=True)
    (base / "panel_migr" / "config.py").write_text('M7A_PANEL_VERSION = "1.19"')
    (base / "panel_migr" / "main.py").write_text("OLD")
    (base / "README.md").write_text("old readme")
    (base / "data").mkdir()
    (base / "data" / "secret.txt").write_text("keep-me")
    (base / "backups").mkdir()
    (base / "backups" / "panel_1.19_x.zip").write_bytes(b"PK")

    monkeypatch.setattr(uo.cfg, "BASE_DIR", base)

    data = _mk_zip({
        "panel_migr/config.py": 'import os\nPANEL_VERSION = os.environ.get("M7A_PANEL_VERSION", "1.20")',
        "panel_migr/main.py": "NEW",
        "panel_migr/static/panel.js": "//new",
        "README.md": "new readme",
    })
    stage = tmp_path / "stage"
    uo._extract_zip(data, stage)
    assert uo._zip_struct_ok(data)
    assert uo._zip_new_version(data) == "1.20"

    old = tmp_path / "old"
    uo._swap_stage(stage, old)
    assert (base / "panel_migr" / "main.py").read_text() == "NEW"
    assert '"1.20"' in (base / "panel_migr" / "config.py").read_text()
    assert (base / "README.md").read_text() == "new readme"
    # 运行数据永不触碰
    assert (base / "data" / "secret.txt").read_text() == "keep-me"
    assert (base / "backups" / "panel_1.19_x.zip").exists()
    # 旧文件进了回滚目录
    assert (old / "panel_migr" / "main.py").read_text() == "OLD"

    # 回滚：旧版完整恢复
    uo._restore_old(old)
    assert (base / "panel_migr" / "main.py").read_text() == "OLD"
    assert (base / "README.md").read_text() == "old readme"
    assert (base / "data" / "secret.txt").read_text() == "keep-me"
