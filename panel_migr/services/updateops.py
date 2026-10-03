"""自动更新 / 镜像更新 / 版本备份回滚 —— 等价 PHP update_*/image_*/backup_* 全套。

v1.20 起自动更新改为「Release 整包 ZIP 自更新」（旧版 PHP release 仍保留 index.php
回退流程）：下载 V*.zip → 校验结构与版本一致性 → 整面板备份到 backups/panel_*.zip
→ 解压换入（data/ backups/ 运行数据不动）→ 版本复核（失败自动回滚）→ 延迟重启。

网络请求走 httpx（同步，handler 在线程池执行）；失败语义与 PHP curl 版一致：
code=0 表示连接失败，body='' 表示无内容。
"""
from __future__ import annotations

import io
import json
import re
import shutil
import time
import zipfile
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
_PROTECT_TOP = ("data", "backups")
_PROTECT_DIRS = {"__pycache__", ".venv", "venv", "node_modules", "_tmp", ".git"}


# ===== HTTP（等价 PHP http_get / http_post）=====

def http_get(url: str, timeout: int = 8) -> dict:
    try:
        r = httpx.get(
            url, timeout=httpx.Timeout(timeout, connect=5), follow_redirects=True,
            headers={"User-Agent": UA}, verify=False,
        )
        return {"code": r.status_code, "body": r.text}
    except Exception:
        return {"code": 0, "body": ""}


def http_bytes(url: str, timeout: int = 60) -> bytes | None:
    """下载二进制（更新包）；失败 → None。"""
    try:
        r = httpx.get(
            url, timeout=httpx.Timeout(timeout, connect=10), follow_redirects=True,
            headers={"User-Agent": UA}, verify=False,
        )
        if r.status_code == 200 and r.content:
            return r.content
    except Exception:
        pass
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
            timeout=httpx.Timeout(timeout, connect=5), follow_redirects=True, verify=False,
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
    """包内必须有 panel_migr 主程序与前端资源。"""
    try:
        names = [n.replace("\\", "/") for n in zipfile.ZipFile(io.BytesIO(data)).namelist()]
    except (zipfile.BadZipFile, OSError):
        return False
    need = ("panel_migr/main.py", "panel_migr/config.py", "panel_migr/static/panel.js")
    return all(any(n.endswith(x) for n in names) for x in need)


def _zip_new_version(data: bytes) -> str:
    """从 ZIP 内 panel_migr/config.py 提取新版本号；缺失 → 抛异常。"""
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for name in z.namelist():
            if name.replace("\\", "/").endswith("panel_migr/config.py"):
                v = _version_from_text(z.read(name).decode("utf-8", "replace"))
                if v:
                    return v
    raise RuntimeError("包内缺少 panel_migr/config.py")


def _installed_version() -> str:
    try:
        txt = (cfg.BASE_DIR / "panel_migr" / "config.py").read_text("utf-8", "replace")
    except OSError:
        return ""
    return _version_from_text(txt)


def _extract_zip(data: bytes, stage: Path) -> None:
    stage.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for info in z.infolist():
            nm = info.filename.replace("\\", "/")
            if nm.startswith("/") or ".." in nm.split("/"):
                raise RuntimeError(f"包内路径非法：{nm}")
        z.extractall(stage)


def _zip_tree(zf: zipfile.ZipFile, root: Path, arc_root: str) -> int:
    n = 0
    try:
        items = sorted(root.rglob("*"))
    except OSError:
        return 0
    for p in items:
        rel = p.relative_to(root)
        if any(part in _PROTECT_DIRS for part in rel.parts):
            continue
        if p.is_file():
            try:
                zf.write(p, str(Path(arc_root) / rel))
                n += 1
            except OSError:
                pass
    return n


def _backup_panel_zip(version: str = "") -> str:
    """整面板快照 → backups/panel_<ver>_<ts>.zip（不含 data/ backups/ 运行数据）。
    成功返回路径，失败返回 ""（与 backup_current_index 语义一致）。"""
    d = backups_dir()
    if not d.is_dir():
        return ""
    ver = re.sub(r"[^A-Za-z0-9._-]", "", (version or cfg.PANEL_VERSION).strip()) or "unknown"
    target = d / f"panel_{ver}_{time.strftime('%Y%m%d%H%M%S')}.zip"
    try:
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
            n = 0
            for item in sorted(cfg.BASE_DIR.iterdir()):
                if item.name in _PROTECT_TOP or item.name.startswith("."):
                    continue
                if item.is_dir():
                    n += _zip_tree(zf, item, item.name)
                elif item.is_file():
                    try:
                        zf.write(item, item.name)
                        n += 1
                    except OSError:
                        pass
        if n == 0 or target.stat().st_size < 200:
            target.unlink()
            return ""
    except OSError:
        try:
            target.unlink()
        except OSError:
            pass
        return ""
    backups_prune()
    return str(target)


def _swap_stage(stage: Path, old: Path) -> None:
    """stage 内一级条目换入面板目录；被替换的条目移入 old 供失败回滚。
    data/ backups/ 等运行数据永不触碰。
    v1.21.1：现有 .venv 运行环境先摘出暂存、换入后放回——此前它随旧目录进入
    old 会被回滚清理一并删掉，导致面板进程起不来（203/EXEC 循环）。"""
    old.mkdir(parents=True, exist_ok=True)
    venv_kept: list[Path] = []
    for item in sorted(stage.iterdir()):
        if item.name in _PROTECT_TOP or item.name.startswith("."):
            continue
        dest = cfg.BASE_DIR / item.name
        stash = None
        src_venv = dest / ".venv"
        if dest.is_dir() and src_venv.is_dir():
            stash = old / f".venv_stash_{item.name}"
            if stash.exists():
                shutil.rmtree(stash, ignore_errors=True)
            shutil.move(str(src_venv), str(stash))
        if dest.exists():
            shutil.move(str(dest), str(old / item.name))
        shutil.move(str(item), str(dest))
        if stash is not None and stash.exists():
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


def _restore_old(old: Path) -> None:
    """回滚：把 old 里的原条目移回面板目录（覆盖换入失败的新条目）。"""
    if not old.is_dir():
        return
    items = sorted(old.iterdir())
    for item in items:
        if item.name.startswith(".venv_stash_"):
            continue
        dest = cfg.BASE_DIR / item.name
        try:
            if dest.exists():
                if dest.is_dir() and not dest.is_symlink():
                    shutil.rmtree(dest, ignore_errors=True)
                else:
                    dest.unlink()
            shutil.move(str(item), str(dest))
        except OSError:
            pass
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
            if not venv_dst.exists() and item.exists():
                shutil.move(str(item), str(venv_dst))
        except OSError:
            pass


def _cleanup_dirs(*dirs: Path) -> None:
    for p in dirs:
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)


def _schedule_restart() -> str:
    """更新完成后延迟 2 秒重启面板（systemd m7a-panel），让当前 HTTP 响应先返回。"""
    try:
        r = run_cmd("systemctl is-active m7a-panel")
        first = (r["out"] or "").strip().split("\n")[0]
        active = r["code"] == 0 and first in ("active", "activating", "reloading")
    except Exception:
        active = False
    if not active:
        return "；未检测到 systemd 服务 m7a-panel，请手动重启面板进程后生效"
    run_cmd("(sleep 2; systemctl restart m7a-panel) >/dev/null 2>&1 &", timeout=5)
    return "，2 秒后面板将自动重启生效"


def _apply_zip(data: bytes, expect_ver: str) -> dict:
    """校验 → 备份 → 解压换入 → 版本复核；任一步失败自动回滚。"""
    ts = time.strftime("%Y%m%d%H%M%S")
    stage = backups_dir() / f".stage_{ts}"
    old = backups_dir() / f".old_{ts}"
    try:
        _extract_zip(data, stage)
        _swap_stage(stage, old)
        cur = _installed_version()
        if cur != expect_ver:
            raise RuntimeError(f"换入后版本校验失败（读到 {cur or '空'}，应为 {expect_ver}）")
    except Exception as e:
        try:
            _restore_old(old)
        except Exception:
            pass
        _cleanup_dirs(stage, old)
        return {"ok": False, "msg": f"更新失败已回滚：{e}"}
    _cleanup_dirs(stage, old)
    return {"ok": True, "msg": ""}


def do_update() -> dict:
    """在线更新：v1.20 起下载 Release 整包 ZIP 自更新；
    旧版 release（无 zip 资产，PHP 时代）回退 index.php 流程。"""
    if not cfg.UPDATE_ENABLED:
        return {"ok": False, "msg": "未启用自动更新"}

    j, rerr = _release_json()
    if isinstance(j, dict):
        zip_url, asset = _zip_asset(j)
        if zip_url:
            errors: list[str] = []
            data = None
            src = "官方源"
            for name, url in _zip_urls(zip_url):
                b = http_bytes(url, 60)
                if b is None:
                    errors.append(f"{name}下载失败")
                    continue
                if len(b) < 20_000:
                    errors.append(f"{name}包体异常({len(b)}B)")
                    continue
                data, src = b, name
                break
            if data is None:
                detail = "；".join(errors)
                return {"ok": False,
                        "msg": f"所有更新源下载失败（{detail}）。请检查服务器网络，或在服务器配置代理后重试"}
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
                return {"ok": False, "msg": "备份当前面板失败（请检查 backups 目录权限），已中止更新"}
            res = _apply_zip(data, newver)
            if not res["ok"]:
                return res
            tail = _schedule_restart()
            return {"ok": True,
                    "msg": (f"已更新到 v{newver}（来源：{src}，资产 {asset}）{tail}；"
                            f"旧版本已备份为 {Path(bak).name}")}

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

def ghcr_config_created(auth: str, base: str, digest: str, timeout: int = 10) -> str:
    try:
        r = httpx.get(
            base + "/blobs/" + digest,
            headers={"Authorization": auth, "Accept": "application/vnd.oci.image.config.v1+json"},
            timeout=httpx.Timeout(timeout, connect=5), follow_redirects=True, verify=False,
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
            headers=hdr, timeout=httpx.Timeout(timeout, connect=5), verify=False,
        )
        token = str((r.json() or {}).get("token") or "")
        if token == "":
            return ""
        auth = "Bearer " + token
        ah = dict(hdr)
        ah["Authorization"] = auth
        ah["Accept"] = ",".join([
            "application/vnd.oci.image.index.v1+json",
            "application/vnd.docker.distribution.manifest.list.v2+json",
            "application/vnd.oci.image.manifest.v1+json",
            "application/vnd.docker.distribution.manifest.v2+json",
        ])
        r2 = httpx.get(base + "/manifests/latest", headers=ah,
                       timeout=httpx.Timeout(timeout, connect=5), verify=False)
        if r2.status_code != 200:
            return ""
        j = r2.json()
        if not isinstance(j, dict):
            return ""
        if isinstance(j.get("config"), dict) and j["config"].get("digest"):
            return ghcr_config_created("Authorization: " + auth, base, j["config"].get("digest"), timeout)
        manifests = j.get("manifests") or []
        digest = manifests[0].get("digest", "") if manifests else ""
        if digest == "":
            return ""
        ah2 = dict(hdr)
        ah2["Authorization"] = auth
        ah2["Accept"] = ",".join([
            "application/vnd.oci.image.index.v1+json",
            "application/vnd.docker.distribution.manifest.list.v2+json",
            "application/vnd.oci.image.manifest.v1+json",
            "application/vnd.docker.distribution.manifest.v2+json",
        ])
        r3 = httpx.get(base + "/manifests/" + digest, headers=ah2,
                       timeout=httpx.Timeout(timeout, connect=5), verify=False)
        if r3.status_code != 200:
            return ""
        j3 = r3.json()
        if not isinstance(j3, dict):
            return ""
        cfgdg = ((j3 or {}).get("config") or {}).get("digest", "")
        if not cfgdg:
            return ""
        return ghcr_config_created("Authorization: " + auth, base, cfgdg, timeout)
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
    """strtotime() 子集：ISO8601 / RFC 时间 → 时间戳；失败 False。"""
    from datetime import datetime
    s = (s or "").strip()
    if not s:
        return False
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            dt = datetime.strptime(s[:25].replace("Z", "+0000"), fmt)
            return int(dt.timestamp())
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
