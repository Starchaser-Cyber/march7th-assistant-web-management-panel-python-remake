"""V1.28.1 修复回归：审计报告 V28-01 / V28-04 / V28-05 / V28-09。

对照 2026-11-10 全方位审计报告的每一条 P1/P2 缺陷，这里给出**可复现的回归断言**：

- V28-01：会话 TTL 误删活跃会话 —— `load_session()` 读成功后必须刷新 mtime，
  连发多次普通请求后 `session_gc()` 不得删除该会话（旧实现 9 次请求零刷新）。
- V28-04：日志聚合条 chip 下钻数量与标注不一致 —— 按 `category` 过滤的行数
  必须等于 `error_summary()` 给该类的计数（旧实现 5 条 → 只剩 1 条）。
- V28-05：告警健康看板失败原因过期 + 同秒成功/失败误判 —— 重试失败要回填
  msg/kind；`last_ok == last_fail` 必须判成失败侧而不是「通道正常」。
- V28-09：错误分类规则顺序导致「界面识别失败」类被吞 —— 含「识别」的行必须
  归到 ui，而不是被 screenshot 先抢走。

另外覆盖 V28-02（dev 依赖声明）与 V28-03（python-multipart 下限）的静态约束。
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

import config as cfg
import phpsess
from services import alerts, logs

_REPO = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _isolate_session_dir(monkeypatch, tmp_path):
    """会话目录按用例隔离（跟 test_v128_health_gc_summary.py 口径一致）。"""
    sess_dir = tmp_path / "sessions"
    sess_dir.mkdir()
    monkeypatch.setattr(cfg, "SESSION_SAVE_PATH", str(sess_dir))
    monkeypatch.setattr(phpsess, "SESSION_SAVE_PATH", str(sess_dir))
    return sess_dir


class _FakeRequest:
    """只带 cookies 的最小 request 替身（load_session 只用到它）。"""

    def __init__(self, sid: str):
        self.cookies = {"PHPSESSID": sid} if sid else {}


def _write_session(sess_dir: Path, sid: str, payload: dict | None = None) -> Path:
    data = payload if payload is not None else {phpsess.SKEY: "1"}
    text = "".join(f"{k}|{phpsess.php_serialize(v)}" for k, v in data.items())
    p = sess_dir / f"sess_{sid}"
    p.write_text(text, encoding="utf-8")
    return p


# ===== V28-01：活跃会话必须续期 =====

def test_load_session_refreshes_mtime_on_every_request(_isolate_session_dir):
    """审计原测法：连发 9 次普通请求，旧实现 mtime 一次都不动 → GC 误删。

    修复后每次 `load_session()` 都 utime，因此 mtime 必然前进。
    """
    sid = "a" * 32
    path = _write_session(_isolate_session_dir, sid)
    # 把 mtime 推到 TTL 之外，模拟「很久没写盘但一直在用」
    old = time.time() - 4000
    os.utime(path, (old, old))
    assert phpsess.session_age(path) > 3600

    seen = []
    for _ in range(9):
        phpsess.load_session(_FakeRequest(sid))
        seen.append(path.stat().st_mtime)
        time.sleep(0.01)          # 让 utime 的秒级/纳秒级差异可观测

    assert path.stat().st_mtime > old, "load_session() 没有刷新会话 mtime"
    assert max(seen) > old


def test_active_session_survives_gc(_isolate_session_dir):
    """审计原测法：9 次活跃请求后 session_gc() 不得删掉这个会话。"""
    sid = "b" * 32
    path = _write_session(_isolate_session_dir, sid)
    old = time.time() - 3605
    os.utime(path, (old, old))

    for _ in range(9):
        phpsess.load_session(_FakeRequest(sid))

    out = phpsess.session_gc(ttl=3600)
    assert out["removed"] == 0, f"活跃会话被 GC 误删: {out}"
    assert path.exists()


def test_idle_session_still_expires(_isolate_session_dir):
    """反向保障：真正闲置（没人读）的会话仍要被 GC 删掉，别把 TTL 改成永不过期。"""
    sid = "c" * 32
    path = _write_session(_isolate_session_dir, sid)
    old = time.time() - 7200
    os.utime(path, (old, old))

    out = phpsess.session_gc(ttl=3600)
    assert out["removed"] == 1
    assert not path.exists()


def test_load_session_does_not_follow_symlink(_isolate_session_dir, tmp_path):
    """续期不得跟随符号链接（P1-12 安全口径保持）。"""
    target = tmp_path / "outside.txt"
    target.write_text(f"{phpsess.SKEY}|s:1:\"1\";", encoding="utf-8")
    os.utime(target, (time.time() - 5000, time.time() - 5000))
    link = _isolate_session_dir / f"sess_{'d' * 32}"
    link.symlink_to(target)

    phpsess.load_session(_FakeRequest("d" * 32))
    # 目标文件 mtime 不应被改动
    assert time.time() - target.stat().st_mtime > 4000


def test_load_session_without_cookie_returns_empty(_isolate_session_dir):
    assert phpsess.load_session(_FakeRequest("")) == {}


# ===== V28-09：分类规则顺序 =====

def test_ui_rule_wins_over_screenshot_for_jiexie():
    """审计实测：」界面识别失败，未找到目标」旧实现归到 screenshot，应为 ui。"""
    assert logs.classify_error("2026-11-10 09:33:13 | ERROR | 界面识别失败，未找到目标") == "ui"
    # 真实线上行（全天 1850 条那类）同样必须是 ui
    assert logs.classify_error(
        "2026-11-10 09:33:13,852 | ERROR | 未识别出任何界面，请确保游戏画面干净，按ESC后重试"
    ) == "ui"
    assert logs.classify_error("2026-11-10 09:35:58 | ERROR | 当前界面：未知") == "ui"
    # 真正的截图类仍归 screenshot
    assert logs.classify_error("2026-11-10 08:32:54 | ERROR | 截图失败：没有找到游戏窗口") == "screenshot"
    assert logs.classify_error("2026-11-10 08:33:54 | ERROR | 发生错误 截图超时") == "screenshot"


def test_rule_order_invariants():
    """规则表的 key 集合必须与白名单一致，否则 filter_log 会静默忽略该类别。"""
    keys = {k for k, _l, _kw, _n in logs._ERROR_RULES} | {logs._ERROR_OTHER[0]}
    assert keys == set(logs._ERROR_CATEGORY_KEYS)


# ===== V28-04：chip 下钻行数必须与标注一致 =====

_NETWORK_LINES = [
    "2026-11-10 10:00:01 | ERROR | 端口 8787 被占用",
    "2026-11-10 10:00:02 | ERROR | 连接被拒绝",
    "2026-11-10 10:00:03 | ERROR | Errno 111 拒绝",
    "2026-11-10 10:00:04 | ERROR | websocket 已断开",
    "2026-11-10 10:00:05 | ERROR | HTTP 502 上游错误",
]


def test_category_filter_matches_summary_count():
    """审计原测法：5 条 network 行，聚合条标 5 条。

    旧实现点 chip 只填 kw='端口' → 只剩 1 条。修复后按 category 过滤必须仍是 5 条。
    """
    summary = logs.error_summary(_NETWORK_LINES)
    by_key = {b["key"]: b for b in summary["buckets"]}
    assert by_key["network"]["count"] == 5

    hit = [ln for ln in _NETWORK_LINES if logs.category_matches(ln, "network")]
    assert len(hit) == by_key["network"]["count"] == 5


def test_category_filter_consistent_for_all_categories():
    """对每个类别：category_matches 的命中数都必须等于 error_summary 的计数。"""
    sample = [
        "2026-11-10 10:00:01 | ERROR | 端口 8787 被占用",
        "2026-11-10 10:00:02 | ERROR | 连接被拒绝",
        "2026-11-10 10:00:03 | ERROR | 截图失败",
        "2026-11-10 10:00:04 | ERROR | 未识别出任何界面",
        "2026-11-10 10:00:05 | CRITICAL | 登录失败，会话过期",
        "2026-11-10 10:00:06 | ERROR | 配置文件缺失",
        "2026-11-10 10:00:07 | ERROR | 任务执行失败",
        "2026-11-10 10:00:08 | ERROR | 请求超时",
        "2026-11-10 10:00:09 | ERROR | chrome 崩溃",
        "2026-11-10 10:00:10 | ERROR | 莫名其妙的问题",
    ]
    summary = logs.error_summary(sample)
    by_key = {b["key"]: b["count"] for b in summary["buckets"]}
    for key in logs._ERROR_CATEGORY_KEYS:
        want = by_key.get(key, 0)
        got = sum(1 for ln in sample if logs.category_matches(ln, key))
        assert got == want, f"类别 {key} 下钻 {got} 条 != 标注 {want} 条"


def test_category_matches_unknown_category_is_empty():
    assert not any(logs.category_matches(ln, "no-such") for ln in _NETWORK_LINES)


# ===== V28-05：健康看板 =====

def _state(**kw) -> dict:
    base = {"push_fail_streak": 0, "push_last_ok_at": 0, "push_last_fail_at": 0,
            "push_last_fail_msg": "", "push_last_fail_kind": ""}
    base.update(kw)
    return base


def test_health_same_second_fail_wins(monkeypatch):
    """同秒先成功后失败：旧实现 last_fail > last_ok 不成立 → 显示「通道正常」。"""
    monkeypatch.setattr(alerts, "panel_config",
                        lambda: {"alert_enable": "1", "alert_target": "https://x.example/",
                                 "alert_channel": "webhook"})
    s = _state(push_last_ok_at=1000, push_last_fail_at=1000,
               push_last_fail_msg="webhook 返回 HTTP 500")
    monkeypatch.setattr(alerts, "alert_state_load", lambda inst: dict(s))
    h = alerts.alert_health({"dir": "/tmp/audit-v1281"})
    assert h["last_ok"] == h["last_fail"] == 1000
    assert h["level"] != "ok", "同秒内刚发生的失败被误判成「通道正常」"


def test_health_ok_when_fail_before_ok(monkeypatch):
    """真正的「先失败后成功」仍要判 ok。"""
    monkeypatch.setattr(alerts, "panel_config",
                        lambda: {"alert_enable": "1", "alert_target": "https://x.example/",
                                 "alert_channel": "webhook"})
    s = _state(push_last_ok_at=2000, push_last_fail_at=1000,
               push_last_fail_msg="旧错误")
    monkeypatch.setattr(alerts, "alert_state_load", lambda inst: dict(s))
    h = alerts.alert_health({"dir": "/tmp/audit-v1281"})
    assert h["level"] == "ok"


def test_retry_push_returns_message_and_kind(monkeypatch):
    """V28-05：`_retry_push` 必须把通道返回的 msg/kind 带出来（旧版只返回 bool）。"""
    monkeypatch.setattr(
        alerts, "alert_send",
        lambda title, body, severity="warn": {"ok": False, "msg": "Bark 返回 HTTP 500",
                                              "kind": "http"},
    )
    pend = [{"kind": "container_down", "title": "t", "body": "b", "severity": "warn"}]
    ok, msg, kind = alerts._retry_push(pend, alerts._pending_by_severity(pend))
    assert ok is False
    assert msg == "Bark 返回 HTTP 500"
    assert kind == "http"


def test_retry_failed_refreshes_reason(monkeypatch):
    """V28-05：重试失败要回填 msg/kind，而不是只刷新时间。"""
    monkeypatch.setattr(alerts, "_publish_alert", lambda *a, **k: None)
    s = _state(push_last_fail_msg="Bark 返回 HTTP 400", push_last_fail_kind="bark",
               push_last_fail_at=100)
    pend = [{"kind": "container_down", "title": "t", "body": "b", "severity": "warn"}]
    alerts._retry_failed({"dir": "/tmp/audit-v1281"}, s, pend, 900,
                         "webhook 连接超时", "network")
    assert s["push_last_fail_at"] == 900
    assert s["push_last_fail_msg"] == "webhook 连接超时"
    assert s["push_last_fail_kind"] == "network"
    assert s["push_last_fail_ms"] > 0


# ===== V28-02 / V28-03：交付依赖静态约束 =====

def test_dev_deps_declare_pytest_asyncio():
    """V28-02：四个声明处不能漏 pytest-asyncio，否则 --strict-markers 收集即 2 errors。"""
    pyproject = (_REPO.parent / "pyproject.toml").read_text(encoding="utf-8")
    assert "pytest-asyncio" in pyproject
    ci = (_REPO.parent / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "requirements-dev.lock" in ci or "pytest-asyncio" in ci
    dev_lock = (_REPO / "requirements-dev.lock").read_text(encoding="utf-8")
    assert "pytest-asyncio" in dev_lock


def test_lock_multipart_version_is_patched():
    """V28-03：lock 里的 python-multipart 必须 >= 0.0.31（六项 2026 PYSEC 的修复线）。"""
    lock = (_REPO / "requirements.lock").read_text(encoding="utf-8")
    ver = None
    for line in lock.splitlines():
        line = line.strip()
        if line.startswith("python-multipart=="):
            ver = line.split("==", 1)[1]
    assert ver is not None, "lock 未锁定 python-multipart"
    parts = [int(x) for x in ver.split(".")]
    assert tuple(parts) >= (0, 0, 31), f"python-multipart {ver} 仍低于 0.0.31"

    req = (_REPO / "requirements.txt").read_text(encoding="utf-8")
    assert "python-multipart>=0.0.31" in req
    pyproject = (_REPO.parent / "pyproject.toml").read_text(encoding="utf-8")
    assert 'python-multipart>=0.0.31' in pyproject


def test_js_chip_passes_category():
    """V28-04 前端侧：chip 点击必须让请求带上 category，而不是只填关键词。"""
    js = (_REPO / "static" / "panel.js").read_text(encoding="utf-8")
    assert "_logSumCat" in js
    assert "&category=" in js


# ===== V28-07：更新流程在无重启权限的环境下不得谎报失败 =====

def test_restart_denied_by_polkit_degrades_to_skip(monkeypatch):
    """审计 V28-07：CI/非 root 环境 systemctl restart 报
    `Interactive authentication required.` 时，代码已换入且校验通过，
    不能因此整单回滚报失败 —— 必须降级为 skipped + 提示手动重启。
    """
    from services import updateops as uo

    calls = {"run": 0, "http": 0}

    def _run(cmd, timeout=30):
        calls["run"] += 1
        if "is-active" in cmd:
            return {"code": 0, "out": "active"}
        return {"code": 1, "out": (
            "Failed to restart m7a-panel.service: Interactive authentication required.\n"
            "See system logs and 'systemctl status m7a-panel.service' for details."
        )}

    def _http(url, timeout=5):
        calls["http"] += 1
        return {"code": 0, "body": ""}

    monkeypatch.setattr(uo, "run_cmd", _run)
    monkeypatch.setattr(uo, "http_get", _http)
    r = uo._restart_and_health_check()
    assert r["ok"] is True, r
    assert r["skipped"] is True
    assert r["probe_ok"] is False
    assert "无法重启" in r["msg"] and "systemctl restart" in r["msg"]


def test_restart_real_failure_still_fails(monkeypatch):
    """反向保障：服务真起不来（单元不存在等）仍必须判失败，不许被降级吞掉。"""
    from services import updateops as uo

    def _run(cmd, timeout=30):
        if "is-active" in cmd:
            return {"code": 0, "out": "active"}
        return {"code": 1, "out": "Unit m7a-panel.service not found."}

    monkeypatch.setattr(uo, "run_cmd", _run)
    monkeypatch.setattr(uo, "http_get", lambda *a, **k: {"code": 0, "body": ""})
    r = uo._restart_and_health_check()
    assert r["ok"] is False
    assert r["skipped"] is False


def test_env_restart_blocked_markers():
    from services.updateops import restart as RS

    assert RS._env_restart_blocked("Interactive authentication required.")
    assert RS._env_restart_blocked("Access denied")
    assert RS._env_restart_blocked("System has not been booted with systemd")
    assert not RS._env_restart_blocked("Unit m7a-panel.service not found.")
    assert not RS._env_restart_blocked("")
