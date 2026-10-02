"""多实例解析 —— 等价 PHP instances_load() / instance_current()。

实例配置在 .instances.php（PHP return array），经 phpfile.load_php_file 读取。
当前实例由 session 键 m7a_instance 决定（session 与 PHP 共享，切换状态互通）。
"""
from __future__ import annotations

import re

from config import DEFAULT_CONTAINER, DEFAULT_DIR, INSTANCES_FILE
from phpfile import load_php_file
from phpsess import session_set

_CONTAINER_RE = re.compile(r"[A-Za-z0-9_-]+")

_FALLBACK = [{
    "id": DEFAULT_CONTAINER,
    "name": "主号",
    "container": DEFAULT_CONTAINER,
    "dir": DEFAULT_DIR,
    "default": True,
}]


def instances_load() -> list[dict]:
    loaded = load_php_file(INSTANCES_FILE, None)
    if not isinstance(loaded, list) or not loaded:
        return [dict(x) for x in _FALLBACK]
    valid = []
    for item in loaded:
        if not isinstance(item, dict):
            continue
        container = str(item.get("container") or "").strip()
        directory = str(item.get("dir") or "").strip().rstrip("/")
        if not container or not directory or not _CONTAINER_RE.fullmatch(container):
            continue
        valid.append({
            "id": str(item.get("id") or container),
            "name": (str(item.get("name") or "").strip() or container),
            "container": container,
            "dir": directory,
            "default": bool(item.get("default")),
        })
    return valid or [dict(x) for x in _FALLBACK]


def instance_current(session: dict) -> dict:
    """session 指定实例 → 否则 default → 否则第一个（对齐 PHP 优先级）。"""
    wanted = str(session.get("m7a_instance") or "")
    lst = instances_load()
    if wanted:
        for it in lst:
            if wanted == it["id"] or wanted == it["container"]:
                return it
    for it in lst:
        if it["default"]:
            return it
    return lst[0]


def panel_config() -> dict:
    """面板自身配置 .panel_config.php —— 等价 PHP panel_config_load()。"""
    cfg = load_php_file(str(_panel_config_path()), None)
    if isinstance(cfg, dict):
        return cfg
    return {}


def panel_config_save(updates: dict) -> bool:
    """等价 PHP panel_config_load()+save()：合并写回 var_export 格式 + chmod 0600。"""
    from phpfile import atomic_write_text, dump_php_return_comment

    cfg = dict(panel_config())
    cfg.update(updates)
    path = _panel_config_path()
    try:
        atomic_write_text(
            path,
            dump_php_return_comment(cfg, "面板自身配置（自动生成，v1.6+）"),
            mode=0o600,
        )
        return True
    except OSError:
        return False


def _panel_config_path():
    from config import PANEL_CONFIG_FILE
    return PANEL_CONFIG_FILE


# ===== 实例操作（等价 PHP instance_* 全套）=====

def instance_dir(inst: dict) -> str:
    return str(inst.get("dir") or DEFAULT_DIR).rstrip("/")


def instance_container(inst: dict) -> str:
    return str(inst.get("container") or DEFAULT_CONTAINER)


def instance_config(inst: dict) -> str:
    return instance_dir(inst) + "/config.yaml"


def instance_name(inst: dict) -> str:
    return str(inst.get("name") or instance_container(inst))


def instance_switch_to(session: dict, sid: str, target: str) -> bool:
    """等价 PHP instance_switch_to()：session['m7a_instance'] = 命中实例 id。"""
    target = str(target or "")
    for item in instances_load():
        if item["id"] == target or item["container"] == target:
            session["m7a_instance"] = item["id"]
            session_set(sid, "m7a_instance", item["id"])
            return True
    return False


def instances_write(items: list) -> bool:
    """写回 .instances.php（var_export 格式，PHP include 与 Python 解析互见）。"""
    from phpfile import atomic_write_text, dump_php_return
    try:
        atomic_write_text(INSTANCES_FILE, dump_php_return(items))
        return True
    except OSError:
        return False
