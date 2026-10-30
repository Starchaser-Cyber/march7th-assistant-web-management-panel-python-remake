"""版本号比较与时间戳解析（Release tag / GHCR 镜像 tag 用）。"""
from __future__ import annotations

import re



from . import _rt


def _nums(v: str) -> list[int]:
    """版本号的数字前缀分段；后缀标签（-python/beta…）不参与比较。"""
    out = []
    for seg in re.split(r"[.\-_]", (v or "").strip()):
        if seg.isdigit():
            out.append(int(seg))
        else:
            break
    return out or [0]


def _version_gt(a: str, b: str) -> bool:
    """数字前缀比较（短侧补 0）：'1.20' > '1.19.0-python' → True；
    '1.19.0-python' vs '1.19' → False（修复旧版把后缀当分段导致的误报）。"""
    pa, pb = list(_rt._nums(a)), list(_rt._nums(b))
    n = max(len(pa), len(pb))
    pa += [0] * (n - len(pa))
    pb += [0] * (n - len(pb))
    return pa > pb


def _version_eq(a: str, b: str) -> bool:
    return not _rt._version_gt(a, b) and not _rt._version_gt(b, a)


def version_gt(a: str, b: str) -> bool:
    """公开版本比较（v1.24 M2）：告警分级与上游本体版本检查复用。"""
    return _rt._version_gt(a, b)


def version_eq(a: str, b: str) -> bool:
    """公开版本相等判断（v1.24 M2）。"""
    return _rt._version_eq(a, b)


def _version_from_text(txt: str) -> str:
    m = re.search(r'M7A_PANEL_VERSION"\s*,\s*"([^"]+)"', txt)
    return m.group(1).strip() if m else ""


def _to_ts(s: str):
    """strtotime() 子集：ISO8601 / RFC 时间 → 时间戳；失败 False。

    v1.24.4（P1-6）：此前用 `s[:25].replace("Z","+0000")` 配 strptime，而 GHCR 的
    created 形如 `2026-10-21T22:37:22.870489069Z`（9 位纳秒）：截到 25 字符正好把
    结尾的 Z 切掉，`%z` 匹配失败 → 恒返回 False。现在先把纳秒/毫秒截到微秒，再交给
    fromisoformat（3.11+ 能吃 Z），失败再退回几种常见格式。
    """
    from datetime import datetime
    s = (s or "").strip()
    if not s:
        return False
    cand = re.sub(r"\.(\d{6})\d+", r".\1", s)          # 纳秒 → 微秒
    cand = re.sub(r"\.(\d{3})\d+", r".\1", cand)       # 毫秒 → 微秒的兜底
    cand = cand.replace("Z", "+00:00").replace("z", "+00:00")
    try:
        return int(datetime.fromisoformat(cand).timestamp())
    except ValueError:
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z",
                "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return int(datetime.strptime(cand, fmt).timestamp())
        except ValueError:
            continue
    return False
