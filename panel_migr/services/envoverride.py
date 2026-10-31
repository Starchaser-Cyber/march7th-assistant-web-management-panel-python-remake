"""环境变量覆盖感知（v1.26.2）。

背景：三月七本体（上游 `module/config/config.py`）有一张 `_ENV_OVERRIDE_MAP`，
容器环境变量只要存在就**无条件盖掉** config.yaml 里的同名字段。镜像 Dockerfile
里预置了 6 个 `ENV`（`MARCH7TH_CLOUD_GAME_ENABLE` / `MARCH7TH_BROWSER_HEADLESS_ENABLE`
/ `MARCH7TH_BROWSER_HEADLESS_RESTART_ON_NOT_LOGGED_IN` / `MARCH7TH_BROWSER_DOWNLOAD_USE_MIRROR`
/ `MARCH7TH_AFTER_FINISH` / `MARCH7TH_LOG_LEVEL`），于是面板改这几项时保存成功、
`config.yaml` 也确实变了，但小助手读到的仍是环境变量的值——表现为「改了没用」。

这个模块只做两件事：
1. `container_env()`：从 `docker inspect` 读出容器实际生效的环境变量（短缓存，
   面板一次渲染只探一次）。
2. `overridden()` / `env_override_html()`：算出哪些面板字段被覆盖，渲染成配置页
   顶部的一条提示，明确告诉用户「这项被环境变量盖住了，改 config.yaml 不会生效」。

不修改任何配置、不重启容器——纯只读提示。真正的解决办法（改 compose 的
`environment:` 或删掉镜像 ENV）交给用户决定，面板只负责把真相摆出来。
"""
from __future__ import annotations

import html
import re
import time

from services.instances import instance_container
from services.shell import run_cmd

# 与上游 module/config/config.py 的 _ENV_OVERRIDE_MAP 一一对应（键 = config.yaml 字段名）
ENV_OVERRIDE_MAP: dict = {
    "cloud_game_enable": "MARCH7TH_CLOUD_GAME_ENABLE",
    "cloud_game_use_paid_time": "MARCH7TH_CLOUD_GAME_USE_PAID_TIME",
    "browser_headless_enable": "MARCH7TH_BROWSER_HEADLESS_ENABLE",
    "browser_headless_restart_on_not_logged_in": "MARCH7TH_BROWSER_HEADLESS_RESTART_ON_NOT_LOGGED_IN",
    "browser_download_use_mirror": "MARCH7TH_BROWSER_DOWNLOAD_USE_MIRROR",
    "log_level": "MARCH7TH_LOG_LEVEL",
    "after_finish": "MARCH7TH_AFTER_FINISH",
    "browser_type": "MARCH7TH_BROWSER_TYPE",
}

# 容器 env 短缓存：一次页面渲染里 read 一次 docker inspect 就够（inspect 约 30ms）
_CACHE_TTL = 10.0
_CACHE: dict = {}

_LINE_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")


def parse_env_output(text: str) -> dict:
    """解析 `docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}'`。

    只保留 MARCH7TH_ 前缀的项；值里可能含 `=`（如 token），所以只按第一个 `=` 切。
    空行、无 `=` 的行直接跳过（docker 输出偶尔带尾随空行）。
    """
    out: dict = {}
    for raw in str(text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        m = _LINE_RE.match(line)
        if not m:
            continue
        k, v = m.group(1), m.group(2)
        if k.startswith("MARCH7TH_"):
            out[k] = v
    return out


def container_env(inst: dict, force: bool = False) -> dict:
    """读容器实际生效的 MARCH7TH_* 环境变量。

    失败（容器不存在 / docker 权限不足 / inspect 报错）返回 {}，调用方据此
    静默跳过提示——环境变量读不到不该让配置页变成错误页。
    """
    name = instance_container(inst)
    now = time.time()
    if not force:
        hit = _CACHE.get(name)
        if hit and hit[0] > now:
            return dict(hit[1])
    r = run_cmd(
        f"docker inspect {name} --format '{{{{range .Config.Env}}}}{{{{println .}}}}{{{{end}}}}'",
        timeout=15,
    )
    env = parse_env_output(r.get("out") or "") if r.get("code") == 0 else {}
    _CACHE[name] = (now + _CACHE_TTL, dict(env))
    return env


def _editable_keys(groups: dict | None) -> set | None:
    """面板 CONFIG_GROUPS 里真正可编辑的字段名集合；groups 为空则返回 None（不过滤）。"""
    if not groups:
        return None
    keys: set = set()
    for g in groups.values():
        if not isinstance(g, dict):
            continue
        fields = g.get("fields")
        if isinstance(fields, dict):
            keys.update(str(k) for k in fields)
    return keys


def overridden(inst: dict, env: dict | None = None, groups: dict | None = None) -> dict:
    """返回被环境变量盖住的字段：{config_key: {"env": 变量名, "value": 值}}。

    `env` 可显式传入（测试用）；不传就读容器。只报告「容器里真有这个 env」的项——
    镜像 ENV 被 compose 覆盖或删掉后，这里自然就空了。

    `groups` 传入时只报告**面板里能编辑**的字段：上游 _ENV_OVERRIDE_MAP 有 8 个键，
    但面板表单只暴露其中一部分（如 browser_type 面板根本没有）。提示里列出面板改
    不了的项纯属噪音——用户看了也不知道去哪改。
    """
    if env is None:
        env = container_env(inst)
    allowed = _editable_keys(groups)
    out: dict = {}
    for key, var in ENV_OVERRIDE_MAP.items():
        if var not in env:
            continue
        if allowed is not None and key not in allowed:
            continue
        out[key] = {"env": var, "value": env[var]}
    return out


def _h(s) -> str:
    return html.escape(str(s), quote=True)


def _label_of(key: str, groups: dict | None) -> str:
    """从 CONFIG_GROUPS 里找字段中文名；找不到就退回字段名本身。"""
    if groups:
        for g in groups.values():
            if not isinstance(g, dict):
                continue
            f = (g.get("fields") or {}).get(key)
            if isinstance(f, dict) and f.get("label"):
                return str(f["label"])
    return key


def env_override_html(inst: dict, groups: dict | None = None, env: dict | None = None) -> str:
    """配置页顶部提示块。没有覆盖项时返回空串（模板里 `| safe` 渲染为空）。"""
    hit = overridden(inst, env, groups)
    if not hit:
        return ""
    items = []
    for key, info in hit.items():
        items.append(
            "<li><b>" + _h(_label_of(key, groups)) + "</b>"
            "（<code>" + _h(key) + "</code>）被容器环境变量 "
            "<code>" + _h(info["env"]) + "=" + _h(info["value"]) + "</code> 覆盖"
            "</li>"
        )
    out = '<div class="envov-wrap" id="envOverrideBox">'
    out += '<div class="envov-title">环境变量覆盖提醒（' + str(len(hit)) + " 项）</div>"
    out += (
        '<div class="envov">下面这些配置项在小助手容器里被<b>环境变量</b>盖住了：'
        "在面板里改了会「保存成功但实际不生效」，因为小助手优先读环境变量。"
        "<ul>" + "".join(items) + "</ul>"
        "要真正改掉，需要修改容器编排文件里的 <code>environment:</code> "
        "（或删掉镜像自带的同名 ENV），再重建容器；面板不会替你改这些变量。"
        "</div>"
    )
    out += "</div>"
    return out


def clear_cache() -> None:
    """测试用：清掉容器 env 缓存。"""
    _CACHE.clear()
