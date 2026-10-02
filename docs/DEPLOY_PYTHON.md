# m7a 面板 Python 版部署与退役手册（DEPLOY.md）

> 适用：从 PHP 版面板（`index.php` v1.19 + nginx fastcgi `:9999`）整体切换到 Python 版（`panel_migr/` + systemd `:8787`）。
> 全部命令在服务器 **127.0.0.1** 执行；**每完成一步停下来验证，再进下一步**。
> 分两阶段：**阶段一（切换上线，当天）** → 观察期 1–2 周 → **阶段二（PHP 退役，你确认后再执行）**。
> 代码之外的事（GitHub、9999 下线时机、对外端口调整）全部由你决定与执行，本手册只给命令。

---

## 0. 架构与路径约定

切换后调用链：

```
阶段一（上线当天）：
  浏览器 → nginx :8888 ── 分流已迁 ?ajax → 127.0.0.1:8787（Python，systemd 托管）
                          └ 其余路径 → fastcgi → 127.0.0.1:9999（PHP 兜底）

阶段二（观察期满后）：
  浏览器 → nginx :8888 ──proxy_pass──→ 127.0.0.1:8787（Python，唯一后端）
  （9999 下线、fastcgi 删除、index.php 归档）
```

约定（按你的实际路径替换）：

| 占位符 | 定位命令 |
|---|---|
| `<站点目录>` | `find /www/wwwroot -maxdepth 2 -name panel_migr -type d` |
| `<新包>` | `panel_migr_final.zip` 解压后的目录 |

代码目录固定为 `<站点目录>/panel_migr/`（与 README 约定一致）。

---

## 1. 部署前快照（5 分钟，回滚的本钱）

```bash
cd <站点目录>
D=/www/backup/panel_migr_$(date +%F)
mkdir -p "$D"

# 1.1 现役代码 + 状态文件（.panel_* 密码/配置、data/ 运行数据）
tar czf "$D/panel_migr_current.tgz" panel_migr/
tar czf "$D/panel_state.tgz" $(ls -d .panel_* data 2>/dev/null)

# 1.2 nginx 配置 + 宝塔计划任务（心跳 cron / 看门狗）
cp /www/server/panel/vhost/nginx/*.conf "$D"/
crontab -l > "$D/crontab.txt" 2>/dev/null

ls -lh "$D"
```

产出的 `$D` 即**终版回滚包**（沿用 `panel_migr_backup_m2.tgz` 的模式），妥善保留。

---

## 2. 全量替换与启动（阶段一核心）

```bash
cd <站点目录>

# 2.1 整目录替换：旧目录改名保留，M2 残余文件随之出清（确认稳定后再删 old）
mv panel_migr "panel_migr.old_$(date +%F)"
unzip -q <新包> -d /tmp/panel_new
cp -a /tmp/panel_new/panel_migr .
cd panel_migr

# 2.2 依赖
python3 -m venv .venv
.venv/bin/pip install -q -r requirements.txt

# 2.3 systemd 托管（首次创建；已存在 m7a-panel.service 则跳过 cat，直接 enable --now）
cat > /etc/systemd/system/m7a-panel.service <<'EOF'
[Unit]
Description=m7a panel (python)
After=network.target

[Service]
WorkingDirectory=<站点目录>/panel_migr
ExecStart=<站点目录>/panel_migr/.venv/bin/python main.py
Restart=always
RestartSec=3
# 服务需执行 docker 命令 → 以 root 运行（等价于 PHP 时代给 www 加 docker 组的权限；
# 服务仅监听 127.0.0.1:8787，不对外暴露）

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now m7a-panel

# 2.4 三件套确认
systemctl is-active m7a-panel                 # 期望：active
curl -sI http://127.0.0.1:8787/ | head -3     # 期望：HTTP/1.1 200（渲染 Python 登录页）
journalctl -u m7a-panel -n 20 --no-pager      # 期望：无 traceback
```

> nginx 分流规则（`?ajax` → 8787）保持现状即可：页面、静态资源、写操作已全部由 Python 出，未覆盖路径仍走 PHP 兜底。

---

## 3. 毒会话清理（一条命令）

M2 过渡期产生过 **root 属主** 的 `/tmp/sess_*`，会让 PHP 顶部挂两条 Warning。先看分布，再只删 root 属主者：

```bash
find /tmp -maxdepth 1 -name 'sess_*' -ls                # 先看属主分布
find /tmp -maxdepth 1 -name 'sess_*' -user root -delete  # 再删（只删 root 属主）
```

删后所有会话失效、需重新登录一次；Python 版会下发新 `PHPSESSID`，无感过渡。

---

## 4. nginx 改造

### 4.1 阶段一：维持现状

不动。现有"已迁 `?ajax` 分流到 8787"规则 + X-M7A-Fwd 防循环段继续生效（PHP 兜底在）。

### 4.2 阶段二：整站直连（PHP 退役时执行）

`server` 块内：

```nginx
# ① 删除：所有 fastcgi_pass 到 9999 的 location / if 分流段
# ② 删除：X-M7A-Fwd 相关 if 排除段（转发链退役）
# ③ 主入口改为直连：
location / {
    proxy_pass http://127.0.0.1:8787;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
}
# ④ WebSocket 必须单列（事件流 + 预览推流），否则 400/426：
location = /m7a-events {
    proxy_pass http://127.0.0.1:8787;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_read_timeout 3600s;
}
location ^~ /m7a-preview/ {
    proxy_pass http://127.0.0.1:8787;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_read_timeout 3600s;
}

nginx -t && nginx -s reload
```

回切：恢复 §1 中 `$D` 里的 conf 备份 → `nginx -t && nginx -s reload`。

---

## 5. A/B 验收

### 5.1 登录页字节级比对（渲染一致性）

```bash
norm() { sed -E 's/value="[0-9a-f]{32}"/value="CSRF"/g; s/[0-9a-f]{32}/SID/g'; }

curl -s http://127.0.0.1:8787/ | norm > /tmp/ab_new.norm   # Python 渲染
curl -s http://127.0.0.1:9999/ | norm > /tmp/ab_old.norm   # PHP 渲染（阶段一 9999 仍在时）

diff /tmp/ab_new.norm /tmp/ab_old.norm && echo "✅ 登录页一致"
```

差异仅允许落在已知动态位（csrf 值、会话 id 已在 `norm` 归一化）。静态资源验收：

```bash
for f in panel.css panel.js; do
  curl -sI "http://127.0.0.1:8787/static/$f" | head -2
done   # 期望：200 + text/css（css）/ application/javascript（js）
```

### 5.2 写操作抽测 9/9（浏览器实测，带登录态）

| # | 操作 | 预期 |
|---|---|---|
| 1 | 登录 / 退出 | 200、进入面板页、退出回登录页 |
| 2 | 重启容器 restart | ok:true、容器状态变化 |
| 3 | 保存配置 config save | ok:true、值持久化 |
| 4 | 告警保存 alert_save | ok:true、badge 同步 |
| 5 | 发送测试告警 alert_test | 手机收到推送 |
| 6 | 切换监控刷新间隔 set_monitor_interval | ok:true、即时生效 |
| 7 | 实例编辑/新增 instance_save | ok:true、列表更新 |
| 8 | 清空任务历史 history_clear（先确认无重要记录） | ok:true、列表清空 |
| 9 | H.264 极致档开启（预览卡 ⚡H.264） | 有 ffmpeg 时紫色徽章出画；无则红色"不可用"不重连 |

全部 `ok:true`、无 500 → 9/9。

---

## 6. 真实登录 E2E（你亲手做）

浏览器**无痕窗口** → `http://127.0.0.1:8888`（或域名入口）：

1. 输密码登录（首次输错一次看横幅，再输对）；
2. 四个 tab 巡览：概览（容器状态徽章）、日志（滚动+过滤）、任务（历史+周统计）、配置（分组折叠）；
3. 游戏预览：720p → 480p → ⚡H.264 各切一次、全屏一次；
4. 告警卡：📋 历史展开有记录（或暂无）、📤 发送测试到手机；
5. F12 Console：无红错；Network 里 `?ajax=status` 每 10s 一轮（合并后唯一固定轮询）。

---

## 7. 心跳 cron 改道与看门狗

- 面板页展示的三条 cron 命令（`monitor_sampler` / `scheduler` / `alerter`）URL 的 host **来自你访问面板的地址（:8888）** → nginx 直连后自动落 Python，**无需改**。
- 仅当宝塔计划任务里存的是直连 `http://127.0.0.1:9999/...` 时才改（key 与路径完全不变）：

```bash
crontab -l | sed 's#http://127.0.0.1:9999#http://127.0.0.1:8787#g' | crontab -
# 若任务在宝塔「计划任务」页管理 → 进页面直接编辑 URL，同规则替换
```

- **看门狗 cron、m7a 容器自动拉起等与面板无关的任务保留不动**；`m7a-panel.service` 的 `Restart=always` 保留。

---

## 8. 阶段二：PHP 退役（观察期满、你点头后执行）

**顺序不能乱**，逐步验证：

```bash
# ① nginx 整站直连（§4.2）→ 浏览器全功能过一遍 §6
# ② index.php 归档（bak-v1.19 思路，保留回退）
cd <站点目录>
mv index.php /www/backup/index.php.retired_v1.19_$(date +%F)

# ③ 9999 下线（⚠️ 用户可见变化：外网 9999 对照入口消失、统一 8888 —— 提前告知）
#    宝塔站点配置里停掉 9999 的监听/反代；php-fpm 本体不动（其他站点在用）

# ④ fastcgi 卸载核对
grep -R "fastcgi_pass" /www/server/panel/vhost/nginx/ && echo "还有残留，回 §4.2 ①" || echo "✅ 已卸载"

# ⑤ 三件套失效验证（逐项改回 → 打开面板全功能正常即通过）
#   | 项 | 改回方式 | 通过标准 |
#   | exec 解禁       | php.ini disable_functions 恢复原值 → reload php-fpm | 面板功能不受影响 |
#   | open_basedir    | 去掉面板目录白名单 | 面板功能不受影响 |
#   | www docker 组   | gpasswd -d www docker | 容器重启/停止照常（服务以 root 跑，不依赖 www） |
#   | 杀软白名单      | 移除面板目录信任项 | 无告警（包内纯文本/Python，无可执行体） |
```

### 回切方法（长期保留，两层）

| 层级 | 场景 | 步骤 |
|---|---|---|
| 服务级回切 | 阶段二后想整体退回 PHP | 恢复 `$D` 快照（代码+状态+nginx conf+index.php）→ `systemctl disable --now m7a-panel` → 恢复 9999 监听 → reload nginx |
| 代码级回切 | 阶段一期间 Python 版有问题 | `mv panel_migr panel_migr.bad && mv panel_migr.old_<日期> panel_migr` → `systemctl restart m7a-panel`（或直接停服务走 PHP 兜底） |

---

## 9. 排障速查

| 症状 | 先查 | 处理 |
|---|---|---|
| 502 / 504 | `systemctl is-active m7a-panel` | `systemctl restart m7a-panel` + `journalctl -u m7a-panel -n 50` |
| 静态 404 | `curl -I http://127.0.0.1:8787/static/panel.css` | 看启动日志中 `/static` mount 是否报错 |
| WS 不连 | 浏览器 Console / Network WS 帧 | nginx 缺 Upgrade 单列 location（§4.2 ④） |
| 登录循环、csrf 失败 | `/tmp/sess_*` 属主 | §3 毒会话清理；对系统时钟 `date` |
| 监控图不动 | 手动 `curl -s "http://127.0.0.1:8787/index.php?monitor_sampler=1&key=<面板页里的key>"` | 看宝塔心跳 cron 是否还指向 9999（§7） |
| H.264 显示"服务器未安装 ffmpeg" | `which ffmpeg` | `apt install -y ffmpeg`，或先用 JPEG 档 |
| 页面数据旧 | F12 看 WS `/m7a-events` 是否 hello | 断开时前端会自动退避重连并恢复轮询；持续失败看 §1 |

---

*手册对应代码版本：panel_migr_final.zip（M2-R → M6 全量）。测试记录见 TEST_REPORT.md，部署后逐条勾选 ACCEPTANCE.md。*
