# m7a_panel v1.19 接口盘点清单（M0 产物，2026-10-01）

> 基线：index.php v1.19（357783B / 6228 行）。
> 三线交叉验证：后端分支 ↔ 前端调用点（fd.append/动态表单/submitPanelAction）↔ $_GET 参数枚举，零漏、零死接口。
> 总计 **47 个入口**：GET ajax 12 + POST action 28 + 内部回调 3 + 文件下载 2 + PWA 2。

## A. GET ajax（12 个，is_auth 后 JSON，免 CSRF）→ M1 只读先行 / M2 写操作

| 接口 | 读写 | 内容 | 里程碑 |
|---|---|---|---|
| status | 只读 | 容器状态/任务状态主轮询 | M1 |
| monitor | 只读 | 资源监控（参数 iv=采样间隔） | M1 |
| log | 只读 | 日志读取（file/keyword/level/hours/lines） | M1 |
| running | 只读 | 运行状态轻量查询 | M1 |
| history | 只读 | 任务执行历史 | M1 |
| config_raw | 只读 | config.yaml 原文 | M1 |
| preview_token | 只读 | 按日 HMAC token（gmdate） | M1 |
| doctor | 写 | 一键体检 10 项（会探测系统） | M2 |
| alert_check | 写 | 告警配置体检 | M2 |
| image_check | 写 | 镜像更新检测（force 参数） | M2 |
| check_update | 写 | 面板自身更新检测 | M2 |
| test_update_source | 写 | 更新源测速 | M2 |

## B. POST action（28 个，全部 CSRF 校验；其中 7 个危险操作额外 confirm 确认）

**认证类（4）→ M1**
- login、logout、setup_pass（首次设密码）、restore_config（恢复配置）

**容器/任务控制（7，2280 行统一 CSRF+confirm 前置）→ M2**
- restart（重启容器）、stop（停止容器）、stop_task（停止任务=重启容器）、stop_loop（停循环子进程）、update（更新程序）、update_image（更新镜像）、do_update（执行更新）

**配置类（7）→ M2**
- save_config_form（表单式保存）、save_config_text（YAML 文本保存）、set_after_finish（跑完后行为）、set_update_mode（更新模式）、set_monitor_interval（监控间隔）、alert_save（告警配置）、alert_test（告警测试）

**计划任务（6）→ M2**
- schedule_add / schedule_del / schedule_toggle / schedule_now / schedule_conflict / history_clear（历史清空）

**多实例（4，instance 系列）→ M2**
- instance_save（保存实例）、instance_switch（切换实例）、instance_delete（删除实例）——⚠️ 涉及实例目录/删除，迁移时保持同等确认强度

**回滚（1）→ M2**
- backup_rollback（backups 目录还原，留 5 份）

## C. 内部回调（3 个，**key 签名鉴权，非登录态**）→ M2

| 接口 | 调用方 | 说明 |
|---|---|---|
| monitor_sampler=1&key= | 采样脚本/cron | 监控数据写入（服务器侧采集器） |
| scheduler=1&key= | 调度脚本 | 计划任务执行回调 |
| alerter=1&key= | 告警脚本 | 告警触发回调 |

⚠️ 这三个 key 机制必须原样迁移——它们是服务器侧脚本反向调面板的门，漏了会静默断掉监控/计划/告警。

## D. 文件下载（2 个，is_auth）→ M2
- download=config（导出 config.yaml）
- export_log=1（导出日志，file/keyword/level/hours 参数）

## E. PWA（2 个，无鉴权）→ M5 收尾
- manifest（PWA manifest，v1.17 零新文件 query 分支）
- icon&size=192（PWA 图标）

## F. preview WS（nginx /m7a-preview/）→ M3
- WS /ws、/m7a-ws、MJPEG /stream、/health——收编 preview_server v3.1 为 preview.py router，砍 socat+cron+独立进程+容器内服务 4 层外挂；token 机制（secret 文件）不变，前端零改动。

## 迁移验证基准
- 每个接口切换前抓 PHP 版返回作 baseline，切换后 diff
- GET 类比 JSON body；POST 类比行为（执行后状态变化）+ 返回体
- 三个内部回调用真实脚本调一遍
- CSFR：POST 全部带 csrf token；危险 7 个有二次确认（action→in_array 前置校验）
