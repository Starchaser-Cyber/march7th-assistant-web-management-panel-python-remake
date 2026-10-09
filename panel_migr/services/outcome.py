"""任务收益解析（v1.23 M1-1）——把「今天跑了什么、跑出什么」变成可读数据。

思路：任务结束判定发生在 history_sync 里，那一刻日志已完整落盘。这里只做两件事：
1. 从日志里按正则提取可量化字段（开拓力、副本次数、实训得分、玩法完成情况）；
2. 结果写进 SQLite events(type="outcome")，供「收益日报」按天聚合。

设计原则：
- 纯只读，任何一步失败都返回空结构，绝不影响任务主流程与历史记录；
- 正则全部容错，日志格式变化只会少统计几项，不会抛异常；
- 解析函数（parse_log）不依赖文件系统，便于单测。
"""
from __future__ import annotations

import re
import time
from datetime import datetime

from services.logs import latest_log_path
from services.shell import run_cmd

# 每次读取的日志尾部行数（任务日志单日约 2 万行，足够覆盖一次任务窗口）
TAIL_LINES = 20000
# 日报保留天数上限
MAX_DAYS = 30

_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
# `2026-10-03 08:02:28,403 | INFO | 开拓力: 100/300`
_PREFIX_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}[,.]?\d*\s*\|\s*[A-Z]+\s*\|\s?")

_STAMINA_RE = re.compile(r"开拓力[:：]\s*(\d+)\s*/\s*(\d+)")
_CHALLENGE_RE = re.compile(r"开拓力[:：]\s*\d+\s*=\s*(\d+)\s*次挑战")
_DUNGEON_RE = re.compile(r"第\s*(\d+)\s*次副本完成")
_SCORE_RE = re.compile(r"\+\s*(\d+)\s*分")
_DAILY_RE = re.compile(r"^(.{2,30}?)[:：]\s*(已完成|待完成)(?:\s|$)")
_MODE_RE = re.compile(
    r"^[「\"]?(货币战争|差分宇宙|模拟宇宙|混沌回忆|虚构叙事|末日幻影|历战余响|忘却之庭|寰宇蝗灾|黄金与机械)[」\"]?\s*已完成")
_CLAIM_RE = re.compile(r"^领取.{0,20}完成$")

# 「每日实训」条目名里不该出现的关键词（防止把系统提示误当实训项）
_DAILY_DENY = ("通知", "日志", "浏览器", "smtp", "OCR", "识别", "截图", "资源")


def _msg(line: str) -> str:
    """剥掉 `时间 | 级别 | ` 前缀，返回纯消息体。"""
    return _PREFIX_RE.sub("", line or "").strip()


def _line_ts(line: str):
    m = _TS_RE.match(line or "")
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").timestamp()
    except ValueError:
        return None


def parse_log(text: str) -> dict:
    """从日志文本里提取收益字段。纯函数，输入为空返回全零结构。"""
    out = {
        "stamina": None, "stamina_total": None, "challenges": 0,
        "dungeon": 0, "score": 0, "daily_done": 0, "daily_todo": 0,
        "modes": [], "notes": [], "lines": 0,
    }
    daily: dict = {}
    modes: list = []
    notes: list = []
    for line in (text or "").splitlines():
        if not line.strip():
            continue
        out["lines"] += 1
        m = _STAMINA_RE.search(line)
        if m:
            out["stamina"] = int(m.group(1))
            out["stamina_total"] = int(m.group(2))
        m = _CHALLENGE_RE.search(line)
        if m:
            out["challenges"] = max(out["challenges"], int(m.group(1)))
        m = _DUNGEON_RE.search(line)
        if m:
            out["dungeon"] = max(out["dungeon"], int(m.group(1)))
        m = _SCORE_RE.search(line)
        if m:
            out["score"] += int(m.group(1))
        body = _msg(line)
        m = _DAILY_RE.match(body)
        if m:
            label = m.group(1).strip()
            if not any(d in label for d in _DAILY_DENY):
                daily[label] = m.group(2)
        m = _MODE_RE.match(body)
        if m and m.group(1) not in modes:
            modes.append(m.group(1))
        if _CLAIM_RE.match(body) and body not in notes:
            notes.append(body)
    out["daily_done"] = sum(1 for v in daily.values() if v == "已完成")
    out["daily_todo"] = sum(1 for v in daily.values() if v == "待完成")
    out["modes"] = modes[:6]
    out["notes"] = notes[:6]
    return out


def read_window(path: str, start_ts: int, end_ts: int,
                tail: int = TAIL_LINES) -> str:
    """读日志尾部并按 [start_ts, end_ts] 时间窗裁剪（行首时间戳判定）。

    时间戳缺失或解析失败的行：窗口起点为 0 时保留，否则丢弃（避免把昨天的
    内容算进今天）。"""
    try:
        import shlex

        r = run_cmd(f"tail -n {int(tail)} {shlex.quote(str(path))}")
    except Exception:
        return ""
    raw = r.get("out") or ""
    if not raw:
        return ""
    start = int(start_ts or 0)
    end = int(end_ts or 0)
    keep = []
    for line in raw.split("\n"):
        ts = _line_ts(line)
        if ts is None:
            if start <= 0:
                keep.append(line)
            continue
        if start > 0 and ts < start:
            continue
        if end > 0 and ts > end + 5:
            continue
        keep.append(line)
    return "\n".join(keep)


def extract_for_task(inst: dict, item: dict) -> dict:
    """取某条任务历史对应的收益（读日志 + 解析）。失败返回 {}。"""
    try:
        path = latest_log_path(inst)
        if not path:
            return {}
        text = read_window(path, int(item.get("start_ts") or 0),
                           int(item.get("end_ts") or 0))
        if not text.strip():
            return {}
        oc = parse_log(text)
        if not any([oc["dungeon"], oc["score"], oc["daily_done"], oc["daily_todo"],
                    oc["modes"], oc["stamina"] is not None]):
            return {}
        return oc
    except Exception:
        return {}


def _day_start(ts: float | None = None) -> int:
    d = datetime.fromtimestamp(float(ts if ts is not None else time.time()))
    return int(d.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())


_WD = ["日", "一", "二", "三", "四", "五", "六"]


def outcome_daily(inst: dict, days: int = 7, now: int | None = None) -> dict:
    """近 N 天收益汇总（含今日卡）。数据源：events(type="outcome")。"""
    from services import eventdb

    try:
        days = int(days) if days is not None else 7
    except (TypeError, ValueError):
        days = 7
    days = max(1, min(days, MAX_DAYS))
    now = int(now if now is not None else time.time())
    sid = str(inst.get("id") or inst.get("container") or "")
    today0 = _day_start(now)
    frm = today0 - (days - 1) * 86400

    buckets: dict = {}
    for i in range(days - 1, -1, -1):
        day = datetime.fromtimestamp(today0 - i * 86400)
        key = day.strftime("%Y-%m-%d")
        buckets[key] = {
            "date": day.strftime("%m-%d"), "key": key, "wd": _WD[day.isoweekday() % 7],
            "today": i == 0, "tasks": 0, "score": 0, "dungeon": 0,
            "modes": [], "notes": [], "stamina": None, "stamina_total": None,
        }

    # 事件按 seq 倒序返回；反过来遍历，让「当天最新一条」覆盖旧值（开拓力等快照类字段）
    rows = eventdb.list_events("outcome", 20000, frm=frm, to=now)
    for e in reversed(rows):
        if str(e.get("inst") or "") != sid:
            continue
        key = datetime.fromtimestamp(int(e.get("ts") or 0)).strftime("%Y-%m-%d")
        b = buckets.get(key)
        if b is None:
            continue
        oc = e.get("outcome") or {}
        if not isinstance(oc, dict):
            continue
        b["tasks"] += 1
        b["score"] += int(oc.get("score") or 0)
        b["dungeon"] += int(oc.get("dungeon") or 0)
        if oc.get("stamina") is not None:
            b["stamina"] = int(oc.get("stamina"))
            b["stamina_total"] = int(oc.get("stamina_total") or 0)
        for m in (oc.get("modes") or []):
            if m not in b["modes"]:
                b["modes"].append(m)
        for n in (oc.get("notes") or []):
            if n not in b["notes"]:
                b["notes"].append(n)

    day_list = list(buckets.values())
    today = day_list[-1] if day_list else None
    return {
        "range_days": days,
        "today": today,
        "list": day_list,
        "total_tasks": sum(d["tasks"] for d in day_list),
        "has_data": bool(today and today["tasks"] > 0),
        "week": {
            "tasks": sum(d["tasks"] for d in day_list),
            "score": sum(d["score"] for d in day_list),
            "dungeon": sum(d["dungeon"] for d in day_list),
        },
    }
