"""换入 / 回滚：逐条目 move、.venv 救出、崩溃残留恢复。"""
from __future__ import annotations

import os
from pathlib import Path



from . import _rt
import contextlib


# 备份/换入时永远不动的运行数据与内部目录
# v1.24.4（P1-1）：sessions/ 必须一起保护 —— 此前它随整包备份进 backups/*.zip
# （0644，绕过 v1.24.3 的会话文件权限加固），一键回滚又把旧快照整体换回，
# 等于复活已注销的会话（认证绕过）。备份与换入两侧都跳过它。
_PROTECT_TOP = ("data", "backups", "sessions")


_PROTECT_DIRS = {"__pycache__", ".venv", "venv", "node_modules", "_tmp", ".git"}


def _swap_stage(stage: Path, old: Path) -> None:
    """stage 内一级条目换入面板目录；被替换的条目移入 old 供失败回滚。
    data/ backups/ sessions/ 等运行数据永不触碰。
    v1.21.1：现有 .venv 运行环境先摘出暂存、换入后放回——此前它随旧目录进入
    old 会被回滚清理一并删掉，导致面板进程起不来（203/EXEC 循环）。
    v1.22.1：暂存/放回改用 lexists 判断，避免 .venv 为符号链接时被静默丢弃；
    stash 一旦建立就必须放回，放回失败直接抛异常（交由上层回滚，不再静默跳过）。
    v1.24.4（P1-7）：换入前校验 stage/panel_migr 必须是目录。带外层目录的包
    （m7a_panel_V1.24.3/panel_migr/...）在这里明确失败，而不是「假成功」或
    在面板根目录留下一个垃圾目录。"""
    if not (stage / "panel_migr").is_dir():
        raise RuntimeError("发布包结构错误：zip 根必须直接是 panel_migr/（不得有外层目录）")
    old.mkdir(parents=True, exist_ok=True)
    venv_kept: list[Path] = []
    for item in sorted(stage.iterdir()):
        if item.name in _rt._PROTECT_TOP or item.name.startswith("."):
            continue
        dest = _rt.cfg.BASE_DIR / item.name
        stash = None
        src_venv = dest / ".venv"
        if dest.is_dir() and os.path.lexists(src_venv):
            stash = old / f".venv_stash_{item.name}"
            if os.path.lexists(stash):
                if stash.is_dir() and not stash.is_symlink():
                    _rt.shutil.rmtree(stash, ignore_errors=True)
                else:
                    stash.unlink()
            _rt.shutil.move(str(src_venv), str(stash))
        # lexists：dest 是指向别处的符号链接时也要先移走，不能静默跳过
        if os.path.lexists(dest):
            _rt.shutil.move(str(dest), str(old / item.name))
        _rt.shutil.move(str(item), str(dest))
        if stash is not None:
            # stash 已建立：必须放回，失败即抛异常触发回滚（绝不静默丢弃运行环境）
            if not os.path.lexists(stash):
                raise RuntimeError(f"暂存的 .venv 丢失，无法放回：{item.name}")
            _rt.shutil.move(str(stash), str(dest / ".venv"))
            venv_kept.append(dest)
    for d in venv_kept:
        _rt._venv_pip_sync(d)


def _restore_old(old: Path) -> dict:
    """回滚：把 old 里的原条目移回面板目录（覆盖换入失败的新条目）。

    v1.22.1：换入成功时 .venv 已放回新目录，回滚若直接 rmtree 新目录会连带删掉运行
    环境（面板下次重启即 203/EXEC）。因此先把当前 .venv 救出暂存、普通条目恢复完再
    放回；救出槽位放在 old 同级，即使放回失败也不会随 old 被清理掉。

    v1.24.4（P0-2）：返回结果字典而不是静默吞掉失败。调用方（_apply_zip）据此决定
    是否允许清理 old/ —— 回滚失败时 old/ 是旧代码的**唯一副本**，删掉就永久损坏。
    返回 {"ok": bool, "restored": int, "failed": int, "errors": [str, ...]}。

    v1.24.7（方向C）：本函数原先 82 行 / 圈复杂度 27，拆成四个单一职责的小函数
    （救出 .venv → 搬回条目 → 放回救出的 .venv → 放回 .venv_stash_* 残留），
    顺序与失败计数语义完全不变。
    """
    res: dict = {"ok": True, "restored": 0, "failed": 0, "errors": []}
    if not old.is_dir():
        return res
    items = sorted(old.iterdir())
    rescue = old.parent / (old.name + ".venvrescue")
    rescued: list[tuple[str, Path]] = []
    for item in items:
        if item.name.startswith(".venv_stash_"):
            continue
        _rescue_current_venv(item, rescue, rescued, res)
        _move_entry_back(item, res)
    _put_back_rescued_venvs(rescued, res)
    _put_back_stashed_venvs(items, res)
    # 空壳 rescue 目录清理（仍有残留槽位则保留，便于人工恢复）
    with contextlib.suppress(OSError):
        if rescue.is_dir() and not any(rescue.iterdir()):
            rescue.rmdir()
    res["ok"] = res["failed"] == 0
    return res


def _rescue_current_venv(item: Path, rescue: Path, rescued: list, res: dict) -> None:
    """回滚前先把「当前目录里的 .venv」挪到 rescue 暂存槽，避免被 rmtree 带走。"""
    cur_venv = _rt.cfg.BASE_DIR / item.name / ".venv"
    if not os.path.lexists(cur_venv):
        return
    try:
        rescue.mkdir(parents=True, exist_ok=True)
        slot = rescue / item.name
        if os.path.lexists(slot):
            if slot.is_dir() and not slot.is_symlink():
                _rt.shutil.rmtree(slot, ignore_errors=True)
            else:
                slot.unlink()
        _rt.shutil.move(str(cur_venv), str(slot))
        rescued.append((item.name, slot))
    except OSError as e:
        res["failed"] += 1
        res["errors"].append(f"暂存 {item.name}/.venv 失败：{e}")


def _move_entry_back(item: Path, res: dict) -> None:
    """把 old/ 里的一个条目搬回面板目录（先清掉换入失败的新条目）。"""
    dest = _rt.cfg.BASE_DIR / item.name
    try:
        if os.path.lexists(dest):
            if dest.is_dir() and not dest.is_symlink():
                _rt.shutil.rmtree(dest, ignore_errors=True)
            else:
                dest.unlink()
        _rt.shutil.move(str(item), str(dest))
        res["restored"] += 1
    except OSError as e:
        res["failed"] += 1
        res["errors"].append(f"恢复 {item.name} 失败：{e}")


def _put_back_rescued_venvs(rescued: list, res: dict) -> None:
    """放回救出的运行环境（恢复后的目录里没有 .venv 时才补）。"""
    for name, slot in rescued:
        _place_venv(_rt.cfg.BASE_DIR / name, slot, f"放回 {name}/.venv 失败", res)


def _put_back_stashed_venvs(items: list, res: dict) -> None:
    """v1.21.1：普通条目恢复完后，把 .venv_stash_* 残留放回对应目录。"""
    for item in items:
        if not item.name.startswith(".venv_stash_"):
            continue
        base_name = item.name[len(".venv_stash_"):]
        _place_venv(_rt.cfg.BASE_DIR / base_name, item,
                    f"放回暂存 {base_name}/.venv 失败", res)


def _place_venv(target: Path, slot: Path, err_prefix: str, res: dict) -> None:
    """把 slot 处的 .venv 放到 target/.venv（目标已有 .venv 或 slot 不存在时跳过）。"""
    try:
        if not target.exists():
            target.mkdir(parents=True, exist_ok=True)
        venv_dst = target / ".venv"
        if not os.path.lexists(venv_dst) and os.path.lexists(slot):
            _rt.shutil.move(str(slot), str(venv_dst))
    except OSError as e:
        res["failed"] += 1
        res["errors"].append(f"{err_prefix}：{e}")


def _cleanup_dirs(*dirs: Path) -> None:
    for p in dirs:
        if p.is_dir():
            _rt.shutil.rmtree(p, ignore_errors=True)


def _old_has_content(old: Path) -> bool:
    """old/ 里是否已经搬进了东西（v1.24.5 Q-09）。

    `_swap_stage` 是逐条目 move 的，中途抛异常时可能已经搬走了一部分 —— 只要
    old/ 非空，线上代码就**可能**已被改动，必须走回滚；old/ 为空（或不存在）
    说明线上代码一个字节都没动过，此时不该声称「已回滚」。
    """
    try:
        if not old.is_dir():
            return False
        return any(old.iterdir())
    except OSError:
        # 读不准就按「动过」处理：宁可多回滚一次，也不能漏掉唯一副本。
        return True


def _recover_interrupted() -> dict:
    """崩溃/断电后恢复：扫描 backups/ 下的 .old_* 与 .stage_* 残留（v1.24.4 P1-4）。

    此前更新中途被 SIGKILL（正是 P0-3 的死锁结局）会永久留下 `.old_*`/`.stage_*`：
    新代码可能只换入了一半，而唯一完整的旧代码躺在 .old_* 里没人管；下次更新又
    按时间戳生成新目录，旧残留永远堆积。这里在**持锁后**调用，规则：
      - `.stage_*`：换入前的临时解压目录，绝无保留价值 → 直接删；
      - `.old_*`：若线上代码**不完整**（见 `_code_intact`）→ 判定换入做到一半，
        把 .old_* 内容换回；换回成功才删残留，失败则原样保留；
      - `.old_*` 且线上代码完整 → 说明换入已完成、只是没来得及清理 → 删。

    v1.24.5（Q-06）：完整性判据从「`panel_migr/main.py` 存在且非空」升级为
    `_code_intact()`（关键文件集 + 版本号可读 + 无残留 .venv_stash）。旧判据下
    「main.py 已换入、其余还是半截」会被误判成换入完成，于是把唯一完整的
    .old_* 删掉 —— 旧代码永久丢失。任何一项读不准时一律按「不完整」处理，
    宁可多换回一次，也不删掉唯一副本。

    返回 {"recovered": [...], "removed": [...], "errors": [...], "notes": [...]}。
    """
    out: dict = {"recovered": [], "removed": [], "errors": [], "notes": []}
    try:
        d = _rt.backups_dir()
        entries = sorted(d.glob(".stage_*")) + sorted(d.glob(".old_*"))
    except OSError as e:
        out["errors"].append(str(e))
        return out
    # 换回目标与 _restore_old 一致（cfg.BASE_DIR）；分离布局下代码目录也要看。
    # 判据是「所有存在 panel_migr/ 的候选根都必须完整」：分离布局下换入写的是
    # BASE_DIR，若它被换了一半而 CODE_ROOT 是完好的，仍然要换回。
    roots = [Path(_rt.cfg.BASE_DIR)]
    try:
        cr = _rt._code_root()
        if cr not in roots:
            roots.append(cr)
    except Exception:
        pass
    for p in entries:
        if not p.is_dir():
            continue
        if p.name.startswith(".stage_"):
            try:
                _rt.shutil.rmtree(p)
                out["removed"].append(p.name)
            except OSError as e:
                out["errors"].append(f"清理 {p.name} 失败：{e}")
            continue
        # .old_*：判断线上代码是否真的换入完成（Q-06 新判据）
        intact, why = _rt._live_code_intact(roots, p)
        if intact:
            try:
                _rt.shutil.rmtree(p)
                out["removed"].append(p.name)
            except OSError as e:
                out["errors"].append(f"清理 {p.name} 失败：{e}")
            continue
        out["notes"].append(f"{p.name}：线上代码不完整（{why}），换回旧代码")
        # 关键文件缺失 → 换回旧代码
        try:
            res = _rt._restore_old(p)
        except Exception as e:                       # noqa: BLE001 - 恢复失败要如实上报
            out["errors"].append(f"恢复 {p.name} 异常：{e}")
            continue
        if res.get("ok"):
            with contextlib.suppress(OSError):
                _rt.shutil.rmtree(p)
            out["recovered"].append(p.name)
        else:
            out["errors"].extend(res.get("errors") or [])
    return out
