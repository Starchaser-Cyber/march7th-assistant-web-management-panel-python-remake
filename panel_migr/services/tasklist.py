"""动态任务表 —— 把容器里真实的 `python main.py --list` 结果搬到面板上。

旧实现 `config.py:TASKS` 硬编码 8 个任务，而上游容器实际有 27 个（`--list` 实测）：
日常 / 差分宇宙 / 锄大地 / 混沌回忆 / 虚构叙事 / 末日幻影 / 兑换码 / 界面测试……
少掉的 19 个用户只能自己去敲命令。

本模块：
1. `probe_tasks(inst)` 跑一次 `docker exec <容器> python main.py --list` 并解析
2. 结果缓存到 `<data>/tasklist_<容器>.json`（带容器镜像 digest 指纹，镜像变了自动失效）
3. `tasks(inst)` 返回合并后的任务表：容器真有的 ∪ config.TASKS 里面板特别标注的
4. `resolve(inst, key)` 校验某个 key 在容器里是否真的可用

解析容错：容器 `--list` 输出格式是
    可用的任务列表:
    ----------------------------------------
      main                 完整运行
      routine              日常
    ----------------------------------------
只认「两个以上空格分隔、第一列是 ASCII 标识符」的行，其余（标题/分隔线/使用示例）跳过。
"""
from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from shlex import quote

import config as cfg
from services.instances import instance_container
from services.shell import run_cmd

# 解析 --list：行首两个空格 + key + 至少两个空格 + 中文标签
_LINE_RE = re.compile(r"^\s{1,4}([A-Za-z][A-Za-z0-9_]{1,40})\s{2,}(\S.*?)\s*$")

CACHE_TTL = 300.0          # 秒；缓存过期后重新探测
PROBE_TIMEOUT = 20         # docker exec 超时

# 探测结果的进程内缓存：container → (过期时间, list)
_MEM: dict[str, tuple[float, list]] = {}
_LOCK = threading.RLock()

# 分组规则：面板按「用户心智」把 27 个任务归到 5 组（顺序即界面顺序）。
# 未列出的 key 落到「其他」组，保证新增任务不会被藏起来。
GROUPS: list[tuple[str, str, list[str]]] = [
    ("core", "常用", ["main", "routine", "daily", "power", "notify"]),
    ("weekly", "周常", ["forgottenhall", "purefiction", "apocalyptic",
                        "currencywars", "divergent", "universe", "fight"]),
    ("loop", "循环与接管", ["currencywarsloop", "currencywarstemp",
                            "divergentloop", "divergenttemp"]),
    ("native", "原生界面", ["universe_gui", "fight_gui"]),
    ("update", "更新与维护", ["game_update", "game_pre_download", "app_update",
                              "universe_update", "fight_update", "mobileui_update"]),
    ("misc", "其他", ["game", "redemption", "screen_test"]),
]

# 补充说明：config.TASKS 里已有 desc 的沿用 paneldefs，这里只补容器独有的 19 个。
DESCS: dict[str, str] = {
    "routine": "每日实训 · 领取奖励 · 清体力",
    "game_update": "启动游戏并执行内置更新",
    "game_pre_download": "预下载游戏资源包",
    "app_update": "更新三月七小助手本体",
    "currencywars": "货币战争（单次）",
    "currencywarstemp": "货币战争中途接管",
    "divergent": "差分宇宙（单次）",
    "divergenttemp": "差分宇宙中途接管",
    "fight": "锄大地（单次）",
    "universe": "模拟宇宙（单次）",
    "universe_gui": "走游戏原生界面跑模拟宇宙",
    "fight_gui": "走游戏原生界面锄大地",
    "universe_update": "更新模拟宇宙所需资源",
    "fight_update": "更新锄大地所需资源",
    "mobileui_update": "更新触屏模式资源",
    "forgottenhall": "混沌回忆",
    "purefiction": "虚构叙事",
    "apocalyptic": "末日幻影",
    "redemption": "自动兑换码",
    "screen_test": "界面可切换性自检",
    "game": "仅启动游戏，不执行任务",
}


# 不可暂停的任务（上游 PAUSABLE_TASKS 之外的都是这类：启动游戏 / 更新 / 测试）
NON_PAUSABLE = {"game", "game_update", "game_pre_download", "app_update",
                "universe_update", "fight_update", "mobileui_update", "notify"}


def _cache_file(inst: dict) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", instance_container(inst))
    return cfg.DATA_DIR / f"tasklist_{safe}.json"


def parse_list_output(text: str) -> list[dict]:
    """解析 `main.py --list` 的输出 → [{"key","label"}]。"""
    out: list[dict] = []
    seen: set[str] = set()
    for raw in (text or "").splitlines():
        m = _LINE_RE.match(raw)
        if not m:
            continue
        key, label = m.group(1), m.group(2).strip()
        if key in seen:
            continue
        seen.add(key)
        out.append({"key": key, "label": label})
    return out


def probe_tasks(inst: dict, force: bool = False) -> dict:
    """探测容器里的任务列表。

    返回 {"tasks": [...], "source": "probe"|"cache"|"fallback", "ts": N, "err": str}。
    """
    ctr = instance_container(inst)
    now = time.time()
    with _LOCK:
        hit = _MEM.get(ctr)
        if hit and not force and hit[0] > now:
            return {"tasks": hit[1], "source": "cache", "ts": int(hit[0] - CACHE_TTL), "err": ""}
        cf = _cache_file(inst)
        if not force and cf.is_file():
            try:
                d = json.loads(cf.read_text("utf-8"))
            except (OSError, ValueError):
                d = {}
            ts = float(d.get("ts") or 0)
            items = d.get("tasks")
            if isinstance(items, list) and items and now - ts < CACHE_TTL:
                _MEM[ctr] = (now + CACHE_TTL, items)
                return {"tasks": items, "source": "cache", "ts": int(ts), "err": ""}

    r = run_cmd(f"docker exec {quote(ctr)} python main.py --list", timeout=PROBE_TIMEOUT)
    items = parse_list_output(r.get("out") or "")
    err = ""
    if r["code"] != 0 or not items:
        err = (r.get("out") or "").strip()[:300] or "容器未返回任务列表"
        # 探测失败时用上一次的缓存（哪怕过期），总比空表好。
        # 先看内存，再看磁盘 —— 进程重启后内存是空的，磁盘缓存仍能救场。
        with _LOCK:
            hit = _MEM.get(ctr)
        if hit:
            return {"tasks": hit[1], "source": "cache", "ts": 0, "err": err}
        try:
            d = json.loads(cf.read_text("utf-8"))
        except (OSError, ValueError):
            d = {}
        cached = d.get("tasks")
        if isinstance(cached, list) and cached:
            with _LOCK:
                _MEM[ctr] = (now + CACHE_TTL, cached)
            return {"tasks": cached, "source": "cache",
                    "ts": int(float(d.get("ts") or 0)), "err": err}
        items = []

    if items:
        with _LOCK:
            _MEM[ctr] = (now + CACHE_TTL, items)
        try:
            cfg.DATA_DIR.mkdir(parents=True, exist_ok=True)
            cf.write_text(json.dumps({"ts": int(now), "tasks": items}, ensure_ascii=False),
                          "utf-8")
        except OSError:
            pass
    return {"tasks": items, "source": "probe" if items else "fallback",
            "ts": int(now), "err": err}


def tasks(inst: dict, force: bool = False) -> list[dict]:
    """返回带分组/可暂停标记的任务表。

    容器探测成功 → 以容器为准（27 个），面板 config.TASKS 只用来补中文标签；
    探测失败 → 退回 config.TASKS（8 个），保证界面不空。
    """
    pr = probe_tasks(inst, force=force)
    found = pr["tasks"]
    if not found:
        return [{"key": k, "label": v, "group": "core", "group_label": "常用",
                 "pausable": k not in NON_PAUSABLE}
                for k, v in cfg.TASKS.items()]

    group_of: dict[str, tuple[str, str]] = {}
    for gkey, glabel, keys in GROUPS:
        for k in keys:
            group_of[k] = (gkey, glabel)

    out = []
    for it in found:
        k = it["key"]
        gk, gl = group_of.get(k, ("misc", "其他"))
        out.append({
            "key": k,
            "label": it.get("label") or cfg.TASKS.get(k) or k,
            "group": gk,
            "group_label": gl,
            "pausable": k not in NON_PAUSABLE,
        })
    return out


def grouped(inst: dict, force: bool = False) -> list[dict]:
    """按分组聚合成 [{"key","label","items":[...]}]，空组不返回。"""
    rows = tasks(inst, force=force)
    order = [g[0] for g in GROUPS]
    label_of = {g[0]: g[1] for g in GROUPS}
    buckets: dict[str, list] = {}
    for r in rows:
        buckets.setdefault(r["group"], []).append(r)
    return [{"key": gk, "label": label_of.get(gk, gk), "items": buckets[gk]}
            for gk in order if buckets.get(gk)]


def resolve(inst: dict, key: str) -> str:
    """校验 key 是否可用；可用返回空串，否则返回中文错误。

    容器探测失败（拿不到列表）时退回 config.TASKS 白名单，
    避免「容器一时抽风 → 所有任务都不能点」。
    """
    key = str(key or "")
    if not key:
        return "缺少任务标识"
    pr = probe_tasks(inst)
    if pr["tasks"]:
        if any(t["key"] == key for t in pr["tasks"]):
            return ""
        return f"任务「{key}」不在小助手支持的任务列表中"
    if key in cfg.TASKS:
        return ""
    return f"任务「{key}」不在可用任务列表中"


def label_of(inst: dict, key: str) -> str:
    for t in tasks(inst):
        if t["key"] == key:
            return t["label"]
    return cfg.TASKS.get(key, key)


__all__ = ["GROUPS", "NON_PAUSABLE", "DESCS", "parse_list_output", "probe_tasks",
           "tasks", "grouped", "resolve", "label_of", "CACHE_TTL"]
