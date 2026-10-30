# -*- coding: utf-8 -*-
"""v1.24.5 专项：Q-06（崩溃恢复完整性判据）+ Q-09（失败阶段区分）。

对应第三方审计报告 Q-06 / Q-09。

本文件全部离线：
- 不真跑 systemctl（run_cmd 一律 monkeypatch）
- 不联网（httpx 一律不触发：失败都发生在重启/探测之前）
- 不真建 venv（`_ensure_runtime` 用 monkeypatch 或走版本校验先失败的分支）
"""
import io
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config as cfg  # noqa: E402
import pytest  # noqa: E402

from services import updateops as U  # noqa: E402


# ================= 公共工具 =================

def _cfg_src(ver: str) -> str:
    """与 panel_migr/config.py 同形的版本声明（_version_from_text 只认这种写法）。"""
    return f'PANEL_VERSION = os.environ.get("M7A_PANEL_VERSION", "{ver}")'


def _mk_zip(files: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
        for name, content in files.items():
            z.writestr(name, content)
    return buf.getvalue()


def _release_zip(ver: str = "1.24.5") -> bytes:
    """结构合法的发布包（zip 根直接是 panel_migr/）。"""
    return _mk_zip({
        "panel_migr/config.py": _cfg_src(ver),
        "panel_migr/main.py": "NEW",
        "panel_migr/static/panel.js": "//new",
        "panel_migr/routers/__init__.py": "",
        "panel_migr/requirements.txt": "fastapi\n",
    })


# 与 updateops._CODE_VITAL 同构的关键文件集（测试侧独立声明，防止实现被改窄）
_VITAL = (
    "panel_migr/main.py",
    "panel_migr/config.py",
    "panel_migr/routers/__init__.py",
    "panel_migr/services/shell.py",
)


def _write_tree(root: Path, files: dict) -> None:
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")


def _full_code(root: Path, ver: str = "1.23.0", main: str = "OLD") -> dict:
    """一套完整的面板代码（全部关键文件齐备）。"""
    files = {rel: f"# {rel}\n" for rel in _VITAL}
    files["panel_migr/config.py"] = _cfg_src(ver)
    files["panel_migr/main.py"] = main
    files["panel_migr/requirements.txt"] = "fastapi\n"
    _write_tree(root, files)
    return files


def _install(tmp_path: Path, ver: str = "1.23.0") -> Path:
    """造一个完整安装目录（BASE_DIR 下确实有 panel_migr/ → _code_root() 取它）。"""
    base = tmp_path / "install"
    (base / "panel_migr").mkdir(parents=True)
    _full_code(base, ver)
    return base


def _patch_paths(monkeypatch, base: Path) -> None:
    monkeypatch.setattr(cfg, "BASE_DIR", base)
    monkeypatch.setattr(U, "BACKUP_DIR", base / "backups")


def _old_dir(base: Path, name: str = ".old_20261030000000_1_ab") -> Path:
    d = base / "backups" / name
    d.mkdir(parents=True, exist_ok=True)
    return d


# ================= Q-06：完整性判据 =================

def test_q06_code_intact_reports_missing_vital_file():
    """`_code_intact` 必须在任一关键文件缺失时判为不完整，并给出原因。"""
    # 用 tmp 目录直接造两棵树，不依赖 cfg
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "root"
        old = Path(td) / "old"
        _write_tree(root, {"panel_migr/main.py": "NEW", "panel_migr/config.py": _cfg_src("1.24.5")})
        _write_tree(old, {rel: "x" for rel in _VITAL})
        ok, why = U._code_intact(root, old)
        assert ok is False
        assert "routers/__init__.py" in why or "services/shell.py" in why


def test_q06_code_intact_true_when_all_vital_present():
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "root"
        old = Path(td) / "old"
        _full_code(root, "1.24.5")
        _write_tree(old, {rel: "x" for rel in _VITAL})
        ok, why = U._code_intact(root, old)
        assert ok is True, why


def test_q06_code_intact_false_when_version_unreadable():
    """config.py 读不出版本号 → 宁可换回也不删（0 字节是换入中断的典型半成品）。"""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "root"
        old = Path(td) / "old"
        _write_tree(root, {"panel_migr/main.py": "NEW", "panel_migr/config.py": ""})
        _write_tree(old, {"panel_migr/main.py": "OLD"})
        ok, why = U._code_intact(root, old)
        assert ok is False
        assert "版本号" in why


def test_q06_code_intact_false_when_venv_stash_left():
    """old/ 里残留 .venv_stash_* → 换入没走完，不能删。"""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "root"
        old = Path(td) / "old"
        _full_code(root, "1.24.5")
        _write_tree(old, {"panel_migr/main.py": "OLD"})
        (old / ".venv_stash_panel_migr").mkdir(parents=True)
        ok, why = U._code_intact(root, old)
        assert ok is False
        assert "venv_stash" in why


def test_q06_recover_restores_when_main_py_present_but_code_half(monkeypatch, tmp_path):
    """Q-06 核心回归：main.py 已换入、其余关键文件还是半截 → 必须换回，不能删 old/。

    旧判据只看 `main.py` 存在且非空，会把这种半成品当成「换入已完成」，
    于是 rmtree 掉唯一完整的 .old_* —— 旧代码永久丢失。
    """
    base = tmp_path / "install"
    # 半截的线上代码：只有 main.py（新），config.py / routers / services 全缺
    _write_tree(base, {"panel_migr/main.py": "NEW-HALF"})
    monkeypatch.setattr(cfg, "BASE_DIR", base)
    monkeypatch.setattr(U, "BACKUP_DIR", base / "backups")

    old = _old_dir(base)
    _full_code(old, "1.23.0", main="OLD-GOOD")

    out = U._recover_interrupted()

    assert out["recovered"] == [old.name]
    assert not old.exists()
    # 旧代码被换回线上位置
    assert (base / "panel_migr" / "main.py").read_text(encoding="utf-8") == "OLD-GOOD"
    assert (base / "panel_migr" / "config.py").is_file()
    assert any("不完整" in n for n in out["notes"])


def test_q06_recover_restores_when_version_unreadable(monkeypatch, tmp_path):
    """`_installed_version()` 读不到（config.py 被截断）时按「不完整」处理。"""
    base = tmp_path / "install"
    _write_tree(base, {"panel_migr/main.py": "NEW", "panel_migr/config.py": ""})
    monkeypatch.setattr(cfg, "BASE_DIR", base)
    monkeypatch.setattr(U, "BACKUP_DIR", base / "backups")

    old = _old_dir(base, ".old_20261030000001_1_ab")
    _full_code(old, "1.23.0", main="OLD-GOOD")

    out = U._recover_interrupted()

    assert out["recovered"] == [old.name]
    assert (base / "panel_migr" / "main.py").read_text(encoding="utf-8") == "OLD-GOOD"


def test_q06_recover_removes_old_when_code_really_intact(monkeypatch, tmp_path):
    """关键文件全齐 + 版本号可读 → 换入已完成，old/ 只是没清理，删掉。"""
    base = _install(tmp_path, "1.24.5")
    _patch_paths(monkeypatch, base)

    old = _old_dir(base)
    _write_tree(old, {rel: "x" for rel in _VITAL})

    out = U._recover_interrupted()

    assert out["removed"] == [old.name]
    assert out["recovered"] == []
    assert not old.exists()


def test_q06_recover_keeps_old_when_venv_stash_left(monkeypatch, tmp_path):
    """old/ 里还有 .venv_stash_* → 换入未走完，必须换回而不是删。"""
    base = _install(tmp_path, "1.24.5")
    _patch_paths(monkeypatch, base)

    old = _old_dir(base)
    _write_tree(old, {rel: "x" for rel in _VITAL})
    (old / ".venv_stash_panel_migr").mkdir()

    out = U._recover_interrupted()

    assert out["recovered"] == [old.name]
    assert not old.exists()
    # .venv 被放回到换回的代码目录
    assert (base / "panel_migr" / ".venv").is_dir()


def test_q06_recover_still_removes_pure_stage(monkeypatch, tmp_path):
    """.stage_* 永远直接删（换入前的解压目录，无保留价值）。"""
    base = _install(tmp_path)
    _patch_paths(monkeypatch, base)

    stage = base / "backups" / ".stage_20261030000002_1_ab"
    stage.mkdir(parents=True)
    (stage / "panel_migr").mkdir()

    out = U._recover_interrupted()

    assert out["removed"] == [stage.name]
    assert not stage.exists()


def test_q06_installed_version_in_reads_arbitrary_root(tmp_path):
    """`_installed_version_in` 必须能读任意代码根的版本（恢复判据依赖它）。"""
    root = tmp_path / "somewhere"
    _write_tree(root, {"panel_migr/config.py": _cfg_src("1.22.9")})
    assert U._installed_version_in(root) == "1.22.9"
    assert U._installed_version_in(tmp_path / "nope") == ""


# ================= Q-09：失败阶段区分 =================

def _assert_no_code_touched(res: dict, base: Path) -> None:
    """「尚未改动线上代码」的统一断言：不谎称回滚、无残留、旧代码原样。"""
    assert res["ok"] is False
    assert res["rolled_back"] is False
    assert res["restored"] == 0
    assert "尚未改动线上代码" in res["msg"]
    assert "已回滚" not in res["msg"]
    assert not list((base / "backups").glob(".old_*"))
    assert not list((base / "backups").glob(".stage_*"))
    assert (base / "panel_migr" / "main.py").read_text(encoding="utf-8") == "OLD"


def test_q09_non_zip_reports_code_untouched(monkeypatch, tmp_path):
    """非 zip 字节：解压阶段就失败，线上代码一个字节都没动 → 不能说「已回滚」。"""
    base = _install(tmp_path)
    _patch_paths(monkeypatch, base)

    res = U._apply_zip(b"not a zip at all", "1.24.5")

    _assert_no_code_touched(res, base)
    assert res["phase"] == "extract"


def test_q09_extract_bomb_reports_code_untouched(monkeypatch, tmp_path):
    """解压防护抛异常（zip bomb 超限）同样属于「未改动线上代码」。"""
    base = _install(tmp_path)
    _patch_paths(monkeypatch, base)

    def boom(data, stage):
        raise RuntimeError("更新包解压后总大小超限（>209715200B），已中止解压")

    monkeypatch.setattr(U, "_extract_zip", boom)

    res = U._apply_zip(_release_zip(), "1.24.5")

    _assert_no_code_touched(res, base)
    assert "总大小超限" in res["msg"]


def test_q09_structure_error_reports_code_untouched(monkeypatch, tmp_path):
    """带外层目录的包：_swap_stage 在动任何文件前就抛结构错误 → 未改动线上代码。"""
    base = _install(tmp_path)
    _patch_paths(monkeypatch, base)

    wrapped = _mk_zip({
        "m7a_panel_V1.24.5/panel_migr/config.py": _cfg_src("1.24.5"),
        "m7a_panel_V1.24.5/panel_migr/main.py": "NEW",
        "m7a_panel_V1.24.5/panel_migr/static/panel.js": "//new",
        "m7a_panel_V1.24.5/panel_migr/routers/__init__.py": "",
    })

    res = U._apply_zip(wrapped, "1.24.5")

    _assert_no_code_touched(res, base)
    assert "发布包结构错误" in res["msg"]


def test_q09_failure_after_swap_still_says_rolled_back(monkeypatch, tmp_path):
    """真正换入过之后的失败（版本校验不通过）→ 必须如实说「已回滚」。"""
    base = _install(tmp_path)
    _patch_paths(monkeypatch, base)
    # 换入后版本复核读到不符 → 进入回滚分支
    monkeypatch.setattr(U, "_installed_version", lambda: "0.0.0")

    res = U._apply_zip(_release_zip("1.24.5"), "1.24.5")

    assert res["ok"] is False
    assert res["rolled_back"] is True
    assert res["restored"] >= 1
    assert "已回滚" in res["msg"]
    assert res["phase"] == "verify"
    # 回滚成功后清理干净，且线上代码恢复为旧版
    assert not list((base / "backups").glob(".old_*"))
    assert (base / "panel_migr" / "main.py").read_text(encoding="utf-8") == "OLD"


def test_q09_rollback_failure_keeps_old_and_reports(monkeypatch, tmp_path):
    """回滚失败：不删 old/、不谎称已回滚，并给出人工恢复路径（P0-2 语义不回退）。"""
    base = _install(tmp_path)
    _patch_paths(monkeypatch, base)
    monkeypatch.setattr(U, "_installed_version", lambda: "0.0.0")
    monkeypatch.setattr(U, "_restore_old", lambda old: {
        "ok": False, "restored": 0, "failed": 1, "errors": ["恢复 panel_migr 失败：只读文件系统"],
    })

    res = U._apply_zip(_release_zip("1.24.5"), "1.24.5")

    assert res["ok"] is False
    assert res["rolled_back"] is False
    assert "已回滚" not in res["msg"]
    assert "自动回滚未完成" in res["msg"]
    assert "旧代码已保留在" in res["msg"]
    assert Path(res["old_dir"]).is_dir()
    assert list((base / "backups").glob(".old_*"))


def test_q09_old_has_content_helper(tmp_path):
    """`_old_has_content` 是阶段判据的基础：空目录/不存在 → False，有东西 → True。"""
    empty = tmp_path / "empty"
    empty.mkdir()
    assert U._old_has_content(empty) is False
    assert U._old_has_content(tmp_path / "missing") is False
    (empty / "panel_migr").mkdir()
    assert U._old_has_content(empty) is True
