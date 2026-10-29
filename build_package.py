#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""m7a_panel 打包脚本 —— 一条命令出包。

用法（在项目根目录，即含 .git 和 panel_migr/ 的目录下执行）：

    python3 build_package.py                  # 发布包 V{版本}.zip（无外层目录）
    python3 build_package.py --with-git        # 交接包（含完整 .git 历史 + 交接文档）
    python3 build_package.py --audit INFO.md   # 审计包（发布包 + 指定说明文件）

设计要点（每一条都是踩过的坑，不要"优化"掉）：

  1. 文件清单从 `git ls-files` 取，不遍历目录 —— 遍历会带进 tests/_tmp、
     __pycache__、.venv。
  2. 不用 zf.write()，改用 writestr + 显式 ZipInfo(date_time=...) —— 云盘文件
     mtime 可能早于 1980，write() 会抛 ValueError。
  3. 纯 Python zipfile，不依赖系统 zip 命令 —— 很多精简环境没有。
  4. 打完必须 testzip() 校验，并打印 sha256 供上传后核对。
  5. 发布包**不能有外层目录**：services/updateops.py::_swap_stage 把解压目录的
     一级条目直接换入面板根目录。若 zip 里多一层 m7a_panel_V1.24.3/，一键更新
     会把整个面板挪进子目录、主程序随之丢失，服务起不来。
     交接包可以有外层目录，因为它不是更新包。

注意：zip 不是字节级可复现的（每个条目用当前时间戳），同内容打两次 sha256
不同。校验"重打包是否正确"要比文件清单和逐文件 sha256，不是整包 sha256。
"""

from __future__ import annotations

import argparse
import hashlib
import re
import subprocess
import sys
import time
import zipfile
from pathlib import Path

# 不进入任何包的路径特征
EXCLUDE_DIRS = ("data/", "backups/", ".venv/", "__pycache__/", "tests/_tmp/", ".pytest_cache/")
EXCLUDE_SUFFIX = (".pyc", ".pyo", ".zip", ".tgz", ".log")
EXCLUDE_NAMES = (".DS_Store", "Thumbs.db")

# 交接包额外附带的本目录文档
HANDOFF_DOCS = ("HANDOFF.md", "FILE_MAP.md", "BUILD.md", "RELEASE.md", "AI_PROMPT.md")


def die(msg: str) -> None:
    print("[FAIL] " + msg, file=sys.stderr)
    sys.exit(1)


def read_version(root: Path) -> str:
    cfg = root / "panel_migr" / "config.py"
    if not cfg.is_file():
        die("找不到 %s，请在项目根目录执行本脚本" % cfg)
    # 两种写法都要兼容：
    #   PANEL_VERSION = "1.24.3"
    #   PANEL_VERSION = os.environ.get("M7A_PANEL_VERSION", "1.24.3")
    for line in cfg.read_text("utf-8").splitlines():
        if "PANEL_VERSION" in line and "=" in line:
            nums = re.findall(r'["\'](\d+\.\d+\.\d+)["\']', line)
            if nums:
                return nums[-1]
    die("config.py 里没有可识别的 PANEL_VERSION")
    return ""  # 不可达


def git_files(root: Path) -> list[str]:
    r = subprocess.run(["git", "ls-files", "-z"], cwd=str(root), capture_output=True)
    if r.returncode != 0:
        die("git ls-files 失败（当前目录是 git 仓库吗？）")
    out = r.stdout.decode("utf-8", "replace")
    return [p for p in out.split("\0") if p]


def _seg_match(rel: str, pat: str) -> bool:
    """按**路径段边界**匹配目录前缀，而不是裸子串（v1.24.4 P2）。

    此前用 `d in rel`：EXCLUDE_DIRS 里的 "data/" 会把 `panel_migr/metadata/...`
    （"data/" 是其子串）一起误伤。现在要求排除项在路径段边界上出现，并支持
    多段模式（如 "tests/_tmp/"）：
      - "data/"       匹配  a/data/x  与 data/x，不匹配 a/metadata/x
      - "tests/_tmp/" 匹配  tests/_tmp/x
    """
    seg = pat.rstrip("/")
    parts = rel.replace("\\", "/").split("/")
    n = len(seg.split("/"))
    if not pat.endswith("/"):
        return seg in parts
    dirs = parts[:-1]                    # 目录段（不含文件名本身）
    return any("/".join(dirs[i:i + n]) == seg for i in range(len(dirs) - n + 1))


def wanted(rel: str) -> bool:
    rel = rel.replace("\\", "/")
    if any(_seg_match(rel, d) for d in EXCLUDE_DIRS):
        return False
    if rel.endswith(EXCLUDE_SUFFIX):
        return False
    if Path(rel).name in EXCLUDE_NAMES:
        return False
    return True


def join(prefix: str, rel: str) -> str:
    return prefix + "/" + rel if prefix else rel


def add_bytes(zf: zipfile.ZipFile, arcname: str, data: bytes, mode: int = 0o644) -> None:
    """写入字节内容，显式指定时间戳规避 1980 限制。"""
    zi = zipfile.ZipInfo(arcname, date_time=time.localtime()[:6])
    zi.compress_type = zipfile.ZIP_DEFLATED
    zi.external_attr = mode << 16
    zf.writestr(zi, data)


def add_file(zf: zipfile.ZipFile, path: Path, arcname: str) -> int:
    add_bytes(zf, arcname, path.read_bytes(), 0o755 if path.suffix == ".py" else 0o644)
    return path.stat().st_size


def build(
    root: Path,
    out_path: Path,
    prefix: str,
    files: list[str],
    with_git: bool,
    extra: dict,
    extra_docs: list,
) -> None:
    count = 0
    raw = 0
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        # 1) 仓库跟踪文件
        for rel in files:
            p = root / rel
            if not p.is_file():
                print("  [skip] 索引里有但磁盘没有：" + rel)
                continue
            raw += add_file(zf, p, join(prefix, rel))
            count += 1

        # 2) 额外说明文件
        for arcname, p in extra.items():
            if not p.is_file():
                die("找不到额外文件 %s" % p)
            raw += add_file(zf, p, join(prefix, arcname))
            count += 1

        # 3) 交接文档
        for p in extra_docs:
            if not p.is_file():
                die("找不到交接文档 %s" % p)
            raw += add_file(zf, p, join(prefix, p.name))
            count += 1

        # 4) .git 整体（交接包专用）
        if with_git:
            gitdir = root / ".git"
            if not gitdir.is_dir():
                die("--with-git 需要 .git 目录")
            n_git = 0
            for p in sorted(gitdir.rglob("*")):
                if p.is_dir() or p.is_symlink():
                    continue
                rel = p.relative_to(gitdir).as_posix()
                # 跳过易变状态；保留 index（否则接手方解压后满屏"被删除"）
                if rel.startswith(("logs/", "ORIG_HEAD", "FETCH_HEAD", "COMMIT_EDITMSG")):
                    continue
                raw += add_file(zf, p, join(prefix, ".git/" + rel))
                n_git += 1
            count += n_git
            print("  .git 历史文件：%d 个" % n_git)

    with zipfile.ZipFile(out_path) as zf:
        bad = zf.testzip()
        if bad:
            die("zip 损坏，首个坏条目：%s" % bad)

    size = out_path.stat().st_size
    sha = hashlib.sha256(out_path.read_bytes()).hexdigest()
    top = (prefix + "/") if prefix else "（无外层目录，panel_migr/ 等直接为根）"
    print("\n=== 打包完成 ===")
    print("  文件      : %s" % out_path)
    print("  条目数    : %d" % count)
    print("  原始体积  : {:,} B ({:.2f} MB)".format(raw, raw / 1048576))
    print("  压缩体积  : {:,} B ({:.2f} MB)".format(size, size / 1048576))
    print("  顶层目录  : %s" % top)
    print("  sha256    : %s" % sha)
    print("\n上传 GitHub Release 后请核对该 digest 与 API 返回一致（见 RELEASE.md §3）")


def main() -> None:
    ap = argparse.ArgumentParser(description="m7a_panel 打包")
    ap.add_argument("--root", default=".", help="项目根目录（含 .git），默认当前目录")
    ap.add_argument("--with-git", action="store_true", help="交接包：附带完整 .git 历史与交接文档")
    ap.add_argument("--audit", default="", help="审计包：指定要附带的说明文件路径")
    ap.add_argument("-o", "--out", default="", help="输出 zip 路径")
    ap.add_argument("--prefix", default="",
                    help="归档外层目录名。发布包必须留空（见文件头第 5 条）")
    args = ap.parse_args()

    root = Path(args.root).resolve()
    ver = read_version(root)
    print("版本：V" + ver)

    all_files = git_files(root)
    files = [f for f in all_files if wanted(f)]
    print("git ls-files 跟踪 %d 个，过滤后 %d 个" % (len(all_files), len(files)))

    extra = {}
    extra_docs = []
    if args.audit:
        extra["AUDIT_INFO.md"] = Path(args.audit).resolve()
    if args.with_git:
        here = Path(__file__).resolve().parent
        extra_docs = [here / n for n in HANDOFF_DOCS]

    if args.with_git:
        prefix = args.prefix or ("m7a_panel_V%s_handoff" % ver)
        out = Path(args.out) if args.out else root / (prefix + ".zip")
    elif args.audit:
        prefix = args.prefix or ("m7a_panel_V%s_%s" % (ver, time.strftime("%Y%m%d")))
        out = Path(args.out) if args.out else root / (prefix + ".zip")
    else:
        # 发布包：必须无外层目录，保持与 updateops._swap_stage / _zip_struct_ok 兼容
        # （v1.24.4 P2：此前 --prefix 会静默产出带外层目录的包，在线更新会「假成功」
        #  或把面板根塞进一个垃圾目录，现在直接报错退出）
        if args.prefix:
            die("发布包不能带外层目录（--prefix=%s）：updateops 要求 zip 根直接是 "
                "panel_migr/。带外层目录的包请改用 --with-git 交接包形式。" % args.prefix)
        prefix = ""
        out = Path(args.out) if args.out else root / ("V%s.zip" % ver)

    if out.exists():
        out.unlink()
    build(root, out, prefix, files, args.with_git, extra, extra_docs)


if __name__ == "__main__":
    main()
