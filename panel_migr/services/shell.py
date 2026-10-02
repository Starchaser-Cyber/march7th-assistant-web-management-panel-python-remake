"""shell 命令执行 —— 等价 PHP run_cmd()（exec($cmd.' 2>&1')）。"""
from __future__ import annotations

import subprocess


def run_cmd(cmd: str, timeout: int = 120) -> dict:
    """执行命令，返回 {'code': int, 'out': str}；stderr 合并进 out，无尾换行（对齐 PHP exec）。"""
    try:
        p = subprocess.run(
            cmd + " 2>&1", shell=True,
            capture_output=True, text=True,
            timeout=timeout, errors="replace",
        )
        return {"code": p.returncode, "out": (p.stdout or "").rstrip("\n")}
    except subprocess.TimeoutExpired as e:
        out = e.stdout or ""
        if isinstance(out, bytes):
            out = out.decode("utf-8", "replace")
        return {"code": 124, "out": (out or "") + "\n[timeout]"}
