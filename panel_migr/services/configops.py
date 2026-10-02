"""config.yaml 读写 + 推送配置体检 —— 等价 PHP config_*/yaml_*/notify_* 全套。

配置字段表与渠道规则表来自 php_tables.json（由 index.php $CONFIG_GROUPS /
$NOTIFY_CHANNEL_RULES 字面量提取，改 PHP 需重新提取）。
"""
from __future__ import annotations

import html
import json
import re
import time
from pathlib import Path

import config as cfg
from services.instances import instance_config, instance_dir

_TABLES = json.loads((Path(__file__).with_name("php_tables.json")).read_text("utf-8"))
CONFIG_GROUPS: dict = _TABLES["config_groups"]
NOTIFY_RULES: list = _TABLES["notify_rules"]


def h(s) -> str:
    """等价 PHP h() = htmlspecialchars(ENT_QUOTES, UTF-8)。"""
    return html.escape(str(s), quote=True)


# ===== 配置字段索引 =====

def config_field_index() -> dict:
    """真实键名 → {'label','group','type','options'?}（来自 $CONFIG_GROUPS）。"""
    idx: dict = {}
    for gk, g in CONFIG_GROUPS.items():
        for fk, f in (g.get("fields") or {}).items():
            d = dict(f)
            d["group"] = gk
            idx[fk] = d
    return idx


# ===== 推送体检（notify_val_* / notify_field_index / notify_channel_name ...）=====

def notify_val_empty(v) -> bool:
    return str("" if v is None else v).strip("\"' \t") == ""


def notify_val_true(v) -> bool:
    return str("" if v is None else v).strip("\"' \t").lower() == "true"


def notify_field_index() -> dict:
    idx: dict = {}
    for gk, g in CONFIG_GROUPS.items():
        for fk, f in (g.get("fields") or {}).items():
            idx[fk] = {
                "label": str(f.get("label") or fk),
                "group": str(gk),
            }
    return idx


def notify_channel_name(rule: dict, rules: list | None = None) -> str:
    """渠道展示名：启用开关 label 去「启用」前后缀；无开关退回分组短标题。"""
    all_rules = NOTIFY_RULES if rules is None else rules
    idx = notify_field_index()
    gk = str(rule.get("group") or "")
    gtitle = str(CONFIG_GROUPS.get(gk, {}).get("title") or "")
    gshort = re.sub(r"^更多渠道\s*·\s*", "", gtitle).strip()
    ek = rule.get("enable")
    name = ""
    if ek is not None and str(ek) != "" and ek in idx:
        name = re.sub(r"^启用\s*", "", idx[ek]["label"]).strip()
        name = re.sub(r"\s*·\s*启用$", "", name).strip()
    if name == "":
        return gshort if gshort != "" else gk
    same_group = sum(
        1 for r in all_rules if isinstance(r, dict) and str(r.get("group") or "") == gk
    )
    if (
        same_group > 1
        and gshort != ""
        and "/" not in gshort
        and len(gshort.encode("utf-8")) <= 15
        and gshort not in name
    ):
        return gshort + " · " + name
    return name


def notify_field_label(key, channel_name: str, fields: dict) -> str:
    label = str(fields[key]["label"]) if key in fields else str(key)
    pos = label.find(" · ")
    if pos != -1:
        head = label[:pos]
        if head == channel_name or (head != "" and head in channel_name):
            label = label[pos + 3:]
    return label.strip()


def notify_health_check(cfg_vals, rules: list | None = None) -> dict:
    """推送配置体检（只读）：master/ok/warn/off。"""
    if rules is None:
        rules = NOTIFY_RULES
    if not isinstance(cfg_vals, dict):
        cfg_vals = {}
    if not isinstance(rules, list):
        rules = []
    fields = notify_field_index()
    master = notify_val_true(cfg_vals.get("notification_enable", ""))
    ok: list = []
    warn: list = []
    off = 0
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        ek = rule.get("enable")
        required = rule.get("required") if isinstance(rule.get("required"), list) else []
        if ek is not None and str(ek) != "":
            enabled = notify_val_true(cfg_vals.get(str(ek), ""))
        else:
            enabled = any(
                not notify_val_empty(cfg_vals.get(str(rk), "")) for rk in required
            )
        if not enabled:
            off += 1
            continue
        name = notify_channel_name(rule, rules)
        missing = [
            notify_field_label(rk, name, fields)
            for rk in required
            if notify_val_empty(cfg_vals.get(str(rk), ""))
        ]
        if missing:
            warn.append({"name": name, "missing": missing})
        else:
            ok.append({"name": name, "missing": []})
    return {"master": master, "ok": ok, "warn": warn, "off": off}


def notify_check_html(res) -> str:
    """体检结果 → 页面提示块（渲染在「消息推送」分组上方）。"""
    if not isinstance(res, dict):
        return ""
    master = bool(res.get("master"))
    oks = res.get("ok") or []
    warns = res.get("warn") or []
    names = [it["name"] for it in oks]
    out = '<div class="nchk-wrap" id="notifyCheckBox">'
    out += '<div class="nchk-title">🔍 推送配置体检</div>'
    if not master:
        out += '<div class="nchk nchk-red">⚠️ 推送总开关（notification_enable）未开启，下面所有渠道都不会生效。</div>'
    if warns:
        out += f'<div class="nchk nchk-orange">⚠️ 有 {len(warns)} 个渠道已启用但缺少必填项：<ul>'
        for it in warns:
            out += "<li><b>" + h(it["name"]) + "</b>：缺少「" + h("」「".join(it["missing"])) + "」</li>"
        out += "</ul></div>"
    if oks:
        if not master:
            out += f'<div class="nchk nchk-green">✅ 已填齐全 {len(oks)} 个渠道：' + h("、".join(names)) + "（把总开关打开后即生效）</div>"
        elif warns:
            out += f'<div class="nchk nchk-green">✅ 当前生效 {len(oks)} 个渠道：' + h("、".join(names)) + "</div>"
        else:
            out += f'<div class="nchk nchk-green">✅ 体检通过，当前生效 {len(oks)} 个渠道：' + h("、".join(names)) + "</div>"
    elif not warns:
        out += f'<div class="nchk nchk-gray">💤 当前没有启用任何推送渠道（共 {int(res.get("off") or 0)} 个渠道，均未启用）。</div>'
    out += '<p class="nchk-foot">说明：体检读取的是<b>已保存</b>的配置（刚保存的这份），只做检查、不会发送任何消息；改完记得点「💾 保存并重启容器」让配置生效。</p>'
    out += "</div>"
    return out


# ===== config.yaml 读写 =====

def config_read_raw(inst: dict, max_bytes: int = 65536):
    p = Path(instance_config(inst))
    if not p.is_file():
        return None
    try:
        with open(p, "rb") as f:
            return f.read(max_bytes).decode("utf-8", "replace")
    except OSError:
        return None


def yaml_read_simple(inst: dict) -> dict:
    """逐行拉平 config.yaml（去引号/行尾注释/多行值取首个非空缩进行）。"""
    vals: dict = {}
    p = Path(instance_config(inst))
    if not p.is_file():
        return vals
    try:
        lines = p.read_text("utf-8", "replace").splitlines(keepends=True)
    except OSError:
        return vals
    n = len(lines)
    for i in range(n):
        line = lines[i]
        m = re.match(r"^([a-zA-Z_]\w*):\s*(.*?)\s*$", line)
        if not m:
            continue
        key, raw = m.group(1), m.group(2)
        if raw == "":  # 多行值：取后续第一个非空缩进行
            for j in range(i + 1, n):
                nl = lines[j]
                if not re.match(r"^\s+", nl):
                    break
                mm = re.search(r"^\s*([^#\s].*?)\s*(#.*)?$", nl)
                if mm:
                    raw = mm.group(1).strip()
                    break
        mq = re.match(r'^"((?:[^"\\]|\\.)*)"\s*(#.*)?$', raw)
        if mq:
            val = _stripslashes(mq.group(1))
        else:
            ms = re.match(r"^'((?:[^'\\]|\\.)*)'\s*(#.*)?$", raw)
            if ms:
                val = _stripslashes(ms.group(1))
            else:
                mc = re.match(r"^(.*?)\s+#", raw)
                val = mc.group(1).strip() if mc else raw.strip()
        vals[key] = val
    return vals


def _stripslashes(s: str) -> str:
    """等价 PHP stripslashes()。"""
    return re.sub(r"\\(.)", r"\1", s)


def _php_float_str(v: float) -> str:
    if v == int(v) and abs(v) < 1e16:
        return str(int(v))
    return repr(v)


def yaml_format_val(val, typ: str):
    """值 → 写入 config.yaml 的行内文本；password 空值返回 None（不修改）。"""
    if typ == "bool":
        s = str(val)
        return "true" if s in ("true", "1") else "false"
    if typ == "int":
        try:
            return str(int(str(val).strip() or 0))
        except ValueError:
            try:
                return str(int(float(str(val))))
            except ValueError:
                return "0"
    if typ == "float":
        try:
            return _php_float_str(float(str(val)))
        except ValueError:
            return "0"
    if typ == "password":
        if val == "" or val is None:
            return None
        return '"' + str(val).replace("\\", "\\\\").replace('"', '\\"') + '"'
    # str / select
    return '"' + str(val).replace("\\", "\\\\").replace('"', '\\"') + '"'


def config_save_form(inst: dict, updates: dict) -> dict:
    p = Path(instance_config(inst))
    if not p.is_file():
        return {"ok": False, "msg": "config.yaml 不存在"}
    try:
        lines = p.read_text("utf-8", "replace").splitlines(keepends=True)
    except OSError:
        return {"ok": False, "msg": "无法读取 config.yaml"}
    out: list = []
    changed: list = []
    for line in lines:
        matched = False
        m = re.match(r"^([a-zA-Z_]\w*):\s*", line)
        if m:
            key = m.group(1)
            if key in updates and updates[key] is not None:
                rest = line[len(m.group(0)):]
                cm = re.search(r"#\s*.*$", rest)
                comment = " " + cm.group(0) if cm else ""
                out.append(f"{key}: {updates[key]}{comment}\n")
                changed.append(key)
                matched = True
        if not matched:
            out.append(line)
    try:
        p.write_text("".join(out), "utf-8")
    except OSError:
        return {"ok": False, "msg": f"写入失败，请检查文件权限：chmod 666 {instance_config(inst)}"}
    return {"ok": True, "msg": f"已更新 {len(changed)} 项配置", "changed": changed}


def config_save_text(inst: dict, content: str) -> dict:
    if "locales:" not in content and "power_enable:" not in content:
        return {"ok": False, "msg": "内容异常，未找到有效配置键，已取消保存"}
    try:
        Path(instance_config(inst)).write_text(content, "utf-8")
    except OSError:
        return {"ok": False, "msg": f"写入失败，请检查文件权限：chmod 666 {instance_config(inst)}"}
    return {"ok": True, "msg": "配置已保存"}


def config_backup(inst: dict):
    """复制 config.yaml → config.yaml.bak.YmdHis；返回路径或 None（等价 PHP copy）。"""
    src = Path(instance_config(inst))
    if not src.is_file():
        return None
    dst = src.with_name("config.yaml.bak." + time.strftime("%Y%m%d%H%M%S"))
    try:
        dst.write_bytes(src.read_bytes())
        return str(dst)
    except OSError:
        return None


def restore_config(inst: dict, fname: str, content: bytes | str) -> dict:
    """等价 PHP restore_config 分支（ext / size / 内容校验 → 备份 → 覆盖）。"""
    ext = (fname.rsplit(".", 1)[-1] if "." in fname else "").lower()
    fsize = len(content) if content is not None else 0
    if ext not in ("yaml", "yml"):
        return {"ok": False, "msg": "文件格式错误，请上传 .yaml / .yml 文件"}
    if fsize <= 0 or fsize > 2 * 1024 * 1024:
        return {"ok": False, "msg": "文件为空或过大（最大 2MB）"}
    text = content.decode("utf-8", "replace") if isinstance(content, bytes) else content
    if len(text.strip()) < 10 or ":" not in text:
        return {"ok": False, "msg": "文件内容异常，未识别到有效 YAML 配置"}
    bak = config_backup(inst)
    try:
        Path(instance_config(inst)).write_text(text, "utf-8")
    except OSError:
        return {"ok": False, "msg": f"写入失败，请检查文件权限：chmod 666 {instance_config(inst)}"}
    return {
        "ok": True,
        "msg": f"配置已恢复（{h(fname)}），原配置备份：{Path(bak).name if bak else '无'}。需重启容器生效。",
    }


def container_is_running(inst: dict) -> bool:
    """等价 PHP container_is_running()。"""
    from shlex import quote
    from services.shell import run_cmd
    r = run_cmd(f'docker inspect -f "{{{{.State.Running}}}}" {quote(instance_container_of(inst))} 2>&1')
    return r["out"].strip() == "true"


def instance_container_of(inst: dict) -> str:
    from services.instances import instance_container
    return instance_container(inst)


def now() -> int:
    return int(time.time())
