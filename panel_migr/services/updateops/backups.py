"""备份文件管理：列表、清理、索引备份、一键回滚。"""
from __future__ import annotations

import re
from pathlib import Path


import config as cfg

from . import _rt
import contextlib


BACKUP_DIR = cfg.BASE_DIR / "backups"


INDEX_FILE = cfg.BASE_DIR / "index.php"


def backups_dir() -> Path:
    with contextlib.suppress(OSError):
        _rt.BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    return _rt.BACKUP_DIR


def _entry(f: Path, kind: str, ver: str, t: str) -> dict:
    try:
        ts = int(_rt.time.mktime((int(t[0:4]), int(t[4:6]), int(t[6:8]),
                              int(t[8:10]), int(t[10:12]), int(t[12:14]), 0, 0, -1)))
    except ValueError:
        ts = 0
    if ts <= 0:
        try:
            ts = int(f.stat().st_mtime)
        except OSError:
            ts = 0
    return {
        "file": f.name, "path": str(f), "version": ver, "time": ts, "kind": kind,
        "timeStr": _rt.time.strftime("%Y-%m-%d %H:%M:%S", _rt.time.localtime(ts)) if ts > 0 else "未知",
        "size": f.stat().st_size if f.exists() else 0,
    }


def backups_list() -> list[dict]:
    """备份列表：index_*.php（旧版单文件）与 panel_*.zip（v1.20 整包）都识别。"""
    out = []
    d = _rt.backups_dir()
    if not d.is_dir():
        return out
    for f in sorted(d.glob("index_*.php")):
        m = re.match(r"^index_(.*)_(\d{14})\.php$", f.name)
        if not m:
            continue
        out.append(_rt._entry(f, "index", m.group(1) or "未知", m.group(2)))
    for f in sorted(d.glob("panel_*.zip")):
        m = re.match(r"^panel_(.*)_(\d{14})\.zip$", f.name)
        if not m:
            continue
        out.append(_rt._entry(f, "panel", m.group(1) or "未知", m.group(2)))
    out.sort(key=lambda x: (x["time"], x["file"]), reverse=True)
    return out


def backups_prune(keep: int | None = None) -> int:
    keep = max(1, int(_rt.cfg.BACKUP_KEEP if keep is None else keep))
    lst = _rt.backups_list()
    if len(lst) <= keep:
        return 0
    removed = 0
    for item in lst[keep:]:
        p = Path(item["path"])
        try:
            p.unlink()
            removed += 1
        except OSError:
            pass
    return removed


def backup_current_index(version: str = "") -> str:
    d = _rt.backups_dir()
    if not d.is_dir():
        return ""
    ver = (version or _rt.cfg.PANEL_VERSION).strip()
    ver = re.sub(r"[^A-Za-z0-9._-]", "", ver) or "unknown"
    target = d / f"index_{ver}_{_rt.time.strftime('%Y%m%d%H%M%S')}.php"
    try:
        if _rt.INDEX_FILE.is_file():
            _rt.shutil.copyfile(_rt.INDEX_FILE, target)
        else:
            return ""
    except OSError:
        return ""
    with contextlib.suppress(OSError):
        target.chmod(0o644)
    _rt.backups_prune()
    return str(target)


def _rollback_panel(base: str) -> dict:
    """一键回滚整包备份：backups/panel_*.zip → 换入面板目录。"""
    d = _rt.backups_dir()
    target = d / base
    if not target.is_file():
        return {"ok": False, "msg": "备份文件不存在，已中止回滚"}
    try:
        real_dir = d.resolve()
        real_target = target.resolve()
        if not str(real_target).startswith(str(real_dir) + "/"):
            return {"ok": False, "msg": "备份文件路径非法，已中止回滚"}
        data = target.read_bytes()
    except OSError:
        return {"ok": False, "msg": "备份文件读取失败，已中止回滚"}
    if not _rt._zip_struct_ok(data):
        return {"ok": False, "msg": "备份内容异常（不是有效的面板整包），已中止回滚"}
    try:
        ver = _rt._zip_new_version(data)
    except Exception as e:
        return {"ok": False, "msg": f"备份内容异常（{e}），已中止回滚"}
    safety = _rt._backup_panel_zip(_rt.cfg.PANEL_VERSION)   # 回滚前先快照当前版本
    res = _rt._apply_zip(data, ver)
    if not res["ok"]:
        return res
    tail = _rt._schedule_restart()
    tip = f"（回滚前的版本已备份为 {Path(safety).name}）" if safety else ""
    return {"ok": True, "msg": f"已回滚到备份 {base}（v{ver}）{tail}{tip}", "file": base}


def backup_rollback(file: str) -> dict:
    """一键回滚：panel_*.zip（v1.20 整包）或 index_*.php（旧版单文件）。"""
    base = Path(str(file).strip()).name
    if base == "":
        return {"ok": False, "msg": "备份文件名不合法，已中止回滚"}
    if re.match(r"^panel_.*\.zip$", base):
        return _rt._rollback_panel(base)
    if not re.match(r"^index_.*\.php$", base):
        return {"ok": False, "msg": "备份文件名不合法，已中止回滚"}
    d = _rt.backups_dir()
    target = d / base
    if not target.is_file():
        return {"ok": False, "msg": "备份文件不存在，已中止回滚"}
    try:
        real_dir = d.resolve()
        real_target = target.resolve()
        if not str(real_target).startswith(str(real_dir) + "/"):
            return {"ok": False, "msg": "备份文件路径非法，已中止回滚"}
        content = target.read_text("utf-8", "replace")
    except OSError:
        return {"ok": False, "msg": "备份文件路径非法，已中止回滚"}
    if len(content) < 200 or not content.lstrip()[:5].lower() == "<?php":
        return {"ok": False, "msg": "备份内容异常（不是有效的面板文件），已中止回滚"}
    safety = _rt.backup_current_index(_rt.cfg.PANEL_VERSION)
    try:
        _rt.INDEX_FILE.write_text(content, "utf-8")
    except OSError:
        return {"ok": False, "msg": "写入面板主文件失败，请检查面板目录权限"}
    tail = f"（回滚前的版本已备份为 {Path(safety).name}）" if safety else ""
    return {"ok": True, "msg": f"已回滚到备份 {base}{tail}，页面即将刷新", "file": base}
