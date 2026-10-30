"""v1.24.7（方向C）回归：services/updateops.py 拆成 services/updateops/ 包后，
对外 API、模块级状态与 monkeypatch 语义必须与拆分前完全一致。

拆分动因：单文件 1,894 行 / 54 个函数，任何一处改动都要在 2000 行里找位置；
拆成 8 个按职责分层的子模块后，单文件最大 300 行。

本文件只测「拆分契约」，不重复测业务逻辑（那由 test_v12xx 各文件覆盖）：
1. 公开名字仍可从包级 import（外部模块的 import 行一个字都不用改）；
2. 关键私有名仍可从包级访问（测试大量 monkeypatch 它们）；
3. 子模块之间无循环 import、包内每个模块都能独立 import；
4. **monkeypatch 打到包级名字上仍然生效** —— 这是拆分最大的风险点；
5. 模块级可变状态（_UPDATE_LOCK / _LAST_BACKUP_ERR）是同一份对象。
"""
import ast
import importlib
import pathlib
import subprocess
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import config as cfg  # noqa: E402
from services import updateops as U  # noqa: E402

PKG_DIR = pathlib.Path(U.__file__).resolve().parent

# 拆分后仍必须从包级可达的公开名字（外部模块 import 的那些 + 对外 API）
PUBLIC_NAMES = [
    "do_update", "backup_rollback", "image_update", "image_check",
    "backups_list", "backups_prune", "backups_dir", "backup_current_index",
    "panel_update_mode", "check_update", "test_update_source",
    "http_get", "http_post", "http_bytes", "version_gt", "version_eq",
    "update_api_url", "update_raw_url", "update_raw_urls",
    "ghcr_config_created", "ghcr_latest_created", "assistant_image_candidates",
]

# 测试里被 monkeypatch / 直接访问的私有名字（grep tests/ 得到的清单）
PRIVATE_NAMES = [
    "_UPDATE_LOCK", "_LAST_BACKUP_ERR", "_PROTECT_TOP", "_PROTECT_DIRS",
    "_CODE_VITAL", "_apply_zip", "_do_update_locked",
    "_recover_interrupted", "_restore_old", "_swap_stage", "_cleanup_dirs",
    "_old_has_content", "_live_code_intact", "_code_intact", "_code_root",
    "_installed_version", "_installed_version_in", "_extract_zip",
    "_zip_struct_ok", "_zip_new_version", "_zip_asset", "_zip_urls",
    "_backup_panel_zip", "_zip_tree", "_zip_date_time", "_ensure_runtime",
    "_venv_pip_sync", "_runtime_python_ok", "_ghcr_auth", "_to_ts",
    "_self_under_unit", "_restart_and_health_check", "_schedule_restart",
    "_detach_restart_and_probe", "_write_probe_script", "_probe_python",
    "_release_json", "_asset_digest", "_sha256", "_version_gt", "_version_eq",
    "_nums", "_version_from_text", "_rollback_panel", "_entry", "_write_cache",
    "_compose", "_probe", "_zip_asset",
    # 上限常量：test_v1244_fix_update.py 直接 monkeypatch 它们
    "_MAX_TOTAL_SIZE", "_MAX_ENTRIES", "_MAX_FILE_SIZE", "_MAX_RATIO",
    "_MAX_DOWNLOAD",
    # 测试会 patch 的模块对象（U.cfg / U.time / U.httpx / U.shutil / U.run_cmd）
    "cfg", "time", "httpx", "shutil", "run_cmd",
]


# ================= 1. 包结构 =================

def test_package_is_a_package_not_a_module():
    """services/updateops 必须是包目录，旧的单文件必须已消失。"""
    assert PKG_DIR.is_dir()
    assert (PKG_DIR / "__init__.py").is_file()
    old = PKG_DIR.parent / "updateops.py"
    assert not old.exists(), "旧的 services/updateops.py 必须删除，否则 Python 会优先加载它"


def test_package_has_expected_submodules():
    """按职责拆分的子模块清单（少一个说明有人把实现塞回 __init__.py 了）。"""
    mods = {p.stem for p in PKG_DIR.glob("*.py")}
    for name in ("http", "version", "release", "zipops", "runtime", "swap",
                 "restart", "backups", "ghcr", "orchestrate", "_rt"):
        assert name in mods, f"缺少子模块 {name}.py"
    assert mods >= {"http", "version", "release", "zipops", "runtime", "swap",
                    "restart", "backups", "ghcr", "orchestrate"}


def test_single_file_shrank():
    """拆分的目的：单文件从 1,894 行降到 400 行以内。"""
    biggest = max(
        (p for p in PKG_DIR.glob("*.py")),
        key=lambda p: len(p.read_text(encoding="utf-8").splitlines()),
    )
    n = len(biggest.read_text(encoding="utf-8").splitlines())
    assert n <= 400, f"{biggest.name} 仍有 {n} 行，拆分目标未达成"


# ================= 2. 对外 API 不变 =================

@pytest.mark.parametrize("name", PUBLIC_NAMES)
def test_public_name_importable_from_package(name):
    """`from services.updateops import X` 必须继续可用。"""
    assert hasattr(U, name), f"包级缺少公开名字 {name}"
    assert name in U.__all__, f"{name} 未列入 __all__"


@pytest.mark.parametrize("name", PRIVATE_NAMES)
def test_private_name_reachable_from_package(name):
    """测试大量直接访问/ patch 这些私有名，包级必须仍然可达。"""
    assert hasattr(U, name), f"包级缺少私有名字 {name}"


def test_external_import_statements_still_work():
    """外部模块的 import 行一个字都没改，必须仍能执行。"""
    from services.updateops import backup_rollback, do_update, image_update  # noqa: F401
    from services.updateops import backups_list, panel_update_mode  # noqa: F401
    from services.updateops import check_update, image_check  # noqa: F401
    from services.updateops import http_get, http_post, version_gt  # noqa: F401

    assert callable(do_update) and callable(backup_rollback)


def test_no_duplicate_definitions_across_submodules():
    """同一个顶层名字只能在**一个**子模块里定义（否则包级 re-export 会取到随机的那个）。"""
    owner: dict[str, str] = {}
    for p in sorted(PKG_DIR.glob("*.py")):
        if p.name in ("__init__.py", "_rt.py"):
            continue
        tree = ast.parse(p.read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
                names = [node.name]
            elif isinstance(node, ast.Assign):
                names = [t.id for t in node.targets if isinstance(t, ast.Name)]
            else:
                continue
            for n in names:
                assert n not in owner, f"{n} 同时定义在 {owner[n]} 与 {p.name}"
                owner[n] = p.name


# ================= 3. 子模块独立性 =================

@pytest.mark.parametrize("mod", ["http", "version", "release", "zipops", "runtime",
                                 "swap", "restart", "backups", "ghcr", "orchestrate"])
def test_submodule_importable_standalone(mod):
    """每个子模块都能独立 import（无循环依赖、无 import 期副作用）。"""
    m = importlib.import_module(f"services.updateops.{mod}")
    assert m.__name__ == f"services.updateops.{mod}"


def test_no_circular_import_in_fresh_interpreter():
    """干净解释器里逐个单独 import 子模块 —— 循环 import 只在冷启动时暴露。"""
    for mod in ("http", "version", "release", "zipops", "runtime", "swap",
                "restart", "backups", "ghcr", "orchestrate"):
        code = (
            "import sys; sys.path.insert(0, '.')\n"
            f"import services.updateops.{mod} as m\n"
            "assert m is not None\n"
        )
        r = subprocess.run([sys.executable, "-c", code],
                           cwd=str(PKG_DIR.parent.parent), capture_output=True, text=True)
        assert r.returncode == 0, f"单独 import {mod} 失败：{r.stderr[-600:]}"


def test_submodules_do_not_import_sibling_names_directly():
    """子模块不得 `from .xxx import name` 直接绑定兄弟模块的函数 ——
    那样 monkeypatch 包级名字对内部调用无效（本次拆分最关键的约束）。
    只允许 `from . import _rt` 这种取包对象的写法。
    """
    bad: list[str] = []
    for p in sorted(PKG_DIR.glob("*.py")):
        if p.name in ("__init__.py", "_rt.py"):
            continue
        tree = ast.parse(p.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            if node.level != 1 or node.module in (None, "_rt"):
                continue
            # from .http import xxx / from . import http 都禁止
            bad.append(f"{p.name}:{node.lineno} from .{node.module} import ...")
    assert not bad, "子模块直接绑定了兄弟模块的名字（monkeypatch 会失效）：\n" + "\n".join(bad)


# ================= 4. monkeypatch 语义（拆分最大风险点）=================

def test_monkeypatch_package_level_name_affects_internal_call(monkeypatch):
    """patch `U._installed_version` 后，_apply_zip 内部必须看到新值。

    _apply_zip 是包级名字（orchestrate 定义），但它内部调用 _installed_version
    时必须走 _rt 回读包级属性，而不是绑定的原函数对象。
    """
    monkeypatch.setattr(U, "_installed_version", lambda: "0.0.0-split-test")
    assert U._installed_version() == "0.0.0-split-test"
    # 子模块经 _rt 回读，看到的必须是 patch 后的值
    from services.updateops import orchestrate, _rt

    assert _rt._installed_version() == "0.0.0-split-test"
    assert orchestrate._rt is _rt


def test_monkeypatch_run_cmd_reaches_restart_module(monkeypatch):
    """`monkeypatch.setattr(U, "run_cmd", ...)` 必须影响 restart 子模块内部调用。"""
    calls: list[str] = []

    def fake_run(cmd, timeout=120):
        calls.append(cmd)
        if "is-active" in cmd:
            return {"code": 1, "out": "inactive"}
        return {"code": 0, "out": ""}

    monkeypatch.setattr(U, "run_cmd", fake_run)
    res = U._restart_and_health_check(port=1234)

    assert calls and calls[0].startswith("systemctl is-active"), calls
    assert res["skipped"] is True  # 非 active → 提示手动重启


def test_monkeypatch_time_object_reaches_submodules(monkeypatch):
    """测试整体替换 `U.time` 为桩对象；子模块必须用同一个桩（否则 sleep 真睡）。"""
    slept: list[int] = []
    monkeypatch.setattr(U, "time", type("T", (), {
        "sleep": staticmethod(lambda s: slept.append(s)),
    })())
    monkeypatch.setattr(U, "_self_under_unit", lambda: False)
    monkeypatch.setattr(U, "run_cmd", lambda cmd, timeout=120: {"code": 0, "out": "active\n"})
    monkeypatch.setattr(U, "http_get", lambda url, timeout=5: {"code": 500, "body": ""})

    res = U._restart_and_health_check(port=4321, retries=2, interval=7)

    assert slept == [7, 7], f"子模块没有用到被替换的 time 桩：{slept}"
    assert res["ok"] is False and res["probe_ok"] is False


def test_monkeypatch_backup_dir_constant_reaches_zipops(monkeypatch, tmp_path):
    """`monkeypatch.setattr(U, "BACKUP_DIR", ...)` 必须影响 backups_dir()。"""
    target = tmp_path / "custom-backups"
    monkeypatch.setattr(U, "BACKUP_DIR", target)
    assert U.backups_dir() == target


def test_monkeypatch_cfg_object_is_shared(monkeypatch, tmp_path):
    """`monkeypatch.setattr(cfg, "BASE_DIR", ...)`（不经过 U）也必须生效。"""
    monkeypatch.setattr(cfg, "BASE_DIR", tmp_path)
    assert U.cfg is cfg
    from services.updateops import swap

    assert swap._rt.cfg is cfg


# ================= 5. 模块级状态同一份 =================

def test_update_lock_is_single_object_shared_with_orchestrate():
    """do_update 与测试用的必须是同一个 Lock 对象，否则互斥形同虚设。"""
    from services.updateops import orchestrate

    assert U._UPDATE_LOCK is orchestrate._rt._UPDATE_LOCK
    assert U._UPDATE_LOCK.acquire(blocking=False)
    try:
        r = U.do_update()
        assert r == {"ok": False, "msg": "已有更新正在进行，请稍后再试"}
    finally:
        U._UPDATE_LOCK.release()


def test_last_backup_err_is_package_level_state(monkeypatch, tmp_path):
    """`_backup_panel_zip` 通过 _rt.set 写回包级，测试读 U._LAST_BACKUP_ERR 能看到。"""
    data = tmp_path / "srv"
    data.mkdir()
    monkeypatch.setattr(cfg, "BASE_DIR", data)
    monkeypatch.setattr(U, "BACKUP_DIR", data / "backups")
    monkeypatch.setattr(U, "CODE_ROOT", tmp_path / "nonexistent-code")

    assert U._backup_panel_zip("1.24.7") == ""
    assert U._LAST_BACKUP_ERR != "", "失败原因没有写回包级 _LAST_BACKUP_ERR"


def test_code_root_unchanged_by_split():
    """CODE_ROOT 拆分前后必须同值（文件深度多了一层，最容易写错的地方）。"""
    # 拆分前：services/updateops.py 的 parent.parent == panel_migr/
    # 拆分后：services/updateops/runtime.py 的 parent.parent.parent == panel_migr/
    expected = pathlib.Path(U.__file__).resolve().parent.parent.parent
    assert expected == U.CODE_ROOT
    assert (U.CODE_ROOT / "config.py").is_file(), "CODE_ROOT 应指向 panel_migr/（含 config.py）"


# ================= 6. 静态检查 =================

def test_package_passes_ruff():
    """本包必须干净（F401 兼容性 re-export 已用 noqa 标注，SIM105 已改 suppress）。"""
    ruff = pathlib.Path("/tmp/auditvenv/bin/ruff")
    if not ruff.exists():  # pragma: no cover - 其它环境没有 ruff 时跳过
        pytest.skip("未安装 ruff")
    r = subprocess.run([str(ruff), "check", str(PKG_DIR)],
                       capture_output=True, text=True)
    assert r.returncode == 0, f"ruff 报错：\n{r.stdout[-1500:]}"
