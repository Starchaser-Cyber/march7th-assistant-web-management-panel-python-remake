"""config.yaml 读写 + 推送配置体检 —— 等价 PHP config_*/yaml_*/notify_* 全套。

配置字段表与渠道规则表来自 php_tables.json（由 index.php $CONFIG_GROUPS /
$NOTIFY_CHANNEL_RULES 字面量提取，改 PHP 需重新提取）。
"""
from __future__ import annotations

import html
import json
import os
import re
import secrets
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


def _atomic_write_text(path, text: str) -> None:
    """config.yaml 原子写（临时文件 + os.replace），避免并发读者读到半截文件。

    mkstemp 建出的临时文件是 0600，直接 replace 会把 config.yaml 权限从 0644
    降成 0600（容器内非 root 进程可能读不到）→ 沿用原文件权限。
    """
    import os as _os
    import stat as _stat

    from phpfile import atomic_write_text

    mode = None
    try:
        mode = _stat.S_IMODE(_os.stat(str(path)).st_mode)
    except OSError:
        mode = None
    atomic_write_text(path, text, mode=mode)


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

# config.yaml 读/写上限（面板 YAML 编辑器整份载入 → 两者必须一致，否则保存即静默截断）
CONFIG_MAX_BYTES = 1024 * 1024


def config_read_raw(inst: dict, max_bytes: int = CONFIG_MAX_BYTES):
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
            val = _yaml_unescape(mq.group(1))
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


_YAML_ESCAPES = {
    "0": "\0",
    "a": "\a",
    "b": "\b",
    "t": "\t",
    "\t": "\t",
    "n": "\n",
    "v": "\v",
    "f": "\f",
    "r": "\r",
    "e": "\x1b",
    " ": " ",
    '"': '"',
    "/": "/",
    "\\": "\\",
    "N": "\x85",
    "_": "\xa0",
    "L": "\u2028",
    "P": "\u2029",
}


def _yaml_unescape(s: str) -> str:
    """还原 YAML 双引号字符串里的转义序列（`\\n`→换行、`\\t`→制表符、`\\\\`→反斜杠…）。

    不能用 PHP 的 stripslashes()：它会把 `\\n`（两字符）错解成 `n`，导致多行值
    读回后丢掉换行，再保存一次就把 config.yaml 写成非法 YAML。
    """
    if "\\" not in s:
        return s

    out: list = []
    i = 0
    n = len(s)
    while i < n:
        c = s[i]
        if c != "\\" or i + 1 >= n:
            out.append(c)
            i += 1
            continue
        nxt = s[i + 1]
        if nxt == "x" and i + 3 < n:
            try:
                out.append(chr(int(s[i + 2:i + 4], 16)))
                i += 4
                continue
            except ValueError:
                pass
        if nxt == "u" and i + 5 < n:
            try:
                out.append(chr(int(s[i + 2:i + 6], 16)))
                i += 6
                continue
            except ValueError:
                pass
        out.append(_YAML_ESCAPES.get(nxt, nxt))
        i += 2
    return "".join(out)


def _yaml_dq(val) -> str:
    """任意值 → 合法的 YAML 双引号标量（保证单行、换行/制表/引号全部转义）。"""
    s = str(val)
    s = (
        s.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )
    # 其它控制字符：YAML 双引号里必须转义，否则整份文件非法
    if any(ord(c) < 0x20 and c not in "\n\r\t" for c in s):
        s = "".join("\\x%02x" % ord(c) if (ord(c) < 0x20 or ord(c) == 0x7F) else c for c in s)
    return '"' + s + '"'


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
        return _yaml_dq(val)
    # str / select / textarea
    return _yaml_dq(val)


def _split_yaml_comment(rest: str) -> tuple[str, str]:
    """把 `值  # 注释` 拆成 (值, 注释)；引号内的 `#` 不算注释。

    textarea 类字段（webhook body / headers）常见 `#` 与 `"`，若按裸正则切注释，
    会把值本身当成注释丢掉，两轮保存后内容被改写。
    """
    quote = ""
    esc = False
    for i, c in enumerate(rest):
        if esc:
            esc = False
            continue
        if c == "\\" and quote:
            esc = True
            continue
        if quote:
            if c == quote:
                quote = ""
            continue
        if c in "\"'":
            quote = c
            continue
        if c == "#" and (i == 0 or rest[i - 1] in " \t"):
            return rest[:i], rest[i:].rstrip("\r\n")
    return rest.rstrip("\r\n"), ""


_DQ_FULL = re.compile(r'^"(?:[^"\\]|\\.)*"$')


def _has_unclosed_quote(val: str) -> bool:
    """值以 `"` 开头但不是完整的双引号标量（旧版把多行值原样塞进引号的形态）。"""
    v = val.strip()
    if not v.startswith('"'):
        return False
    return _DQ_FULL.match(v) is None


def _closes_quote(line: str) -> bool:
    """续行里是否出现了未转义的闭合双引号。"""
    esc = False
    for c in line:
        if esc:
            esc = False
        elif c == "\\":
            esc = True
        elif c == '"':
            return True
    return False


def _escape_controls(text: str) -> str:
    """把裸控制字符（换行/回车/制表/其它）换成 YAML 双引号内的转义序列。"""
    out = []
    for c in text:
        if c == "\n":
            out.append("\\n")
        elif c == "\r":
            out.append("\\r")
        elif c == "\t":
            out.append("\\t")
        elif ord(c) < 0x20 or ord(c) == 0x7F:
            out.append("\\x%02x" % ord(c))
        else:
            out.append(c)
    return "".join(out)


_PLAIN_UNSAFE = re.compile(r'[\x00-\x1f\x7f]')


def _needs_quote(val: str) -> bool:
    """裸标量是否会写坏/误解析 YAML（多行、控制字符、首尾空白、空值、特殊首字符）。"""
    if val == "" or val != val.strip():
        return True
    if _PLAIN_UNSAFE.search(val):
        return True
    if val[0] in "-?:,[]{}#&*!|>'\"%@`":
        return True
    if ": " in val or val.endswith(":"):
        return True   # `k: a: b` 裸写会变成映射/非法 YAML
    if val.lower() in ("true", "false", "yes", "no", "on", "off", "null", "~"):
        return True
    return False


def _dq_wrap(val: str) -> str:
    """保证返回值是单行、合法的 YAML 双引号标量。

    调用方（routers/write.py::_form_updates）对 password 字段自行拼了引号，
    这里兼容两种入参：已正确加引号的值只补转义裸控制字符（不破坏原有 `\\n`），
    其它一律当原始值整体转义包裹。
    """
    s = str(val)
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        inner = s[1:-1]
        if re.fullmatch(r'(?:[^"\\]|\\.)*', inner):
            return '"' + _escape_controls(inner) + '"'
        # 内部仍有裸引号 → 不是合法 YAML 标量，整串当原始值重新转义
    return _yaml_dq(s)


def config_save_form(inst: dict, updates: dict) -> dict:
    p = Path(instance_config(inst))
    if not p.is_file():
        return {"ok": False, "msg": "config.yaml 不存在"}
    try:
        lines = p.read_text("utf-8", "replace").splitlines(keepends=True)
    except OSError:
        return {"ok": False, "msg": "无法读取 config.yaml"}
    # v1.24.4：字符串值走双引号转义，杜绝多行值写坏整份 YAML；
    # 但 bool/int/float 必须保持裸标量，否则 `true` 会变成字符串 "true"、`3600` 变成 "3600"。
    idx = config_field_index()
    typed: dict = {}
    for k, v in updates.items():
        if not isinstance(v, str):
            typed[k] = v
            continue
        typ = (idx.get(k) or {}).get("type")
        if typ in ("bool", "int", "float"):
            formatted = yaml_format_val(v, typ)
            typed[k] = v if formatted is None else formatted
        elif typ is None:
            # 未知键：只有确实需要引号时才包，保持旧版裸标量行为
            typed[k] = _dq_wrap(v) if _needs_quote(v) else v
        else:
            typed[k] = _dq_wrap(v)
    updates = typed
    out: list = []
    changed: list = []
    n = len(lines)
    i = 0
    while i < n:
        line = lines[i]
        i += 1
        matched = False
        m = re.match(r"^([a-zA-Z_]\w*):\s*", line)
        if m:
            key = m.group(1)
            if key in updates and updates[key] is not None:
                rest = line[len(m.group(0)):]
                _val, cm = _split_yaml_comment(rest)
                comment = (" " + cm) if cm else ""
                out.append(f"{key}: {updates[key]}{comment}\n")
                changed.append(key)
                matched = True
                # v1.24.4 修复历史坏文件：旧版把多行值原样塞进双引号，值跨了多行。
                # 重写该键时把后续续行一并吞掉，否则残留续行仍是非法 YAML。
                if _has_unclosed_quote(_val):
                    while i < n and not _closes_quote(lines[i]):
                        if re.match(r"^[a-zA-Z_]\w*:", lines[i]):
                            break  # 撞到下一个键：到此为止，别把后面整份文件吞了
                        i += 1
                    else:
                        i += 1 if i < n else 0
        if not matched:
            out.append(line)
    try:
        _atomic_write_text(instance_config(inst), "".join(out))
    except OSError:
        return {"ok": False, "msg": f"写入失败，请检查文件权限：chmod 666 {instance_config(inst)}"}
    return {"ok": True, "msg": f"已更新 {len(changed)} 项配置", "changed": changed}


def config_save_text(inst: dict, content: str) -> dict:
    if "locales:" not in content and "power_enable:" not in content:
        return {"ok": False, "msg": "内容异常，未找到有效配置键，已取消保存"}
    if len(content.encode("utf-8")) > CONFIG_MAX_BYTES:
        return {"ok": False, "msg": f"内容过大（上限 {CONFIG_MAX_BYTES // 1024}KB），已取消保存"}
    try:
        _atomic_write_text(instance_config(inst), content)
    except OSError:
        return {"ok": False, "msg": f"写入失败，请检查文件权限：chmod 666 {instance_config(inst)}"}
    return {"ok": True, "msg": "配置已保存"}


def config_backup(inst: dict):
    """复制 config.yaml → config.yaml.bak.YmdHis.pid.rand；返回路径或 None（等价 PHP copy）。

    加 pid + 随机后缀：同一秒内连续两次保存不再互相覆盖备份。
    """
    src = Path(instance_config(inst))
    if not src.is_file():
        return None
    stem = "config.yaml.bak." + time.strftime("%Y%m%d%H%M%S")
    for _ in range(8):
        dst = src.with_name(f"{stem}.{os.getpid()}.{secrets.token_hex(3)}")
        try:
            fd = os.open(str(dst), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except FileExistsError:
            continue
        except OSError:
            return None
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(src.read_bytes())
            return str(dst)
        except OSError:
            try:
                os.unlink(str(dst))
            except OSError:
                pass
            return None
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
        _atomic_write_text(instance_config(inst), text)
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
