"""容器 / 任务操作 —— 等价 PHP compose()/task_start()/POST 容器与任务分支。

所有函数返回 dict：成功给 {"msg": ...}，失败给 {"err": ...}（PHP 的 $msg/$err 分支）。
"""
from __future__ import annotations

import time
from shlex import quote

import config as cfg
from services import runs
from services.history import history_add, history_write, history_read
from services.instances import instance_container, instance_dir
from services.shell import run_cmd


def compose(inst: dict, args: str) -> dict:
    """等价 PHP compose()：cd <实例目录> && docker compose <args>。"""
    return run_cmd(f"cd {quote(instance_dir(inst))} && docker compose {args}")


def task_start(inst: dict, sub: str, label: str = "",
               after_finish: str = "", invoker: str = "panel") -> dict:
    """启动任务并建运行账本（v1.26 RunLedger）。

    旧实现是 `docker exec -d <容器> python main.py <sub>`（fire-and-forget），
    启动后拿不到 pid / 退出码 / 输出。现在走 services.runs 的 shell 包装层：
    仍然不需要升级小助手镜像，但账本里会有 run_id、pid、exit、完整日志。

    返回沿用 run_cmd 的形状（调用方只看 code/out）：
    - 成功 `{"code": 0, "out": "", "run_id": "<rid>", "start_ts": N}`
    - 失败 `{"code": 1, "out": "<原因>"}`
    """
    r = runs.start_run(inst, sub, label or sub, after_finish=after_finish, invoker=invoker)
    if r.get("err"):
        out = {"code": 1, "out": str(r["err"])}
        if r.get("busy"):
            # 任务互斥：已有运行在跑。调用方据此给出「等它结束/先停止」的提示，
            # 而不是笼统的「启动失败」。
            out["busy"] = r["busy"]
        return out
    return {"code": 0, "out": "", "run_id": r["run_id"], "start_ts": r["start_ts"]}


def container_is_running(inst: dict) -> bool:
    r = run_cmd(f'docker inspect -f "{{{{.State.Running}}}}" {quote(instance_container(inst))} 2>&1')
    return r["out"].strip() == "true"


def ensure_running(inst: dict, timeout: float = 15.0) -> dict:
    """v1.21 链式启动：容器未运行时自动 compose up -d 并轮询等待就绪。
    成功返回 {"started": bool}（True=本次拉起了容器）；失败返回 {"err": ...}。
    供快捷操作入口在 exec 任务前调用，实现「点一下 → 小助手自动运行」。"""
    if container_is_running(inst):
        return {"started": False}
    r = compose(inst, "up -d")
    if r["code"] != 0:
        return {"err": "小助手自动启动失败：" + r["out"]}
    deadline = time.time() + max(1.0, timeout)
    while time.time() < deadline:
        if container_is_running(inst):
            time.sleep(0.6)  # 等容器主进程初始化完再 exec 任务
            return {"started": True}
        time.sleep(0.5)
    return {"err": f"小助手启动超时（{int(max(1.0, timeout))} 秒内未进入运行状态），请点「重启容器」后重试"}


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
    r = task_start(inst, action, label)
    if r["code"] == 0:
        history_add(inst, action, label, run_id=r.get("run_id") or "")
        return {"msg": f"任务「{label}」已后台启动"}
    return {"msg": "任务启动失败：" + r["out"]}


def op_stop_run(inst: dict, run_id: str) -> dict:
    """v1.26：按 run_id 精确停止一次运行（不再重启整个容器）。"""
    r = runs.stop_run(inst, run_id)
    if r.get("err"):
        return {"err": r["err"]}
    return {"msg": r.get("msg") or "已发送停止信号"}


# ===== 任务历史（与 services/history 复用）=====

def history_clear(inst: dict) -> dict:
    """POST history_clear：清空执行历史。"""
    if history_write(inst, {"items": []}):
        return {"ok": True, "msg": "任务执行历史已清空"}
    return {"ok": False, "msg": "清空失败，请检查 data 目录写权限"}


__all__ = [
    "compose", "task_start", "container_is_running", "container_status", "ensure_running",
    "op_restart", "op_update", "op_stop_task", "op_stop_loop", "op_stop",
    "op_task", "op_stop_run", "history_clear", "history_read",
]
