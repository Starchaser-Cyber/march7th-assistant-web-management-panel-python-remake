"""重启与健康探测：systemd 单元判定、detach 探测脚本、端口轮询。"""
from __future__ import annotations

import os
import sys
from pathlib import Path



from . import _rt


# 重启与健康探测（v1.24.4 P0-3/P0-6）
_RESTART_UNIT = "m7a-panel"


_HEALTH_PROBE_SCRIPT = "/tmp/m7a_health_probe.py"


# detach 出去的健康探测 / 回滚脚本（v1.24.4 P0-3）。
# 为什么要独立脚本：一键更新跑在被重启进程**自己的**请求线程里
# （routers/write.py h_do_update → main.py:59 to_thread.run_sync），
# 原地 systemctl restart 会让 systemctl 与 uvicorn 优雅关停互等，
# 90s 后 SIGKILL，重启后的探测与回滚成为死代码。
# 因此把「restart → 轮询端口 → 失败回滚」整体交给 setsid 出去的独立进程：
# 父进程立即返回响应，随后被杀也不影响探测；脚本自己持有 old/ 的清理权。
_PROBE_SCRIPT_SRC = '''#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""m7a-panel 更新后重启 / 健康探测 / 失败自动回滚（v1.24.4 P0-3）。

由 panel 更新收尾时以 detach 方式启动（setsid + 后台），因此**不依赖发起更新的
进程存活**：systemctl restart 会杀掉那个进程，本脚本继续跑完探测；探测失败时把
old/ 里的旧代码换回 base 再重启一次。

用法（参数全部由 updateops 传入）：
  m7a_health_probe.py <base> <old> <stage> <port> <retries> <interval> <unit>

时序：
  0s    启动（父进程已把 HTTP 响应返回给浏览器）
  1s    systemctl restart <unit>
  +N s  每 interval 秒 GET http://127.0.0.1:<port>/ 一次，共 retries 次
  成功  → 删 old/ stage/，退出 0
  失败  → 用 old/ 覆盖回 base，再 restart 一次；old/ 换回成功才删，
          失败则原样保留并退出 3（旧代码仍在 old/，可人工恢复）
日志写到 <old>.probe.log，便于事后排查。
"""
import os
import shutil
import subprocess
import sys
import time
import urllib.request


def log(msg, path):
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\\n")
    except OSError:
        pass


def run(cmd, timeout=90):
    try:
        p = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return p.returncode, ((p.stdout or "") + (p.stderr or "")).strip()
    except Exception as e:
        return 1, str(e)


def probe(port):
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/" % int(port), timeout=5) as r:
            return int(getattr(r, "status", 0) or 0) == 200
    except Exception:
        return False


def rollback(base, old):
    """把 old/ 里的原条目换回 base；返回失败条目数。"""
    failed = 0
    for name in sorted(os.listdir(old)):
        if name.startswith(".venv_stash_"):
            continue
        src = os.path.join(old, name)
        dst = os.path.join(base, name)
        try:
            if os.path.isdir(dst) and not os.path.islink(dst):
                shutil.rmtree(dst)
            elif os.path.lexists(dst):
                os.unlink(dst)
            shutil.move(src, dst)
        except OSError:
            failed += 1
    return failed


def main():
    if len(sys.argv) < 8:
        return 2
    base, old, stage, port, retries, interval, unit = sys.argv[1:8]
    logpath = old.rstrip("/") + ".probe.log"
    time.sleep(1)
    rc, out = run("systemctl restart %s" % unit)
    log("restart rc=%s %s" % (rc, out[:200]), logpath)
    for _ in range(int(retries)):
        time.sleep(float(interval))
        if probe(port):
            log("probe ok, cleanup", logpath)
            shutil.rmtree(old, ignore_errors=True)
            shutil.rmtree(stage, ignore_errors=True)
            return 0
    log("probe failed, rolling back", logpath)
    if not os.path.isdir(old):
        log("no old/ to roll back, giving up", logpath)
        return 4
    failed = rollback(base, old)
    run("systemctl restart %s" % unit)
    if failed:
        log("rollback incomplete (%d items), kept %s" % (failed, old), logpath)
        return 3
    shutil.rmtree(old, ignore_errors=True)
    shutil.rmtree(stage, ignore_errors=True)
    log("rolled back ok", logpath)
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''


def _schedule_restart() -> str:
    """占位：重启已在 _apply_zip 内同步完成（含健康探测）。
    此处只返回就绪提示，供 do_update 拼消息。"""
    return "，面板已重启并就绪"


def _self_under_unit() -> bool:
    """当前进程是否**就由** _RESTART_UNIT 这个 systemd 单元托管（v1.24.4 P0-3）。

    为什么必须区分：一键更新的调用链是 routers/write.py h_do_update →
    main.py:59 `await to_thread.run_sync(handler, request)`，即在**本进程自己的
    线程**里执行 `systemctl restart m7a-panel`。systemctl 等 uvicorn 优雅退出，
    uvicorn 等这个请求线程跑完，两边互等到 TimeoutStopSec=90s 被 SIGKILL ——
    重启后的健康探测与自动回滚永远是死代码。
    因此只有「确实会被自己重启掉」的进程才走 detach；pytest / 前台手工运行
    仍走同步路径（行为与 v1.24.1 一致，便于测试与手工验证）。
    """
    try:
        cg = Path("/proc/self/cgroup").read_text("utf-8", "replace")
    except OSError:
        return False
    return f"/{_rt._RESTART_UNIT}.service" in cg


def _probe_python() -> str:
    """detach 脚本用的解释器：优先当前解释器，退回系统 python3。"""
    exe = sys.executable or ""
    try:
        if exe and Path(exe).exists():
            return exe
    except OSError:
        pass
    return _rt.shutil.which("python3") or "/usr/bin/python3"


def _write_probe_script() -> Path:
    """把自包含的健康探测/回滚脚本写到 /tmp（每次调用重写，避免陈旧版本）。"""
    p = Path(_rt._HEALTH_PROBE_SCRIPT)
    try:
        p.write_text(_rt._PROBE_SCRIPT_SRC, "utf-8")
        p.chmod(0o755)
    except OSError:
        pass
    return p


def _detach_restart_and_probe(port: int, retries: int, interval: int,
                              old: Path, stage: Path | None) -> dict:
    """把「restart → 轮询端口 → 失败回滚」交给 detach 出去的独立进程。

    v1.24.4（P0-3）。这里必须**立刻返回**：本进程马上会被 systemctl 杀掉，
    后续探测与回滚由 _HEALTH_PROBE_SCRIPT 那个独立进程完成，它持有 old/ 的
    清理权（成功删、失败保留现场）。因此本函数返回 ok=True 且 detach=True，
    _apply_zip 见到 detach 就不再同步清理 old/。

    两种 detach 手段，按「能不能活过本次 restart」排序：

    ① `systemd-run --no-block --unit=m7a-update-probe-<pid> ...`：把脚本放进
       **独立的 transient unit**，因此不在本服务的 cgroup 里。`systemctl restart`
       的默认 KillMode=control-group 只会杀本服务 cgroup 内的进程 —— 独立 unit
       的探测脚本不受影响，能完整跑完「restart → 轮询 → 失败回滚」。这是首选。
    ② 退化：`setsid ... &`。它脱离会话但**仍在同一 cgroup**，restart 时会被连带
       杀掉 —— 探测与回滚就失去保障。仅在 ① 不可用时使用（例如非 root 运行、
       polkit 拒绝 "Interactive authentication required"），此时 msg 会明确告知
       探测能力已降级，运维需人工确认新版本是否起来。
    """
    script = _rt._write_probe_script()
    argv = [
        _rt._probe_python(), str(script),
        str(_rt.cfg.BASE_DIR), str(old), str(stage or ""), str(port),
        str(retries), str(interval), _rt._RESTART_UNIT,
    ]
    # 用单引号包裹每个参数，避免路径里的空格/特殊字符被 shell 拆开
    quoted = " ".join("'" + a.replace("'", "'\\''") + "'" for a in argv)

    # —— ① 独立 transient unit（首选，能活过 restart）——
    if _rt.shutil.which("systemd-run"):
        unit = f"m7a-update-probe-{os.getpid()}"
        cmd = f"systemd-run --no-block --collect --unit={unit} {quoted}"
        try:
            r = _rt.run_cmd(cmd, timeout=10)
        except Exception:
            r = {"code": 1, "out": ""}
        if r.get("code") == 0:
            return {
                "ok": True, "skipped": False, "probe_ok": False, "detach": True,
                "detach_mode": "systemd-run",
                "msg": (f"，面板正在重启（独立单元 {unit} 探测 127.0.0.1:{port}，"
                        f"失败会自动回滚并保留日志 {old}.probe.log）"),
            }
        # 失败（多为 polkit 权限）→ 落②，不把权限错误当成更新失败

    # —— ② 退化：setsid 脱离会话（不需要权限，但会被 cgroup 连带杀掉）——
    cmd = f"setsid {quoted} >/dev/null 2>&1 </dev/null &"
    try:
        r = _rt.run_cmd(cmd, timeout=10)
    except Exception as e:
        return {"ok": False, "skipped": False, "probe_ok": False, "detach": False,
                "msg": f"无法 detach 重启进程（{e}），请手动重启面板"}
    if r.get("code") != 0:
        return {"ok": False, "skipped": False, "probe_ok": False, "detach": False,
                "msg": (f"无法 detach 重启进程（{r.get('code')} "
                        f"{(r.get('out') or '').strip()[:120]}），请手动重启面板")}
    return {
        "ok": True, "skipped": False, "probe_ok": False, "detach": True,
        "detach_mode": "setsid",
        "msg": (f"，面板正在重启（setsid 探测 127.0.0.1:{port}；注意：本机无法建"
                f"独立 systemd 单元，探测进程可能随重启被杀，失败自动回滚不保证，"
                f"日志见 {old}.probe.log）"),
    }


def _restart_and_health_check(port: int = 0, retries: int = 3, interval: int = 2,
                              old: Path | None = None, stage: Path | None = None) -> dict:
    """更新后重启 m7a-panel 并确认本地端口就绪；失败返回失败原因。

    三种部署形态：
    - systemd 且**本进程就在该单元内**（正常部署）：detach 一个独立脚本去做
      「restart → 轮询 HTTP 127.0.0.1:<port>/ → 失败自动回滚」，本进程立即返回，
      随后被 systemd 杀掉也不影响探测与回滚（v1.24.4 P0-3）。
    - systemd 但本进程不在该单元内（pytest / 手工前台运行）：同步 restart +
      轮询 HTTP 127.0.0.1:<port>/ 直到 200 或超时，失败由 _apply_zip 回滚。
    - cron 看门狗 / 手动托管：不强行重启，返回 skipped=True 让上层提示用户手动处理。

    v1.24.4（P0-6）：端口默认取 cfg.LISTEN_PORT，不再硬编码 9999（部署普遍是 8787）。
    """
    port = int(port or _rt.cfg.LISTEN_PORT)
    try:
        r = _rt.run_cmd("systemctl is-active m7a-panel", timeout=10)
        first = (r["out"] or "").strip().split("\n")[0]
        systemd_active = r["code"] == 0 and first in ("active", "activating", "reloading")
    except Exception:
        systemd_active = False

    if not systemd_active:
        return {
            "ok": True, "skipped": True, "probe_ok": False,
            "msg": "未检测到 systemd 服务 m7a-panel，请手动重启面板后生效",
        }

    # —— 形态一：本进程就在该单元内 → detach，交给独立脚本 restart+探测+回滚 ——
    # 绝不能在这里原地 systemctl restart 自己：见 _self_under_unit() 的说明。
    if old is not None and _rt._self_under_unit():
        return _rt._detach_restart_and_probe(port, retries, interval, old, stage)

    # —— 形态二：systemd 托管但本进程不在单元内（测试 / 前台手工）→ 同步重启 + 探测 ——
    r = _rt.run_cmd(f"systemctl restart {_rt._RESTART_UNIT}", timeout=30)
    if r.get("code") != 0:
        return {"ok": False, "skipped": False, "probe_ok": False,
                "msg": f"systemctl restart 失败：{(r.get('out') or '').strip()}"}

    for i in range(retries):
        _rt.time.sleep(interval)
        try:
            hr = _rt.http_get(f"http://127.0.0.1:{port}/", timeout=5)
            if hr["code"] == 200:
                return {"ok": True, "skipped": False, "probe_ok": True, "msg": ""}
        except Exception:
            pass

    # 失败：拉一条 systemd 最后几行日志方便定位
    journal = ""
    try:
        jr = _rt.run_cmd("journalctl -u m7a-panel -n 8 --no-pager", timeout=10)
        journal = (jr.get("out") or "").strip()
    except Exception:
        pass
    tail = f"；最近日志：{journal}" if journal else ""
    return {
        "ok": False, "skipped": False, "probe_ok": False,
        "msg": f"服务重启后健康探测失败（{retries} 次 × {interval}s 均未收到 HTTP 200）{tail}",
    }
