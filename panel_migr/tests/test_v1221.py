# -*- coding: utf-8 -*-
"""v1.22.1 专项：一键更新的运行环境兜底。

真实故障：更新包不含 panel_migr/.venv，若更新开始前运行环境已缺失，换入新代码后
systemd 找不到 panel_migr/.venv/bin/python → 203/EXEC 反复重启，面板打不开。

本文件全部离线，禁止真实网络请求与真实创建 venv：
- _ensure_runtime(rebuild) 一律注入假 runner（runner(cmd, timeout) -> {"code","out"}）；
- _apply_zip / do_update 走 monkeypatch 的受控分支。
"""
import sys
import shutil
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config as cfg  # noqa: E402
import pytest  # noqa: E402

from services import updateops as U  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------- 公共工具 ----------

def _mk_zip(files: dict) -> bytes:
    import io as _io
    buf = _io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, content in files.items():
            z.writestr(name, content)
    return buf.getvalue()


def _mk_venv(base: Path, executable: bool = True) -> Path:
    """在 base/panel_migr/.venv/bin/python 造一个可执行占位文件。"""
    py = base / "panel_migr" / ".venv" / "bin" / "python"
    py.parent.mkdir(parents=True, exist_ok=True)
    py.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    py.chmod(0o755 if executable else 0o644)
    return py


def _new_zip(files: dict | None = None) -> bytes:
    files = files or {}
    base = {
        "panel_migr/config.py": 'import os\nPANEL_VERSION = os.environ.get("M7A_PANEL_VERSION", "1.22.1")',
        "panel_migr/main.py": "NEW",
        "panel_migr/static/panel.js": "//new",
        "panel_migr/requirements.txt": "fastapi\n",
    }
    base.update(files)
    return _mk_zip(base)


def _setup_install(tmp_path: Path) -> Path:
    """造一个旧版安装目录：config=1.21.1、main.py=OLD、.venv 就绪。"""
    base = tmp_path / "install"
    (base / "panel_migr").mkdir(parents=True)
    (base / "panel_migr" / "config.py").write_text(
        'M7A_PANEL_VERSION = "1.21.1"', encoding="utf-8")
    (base / "panel_migr" / "main.py").write_text("OLD", encoding="utf-8")
    (base / "panel_migr" / "requirements.txt").write_text("fastapi\n", encoding="utf-8")
    _mk_venv(base)
    return base


def _patch_paths(monkeypatch, base: Path) -> None:
    """_apply_zip 用 backups_dir()（模块级 BACKUP_DIR）+ cfg.BASE_DIR，两个都要指向 tmp。"""
    monkeypatch.setattr(cfg, "BASE_DIR", base)
    monkeypatch.setattr(U, "BACKUP_DIR", base / "backups")


# ================= 1. .venv 存在时更新后仍在（回归）=================

def test_apply_zip_keeps_existing_venv(monkeypatch, tmp_path):
    base = _setup_install(tmp_path)
    _patch_paths(monkeypatch, base)

    res = U._apply_zip(_new_zip(), "1.22.1")

    assert res["ok"] is True
    assert res["rebuilt"] is False
    assert (base / "panel_migr" / "main.py").read_text(encoding="utf-8") == "NEW"
    assert '"1.22.1"' in (base / "panel_migr" / "config.py").read_text(encoding="utf-8")
    assert U._runtime_python_ok(base / "panel_migr" / ".venv")     # 运行环境未丢
    assert not list((base / "backups").glob(".stage_*"))           # 中间目录已清理
    assert not list((base / "backups").glob(".old_*"))


def test_swap_stage_preserves_venv_symlink(monkeypatch, tmp_path):
    """v1.22.1：.venv 为符号链接时也不能被静默丢弃（lexists 保护）。"""
    base, stage, old = tmp_path / "base", tmp_path / "stage", tmp_path / "old"
    monkeypatch.setattr(cfg, "BASE_DIR", base)

    real_venv = tmp_path / "real_venv"
    (real_venv / "bin").mkdir(parents=True)
    (real_venv / "bin" / "python").write_text("#!/bin/sh\n", encoding="utf-8")
    (base / "panel_migr").mkdir(parents=True)
    (base / "panel_migr" / "main.py").write_text("OLD", encoding="utf-8")
    (base / "panel_migr" / ".venv").symlink_to(real_venv)

    (stage / "panel_migr").mkdir(parents=True)
    (stage / "panel_migr" / "main.py").write_text("NEW", encoding="utf-8")

    U._swap_stage(stage, old)

    assert (base / "panel_migr" / "main.py").read_text(encoding="utf-8") == "NEW"
    assert (base / "panel_migr" / ".venv").is_symlink()            # 链接形态保留
    assert not (old / ".venv_stash_panel_migr").exists()


# ================= 2. .venv 缺失时 _ensure_runtime() 会重建 =================

def test_ensure_runtime_ok_when_present(tmp_path):
    base = tmp_path
    (base / "panel_migr").mkdir(parents=True)
    (base / "panel_migr" / "requirements.txt").write_text("fastapi\n", encoding="utf-8")
    _mk_venv(base)

    def _boom(cmd, timeout=120):                    # 不应执行任何命令
        raise AssertionError(f"运行环境已就绪却执行了命令：{cmd}")

    r = U._ensure_runtime(runner=_boom, base_dir=base)
    assert r["ok"] is True and r["rebuilt"] is False


def test_ensure_runtime_rebuilds_when_missing(tmp_path):
    base = tmp_path
    (base / "panel_migr").mkdir(parents=True)
    (base / "panel_migr" / "requirements.txt").write_text("fastapi\n", encoding="utf-8")
    calls: list[str] = []

    def fake(cmd, timeout=120):
        calls.append(cmd)
        if "-m venv" in cmd:                        # 假装创建出可执行 python
            v = base / "panel_migr" / ".venv" / "bin"
            v.mkdir(parents=True, exist_ok=True)
            py = v / "python"
            py.write_text("#!/bin/sh\n", encoding="utf-8")
            py.chmod(0o755)
        return {"code": 0, "out": ""}

    r = U._ensure_runtime(runner=fake, base_dir=base)

    assert r["ok"] is True and r["rebuilt"] is True
    assert U._runtime_python_ok(base / "panel_migr" / ".venv")
    assert any("-m venv" in c for c in calls)
    assert any("install" in c and "requirements.txt" in c for c in calls)


def test_ensure_runtime_falls_back_to_mirror(tmp_path):
    """默认源安装失败 → 回退清华源。"""
    base = tmp_path
    (base / "panel_migr").mkdir(parents=True)
    (base / "panel_migr" / "requirements.txt").write_text("fastapi\n", encoding="utf-8")
    seen: list[str] = []

    def fake(cmd, timeout=120):
        seen.append(cmd)
        if "-m venv" in cmd:
            v = base / "panel_migr" / ".venv" / "bin"
            v.mkdir(parents=True, exist_ok=True)
            py = v / "python"
            py.write_text("#!/bin/sh\n", encoding="utf-8")
            py.chmod(0o755)
            return {"code": 0, "out": ""}
        if "install" in cmd and "-i" not in cmd:    # 默认源失败
            return {"code": 1, "out": "default index failed"}
        return {"code": 0, "out": ""}

    r = U._ensure_runtime(runner=fake, base_dir=base)
    assert r["ok"] is True
    assert any("pypi.tuna.tsinghua.edu.cn" in c for c in seen)


def test_ensure_runtime_failure_reports_reason(tmp_path):
    base = tmp_path
    (base / "panel_migr").mkdir(parents=True)
    (base / "panel_migr" / "requirements.txt").write_text("fastapi\n", encoding="utf-8")

    def fake(cmd, timeout=120):
        return {"code": 1, "out": "env create boom"}

    r = U._ensure_runtime(runner=fake, base_dir=base)
    assert r["ok"] is False
    assert r["rebuilt"] is False
    assert "boom" in r["msg"]                       # 失败原因不吞


# ================= 3. 重建失败 → _apply_zip 返回 ok=False 且确实回滚 =================

def test_apply_zip_rolls_back_when_runtime_rebuild_fails(monkeypatch, tmp_path):
    base = _setup_install(tmp_path)
    _patch_paths(monkeypatch, base)
    monkeypatch.setattr(U, "_ensure_runtime",
                        lambda *a, **k: {"ok": False, "rebuilt": False,
                                         "msg": "运行环境重建失败（无法创建 venv）"})

    res = U._apply_zip(_new_zip(), "1.22.1")

    assert res["ok"] is False
    assert "已回滚" in res["msg"]
    # 旧版内容完整恢复
    assert (base / "panel_migr" / "main.py").read_text(encoding="utf-8") == "OLD"
    assert '"1.21.1"' in (base / "panel_migr" / "config.py").read_text(encoding="utf-8")
    # 中间目录清理干净
    assert not list((base / "backups").glob(".stage_*"))
    assert not list((base / "backups").glob(".old_*"))


# ================= 4. _apply_zip 失败时 do_update 不调度重启 =================

def test_do_update_does_not_restart_when_apply_fails(monkeypatch, tmp_path):
    restarted: list[int] = []
    monkeypatch.setattr(U, "_release_json", lambda: ({"tag_name": "V1.22.1"}, ""))
    monkeypatch.setattr(U, "_zip_asset", lambda j: ("https://example.invalid/V1.22.1.zip", "V1.22.1.zip"))
    monkeypatch.setattr(U, "_zip_urls", lambda url: [("官方源", url)])
    monkeypatch.setattr(U, "http_bytes", lambda url, timeout=60: b"x" * 30_000)
    monkeypatch.setattr(U, "_zip_struct_ok", lambda data: True)
    monkeypatch.setattr(U, "_zip_new_version", lambda data: "1.22.1")
    monkeypatch.setattr(U, "_backup_panel_zip", lambda version="": str(tmp_path / "backups" / "panel_1.22_1.zip"))
    monkeypatch.setattr(U, "_apply_zip", lambda data, ver: {"ok": False, "msg": "更新失败已回滚：X"})
    monkeypatch.setattr(U, "_schedule_restart",
                        lambda: restarted.append(1) or "，2 秒后面板将自动重启生效")

    res = U.do_update()

    assert res["ok"] is False
    assert restarted == []                          # 关键：失败绝不调度重启


def test_do_update_restarts_and_notes_rebuild(monkeypatch, tmp_path):
    """成功路径：调度重启，且消息体现本次重建过运行环境。"""
    restarted: list[int] = []
    monkeypatch.setattr(U, "_release_json", lambda: ({"tag_name": "V1.22.1"}, ""))
    monkeypatch.setattr(U, "_zip_asset", lambda j: ("https://example.invalid/V1.22.1.zip", "V1.22.1.zip"))
    monkeypatch.setattr(U, "_zip_urls", lambda url: [("官方源", url)])
    monkeypatch.setattr(U, "http_bytes", lambda url, timeout=60: b"x" * 30_000)
    monkeypatch.setattr(U, "_zip_struct_ok", lambda data: True)
    monkeypatch.setattr(U, "_zip_new_version", lambda data: "1.22.1")
    monkeypatch.setattr(U, "_backup_panel_zip", lambda version="": str(tmp_path / "backups" / "panel_1.22_1.zip"))
    monkeypatch.setattr(U, "_apply_zip", lambda data, ver: {"ok": True, "msg": "", "rebuilt": True})
    monkeypatch.setattr(U, "_schedule_restart",
                        lambda: restarted.append(1) or "，2 秒后面板将自动重启生效")

    res = U.do_update()

    assert res["ok"] is True
    assert restarted == [1]
    assert "运行环境已重建" in res["msg"]


# ================= 5. _swap_stage 在 stash 无法放回时抛异常 =================

def test_swap_stage_raises_when_stash_cannot_return(monkeypatch, tmp_path):
    base, stage, old = tmp_path / "base", tmp_path / "stage", tmp_path / "old"
    monkeypatch.setattr(cfg, "BASE_DIR", base)

    (base / "panel_migr" / ".venv" / "bin").mkdir(parents=True)
    (base / "panel_migr" / ".venv" / "bin" / "python").write_text("#!/bin/sh\n", encoding="utf-8")
    (base / "panel_migr" / "main.py").write_text("OLD", encoding="utf-8")
    (stage / "panel_migr").mkdir(parents=True)
    (stage / "panel_migr" / "main.py").write_text("NEW", encoding="utf-8")

    real_move = shutil.move

    def _move(src, dst, *a, **k):
        if ".venv_stash_" in str(src) and str(dst).endswith(".venv"):
            raise OSError("simulated put-back failure")
        return real_move(src, dst, *a, **k)

    monkeypatch.setattr(shutil, "move", _move)

    with pytest.raises(OSError):
        U._swap_stage(stage, old)


# ================= 6. 版本（版本无关断言）=================

def test_version_shape():
    v = cfg.PANEL_VERSION
    assert isinstance(v, str) and v
    parts = v.split(".")
    assert len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit()


# ================= 7. 回滚不丢运行环境 =================

def test_restore_old_keeps_current_venv(monkeypatch, tmp_path):
    """换入成功后触发回滚：新目录里已放回的 .venv 必须活下来，否则下次重启 203/EXEC。"""
    base = tmp_path / "install"
    monkeypatch.setattr(cfg, "BASE_DIR", base)

    # 新目录（换入后）：新代码 + 已放回的 .venv
    (base / "panel_migr").mkdir(parents=True)
    (base / "panel_migr" / "main.py").write_text("NEW", encoding="utf-8")
    _mk_venv(base)

    # old：旧目录（无 .venv——换入时已被摘出并放回新目录）
    old = tmp_path / "backups" / ".old_T"
    (old / "panel_migr").mkdir(parents=True)
    (old / "panel_migr" / "main.py").write_text("OLD", encoding="utf-8")

    U._restore_old(old)

    assert (base / "panel_migr" / "main.py").read_text(encoding="utf-8") == "OLD"
    assert U._runtime_python_ok(base / "panel_migr" / ".venv") is True
