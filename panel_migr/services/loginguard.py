"""登录失败限速（M5 安全加固）。

窗口内连续失败达阈值 → 临时锁定该来源；登录成功即清零。
纯内存实现（面板单机场景），进程重启自动复位；时间源可替换便于测试。
"""
from __future__ import annotations

import time

WINDOW = 300.0    # 失败统计窗口（秒）
MAX_FAIL = 5      # 窗口内允许的失败次数
LOCK = 300.0      # 达到阈值后的锁定时长（秒）
_CAP = 4096       # 来源数上限（防异常灌入）

# ip -> (窗口起点, 窗口内失败次数, 锁定截止时间戳)
_state: dict[str, tuple[float, int, float]] = {}


def _now() -> float:
    return time.time()


def _sweep(now: float) -> None:
    """容量保护：优先丢已过期条目，仍超限按窗口起点丢最旧。"""
    if len(_state) <= _CAP:
        return
    for ip in [k for k, (ws, n, locked) in _state.items()
               if now >= locked and (n == 0 or now - ws > WINDOW)]:
        _state.pop(ip, None)
    if len(_state) > _CAP:
        for ip in sorted(_state, key=lambda k: _state[k][0])[: len(_state) - _CAP]:
            _state.pop(ip, None)


def check(ip: str) -> bool:
    """True=允许尝试；False=处于锁定窗口。"""
    now = _now()
    st = _state.get(ip)
    if st is None:
        return True
    ws, _fails, locked = st
    if now < locked:
        return False
    if now - ws > WINDOW:
        _state.pop(ip, None)       # 窗口过期，历史清零
    return True


def remaining(ip: str) -> int:
    """剩余锁定秒数（0=未锁定）。"""
    st = _state.get(ip)
    if st is None:
        return 0
    left = st[2] - _now()
    return int(left) + 1 if left > 0 else 0


def fail(ip: str) -> None:
    now = _now()
    ws, fails, _locked = _state.get(ip, (now, 0, 0.0))
    if now - ws > WINDOW:
        ws, fails = now, 0
    fails += 1
    locked = now + LOCK if fails >= MAX_FAIL else 0.0
    _state[ip] = (ws, fails, locked)
    _sweep(now)


def ok(ip: str) -> None:
    _state.pop(ip, None)


def reset() -> None:
    """测试辅助：清空全部状态。"""
    _state.clear()
