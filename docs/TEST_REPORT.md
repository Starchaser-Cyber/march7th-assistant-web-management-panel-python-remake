# m7a 面板 Python 迁移全量测试报告（TEST_REPORT.md）

> 基线：index.php v1.19（357,783 B / 6,228 行）→ panel_migr Python 版（M2-R → M6）。
> 测试框架：pytest（`tests/`，10 个测试文件）；静态检测：`py_compile` + Jinja 解析 + `node --check`。

## 1. 里程碑全绿记录

| 里程碑 | 内容 | 测试结果 |
|---|---|---|
| M2 | 40 个写操作端点 Python 实现 | 79 passed |
| M2-R | 基线恢复 + 会话写入根治（显式 `session_set` 持久化） | 89 passed |
| M3 | 预览收编 + H.264 极致档后端（ws264 / ffmpeg / MSE 契约） | 106 passed |
| M4 | 监控/日志/历史/告警事件化（`/m7a-events` WS + SQLite 归档） | 127 passed |
| M5-A | 会话轮换 + 登录限速 + key 迁 header + preview_token | 13 项安全专项 passed |
| M5-B | 前端拆包（templates/ + static/ + pagectx 渲染链） | 140 passed, 3 skipped |
| M5-C | 轮询合并 + WS 接入 + H.264 切档 UI + 告警历史 UI | 140 passed, 3 skipped |
| M5-D | 转码去 PHP（零残留）+ JSON 收敛口径 + 安全回归 | 13 安全专项 passed + 全量绿 |

## 2. 最终全量结果

```
$ python3 -m pytest -q
........................................................................ [ 50%]
.................................................................sss...  [100%]
140 passed, 3 skipped in 99.57s (0:01:39)
```

- **140 passed**，0 failed；
- **3 skipped** 为需要外部依赖（ffmpeg/外网/真实容器）的用例，在无依赖环境按设计跳过；
- 含 **M5 安全专项 13 项**：会话轮换、登录限速、cookie 属性、csrf、key header/query 双通道、未登录全路径拦截等。

## 3. 静态检测

| 检测项 | 结果 |
|---|---|
| 全部 `.py` `py_compile`（pagectx/render 链改写后复检） | ✅ 通过 |
| `templates/panel.html.j2` Jinja 解析（for/if 平衡 14/14、15/15） | ✅ 通过 |
| `static/panel.js` `node --check`（11 处改造后） | ✅ 通过 |
| 模板 `<?php` 残留计数 | ✅ 0 |
| 前端 `<?php` 残留计数（panel.js PHP 插值清零） | ✅ 0 |
| 服务端 `mb_/iconv/GBK` 转码残留全扫 | ✅ 0 命中 |

## 4. 关键口径备忘（测试覆盖之外的人工判定）

1. **5 老轮询 URL**（`?ajax=status|log|running|monitor|history`）同名同形长期保留——测试断言按此口径校验。
2. **key 迁移**：header（`X-Panel-Key`）首认、老 URL `&key=` 兼容——双通道均有测试。
3. **散落 JSON 保留**：`alert_state.json` / `history_*.json` / `monitor_*.json` / `schedule.json` / `.image_check_cache.json` 与 PHP 共享，是**回切保障**而非收敛遗漏；时序与状态事件已归档 SQLite（eventdb）。PHP 退役（阶段二）后再评估是否撤除。
4. **转码去 PHP**：PHP 源本身无转码调用（仅 `mb_strlen` 计数，Python 以 `len()` 字符数等价）；Python 侧外部文件读取全线 `errors="replace"` 兜底，比 PHP 字节直出更稳。
5. **未登录路径**：GET/POST/ajax/download 全部落 Python 登录页渲染，对齐 PHP 行为（测试逐路径覆盖）。

## 5. 前端改造验证（M5-C，node --check 之外的人工走查清单）

- `TASK_LIST` / `PANEL_UPDATE_MODE` 由模板内联注入（`json_encode` 镜像 PHP `JSON_HEX_TAG` 防 `</script>` 注入）；
- `/m7a-events` WS：hello 权威锚点、断线退避重连（2s→30s）、resync 全量兜底、连通期挂起 monitor/log 轮询；
- H.264 档：MSE fMP4 播放 + 60s 缓冲清理 + close 1013/1008 不重连；
- 告警历史：`?ajax=alert_history` 渲染 + WS alert toast 联动。

---

*报告生成：迁移收官时（M6）。复核命令：`python3 -m pytest -q`（期望 140 passed, 3 skipped）。*
