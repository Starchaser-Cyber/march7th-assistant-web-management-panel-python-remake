# m7a 面板 Python 版验收清单（ACCEPTANCE.md）

> 部署后逐条勾选；**阶段一全部通过、稳定运行 1–2 周**后再进入阶段二。
> 命令类项目给出可直接粘贴的命令；浏览器类项目按描述人工点验。

## 阶段一：切换上线

### 1. 部署与启动

- [ ] 快照包已生成：`ls /www/backup/panel_migr_$(date +%F)/` 见 `panel_migr_current.tgz`、`panel_state.tgz`、nginx conf、`crontab.txt`
- [ ] 代码替换完成：`ls <站点目录>/panel_migr/main.py` 存在，旧目录 `panel_migr.old_*` 保留
- [ ] 依赖安装：`<站点目录>/panel_migr/.venv/bin/python -c "import fastapi, uvicorn, jinja2, httpx, bcrypt; print('deps ok')"`
- [ ] 服务托管：`systemctl is-active m7a-panel` = `active`，`systemctl is-enabled m7a-panel` = `enabled`
- [ ] 直连应答：`curl -sI http://127.0.0.1:8787/ | head -1` = `HTTP/1.1 200`

### 2. 页面与静态资源

- [ ] 登录页由 Python 渲染：`curl -s http://127.0.0.1:8787/ | grep -c 'auth-card'` ≥ 1
- [ ] 登录页 A/B 字节级一致（归一化后 diff 为空）：见 DEPLOY §5.1
- [ ] 静态直出：`curl -sI http://127.0.0.1:8787/static/panel.css | head -2` → 200 + `text/css`
- [ ] `curl -sI http://127.0.0.1:8787/static/panel.js | head -2` → 200 + javascript
- [ ] 模板无 PHP 残留：`grep -c '<?php' <站点目录>/panel_migr/templates/panel.html.j2` = 0
- [ ] 前端无 PHP 残留：`grep -c '<?php' <站点目录>/panel_migr/static/panel.js` = 0

### 3. 登录与会话安全

- [ ] 真实登录 E2E（无痕窗口手工）：DEPLOY §6 全流程通过
- [ ] 错密码：显示横幅、不放行；连续错误触发登录限速提示
- [ ] 新访客下发 `PHPSESSID` cookie：`curl -sI http://127.0.0.1:8787/ | grep -i set-cookie` → `HttpOnly; SameSite=lax`
- [ ] 毒会话已清：`find /tmp -maxdepth 1 -name 'sess_*' -user root` 输出为空
- [ ] POST 无 csrf 被拒：面板 F12 手工去掉 csrf 提交 → 拒绝
- [ ] 会话轮换生效：登录动作后旧 sid 失效（用旧 cookie 复访需重登）

### 4. 写操作抽测（9/9，DEPLOY §5.2）

- [ ] 1 登录/退出
- [ ] 2 restart 重启容器
- [ ] 3 配置保存
- [ ] 4 告警保存
- [ ] 5 测试告警到手机
- [ ] 6 监控刷新间隔
- [ ] 7 实例编辑/新增
- [ ] 8 清空任务历史
- [ ] 9 H.264 档开启（有/无 ffmpeg 两种表现均符合预期）

### 5. 只读接口（5 老轮询，同名同形长期保留）

- [ ] `curl -s -b "PHPSESSID=<sid>" 'http://127.0.0.1:8787/?ajax=status'` 有容器状态文本
- [ ] `?ajax=running` 返回 `{"running": ...}` JSON
- [ ] `?ajax=log` 返回 JSON（files/lines）
- [ ] `?ajax=monitor&iv=1` 返回 JSON（points 非空）
- [ ] `?ajax=history` 返回 JSON（items 数组）
- [ ] key 迁 header：`curl -s -H 'X-Panel-Key: <key>' 'http://127.0.0.1:8787/?monitor_sampler=1'` 正常；老 URL 带 `&key=` 照常

### 6. 预览（M3）

- [ ] `?ajax=preview_token` 返回按日 HMAC token
- [ ] JPEG 档 720p/480p 切档出画
- [ ] ⚡H.264 极致档：装了 ffmpeg → 紫色徽章出画；没装 → 红色"不可用"展示 close 原因且不重连
- [ ] 断线自动重连（拔网线 10s 再插）

### 7. 事件流 WS（M4/M5-C）

- [ ] `/m7a-events` 握手成功：F12 Network → WS → 收到 `hello` 帧（含 seq）
- [ ] 监控事件推送：等待 15–30s 收到 `type:"monitor"` 帧，图表随之更新
- [ ] 日志事件：跑一个任务 → 日志页自动滚动（事件驱动，非定时拉取）
- [ ] 历史事件：任务结束 → 任务页列表自动更新
- [ ] 轮询合并后固定轮询只剩 `?ajax=status`+`?ajax=running`（10s 一轮）；monitor/log 在 WS 连通期无轮询请求
- [ ] 断开恢复：关闭 WS（或重启服务）→ 前端退避重连，恢复后轮询按原状态接管

### 8. 告警（M4/M5-C）

- [ ] 告警卡 📋 历史展开：`?ajax=alert_history` 列表渲染（时间/标题/kind/推送状态）
- [ ] 真实触发一次告警（停容器）→ WS `alert` 帧 toast + 历史列表新增 + 手机推送
- [ ] 心跳 cron 在线：宝塔计划任务最近一次执行成功（`monitor_sampler`）

### 9. 数据与编码（M5-D）

- [ ] 监控时序已归档 SQLite：`python3 -c "import sqlite3;print(sqlite3.connect('<数据目录>/events.db').execute('select count(*) from samples').fetchone())"` 计数随时间增长
- [ ] 散落 JSON 保留作 PHP 回切共享层（`alert_state.json` / `history_*.json` / `monitor_*.json` / `schedule.json`）——**属预期，非遗漏**
- [ ] 中文配置无乱码：配置页保存含中文的值 → 刷新还原
- [ ] 服务日志无 UnicodeDecodeError / 500

### 10. 回切演练（至少一次）

- [ ] 代码级回切演练：换回 `panel_migr.old_*` → 服务停 → PHP 兜底照常出页面 → 换回新代码 → 服务起
- [ ] nginx 分流删除演练：删分流 → reload → 全量走 PHP → 恢复分流 → reload（阶段一等价回切）

---

## 阶段二：PHP 退役（观察期满后执行，逐项勾选）

- [ ] nginx 整站 `proxy_pass 8787`、WS 两处 upgrade location 就位、`nginx -t` 通过
- [ ] 转发链删除：conf 中无 `X-M7A-Fwd`、无分流 `if` 段
- [ ] `index.php` 已归档至 `/www/backup/index.php.retired_v1.19_*`
- [ ] 9999 已下线：`curl -sI http://127.0.0.1:9999/` 失败；外网 9999 入口关闭（**已提前告知用户**）
- [ ] fastcgi 卸载：`grep -R fastcgi_pass /www/server/panel/vhost/nginx/` 无面板站点命中
- [ ] 心跳 cron 有效：宝塔任务成功（8888 或直连 8787 均落 Python）
- [ ] 三件套失效验证（四项全过，改回后面板功能不受影响）：
  - [ ] exec 解禁恢复 → 面板正常
  - [ ] open_basedir 白名单移除 → 面板正常
  - [ ] `gpasswd -d www docker` → 容器重启/停止照常（服务以 root 跑）
  - [ ] 杀软白名单移除 → 无告警
- [ ] 看门狗 cron 保留、`Restart=always` 保留
- [ ] 外网全功能回归（走 8888 完成一遍阶段一 §2–§8 抽验）
- [ ] 端口/进程清单收敛：`ss -lntp | grep -E '8787|9999'` → 仅 8787（面板相关）
- [ ] 服务级回切路径仍可用：`$D` 快照包 + conf 备份在位

---

## 附：全量测试复核（部署机可选）

```bash
cd <站点目录>/panel_migr
.venv/bin/pip install -q pytest
.venv/bin/python -m pytest -q    # 期望：140 passed, 3 skipped
```

勾选全部完成 → 迁移收官。
