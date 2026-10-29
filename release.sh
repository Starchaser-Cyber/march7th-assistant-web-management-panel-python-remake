#!/usr/bin/env bash
# m7a_panel V1.24.4 打包 + 发布脚本（分步执行，每步都可单独跑）
#
# 用法：
#   bash release.sh check          # 发版前检查（版本三处一致 / 语法 / 测试）
#   bash release.sh package        # 打三个 zip（发布包 / 审计包 / 审计报告）
#   bash release.sh page           # 生成下载页 index.html
#   bash release.sh serve          # 起公网下载服务（18090）
#   bash release.sh push  <PAT>    # 提交 + 推送 + 打 tag（需 PAT）
#   bash release.sh ghrel <PAT>    # 建 GitHub Release 并上传资产（需 PAT）
#
set -u
ROOT=/home/huangsongping/dsh-workspace/m7a-fix
DL=/home/huangsongping/dsh-workspace/m7a-dl
AUD=/home/huangsongping/dsh-workspace/m7a-audit
VER=1.24.4
TAG=V${VER}
REPO=Starchaser-Cyber/march7th-assistant-web-management-panel-python-remake
PY=/tmp/auditvenv/bin/python

die() { echo "[FAIL] $*" >&2; exit 1; }
ok()  { echo "[ OK ] $*"; }

cmd_check() {
  cd "$ROOT" || die "根目录不存在"
  echo "=== 1) 版本号三处一致 ==="
  grep -n 'PANEL_VERSION' panel_migr/config.py | head -2
  grep -n 'version-V' README.md | head -2
  head -5 CHANGELOG.md | grep -n 'V1\.' || true
  echo
  echo "=== 2) 语法检查 ==="
  "$PY" -m compileall -q panel_migr >/dev/null 2>&1 && ok "compileall 通过" || die "compileall 失败"
  node --check panel_migr/static/panel.js && ok "panel.js 语法通过" || die "panel.js 语法失败"
  echo
  echo "=== 3) 全量测试 ==="
  cd panel_migr || die "panel_migr 不存在"
  M7A_EVENTS_BG=0 "$PY" -m pytest tests/ -q 2>&1 | tail -6
}

cmd_package() {
  cd "$ROOT" || die "根目录不存在"
  mkdir -p "$DL"
  echo "=== 发布包（无外层目录）==="
  "$PY" build_package.py -o "$DL/${TAG}.zip" || die "发布包失败"
  echo
  echo "=== 审计包（附 AUDIT_INFO.md）==="
  "$PY" build_package.py --audit "$AUD/AUDIT_INFO.md" -o "$DL/${TAG}-audit.zip" || die "审计包失败"
  echo
  echo "=== 审计报告全文 ==="
  "$PY" - "$AUD" "$DL/m7a-audit-report.zip" <<'PYEOF'
import sys, zipfile
from pathlib import Path
src, out = Path(sys.argv[1]), Path(sys.argv[2])
files = ["00_SUMMARY.md", "A_update.md", "B_auth.md", "C_services.md", "D_handlers.md", "AUDIT_INFO.md"]
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
    for n in files:
        p = src / n
        if p.is_file():
            zf.write(p, "m7a-audit/" + n)
print("审计报告包：%s (%d B)" % (out, out.stat().st_size))
PYEOF
  echo
  echo "=== 产物清单 ==="
  ls -la "$DL"/*.zip
  echo
  echo "=== sha256 ==="
  sha256sum "$DL"/*.zip
}

cmd_page() {
  "$PY" - "$DL" <<'PYEOF'
import hashlib, sys, zipfile
from pathlib import Path
dl = Path(sys.argv[1])
tpl = (dl / "index.html.tpl").read_text("utf-8")

def info(name):
    p = dl / name
    if not p.is_file():
        return dict(size="（未生成）", count="-", sha="-")
    with zipfile.ZipFile(p) as zf:
        n = len(zf.namelist())
    return dict(
        size="{:,} B（约 {:.2f} MB）".format(p.stat().st_size, p.stat().st_size / 1048576),
        count="%d 个文件" % n,
        sha=hashlib.sha256(p.read_bytes()).hexdigest(),
    )

rel = info("V1.24.4.zip")
aud = info("V1.24.4-audit.zip")
rpt = info("m7a-audit-report.zip")
html = (tpl
    .replace("__REL_SIZE__", rel["size"]).replace("__REL_COUNT__", rel["count"]).replace("__REL_SHA__", rel["sha"])
    .replace("__AUD_SIZE__", aud["size"]).replace("__AUD_COUNT__", aud["count"]).replace("__AUD_SHA__", aud["sha"])
    .replace("__RPT_SIZE__", rpt["size"]).replace("__RPT_SHA__", rpt["sha"]))
(dl / "index.html").write_text(html, "utf-8")
print("下载页已生成：%s" % (dl / "index.html"))
PYEOF
}

cmd_serve() {
  cd "$DL" || die "下载目录不存在"
  if ss -tln 2>/dev/null | grep -q ":18090 "; then
    ok "18090 已在监听"
  else
    nohup "$PY" serve.py >> /tmp/m7a-dl.log 2>&1 &
    sleep 2
    ss -tln 2>/dev/null | grep -q ":18090 " && ok "已启动 18090" || die "启动失败，看 /tmp/m7a-dl.log"
  fi
  echo
  echo "本机     : http://127.0.0.1:18090/"
  echo "公网 IP  : http://211.158.61.148:18090/"
  echo "域名     : http://starchaser-cyber.duckdns.org:18090/"
}

cmd_push() {
  PAT="${1:-}"; [ -n "$PAT" ] || die "用法：release.sh push <PAT>"
  cd "$ROOT" || die "根目录不存在"
  echo "=== 待提交文件 ==="
  git status --short
  echo
  git add -A
  git -c user.name="Starchaser-Cyber" -c user.email="starchaser-cyber@users.noreply.github.com" \
      commit -m "fix(V1.24.4): 修复审计发现的 6 个 P0 与 25 个 P1 缺陷

- P0: 配置页多行值写坏 config.yaml；更新回滚失败吞异常并删旧代码；
  在线更新自重启时序死锁；告警静默期吞掉 down 边沿导致永久不告警；
  alert_state 无锁并发覆盖；健康探测端口硬编码 9999
- P1: CSRF 空 token 绕过、全局登录锁 DoS、session 无锁/O_NOFOLLOW、
  非面板路径全 500、计划任务 cron 用错 key 通道、事件循环被阻塞、
  备份含 session 复活已注销会话、镜像检查头前缀与时间解析、
  发布包层级校验、zip bomb 防护、YAML 校验、inst_dir 校验、
  websockets 依赖缺失等 18 项
- 新增针对性回归测试；文档同步" || die "commit 失败"
  echo
  git --no-pager show --stat HEAD | head -40
  echo
  echo "=== 推送 main ==="
  git push "https://${PAT}@github.com/${REPO}.git" HEAD:main || die "push 失败"
  echo
  echo "=== 打 tag ${TAG} ==="
  git tag -f "$TAG"
  git push "https://${PAT}@github.com/${REPO}.git" "refs/tags/${TAG}" || die "tag push 失败"
  ok "推送完成"
}

cmd_ghrel() {
  PAT="${1:-}"; [ -n "$PAT" ] || die "用法：release.sh ghrel <PAT>"
  cd "$DL" || die "下载目录不存在"
  ZIP="$DL/${TAG}.zip"
  [ -f "$ZIP" ] || die "找不到 $ZIP，先跑 package"
  NOTES="$DL/release_notes.md"
  [ -f "$NOTES" ] || die "找不到 $NOTES"

  echo "=== 建 Release ==="
  "$PY" - "$PAT" "$REPO" "$TAG" "$NOTES" <<'PYEOF'
import json, sys, urllib.request
pat, repo, tag, notes = sys.argv[1:5]
body = {"tag_name": tag, "name": tag + " Release",
        "body": open(notes, encoding="utf-8").read(),
        "draft": False, "prerelease": False}
req = urllib.request.Request(
    "https://api.github.com/repos/%s/releases" % repo,
    data=json.dumps(body).encode("utf-8"),
    headers={"Authorization": "Bearer " + pat, "Accept": "application/vnd.github+json",
             "Content-Type": "application/json"}, method="POST")
try:
    with urllib.request.urlopen(req, timeout=60) as r:
        j = json.load(r)
except urllib.error.HTTPError as e:
    print("HTTP %s: %s" % (e.code, e.read().decode("utf-8", "replace")[:800]))
    sys.exit(1)
print("release id =", j["id"])
open("/tmp/m7a_rid.txt", "w").write(str(j["id"]))
PYEOF
  RID=$(cat /tmp/m7a_rid.txt) || die "没拿到 release id"
  echo "RID=$RID"

  echo "=== 上传资产 ${TAG}.zip ==="
  "$PY" - "$PAT" "$REPO" "$RID" "$ZIP" "$TAG" <<'PYEOF'
import hashlib, json, sys, urllib.request
pat, repo, rid, path, tag = sys.argv[1:6]
data = open(path, "rb").read()
local = hashlib.sha256(data).hexdigest()
req = urllib.request.Request(
    "https://uploads.github.com/repos/%s/releases/%s/assets?name=%s.zip" % (repo, rid, tag),
    data=data,
    headers={"Authorization": "Bearer " + pat, "Content-Type": "application/zip"}, method="POST")
try:
    with urllib.request.urlopen(req, timeout=600) as r:
        j = json.load(r)
except urllib.error.HTTPError as e:
    print("HTTP %s: %s" % (e.code, e.read().decode("utf-8", "replace")[:800]))
    sys.exit(1)
remote = (j.get("digest") or "").replace("sha256:", "")
print("本地 sha256 :", local)
print("远端 digest :", remote or "（API 未返回）")
print("一致        :", "是" if remote == local else ("无法核对" if not remote else "否 ⚠️"))
print("下载地址    :", j.get("browser_download_url"))
PYEOF
}

case "${1:-}" in
  check)   cmd_check ;;
  package) cmd_package ;;
  page)    cmd_page ;;
  serve)   cmd_serve ;;
  push)    shift; cmd_push "$@" ;;
  ghrel)   shift; cmd_ghrel "$@" ;;
  *) echo "用法：bash release.sh {check|package|page|serve|push <PAT>|ghrel <PAT>}"; exit 2 ;;
esac
