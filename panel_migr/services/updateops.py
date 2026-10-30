"""自动更新 / 镜像更新 / 版本备份回滚 —— 等价 PHP update_*/image_*/backup_* 全套。

v1.20 起自动更新改为「Release 整包 ZIP 自更新」（旧版 PHP release 仍保留 index.php
回退流程）：下载 V*.zip → 校验结构与版本一致性 → 整面板备份到 backups/panel_*.zip
→ 解压换入（data/ backups/ 运行数据不动）→ 版本复核（失败自动回滚）→ 延迟重启。

网络请求走 httpx（同步，handler 在线程池执行）；失败语义与 PHP curl 版一致：
code=0 表示连接失败，body='' 表示无内容。
"""
from __future__ import annotations

import hashlib
import hmac
import io
import json
import os
import re
import secrets
import shutil
import sys
import threading
import time
import zipfile
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote as rawurlencode

import httpx

import config as cfg
from services.instances import instance_container, instance_current, instance_dir
from services.shell import run_cmd

UA = f"M7A-Panel/{cfg.PANEL_VERSION}"
MIRRORS = ("https://ghfast.top/", "https://gh-proxy.com/", "https://ghproxy.net/", "https://ghps.cc/")
IMAGE_CACHE = cfg.BASE_DIR / ".image_check_cache.json"
BACKUP_DIR = cfg.BASE_DIR / "backups"
INDEX_FILE = cfg.BASE_DIR / "index.php"
IMAGE_REPO = "moesnow/march7thassistant"
GHCR_PREFIXES = ("ghcr.io/", "ghcr.nju.edu.cn/", "ghcr.m.daocloud.io/", "ghcr.dockerproxy.com/")

# 备份/换入时永远不动的运行数据与内部目录
# v1.24.4（P1-1）：sessions/ 必须一起保护 —— 此前它随整包备份进 backups/*.zip
# （0644，绕过 v1.24.3 的会话文件权限加固），一键回滚又把旧快照整体换回，
# 等于复活已注销的会话（认证绕过）。备份与换入两侧都跳过它。
_PROTECT_TOP = ("data", "backups", "sessions")
_PROTECT_DIRS = {"__pycache__", ".venv", "venv", "node_modules", "_tmp", ".git"}

# 代码根：panel_migr/ 的父目录。与 cfg.BASE_DIR（数据根，可被 M7A_PANEL_DIR 改成
# /srv/m7a-panel）不同 —— README 推荐的「代码/数据分离」部署下两者不是同一目录。
# v1.24.4（P1-5）：整包备份与版本复核都必须以代码根为准，否则在分离布局下
# 备份到 0 个文件、版本读成空串，更新必然失败。
CODE_ROOT = Path(__file__).resolve().parent.parent

# 更新互斥（v1.24.4 P1-3）：同一秒并发会共用 .stage_<ts>/.old_<ts> 目录，互相
# rmtree 掉对方正在使用的目录。do_update 全程非阻塞持锁，拿不到直接拒绝。
_UPDATE_LOCK = threading.Lock()

# 解压/下载上限（v1.24.4 P1-8）：防 zip bomb 打爆内存与磁盘（本机 8G 红线）。
_MAX_TOTAL_SIZE = 200 * 1024 * 1024      # 解压后总大小上限 200MB
_MAX_ENTRIES = 20000                     # 条目数上限
_MAX_FILE_SIZE = 50 * 1024 * 1024        # 单文件上限 50MB
_MAX_RATIO = 200                         # 压缩比上限 200:1
_MAX_DOWNLOAD = 64 * 1024 * 1024         # 下载包体上限 64MB

# 重启与健康探测（v1.24.4 P0-3/P0-6）
_RESTART_UNIT = "m7a-panel"
_HEALTH_PROBE_SCRIPT = "/tmp/m7a_health_probe.py"

# detach 出去的健康探测 / 回滚脚本（v1.24.4 P0-3）。
# 为什么要独立脚本：一键更新跑在被重启进程**自己的**请求线程里
# （routers/write.py h_do_update → main.py:59 to_thread.run_sync），
# 原地 systemctl restart 会让 systemctl 与 uvicorn 优雅关停互等，
# 90s 后 SIGKILL，重启后的探测与回滚成为死代码。
# 因此把「restart → 轮询端口 → 失败回滚」整体交给 setsid 出去的独立进程：
# 父进程立即返回响应，随后被杀也不影响探测；脚本自己持有 old/ 的清理权。
_PROBE_SCRIPT_SRC = '''#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""m7a-panel 更新后重启 / 健康探测 / 失败自动回滚（v1.24.4 P0-3）。

由 panel 更新收尾时以 detach 方式启动（setsid + 后台），因此**不依赖发起更新的
进程存活**：systemctl restart 会杀掉那个进程，本脚本继续跑完探测；探测失败时把
old/ 里的旧代码换回 base 再重启一次。

用法（参数全部由 updateops 传入）：
  m7a_health_probe.py <base> <old> <stage> <port> <retries> <interval> <unit>

时序：
  0s    启动（父进程已把 HTTP 响应返回给浏览器）
  1s    systemctl restart <unit>
  +N s  每 interval 秒 GET http://127.0.0.1:<port>/ 一次，共 retries 次
  成功  → 删 old/ stage/，退出 0
  失败  → 用 old/ 覆盖回 base，再 restart 一次；old/ 换回成功才删，
          失败则原样保留并退出 3（旧代码仍在 old/，可人工恢复）
日志写到 <old>.probe.log，便于事后排查。
"""
import os
import shutil
import subprocess
import sys
import time
import urllib.request


def log(msg, path):
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\\n")
    except OSError:
        pass


def run(cmd, timeout=90):
    try:
        p = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return p.returncode, ((p.stdout or "") + (p.stderr or "")).strip()
    except Exception as e:
        return 1, str(e)


def probe(port):
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/" % int(port), timeout=5) as r:
            return int(getattr(r, "status", 0) or 0) == 200
    except Exception:
        return False


def rollback(base, old):
    """把 old/ 里的原条目换回 base；返回失败条目数。"""
    failed = 0
    for name in sorted(os.listdir(old)):
        if name.startswith(".venv_stash_"):
            continue
        src = os.path.join(old, name)
        dst = os.path.join(base, name)
        try:
            if os.path.isdir(dst) and not os.path.islink(dst):
                shutil.rmtree(dst)
            elif os.path.lexists(dst):
                os.unlink(dst)
            shutil.move(src, dst)
        except OSError:
            failed += 1
    return failed


def main():
    if len(sys.argv) < 8:
        return 2
    base, old, stage, port, retries, interval, unit = sys.argv[1:8]
    logpath = old.rstrip("/") + ".probe.log"
    time.sleep(1)
    rc, out = run("systemctl restart %s" % unit)
    log("restart rc=%s %s" % (rc, out[:200]), logpath)
    for _ in range(int(retries)):
        time.sleep(float(interval))
        if probe(port):
            log("probe ok, cleanup", logpath)
            shutil.rmtree(old, ignore_errors=True)
            shutil.rmtree(stage, ignore_errors=True)
            return 0
    log("probe failed, rolling back", logpath)
    if not os.path.isdir(old):
        log("no old/ to roll back, giving up", logpath)
        return 4
    failed = rollback(base, old)
    run("systemctl restart %s" % unit)
    if failed:
        log("rollback incomplete (%d items), kept %s" % (failed, old), logpath)
        return 3
    shutil.rmtree(old, ignore_errors=True)
    shutil.rmtree(stage, ignore_errors=True)
    log("rolled back ok", logpath)
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''

# 最近一次 _backup_panel_zip 失败的具体原因（空串=上次成功）。do_update 用它
# 替代原先一律甩给用户的「请检查 backups 目录权限」误导文案（v1.24.4 P1-2/P1-5）。
_LAST_BACKUP_ERR = ""


# ===== HTTP（等价 PHP http_get / http_post）=====

def http_get(url: str, timeout: int = 8) -> dict:
    try:
        r = httpx.get(
            url, timeout=httpx.Timeout(timeout, connect=5), follow_redirects=True,
            headers={"User-Agent": UA},
        )
        return {"code": r.status_code, "body": r.text}
    except Exception:
        return {"code": 0, "body": ""}


def http_bytes(url: str, timeout: int = 60) -> bytes | None:
    """下载二进制（更新包）；失败 / 超过 _MAX_DOWNLOAD → None。

    v1.24.4（P1-8）：改流式读取 + 边读边累计上限。此前用 httpx.get 把整包读进
    内存（无 Content-Length 上限），投毒包可直接把 8G 机器打爆。
    """
    try:
        with httpx.stream(
            "GET", url, timeout=httpx.Timeout(timeout, connect=10),
            follow_redirects=True, headers={"User-Agent": UA},
        ) as r:
            if r.status_code != 200:
                return None
            # 有 Content-Length 且明显超限：不读包体，直接放弃
            try:
                cl = int(r.headers.get("content-length") or 0)
            except (TypeError, ValueError):
                cl = 0
            if cl > _MAX_DOWNLOAD:
                return None
            buf = bytearray()
            for chunk in r.iter_bytes():
                buf.extend(chunk)
                if len(buf) > _MAX_DOWNLOAD:
                    return None
            return bytes(buf) if buf else None
    except Exception:
        return None


def http_post(url: str, fields: dict, timeout: int = 8, as_json: bool = False) -> dict:
    try:
        if as_json:
            r = httpx.post(url, content=json.dumps(fields, ensure_ascii=False),
                           headers={"Content-Type": "application/json"},
                           timeout=httpx.Timeout(timeout, connect=5), follow_redirects=True)
        else:
            r = httpx.post(url, data=fields,
                           headers={"Content-Type": "application/x-www-form-urlencoded"},
                           timeout=httpx.Timeout(timeout, connect=5), follow_redirects=True)
        return {"code": r.status_code, "body": r.text}
    except Exception:
        return {"code": 0, "body": "request failed"}


def _probe(url: str, timeout: int = 8) -> dict:
    """轻量下载探测（Range 头，尽量只取首字节）→ {code, ok}。"""
    try:
        r = httpx.get(
            url, headers={"User-Agent": UA, "Range": "bytes=0-0"},
            timeout=httpx.Timeout(timeout, connect=5), follow_redirects=True,
        )
        return {"code": r.status_code, "ok": r.status_code in (200, 206)}
    except Exception:
        return {"code": 0, "ok": False}


# ===== 更新源地址 =====

def update_api_url() -> str:
    owner, repo = rawurlencode(cfg.UPDATE_OWNER), rawurlencode(cfg.UPDATE_REPO)
    if cfg.UPDATE_TYPE == "github":
        return f"https://api.github.com/repos/{owner}/{repo}/releases/latest"
    return cfg.UPDATE_HOST.rstrip("/") + f"/api/v1/repos/{owner}/{repo}/releases/latest"


def update_raw_url() -> str:
    owner, repo = rawurlencode(cfg.UPDATE_OWNER), rawurlencode(cfg.UPDATE_REPO)
    if cfg.UPDATE_TYPE == "github":
        return f"https://raw.githubusercontent.com/{owner}/{repo}/{cfg.UPDATE_BRANCH}/index.php"
    return cfg.UPDATE_HOST.rstrip("/") + f"/{owner}/{repo}/raw/branch/{cfg.UPDATE_BRANCH}/index.php"


def update_raw_urls() -> list[str]:
    if cfg.UPDATE_TYPE != "github":
        return [update_raw_url()]
    official = update_raw_url()
    mirrors = [official] + [m + official for m in MIRRORS]
    ts = "?t=" + str(int(time.time()))
    return [u + ts for u in mirrors]


def panel_update_mode() -> str:
    from services.instances import panel_config
    c = panel_config()
    return "manual" if str(c.get("update_mode") or "") == "manual" else "auto"


# ===== 版本比较 =====

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
    pa, pb = list(_nums(a)), list(_nums(b))
    n = max(len(pa), len(pb))
    pa += [0] * (n - len(pa))
    pb += [0] * (n - len(pb))
    return pa > pb


def _version_eq(a: str, b: str) -> bool:
    return not _version_gt(a, b) and not _version_gt(b, a)


def version_gt(a: str, b: str) -> bool:
    """公开版本比较（v1.24 M2）：告警分级与上游本体版本检查复用。"""
    return _version_gt(a, b)


def version_eq(a: str, b: str) -> bool:
    """公开版本相等判断（v1.24 M2）。"""
    return _version_eq(a, b)


# ===== Release / ZIP 资产 =====

def _release_json() -> tuple[dict | None, str]:
    """→ (release JSON, 错误说明)。"""
    r = http_get(update_api_url(), 10)
    if r["code"] != 200 or not r["body"]:
        return None, f"HTTP {r['code']}"
    try:
        j = json.loads(r["body"])
    except ValueError:
        return None, "返回数据无法解析"
    if not isinstance(j, dict) or not j.get("tag_name"):
        return None, "无可用 release"
    return j, ""


def _zip_asset(j: dict) -> tuple[str, str]:
    """→ (zip 下载 URL, 资产名)。命名约定 V{版本}.zip，其次任意 *.zip。"""
    if not isinstance(j, dict):
        return "", ""
    assets = j.get("assets") or []
    zips = [a for a in assets
            if isinstance(a, dict) and str(a.get("name") or "").lower().endswith(".zip")]
    if not zips:
        return "", ""
    prefer = [a for a in zips if re.match(r"^[Vv]\d", str(a.get("name") or ""))]
    a = (prefer or zips)[0]
    return str(a.get("browser_download_url") or ""), str(a.get("name") or "")


def _zip_urls(zip_url: str) -> list[tuple[str, str]]:
    """官方源 + 加速镜像的下载地址列表。"""
    if cfg.UPDATE_TYPE != "github":
        return [("官方源", zip_url)]
    out = [("官方源", zip_url)]
    out += [(f"加速镜像{i}", m + zip_url) for i, m in enumerate(MIRRORS, 1)]
    return out


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _asset_digest(j: dict, asset_name: str) -> str:
    """从 Release JSON 取资产 sha256 digest。

    v1.24.3 安全加固（M7A-01）：GitHub Release API 自 2024 年起为每个资产返回
    `digest: "sha256:<64hex>"`，该值经 TLS 校验的 API 通道取得，可作为更新包
    的真实性锚点。第三方加速镜像可替换包体但无法替换该值 —— 下载后比对，
    不符即丢弃该来源。

    返回 64 位小写 hex；无该字段（旧 API / gitea / 自建源）→ ""。
    """
    if not isinstance(j, dict) or asset_name == "":
        return ""
    for a in j.get("assets") or []:
        if not isinstance(a, dict):
            continue
        if str(a.get("name") or "") != asset_name:
            continue
        d = str(a.get("digest") or "").strip().lower()
        if d.startswith("sha256:") and re.fullmatch(r"[0-9a-f]{64}", d[7:]):
            return d[7:]
    return ""


def check_update() -> dict:
    if not cfg.UPDATE_ENABLED:
        return {"ok": True, "enabled": False}
    r = http_get(update_api_url(), 8)
    if r["code"] != 200 or not r["body"]:
        return {"ok": False, "err": f"更新源连接失败（HTTP {r['code']}）"}
    try:
        j = json.loads(r["body"])
    except ValueError:
        j = None
    if not isinstance(j, dict) or not j.get("tag_name"):
        return {"ok": False, "err": "更新源返回数据异常"}
    latest = str(j["tag_name"]).lstrip("vV")
    note = str(j.get("body") or "").strip()
    _, asset = _zip_asset(j)
    return {
        "ok": True,
        "enabled": True,
        "has_update": _version_gt(latest, cfg.PANEL_VERSION),
        "latest": latest,
        "current": cfg.PANEL_VERSION,
        "note": note,
        "asset": asset,
        "digest_ok": _asset_digest(j, asset) != "",
        "update_mode": panel_update_mode(),
    }


def test_update_source() -> dict:
    """连通性测试：release API + 更新包下载源（v1.20 为 ZIP 资产；
    旧版 release 无 zip 时回退 index.php raw 探测）。响应形状保持前端兼容。"""
    api = update_api_url()
    ar = http_get(api, 8)
    j = None
    if ar["code"] == 200:
        try:
            j = json.loads(ar["body"])
        except ValueError:
            j = None
    if ar["code"] == 200 and "tag_name" in ar["body"].lower():
        api_state = "ok_release"
    elif ar["code"] == 200:
        api_state = "ok_no_release"
    else:
        api_state = "fail"

    mirrors = []
    raw_ok = False
    raw_code = 0
    raw_url = update_raw_url()

    zip_url, _asset = _zip_asset(j) if isinstance(j, dict) else ("", "")
    if zip_url:
        raw_url = zip_url
        urls = [(name, url) for name, url in _zip_urls(zip_url)]
        for i, (name, url) in enumerate(urls):
            pr = _probe(url)
            mirrors.append({"name": name, "url": url, "code": pr["code"], "ok": pr["ok"]})
            if i == 0:
                raw_ok, raw_code = pr["ok"], pr["code"]
    else:
        for i, url in enumerate(update_raw_urls()):
            rr = http_get(url, 8)
            ok = rr["code"] == 200 and (rr["body"].lstrip()[:5].lower() == "<?php")
            mirrors.append({
                "name": "官方源" if i == 0 else f"镜像{i}",
                "url": url,
                "code": rr["code"],
                "ok": ok,
            })
            if i == 0:
                raw_ok, raw_code = ok, rr["code"]

    return {
        "ok": api_state != "fail" and raw_ok,
        "api": {"url": api, "code": ar["code"], "state": api_state},
        "raw": {"url": raw_url, "code": raw_code, "ok": raw_ok},
        "mirrors": mirrors,
    }


# ===== ZIP 整包自更新（v1.20）=====

def _version_from_text(txt: str) -> str:
    m = re.search(r'M7A_PANEL_VERSION"\s*,\s*"([^"]+)"', txt)
    return m.group(1).strip() if m else ""


def _zip_struct_ok(data: bytes) -> bool:
    """包内必须有 panel_migr 主程序与前端资源。

    v1.24.4（P1-7）：不再用 endswith 匹配 —— 那会放过带外层目录的包
    （m7a_panel_V1.24.3/panel_migr/...），而 _swap_stage 是按解压根目录的一级
    条目换入的：多一层外层目录时，同版本重打包会「假成功」（一行代码没换、还
    往面板根塞进一个目录），不同版本则回滚后残留垃圾目录。
    现在要求包根**直接**含 panel_migr/，且不存在单一顶层目录包裹。
    """
    try:
        names = [n.replace("\\", "/") for n in zipfile.ZipFile(io.BytesIO(data)).namelist()]
    except (zipfile.BadZipFile, OSError):
        return False
    if not names:
        return False
    # 包根必须直接是 panel_migr/...（不能有外层目录包裹）
    if any(n.startswith("/") for n in names):
        return False
    tops = {n.split("/", 1)[0] for n in names if n.strip("/")}
    if "panel_migr" not in tops:
        return False
    # 只有一个顶层目录且不是 panel_migr → 典型的「多一层外层目录」包
    if len(tops) == 1 and "panel_migr" not in tops:
        return False
    need = (
        "panel_migr/main.py", "panel_migr/config.py", "panel_migr/static/panel.js",
        "panel_migr/routers/__init__.py",   # v1.24.1：Python 包初始化，缺它则 main.py 顶层 import 炸
    )
    return all(any(n == x or n.startswith(x + "/") for n in names) for x in need)


def _zip_new_version(data: bytes) -> str:
    """从 ZIP 内 panel_migr/config.py 提取新版本号；缺失 → 抛异常。"""
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for name in z.namelist():
            if name.replace("\\", "/").endswith("panel_migr/config.py"):
                v = _version_from_text(z.read(name).decode("utf-8", "replace"))
                if v:
                    return v
    raise RuntimeError("包内缺少 panel_migr/config.py")


def _code_root() -> Path:
    """代码所在目录（含 panel_migr/ 的那一级）。

    v1.24.4（P1-5）：README 推荐的部署把代码放 /opt/m7a-panel-repo、数据放
    /srv/m7a-panel（M7A_PANEL_DIR）。此前备份与版本复核都以 cfg.BASE_DIR 为根，
    在分离布局下会备份到 0 个文件并误报「请检查 backups 目录权限」。
    兼容单目录布局：BASE_DIR 下确实有 panel_migr/ 时仍以它为根（旧部署 + 测试）。
    """
    base = Path(cfg.BASE_DIR)
    try:
        if (base / "panel_migr").is_dir():
            return base
    except OSError:
        pass
    return CODE_ROOT


def _installed_version() -> str:
    """读取**代码目录**里已安装的版本号（v1.24.4 P1-5）。"""
    try:
        txt = (_code_root() / "panel_migr" / "config.py").read_text("utf-8", "replace")
    except OSError:
        return ""
    return _version_from_text(txt)


def _installed_version_in(root: Path) -> str:
    """读取任意代码根下的已安装版本号（v1.24.5 Q-06）。

    `_recover_interrupted()` 要用它比对「.old_* 里那份代码」和「当前 BASE_DIR 里
    的代码」，所以不能复用固定读 `_code_root()` 的 `_installed_version()`。
    读不到（文件缺失 / 内容被截断成半截）时返回 ""。
    """
    try:
        txt = (Path(root) / "panel_migr" / "config.py").read_text("utf-8", "replace")
    except OSError:
        return ""
    return _version_from_text(txt)


# 「换入已完成」必须同时具备的关键文件（v1.24.5 Q-06）。
# 只看 panel_migr/main.py 不够：_swap_stage 是**逐顶层条目** move，被 SIGKILL
# 打断时完全可能停在「main.py 已换入、services/ 还是旧的」或「panel_migr 换进来了
# 但 .venv 还没放回」的位置，这种半成品一旦被当成「换入已完成」就会把唯一完整的
# .old_* 删掉 —— 旧代码永久丢失。这里挑的是任何一份能跑起来的 panel_migr 都必有的
# 最小文件集。
_CODE_VITAL = (
    "panel_migr/main.py",
    "panel_migr/config.py",
    "panel_migr/routers/__init__.py",
    "panel_migr/services/shell.py",
)


def _code_intact(root: Path, old: Path | None = None) -> tuple[bool, str]:
    """判断 `root` 下的面板代码是否「换入已完成、可以安全删掉 .old_*」。

    返回 (是否完整, 原因)。判据（全部满足才算完整）：
      1. `.old_*` 里确实存在的每个 `_CODE_VITAL` 文件，在 root 里也必须存在且非空。
         只比对 .old_* 里有的文件，是为了不把「精简安装 / 测试夹具」误判成半成品；
         .old_* 里没有的文件无从比对，不作为证据。
      2. root 的 `panel_migr/config.py` 必须能解析出合法版本号。读不到时**宁可换回
         也不要删** —— 0 字节或被截断的 config.py 正是换入中断的典型半成品。
      3. `.old_*` 里不得残留 `.venv_stash_*`：那是 `_swap_stage` 换入中途用来暂存
         运行环境的槽位，只要还在就说明换入没走完（.venv 还没放回）。
    """
    root = Path(root)
    checked = 0
    for rel in _CODE_VITAL:
        if old is not None and not (Path(old) / rel).exists():
            continue                      # 旧快照里本来就没有 → 无从比对
        checked += 1
        p = root / rel
        try:
            if not p.is_file() or p.stat().st_size <= 0:
                return False, f"{rel} 缺失或为空"
        except OSError as e:
            return False, f"{rel} 无法访问：{e}"
    if checked == 0:
        # 旧快照里一个关键文件都没有（异常残留），退化为只看 main.py
        main_py = root / "panel_migr" / "main.py"
        try:
            if not main_py.is_file() or main_py.stat().st_size <= 0:
                return False, "panel_migr/main.py 缺失或为空"
        except OSError as e:
            return False, f"panel_migr/main.py 无法访问：{e}"
    if not _installed_version_in(root):
        return False, "读不到已安装版本号（config.py 缺失或损坏）"
    if old is not None:
        try:
            if any(Path(old).glob(".venv_stash_*")):
                return False, ".old_* 里仍有 .venv_stash_*（换入未走完）"
        except OSError:
            pass
    return True, "关键文件齐全且版本号可读"


def _extract_zip(data: bytes, stage: Path) -> None:
    """解压更新包到 stage；带 zip bomb 防护（v1.24.4 P1-8）。

    先遍历 infolist 累计 file_size（压缩包元数据，不需要真的解压），校验
    总大小 / 条目数 / 单文件大小 / 压缩比，任一超限直接抛异常，绝不解压。
    再逐条 extract（不用 extractall，便于按条目复核实际写盘大小）。
    """
    stage.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        infos = z.infolist()
        if len(infos) > _MAX_ENTRIES:
            raise RuntimeError(
                f"更新包条目数异常（{len(infos)} > {_MAX_ENTRIES}），已中止解压")
        total = 0
        for info in infos:
            nm = info.filename.replace("\\", "/")
            if nm.startswith("/") or ".." in nm.split("/"):
                raise RuntimeError(f"包内路径非法：{nm}")
            size = int(info.file_size or 0)
            if size > _MAX_FILE_SIZE:
                raise RuntimeError(
                    f"更新包内单文件过大（{nm} {size}B > {_MAX_FILE_SIZE}B），已中止解压")
            total += size
            if total > _MAX_TOTAL_SIZE:
                raise RuntimeError(
                    f"更新包解压后总大小超限（>{_MAX_TOTAL_SIZE}B），已中止解压")
            csize = int(info.compress_size or 0)
            if csize > 0 and size / csize > _MAX_RATIO:
                raise RuntimeError(
                    f"更新包压缩比异常（{nm} {size}/{csize} > {_MAX_RATIO}:1），"
                    f"疑似解压炸弹，已中止解压")
        z.extractall(stage)


class _BackupError(RuntimeError):
    """备份过程中遇到的、需要如实告知用户的失败（v1.24.4 P1-2）。"""


# 1980-01-01T00:00:00Z —— ZIP 格式能表示的最早时间；早于它的 mtime 会让
# zf.write() 抛 ValueError（云盘/网盘同步、旧包解压都常见）。
_ZIP_MIN_TS = 315532800


def _zip_date_time(p: Path) -> tuple:
    """文件 mtime → ZIP date_time，钳到 1980 之后（v1.24.4 P1-2）。

    注意不能只把时间戳钳到 315532800：负时区下 localtime 会落到 1979-12-31，
    仍然抛 ValueError，所以再兜一层年月检查。
    """
    try:
        ts = int(p.stat().st_mtime)
    except OSError:
        ts = int(time.time())
    if ts < _ZIP_MIN_TS:
        ts = _ZIP_MIN_TS
    dt = time.localtime(ts)[:6]
    if dt[0] < 1980:
        return (1980, 1, 1, 0, 0, 0)
    return dt


def _zip_tree(zf: zipfile.ZipFile, root: Path, arc_root: str) -> int:
    """递归把 root 下的文件写进 zf；返回写入条目数。

    v1.24.4（P1-2）：不再用 zf.write（对 <1980 的 mtime 抛 ValueError，而旧代码
    只捕获 OSError → 异常穿出 do_update 变成 500，还留下半截 zip 被当成合法备份）。
    改为显式 ZipInfo(date_time=...)，且 (OSError, ValueError) 都转成 _BackupError，
    由 _backup_panel_zip 删残件并如实报错。
    """
    n = 0
    try:
        items = sorted(root.rglob("*"))
    except OSError as e:
        raise _BackupError(f"遍历 {root} 失败：{e}") from e
    for p in items:
        rel = p.relative_to(root)
        if any(part in _PROTECT_DIRS for part in rel.parts):
            continue
        if not p.is_file():
            continue
        try:
            mode = p.stat().st_mode & 0o777
        except OSError:
            mode = 0o644
        try:
            zi = zipfile.ZipInfo(str(Path(arc_root) / rel), date_time=_zip_date_time(p))
            zi.compress_type = zipfile.ZIP_DEFLATED
            zi.external_attr = (mode or 0o644) << 16
            zf.writestr(zi, p.read_bytes())
            n += 1
        except (OSError, ValueError) as e:
            raise _BackupError(f"打包 {rel} 失败：{e}") from e
    return n


def _backup_panel_zip(version: str = "") -> str:
    """整面板快照 → backups/panel_<ver>_<ts>.zip（不含 data/ backups/ sessions/）。
    成功返回路径，失败返回 ""（与 backup_current_index 语义一致）；
    失败原因写入模块级 _LAST_BACKUP_ERR，供 do_update 如实转述给用户。

    v1.24.4（P1-5）：打包根改为**代码目录**（_code_root()）。此前以 cfg.BASE_DIR
    为根，在 README 推荐的「代码 /opt/m7a-panel-repo + 数据 /srv/m7a-panel」布局下
    一个文件都打包不到（n==0），恒报「备份失败（请检查 backups 目录权限）」——
    错误信息完全误导运维。
    """
    global _LAST_BACKUP_ERR
    _LAST_BACKUP_ERR = ""
    d = backups_dir()
    if not d.is_dir():
        _LAST_BACKUP_ERR = f"备份目录不可用：{d}"
        return ""
    root = _code_root()
    if not (root / "panel_migr").is_dir():
        _LAST_BACKUP_ERR = f"找不到面板代码目录（{root}/panel_migr 不存在）"
        return ""
    ver = re.sub(r"[^A-Za-z0-9._-]", "", (version or cfg.PANEL_VERSION).strip()) or "unknown"
    target = d / f"panel_{ver}_{time.strftime('%Y%m%d%H%M%S')}.zip"
    try:
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
            n = 0
            for item in sorted(root.iterdir()):
                if item.name in _PROTECT_TOP or item.name.startswith("."):
                    continue
                if item.is_dir():
                    n += _zip_tree(zf, item, item.name)
                elif item.is_file():
                    try:
                        zi = zipfile.ZipInfo(item.name, date_time=_zip_date_time(item))
                        zi.compress_type = zipfile.ZIP_DEFLATED
                        zi.external_attr = (item.stat().st_mode & 0o777) << 16
                        zf.writestr(zi, item.read_bytes())
                        n += 1
                    except (OSError, ValueError) as e:
                        raise _BackupError(f"打包 {item.name} 失败：{e}") from e
        if n == 0 or target.stat().st_size < 200:
            # 不是权限问题：这个目录里根本没有可备份的面板文件（布局不匹配）
            _LAST_BACKUP_ERR = (
                f"没有可备份的面板文件（代码目录 {root} 下未找到 panel_migr/ 等面板文件）")
            try:
                target.unlink()
            except OSError:
                pass
            return ""
    except _BackupError as e:
        _LAST_BACKUP_ERR = str(e)
        try:
            target.unlink()
        except OSError:
            pass
        return ""
    except (OSError, ValueError) as e:
        _LAST_BACKUP_ERR = f"写入备份失败：{e}"
        try:
            target.unlink()
        except OSError:
            pass
        return ""
    backups_prune()
    return str(target)


def _swap_stage(stage: Path, old: Path) -> None:
    """stage 内一级条目换入面板目录；被替换的条目移入 old 供失败回滚。
    data/ backups/ sessions/ 等运行数据永不触碰。
    v1.21.1：现有 .venv 运行环境先摘出暂存、换入后放回——此前它随旧目录进入
    old 会被回滚清理一并删掉，导致面板进程起不来（203/EXEC 循环）。
    v1.22.1：暂存/放回改用 lexists 判断，避免 .venv 为符号链接时被静默丢弃；
    stash 一旦建立就必须放回，放回失败直接抛异常（交由上层回滚，不再静默跳过）。
    v1.24.4（P1-7）：换入前校验 stage/panel_migr 必须是目录。带外层目录的包
    （m7a_panel_V1.24.3/panel_migr/...）在这里明确失败，而不是「假成功」或
    在面板根目录留下一个垃圾目录。"""
    if not (stage / "panel_migr").is_dir():
        raise RuntimeError("发布包结构错误：zip 根必须直接是 panel_migr/（不得有外层目录）")
    old.mkdir(parents=True, exist_ok=True)
    venv_kept: list[Path] = []
    for item in sorted(stage.iterdir()):
        if item.name in _PROTECT_TOP or item.name.startswith("."):
            continue
        dest = cfg.BASE_DIR / item.name
        stash = None
        src_venv = dest / ".venv"
        if dest.is_dir() and os.path.lexists(src_venv):
            stash = old / f".venv_stash_{item.name}"
            if os.path.lexists(stash):
                if stash.is_dir() and not stash.is_symlink():
                    shutil.rmtree(stash, ignore_errors=True)
                else:
                    stash.unlink()
            shutil.move(str(src_venv), str(stash))
        # lexists：dest 是指向别处的符号链接时也要先移走，不能静默跳过
        if os.path.lexists(dest):
            shutil.move(str(dest), str(old / item.name))
        shutil.move(str(item), str(dest))
        if stash is not None:
            # stash 已建立：必须放回，失败即抛异常触发回滚（绝不静默丢弃运行环境）
            if not os.path.lexists(stash):
                raise RuntimeError(f"暂存的 .venv 丢失，无法放回：{item.name}")
            shutil.move(str(stash), str(dest / ".venv"))
            venv_kept.append(dest)
    for d in venv_kept:
        _venv_pip_sync(d)


def _venv_pip_sync(dest: Path) -> None:
    """复用现有 .venv 时按新 requirements.txt 对齐依赖（已装齐则秒回，失败不影响更新）。"""
    pip = dest / ".venv" / "bin" / "pip"
    req = dest / "requirements.txt"
    if not (pip.is_file() and req.is_file()):
        return
    try:
        run_cmd(f'"{pip}" install -q -r "{req}"', timeout=180)
    except Exception:
        pass


# 依赖安装回退源（默认源失败时用）
PIP_MIRROR = "https://pypi.tuna.tsinghua.edu.cn/simple"


def _runtime_python_ok(venv_dir: Path) -> bool:
    """运行环境是否可用：<venv>/bin/python 存在且有可执行权限。"""
    py = venv_dir / "bin" / "python"
    try:
        return py.is_file() and os.access(py, os.X_OK)
    except OSError:
        return False


def _ensure_runtime(runner=None, base_dir: Path | None = None) -> dict:
    """校验 / 重建面板运行环境（panel_migr/.venv）。

    v1.22.1：更新包不含 .venv，若更新开始前运行环境已缺失，换入新代码后
    systemd 会因找不到 panel_migr/.venv/bin/python 报 203/EXEC 反复重启。
    这里在换入后做兜底校验：缺失或不可执行则就地重建并装依赖。

    - 优先用当前解释器 sys.executable（已在跑，最贴近原环境）；
    - 不可用则回退 python3 -m venv；
    - pip 默认源失败回退清华源；
    - 全程不吞错误：命令返回码与输出都带进 msg。

    runner 可注入（测试用），签名 runner(cmd, timeout) -> {"code","out"}；
    默认用 services.shell.run_cmd。
    """
    run = runner or run_cmd
    base = Path(base_dir if base_dir is not None else cfg.BASE_DIR)
    venv_dir = base / "panel_migr" / ".venv"
    req = base / "panel_migr" / "requirements.txt"

    if _runtime_python_ok(venv_dir):
        return {"ok": True, "rebuilt": False, "msg": "运行环境就绪"}

    # —— 需要重建 ——
    log: list[str] = []
    try:
        venv_dir.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return {"ok": False, "rebuilt": False,
                "msg": f"运行环境目录不可创建：{e}"}

    made = False
    py_exe = getattr(sys, "executable", "") or ""
    if py_exe:
        r = run(f'"{py_exe}" -m venv "{venv_dir}"', 300)
        if r.get("code") == 0 and _runtime_python_ok(venv_dir):
            made = True
        else:
            log.append(f"用当前解释器创建 venv 失败：{r.get('out', '').strip()}")
    if not made:
        r = run(f'python3 -m venv "{venv_dir}"', 300)
        if r.get("code") == 0 and _runtime_python_ok(venv_dir):
            made = True
        else:
            log.append(f"python3 -m venv 失败：{r.get('out', '').strip()}")

    if not made:
        return {"ok": False, "rebuilt": False,
                "msg": "运行环境重建失败（无法创建 venv）：" + "；".join(filter(None, log))}

    # 装依赖：默认源 → 清华源回退
    pip = venv_dir / "bin" / "pip"
    if req.is_file():
        r = run(f'"{pip}" install -q -r "{req}"', 600)
        if r.get("code") != 0:
            log.append(f"默认源安装失败：{r.get('out', '').strip()}")
            r2 = run(f'"{pip}" install -q -r "{req}" -i {PIP_MIRROR}', 600)
            if r2.get("code") != 0:
                log.append(f"镜像源安装失败：{r2.get('out', '').strip()}")
    else:
        log.append(f"未找到依赖清单 {req}，跳过依赖安装")

    if not _runtime_python_ok(venv_dir):
        return {"ok": False, "rebuilt": True,
                "msg": "运行环境重建后校验仍失败：" + "；".join(filter(None, log))}

    if log:
        return {"ok": True, "rebuilt": True,
                "msg": "运行环境已重建（部分步骤有提示）：" + "；".join(filter(None, log))}
    return {"ok": True, "rebuilt": True, "msg": "运行环境已重建"}


def _restore_old(old: Path) -> dict:
    """回滚：把 old 里的原条目移回面板目录（覆盖换入失败的新条目）。

    v1.22.1：换入成功时 .venv 已放回新目录，回滚若直接 rmtree 新目录会连带删掉运行
    环境（面板下次重启即 203/EXEC）。因此先把当前 .venv 救出暂存、普通条目恢复完再
    放回；救出槽位放在 old 同级，即使放回失败也不会随 old 被清理掉。

    v1.24.4（P0-2）：返回结果字典而不是静默吞掉失败。调用方（_apply_zip）据此决定
    是否允许清理 old/ —— 回滚失败时 old/ 是旧代码的**唯一副本**，删掉就永久损坏。
    返回 {"ok": bool, "restored": int, "failed": int, "errors": [str, ...]}。
    """
    res = {"ok": True, "restored": 0, "failed": 0, "errors": []}
    if not old.is_dir():
        return res
    items = sorted(old.iterdir())
    rescue = old.parent / (old.name + ".venvrescue")
    rescued: list = []
    for item in items:
        if item.name.startswith(".venv_stash_"):
            continue
        dest = cfg.BASE_DIR / item.name
        cur_venv = dest / ".venv"
        if os.path.lexists(cur_venv):
            try:
                rescue.mkdir(parents=True, exist_ok=True)
                slot = rescue / item.name
                if os.path.lexists(slot):
                    if slot.is_dir() and not slot.is_symlink():
                        shutil.rmtree(slot, ignore_errors=True)
                    else:
                        slot.unlink()
                shutil.move(str(cur_venv), str(slot))
                rescued.append((item.name, slot))
            except OSError as e:
                res["failed"] += 1
                res["errors"].append(f"暂存 {item.name}/.venv 失败：{e}")
        try:
            if os.path.lexists(dest):
                if dest.is_dir() and not dest.is_symlink():
                    shutil.rmtree(dest, ignore_errors=True)
                else:
                    dest.unlink()
            shutil.move(str(item), str(dest))
            res["restored"] += 1
        except OSError as e:
            res["failed"] += 1
            res["errors"].append(f"恢复 {item.name} 失败：{e}")
    # 放回救出的运行环境（恢复后的目录里没有 .venv 时才补）
    for name, slot in rescued:
        target = cfg.BASE_DIR / name
        try:
            if not target.exists():
                target.mkdir(parents=True, exist_ok=True)
            venv_dst = target / ".venv"
            if not os.path.lexists(venv_dst) and os.path.lexists(slot):
                shutil.move(str(slot), str(venv_dst))
        except OSError as e:
            res["failed"] += 1
            res["errors"].append(f"放回 {name}/.venv 失败：{e}")
    # v1.21.1：普通条目恢复完后，把暂存的 .venv 放回对应目录（回滚后运行环境不丢）
    for item in items:
        if not item.name.startswith(".venv_stash_"):
            continue
        base_name = item.name[len(".venv_stash_"):]
        target = cfg.BASE_DIR / base_name
        try:
            if not target.exists():
                target.mkdir(parents=True, exist_ok=True)
            venv_dst = target / ".venv"
            if not os.path.lexists(venv_dst) and os.path.lexists(item):
                shutil.move(str(item), str(venv_dst))
        except OSError as e:
            res["failed"] += 1
            res["errors"].append(f"放回暂存 {base_name}/.venv 失败：{e}")
    # 空壳 rescue 目录清理（仍有残留槽位则保留，便于人工恢复）
    try:
        if rescue.is_dir() and not any(rescue.iterdir()):
            rescue.rmdir()
    except OSError:
        pass
    res["ok"] = res["failed"] == 0
    return res


def _cleanup_dirs(*dirs: Path) -> None:
    for p in dirs:
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)


def _old_has_content(old: Path) -> bool:
    """old/ 里是否已经搬进了东西（v1.24.5 Q-09）。

    `_swap_stage` 是逐条目 move 的，中途抛异常时可能已经搬走了一部分 —— 只要
    old/ 非空，线上代码就**可能**已被改动，必须走回滚；old/ 为空（或不存在）
    说明线上代码一个字节都没动过，此时不该声称「已回滚」。
    """
    try:
        if not old.is_dir():
            return False
        return any(old.iterdir())
    except OSError:
        # 读不准就按「动过」处理：宁可多回滚一次，也不能漏掉唯一副本。
        return True


def _live_code_intact(roots: list[Path], old: Path) -> tuple[bool, str]:
    """线上代码（任一候选根）是否「换入已完成」（v1.24.5 Q-06）。

    多个候选根时取**最严格**的结论：只要有任何一个根上有 panel_migr/ 却判为不完整，
    就返回不完整（宁可换回，也不删唯一副本）。
    """
    judged = 0
    last_why = "未找到 panel_migr/ 目录"
    for root in roots:
        try:
            if not (Path(root) / "panel_migr").is_dir():
                continue
        except OSError:
            continue
        judged += 1
        ok, why = _code_intact(root, old)
        if not ok:
            return False, f"{root}：{why}"
        last_why = why
    if judged == 0:
        return False, last_why
    return True, last_why


def _recover_interrupted() -> dict:
    """崩溃/断电后恢复：扫描 backups/ 下的 .old_* 与 .stage_* 残留（v1.24.4 P1-4）。

    此前更新中途被 SIGKILL（正是 P0-3 的死锁结局）会永久留下 `.old_*`/`.stage_*`：
    新代码可能只换入了一半，而唯一完整的旧代码躺在 .old_* 里没人管；下次更新又
    按时间戳生成新目录，旧残留永远堆积。这里在**持锁后**调用，规则：
      - `.stage_*`：换入前的临时解压目录，绝无保留价值 → 直接删；
      - `.old_*`：若线上代码**不完整**（见 `_code_intact`）→ 判定换入做到一半，
        把 .old_* 内容换回；换回成功才删残留，失败则原样保留；
      - `.old_*` 且线上代码完整 → 说明换入已完成、只是没来得及清理 → 删。

    v1.24.5（Q-06）：完整性判据从「`panel_migr/main.py` 存在且非空」升级为
    `_code_intact()`（关键文件集 + 版本号可读 + 无残留 .venv_stash）。旧判据下
    「main.py 已换入、其余还是半截」会被误判成换入完成，于是把唯一完整的
    .old_* 删掉 —— 旧代码永久丢失。任何一项读不准时一律按「不完整」处理，
    宁可多换回一次，也不删掉唯一副本。

    返回 {"recovered": [...], "removed": [...], "errors": [...], "notes": [...]}。
    """
    out: dict = {"recovered": [], "removed": [], "errors": [], "notes": []}
    try:
        d = backups_dir()
        entries = sorted(d.glob(".stage_*")) + sorted(d.glob(".old_*"))
    except OSError as e:
        out["errors"].append(str(e))
        return out
    # 换回目标与 _restore_old 一致（cfg.BASE_DIR）；分离布局下代码目录也要看。
    # 判据是「所有存在 panel_migr/ 的候选根都必须完整」：分离布局下换入写的是
    # BASE_DIR，若它被换了一半而 CODE_ROOT 是完好的，仍然要换回。
    roots = [Path(cfg.BASE_DIR)]
    try:
        cr = _code_root()
        if cr not in roots:
            roots.append(cr)
    except Exception:
        pass
    for p in entries:
        if not p.is_dir():
            continue
        if p.name.startswith(".stage_"):
            try:
                shutil.rmtree(p)
                out["removed"].append(p.name)
            except OSError as e:
                out["errors"].append(f"清理 {p.name} 失败：{e}")
            continue
        # .old_*：判断线上代码是否真的换入完成（Q-06 新判据）
        intact, why = _live_code_intact(roots, p)
        if intact:
            try:
                shutil.rmtree(p)
                out["removed"].append(p.name)
            except OSError as e:
                out["errors"].append(f"清理 {p.name} 失败：{e}")
            continue
        out["notes"].append(f"{p.name}：线上代码不完整（{why}），换回旧代码")
        # 关键文件缺失 → 换回旧代码
        try:
            res = _restore_old(p)
        except Exception as e:                       # noqa: BLE001 - 恢复失败要如实上报
            out["errors"].append(f"恢复 {p.name} 异常：{e}")
            continue
        if res.get("ok"):
            try:
                shutil.rmtree(p)
            except OSError:
                pass
            out["recovered"].append(p.name)
        else:
            out["errors"].extend(res.get("errors") or [])
    return out


def _schedule_restart() -> str:
    """占位：重启已在 _apply_zip 内同步完成（含健康探测）。
    此处只返回就绪提示，供 do_update 拼消息。"""
    return "，面板已重启并就绪"


def _self_under_unit() -> bool:
    """当前进程是否**就由** _RESTART_UNIT 这个 systemd 单元托管（v1.24.4 P0-3）。

    为什么必须区分：一键更新的调用链是 routers/write.py h_do_update →
    main.py:59 `await to_thread.run_sync(handler, request)`，即在**本进程自己的
    线程**里执行 `systemctl restart m7a-panel`。systemctl 等 uvicorn 优雅退出，
    uvicorn 等这个请求线程跑完，两边互等到 TimeoutStopSec=90s 被 SIGKILL ——
    重启后的健康探测与自动回滚永远是死代码。
    因此只有「确实会被自己重启掉」的进程才走 detach；pytest / 前台手工运行
    仍走同步路径（行为与 v1.24.1 一致，便于测试与手工验证）。
    """
    try:
        cg = Path("/proc/self/cgroup").read_text("utf-8", "replace")
    except OSError:
        return False
    return f"/{_RESTART_UNIT}.service" in cg


def _probe_python() -> str:
    """detach 脚本用的解释器：优先当前解释器，退回系统 python3。"""
    exe = sys.executable or ""
    try:
        if exe and Path(exe).exists():
            return exe
    except OSError:
        pass
    return shutil.which("python3") or "/usr/bin/python3"


def _write_probe_script() -> Path:
    """把自包含的健康探测/回滚脚本写到 /tmp（每次调用重写，避免陈旧版本）。"""
    p = Path(_HEALTH_PROBE_SCRIPT)
    try:
        p.write_text(_PROBE_SCRIPT_SRC, "utf-8")
        p.chmod(0o755)
    except OSError:
        pass
    return p


def _detach_restart_and_probe(port: int, retries: int, interval: int,
                              old: Path, stage: Path | None) -> dict:
    """把「restart → 轮询端口 → 失败回滚」交给 detach 出去的独立进程。

    v1.24.4（P0-3）。这里必须**立刻返回**：本进程马上会被 systemctl 杀掉，
    后续探测与回滚由 _HEALTH_PROBE_SCRIPT 那个独立进程完成，它持有 old/ 的
    清理权（成功删、失败保留现场）。因此本函数返回 ok=True 且 detach=True，
    _apply_zip 见到 detach 就不再同步清理 old/。

    两种 detach 手段，按「能不能活过本次 restart」排序：

    ① `systemd-run --no-block --unit=m7a-update-probe-<pid> ...`：把脚本放进
       **独立的 transient unit**，因此不在本服务的 cgroup 里。`systemctl restart`
       的默认 KillMode=control-group 只会杀本服务 cgroup 内的进程 —— 独立 unit
       的探测脚本不受影响，能完整跑完「restart → 轮询 → 失败回滚」。这是首选。
    ② 退化：`setsid ... &`。它脱离会话但**仍在同一 cgroup**，restart 时会被连带
       杀掉 —— 探测与回滚就失去保障。仅在 ① 不可用时使用（例如非 root 运行、
       polkit 拒绝 "Interactive authentication required"），此时 msg 会明确告知
       探测能力已降级，运维需人工确认新版本是否起来。
    """
    script = _write_probe_script()
    argv = [
        _probe_python(), str(script),
        str(cfg.BASE_DIR), str(old), str(stage or ""), str(port),
        str(retries), str(interval), _RESTART_UNIT,
    ]
    # 用单引号包裹每个参数，避免路径里的空格/特殊字符被 shell 拆开
    quoted = " ".join("'" + a.replace("'", "'\\''") + "'" for a in argv)

    # —— ① 独立 transient unit（首选，能活过 restart）——
    if shutil.which("systemd-run"):
        unit = f"m7a-update-probe-{os.getpid()}"
        cmd = f"systemd-run --no-block --collect --unit={unit} {quoted}"
        try:
            r = run_cmd(cmd, timeout=10)
        except Exception:
            r = {"code": 1, "out": ""}
        if r.get("code") == 0:
            return {
                "ok": True, "skipped": False, "probe_ok": False, "detach": True,
                "detach_mode": "systemd-run",
                "msg": (f"，面板正在重启（独立单元 {unit} 探测 127.0.0.1:{port}，"
                        f"失败会自动回滚并保留日志 {old}.probe.log）"),
            }
        # 失败（多为 polkit 权限）→ 落②，不把权限错误当成更新失败

    # —— ② 退化：setsid 脱离会话（不需要权限，但会被 cgroup 连带杀掉）——
    cmd = f"setsid {quoted} >/dev/null 2>&1 </dev/null &"
    try:
        r = run_cmd(cmd, timeout=10)
    except Exception as e:
        return {"ok": False, "skipped": False, "probe_ok": False, "detach": False,
                "msg": f"无法 detach 重启进程（{e}），请手动重启面板"}
    if r.get("code") != 0:
        return {"ok": False, "skipped": False, "probe_ok": False, "detach": False,
                "msg": (f"无法 detach 重启进程（{r.get('code')} "
                        f"{(r.get('out') or '').strip()[:120]}），请手动重启面板")}
    return {
        "ok": True, "skipped": False, "probe_ok": False, "detach": True,
        "detach_mode": "setsid",
        "msg": (f"，面板正在重启（setsid 探测 127.0.0.1:{port}；注意：本机无法建"
                f"独立 systemd 单元，探测进程可能随重启被杀，失败自动回滚不保证，"
                f"日志见 {old}.probe.log）"),
    }


def _restart_and_health_check(port: int = 0, retries: int = 3, interval: int = 2,
                              old: Path | None = None, stage: Path | None = None) -> dict:
    """更新后重启 m7a-panel 并确认本地端口就绪；失败返回失败原因。

    三种部署形态：
    - systemd 且**本进程就在该单元内**（正常部署）：detach 一个独立脚本去做
      「restart → 轮询 HTTP 127.0.0.1:<port>/ → 失败自动回滚」，本进程立即返回，
      随后被 systemd 杀掉也不影响探测与回滚（v1.24.4 P0-3）。
    - systemd 但本进程不在该单元内（pytest / 手工前台运行）：同步 restart +
      轮询 HTTP 127.0.0.1:<port>/ 直到 200 或超时，失败由 _apply_zip 回滚。
    - cron 看门狗 / 手动托管：不强行重启，返回 skipped=True 让上层提示用户手动处理。

    v1.24.4（P0-6）：端口默认取 cfg.LISTEN_PORT，不再硬编码 9999（部署普遍是 8787）。
    """
    port = int(port or cfg.LISTEN_PORT)
    try:
        r = run_cmd("systemctl is-active m7a-panel", timeout=10)
        first = (r["out"] or "").strip().split("\n")[0]
        systemd_active = r["code"] == 0 and first in ("active", "activating", "reloading")
    except Exception:
        systemd_active = False

    if not systemd_active:
        return {
            "ok": True, "skipped": True, "probe_ok": False,
            "msg": "未检测到 systemd 服务 m7a-panel，请手动重启面板后生效",
        }

    # —— 形态一：本进程就在该单元内 → detach，交给独立脚本 restart+探测+回滚 ——
    # 绝不能在这里原地 systemctl restart 自己：见 _self_under_unit() 的说明。
    if old is not None and _self_under_unit():
        return _detach_restart_and_probe(port, retries, interval, old, stage)

    # —— 形态二：systemd 托管但本进程不在单元内（测试 / 前台手工）→ 同步重启 + 探测 ——
    r = run_cmd(f"systemctl restart {_RESTART_UNIT}", timeout=30)
    if r.get("code") != 0:
        return {"ok": False, "skipped": False, "probe_ok": False,
                "msg": f"systemctl restart 失败：{(r.get('out') or '').strip()}"}

    for i in range(retries):
        time.sleep(interval)
        try:
            hr = http_get(f"http://127.0.0.1:{port}/", timeout=5)
            if hr["code"] == 200:
                return {"ok": True, "skipped": False, "probe_ok": True, "msg": ""}
        except Exception:
            pass

    # 失败：拉一条 systemd 最后几行日志方便定位
    journal = ""
    try:
        jr = run_cmd("journalctl -u m7a-panel -n 8 --no-pager", timeout=10)
        journal = (jr.get("out") or "").strip()
    except Exception:
        pass
    tail = f"；最近日志：{journal}" if journal else ""
    return {
        "ok": False, "skipped": False, "probe_ok": False,
        "msg": f"服务重启后健康探测失败（{retries} 次 × {interval}s 均未收到 HTTP 200）{tail}",
    }


def _apply_zip(data: bytes, expect_ver: str) -> dict:
    """校验 → 备份 → 解压换入 → 版本复核 → 运行环境兜底 → 重启+健康探测；
    任一步失败自动回滚。

    v1.22.1：版本复核通过后再校验/重建 panel_migr/.venv，失败走与版本复核失败
    完全相同的回滚路径（否则新代码换入但运行环境缺失 → 203/EXEC 起不来）。
    v1.24.1：换入 + 运行环境就绪后重启 m7a-panel 并轮询本地端口直到 HTTP 200；
    探测失败走回滚，绝不让「代码起不来 → 面板失联」再发生。
    v1.24.4：
    - P1-3：中间目录名带 pid + 随机后缀，同秒并发不再共用同一个 .stage_/.old_；
    - P0-2：回滚失败时**保留 old/ 现场**（它是旧代码唯一副本），文案如实告知；
    - P0-3：detach 形态下 old/ 的清理权移交给独立探测进程，此处不删。
    """
    ts = time.strftime("%Y%m%d%H%M%S")
    tag = f"{ts}_{os.getpid()}_{secrets.token_hex(2)}"
    stage = backups_dir() / f".stage_{tag}"
    old = backups_dir() / f".old_{tag}"
    rebuilt = False
    # v1.24.5（Q-09）：区分失败阶段。只有**线上代码真的被动过**，说「已回滚」才成立。
    # 解压 / 结构校验阶段失败时线上代码一个字节都没动过，此时报「更新失败已回滚」
    # 会让排障的人误以为发生过一次换入 + 回滚，去 backups/ 找并不存在的 .old_*。
    # 判据用「old/ 里是否有东西」而不是「_swap_stage 是否返回」：换入是逐条目
    # move，中途抛异常时可能已经搬走了一部分，那仍然必须回滚。
    phase = "extract"          # extract → swap → verify → runtime → health
    hr: dict = {"ok": True, "skipped": True, "probe_ok": False, "msg": ""}
    try:
        _extract_zip(data, stage)
        phase = "swap"
        _swap_stage(stage, old)
        phase = "verify"
        cur = _installed_version()
        if cur != expect_ver:
            raise RuntimeError(f"换入后版本校验失败（读到 {cur or '空'}，应为 {expect_ver}）")
        phase = "runtime"
        rt = _ensure_runtime()
        if not rt.get("ok"):
            raise RuntimeError(rt.get("msg") or "运行环境校验失败")
        rebuilt = bool(rt.get("rebuilt"))
        phase = "health"
        hr = _restart_and_health_check(old=old, stage=stage)
        if not hr.get("ok") and not hr.get("skipped"):
            raise RuntimeError(hr.get("msg") or "重启后健康探测失败")
    except Exception as e:
        # v1.24.5（Q-09）：解压/结构校验阶段失败 —— 线上代码未被触碰，谈不上回滚。
        # 直接清掉解压残渣，文案如实说明「尚未改动线上代码」。
        if not _old_has_content(old):
            # old/ 为空（或不存在）：_swap_stage 连一个条目都没搬走，删掉它也不会
            # 丢东西；留着空 .old_* 只会让下次 _recover_interrupted 多一次判断。
            _cleanup_dirs(stage, old)
            return {
                "ok": False, "rolled_back": False, "restored": 0, "phase": phase,
                "msg": f"更新失败（尚未改动线上代码）：{e}",
            }
        # v1.24.4（P0-2）：回滚结果必须如实反映到消息里。此前 _restore_old 的异常
        # 被 `except Exception: pass` 静默吞掉，紧接着 _cleanup_dirs 删掉 old/ ——
        # 旧代码唯一副本没了，文案还谎称「已回滚」。
        rb = {"ok": True, "restored": 0, "failed": 0, "errors": []}
        try:
            rb = _restore_old(old)
        except Exception as re:
            rb = {"ok": False, "restored": 0, "failed": 1, "errors": [str(re)]}
        if not rb.get("ok"):
            detail = "；".join(rb.get("errors") or []) or "未知原因"
            return {
                "ok": False, "rolled_back": False, "restored": int(rb.get("restored") or 0),
                "old_dir": str(old), "phase": phase,
                "msg": (f"更新失败，且自动回滚未完成：{e}（回滚错误：{detail}）。"
                        f"旧代码已保留在 {old}，请人工恢复后再重启面板"),
            }
        _cleanup_dirs(stage, old)
        return {"ok": False, "rolled_back": True, "restored": int(rb.get("restored") or 0),
                "phase": phase, "msg": f"更新失败已回滚：{e}"}
    if hr.get("detach"):
        # 重启/探测/回滚由独立进程完成，old/ 的清理权也在它手里：此处只清 stage/
        _cleanup_dirs(stage)
        return {"ok": True, "msg": hr.get("msg", ""), "rebuilt": rebuilt,
                "probe_ok": False, "skipped": False, "detach": True}
    _cleanup_dirs(stage, old)
    return {"ok": True, "msg": hr.get("msg", ""), "rebuilt": rebuilt,
            "probe_ok": hr.get("probe_ok", False), "skipped": hr.get("skipped", False)}


def do_update() -> dict:
    """在线更新入口：非阻塞获取更新互斥锁后执行（v1.24.4 P1-3）。

    两次更新撞在同一秒时，旧代码会用同一个 `.stage_<ts>`/`.old_<ts>` 目录名，
    互相 rmtree 对方正在使用的目录。这里在入口处非阻塞拿锁，拿不到直接拒绝，
    不排队（排队会让第二个请求在面板被杀后悬空）。
    """
    if not _UPDATE_LOCK.acquire(blocking=False):
        return {"ok": False, "msg": "已有更新正在进行，请稍后再试"}
    try:
        return _do_update_locked()
    finally:
        _UPDATE_LOCK.release()


def _do_update_locked() -> dict:
    """在线更新：v1.20 起下载 Release 整包 ZIP 自更新；
    旧版 release（无 zip 资产，PHP 时代）回退 index.php 流程。"""
    if not cfg.UPDATE_ENABLED:
        return {"ok": False, "msg": "未启用自动更新"}

    # v1.24.4（P1-4）：先清理/恢复上次被 SIGKILL 打断留下的 .stage_*/.old_*。
    # 必须持锁后做，否则可能误删另一条更新线程正在使用的目录。
    try:
        _recover_interrupted()
    except Exception:
        pass

    j, rerr = _release_json()
    if isinstance(j, dict):
        zip_url, asset = _zip_asset(j)
        if zip_url:
            # v1.24.3 安全加固（M7A-01）：以 Release 资产的 sha256 为唯一真实性锚点。
            # 有 digest → 任何来源（含第三方镜像）都必须哈希一致才换入；
            # 无 digest → 第三方镜像运营方不可问责，只接受官方源。
            want = _asset_digest(j, asset)
            errors: list[str] = []
            data = None
            src = "官方源"
            for name, url in _zip_urls(zip_url):
                if want == "" and name != "官方源":
                    continue
                b = http_bytes(url, 60)
                if b is None:
                    errors.append(f"{name}下载失败")
                    continue
                if len(b) < 20_000:
                    errors.append(f"{name}包体异常({len(b)}B)")
                    continue
                if want:
                    got = _sha256(b)
                    if not hmac.compare_digest(got, want):
                        errors.append(
                            f"{name}完整性校验失败（sha256 {got[:12]}… ≠ 期望 {want[:12]}…）"
                        )
                        continue
                data, src = b, name
                break
            if data is None:
                detail = "；".join(errors)
                return {"ok": False,
                        "msg": f"所有更新源下载失败或未通过完整性校验（{detail}）。"
                               f"请检查服务器网络，或在服务器配置代理后重试"}
            if not _zip_struct_ok(data):
                return {"ok": False, "msg": "更新包结构异常（缺少 panel_migr 主程序），已中止"}
            try:
                newver = _zip_new_version(data)
            except Exception as e:
                return {"ok": False, "msg": f"更新包校验失败（{e}），已中止"}
            latest = str(j.get("tag_name") or "").lstrip("vV")
            if latest and not _version_eq(latest, newver):
                return {"ok": False,
                        "msg": f"更新包版本（{newver}）与 Release 版本（{latest}）不一致，已中止"}
            bak = _backup_panel_zip(cfg.PANEL_VERSION)
            if bak == "":
                # v1.24.4（P1-5）：如实转述真实原因。此前一律甩「请检查 backups
                # 目录权限」，而在代码/数据分离布局下真正的原因是「一个文件都没
                # 打包到」，运维会照着权限方向白查。
                why = _LAST_BACKUP_ERR or "未知原因"
                return {"ok": False, "msg": f"备份当前面板失败：{why}，已中止更新"}
            res = _apply_zip(data, newver)
            if not res["ok"]:
                return res
            tail = _schedule_restart()
            rt_tip = "，运行环境已重建" if res.get("rebuilt") else ""
            vtip = "，sha256 校验通过" if want else "，官方源直连（该 Release 未提供哈希）"
            return {"ok": True,
                    "msg": (f"已更新到 v{newver}（来源：{src}，资产 {asset}）{vtip}"
                            f"{tail}{rt_tip}；旧版本已备份为 {Path(bak).name}")}

    # —— 旧版回退：PHP release 的 index.php 流程（行为与旧版一致）——
    errors = []
    if j is None:
        errors.append(f"release 查询失败（{rerr}）")
    for i, url in enumerate(update_raw_urls()):
        src = "官方源" if i == 0 else f"加速镜像{i}"
        r = http_get(url, 15)
        if r["code"] != 200 or not r["body"]:
            errors.append(f"{src} HTTP {r['code']}")
            continue
        content = r["body"]
        if not content.lstrip()[:5].lower() == "<?php":
            errors.append(f"{src}内容校验失败")
            continue
        bak = backup_current_index(cfg.PANEL_VERSION)
        if bak == "":
            return {"ok": False, "msg": "备份当前文件失败（请检查 backups 目录权限），已中止更新"}
        try:
            INDEX_FILE.write_text(content, "utf-8")
        except OSError:
            try:
                shutil.copyfile(bak, INDEX_FILE)
            except OSError:
                pass
            return {"ok": False, "msg": "写入新版本失败，已回滚到备份"}
        return {
            "ok": True,
            "msg": f"更新完成（来源：{src}），旧版本已备份为 {Path(bak).name}，页面即将刷新",
            "bak": Path(bak).name,
        }
    detail = "；".join(errors)
    return {"ok": False, "msg": f"所有更新源下载失败（{detail}）。请检查服务器网络，或在服务器配置代理后重试"}


# ===== 小助手镜像 =====

def _ghcr_auth(token: str) -> str:
    """GHCR 鉴权头的**唯一**构造点（v1.24.4 P1-6）。

    此前 ghcr_latest_created 里先拼好 `auth = "Bearer " + token`，再调用
    ghcr_config_created("Authorization: " + auth, ...)，而后者内部又写成
    headers={"Authorization": auth} → 实际发出
    `Authorization: Authorization: Bearer ey...`，blob 请求必然 401，
    镜像构建时间恒为空。现在统一在这里加一次前缀，调用方只传裸 token。
    """
    t = (token or "").strip()
    if t.lower().startswith("authorization:"):
        t = t.split(":", 1)[1].strip()
    if t.lower().startswith("bearer "):
        t = t[7:].strip()
    return "Bearer " + t


def ghcr_config_created(auth: str, base: str, digest: str, timeout: int = 10) -> str:
    """取镜像 config blob 的 created 时间；auth 只传裸 token（前缀由 _ghcr_auth 加）。"""
    try:
        r = httpx.get(
            base + "/blobs/" + digest,
            headers={"Authorization": _ghcr_auth(auth),
                     "Accept": "application/vnd.oci.image.config.v1+json"},
            timeout=httpx.Timeout(timeout, connect=5), follow_redirects=True,
        )
        j = r.json() if r.status_code == 200 else None
        return str(j.get("created") or "") if isinstance(j, dict) else ""
    except Exception:
        return ""


def ghcr_latest_created(timeout: int = 10) -> str:
    """查 GHCR latest 镜像构建时间（token → manifest → config blob）。"""
    repo = IMAGE_REPO
    base = f"https://ghcr.io/v2/{repo}"
    hdr = {"Accept": "application/json", "User-Agent": UA}
    try:
        r = httpx.get(
            f"https://ghcr.io/token?scope=repository:{repo}:pull",
            headers=hdr, timeout=httpx.Timeout(timeout, connect=5),
        )
        token = str((r.json() or {}).get("token") or "")
        if token == "":
            return ""
        ah = dict(hdr)
        ah["Authorization"] = _ghcr_auth(token)   # v1.24.4 P1-6：唯一构造点
        ah["Accept"] = ",".join([
            "application/vnd.oci.image.index.v1+json",
            "application/vnd.docker.distribution.manifest.list.v2+json",
            "application/vnd.oci.image.manifest.v1+json",
            "application/vnd.docker.distribution.manifest.v2+json",
        ])
        r2 = httpx.get(base + "/manifests/latest", headers=ah,
                       timeout=httpx.Timeout(timeout, connect=5))
        if r2.status_code != 200:
            return ""
        j = r2.json()
        if not isinstance(j, dict):
            return ""
        if isinstance(j.get("config"), dict) and j["config"].get("digest"):
            return ghcr_config_created(token, base, j["config"].get("digest"), timeout)
        manifests = j.get("manifests") or []
        digest = manifests[0].get("digest", "") if manifests else ""
        if digest == "":
            return ""
        ah2 = dict(hdr)
        ah2["Authorization"] = _ghcr_auth(token)  # v1.24.4 P1-6：唯一构造点
        ah2["Accept"] = ",".join([
            "application/vnd.oci.image.index.v1+json",
            "application/vnd.docker.distribution.manifest.list.v2+json",
            "application/vnd.oci.image.manifest.v1+json",
            "application/vnd.docker.distribution.manifest.v2+json",
        ])
        r3 = httpx.get(base + "/manifests/" + digest, headers=ah2,
                       timeout=httpx.Timeout(timeout, connect=5))
        if r3.status_code != 200:
            return ""
        j3 = r3.json()
        if not isinstance(j3, dict):
            return ""
        cfgdg = ((j3 or {}).get("config") or {}).get("digest", "")
        if not cfgdg:
            return ""
        return ghcr_config_created(token, base, cfgdg, timeout)
    except Exception:
        return ""


def assistant_image_candidates(image: str) -> list[str]:
    rest = ""
    low = image.lower()
    for pre in GHCR_PREFIXES:
        if low.startswith(pre):
            rest = image[len(pre):]
            break
    if rest == "":
        return [image]
    out = []
    for pre in GHCR_PREFIXES:
        cand = pre + rest
        if cand not in out:
            out.append(cand)
    return out


def image_update(inst: dict) -> dict:
    """依次官方源 + 加速镜像拉取小助手镜像 → tag 回原名 → 重建容器。"""
    from shlex import quote
    r1 = run_cmd(f'docker inspect --format "{{{{.Config.Image}}}}" {quote(instance_container(inst))}')
    image = r1["out"].strip()
    low = image.lower()
    if r1["code"] != 0 or image == "" or "error" in low or "not found" in low:
        return {"ok": False, "err": "无法获取当前容器镜像（容器未运行？）：" + image}
    cands = assistant_image_candidates(image)
    used = ""
    detail = []
    for i, cand in enumerate(cands):
        r = run_cmd(f"docker pull {quote(cand)}", timeout=1800)
        if r["code"] == 0:
            used = cand
            break
        detail.append(("官方源" if i == 0 else f"加速镜像{i}") + "失败：" + r["out"].strip())
    if used == "":
        return {"ok": False, "err": "所有镜像源拉取失败：" + " | ".join(detail)}
    if used != image:
        run_cmd(f"docker tag {quote(used)} {quote(image)}")
    r2 = _compose(inst, "up -d")
    if r2["code"] != 0:
        return {"ok": False, "err": "镜像已拉取但容器重建失败：" + r2["out"].strip()}
    try:
        IMAGE_CACHE.unlink()
    except OSError:
        pass
    src_name = "镜像源"
    labels = {
        "ghcr.io/": "官方源", "ghcr.nju.edu.cn/": "南大镜像",
        "ghcr.m.daocloud.io/": "DaoCloud", "ghcr.dockerproxy.com/": "dockerproxy",
    }
    ulow = used.lower()
    for pre, label in labels.items():
        if ulow.startswith(pre):
            src_name = label
            break
    return {"ok": True, "src": src_name, "msg": f"镜像已更新（{src_name}），容器已重建"}


def image_check(inst: dict, force: bool = False) -> dict:
    if not force and IMAGE_CACHE.is_file():
        try:
            c = json.loads(IMAGE_CACHE.read_text("utf-8"))
        except (OSError, ValueError):
            c = None
        if isinstance(c, dict) and int(c.get("ts") or 0) and int(time.time()) - int(c["ts"]) < 21600:
            return c
    from shlex import quote as _q
    r1 = run_cmd(f'docker inspect --format "{{{{.Config.Image}}}}" {_q(instance_container(inst))}')
    image = r1["out"].strip()
    low = image.lower()
    if r1["code"] != 0 or image == "" or "error" in low or "not found" in low:
        err = image
        data = {
            "ok": False,
            "err": "无法获取容器镜像" + (f"：{err}" if err != "" else "（容器未运行？）"),
            "ts": int(time.time()),
        }
        _write_cache(data)
        return data
    r2 = run_cmd(f'docker image inspect --format "{{{{.Created}}}}" {_q(image)}')
    local = r2["out"].strip()
    remote = ghcr_latest_created(10)
    has_update = False
    lt = _to_ts(local)
    rt = _to_ts(remote)
    if lt and rt and rt > lt + 3600:
        has_update = True
    data = {
        "ok": True, "image": image, "local": local, "remote": remote,
        "remote_unknown": remote == "", "has_update": has_update, "ts": int(time.time()),
    }
    _write_cache(data)
    return data


def _write_cache(data: dict) -> None:
    try:
        IMAGE_CACHE.write_text(json.dumps(data, ensure_ascii=False), "utf-8")
    except OSError:
        pass


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


# ===== 版本备份 / 回滚 =====

def backups_dir() -> Path:
    try:
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return BACKUP_DIR


def _entry(f: Path, kind: str, ver: str, t: str) -> dict:
    try:
        ts = int(time.mktime((int(t[0:4]), int(t[4:6]), int(t[6:8]),
                              int(t[8:10]), int(t[10:12]), int(t[12:14]), 0, 0, -1)))
    except ValueError:
        ts = 0
    if ts <= 0:
        try:
            ts = int(f.stat().st_mtime)
        except OSError:
            ts = 0
    return {
        "file": f.name, "path": str(f), "version": ver, "time": ts, "kind": kind,
        "timeStr": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)) if ts > 0 else "未知",
        "size": f.stat().st_size if f.exists() else 0,
    }


def backups_list() -> list[dict]:
    """备份列表：index_*.php（旧版单文件）与 panel_*.zip（v1.20 整包）都识别。"""
    out = []
    d = backups_dir()
    if not d.is_dir():
        return out
    for f in sorted(d.glob("index_*.php")):
        m = re.match(r"^index_(.*)_(\d{14})\.php$", f.name)
        if not m:
            continue
        out.append(_entry(f, "index", m.group(1) or "未知", m.group(2)))
    for f in sorted(d.glob("panel_*.zip")):
        m = re.match(r"^panel_(.*)_(\d{14})\.zip$", f.name)
        if not m:
            continue
        out.append(_entry(f, "panel", m.group(1) or "未知", m.group(2)))
    out.sort(key=lambda x: (x["time"], x["file"]), reverse=True)
    return out


def backups_prune(keep: int | None = None) -> int:
    keep = max(1, int(cfg.BACKUP_KEEP if keep is None else keep))
    lst = backups_list()
    if len(lst) <= keep:
        return 0
    removed = 0
    for item in lst[keep:]:
        p = Path(item["path"])
        try:
            p.unlink()
            removed += 1
        except OSError:
            pass
    return removed


def backup_current_index(version: str = "") -> str:
    d = backups_dir()
    if not d.is_dir():
        return ""
    ver = (version or cfg.PANEL_VERSION).strip()
    ver = re.sub(r"[^A-Za-z0-9._-]", "", ver) or "unknown"
    target = d / f"index_{ver}_{time.strftime('%Y%m%d%H%M%S')}.php"
    try:
        if INDEX_FILE.is_file():
            shutil.copyfile(INDEX_FILE, target)
        else:
            return ""
    except OSError:
        return ""
    try:
        target.chmod(0o644)
    except OSError:
        pass
    backups_prune()
    return str(target)


def _rollback_panel(base: str) -> dict:
    """一键回滚整包备份：backups/panel_*.zip → 换入面板目录。"""
    d = backups_dir()
    target = d / base
    if not target.is_file():
        return {"ok": False, "msg": "备份文件不存在，已中止回滚"}
    try:
        real_dir = d.resolve()
        real_target = target.resolve()
        if not str(real_target).startswith(str(real_dir) + "/"):
            return {"ok": False, "msg": "备份文件路径非法，已中止回滚"}
        data = target.read_bytes()
    except OSError:
        return {"ok": False, "msg": "备份文件读取失败，已中止回滚"}
    if not _zip_struct_ok(data):
        return {"ok": False, "msg": "备份内容异常（不是有效的面板整包），已中止回滚"}
    try:
        ver = _zip_new_version(data)
    except Exception as e:
        return {"ok": False, "msg": f"备份内容异常（{e}），已中止回滚"}
    safety = _backup_panel_zip(cfg.PANEL_VERSION)   # 回滚前先快照当前版本
    res = _apply_zip(data, ver)
    if not res["ok"]:
        return res
    tail = _schedule_restart()
    tip = f"（回滚前的版本已备份为 {Path(safety).name}）" if safety else ""
    return {"ok": True, "msg": f"已回滚到备份 {base}（v{ver}）{tail}{tip}", "file": base}


def backup_rollback(file: str) -> dict:
    """一键回滚：panel_*.zip（v1.20 整包）或 index_*.php（旧版单文件）。"""
    base = Path(str(file).strip()).name
    if base == "":
        return {"ok": False, "msg": "备份文件名不合法，已中止回滚"}
    if re.match(r"^panel_.*\.zip$", base):
        return _rollback_panel(base)
    if not re.match(r"^index_.*\.php$", base):
        return {"ok": False, "msg": "备份文件名不合法，已中止回滚"}
    d = backups_dir()
    target = d / base
    if not target.is_file():
        return {"ok": False, "msg": "备份文件不存在，已中止回滚"}
    try:
        real_dir = d.resolve()
        real_target = target.resolve()
        if not str(real_target).startswith(str(real_dir) + "/"):
            return {"ok": False, "msg": "备份文件路径非法，已中止回滚"}
        content = target.read_text("utf-8", "replace")
    except OSError:
        return {"ok": False, "msg": "备份文件路径非法，已中止回滚"}
    if len(content) < 200 or not content.lstrip()[:5].lower() == "<?php":
        return {"ok": False, "msg": "备份内容异常（不是有效的面板文件），已中止回滚"}
    safety = backup_current_index(cfg.PANEL_VERSION)
    try:
        INDEX_FILE.write_text(content, "utf-8")
    except OSError:
        return {"ok": False, "msg": "写入面板主文件失败，请检查面板目录权限"}
    tail = f"（回滚前的版本已备份为 {Path(safety).name}）" if safety else ""
    return {"ok": True, "msg": f"已回滚到备份 {base}{tail}，页面即将刷新", "file": base}


# ===== 容器 compose 复用（image_update 内部用）=====

def _compose(inst: dict, args: str) -> dict:
    from shlex import quote
    return run_cmd(f"cd {quote(instance_dir(inst))} && docker compose {args}")
