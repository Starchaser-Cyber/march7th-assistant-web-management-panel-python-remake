"""在线更新编排：do_update → 下载校验 → 备份 → 换入 → 重启。"""
from __future__ import annotations

import hmac
import os
import secrets
from pathlib import Path



from . import _rt
import contextlib


def do_update() -> dict:
    """在线更新入口：非阻塞获取更新互斥锁后执行（v1.24.4 P1-3）。

    两次更新撞在同一秒时，旧代码会用同一个 `.stage_<ts>`/`.old_<ts>` 目录名，
    互相 rmtree 对方正在使用的目录。这里在入口处非阻塞拿锁，拿不到直接拒绝，
    不排队（排队会让第二个请求在面板被杀后悬空）。
    """
    if not _rt._UPDATE_LOCK.acquire(blocking=False):
        return {"ok": False, "msg": "已有更新正在进行，请稍后再试"}
    try:
        return _rt._do_update_locked()
    finally:
        _rt._UPDATE_LOCK.release()


def _do_update_locked() -> dict:
    """在线更新入口：先恢复崩溃残留，再按 Release 形态分派（v1.24.7 削复杂度）。

    拆成三个阶段函数，本函数只负责「判断走哪条路」：
      - `_update_from_zip_asset`：v1.20 起的整包 ZIP 自更新（正常路径）；
      - `_update_from_php_source`：旧版 PHP release 的 index.php 回退流程。
    """
    if not _rt.cfg.UPDATE_ENABLED:
        return {"ok": False, "msg": "未启用自动更新"}

    # v1.24.4（P1-4）：先清理/恢复上次被 SIGKILL 打断留下的 .stage_*/.old_*。
    # 必须持锁后做，否则可能误删另一条更新线程正在使用的目录。
    with contextlib.suppress(Exception):
        _rt._recover_interrupted()

    j, rerr = _rt._release_json()
    if isinstance(j, dict):
        zip_url, asset = _rt._zip_asset(j)
        if zip_url:
            return _update_from_zip_asset(j, zip_url, asset)
    return _update_from_php_source(j, rerr)


def _download_verified_zip(zip_url: str, want: str) -> dict:
    """按源顺序下载包体并做 sha256 锚点校验。

    v1.24.3 安全加固（M7A-01）：以 Release 资产的 sha256 为唯一真实性锚点。
    有 digest → 任何来源（含第三方镜像）都必须哈希一致才换入；
    无 digest → 第三方镜像运营方不可问责，只接受官方源。

    返回 {"ok": True, "data": bytes, "src": 源名} 或 {"ok": False, "msg": ...}。
    """
    errors: list[str] = []
    for name, url in _rt._zip_urls(zip_url):
        if want == "" and name != "官方源":
            continue
        b = _rt.http_bytes(url, 60)
        if b is None:
            errors.append(f"{name}下载失败")
            continue
        if len(b) < 20_000:
            errors.append(f"{name}包体异常({len(b)}B)")
            continue
        if want:
            got = _rt._sha256(b)
            if not hmac.compare_digest(got, want):
                errors.append(
                    f"{name}完整性校验失败（sha256 {got[:12]}… ≠ 期望 {want[:12]}…）"
                )
                continue
        return {"ok": True, "data": b, "src": name}
    detail = "；".join(errors)
    return {"ok": False,
            "msg": f"所有更新源下载失败或未通过完整性校验（{detail}）。"
                   f"请检查服务器网络，或在服务器配置代理后重试"}


def _check_zip_payload(data: bytes, tag: str) -> tuple[str, dict | None]:
    """结构 + 版本一致性校验。通过返回 (新版本号, None)，失败返回 ("", 错误字典)。"""
    if not _rt._zip_struct_ok(data):
        return "", {"ok": False, "msg": "更新包结构异常（缺少 panel_migr 主程序），已中止"}
    try:
        newver = _rt._zip_new_version(data)
    except Exception as e:
        return "", {"ok": False, "msg": f"更新包校验失败（{e}），已中止"}
    latest = tag.lstrip("vV")
    if latest and not _rt._version_eq(latest, newver):
        return "", {"ok": False,
                    "msg": f"更新包版本（{newver}）与 Release 版本（{latest}）不一致，已中止"}
    return newver, None


def _update_from_zip_asset(j: dict, zip_url: str, asset: str) -> dict:
    """ZIP 自更新主流程：下载 → 校验 → 整包备份 → 换入 → 调度重启。"""
    want = _rt._asset_digest(j, asset)
    got = _download_verified_zip(zip_url, want)
    if not got.get("ok"):
        return {"ok": False, "msg": got.get("msg") or "下载更新包失败"}
    data, src = got["data"], got["src"]
    newver, err = _check_zip_payload(data, str(j.get("tag_name") or ""))
    if err is not None:
        return err
    bak = _rt._backup_panel_zip(_rt.cfg.PANEL_VERSION)
    if bak == "":
        # v1.24.4（P1-5）：如实转述真实原因。此前一律甩「请检查 backups
        # 目录权限」，而在代码/数据分离布局下真正的原因是「一个文件都没
        # 打包到」，运维会照着权限方向白查。
        why = _rt._LAST_BACKUP_ERR or "未知原因"
        return {"ok": False, "msg": f"备份当前面板失败：{why}，已中止更新"}
    res = _rt._apply_zip(data, newver)
    if not res["ok"]:
        return res
    tail = _rt._schedule_restart()
    rt_tip = "，运行环境已重建" if res.get("rebuilt") else ""
    vtip = "，sha256 校验通过" if want else "，官方源直连（该 Release 未提供哈希）"
    return {"ok": True,
            "msg": (f"已更新到 v{newver}（来源：{src}，资产 {asset}）{vtip}"
                    f"{tail}{rt_tip}；旧版本已备份为 {Path(bak).name}")}


def _update_from_php_source(j: dict | None, rerr: str) -> dict:
    """旧版回退：PHP release 的 index.php 流程（行为与旧版一致）。"""
    errors = []
    if j is None:
        errors.append(f"release 查询失败（{rerr}）")
    for i, url in enumerate(_rt.update_raw_urls()):
        src = "官方源" if i == 0 else f"加速镜像{i}"
        r = _rt.http_get(url, 15)
        if r["code"] != 200 or not r["body"]:
            errors.append(f"{src} HTTP {r['code']}")
            continue
        content = r["body"]
        if not content.lstrip()[:5].lower() == "<?php":
            errors.append(f"{src}内容校验失败")
            continue
        bak = _rt.backup_current_index(_rt.cfg.PANEL_VERSION)
        if bak == "":
            return {"ok": False, "msg": "备份当前文件失败（请检查 backups 目录权限），已中止更新"}
        try:
            _rt.INDEX_FILE.write_text(content, "utf-8")
        except OSError:
            with contextlib.suppress(OSError):
                _rt.shutil.copyfile(bak, _rt.INDEX_FILE)
            return {"ok": False, "msg": "写入新版本失败，已回滚到备份"}
        return {
            "ok": True,
            "msg": f"更新完成（来源：{src}），旧版本已备份为 {Path(bak).name}，页面即将刷新",
            "bak": Path(bak).name,
        }
    detail = "；".join(errors)
    return {"ok": False, "msg": f"所有更新源下载失败（{detail}）。请检查服务器网络，或在服务器配置代理后重试"}


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
    ts = _rt.time.strftime("%Y%m%d%H%M%S")
    tag = f"{ts}_{os.getpid()}_{secrets.token_hex(2)}"
    stage = _rt.backups_dir() / f".stage_{tag}"
    old = _rt.backups_dir() / f".old_{tag}"
    rebuilt = False
    # v1.24.5（Q-09）：区分失败阶段。只有**线上代码真的被动过**，说「已回滚」才成立。
    # 解压 / 结构校验阶段失败时线上代码一个字节都没动过，此时报「更新失败已回滚」
    # 会让排障的人误以为发生过一次换入 + 回滚，去 backups/ 找并不存在的 .old_*。
    # 判据用「old/ 里是否有东西」而不是「_swap_stage 是否返回」：换入是逐条目
    # move，中途抛异常时可能已经搬走了一部分，那仍然必须回滚。
    phase = "extract"          # extract → swap → verify → runtime → health
    hr: dict = {"ok": True, "skipped": True, "probe_ok": False, "msg": ""}
    try:
        _rt._extract_zip(data, stage)
        phase = "swap"
        _rt._swap_stage(stage, old)
        phase = "verify"
        cur = _rt._installed_version()
        if cur != expect_ver:
            raise RuntimeError(f"换入后版本校验失败（读到 {cur or '空'}，应为 {expect_ver}）")
        phase = "runtime"
        rt = _rt._ensure_runtime()
        if not rt.get("ok"):
            raise RuntimeError(rt.get("msg") or "运行环境校验失败")
        rebuilt = bool(rt.get("rebuilt"))
        phase = "health"
        hr = _rt._restart_and_health_check(old=old, stage=stage)
        if not hr.get("ok") and not hr.get("skipped"):
            raise RuntimeError(hr.get("msg") or "重启后健康探测失败")
    except Exception as e:
        # v1.24.5（Q-09）：解压/结构校验阶段失败 —— 线上代码未被触碰，谈不上回滚。
        # 直接清掉解压残渣，文案如实说明「尚未改动线上代码」。
        if not _rt._old_has_content(old):
            # old/ 为空（或不存在）：_swap_stage 连一个条目都没搬走，删掉它也不会
            # 丢东西；留着空 .old_* 只会让下次 _recover_interrupted 多一次判断。
            _rt._cleanup_dirs(stage, old)
            return {
                "ok": False, "rolled_back": False, "restored": 0, "phase": phase,
                "msg": f"更新失败（尚未改动线上代码）：{e}",
            }
        # v1.24.4（P0-2）：回滚结果必须如实反映到消息里。此前 _restore_old 的异常
        # 被 `except Exception: pass` 静默吞掉，紧接着 _cleanup_dirs 删掉 old/ ——
        # 旧代码唯一副本没了，文案还谎称「已回滚」。
        rb = {"ok": True, "restored": 0, "failed": 0, "errors": []}
        try:
            rb = _rt._restore_old(old)
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
        _rt._cleanup_dirs(stage, old)
        return {"ok": False, "rolled_back": True, "restored": int(rb.get("restored") or 0),
                "phase": phase, "msg": f"更新失败已回滚：{e}"}
    if hr.get("detach"):
        # 重启/探测/回滚由独立进程完成，old/ 的清理权也在它手里：此处只清 stage/
        _rt._cleanup_dirs(stage)
        return {"ok": True, "msg": hr.get("msg", ""), "rebuilt": rebuilt,
                "probe_ok": False, "skipped": False, "detach": True}
    _rt._cleanup_dirs(stage, old)
    return {"ok": True, "msg": hr.get("msg", ""), "rebuilt": rebuilt,
            "probe_ok": hr.get("probe_ok", False), "skipped": hr.get("skipped", False)}
