# -*- coding: utf-8 -*-
"""v1.24.4 更新链路专项：P0-2 / P0-3 / P0-6 与 P1-1..P1-8。

对应审计报告 A_update.md。本文件全部离线：
- 不真跑 systemctl（run_cmd 一律 monkeypatch）
- 不联网（httpx.get / httpx.stream 一律 monkeypatch）
- 不真建 venv（_ensure_runtime 一律 monkeypatch 或走已就绪分支）
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


def _mk_zip(files: dict, deflated: bool = False) -> bytes:
    buf = io.BytesIO()
    ct = zipfile.ZIP_DEFLATED if deflated else zipfile.ZIP_STORED
    with zipfile.ZipFile(buf, "w", ct) as z:
        for name, content in files.items():
            z.writestr(name, content)
    return buf.getvalue()


def _release_zip(ver: str = "1.24.4", extra: dict | None = None) -> bytes:
    """结构合法的发布包（zip 根直接是 panel_migr/）。"""
    files = {
        "panel_migr/config.py": _cfg_src(ver),
        "panel_migr/main.py": "NEW",
        "panel_migr/static/panel.js": "//new",
        "panel_migr/routers/__init__.py": "",
        "panel_migr/requirements.txt": "fastapi\n",
    }
    files.update(extra or {})
    return _mk_zip(files)


def _install(tmp_path: Path, ver: str = "1.23.0") -> Path:
    """造一个旧版安装目录（BASE_DIR 下确实有 panel_migr/ → _code_root() 取它）。"""
    base = tmp_path / "install"
    (base / "panel_migr").mkdir(parents=True)
    (base / "panel_migr" / "config.py").write_text(_cfg_src(ver), encoding="utf-8")
    (base / "panel_migr" / "main.py").write_text("OLD", encoding="utf-8")
    (base / "panel_migr" / "requirements.txt").write_text("fastapi\n", encoding="utf-8")
    return base


def _patch_paths(monkeypatch, base: Path) -> None:
    monkeypatch.setattr(cfg, "BASE_DIR", base)
    monkeypatch.setattr(U, "BACKUP_DIR", base / "backups")


# ================= P0-2：回滚失败不删 old/，文案如实 =================

def test_p0_2_rollback_failure_keeps_old_copy(monkeypatch, tmp_path):
    """回滚失败时必须保留 .old_*（旧代码唯一副本），且不得谎称「已回滚」。"""
    base = _install(tmp_path)
    _patch_paths(monkeypatch, base)

    # 让版本复核失败，从而进入 except 回滚分支
    monkeypatch.setattr(U, "_installed_version", lambda: "0.0.0")
    # 回滚本身失败（模拟磁盘满 / 权限错误）
    monkeypatch.setattr(U, "_restore_old", lambda old: {
        "ok": False, "restored": 0, "failed": 1,
        "errors": ["恢复 panel_migr 失败：OSError(28, 'No space left on device')"],
    })

    res = U._apply_zip(_release_zip("1.24.4"), "1.24.4")

    assert res["ok"] is False
    assert res["rolled_back"] is False
    # 旧代码保留在现场
    olds = list((base / "backups").glob(".old_*"))
    assert len(olds) == 1
    assert (olds[0] / "panel_migr" / "main.py").read_text(encoding="utf-8") == "OLD"
    assert res["old_dir"] == str(olds[0])
    # 文案如实：不再说「已回滚」，而是明确告知需人工处理
    assert "已回滚" not in res["msg"]
    assert "自动回滚未完成" in res["msg"]
    assert "旧代码已保留在" in res["msg"]
    assert "请人工恢复" in res["msg"]


def test_p0_2_rollback_success_still_cleans(monkeypatch, tmp_path):
    """回滚成功时照旧清理 stage/old（不能因为修 P0-2 就永远不清理）。"""
    base = _install(tmp_path)
    _patch_paths(monkeypatch, base)
    monkeypatch.setattr(U, "_installed_version", lambda: "0.0.0")

    res = U._apply_zip(_release_zip("1.24.4"), "1.24.4")

    assert res["ok"] is False
    assert res["rolled_back"] is True
    assert "已回滚" in res["msg"]
    assert not list((base / "backups").glob(".old_*"))
    assert not list((base / "backups").glob(".stage_*"))


def test_p0_2_restore_old_reports_failures(monkeypatch, tmp_path):
    """_restore_old 内部 OSError 必须计入返回值，而不是 `except OSError: pass`。"""
    base = tmp_path / "base"
    base.mkdir()
    old = tmp_path / "old"
    (old / "panel_migr").mkdir(parents=True)
    (old / "panel_migr" / "main.py").write_text("OLD", encoding="utf-8")
    monkeypatch.setattr(cfg, "BASE_DIR", base)

    real_move = U.shutil.move

    def _boom(src, dst, *a, **k):
        if "panel_migr" in str(src):
            raise OSError(13, "Permission denied")
        return real_move(src, dst, *a, **k)

    monkeypatch.setattr(U.shutil, "move", _boom)
    res = U._restore_old(old)

    assert res["ok"] is False
    assert res["failed"] >= 1
    assert any("恢复 panel_migr 失败" in e for e in res["errors"])


def test_p0_2_restore_old_ok_dict(monkeypatch, tmp_path):
    """正常回滚返回 ok=True 且 restored>0（既有调用方只当语句用，兼容）。"""
    base = tmp_path / "base"
    (base / "panel_migr").mkdir(parents=True)
    (base / "panel_migr" / "main.py").write_text("NEW", encoding="utf-8")
    old = tmp_path / "old"
    (old / "panel_migr").mkdir(parents=True)
    (old / "panel_migr" / "main.py").write_text("OLD", encoding="utf-8")
    monkeypatch.setattr(cfg, "BASE_DIR", base)

    res = U._restore_old(old)

    assert res["ok"] is True and res["restored"] == 1
    assert (base / "panel_migr" / "main.py").read_text(encoding="utf-8") == "OLD"


# ================= P0-3：detach 重启 =================

def _fake_detach_env(monkeypatch, *, has_systemd_run: bool):
    """构造 detach 测试环境：记录 run_cmd 调用，不执行任何真实命令。"""
    calls: list[str] = []

    def fake_run(cmd, timeout=120):
        calls.append(cmd)
        if "is-active" in cmd:
            return {"code": 0, "out": "active\n"}
        return {"code": 0, "out": ""}

    monkeypatch.setattr(U, "run_cmd", fake_run)
    monkeypatch.setattr(U, "_write_probe_script", lambda: Path("/tmp/fake_probe.py"))
    monkeypatch.setattr(U, "_probe_python", lambda: "/usr/bin/python3")
    real_which = U.shutil.which

    def fake_which(n):
        if n == "systemd-run":
            return "/usr/bin/systemd-run" if has_systemd_run else None
        return real_which(n)

    monkeypatch.setattr(U.shutil, "which", fake_which)
    return calls


def test_p0_3_self_under_unit_uses_detach_not_inline_restart(monkeypatch, tmp_path):
    """本进程就在被重启的 unit 内 → 绝不原地 systemctl restart，而是 detach。"""
    calls = _fake_detach_env(monkeypatch, has_systemd_run=True)
    monkeypatch.setattr(U, "_self_under_unit", lambda: True)
    monkeypatch.setattr(U, "http_get", lambda *a, **k: pytest.fail("不应同步探测"))

    res = U._restart_and_health_check(old=tmp_path / "old")

    assert res["ok"] is True and res["detach"] is True
    assert res["detach_mode"] == "systemd-run"
    # 唯一的重启动作在 systemd-run 里，由独立 transient unit 持有
    assert any(c.startswith("systemd-run --no-block") for c in calls)
    assert not any(c.startswith("systemctl restart") for c in calls)
    assert str(cfg.LISTEN_PORT) in " ".join(calls)


def test_p0_3_detach_falls_back_to_setsid(monkeypatch, tmp_path):
    """systemd-run 不可用（非 root / 无 polkit）→ 退化 setsid，并如实告知降级。"""
    calls = _fake_detach_env(monkeypatch, has_systemd_run=False)
    monkeypatch.setattr(U, "_self_under_unit", lambda: True)
    monkeypatch.setattr(U, "http_get", lambda *a, **k: pytest.fail("不应同步探测"))

    res = U._restart_and_health_check(old=tmp_path / "old")

    assert res["ok"] is True and res["detach"] is True
    assert res["detach_mode"] == "setsid"
    assert any("setsid" in c for c in calls)
    assert "不保证" in res["msg"]          # 降级要如实说清


def test_p0_3_not_under_unit_stays_synchronous(monkeypatch, tmp_path):
    """pytest / 前台手工运行（不在 unit 内）→ 保持同步重启 + 探测，便于测试。"""
    calls: list[str] = []
    monkeypatch.setattr(U, "_self_under_unit", lambda: False)
    monkeypatch.setattr(U, "time", type("T", (), {"sleep": staticmethod(lambda s: None)})())

    def fake_run(cmd, timeout=120):
        calls.append(cmd)
        if "is-active" in cmd:
            return {"code": 0, "out": "active\n"}
        return {"code": 0, "out": ""}

    monkeypatch.setattr(U, "run_cmd", fake_run)
    monkeypatch.setattr(U, "http_get", lambda url, timeout=5: {"code": 200, "body": ""})

    res = U._restart_and_health_check(port=1234)

    assert res["ok"] is True and res["probe_ok"] is True
    assert res.get("detach") is not True
    assert any(c == "systemctl restart m7a-panel" for c in calls)


def test_p0_3_apply_zip_detach_keeps_old_dir(monkeypatch, tmp_path):
    """detach 形态下 old/ 的清理权归探测进程，_apply_zip 只清 stage/。"""
    base = _install(tmp_path)
    _patch_paths(monkeypatch, base)
    monkeypatch.setattr(U, "_ensure_runtime", lambda *a, **k: {"ok": True, "rebuilt": False})
    monkeypatch.setattr(U, "_restart_and_health_check", lambda **k: {
        "ok": True, "skipped": False, "probe_ok": False, "detach": True,
        "detach_mode": "systemd-run", "msg": "，面板正在重启",
    })

    res = U._apply_zip(_release_zip("1.24.4"), "1.24.4")

    assert res["ok"] is True and res["detach"] is True
    assert not list((base / "backups").glob(".stage_*"))
    assert len(list((base / "backups").glob(".old_*"))) == 1


# ================= P0-6：健康探测端口取 cfg.LISTEN_PORT =================

def test_p0_6_health_check_uses_listen_port(monkeypatch):
    """默认端口必须来自 cfg.LISTEN_PORT，不再硬编码 9999。"""
    seen: list[str] = []
    monkeypatch.setattr(U, "_self_under_unit", lambda: False)
    monkeypatch.setattr(U, "time", type("T", (), {"sleep": staticmethod(lambda s: None)})())

    def fake_run(cmd, timeout=120):
        if "is-active" in cmd:
            return {"code": 0, "out": "active\n"}
        return {"code": 0, "out": ""}

    def fake_http(url, timeout=5):
        seen.append(url)
        return {"code": 200, "body": ""}

    monkeypatch.setattr(U, "run_cmd", fake_run)
    monkeypatch.setattr(U, "http_get", fake_http)

    res = U._restart_and_health_check()

    assert res["ok"] is True
    assert seen == [f"http://127.0.0.1:{cfg.LISTEN_PORT}/"]
    assert "9999" not in " ".join(seen)


def test_p0_6_explicit_port_still_honored(monkeypatch):
    """显式传端口时仍按参数探测（向后兼容）。"""
    seen: list[str] = []
    monkeypatch.setattr(U, "_self_under_unit", lambda: False)
    monkeypatch.setattr(U, "time", type("T", (), {"sleep": staticmethod(lambda s: None)})())
    monkeypatch.setattr(U, "run_cmd",
                        lambda cmd, timeout=120: {"code": 0, "out": "active\n"})
    monkeypatch.setattr(U, "http_get",
                        lambda url, timeout=5: (seen.append(url), {"code": 200, "body": ""})[1])

    U._restart_and_health_check(port=4321)

    assert seen == ["http://127.0.0.1:4321/"]


# ================= P1-1：sessions 永不进包/换入 =================

def test_p1_1_protect_top_contains_sessions():
    assert "sessions" in U._PROTECT_TOP


def test_p1_1_backup_excludes_sessions(monkeypatch, tmp_path):
    """备份 zip 不得含 sessions/（否则回滚会复活已注销会话）。"""
    base = _install(tmp_path)
    (base / "sessions").mkdir()
    (base / "sessions" / "sess_abc").write_text("PHPSESSID|1", encoding="utf-8")
    (base / "data").mkdir()
    (base / "data" / "x.txt").write_text("keep", encoding="utf-8")
    _patch_paths(monkeypatch, base)

    zpath = U._backup_panel_zip("1.24.3")
    assert zpath != ""
    with zipfile.ZipFile(zpath) as z:
        names = z.namelist()
    assert any(n.startswith("panel_migr/") for n in names)
    assert not any(n.startswith("sessions/") for n in names)
    assert not any(n.startswith("data/") for n in names)


def test_p1_1_swap_stage_skips_sessions(monkeypatch, tmp_path):
    """换入侧同样跳过：包里的 sessions/ 不能被换进面板根。"""
    base = _install(tmp_path)
    (base / "sessions").mkdir()
    (base / "sessions" / "sess_live").write_text("LIVE", encoding="utf-8")
    monkeypatch.setattr(cfg, "BASE_DIR", base)

    stage = tmp_path / "stage"
    (stage / "panel_migr").mkdir(parents=True)
    (stage / "panel_migr" / "main.py").write_text("NEW", encoding="utf-8")
    (stage / "sessions").mkdir()
    (stage / "sessions" / "sess_evil").write_text("EVIL", encoding="utf-8")

    U._swap_stage(stage, tmp_path / "old")

    assert (base / "sessions" / "sess_live").read_text(encoding="utf-8") == "LIVE"
    assert not (base / "sessions" / "sess_evil").exists()


# ================= P1-2：mtime < 1980 不再炸 =================

def test_p1_2_zip_date_time_clamps_pre_1980(tmp_path):
    p = tmp_path / "old.txt"
    p.write_text("x", encoding="utf-8")
    import os as _os
    _os.utime(p, (0, 0))                       # 1970-01-01
    dt = U._zip_date_time(p)
    assert dt[0] >= 1980
    assert len(dt) == 6


def test_p1_2_zip_tree_handles_pre_1980_mtime(monkeypatch, tmp_path):
    """回归：旧实现 zf.write() 对 <1980 的 mtime 抛 ValueError，只捕 OSError 就穿出。"""
    root = tmp_path / "src"
    root.mkdir()
    f = root / "a.txt"
    f.write_text("hello", encoding="utf-8")
    import os as _os
    _os.utime(f, (0, 0))

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        n = U._zip_tree(zf, root, "src")
    assert n == 1
    with zipfile.ZipFile(io.BytesIO(buf.getvalue())) as z:
        assert z.namelist() == ["src/a.txt"]


def test_p1_2_backup_failure_removes_partial_zip(monkeypatch, tmp_path):
    """打包中途失败：删掉半截 zip 并返回明确错误（不能当合法备份留下）。"""
    base = _install(tmp_path)
    _patch_paths(monkeypatch, base)

    def _boom(self, zi, data):
        raise ValueError("ZIP does not support timestamps before 1980")

    monkeypatch.setattr(zipfile.ZipFile, "writestr", _boom)

    assert U._backup_panel_zip("1.24.3") == ""
    assert U._LAST_BACKUP_ERR != ""
    assert not list((base / "backups").glob("panel_*.zip"))


# ================= P1-3：更新互斥锁 + 唯一目录名 =================

def test_p1_3_update_lock_rejects_concurrent_update():
    assert U._UPDATE_LOCK.acquire(blocking=False) is True
    try:
        res = U.do_update()
        assert res["ok"] is False
        assert "已有更新正在进行" in res["msg"]
    finally:
        U._UPDATE_LOCK.release()


def test_p1_3_stage_dir_name_unique_in_same_second(monkeypatch, tmp_path):
    """同秒两次更新必须用不同的 .stage_*/.old_*（pid + 随机后缀）。"""
    base = _install(tmp_path)
    _patch_paths(monkeypatch, base)
    monkeypatch.setattr(U.time, "strftime", lambda fmt, *a: "20261029213000")

    names: list[tuple[str, str]] = []

    def fake_extract(data, stage):
        names.append((stage.name, (stage.parent / stage.name.replace(".stage_", ".old_")).name))
        raise RuntimeError("stop here")

    monkeypatch.setattr(U, "_extract_zip", fake_extract)

    U._apply_zip(_release_zip(), "1.24.4")
    U._apply_zip(_release_zip(), "1.24.4")

    assert len(names) == 2
    assert names[0][0] != names[1][0]
    for stage_name, _ in names:
        assert stage_name.startswith(".stage_20261029213000_")
        assert len(stage_name.split("_")) >= 4


# ================= P1-4：崩溃残留恢复 =================

def test_p1_4_recover_removes_stage_and_restores_old(monkeypatch, tmp_path):
    base = tmp_path / "install"
    (base / "panel_migr").mkdir(parents=True)      # 关键文件 main.py 故意缺失
    (base / "backups").mkdir()
    monkeypatch.setattr(cfg, "BASE_DIR", base)
    monkeypatch.setattr(U, "BACKUP_DIR", base / "backups")

    stage = base / "backups" / ".stage_20261029213000_1_ab"
    stage.mkdir()
    (stage / "panel_migr").mkdir()
    (stage / "panel_migr" / "main.py").write_text("HALF", encoding="utf-8")

    old = base / "backups" / ".old_20261029213000_1_ab"
    (old / "panel_migr").mkdir(parents=True)
    (old / "panel_migr" / "main.py").write_text("OLD-GOOD", encoding="utf-8")

    out = U._recover_interrupted()

    assert not stage.exists()                                  # 纯 stage 直接删
    assert not old.exists()                                    # 换回成功后删残留
    assert out["recovered"] and out["removed"]
    assert (base / "panel_migr" / "main.py").read_text(encoding="utf-8") == "OLD-GOOD"


def test_p1_4_recover_keeps_old_when_code_intact(monkeypatch, tmp_path):
    """关键文件完好 → 说明换入已完成，old/ 只是没来得及清理，可以删。"""
    base = _install(tmp_path)
    _patch_paths(monkeypatch, base)
    old = base / "backups" / ".old_20261029213000_2_cd"
    (old / "panel_migr").mkdir(parents=True)
    (old / "panel_migr" / "main.py").write_text("STALE", encoding="utf-8")

    out = U._recover_interrupted()

    assert not old.exists()
    assert out["removed"]


# ================= P1-5：代码/数据分离布局 =================

def test_p1_5_code_root_prefers_base_when_has_panel_migr(monkeypatch, tmp_path):
    base = _install(tmp_path)
    monkeypatch.setattr(cfg, "BASE_DIR", base)
    assert U._code_root() == base


def test_p1_5_code_root_falls_back_to_code_dir(monkeypatch, tmp_path):
    """M7A_PANEL_DIR 指向纯数据目录（无 panel_migr/）→ 用代码目录。"""
    data = tmp_path / "srv" / "m7a-panel"
    data.mkdir(parents=True)
    monkeypatch.setattr(cfg, "BASE_DIR", data)
    assert U._code_root() == U.CODE_ROOT


def test_p1_5_backup_reports_no_files_not_permissions(monkeypatch, tmp_path):
    """n==0 时错误信息必须是「没有可备份的面板文件」，不能误导成权限问题。"""
    data = tmp_path / "srv"
    data.mkdir()
    empty_code = tmp_path / "code"
    (empty_code / "panel_migr").mkdir(parents=True)   # 目录在、但没有可备份文件
    monkeypatch.setattr(cfg, "BASE_DIR", data)
    monkeypatch.setattr(U, "BACKUP_DIR", data / "backups")
    monkeypatch.setattr(U, "CODE_ROOT", empty_code)

    assert U._backup_panel_zip("1.24.3") == ""
    err = U._LAST_BACKUP_ERR
    assert "没有可备份的面板文件" in err
    assert "权限" not in err


def test_p1_5_installed_version_reads_code_dir(monkeypatch, tmp_path):
    """版本复核也基于代码目录：数据目录里的 config.py 不参与。"""
    data = tmp_path / "srv"
    data.mkdir()
    code = tmp_path / "repo"
    (code / "panel_migr").mkdir(parents=True)
    (code / "panel_migr" / "config.py").write_text(_cfg_src("9.9.9"), encoding="utf-8")
    monkeypatch.setattr(cfg, "BASE_DIR", data)
    monkeypatch.setattr(U, "CODE_ROOT", code)

    assert U._installed_version() == "9.9.9"


def test_p1_5_backup_packs_from_code_dir(monkeypatch, tmp_path):
    """分离布局下备份应打到代码目录里的文件（此前 n==0 恒失败）。"""
    data = tmp_path / "srv"
    data.mkdir()
    code = tmp_path / "repo"
    (code / "panel_migr").mkdir(parents=True)
    # 内容要够大：_backup_panel_zip 把 <200B 的包体也视为「没打包到东西」
    (code / "panel_migr" / "main.py").write_text(
        "".join(f"line{i} = {i!r}\n" for i in range(300)), encoding="utf-8")
    monkeypatch.setattr(cfg, "BASE_DIR", data)
    monkeypatch.setattr(U, "BACKUP_DIR", data / "backups")
    monkeypatch.setattr(U, "CODE_ROOT", code)

    zpath = U._backup_panel_zip("1.24.3")

    assert zpath != ""
    with zipfile.ZipFile(zpath) as z:
        assert "panel_migr/main.py" in z.namelist()


# ================= P1-6：GHCR 鉴权头 + 时间解析 =================

def test_p1_6_ghcr_auth_single_prefix():
    assert U._ghcr_auth("abc") == "Bearer abc"
    # 已带前缀 / 已带 Authorization: 的输入不重复叠加
    assert U._ghcr_auth("Bearer abc") == "Bearer abc"
    assert U._ghcr_auth("Authorization: Bearer abc") == "Bearer abc"
    assert U._ghcr_auth("") == "Bearer "


def test_p1_6_config_blob_header_not_doubled(monkeypatch):
    seen: list[dict] = []

    class _R:
        status_code = 200

        def json(self):
            return {"created": "2026-10-21T22:37:22.870489069Z"}

    def fake_get(url, headers=None, timeout=None, follow_redirects=True):
        seen.append(headers or {})
        return _R()

    monkeypatch.setattr(U.httpx, "get", fake_get)

    out = U.ghcr_config_created("Bearer ey-token", "https://ghcr.io/v2/x/y", "sha256:abc")

    assert out.startswith("2026-10-21")
    assert seen[0]["Authorization"] == "Bearer ey-token"
    assert "Authorization: Bearer" not in seen[0]["Authorization"]


def test_p1_6_to_ts_nanoseconds():
    """回归：s[:25] 会把 Z 切掉 → 恒 False。纳秒时间必须能解析。"""
    ts = U._to_ts("2026-10-21T22:37:22.870489069Z")
    assert ts is not False
    assert isinstance(ts, int) and ts > 0


def test_p1_6_to_ts_common_formats():
    assert U._to_ts("2026-10-21T22:37:22Z") is not False
    assert U._to_ts("2026-10-21T22:37:22.870Z") is not False
    assert U._to_ts("2026-10-21 22:37:22") is not False
    assert U._to_ts("2026-10-21") is not False
    assert U._to_ts("") is False
    assert U._to_ts("not-a-time") is False


# ================= P1-7：包结构必须根直接是 panel_migr/ =================

def test_p1_7_rejects_package_with_outer_dir():
    wrapped = _mk_zip({
        "m7a_panel_V1.24.3/panel_migr/config.py": 'PANEL_VERSION = "1.24.4"',
        "m7a_panel_V1.24.3/panel_migr/main.py": "NEW",
        "m7a_panel_V1.24.3/panel_migr/static/panel.js": "//new",
        "m7a_panel_V1.24.3/panel_migr/routers/__init__.py": "",
    })
    assert U._zip_struct_ok(wrapped) is False
    assert U._zip_struct_ok(_release_zip()) is True


def test_p1_7_swap_stage_rejects_bad_layout(tmp_path):
    stage = tmp_path / "stage"
    (stage / "m7a_panel_V1.24.3" / "panel_migr").mkdir(parents=True)
    (stage / "m7a_panel_V1.24.3" / "panel_migr" / "main.py").write_text(
        "NEW", encoding="utf-8")

    with pytest.raises(RuntimeError) as ei:
        U._swap_stage(stage, tmp_path / "old")
    assert "发布包结构错误" in str(ei.value)
    assert "panel_migr/" in str(ei.value)


# ================= P1-8：zip bomb 防护 + 流式下载上限 =================

def test_p1_8_extract_rejects_too_many_entries(monkeypatch, tmp_path):
    monkeypatch.setattr(U, "_MAX_ENTRIES", 3)
    data = _mk_zip({f"panel_migr/f{i}.py": "x" for i in range(5)})
    with pytest.raises(RuntimeError) as ei:
        U._extract_zip(data, tmp_path / "stage")
    assert "条目数异常" in str(ei.value)


def test_p1_8_extract_rejects_huge_file(monkeypatch, tmp_path):
    monkeypatch.setattr(U, "_MAX_FILE_SIZE", 10)
    data = _mk_zip({"panel_migr/big.py": "x" * 100})
    with pytest.raises(RuntimeError) as ei:
        U._extract_zip(data, tmp_path / "stage")
    assert "单文件过大" in str(ei.value)


def test_p1_8_extract_rejects_total_size(monkeypatch, tmp_path):
    monkeypatch.setattr(U, "_MAX_TOTAL_SIZE", 10)
    data = _mk_zip({"panel_migr/a.py": "x" * 20})
    with pytest.raises(RuntimeError) as ei:
        U._extract_zip(data, tmp_path / "stage")
    assert "总大小超限" in str(ei.value)


def test_p1_8_extract_rejects_bomb_ratio(monkeypatch, tmp_path):
    monkeypatch.setattr(U, "_MAX_RATIO", 2)
    data = _mk_zip({"panel_migr/a.py": "0" * 100000}, deflated=True)  # 高压缩比
    with pytest.raises(RuntimeError) as ei:
        U._extract_zip(data, tmp_path / "stage")
    assert "压缩比异常" in str(ei.value)


def test_p1_8_extract_rejects_traversal(tmp_path):
    data = _mk_zip({"../evil.py": "x"})
    with pytest.raises(RuntimeError) as ei:
        U._extract_zip(data, tmp_path / "stage")
    assert "路径非法" in str(ei.value)


def test_p1_8_extract_ok_for_normal_package(tmp_path):
    stage = tmp_path / "stage"
    U._extract_zip(_release_zip(), stage)
    assert (stage / "panel_migr" / "main.py").read_text(encoding="utf-8") == "NEW"


class _FakeStream:
    """httpx.stream(...) 的上下文管理器替身。"""

    def __init__(self, code=200, headers=None, chunks=()):
        self.status_code = code
        self.headers = headers or {}
        self._chunks = chunks

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def iter_bytes(self):
        yield from self._chunks


def test_p1_8_http_bytes_streams_with_cap(monkeypatch):
    """下载改为流式 + 边读边累计上限（不再整包进内存）。"""
    monkeypatch.setattr(U, "_MAX_DOWNLOAD", 10)
    monkeypatch.setattr(U.httpx, "stream",
                        lambda *a, **k: _FakeStream(chunks=[b"x" * 20]))
    assert U.http_bytes("https://example.com/a.zip") is None


def test_p1_8_http_bytes_rejects_big_content_length(monkeypatch):
    monkeypatch.setattr(U, "_MAX_DOWNLOAD", 10)
    monkeypatch.setattr(U.httpx, "stream",
                        lambda *a, **k: _FakeStream(headers={"content-length": "999"},
                                                    chunks=[b"ok"]))
    assert U.http_bytes("https://example.com/a.zip") is None


def test_p1_8_http_bytes_ok(monkeypatch):
    monkeypatch.setattr(U.httpx, "stream",
                        lambda *a, **k: _FakeStream(chunks=[b"ab", b"cd"]))
    assert U.http_bytes("https://example.com/a.zip") == b"abcd"
