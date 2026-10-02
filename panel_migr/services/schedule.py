"""计划任务（data/schedule.json）—— 等价 PHP schedule_* 全套（v1.15+）。"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

import config as cfg
from services.containers import compose, container_is_running, task_start
from services.history import history_add, history_sync, history_data_file
from config import SCHEDULE_WINDOW_SECONDS


def schedule_data_file(inst: dict) -> Path:
    return history_data_file(inst).parent / "schedule.json"


def schedule_default() -> dict:
    return {"conflict": "skip", "tasks": []}


def schedule_load(inst: dict) -> dict:
    f = schedule_data_file(inst)
    try:
        raw = f.read_text("utf-8")
    except OSError:
        return schedule_default()
    if raw.strip() == "":
        return schedule_default()
    try:
        d = json.loads(raw)
    except ValueError:
        return schedule_default()
    if not isinstance(d, dict):
        return schedule_default()
    out = schedule_default()
    if d.get("conflict") == "stop":
        out["conflict"] = "stop"
    if isinstance(d.get("tasks"), list):
        for t in d["tasks"]:
            if not isinstance(t, dict):
                continue
            out["tasks"].append({
                "id": str(t.get("id") or ""),
                "name": str(t.get("name") or ""),
                "time": str(t.get("time") or ""),
                "days": [x for x in (t.get("days") or [])] if isinstance(t.get("days"), list) else [],
                "args": str(t.get("args") or ""),
                "enabled": bool(t.get("enabled")),
                "last_run": str(t.get("last_run") or ""),
                "last_result": str(t.get("last_result") or ""),
            })
    return out


def schedule_save(inst: dict, data: dict) -> bool:
    if not isinstance(data, dict):
        return False
    f = schedule_data_file(inst)
    try:
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp = f.with_name(f.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), "utf-8")
        os.replace(tmp, f)
        return True
    except (OSError, ValueError):
        return False


def schedule_normalize_days(days) -> list[int]:
    """星期清洗：只收 1-7，去重升序；空数组表示每天。"""
    out: list[int] = []
    if isinstance(days, list):
        for d in days:
            try:
                n = int(str(d).strip() or 0)
            except ValueError:
                continue
            if 1 <= n <= 7 and n not in out:
                out.append(n)
    return sorted(out)


def schedule_days_label(days) -> str:
    days = schedule_normalize_days(days)
    if not days:
        return "每天"
    name = {1: "一", 2: "二", 3: "三", 4: "四", 5: "五", 6: "六", 7: "日"}
    return "、".join(f"周{name[n]}" for n in days)


def schedule_validate(inp: dict) -> tuple[str, dict | None]:
    """返回 (错误信息, 规范化数据)；错误为空串表示通过。"""
    inp = inp if isinstance(inp, dict) else {}
    name = str(inp.get("name") or "").strip()
    if name == "":
        return "任务名称不能为空", None
    if len(name) > 40:
        return "任务名称过长（最多 40 个字）", None

    t = str(inp.get("time") or "").strip()
    if not re.match(r"^([01]?\d|2[0-3]):[0-5]\d$", t):
        return "时间格式不正确，请用 24 小时制（如 04:00）", None
    hh, mm = t.split(":")
    t = "%02d:%02d" % (int(hh), int(mm))

    raw_days = inp.get("days") if isinstance(inp.get("days"), list) else [inp.get("days")]
    days: list[int] = []
    for d in raw_days:
        if isinstance(d, (list, dict)):
            return "星期选择不合法（只能选周一至周日）", None
        s = str(d if d is not None else "").strip()
        if not re.match(r"^[1-7]$", s):
            return "星期选择不合法（只能选周一至周日）", None
        n = int(s)
        if n not in days:
            days.append(n)
    days.sort()

    args = str(inp.get("args") or "").strip()
    if args == "":
        return "请选择要执行的任务", None
    if args not in cfg.TASKS:
        return f"任务「{args}」不在可用任务列表中", None

    return "", {"name": name, "time": t, "days": days, "args": args}


def _local_now(now=None) -> int:
    return int(now if now is not None else time.time())


def schedule_due(task: dict, now: int | None = None) -> tuple[bool, str]:
    """该计划任务此刻是否应触发；返回 (是否触发, 防重复时间点 slot)。"""
    slot = ""
    if not isinstance(task, dict) or not task.get("enabled"):
        return False, slot
    t = str(task.get("time") or "").strip()
    if not re.match(r"^([01]?\d|2[0-3]):[0-5]\d$", t):
        return False, slot
    hh, mm = int(t.split(":")[0]), int(t.split(":")[1])
    now = _local_now(now)

    days = schedule_normalize_days(task.get("days"))
    if days:
        w = time.localtime(now).tm_wday + 1  # 1=周一 … 7=周日
        if w not in days:
            return False, slot

    lt = time.localtime(now)
    target = int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, hh, mm, 0, 0, 0, -1)))
    diff = now - target
    if diff < 0:
        return False, slot
    if diff > SCHEDULE_WINDOW_SECONDS:
        return False, slot
    slot = time.strftime("%Y%m%d-%H%M", time.localtime(target))
    if str(task.get("last_run") or "") == slot:
        return False, slot
    return True, slot


def schedule_run_due(inst: dict, now: int | None = None) -> dict:
    """执行一轮检查：命中即起任务。返回 {'ok','time','ran','skipped'}。"""
    d = schedule_load(inst)
    now = _local_now(now)
    ran: list[str] = []
    skipped: list[str] = []
    changed = False
    busy: bool | None = None

    for i, t in enumerate(d["tasks"]):
        due, slot = schedule_due(t, now)
        if not due:
            continue
        name = t["name"] if t["name"] != "" else t["id"]
        d["tasks"][i]["last_run"] = slot  # 先标记，避免每分钟重复判定
        changed = True

        if not container_is_running(inst):
            d["tasks"][i]["last_result"] = "跳过（容器未运行）"
            skipped.append(name)
            continue

        if busy is None:
            busy = any(
                it.get("status") == "running" for it in history_sync(inst)["items"]
            )
        if busy:
            if d["conflict"] == "stop":
                rr = compose(inst, "restart")
                if rr["code"] != 0:
                    d["tasks"][i]["last_result"] = "失败：停掉当前任务失败（" + rr["out"].strip() + "）"
                    continue
                busy = False
            else:
                d["tasks"][i]["last_result"] = "跳过（有任务在跑）"
                skipped.append(name)
                continue

        args = str(t["args"])
        label = cfg.TASKS.get(args, args)
        sr = task_start(inst, args)
        if sr["code"] == 0:
            history_add(inst, args, label)
            d["tasks"][i]["last_result"] = "已触发"
            ran.append(name)
            busy = True
        else:
            d["tasks"][i]["last_result"] = "失败：" + sr["out"].strip()

    if changed:
        schedule_save(inst, d)
    return {
        "ok": True,
        "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
        "ran": ran,
        "skipped": skipped,
    }


def config_scheduled_tasks_count(inst: dict) -> int:
    """只读统计 config.yaml 自带 scheduled_tasks 条数（顶层块扫描）。"""
    from services.configops import config_read_raw

    raw = config_read_raw(inst, 262144)
    if not raw:
        return 0
    count = 0
    in_block = False
    for line in re.split(r"\r\n|\r|\n", raw):
        if not in_block:
            if re.match(r"^scheduled_tasks\s*:", line):
                in_block = True
            continue
        if line.strip() == "":
            continue
        if not re.match(r"^\s", line):
            in_block = False
            continue
        if re.match(r"^\s*-\s*id\s*:", line):
            count += 1
    return count
