"""一键体检 —— 等价 PHP panel_doctor()（10 项逐项检查 → JSON 列表）。

v1.24.7（方向D）：原先 `panel_doctor` 是 129 行 / CC=35 的单体函数，10 项检查
写在同一段流水账里，加/删一项要改大函数、也没法单独测。现在拆成 10 个
`_check_*` 函数 + `_CHECKS` 注册表：

- 每个 `_check_xxx(inst) -> dict` 返回 `{"name","level","msg"}`，只关心自己那项；
- 注册表顺序 = 前端展示顺序（与 PHP 版一致，不要随意调序）；
- 单项异常不拖垮整体体检：`_run_check` 兜住异常转成 err 项，用户仍能看到
  其余 9 项结果（原先任何一项抛异常都会让整个 ?ajax=doctor 500）。

注意：第 5 项「推送配置」里 `"Array"` 是**故意复刻 PHP 缺陷**（见该函数注释），
不是笔误，改动会破坏与 PHP 版的行为对齐。
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import config as cfg
from services.alerts import heartbeat_read
from services.configops import notify_health_check, yaml_read_simple
from services.containers import container_is_running
from services.history import history_data_file
from services.instances import instance_config
from services.schedule import schedule_load
from services.shell import run_cmd
from services.updateops import check_update, image_check


def os_writable(p) -> bool:
    try:
        return os.access(p, os.W_OK)
    except OSError:
        return False


# ===== 10 项检查（顺序即展示顺序）=====

def _check_docker(inst: dict) -> dict:
    """1. Docker 权限：docker 命令能否以当前用户执行。"""
    r = run_cmd('docker ps -a --format "{{.Names}}"')
    if r["code"] == 0:
        return {"name": "Docker 权限", "level": "ok",
                "msg": "docker 命令可用（www 用户）"}
    return {"name": "Docker 权限", "level": "err",
            "msg": f"docker ps 失败(exit {r['code']})：请把 www 用户加入 docker 组"
                   "（usermod -aG docker www 后重登生效）"}


def _check_container(inst: dict) -> dict:
    """2. 容器状态。"""
    run = container_is_running(inst)
    return {"name": "容器状态", "level": "ok" if run else "warn",
            "msg": "容器运行中" if run else "容器已停止：跑任务前需先启动容器"}


def _check_config(inst: dict) -> dict:
    """3. config.yaml 是否存在且可写。"""
    cfg_path = Path(instance_config(inst))
    if not cfg_path.is_file():
        return {"name": "config.yaml", "level": "err",
                "msg": "配置文件不存在：" + str(cfg_path)}
    if not os_writable(cfg_path):
        return {"name": "config.yaml", "level": "err",
                "msg": "配置文件不可写，页面保存设置会失败（可试 chmod 666）"}
    return {"name": "config.yaml", "level": "ok", "msg": "存在且可写"}


def _check_data_dir(inst: dict) -> dict:
    """4. 数据目录（历史/计划任务/告警状态落盘处）可写。"""
    data_dir = history_data_file(inst).parent
    if not data_dir.is_dir() or not os_writable(data_dir):
        return {"name": "数据目录", "level": "err",
                "msg": f"不可写：{data_dir}（历史/计划任务/告警状态都存这里）"}
    return {"name": "数据目录", "level": "ok",
            "msg": "可写（历史、计划任务、告警状态正常落盘）"}


def _check_notify(inst: dict) -> dict:
    """5. 小助手推送配置。

    PHP 原版用 `implode('、', array_map('strval', $nh['warn']))` —— 数组转字符串
    在 PHP 里恒为 "Array"，于是「缺哪几路」显示成一串 "Array"。
    这里**故意复刻该缺陷**以保持与 PHP 版逐字节一致（有用户/脚本依赖这个输出），
    修它会改变线上可见文案，属于产品决策而非缺陷修复。
    """
    nh = notify_health_check(yaml_read_simple(inst))
    if not nh["master"]:
        return {"name": "推送配置", "level": "warn",
                "msg": "通知总开关未开，小助手不会发任何推送"}
    if nh["warn"]:
        return {"name": "推送配置", "level": "warn",
                "msg": f"已开 {len(nh['ok'])} 路；缺："
                       + "、".join(["Array"] * len(nh["warn"]))}
    if nh["ok"]:
        return {"name": "推送配置", "level": "ok",
                "msg": f"{len(nh['ok'])} 路推送就绪"}
    return {"name": "推送配置", "level": "warn",
            "msg": "总开关已开但没有配置任何推送渠道"}


def _check_cron(inst: dict) -> dict:
    """6. 计划任务 cron 心跳（5 分钟内应有一次巡检心跳）。"""
    sched = schedule_load(inst)
    sched_on = sum(1 for t in sched.get("tasks", []) if t.get("enabled"))
    fresh = max(heartbeat_read(inst).values(), default=0)
    fresh_ok = fresh > 0 and (int(time.time()) - fresh) < 300
    if sched_on > 0 and not fresh_ok:
        return {"name": "计划任务 cron", "level": "err",
                "msg": f"已启用 {sched_on} 个计划任务，但 5 分钟内没收到巡检心跳——"
                       "宝塔计划任务可能没建或停了，定时任务不会触发"}
    if fresh_ok:
        guard = f"守护 {sched_on} 个计划任务" if sched_on > 0 else "面板暂无启用的计划任务"
        return {"name": "计划任务 cron", "level": "ok",
                "msg": f"巡检心跳正常（{guard}）"}
    return {"name": "计划任务 cron", "level": "warn",
            "msg": "未检测到巡检心跳：如需离线告警/定时任务，把「异常告警」卡里的"
                   "巡检命令加进宝塔计划任务（每 1 分钟）"}


def _check_image(inst: dict) -> dict:
    """7. 小助手镜像（6 小时缓存）。"""
    ic = image_check(inst, False)
    if not ic.get("ok"):
        err = "：" + ic["err"] if ic.get("err") else ""
        return {"name": "小助手镜像", "level": "info",
                "msg": f"暂无法获取镜像信息{err}（可稍后重试）"}
    if ic.get("has_update"):
        latest = f"（{ic['latest']}）" if ic.get("latest") else ""
        return {"name": "小助手镜像", "level": "warn",
                "msg": f"有新镜像可用{latest}，可在「镜像更新」卡一键更新"}
    cur = f"（{ic['current']}）" if ic.get("current") else ""
    return {"name": "小助手镜像", "level": "ok", "msg": "已是最新" + cur}


def _check_panel_version(inst: dict) -> dict:
    """8. 面板版本（联网检查，最慢的一项，放最后几项）。"""
    cu = check_update()
    if cu.get("ok") and cu.get("enabled"):
        if cu.get("has_update"):
            latest = "v" + cu["latest"] if cu.get("latest") else "新版本"
            return {"name": "面板版本", "level": "warn",
                    "msg": f"发现 {latest}（当前 v{cfg.PANEL_VERSION}），"
                           "可在「管理面板更新」卡升级"}
        return {"name": "面板版本", "level": "ok",
                "msg": f"已是最新 v{cfg.PANEL_VERSION}"}
    msg = cu.get("msg") or "网络或更新源问题"
    return {"name": "面板版本", "level": "info",
            "msg": f"自动检查不可用（{msg}），当前 v{cfg.PANEL_VERSION}"}


def _check_disk(inst: dict) -> dict:
    """9. 磁盘空间（系统盘 + /data 数据盘，取使用率最高者）。"""
    df = run_cmd("df -B1 / /data 2>/dev/null | tail -n +2")
    max_use, worst = 0, ""
    for line in df["out"].replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        cols = line.strip().split()
        if len(cols) < 6:
            continue
        try:
            pct = int(cols[-2].replace("%", ""))
        except ValueError:
            continue
        if pct >= max_use:
            max_use, worst = pct, cols[0]
    if max_use >= 95:
        return {"name": "磁盘空间", "level": "err",
                "msg": f"{worst} 已用 {max_use}%，快满了，尽快清理"}
    if max_use >= 85:
        return {"name": "磁盘空间", "level": "warn",
                "msg": f"{worst} 已用 {max_use}%，留意大文件增长"}
    if max_use > 0:
        return {"name": "磁盘空间", "level": "ok",
                "msg": f"最高使用率 {max_use}%（{worst}）"}
    return {"name": "磁盘空间", "level": "info", "msg": "无法读取磁盘信息"}


def _check_panel_dir(inst: dict) -> dict:
    """10. 面板目录可写（在线更新/回滚需要）。"""
    if os_writable(cfg.BASE_DIR):
        return {"name": "面板目录", "level": "ok",
                "msg": "可写（在线更新与回滚可用）"}
    return {"name": "面板目录", "level": "warn",
            "msg": "不可写，在线更新与回滚会失败"}


# 注册表：(展示名, 检查函数)。顺序 = 前端展示顺序（与 PHP panel_doctor 一致）。
# 展示名同时用于「该项抛异常」时的兜底文案，避免依赖 docstring 猜名字。
_CHECKS = (
    ("Docker 权限", _check_docker),
    ("容器状态", _check_container),
    ("config.yaml", _check_config),
    ("数据目录", _check_data_dir),
    ("推送配置", _check_notify),
    ("计划任务 cron", _check_cron),
    ("小助手镜像", _check_image),
    ("面板版本", _check_panel_version),
    ("磁盘空间", _check_disk),
    ("面板目录", _check_panel_dir),
)


def _run_check(label: str, fn, inst: dict) -> dict:
    """跑单项检查；意外异常转成 err 项，保证其余项照常返回。

    单项失败不该让整份体检 500 —— 用户点「开始体检」是想知道哪里有问题，
    而不是拿到一个报错页。异常信息里带类型与文案，便于定位是哪项炸了。
    """
    try:
        return fn(inst)
    except Exception as e:                       # noqa: BLE001 —— 体检的最后一道防线
        return {"name": label, "level": "err",
                "msg": f"该项检查异常（{type(e).__name__}: {e}）"}


def panel_doctor(inst: dict) -> dict:
    """一键体检：按注册表顺序跑 10 项检查，返回 {ok, items, took}。"""
    t0 = time.time()
    items = [_run_check(label, fn, inst) for label, fn in _CHECKS]
    return {"ok": True, "items": items, "took": round(time.time() - t0, 1)}
