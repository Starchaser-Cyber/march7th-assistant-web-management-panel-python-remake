"""一键体检 —— 等价 PHP panel_doctor()（10 项逐项检查 → JSON 列表）。"""
from __future__ import annotations

import time
from pathlib import Path

import config as cfg
from services.alerts import heartbeat_read
from services.configops import notify_health_check, yaml_read_simple, h
from services.containers import container_is_running
from services.instances import instance_config
from services.schedule import schedule_load
from services.shell import run_cmd
from services.updateops import check_update, image_check


def panel_doctor(inst: dict) -> dict:
    t0 = time.time()
    items: list[dict] = []

    # 1. Docker 权限
    r = run_cmd('docker ps -a --format "{{.Names}}"')
    if r["code"] == 0:
        items.append({"name": "Docker 权限", "level": "ok", "msg": "docker 命令可用（www 用户）"})
    else:
        items.append({"name": "Docker 权限", "level": "err",
                      "msg": f"docker ps 失败(exit {r['code']})：请把 www 用户加入 docker 组"
                             "（usermod -aG docker www 后重登生效）"})

    # 2. 容器状态
    run = container_is_running(inst)
    items.append({"name": "容器状态", "level": "ok" if run else "warn",
                  "msg": "容器运行中" if run else "容器已停止：跑任务前需先启动容器"})

    # 3. config.yaml 可写
    cfg_path = Path(instance_config(inst))
    if not cfg_path.is_file():
        items.append({"name": "config.yaml", "level": "err", "msg": "配置文件不存在：" + str(cfg_path)})
    elif not os_writable(cfg_path):
        items.append({"name": "config.yaml", "level": "err",
                      "msg": "配置文件不可写，页面保存设置会失败（可试 chmod 666）"})
    else:
        items.append({"name": "config.yaml", "level": "ok", "msg": "存在且可写"})

    # 4. 数据目录可写
    from services.history import history_data_file
    data_dir = history_data_file(inst).parent
    if not data_dir.is_dir() or not os_writable(data_dir):
        items.append({"name": "数据目录", "level": "err",
                      "msg": f"不可写：{data_dir}（历史/计划任务/告警状态都存这里）"})
    else:
        items.append({"name": "数据目录", "level": "ok",
                      "msg": "可写（历史、计划任务、告警状态正常落盘）"})

    # 5. 小助手推送配置
    nh = notify_health_check(yaml_read_simple(inst))
    if not nh["master"]:
        items.append({"name": "推送配置", "level": "warn", "msg": "通知总开关未开，小助手不会发任何推送"})
    elif nh["warn"]:
        # PHP：implode('、', array_map('strval', $nh['warn'])) —— 数组转字符串即 "Array"
        items.append({"name": "推送配置", "level": "warn",
                      "msg": f"已开 {len(nh['ok'])} 路；缺：" + "、".join(["Array"] * len(nh["warn"]))})
    elif nh["ok"]:
        items.append({"name": "推送配置", "level": "ok", "msg": f"{len(nh['ok'])} 路推送就绪"})
    else:
        items.append({"name": "推送配置", "level": "warn", "msg": "总开关已开但没有配置任何推送渠道"})

    # 6. 计划任务 cron 心跳
    sched = schedule_load(inst)
    sched_on = sum(1 for t in sched.get("tasks", []) if t.get("enabled"))
    fresh = max(heartbeat_read(inst).values(), default=0)
    fresh_ok = fresh > 0 and (int(time.time()) - fresh) < 300
    if sched_on > 0 and not fresh_ok:
        items.append({"name": "计划任务 cron", "level": "err",
                      "msg": f"已启用 {sched_on} 个计划任务，但 5 分钟内没收到巡检心跳——"
                             "宝塔计划任务可能没建或停了，定时任务不会触发"})
    elif fresh_ok:
        guard = f"守护 {sched_on} 个计划任务" if sched_on > 0 else "面板暂无启用的计划任务"
        items.append({"name": "计划任务 cron", "level": "ok", "msg": f"巡检心跳正常（{guard}）"})
    else:
        items.append({"name": "计划任务 cron", "level": "warn",
                      "msg": "未检测到巡检心跳：如需离线告警/定时任务，把「异常告警」卡里的"
                             "巡检命令加进宝塔计划任务（每 1 分钟）"})

    # 7. 小助手镜像（6 小时缓存）
    ic = image_check(inst, False)
    if ic.get("ok"):
        if ic.get("has_update"):
            latest = f"（{ic['latest']}）" if ic.get("latest") else ""
            items.append({"name": "小助手镜像", "level": "warn",
                          "msg": f"有新镜像可用{latest}，可在「镜像更新」卡一键更新"})
        else:
            cur = f"（{ic['current']}）" if ic.get("current") else ""
            items.append({"name": "小助手镜像", "level": "ok", "msg": "已是最新" + cur})
    else:
        err = "：" + ic["err"] if ic.get("err") else ""
        items.append({"name": "小助手镜像", "level": "info",
                      "msg": f"暂无法获取镜像信息{err}（可稍后重试）"})

    # 8. 面板版本（联网检查，最慢的一项放最后）
    cu = check_update()
    if cu.get("ok") and cu.get("enabled"):
        if cu.get("has_update"):
            latest = "v" + cu["latest"] if cu.get("latest") else "新版本"
            items.append({"name": "面板版本", "level": "warn",
                          "msg": f"发现 {latest}（当前 v{cfg.PANEL_VERSION}），可在「管理面板更新」卡升级"})
        else:
            items.append({"name": "面板版本", "level": "ok", "msg": f"已是最新 v{cfg.PANEL_VERSION}"})
    else:
        msg = cu.get("msg") or "网络或更新源问题"
        items.append({"name": "面板版本", "level": "info",
                      "msg": f"自动检查不可用（{msg}），当前 v{cfg.PANEL_VERSION}"})

    # 9. 磁盘空间（系统盘 + /data 数据盘）
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
        items.append({"name": "磁盘空间", "level": "err",
                      "msg": f"{worst} 已用 {max_use}%，快满了，尽快清理"})
    elif max_use >= 85:
        items.append({"name": "磁盘空间", "level": "warn",
                      "msg": f"{worst} 已用 {max_use}%，留意大文件增长"})
    elif max_use > 0:
        items.append({"name": "磁盘空间", "level": "ok",
                      "msg": f"最高使用率 {max_use}%（{worst}）"})
    else:
        items.append({"name": "磁盘空间", "level": "info", "msg": "无法读取磁盘信息"})

    # 10. 面板目录可写（在线更新/回滚需要）
    if os_writable(cfg.BASE_DIR):
        items.append({"name": "面板目录", "level": "ok", "msg": "可写（在线更新与回滚可用）"})
    else:
        items.append({"name": "面板目录", "level": "warn", "msg": "不可写，在线更新与回滚会失败"})

    return {"ok": True, "items": items, "took": round(time.time() - t0, 1)}


def os_writable(p) -> bool:
    import os
    try:
        return os.access(p, os.W_OK)
    except OSError:
        return False
