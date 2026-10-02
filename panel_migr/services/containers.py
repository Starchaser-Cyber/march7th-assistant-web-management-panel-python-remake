"""容器 / 任务操作 —— 等价 PHP compose()/task_start()/POST 容器与任务分支。

所有函数返回 dict：成功给 {"msg": ...}，失败给 {"err": ...}（PHP 的 $msg/$err 分支）。
"""
from __future__ import annotations

from shlex import quote

import config as cfg
from services.history import history_add, history_write, history_read
from services.instances import instance_config, instance_container, instance_dir
from services.shell import run_cmd


def compose(inst: dict, args: str) -> dict:
    """等价 PHP compose()：cd <实例目录> && docker compose <args>。"""
    return run_cmd(f"cd {quote(instance_dir(inst))} && docker compose {args}")


def task_start(inst: dict, sub: str) -> dict:
    """等价 PHP task_start()：docker exec -d <容器> python main.py <sub>。"""
    return run_cmd(f"docker exec -d {quote(instance_container(inst))} python main.py {quote(sub)}")


def container_is_running(inst: dict) -> bool:
    r = run_cmd(f'docker inspect -f "{{{{.State.Running}}}}" {quote(instance_container(inst))} 2>&1')
    return r["out"].strip() == "true"


def container_status(inst: dict) -> str:
    return compose(inst, "ps")["out"]


# ===== 容器操作分支（POST）=====

def op_restart(inst: dict) -> dict:
    r = compose(inst, "restart")
    return {"msg": "容器已重启"} if r["code"] == 0 else {"err": "重启失败：" + r["out"]}


def op_update(inst: dict) -> dict:
    r1 = compose(inst, "pull")
    if r1["code"] != 0:
        return {"err": "拉取镜像失败：" + r1["out"]}
    r2 = compose(inst, "up -d")
    if r2["code"] == 0:
        return {"msg": "镜像已更新，容器已重建"}
    return {"err": "更新失败：" + r2["out"]}


def op_stop_task(inst: dict) -> dict:
    r = compose(inst, "restart")
    if r["code"] == 0:
        return {"msg": "⏹️ 任务已停止（容器已重启），如需继续请重新启动任务"}
    return {"err": "容器重启失败：" + r["out"]}


def op_stop_loop(inst: dict) -> dict:
    r = run_cmd(
        f'docker exec {quote(instance_container(inst))} '
        'pkill -15 -f "main.py currencywarsloop"'
    )
    if r["code"] == 0:
        return {"msg": "🛑 货币战争循环已停止（仅结束循环子进程，容器未重启）"}
    return {"err": "循环停止失败（可能循环未在运行）：" + r["out"]}


def op_stop(inst: dict) -> dict:
    r = compose(inst, "stop")
    if r["code"] == 0:
        return {"msg": "⏸️ 容器已停止，想再运行请点「重启容器」恢复"}
    return {"err": "容器停止失败：" + r["out"]}


def op_task(inst: dict, action: str) -> dict:
    """$TASKS 分支：启动任务 + 记执行历史（PHP 用 $msg 承载失败，不是 $err）。"""
    label = cfg.TASKS.get(action, action)
    r = task_start(inst, action)
    if r["code"] == 0:
        history_add(inst, action, label)
        return {"msg": f"任务「{label}」已后台启动"}
    return {"msg": "任务启动失败：" + r["out"]}


# ===== 任务历史（与 services/history 复用）=====

def history_clear(inst: dict) -> dict:
    """POST history_clear：清空执行历史。"""
    if history_write(inst, {"items": []}):
        return {"ok": True, "msg": "任务执行历史已清空"}
    return {"ok": False, "msg": "清空失败，请检查 data 目录写权限"}


__all__ = [
    "compose", "task_start", "container_is_running", "container_status",
    "op_restart", "op_update", "op_stop_task", "op_stop_loop", "op_stop",
    "op_task", "history_clear", "history_read",
]
