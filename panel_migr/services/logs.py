"""日志文件枚举与过滤 —— 等价 PHP log_files() / latest_log_path() / filter_log()。"""
from __future__ import annotations

import glob
import os
import re
import shlex
import time
from datetime import datetime

from services.shell import run_cmd


def log_files(inst: dict) -> list[str]:
    files = glob.glob(os.path.join(inst["dir"], "logs", "*.log"))
    if not files:
        return []
    files.sort(key=lambda f: os.path.getmtime(f), reverse=True)
    return files


def latest_log_path(inst: dict):
    files = log_files(inst)
    return files[0] if files else None


def _parse_line_ts(prefix: str):
    """行首 19 字符 → epoch；解析失败返回 None（PHP strtotime false 分支：不过滤）。"""
    try:
        return datetime.strptime(prefix, "%Y-%m-%d %H:%M:%S").timestamp()
    except ValueError:
        return None


def filter_log(path: str, opts: dict) -> dict:
    """tail 最后 N 行 + 关键词/级别/时间窗过滤（与 PHP filter_log 逐条等价）。"""
    lines_n = int(opts.get("lines", 2000) or 2000)
    lines_n = max(100, min(5000, lines_n))
    r = run_cmd(f"tail -n {lines_n} {shlex.quote(path)}")
    raw = r["out"].split("\n") if r["out"] else []

    kw = str(opts.get("keyword") or "").strip()
    level = str(opts.get("level") or "").strip().upper()
    hours = int(opts.get("hours", 0) or 0)
    cutoff = time.time() - hours * 3600 if hours > 0 else None

    level_re = None
    if level and level != "ALL":
        level_re = re.compile(r"\|\s*" + re.escape(level) + r"\s*\|", re.I)

    out = []
    for line in raw:
        if kw and kw.lower() not in line.lower():
            continue
        if level_re is not None and not level_re.search(line):
            continue
        if cutoff is not None:
            ts = _parse_line_ts(line[:19])
            if ts is not None and ts < cutoff:
                continue
        out.append(line)
    return {"lines": out, "total": len(out)}
