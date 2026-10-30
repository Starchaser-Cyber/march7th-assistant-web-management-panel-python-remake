"""发布包 ZIP：结构校验、版本提取、防 zip bomb 解压、整包备份。"""
from __future__ import annotations

import io
import re
import zipfile
from pathlib import Path



from . import _rt
import contextlib


# 解压/下载上限（v1.24.4 P1-8）：防 zip bomb 打爆内存与磁盘（本机 8G 红线）。
_MAX_TOTAL_SIZE = 200 * 1024 * 1024      # 解压后总大小上限 200MB


_MAX_ENTRIES = 20000                     # 条目数上限


_MAX_FILE_SIZE = 50 * 1024 * 1024        # 单文件上限 50MB


_MAX_RATIO = 200                         # 压缩比上限 200:1


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
                v = _rt._version_from_text(z.read(name).decode("utf-8", "replace"))
                if v:
                    return v
    raise RuntimeError("包内缺少 panel_migr/config.py")


def _extract_zip(data: bytes, stage: Path) -> None:
    """解压更新包到 stage；带 zip bomb 防护（v1.24.4 P1-8）。

    先遍历 infolist 累计 file_size（压缩包元数据，不需要真的解压），校验
    总大小 / 条目数 / 单文件大小 / 压缩比，任一超限直接抛异常，绝不解压。
    再逐条 extract（不用 extractall，便于按条目复核实际写盘大小）。
    """
    stage.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        infos = z.infolist()
        if len(infos) > _rt._MAX_ENTRIES:
            raise RuntimeError(
                f"更新包条目数异常（{len(infos)} > {_rt._MAX_ENTRIES}），已中止解压")
        total = 0
        for info in infos:
            nm = info.filename.replace("\\", "/")
            if nm.startswith("/") or ".." in nm.split("/"):
                raise RuntimeError(f"包内路径非法：{nm}")
            size = int(info.file_size or 0)
            if size > _rt._MAX_FILE_SIZE:
                raise RuntimeError(
                    f"更新包内单文件过大（{nm} {size}B > {_rt._MAX_FILE_SIZE}B），已中止解压")
            total += size
            if total > _rt._MAX_TOTAL_SIZE:
                raise RuntimeError(
                    f"更新包解压后总大小超限（>{_rt._MAX_TOTAL_SIZE}B），已中止解压")
            csize = int(info.compress_size or 0)
            if csize > 0 and size / csize > _rt._MAX_RATIO:
                raise RuntimeError(
                    f"更新包压缩比异常（{nm} {size}/{csize} > {_rt._MAX_RATIO}:1），"
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
        ts = int(_rt.time.time())
    if ts < _rt._ZIP_MIN_TS:
        ts = _rt._ZIP_MIN_TS
    dt = _rt.time.localtime(ts)[:6]
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
        raise _rt._BackupError(f"遍历 {root} 失败：{e}") from e
    for p in items:
        rel = p.relative_to(root)
        if any(part in _rt._PROTECT_DIRS for part in rel.parts):
            continue
        if not p.is_file():
            continue
        try:
            mode = p.stat().st_mode & 0o777
        except OSError:
            mode = 0o644
        try:
            zi = zipfile.ZipInfo(str(Path(arc_root) / rel), date_time=_rt._zip_date_time(p))
            zi.compress_type = zipfile.ZIP_DEFLATED
            zi.external_attr = (mode or 0o644) << 16
            zf.writestr(zi, p.read_bytes())
            n += 1
        except (OSError, ValueError) as e:
            raise _rt._BackupError(f"打包 {rel} 失败：{e}") from e
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
    # v1.24.7（方向C）：_LAST_BACKUP_ERR 是**包级**状态（测试直接读
    # U._LAST_BACKUP_ERR），因此经 _rt.set() 写回，不能用本模块的 global。
    _rt.set("_LAST_BACKUP_ERR", "")
    d = _rt.backups_dir()
    if not d.is_dir():
        _rt.set("_LAST_BACKUP_ERR", f"备份目录不可用：{d}")
        return ""
    root = _rt._code_root()
    if not (root / "panel_migr").is_dir():
        _rt.set("_LAST_BACKUP_ERR", f"找不到面板代码目录（{root}/panel_migr 不存在）")
        return ""
    ver = re.sub(r"[^A-Za-z0-9._-]", "", (version or _rt.cfg.PANEL_VERSION).strip()) or "unknown"
    target = d / f"panel_{ver}_{_rt.time.strftime('%Y%m%d%H%M%S')}.zip"
    try:
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
            n = 0
            for item in sorted(root.iterdir()):
                if item.name in _rt._PROTECT_TOP or item.name.startswith("."):
                    continue
                if item.is_dir():
                    n += _rt._zip_tree(zf, item, item.name)
                elif item.is_file():
                    try:
                        zi = zipfile.ZipInfo(item.name, date_time=_rt._zip_date_time(item))
                        zi.compress_type = zipfile.ZIP_DEFLATED
                        zi.external_attr = (item.stat().st_mode & 0o777) << 16
                        zf.writestr(zi, item.read_bytes())
                        n += 1
                    except (OSError, ValueError) as e:
                        raise _rt._BackupError(f"打包 {item.name} 失败：{e}") from e
        if n == 0 or target.stat().st_size < 200:
            # 不是权限问题：这个目录里根本没有可备份的面板文件（布局不匹配）
            _rt.set("_LAST_BACKUP_ERR", (
                f"没有可备份的面板文件（代码目录 {root} 下未找到 panel_migr/ 等面板文件）"))
            with contextlib.suppress(OSError):
                target.unlink()
            return ""
    except _rt._BackupError as e:
        _rt.set("_LAST_BACKUP_ERR", str(e))
        with contextlib.suppress(OSError):
            target.unlink()
        return ""
    except (OSError, ValueError) as e:
        _rt.set("_LAST_BACKUP_ERR", f"写入备份失败：{e}")
        with contextlib.suppress(OSError):
            target.unlink()
        return ""
    _rt.backups_prune()
    return str(target)
