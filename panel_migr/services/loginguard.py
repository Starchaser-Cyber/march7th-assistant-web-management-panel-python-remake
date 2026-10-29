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

# v1.24.3 安全加固（M7A-03）：全局限速兜底。
# 单 IP 计数可被伪造 X-Forwarded-For 摊薄，故再加一层不分来源的总额度。
#
# v1.24.4 修复（P1-10 / 审计 B-02）：旧实现是自伤式 DoS —— 全局桶不看来源、成功
# 登录不清零，而 h_login 在**校验密码之前**就用它拒绝请求，于是锁定期内成功路径
# 永远走不到、清零永远不可能发生：任何人 20 次/分钟错密码即可让所有管理员无限期
# 无法登录。现在：
#   1) 全局桶是滑动窗口内的 (失败时间戳, 来源 IP)，超窗淘汰；仍计入同 IP 反复失败
#      （保留 M7A-03「伪造 IP 摊薄无效」的原始意图）；
#   2) 任一来源登录成功 → ok() 把全局桶**整体清零**（不再只清自己的单 IP 桶）；
#   3) h_login 不再「先拒绝再校验」：全局风暴期间仍然校验密码，密码正确即放行并
#      清零（合法管理员永远不会被攻击者锁在门外）；密码错误才回限速文案；
#   4) 新增 global_sources()：窗口内不同来源数。只有「不同来源数 ≥ GLOBAL_SOURCES_MIN」
#      才判定为分布式爆破并显示全局限速文案 —— 单人反复失败（来源数≈1）不会把
#      所有人拖进全局限速叙事，避免误导与噪声。
#
# 权衡说明：全局桶**不再拒绝**请求（拒绝必然重新引入 DoS），因此它现在只承担
# 「检测 + 文案」职责，不再提供对分布式爆破的硬限流；硬防护由单 IP 锁定
# （5 次/5 分钟）+ bcrypt 成本 + 反代层限流承担。这是 P1-10 要求下的必然取舍：
# 「全局硬锁」与「合法管理员永不被锁」不可兼得。
GLOBAL_WINDOW = 60.0
GLOBAL_MAX = 20
GLOBAL_SOURCES_MIN = 8      # 判定为分布式爆破所需的最少不同来源数
_gfail: list[tuple[float, str]] = []

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


def global_locked() -> int:
    """全局限速剩余秒数（0=未触发）。不分来源，防伪造 IP 摊薄。"""
    now = _now()
    _gfail[:] = [it for it in _gfail if now - it[0] <= GLOBAL_WINDOW]
    if len(_gfail) >= GLOBAL_MAX:
        return int(GLOBAL_WINDOW - (now - _gfail[0][0])) + 1
    return 0


def global_sources() -> int:
    """窗口内触发全局失败的**不同来源**数（v1.24.4：观测/判定用）。

    单人反复失败时该值≈1；分布式爆破才会上涨。h_login 只用它做日志级判断，
    不据此拒绝登录（拒绝会重新引入 DoS）。
    """
    now = _now()
    _gfail[:] = [it for it in _gfail if now - it[0] <= GLOBAL_WINDOW]
    return len({src for _, src in _gfail})


def global_fail(ip: str = "-") -> None:
    """记录一次全局失败（与 fail() 并行使用）。ip 仅用于来源统计。"""
    _gfail.append((_now(), str(ip or "-")))
    if len(_gfail) > GLOBAL_MAX * 4:          # 有界，防无限增长
        del _gfail[:-GLOBAL_MAX]


def ok(ip: str) -> None:
    """登录成功：清该来源的单桶，并**整体清零**全局失败桶（v1.24.4 / P1-10）。"""
    _state.pop(ip, None)
    _gfail.clear()


def reset() -> None:
    """测试辅助：清空全部状态。"""
    _state.clear()
    _gfail.clear()
