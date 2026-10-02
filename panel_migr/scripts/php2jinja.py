#!/usr/bin/env python3
"""index.php 页面段 → Jinja2 模板一次性迁移工具（M5 前端拆包，v2）。

机械规则自动转换；多行/多语句复杂块原样保留并汇总到 stdout 清单，由人工转换。
PHP 函数（container_status/format_size/count/empty/is_file/basename/strtoupper/
trim/in_array/instance_*/…）保留原名，运行期由 render_context 注入 Jinja globals。
用法：python3 scripts/php2jinja.py [源 index.php] [目标模板]
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

PAGE_FROM, PAGE_TO = 3193, 6227  # 1-based 含边界：<!DOCTYPE … </html>


def translate_expr(expr: str) -> str:
    """PHP 标量表达式 → Jinja 表达式。"""
    e = expr.strip()
    e = e.replace("===", "==").replace("!==", "!=")
    e = e.replace("&&", " and ").replace("||", " or ")
    e = re.sub(r"!(?!=)", " not ", e)                 # ! → not（!==已先转）
    e = re.sub(r"\$([A-Za-z_][A-Za-z0-9_]*)", r"\1", e)
    # ?? 合并 → Jinja default 过滤器（falsy 语义差异仅 '0' 边缘场景，ACCEPTANCE 注记）
    e = re.sub(r'''\((.+?) \?\? (.+?)\)''', r'''((\1) | default(\2, true))''', e)
    e = re.sub(r'''([\w\[\]\']+) \?\? ('[^']*'|"[^"]*")''', r'''((\1) | default(\2, true))''', e)
    # PHP 字符串拼接 . → Jinja ~（仅空格点空格，安全）
    e = e.replace(" . ", " ~ ")
    e = re.sub(r"\btrue\b", "true", e)
    e = re.sub(r"\bfalse\b", "false", e)
    e = re.sub(r"\bnull\b", "none", e)
    # 三元：A ? B : C → (B if A else C)（块内恰好一个顶层 ?）
    if "?" in e:
        cond, rest = e.split("?", 1)
        left, _, right = rest.partition(":")
        e = "(%s if %s else %s)" % (left.strip(), cond.strip(), right.strip())
    return e


def convert_inner(inner: str, manual: list[str], raw: str) -> str | None:
    """返回替换文本；None 表示交由调用方处理/保留。"""
    # ---- 复杂块（多语句、多行、合写 else:foreach 带 set）→ 人工 ----
    if "\n" in inner:
        return None
    # foreach … : $cur = instance_current();  （块尾随 set）
    m = re.fullmatch(r"foreach \((.+) as \$([A-Za-z_]\w*)\): \$([A-Za-z_]\w*) = ([^;]+);", inner)
    if m:
        return "{%% for %s in %s %%}{%% set %s = %s %%}" % (
            m.group(2), translate_expr(m.group(1)), m.group(3), translate_expr(m.group(4)))
    # else: foreach（单行合写，无 set）
    m = re.fullmatch(r"else: foreach \((.+) as \$([A-Za-z_]\w*)\):", inner)
    if m:
        return "{%% else %%}{%% for %s in %s %%}" % (
            m.group(2), translate_expr(m.group(1)))
    # 单行前置 set：$a = …; $b = …;（无 if/echo/foreach 关键字）
    if re.fullmatch(r"(\$[\w\[\]'\"$ ]+ = [^;]+; ?)+", inner) and "if " not in inner:
        parts = [p.strip() for p in inner.split(";") if p.strip()]
        out = []
        for p in parts:
            var, _, val = p.lstrip("$").partition(" = ")
            out.append("{%% set %s = %s %%}" % (var.replace("$", ""), translate_expr(val)))
        return "".join(out)
    # 简单 echo / 控制流
    m = re.fullmatch(r"echo h\((.*)\);", inner)
    if m:
        return "{{ %s }}" % translate_expr(m.group(1))
    m = re.fullmatch(r"echo \(int\)(.*);", inner)
    if m:
        return "{{ (%s)|int }}" % translate_expr(m.group(1))
    m = re.fullmatch(r"echo (.*);", inner)
    if m:
        return "{{ %s }}" % translate_expr(m.group(1))
    m = re.fullmatch(r"if \((.*)\):", inner)
    if m:
        return "{%% if %s %%}" % translate_expr(m.group(1))
    m = re.fullmatch(r"elseif \((.*)\):", inner)
    if m:
        return "{%% elif %s %%}" % translate_expr(m.group(1))
    if inner == "else:":
        return "{% else %}"
    if inner == "endif;":
        return "{% endif %}"
    m = re.fullmatch(r"foreach \((.+) as \$([A-Za-z_]\w*)\):", inner)
    if m:
        return "{%% for %s in %s %%}" % (m.group(2), translate_expr(m.group(1)))
    m = re.fullmatch(r"foreach \((.+) as \$([A-Za-z_]\w*) => \$([A-Za-z_]\w*)\):", inner)
    if m:
        coll = translate_expr(m.group(1))
        if "(" not in coll:
            coll += ".items()"
        return "{%% for %s, %s in %s %%}" % (m.group(2), m.group(3), coll)
    if inner == "endforeach;":
        return "{% endfor %}"
    if inner == "endforeach; endif;":
        return "{% endfor %}{% endif %}"
    if inner == "else: endif;":
        return "{% else %}{% endif %}"
    return None


def convert(text: str) -> tuple[str, list[str]]:
    manual: list[str] = []
    text = re.sub(r"<\?php\s+echo csrf_field\(\);\s*\?>", "{{ csrf_field() }}", text)

    def repl(m: re.Match) -> str:
        raw = m.group(0)
        inner = re.sub(r"^<\?php\s*|\s*\?>$", "", raw, flags=re.S).strip()
        out = convert_inner(inner, manual, raw)
        if out is None:
            manual.append(" ".join(raw.split())[:200])
            return raw
        return out

    return re.sub(r"<\?php.*?\?>", repl, text, flags=re.S), manual


def main() -> int:
    src = Path(sys.argv[1] if len(sys.argv) > 1 else ".tmp/php_src/index.php")
    dst = Path(sys.argv[2] if len(sys.argv) > 2 else "panel_py_repo/templates/panel.html.j2")
    lines = src.read_text("utf-8").splitlines(keepends=True)
    page = "".join(lines[PAGE_FROM - 1:PAGE_TO])
    page = re.sub(r"<style>.*?</style>",
                  '<link rel="stylesheet" href="/static/panel.css">', page, flags=re.S)
    page = re.sub(r"<script>.*?</script>",
                  '<script src="/static/panel.js"></script>', page, flags=re.S)
    out, manual = convert(page)
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(out, "utf-8")
    residue = out.count("<?php") + out.count("<?=")
    print(f"wrote {dst}  lines={out.count(chr(10))}  residue={residue}  manual={len(manual)}")
    for i, b in enumerate(manual, 1):
        print(f"  [{i:02d}] {b}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
