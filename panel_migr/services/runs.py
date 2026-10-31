"""运行账本（RunLedger）—— 给「启动即失联」的任务补上身份、退出码与实时输出。

背景：旧实现 `docker exec -d <容器> python main.py <task>` 是 fire-and-forget，
启动后面板只有「已后台启动」一句话，拿不到 run_id / pid / 退出码 / 输出，
结束时间只能靠「日志静默 90 秒」反推（多个任务串行时会被后一个任务续命，
导致历史里出现 6.5 小时的假耗时）。

本模块用一层 shell 包装把信息补回来，**不需要升级小助手镜像**（旧镜像实测可用）：

    docker exec -d -e MARCH7TH_CONTROL_FILE=... <容器> sh -c '
        cd /m7a
        python main.py <task> > <runs>/<run_id>.log 2>&1 &
        echo $! > <runs>/<run_id>.pid
        wait $!                      # 等子进程结束（exec -d 让这个 sh 在后台挂着）
        echo $? > <runs>/<run_id>.exit
    '

账本落在实例目录的 `logs/runs/`（容器内 `/m7a/logs/runs`，bind mount 实时可见）：
- `<run_id>.log`    完整输出（含 ANSI，读取时清洗）
- `<run_id>.pid`    子进程 pid（精确停止用）
- `<run_id>.exit`   退出码（结束的唯一权威依据）
- `<run_id>.json`   元数据（task/label/start_ts/argv/invoker/version）
- `<run_id>.control` 暂停控制文件（旧镜像只认 pause/resume）
- `<run_id>.control.state` 控制回执

run_id 与上游 v2026.10.16+ 的 `utils/runs.py` 完全一致：`%Y%m%d-%H%M%S-<4位hex>`，
将来镜像升级到带原生 `--status/--follow/--control` 的版本时可以直接对接。
"""
from __future__ import annotations

import json
import os
import re
import secrets
import time
from pathlib import Path
from shlex import quote

from services.instances import instance_container, instance_dir
from services.shell import run_cmd

# 容器内 logs 目录（bind mount 到 <实例目录>/logs）
CONTAINER_LOGS_DIR = os.environ.get("M7A_CONTAINER_LOGS", "/m7a/logs")
RUNS_DIR_NAME = "runs"

RUN_ID_RE = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{4}\Z")
# 任务 key 只允许字母数字下划线：它会被拼进 shell 脚本，必须严校验
_TASK_RE = re.compile(r"^[A-Za-z0-9_]{1,64}$")

# 账本保留份数（按 .json 计数，超出删最老的整套文件）
KEEP_RUNS = 100
# 进程已死但 .exit 尚未落盘时，日志 mtime 在这么多秒内视为「正在收尾」，不算中断
_FINISH_GRACE = 8

# 退出码 → 人话（对齐上游 utils/runs.py 的约定）
EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_FIRST_RUN_REQUIRED = 3
EXIT_BUSY = 4
EXIT_ELEVATION_FAILED = 5
EXIT_STOPPED = 6

EXIT_LABELS = {
    0: "成功",
    1: "失败",
    2: "参数错误",
    3: "需要先完成首次配置",
    4: "已有任务在跑",
    5: "提权失败",
    6: "已停止",
    -1: "已中断",
    -9: "被强制杀死",
    -15: "被终止（SIGTERM）",
    124: "超时",
}

# pid 存活探测结果的进程内缓存：run_id → (过期时间, bool)
_PID_CACHE: dict[str, tuple[float, bool]] = {}
_PID_CACHE_TTL = 2.0

# 被信号杀死的退出码（shell 约定 128+N）：SIGTERM=143、SIGKILL=137、SIGINT=130
_STOP_SIGNALS = {130, 137, 143}


def exit_label(code) -> str:
    """退出码 → 人话；未知码给出「退出码 N」。"""
    if code is None:
        return ""
    if code in EXIT_LABELS:
        return EXIT_LABELS[code]
    if code in _STOP_SIGNALS:
        return "被终止（收到停止信号）"
    if isinstance(code, int) and code > 128:
        return f"被信号终止（{code - 128}）"
    return f"退出码 {code}"


# ===== 路径 =====

def runs_dir(inst: dict) -> Path:
    """宿主侧账本目录：<实例目录>/logs/runs（容器内同名 mount 为 /m7a/logs/runs）。"""
    return Path(instance_dir(inst)) / "logs" / RUNS_DIR_NAME


def container_runs_dir(inst: dict) -> str:
    return CONTAINER_LOGS_DIR.rstrip("/") + "/" + RUNS_DIR_NAME


def _ensure_runs_dir(inst: dict) -> Path:
    d = runs_dir(inst)
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return d


# ===== 基础读写 =====

def new_run_id() -> str:
    """与上游一致的 run_id：20261031-131045-a1b2。"""
    return time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)


def valid_run_id(rid) -> bool:
    return bool(isinstance(rid, str) and RUN_ID_RE.match(rid))


def _atomic_write(path: Path, text: str) -> bool:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(3)}.tmp")
    try:
        tmp.write_text(text, "utf-8")
        os.replace(tmp, path)
        return True
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        return False


def _read_json(path: Path) -> dict:
    try:
        d = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def run_meta_path(inst: dict, rid: str) -> Path:
    return runs_dir(inst) / f"{rid}.json"


def run_log_path(inst: dict, rid: str) -> Path:
    return runs_dir(inst) / f"{rid}.log"


def run_exit_path(inst: dict, rid: str) -> Path:
    return runs_dir(inst) / f"{rid}.exit"


def run_pid_path(inst: dict, rid: str) -> Path:
    return runs_dir(inst) / f"{rid}.pid"


def run_control_path(inst: dict, rid: str) -> Path:
    return runs_dir(inst) / f"{rid}.control"


def _read_text(path: Path) -> str:
    try:
        return path.read_text("utf-8", errors="replace").strip()
    except OSError:
        return ""


def _read_int(path: Path):
    t = _read_text(path)
    try:
        return int(t)
    except ValueError:
        return None


def _file_mtime(path: Path) -> int:
    try:
        return int(path.stat().st_mtime)
    except OSError:
        return 0


# ===== 启动 =====

def _build_script(inst: dict, rid: str, task_key: str) -> str:
    """拼 docker exec -d 里跑的 sh 脚本。

    task_key 已通过 _TASK_RE 校验；rid 由本模块生成，不含 shell 元字符。
    """
    crd = container_runs_dir(inst)
    r = f"{crd}/{rid}"
    ctl = f"{r}.control"
    parts = [
        f"cd {quote('/m7a')}",
        f"export MARCH7TH_CONTROL_FILE={quote(ctl)}",
        f"python main.py {task_key} > {quote(r + '.log')} 2>&1 &",
        "_rl_pid=$!",
        f"echo $_rl_pid > {quote(r + '.pid')}",
        "wait $_rl_pid",
        f"echo $? > {quote(r + '.exit')}",
    ]
    # 用换行而不是 ';' 连接：`cmd &` 后面紧跟 ';' 在 sh 里是语法错误
    return "\n".join(parts)


def active_run(inst: dict, scan: int = 5) -> dict | None:
    """返回当前仍在跑的那一次运行（没有则 None）——任务互斥锁的判定依据。

    为什么必须互斥：小助手本体没有 `active.json` 锁，两个 `python main.py`
    并发时 `module/game/cloud.py` 的 `close_all_m7a_browser()` 会按 cmdline
    匹配到**对方**的浏览器并 terminate，两边轮流关对方的浏览器、抢同一个
    CDP 端口（9222 → 冲突后回退 9224），最终 `tab crashed` 雪崩。10-31
    15:47 与 15:53 各起一次 currencywarsloop 就是这样双双失败的。

    只扫最近 `scan` 条账本：正在跑的一定是最新的几条，全量扫要逐条
    `docker exec` 探测 pid，在 100 条账本时会让「点启动」卡好几秒。
    """
    for r in list_runs(inst, limit=max(1, int(scan))):
        if str(r.get("status") or "") in ("running", "paused", "pausing", "queued"):
            return r
    return None


def start_run(inst: dict, task_key: str, task_label: str = "",
              after_finish: str = "", invoker: str = "panel") -> dict:
    """启动一次任务并建账本。

    返回 {"ok": True, "run_id": ...} 或 {"err": "..."}。
    """
    if not _TASK_RE.match(str(task_key or "")):
        return {"err": f"非法任务名：{task_key!r}"}
    _ensure_runs_dir(inst)
    busy = active_run(inst)
    if busy:
        label = busy.get("task_label") or busy.get("task_key") or "任务"
        return {"err": f"已有任务在运行（{label} · {busy.get('run_id')}），"
                       f"请等它结束或先停止它", "busy": busy}
    rid = new_run_id()
    started = int(time.time())
    meta = {
        "run_id": rid,
        "task_key": task_key,
        "task_label": task_label or task_key,
        "instance": instance_container(inst),
        "start_ts": started,
        "end_ts": 0,
        "status": "queued",
        "exit_code": None,
        "invoker": invoker,
        "schema": 1,
        "after_finish": after_finish or "",
        "argv": ["python", "main.py", task_key],
    }
    if not _atomic_write(run_meta_path(inst, rid), json.dumps(meta, ensure_ascii=False)):
        return {"err": f"写运行账本失败（检查 {runs_dir(inst)} 的写权限）"}

    env = ""
    if after_finish in ("None", "Exit"):
        env = f"-e MARCH7TH_AFTER_FINISH={after_finish} "
    script = _build_script(inst, rid, task_key)
    cmd = (f"docker exec -d {env}{quote(instance_container(inst))} "
           f"sh -c {quote(script)}")
    r = run_cmd(cmd)
    if r["code"] != 0:
        meta["status"] = "failed"
        meta["exit_code"] = r["code"]
        meta["end_ts"] = int(time.time())
        meta["error"] = (r.get("out") or "").strip()[:500]
        _atomic_write(run_meta_path(inst, rid), json.dumps(meta, ensure_ascii=False))
        return {"err": "任务启动失败：" + (r.get("out") or "").strip(), "run_id": rid}

    meta["status"] = "running"
    _atomic_write(run_meta_path(inst, rid), json.dumps(meta, ensure_ascii=False))
    prune_runs(inst)
    return {"ok": True, "run_id": rid, "start_ts": started}


# ===== 状态 =====

def _pid_alive(inst: dict, rid: str, pid: int) -> bool:
    key = f"{instance_container(inst)}|{rid}|{pid}"
    now = time.time()
    hit = _PID_CACHE.get(key)
    if hit and hit[0] > now:
        return hit[1]
    r = run_cmd(
        f"docker exec {quote(instance_container(inst))} "
        f"sh -c {quote(f'test -d /proc/{int(pid)} && echo alive')}"
    )
    alive = "alive" in (r.get("out") or "")
    _PID_CACHE[key] = (now + _PID_CACHE_TTL, alive)
    return alive


def control_state(inst: dict, rid: str) -> str:
    """读控制回执：running / pausing / paused / ''（无）。"""
    d = _read_json(Path(str(run_control_path(inst, rid)) + ".state"))
    return str(d.get("state") or "")


def run_status(inst: dict, rid: str, meta: dict | None = None) -> dict:
    """综合 .json / .exit / .pid / 日志 mtime 判定一次运行的真实状态。

    判定优先级：
    1. `.exit` 存在 → 终态（退出码即权威结论）
    2. 有 pid 且容器内进程还活着 → running
    3. 有 pid 但进程没了且无 `.exit` → 容器被重启/进程被强杀 → aborted
    4. 无 pid：刚启动不久 → running；超过 120 秒仍无 pid → failed
    """
    meta = meta if isinstance(meta, dict) and meta else _read_json(run_meta_path(inst, rid))
    start = int(meta.get("start_ts") or 0)
    now = int(time.time())
    if not meta or start <= 0:
        # 账本被 prune 掉 / 不是本面板启动的运行 → 交回调用方的 mtime 启发式，
        # 绝不能凭「没元数据」就判 failed（会污染历史成功率）。
        return {"run_id": rid, "task_key": "", "task_label": "", "start_ts": 0,
                "end_ts": 0, "status": "unknown", "exit_code": None,
                "exit_label": "", "pid": None, "duration": 0, "log_mtime": 0,
                "after_finish": ""}
    code = _read_int(run_exit_path(inst, rid))
    pid = _read_int(run_pid_path(inst, rid))
    mtime = _file_mtime(run_log_path(inst, rid))

    status = str(meta.get("status") or "queued")
    end = 0
    if code is not None:
        end = mtime if mtime > start else max(start, now)
        status = "success" if code == EXIT_OK else (
            "stopped" if code in _STOP_SIGNALS or code == EXIT_STOPPED else "failed")
    elif pid is not None:
        if _pid_alive(inst, rid, pid):
            status = "running"
            st = control_state(inst, rid)
            if st == "paused":
                status = "paused"
            elif st == "pausing":
                status = "pausing"
        elif mtime and (now - mtime) <= _FINISH_GRACE:
            # 进程刚退出、`.exit` 还没落盘的竞态窗口：日志还在长（或刚停），
            # 判 aborted 会把成功的任务误报成中断。这里先当 running，
            # 下一轮轮询时 `.exit` 就位即可拿到真实退出码。
            status = "running"
        else:
            status = "aborted"
            end = mtime if mtime > start else max(start, now)
    else:
        # pid 还没落盘（启动瞬间）或根本没起来
        if now - start > 120:
            status = "failed"
            end = now
        else:
            status = "running"

    dur = (end if end > 0 else now) - start if start > 0 else 0
    return {
        "run_id": rid,
        "task_key": str(meta.get("task_key") or ""),
        "task_label": str(meta.get("task_label") or ""),
        "start_ts": start,
        "end_ts": end,
        "status": status,
        "exit_code": code,
        "exit_label": exit_label(code),
        "pid": pid,
        "duration": max(0, dur),
        "log_mtime": mtime,
        "after_finish": str(meta.get("after_finish") or ""),
    }


def list_runs(inst: dict, limit: int = 30) -> list[dict]:
    """按开始时间倒序返回最近若干次运行。"""
    d = runs_dir(inst)
    if not d.is_dir():
        return []
    out = []
    try:
        names = [p.name for p in d.glob("*.json") if RUN_ID_RE.match(p.stem)]
    except OSError:
        return []
    for name in sorted(names, reverse=True)[:max(1, min(int(limit or 30), 200))]:
        rid = name[:-5]
        try:
            out.append(run_status(inst, rid))
        except Exception:  # 单条坏账本不影响整表
            continue
    return out


def latest_run(inst: dict) -> dict | None:
    d = runs_dir(inst)
    if not d.is_dir():
        return None
    try:
        names = [p.name for p in d.glob("*.json") if RUN_ID_RE.match(p.stem)]
    except OSError:
        return None
    if not names:
        return None
    return run_status(inst, sorted(names)[-1][:-5])


# ===== 停止 / 暂停 =====

def stop_run(inst: dict, rid: str) -> dict:
    """精确停止一次运行：先请求暂停（让键鼠立刻停手）再 SIGTERM 目标 pid。"""
    if not valid_run_id(rid):
        return {"err": "运行 ID 非法"}
    st = run_status(inst, rid)
    if st["status"] not in ("running", "paused", "pausing"):
        return {"err": f"该运行已结束（{st['status']}），无需停止"}
    pid = st.get("pid")
    # 先写 pause 指令，避免 SIGTERM 到达前还在猛点游戏
    request_control(inst, rid, "pause")
    time.sleep(0.2)
    if not pid:
        return {"err": "拿不到该运行的进程号，无法精确停止（可改用「重启容器」）"}
    r = run_cmd(
        f"docker exec {quote(instance_container(inst))} "
        f"sh -c {quote(f'kill -15 {int(pid)} 2>/dev/null; echo ok')}"
    )
    if r["code"] != 0:
        return {"err": "停止失败：" + (r.get("out") or "").strip()}
    _PID_CACHE.clear()
    return {"ok": True, "msg": f"已向运行 {rid}（pid {pid}）发送停止信号"}


def request_control(inst: dict, rid: str, command: str) -> dict:
    """写控制文件：pause / resume（旧镜像支持，stop 不支持）。"""
    if not valid_run_id(rid):
        return {"err": "运行 ID 非法"}
    if command not in ("pause", "resume"):
        return {"err": "只支持 pause / resume"}
    _ensure_runs_dir(inst)
    p = run_control_path(inst, rid)
    if not _atomic_write(p, json.dumps({"command": command})):
        return {"err": "写控制文件失败（检查 logs/runs 权限）"}
    return {"ok": True, "msg": ("已请求暂停" if command == "pause" else "已请求恢复")}


# ===== 日志 =====

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*\x07")


def clean_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text or "")


def read_log(inst: dict, rid: str, lines: int = 300) -> dict:
    """读某次运行的输出尾部（容器内 tail，避免整文件搬进内存）。"""
    if not valid_run_id(rid):
        return {"lines": "", "total": 0}
    n = max(20, min(int(lines or 300), 2000))
    path = run_log_path(inst, rid)
    if not path.is_file():
        return {"lines": "", "total": 0}
    r = run_cmd(f"tail -n {n} {quote(str(path))}")
    out = clean_ansi(r.get("out") or "")
    return {"lines": out, "total": len(out.splitlines())}


# ===== 清理 =====

def prune_runs(inst: dict, keep: int = KEEP_RUNS) -> int:
    """按 .json 倒序保留最近 keep 套账本，其余整套删除。返回删除套数。"""
    d = runs_dir(inst)
    if not d.is_dir():
        return 0
    try:
        stems = sorted([p.stem for p in d.glob("*.json") if RUN_ID_RE.match(p.stem)],
                       reverse=True)
    except OSError:
        return 0
    removed = 0
    for stem in stems[max(1, int(keep)):]:
        for suf in (".json", ".log", ".pid", ".exit", ".control", ".control.state"):
            try:
                (d / (stem + suf)).unlink()
            except OSError:
                pass
        removed += 1
    return removed


__all__ = [
    "RUN_ID_RE", "KEEP_RUNS", "EXIT_LABELS", "EXIT_OK", "EXIT_FAILED", "EXIT_STOPPED",
    "runs_dir", "container_runs_dir", "new_run_id", "valid_run_id",
    "run_meta_path", "run_log_path", "run_exit_path", "run_pid_path", "run_control_path",
    "start_run", "run_status", "list_runs", "latest_run",
    "stop_run", "request_control", "control_state",
    "read_log", "clean_ansi", "prune_runs", "exit_label",
]
