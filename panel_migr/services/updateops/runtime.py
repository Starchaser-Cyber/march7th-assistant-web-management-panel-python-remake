"""代码根定位、版本读取、代码完整性判据、面板 .venv 运行环境兜底。"""
from __future__ import annotations

import os
import sys
from pathlib import Path



from . import _rt
import contextlib


# 代码根：panel_migr/ 的父目录。与 cfg.BASE_DIR（数据根，可被 M7A_PANEL_DIR 改成
# /srv/m7a-panel）不同 —— README 推荐的「代码/数据分离」部署下两者不是同一目录。
# v1.24.4（P1-5）：整包备份与版本复核都必须以代码根为准，否则在分离布局下
# 备份到 0 个文件、版本读成空串，更新必然失败。
# v1.24.7（方向C）：本文件从 services/updateops.py 移进 services/updateops/ 包，
# 相对深度多了一层，因此比原实现多一次 .parent —— 取值仍是 panel_migr/ 的父目录
# （拆分前后必须完全一致，测试断言 U._code_root() == U.CODE_ROOT）。
CODE_ROOT = Path(__file__).resolve().parent.parent.parent


def _code_root() -> Path:
    """代码所在目录（含 panel_migr/ 的那一级）。

    v1.24.4（P1-5）：README 推荐的部署把代码放 /opt/m7a-panel-repo、数据放
    /srv/m7a-panel（M7A_PANEL_DIR）。此前备份与版本复核都以 cfg.BASE_DIR 为根，
    在分离布局下会备份到 0 个文件并误报「请检查 backups 目录权限」。
    兼容单目录布局：BASE_DIR 下确实有 panel_migr/ 时仍以它为根（旧部署 + 测试）。
    """
    base = Path(_rt.cfg.BASE_DIR)
    try:
        if (base / "panel_migr").is_dir():
            return base
    except OSError:
        pass
    return _rt.CODE_ROOT


def _installed_version() -> str:
    """读取**代码目录**里已安装的版本号（v1.24.4 P1-5）。"""
    try:
        txt = (_rt._code_root() / "panel_migr" / "config.py").read_text("utf-8", "replace")
    except OSError:
        return ""
    return _rt._version_from_text(txt)


def _installed_version_in(root: Path) -> str:
    """读取任意代码根下的已安装版本号（v1.24.5 Q-06）。

    `_recover_interrupted()` 要用它比对「.old_* 里那份代码」和「当前 BASE_DIR 里
    的代码」，所以不能复用固定读 `_code_root()` 的 `_installed_version()`。
    读不到（文件缺失 / 内容被截断成半截）时返回 ""。
    """
    try:
        txt = (Path(root) / "panel_migr" / "config.py").read_text("utf-8", "replace")
    except OSError:
        return ""
    return _rt._version_from_text(txt)


# 「换入已完成」必须同时具备的关键文件（v1.24.5 Q-06）。
# 只看 panel_migr/main.py 不够：_swap_stage 是**逐顶层条目** move，被 SIGKILL
# 打断时完全可能停在「main.py 已换入、services/ 还是旧的」或「panel_migr 换进来了
# 但 .venv 还没放回」的位置，这种半成品一旦被当成「换入已完成」就会把唯一完整的
# .old_* 删掉 —— 旧代码永久丢失。这里挑的是任何一份能跑起来的 panel_migr 都必有的
# 最小文件集。
_CODE_VITAL = (
    "panel_migr/main.py",
    "panel_migr/config.py",
    "panel_migr/routers/__init__.py",
    "panel_migr/services/shell.py",
)


def _code_intact(root: Path, old: Path | None = None) -> tuple[bool, str]:
    """判断 `root` 下的面板代码是否「换入已完成、可以安全删掉 .old_*」。

    返回 (是否完整, 原因)。判据（全部满足才算完整）：
      1. `.old_*` 里确实存在的每个 `_CODE_VITAL` 文件，在 root 里也必须存在且非空。
         只比对 .old_* 里有的文件，是为了不把「精简安装 / 测试夹具」误判成半成品；
         .old_* 里没有的文件无从比对，不作为证据。
      2. root 的 `panel_migr/config.py` 必须能解析出合法版本号。读不到时**宁可换回
         也不要删** —— 0 字节或被截断的 config.py 正是换入中断的典型半成品。
      3. `.old_*` 里不得残留 `.venv_stash_*`：那是 `_swap_stage` 换入中途用来暂存
         运行环境的槽位，只要还在就说明换入没走完（.venv 还没放回）。
    """
    root = Path(root)
    checked = 0
    for rel in _rt._CODE_VITAL:
        if old is not None and not (Path(old) / rel).exists():
            continue                      # 旧快照里本来就没有 → 无从比对
        checked += 1
        p = root / rel
        try:
            if not p.is_file() or p.stat().st_size <= 0:
                return False, f"{rel} 缺失或为空"
        except OSError as e:
            return False, f"{rel} 无法访问：{e}"
    if checked == 0:
        # 旧快照里一个关键文件都没有（异常残留），退化为只看 main.py
        main_py = root / "panel_migr" / "main.py"
        try:
            if not main_py.is_file() or main_py.stat().st_size <= 0:
                return False, "panel_migr/main.py 缺失或为空"
        except OSError as e:
            return False, f"panel_migr/main.py 无法访问：{e}"
    if not _rt._installed_version_in(root):
        return False, "读不到已安装版本号（config.py 缺失或损坏）"
    if old is not None:
        try:
            if any(Path(old).glob(".venv_stash_*")):
                return False, ".old_* 里仍有 .venv_stash_*（换入未走完）"
        except OSError:
            pass
    return True, "关键文件齐全且版本号可读"


def _live_code_intact(roots: list[Path], old: Path) -> tuple[bool, str]:
    """线上代码（任一候选根）是否「换入已完成」（v1.24.5 Q-06）。

    多个候选根时取**最严格**的结论：只要有任何一个根上有 panel_migr/ 却判为不完整，
    就返回不完整（宁可换回，也不删唯一副本）。
    """
    judged = 0
    last_why = "未找到 panel_migr/ 目录"
    for root in roots:
        try:
            if not (Path(root) / "panel_migr").is_dir():
                continue
        except OSError:
            continue
        judged += 1
        ok, why = _rt._code_intact(root, old)
        if not ok:
            return False, f"{root}：{why}"
        last_why = why
    if judged == 0:
        return False, last_why
    return True, last_why


def _venv_pip_sync(dest: Path) -> None:
    """复用现有 .venv 时按新 requirements.txt 对齐依赖（已装齐则秒回，失败不影响更新）。"""
    pip = dest / ".venv" / "bin" / "pip"
    req = dest / "requirements.txt"
    if not (pip.is_file() and req.is_file()):
        return
    with contextlib.suppress(Exception):
        _rt.run_cmd(f'"{pip}" install -q -r "{req}"', timeout=180)


# 依赖安装回退源（默认源失败时用）
PIP_MIRROR = "https://pypi.tuna.tsinghua.edu.cn/simple"


def _runtime_python_ok(venv_dir: Path) -> bool:
    """运行环境是否可用：<venv>/bin/python 存在且有可执行权限。"""
    py = venv_dir / "bin" / "python"
    try:
        return py.is_file() and os.access(py, os.X_OK)
    except OSError:
        return False


def _ensure_runtime(runner=None, base_dir: Path | None = None) -> dict:
    """校验 / 重建面板运行环境（panel_migr/.venv）。

    v1.22.1：更新包不含 .venv，若更新开始前运行环境已缺失，换入新代码后
    systemd 会因找不到 panel_migr/.venv/bin/python 报 203/EXEC 反复重启。
    这里在换入后做兜底校验：缺失或不可执行则就地重建并装依赖。

    - 优先用当前解释器 sys.executable（已在跑，最贴近原环境）；
    - 不可用则回退 python3 -m venv；
    - pip 默认源失败回退清华源；
    - 全程不吞错误：命令返回码与输出都带进 msg。

    runner 可注入（测试用），签名 runner(cmd, timeout) -> {"code","out"}；
    默认用 services.shell.run_cmd。
    """
    run = runner or _rt.run_cmd
    base = Path(base_dir if base_dir is not None else _rt.cfg.BASE_DIR)
    venv_dir = base / "panel_migr" / ".venv"
    req = base / "panel_migr" / "requirements.txt"

    if _rt._runtime_python_ok(venv_dir):
        return {"ok": True, "rebuilt": False, "msg": "运行环境就绪"}

    # —— 需要重建 ——
    log: list[str] = []
    try:
        venv_dir.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return {"ok": False, "rebuilt": False,
                "msg": f"运行环境目录不可创建：{e}"}

    made = False
    py_exe = getattr(sys, "executable", "") or ""
    if py_exe:
        r = run(f'"{py_exe}" -m venv "{venv_dir}"', 300)
        if r.get("code") == 0 and _rt._runtime_python_ok(venv_dir):
            made = True
        else:
            log.append(f"用当前解释器创建 venv 失败：{r.get('out', '').strip()}")
    if not made:
        r = run(f'python3 -m venv "{venv_dir}"', 300)
        if r.get("code") == 0 and _rt._runtime_python_ok(venv_dir):
            made = True
        else:
            log.append(f"python3 -m venv 失败：{r.get('out', '').strip()}")

    if not made:
        return {"ok": False, "rebuilt": False,
                "msg": "运行环境重建失败（无法创建 venv）：" + "；".join(filter(None, log))}

    # 装依赖：默认源 → 清华源回退
    pip = venv_dir / "bin" / "pip"
    if req.is_file():
        r = run(f'"{pip}" install -q -r "{req}"', 600)
        if r.get("code") != 0:
            log.append(f"默认源安装失败：{r.get('out', '').strip()}")
            r2 = run(f'"{pip}" install -q -r "{req}" -i {_rt.PIP_MIRROR}', 600)
            if r2.get("code") != 0:
                log.append(f"镜像源安装失败：{r2.get('out', '').strip()}")
    else:
        log.append(f"未找到依赖清单 {req}，跳过依赖安装")

    if not _rt._runtime_python_ok(venv_dir):
        return {"ok": False, "rebuilt": True,
                "msg": "运行环境重建后校验仍失败：" + "；".join(filter(None, log))}

    if log:
        return {"ok": True, "rebuilt": True,
                "msg": "运行环境已重建（部分步骤有提示）：" + "；".join(filter(None, log))}
    return {"ok": True, "rebuilt": True, "msg": "运行环境已重建"}
