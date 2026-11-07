"""日志文件枚举与过滤 —— 等价 PHP log_files() / latest_log_path() / filter_log()。

v1.24.4 修复：
- `log_files()` 排序改用 `os.stat` 并吞掉 `OSError`：`glob` 与取 mtime 之间存在
  TOCTOU 窗口（任务结束清理 / 日志轮转），原先 `os.path.getmtime` 直接抛
  FileNotFoundError → 页面 500、alert_tick 整轮异常、schedule_run_due 抛出后
  `schedule_save` 永不执行（last_run 丢失 → 下一分钟重复触发）。
- 新增时钟安全的节流辅助 `elapsed_ok()` / `before_deadline()`：全项目
  「now - last < iv」型节流改用它，时钟回拨（last 落在未来）时判为「已到期」
  而不是恒真，避免采样/告警/巡检永久停摆。放在本模块是因为它只依赖标准库，
  被 alerts / monitor / eventdb / history 共同引用不会引入循环导入。
"""
from __future__ import annotations

import glob
import os
import re
import shlex
import time
from datetime import datetime

from services.shell import run_cmd

# 「截止时间」类时间戳的可信上限（秒）：比 now 超前超过该值判为时钟回拨遗留的
# 脏数据并忽略。项目内最大的正常截止值是 alert_quiet 的 600 秒。
MAX_FUTURE_SKEW = 7200


def elapsed_ok(now, last, interval) -> bool:
    """距上次执行是否已满 interval 秒（True = 该执行了）。

    时钟回拨保护：`last` 落在未来时 `now - last` 为负，原写法 `now - last < iv`
    恒真 → 永不执行。这里把负差值视为「已到期」，由调用方把 last 重置为 now。
    `last` 为 0/缺失/不可解析一律视为已到期（首次执行）。
    """
    try:
        last_i = int(last or 0)
        iv_i = int(interval or 0)
        now_i = int(now)
    except (TypeError, ValueError):
        return True
    if last_i <= 0:
        return True
    delta = now_i - last_i
    if delta < 0:
        # last 落在未来。两种情况要分开：
        #   1) 时钟回拨：偏差在 MAX_FUTURE_SKEW 内 → 视为已到期，调用方会把
        #      last 重置为 now，从而恢复执行（P1-20 的修复点）。
        #   2) 脏数据/损坏文件：偏差远超合理范围（如 lastSample=9999999999），
        #      沿用原有「节流命中、不采样」的保护，避免被一条坏记录带偏。
        return -delta <= MAX_FUTURE_SKEW
    return delta >= iv_i


def before_deadline(now, until) -> bool:
    """now 是否仍早于截止时间 `until`（True = 还在等待期内）。

    用于 quiet_until / retry_at / push_backoff_until 这类绝对时间戳。
    时钟回拨保护：`until` 比 now 超前超过 MAX_FUTURE_SKEW 时判为脏数据，
    返回 False（视为已过期），避免重试队列/冷静期永久卡死。
    """
    try:
        u = int(until or 0)
        now_i = int(now)
    except (TypeError, ValueError):
        return False
    if u <= 0:
        return False
    delta = u - now_i
    if delta <= 0:
        return False
    return delta <= MAX_FUTURE_SKEW


def log_files(inst: dict) -> list[str]:
    """按 mtime 倒序列出 logs/*.log；stat 失败（已轮转/被删）的文件跳过。"""
    files = glob.glob(os.path.join(inst["dir"], "logs", "*.log"))
    if not files:
        return []
    stamped: list[tuple[float, str]] = []
    for f in files:
        try:
            st = os.stat(f)             # 一次 stat 拿 mtime，缩小 TOCTOU 窗口
        except OSError:
            continue                    # glob 之后被删除/改名 → 跳过，不让调用方炸
        stamped.append((st.st_mtime, f))
    stamped.sort(key=lambda x: x[0], reverse=True)
    return [f for _, f in stamped]


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


# ===== v1.28：日志错误聚合 =====
# 背景：日志页只能逐行翻，171k 行里哪类错误占大头、有没有在持续刷，肉眼看不出来。
# 2026-10-31 那次事故就是「同一类错误刷了 11 小时」——行数本身是信号，但界面没给。

# 错误行判定：m7a 本体的日志级别用 | LEVEL | 包裹（与 filter_log 的 level_re 一致）。
_ERROR_LEVEL_RE = re.compile(r"\|\s*(ERROR|CRITICAL|FATAL)\s*\|", re.I)

# 分类规则（有序，先命中先归类）。key 是稳定标识，label 给人看，
# kw 是「点这个分类要往关键词框里填什么」—— 必须是日志里真实出现的串，
# 否则填进去命中 0 条（v1.28 首版就踩了这个坑：填显示名「截图 / 识别失败」）。
_ERROR_RULES: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    ("screenshot", "截图 / 识别失败", "截图", ("截图", "识别", "ocr", "OCR")),
    ("timeout", "超时", "超时", ("超时", "timed out", "timeout")),
    ("network", "网络 / 端口", "端口", ("端口", "连接", "Errno", "Connection", "websocket", "HTTP")),
    ("session", "登录 / 会话", "登录", ("登录", "会话", "session", "cookie")),
    ("browser", "浏览器 / 驱动", "浏览器", ("chrome", "Chrome", "浏览器", "webdriver", "session id")),
    ("config", "配置 / 文件", "配置", ("配置", "yaml", "config", "文件", "路径")),
)
_ERROR_OTHER = ("other", "其他错误", "")


def classify_error(line: str) -> str:
    """把一条错误行归到某个类别 key（未命中任何规则 → "other"）。"""
    for key, _label, _kw, needles in _ERROR_RULES:
        for nd in needles:
            if nd in line:
                return key
    return _ERROR_OTHER[0]


def error_summary(lines: list[str]) -> dict:
    """统计错误行：总数、按类别分布、首末时间、最集中的类别。

    纯函数（不读文件、不碰时间），输入就是日志页当前展示的那些行 ——
    用户在页面上筛了什么，聚合就跟着算什么，两边口径必然一致。
    每个 bucket 带 `kw`（可点击过滤的关键词，空 = 不可点击）。
    """
    total = len(lines)
    buckets: dict[str, int] = {}
    first_ts = None
    last_ts = None
    errors = 0
    for line in lines:
        if not _ERROR_LEVEL_RE.search(line):
            continue
        errors += 1
        key = classify_error(line)
        buckets[key] = buckets.get(key, 0) + 1
        ts = _parse_line_ts(line[:19])
        if ts is not None:
            if first_ts is None or ts < first_ts:
                first_ts = ts
            if last_ts is None or ts > last_ts:
                last_ts = ts

    meta = {k: (lb, kw) for k, lb, kw, _ in _ERROR_RULES}
    meta[_ERROR_OTHER[0]] = (_ERROR_OTHER[1], _ERROR_OTHER[2])
    ordered = sorted(buckets.items(), key=lambda kv: (-kv[1], kv[0]))

    def _bucket(k, n):
        lb, kw = meta.get(k, (k, ""))
        return {"key": k, "label": lb, "kw": kw, "count": n}

    return {
        "total": total,
        "errors": errors,
        "rate": round(errors * 100.0 / total, 1) if total else 0.0,
        "buckets": [_bucket(k, n) for k, n in ordered],
        "top": (_bucket(ordered[0][0], ordered[0][1]) if ordered else None),
        "first_ts": int(first_ts) if first_ts is not None else 0,
        "last_ts": int(last_ts) if last_ts is not None else 0,
    }
