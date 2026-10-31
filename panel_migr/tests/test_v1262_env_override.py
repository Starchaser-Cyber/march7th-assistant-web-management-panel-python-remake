"""V1.26.2：环境变量覆盖感知（面板改了 config.yaml 却被容器 ENV 盖住）。

背景是一个真事故：宿主 `config.yaml` 里 `after_finish: "Exit"`、
`cloud_game_use_paid_time: false`，但容器 `Config.get_value()` 读出
`after_finish='Loop'`、`cloud_game_use_paid_time=True` —— 镜像 Dockerfile 的
`ENV MARCH7TH_AFTER_FINISH=Loop` 等 6 个变量优先级高于 config.yaml，面板保存
成功却毫无效果。本模块的职责只是「把真相显示出来」，不修配置、不动容器。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
from services import envoverride
from services.envoverride import (
    ENV_OVERRIDE_MAP,
    clear_cache,
    container_env,
    env_override_html,
    overridden,
    parse_env_output,
)


@pytest.fixture(autouse=True)
def _clear():
    clear_cache()
    yield
    clear_cache()


# ===== parse_env_output =====

def test_parse_env_output_basic():
    text = "PATH=/usr/bin\nMARCH7TH_LOG_LEVEL=DEBUG\nHOME=/root\n"
    env = parse_env_output(text)
    assert env == {"MARCH7TH_LOG_LEVEL": "DEBUG"}


def test_parse_env_output_keeps_only_march7th():
    text = "FOO=1\nMARCH7TH_A=2\nBAR=3\nMARCH7TH_B=4"
    assert parse_env_output(text) == {"MARCH7TH_A": "2", "MARCH7TH_B": "4"}


def test_parse_env_output_splits_on_first_equals_only():
    """值里含 = 时（token / URL 参数）不能把值切断。"""
    env = parse_env_output("MARCH7TH_TOKEN=a=b=c")
    assert env["MARCH7TH_TOKEN"] == "a=b=c"


def test_parse_env_output_skips_blank_and_malformed():
    text = "\n\n   \nno_equals_here\nMARCH7TH_OK=1\n"
    assert parse_env_output(text) == {"MARCH7TH_OK": "1"}


def test_parse_env_output_empty_input():
    assert parse_env_output("") == {}
    assert parse_env_output(None) == {}


def test_parse_env_output_allows_empty_value():
    assert parse_env_output("MARCH7TH_EMPTY=") == {"MARCH7TH_EMPTY": ""}


def test_parse_env_output_strips_crlf():
    """Windows 换行不能让值尾部带 \\r（否则日志里会多出不可见字符）。"""
    env = parse_env_output("MARCH7TH_A=1\r\nMARCH7TH_B=2\r\n")
    assert env == {"MARCH7TH_A": "1", "MARCH7TH_B": "2"}


# ===== ENV_OVERRIDE_MAP =====

def test_env_override_map_matches_upstream_keys():
    """键必须与上游 module/config/config.py 的 _ENV_OVERRIDE_MAP 完全一致。

    上游有 8 个键；这里少一个就会漏报（面板显示「没被覆盖」而实际被覆盖）。
    """
    expected = {
        "cloud_game_enable",
        "cloud_game_use_paid_time",
        "browser_headless_enable",
        "browser_headless_restart_on_not_logged_in",
        "browser_download_use_mirror",
        "log_level",
        "after_finish",
        "browser_type",
    }
    assert set(ENV_OVERRIDE_MAP) == expected


def test_env_override_map_values_are_env_names():
    for key, var in ENV_OVERRIDE_MAP.items():
        assert var.startswith("MARCH7TH_"), key
        assert re.fullmatch(r"MARCH7TH_[A-Z0-9_]+", var), (key, var)


def test_env_override_map_no_duplicate_env_names():
    names = list(ENV_OVERRIDE_MAP.values())
    assert len(names) == len(set(names))


# ===== overridden =====

_INST = {"name": "m7a"}


def test_overridden_empty_env():
    assert overridden(_INST, env={}) == {}


def test_overridden_reports_hits():
    env = {"MARCH7TH_AFTER_FINISH": "Loop", "MARCH7TH_LOG_LEVEL": "DEBUG"}
    hit = overridden(_INST, env=env)
    assert set(hit) == {"after_finish", "log_level"}
    assert hit["after_finish"] == {"env": "MARCH7TH_AFTER_FINISH", "value": "Loop"}
    assert hit["log_level"]["value"] == "DEBUG"


def test_overridden_ignores_unrelated_env():
    hit = overridden(_INST, env={"MARCH7TH_DOCKER_STARTED": "true", "PATH": "/bin"})
    assert hit == {}


def test_overridden_ignores_partial_name_match():
    """`MARCH7TH_AFTER_FINISH_X` 不能被当成 `MARCH7TH_AFTER_FINISH`。"""
    assert overridden(_INST, env={"MARCH7TH_AFTER_FINISH_X": "1"}) == {}


# ===== container_env =====

def test_container_env_reads_docker_inspect(monkeypatch):
    calls = []

    def fake_run(cmd, timeout=120):
        calls.append(cmd)
        return {"code": 0, "out": "MARCH7TH_LOG_LEVEL=DEBUG\n"}

    monkeypatch.setattr(envoverride, "run_cmd", fake_run)
    assert container_env(_INST) == {"MARCH7TH_LOG_LEVEL": "DEBUG"}
    assert len(calls) == 1
    assert "docker inspect" in calls[0]
    assert "m7a" in calls[0]


def test_container_env_cached(monkeypatch):
    calls = []

    def fake_run(cmd, timeout=120):
        calls.append(cmd)
        return {"code": 0, "out": "MARCH7TH_A=1\n"}

    monkeypatch.setattr(envoverride, "run_cmd", fake_run)
    container_env(_INST)
    container_env(_INST)
    container_env(_INST)
    assert len(calls) == 1, "短缓存应让三次调用只探一次 docker"


def test_container_env_force_bypasses_cache(monkeypatch):
    calls = []

    def fake_run(cmd, timeout=120):
        calls.append(cmd)
        return {"code": 0, "out": "MARCH7TH_A=1\n"}

    monkeypatch.setattr(envoverride, "run_cmd", fake_run)
    container_env(_INST)
    container_env(_INST, force=True)
    assert len(calls) == 2


def test_container_env_returns_empty_on_docker_error(monkeypatch):
    monkeypatch.setattr(
        envoverride, "run_cmd",
        lambda cmd, timeout=120: {"code": 1, "out": "No such object: m7a"},
    )
    assert container_env(_INST) == {}


def test_container_env_returns_empty_on_garbage_output(monkeypatch):
    monkeypatch.setattr(
        envoverride, "run_cmd",
        lambda cmd, timeout=120: {"code": 0, "out": "not an env dump at all"},
    )
    assert container_env(_INST) == {}


def test_container_env_cache_is_per_container(monkeypatch):
    def fake_run(cmd, timeout=120):
        if "m7a2" in cmd:
            return {"code": 0, "out": "MARCH7TH_B=2\n"}
        return {"code": 0, "out": "MARCH7TH_A=1\n"}

    monkeypatch.setattr(envoverride, "run_cmd", fake_run)
    assert container_env({"container": "m7a"}) == {"MARCH7TH_A": "1"}
    assert container_env({"container": "m7a2"}) == {"MARCH7TH_B": "2"}


# ===== env_override_html =====

_GROUPS = {
    "basic": {"fields": {"log_level": {"label": "日志等级"}, "after_finish": {"label": "完成后操作"}}},
}


def test_html_empty_when_no_override():
    assert env_override_html(_INST, _GROUPS, env={}) == ""


def test_html_renders_box_when_overridden():
    html = env_override_html(_INST, _GROUPS, env={"MARCH7TH_AFTER_FINISH": "Loop"})
    assert 'id="envOverrideBox"' in html
    assert "环境变量覆盖提醒（1 项）" in html
    assert "完成后操作" in html
    assert "MARCH7TH_AFTER_FINISH" in html
    assert "Loop" in html


def test_html_uses_chinese_label_from_groups():
    html = env_override_html(_INST, _GROUPS, env={"MARCH7TH_LOG_LEVEL": "DEBUG"})
    assert "日志等级" in html


def test_html_falls_back_to_key_when_no_label():
    html = env_override_html(_INST, None, env={"MARCH7TH_LOG_LEVEL": "DEBUG"})
    assert "<code>log_level</code>" in html


def test_html_counts_multiple():
    env = {"MARCH7TH_LOG_LEVEL": "DEBUG", "MARCH7TH_AFTER_FINISH": "Loop"}
    html = env_override_html(_INST, _GROUPS, env=env)
    assert "（2 项）" in html


def test_html_escapes_value():
    """环境变量值来自容器，不能直接当 HTML 插入。"""
    html = env_override_html(_INST, _GROUPS, env={"MARCH7TH_LOG_LEVEL": "<script>x</script>"})
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_html_mentions_compose_environment():
    html = env_override_html(_INST, _GROUPS, env={"MARCH7TH_LOG_LEVEL": "DEBUG"})
    assert "environment:" in html


# ===== 接入 pagectx / 模板 =====

def test_pagectx_has_env_override_html_key():
    """配置页上下文必须带 envOverrideHtml，否则模板渲染成空（提示永远不出现）。"""
    from pathlib import Path

    src = (_REPO / "services" / "pagectx.py").read_text("utf-8")
    assert "envOverrideHtml" in src
    assert "from services.envoverride import env_override_html" in src


def test_template_renders_env_override():
    from pathlib import Path

    tpl = (_REPO / "templates" / "panel.html.j2").read_text("utf-8")
    assert "{{ envOverrideHtml | safe }}" in tpl


def test_template_places_box_before_groups():
    """提示必须在配置分组之前——用户往下滚之前就能看到。"""
    from pathlib import Path

    tpl = (_REPO / "templates" / "panel.html.j2").read_text("utf-8")
    i_box = tpl.index("{{ envOverrideHtml | safe }}")
    i_loop = tpl.index("{% for gKey, g in CONFIG_GROUPS.items() %}")
    assert i_box < i_loop


def test_css_has_envov_rules():
    from pathlib import Path

    css = (_REPO / "static" / "panel.css").read_text("utf-8")
    assert ".envov-wrap" in css
    assert ".envov-title" in css


def test_pagectx_helper_swallows_errors(monkeypatch):
    """容器读不到 env 时配置页不能变成 500。"""
    from services import pagectx

    def boom(*a, **k):
        raise RuntimeError("docker down")

    monkeypatch.setattr(pagectx, "env_override_html", boom)
    assert pagectx._env_override_html({"name": "m7a"}) == ""


# ===== 只报「面板能编辑」的字段 =====

def test_overridden_filters_to_editable_when_groups_given():
    """browser_type 不在 CONFIG_GROUPS 里 → 不该出现在提示里（用户改不了）。"""
    env = {"MARCH7TH_BROWSER_TYPE": "chrome", "MARCH7TH_LOG_LEVEL": "DEBUG"}
    hit = overridden(_INST, env=env, groups=_GROUPS)
    assert "log_level" in hit
    assert "browser_type" not in hit


def test_overridden_no_filter_without_groups():
    env = {"MARCH7TH_BROWSER_TYPE": "chrome"}
    assert "browser_type" in overridden(_INST, env=env)


def test_overridden_empty_groups_dict_means_no_filter():
    """groups={} 视为「没给」，不过滤（避免调用方传空字典时提示莫名消失）。"""
    env = {"MARCH7TH_BROWSER_TYPE": "chrome"}
    assert "browser_type" in overridden(_INST, env=env, groups={})


def test_html_filters_uneditable_fields():
    env = {"MARCH7TH_BROWSER_TYPE": "chrome"}
    assert env_override_html(_INST, _GROUPS, env=env) == ""


def test_html_counts_only_editable():
    env = {
        "MARCH7TH_BROWSER_TYPE": "chrome",
        "MARCH7TH_LOG_LEVEL": "DEBUG",
        "MARCH7TH_AFTER_FINISH": "Loop",
    }
    html = env_override_html(_INST, _GROUPS, env=env)
    assert "（2 项）" in html
