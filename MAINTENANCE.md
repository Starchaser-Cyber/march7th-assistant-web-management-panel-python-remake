# 三月七小助手（m7a）运维维护手册

> 适用主机：本机（Ubuntu 24.04.4 / kernel 7.0.0-38-generic / Intel i3-4160 / 8G RAM）
> 首次成文：2026-11-11，基于 2026-11-10 全天故障的排查与修复
> 维护者接手时请先通读「第一章：系统全貌」和「第八章：故障速查表」

---

## 目录

1. 系统全貌（组件、路径、端口）
2. 日常运维操作（启停、查看、日志）
3. 配置管理（config.yaml 与环境变量的优先级陷阱）
4. 已实施的非上游改动（补丁、守护、告警接线）
5. 故障案例库（2026-11-10 大故障完整复盘）
6. 监控与告警
7. 备份与回滚
8. 故障速查表
9. 已知限制与未解问题
10. 变更记录

---

## 第一章 系统全貌

### 1.1 三个组件

```
┌─────────────────────────────────────────────────────────────┐
│ 宿主机（8G RAM + 4G swap，/dev/sdb1 109G 已用 58%）          │
│                                                              │
│  ┌────────────────────┐   ┌──────────────────────────────┐  │
│  │ m7a 容器           │   │ m7a-panel 面板（systemd）     │  │
│  │ 三月七小助手本体    │◄──┤ 端口 9999，root 运行          │  │
│  │ 镜像 v2026.11.8    │   │ /www/wwwroot/m7a-panel        │  │
│  │ 目录 /home/march7thassistant │ 读日志/控容器/推告警   │  │
│  └────────────────────┘   └──────────────────────────────┘  │
│                                                              │
│  ┌──────────────────────────────────────────────────────┐   │
│  │ 下载页（serve.py，非托管进程）端口 18090              │   │
│  │ /home/huangsongping/dsh-workspace/m7a-dl             │   │
│  └──────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────┘
```

### 1.2 关键路径速查

| 用途 | 路径 |
|---|---|
| 小助手根目录 | `/home/march7thassistant`（属主 `www:www`） |
| compose 文件 | `/home/march7thassistant/docker-compose.yml` |
| 主配置 | `/home/march7thassistant/config.yaml`（约 45KB / 356 字段） |
| 日志总文件 | `/home/march7thassistant/logs/YYYY-MM-DD.log` |
| 单轮运行日志 | `/home/march7thassistant/logs/runs/<yyyyMMdd-HHmmss-xxxx>.log` |
| 浏览器凭证 | `/home/march7thassistant/3rdparty/WebBrowser/UserProfile/Integrated/Default/` |
| 上游代码补丁 | `/home/march7thassistant/patch/m7a/` |
| 配置守护脚本 | `/home/march7thassistant/config_guard.py` |
| 配置良好副本 | `/home/march7thassistant/config-backup/` |
| 面板源码（线上） | `/www/wwwroot/m7a-panel/panel_migr` |
| 面板源码（仓库） | `/home/huangsongping/dsh-workspace/m7a-fix` |
| 面板回滚包 | `/www/wwwroot/m7a-panel/backups/` |
| 下载页 | `/home/huangsongping/dsh-workspace/m7a-dl` |

### 1.3 端口

| 端口 | 服务 | 说明 |
|---|---|---|
| 9999 | m7a-panel | 管理面板，浏览器访问 |
| 18090 | 下载页 | 公网 `starchaser-cyber.duckdns.org:18090` |
| 8000 | 其它服务 | 与本项目无关 |
| 9222 / 9223 | 容器内 chrome CDP | 仅供容器内部与面板预览使用，不暴露 |

### 1.4 镜像与版本

```
镜像: ghcr.nju.edu.cn/moesnow/march7thassistant:latest
ID:   sha256:4253b8cddbb1...
版本: v2026.11.8
上游: 17ec9ddc82208aa2f2e01f4deaa20527ff176490
构建: 2026-11-07T20:45:38Z
```

> **注意**：镜像内没有 `.git`，版本只能靠 OCI label
> `org.opencontainers.image.version` / `.revision` 定位。

**保留的可回退旧镜像**

| 镜像 ID | 版本 | 说明 |
|---|---|---|
| `4253b8cddbb1` | v2026.11.8 | **当前使用** |
| `e47eeb59e812` | v2026.11.7 | 上一版，可回退 |
| `f2b8b0f21588` | v2026.10.31 系列 | 更旧 |

### 1.5 sudo 用法（重要）

```bash
echo 'Huang183' | sudo -S -p '' <命令>
```

> **三个坑**
> 1. 带 heredoc 的 `sudo tee <<EOF` 组合会失败并触发「3 次错误密码尝试」锁定。
>    要写 root 文件，先用 `write` 工具写到 `/tmp`，再 `sudo cp`。
> 2. `/home/march7thassistant` 属主是 `www:www`，非 sudo 无写权限。
> 3. 容器内 `/tmp` 与宿主 `/tmp` **不共享**。要在容器里留文件给宿主看，
>    写到 `/m7a/logs/`（= 宿主 `/home/march7thassistant/logs/`）。

---

## 第二章 日常运维操作

### 2.1 启停容器

```bash
# 启动（改了 compose 一定要用 --force-recreate，否则 ENV/资源限制不生效）
sudo docker compose -f /home/march7thassistant/docker-compose.yml up -d --force-recreate

# 停止
sudo docker compose -f /home/march7thassistant/docker-compose.yml down

# 只看状态
sudo docker inspect -f 'Status={{.State.Status}} Health={{.State.Health.Status}} RestartCount={{.RestartCount}}' m7a
```

### 2.2 当前运行模式（Loop 定时）

```
跑完一轮 → stop_game() 关浏览器 → 等到下一个 scheduled_time → 自动开跑
```

实测日志：

```
22:18:39,465 | INFO | 关闭浏览器成功
22:18:39,556 | INFO | 将在明天8时0分继续运行
```

**等待期间不占云游戏时长**（浏览器已关），资源占用约 **375MB / CPU 0.11%**。

### 2.3 看日志

```bash
# 容器 stdout（带 ANSI 色码，必须过滤，否则 grep 不到关键字）
sudo docker logs --tail 200 m7a 2>&1 | sed 's/\x1b\[[0-9;]*m//g'

# 总日志
sudo tail -f /home/march7thassistant/logs/$(date +%Y-%m-%d).log

# 某一轮的完整日志
sudo ls -lt /home/march7thassistant/logs/runs/ | head
```

> **坑**：日志带 ANSI 色码，直接 `grep "invalid session id"` 会 0 命中。
> 一定要先 `sed 's/\x1b\[[0-9;]*m//g'` 再 grep。

### 2.4 容器内执行命令

```bash
# 容器内没有 curl，用 python
sudo docker exec m7a python3 -c "import urllib.request,json; print(json.load(urllib.request.urlopen('http://127.0.0.1:9222/json/list')))"

# 不进容器读镜像文件
sudo docker run --rm --entrypoint sh ghcr.nju.edu.cn/moesnow/march7thassistant:latest -c 'cat /m7a/config.yaml'
```

### 2.5 浏览器登录（凭证失效时）

面板 → 「游戏画面预览」→ 手动扫码登录。凭证存在
`3rdparty/WebBrowser/UserProfile/Integrated/Default/`，长期有效。

> 2026-11-10 曾按用户要求清除过一次凭证，备份在
> `/home/march7thassistant/cookies-backup-20261110-2145.tar.gz`。

---

## 第三章 配置管理

### 3.1 ⚠️ 环境变量优先级高于配置文件（最大的坑）

上游 `module/config/config.py:123` 的 `_ENV_OVERRIDE_MAP` 注释原文：

> **环境变量优先级高于配置文件**

**实际踩到的后果**：用户 9-27 起在配置页把 `after_finish` 设为 `Exit`，
但 compose 里一直是 `MARCH7TH_AFTER_FINISH=Loop`，容器永远读到 `Loop`，
用户在页面怎么改都没用。

**排查方法**：

```bash
# 1. 看配置文件写的什么
sudo grep -nE "^after_finish|^log_level|^browser_headless" /home/march7thassistant/config.yaml

# 2. 看容器实际读到的什么
sudo docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' m7a | grep MARCH7TH
```

**两边不一致时，以环境变量为准，且必须改 compose。**

### 3.2 当前生效配置对照

| 配置项 | config.yaml | compose ENV | 实际生效 |
|---|---|---|---|
| `after_finish` | `Exit` | `MARCH7TH_AFTER_FINISH=Loop` | **Loop** |
| `log_level` | `INFO` | `MARCH7TH_LOG_LEVEL=INFO` | INFO |
| `browser_headless_enable` | `true` | `MARCH7TH_BROWSER_HEADLESS_ENABLE=true` | true |
| `loop_mode` | `scheduled` | — | scheduled |
| `scheduled_time` | `8:00` | — | 每天 8:00 |

> `after_finish` 两边不一致是**已知且有意为之**：以 Loop 为准实现
> 「每天 8 点自动跑」。若要彻底一致，可把 config.yaml:34 也改成 `Loop`。

### 3.3 环境变量覆盖表（官方 Docker.md:170-180）

| 环境变量 | 说明 | 官方默认 |
|---|---|---|
| `MARCH7TH_CLOUD_GAME_ENABLE` | 启用云游戏模式 | `true` |
| `MARCH7TH_BROWSER_HEADLESS_ENABLE` | 无头浏览器模式 | `true` |
| `MARCH7TH_BROWSER_HEADLESS_RESTART_ON_NOT_LOGGED_IN` | 未登录时重启浏览器 | `false` |
| `MARCH7TH_BROWSER_DOWNLOAD_USE_MIRROR` | 浏览器下载走镜像 | `false` |
| `MARCH7TH_AFTER_FINISH` | 任务完成后操作 | **`Loop`** |
| `MARCH7TH_LOG_LEVEL` | 日志等级 | - |
| `MARCH7TH_NO_RUN_IMMEDIATELY` | 启动时禁用立即运行 | `false` |

> 官方术语对照：**「每天定时执行」= `AFTER_FINISH=Loop`**
> （见镜像内 `/m7a/assets/docs/Termux.md:115`）。
> `Exit` 是「跑一次就退出」，不是定时模式。

### 3.4 config.yaml 非原子写入（已知缺陷，已加守护）

**缺陷来源**：compose 把 config.yaml 当**单文件**挂载：

```yaml
- ./config.yaml:/m7a/config.yaml
```

`os.replace()` 换 inode 时撞上挂载点报 `EBUSY`（永久性错误，不重试），
上游降级为**原地覆盖写入**。上游注释原文（`module/config/config.py:419`）：

```
# 1. Docker 单文件挂载的 config.yaml：rename 撞上挂载点报 EBUSY（永久性，未重试）；
# 2. Windows 共享冲突重试用尽（WinError 5/32）或其他替换错误。
# 降级为原地覆盖写入（非原子，写入中途被中断会截断配置文件），
# 因此降级前先把当前配置复制一份留底。
```

**为什么不修**：改挂目录会让容器内 `/m7a/config.yaml` 消失
（`module/config/__init__.py:8` 是相对路径），程序会重新生成默认配置，
**356 个字段全丢**。实测验证过，不可行。

**风险量化**：降级写入用 `shutil.copyfileobj` 拷 45KB，耗时 **0.1~1 毫秒**。
只有这个窗口内被 SIGKILL 才截断。

**防线**：`config_guard.py` 每分钟校验（详见 4.3）。

---

## 第四章 已实施的非上游改动

> 所有改动都可回退，原文件均保留 `.orig` 或 `.bak.*` 备份。

### 4.1 上游代码补丁（挂载方式注入，不动镜像）

compose 里两条只读挂载：

```yaml
- ./patch/m7a/utils/image_utils.py:/m7a/utils/image_utils.py:ro
- ./patch/m7a/module/screen/screen.py:/m7a/module/screen/screen.py:ro
```

#### 补丁 1：图片缓存（`patch/m7a/utils/image_utils.py`，10646 B）

**治的病**：`ImageUtils.read_image()` 每次调用都 `np.fromfile` + `cv2.imdecode`，
界面识别每秒调约 490 次（49 模板 × 10 重试 × 每秒 1 帧）。

**改法**：类级 LRU 缓存

```python
_CACHE_LOCK = threading.RLock()
_CACHE = {}
_CACHE_MAX = 512
# key = (abspath, st_mtime_ns, st_size, int(flags))
```

**验证**：缓存命中返回同一对象（`img2 is img1`）、1000 次重复后 cache 仍 1 条、
灰度/彩色分开缓存、20 线程 × 200 次并发安全。

#### 补丁 2：线程池限流（`patch/m7a/module/screen/screen.py`，20622 B）

**治的病**：`get_current_screen()` 每轮开 `len(screen_map)` = **57 个线程**，
`shutdown(wait=False)` 不等待 → 线程跨轮堆积。

**改法**：

```python
_MAX_MATCH_WORKERS = 8
executor = ThreadPoolExecutor(max_workers=min(len(self.screen_map), _MAX_MATCH_WORKERS))
try:
    futures = [executor.submit(find_screen, name, screen) for name, screen in self.screen_map.items()]
    for future in as_completed(futures):
        if future.result() is not None:
            break
finally:
    executor.shutdown(wait=True, cancel_futures=True)   # ← wait=True 是关键
```

**效果**：峰值内存 3.7G → 1.86G。

#### 补丁的维护要点

镜像升级后补丁**仍然有效**（因为是挂载覆盖，不依赖镜像内容），但如果
上游改了这两个文件的函数签名，需要在**新镜像里重新核对**：

```bash
# 核对补丁是否还能加载
sudo docker exec -w /m7a m7a python3 -c "
import inspect
from module.screen.screen import Screen
src = inspect.getsource(Screen.get_current_screen)
print('_MAX_MATCH_WORKERS' in src, 'wait=True' in src)"
```

> **验证坑**：用 `PYTHONPATH` 覆盖 `module` 包会因 `module/screen/__init__.py`
> 的 `from module.logger import log` 失败。要验证就用上面 `docker exec` 的方式。

### 4.2 面板 v1.28.2（容器健康接入告警）

**仓库**：`/home/huangsongping/dsh-workspace/m7a-fix`，commit `5cdfe02`

| 文件 | 改动 |
|---|---|
| `services/sysmetrics.py` | `container_info` 增加 `health` / `health_since` / `health_fails` / `restarting` / `oom_killed` |
| `services/sysmetrics.py` | **新增 `_dechunk()`**，修 `sock_json` 不解 chunked 的老 bug |
| `services/monitor.py` | 新增 `container_health()`；`Running=False` 时把 health 归一为 `''` |
| `services/alerts.py` | 新增 `_observe_unhealthy_edge()`、`_unhealthy_body()`；kind 用 `unhealthy` / `unhealthy_recovered` |
| `services/alerts.py` | `alert_health()` 增加 `container_health` / `container_fails` / `container_oom` |
| `static/panel.js` | `ALERT_KIND` 加 `unhealthy` 配色 + 筛选项 |
| `static/panel.css` | `.alert-tl-dot.unhealthy` / `.alert-tl-kind.unhealthy` |

**顺带修的真 bug（`sock_json` 不解 chunked）**

Docker Engine API 一律回 `Transfer-Encoding: chunked`，原实现直接把 chunked 帧
（`1e9c\r\n{json}\r\n0\r\n\r\n`）交给 `json.loads`，必然 ValueError →
`(200, None)` → 所有 `container_info` 静默退回 `docker inspect` 子进程。

发现方式：

```bash
curl -s --unix-socket /var/run/docker.sock -D - -o /dev/null http://docker/containers/x/json
# 响应头里有 Transfer-Encoding: chunked
```

**测试**：新增 `tests/test_v1282_healthcheck_alert.py`（15）+
`tests/test_v1282_sock_chunked.py`（6），全量 **1435 passed / 1 skipped**，ruff 全过。

### 4.3 配置完整性守护（`config_guard.py`）

**位置**：`/home/march7thassistant/config_guard.py`（5576 B）

**难在哪**：截断到 5000 字节的 config.yaml **仍然语法合法**
（能解析出 39 个字段，`yaml.safe_load` 不报错）。只判「能否解析」会漏掉。

**三重判定**（任一不满足即恢复）：

1. 能解析成 dict
2. 顶层字段数 ≥ 历史良好副本的最小值
3. 体积 ≥ 基线 60%

**良好副本**：按 sha256 去重存 `config-backup/config.yaml.<sha16>`，保留 20 份。

**cron**（root crontab）：

```
* * * * * /usr/bin/python3 /home/march7thassistant/config_guard.py >/dev/null 2>&1
```

> **cron 的坑**：脚本 `log()` 既写文件又 `print`，如果 cron 再 `>>` 到同一个日志
> 文件，每行会出现两次。所以必须 `>/dev/null 2>&1`。

**实测三种损坏场景均恢复成功**：

| 场景 | 检出依据 | 结果 |
|---|---|---|
| 截断到 5000B（合法 YAML，39 字段） | 字段数 39 < 基线 356 | ✅ 恢复 45429B / 356 字段 |
| 截断到 100B（非法 YAML） | 无法解析为 dict | ✅ 恢复 |
| 文件被删 | 不存在 | ✅ 恢复 |

**基线**：45429 B / 356 字段 / sha256 前缀 `a63893fcb23874e9`

### 4.4 compose 增强项

```yaml
mem_limit: 4g                    # 实测峰值 1.86G，留一倍余量
memswap_limit: 4g
restart: on-failure:3            # 真崩最多 3 次，不无限重启
init: true                       # tini 回收僵尸进程
shm_size: 1g
logging:                         # json-file 默认无上限，会吃满磁盘
  driver: json-file
  options:
    max-size: "20m"
    max-file: "5"
healthcheck:                     # m7a 没有 HTTP 端口，用 pgrep 判活
  test: ["CMD-SHELL", "pgrep -f 'python main.py' >/dev/null || exit 1"]
  interval: 60s
  timeout: 10s
  retries: 3
  start_period: 120s
environment:
  - TZ=Asia/Shanghai
  - MARCH7TH_LOG_LEVEL=INFO
  - MARCH7TH_AFTER_FINISH=Loop
  - MARCH7TH_BROWSER_HEADLESS_ENABLE=true
  - MARCH7TH_BROWSER_HEADLESS_RESTART_ON_NOT_LOGGED_IN=false
```

---

## 第五章 故障案例库：2026-11-10 大故障完整复盘

### 5.1 时间线与表象

```
08:04  开始跑，云游戏剩余 809 分钟
08:32  首次出现「截图失败：没有找到游戏窗口」，每秒 1 条，持续 60 秒
...
18:28  最后一次内核 OOM
21:39  人工停止，云游戏剩余 6 分钟（烧掉 803 分钟）
```

**当天统计**

| 指标 | 数值 |
|---|---|
| 日志体积 | 15,825,596 B / 134,834 行 |
| 内核 OOM | **26 次**（21 次 python + 5 次 chrome） |
| 「未识别出任何界面」 | **1850 条**（每小时 162–194 条，平均 19.5 秒一条） |
| 「截图失败：没有找到游戏窗口」 | 1220 条（20 次爆发 × 每次 61 条 × 持续 60 秒） |
| 界面识别失败引发的云游戏时长消耗 | **803 分钟** |
| 任务成功数 | **0** |

### 5.2 三个叠加的根因

#### 根因 A：内存暴涨 → OOM 被杀

```
16:54:16  1.29G → 16:54:32 1.19G → 16:54:49 2.03G → OOM
16:54:58  Memory cgroup out of memory: Killed process 1802123 (python)
          total-vm:9061056kB, anon-rss:3742500kB
```

爆炸点在 `screen.get_current_screen()`：

1. **57 线程并行**，`shutdown(wait=False)` 不等待 → 线程跨轮堆积
2. **图片无缓存**，每秒 490 次 `np.fromfile` + `imdecode`
3. **容器原本无内存上限**（`HostConfig.Memory=0`），靠宿主 7.8G 硬扛

> **「为什么以前没问题」的答案**
>
> `screen.py` 的多线程逻辑早于 2026-01 就存在，
> v2026.11.7 与 v2026.11.8 的 `screen.py` md5 完全相同
> （`a7d423abf13630f5087a117dcb89453f`），**不是升级引入的**。
>
> 真正差异是 **mem_limit**：无上限时 python 涨到 5.4G 也能勉强跑完；
> 2026-11-10 18:45 第一次加 `mem_limit: 3g` 之后，变成「一超 cgroup 天花板必死」，
> 把长期存在的内存缺陷暴露了出来。

#### 根因 B：`invalid session id` → 死循环

```
截图失败：Message: invalid session id; For documentation on this error,
please visit: https://www.selenium.dev/documentation/webdriver/troubleshooting/errors#invalidsessionidexception
```

失效机制（`module/game/cloud.py:405-435` `_connect_or_create_browser`）：

```
if not connect_only:
    self.close_all_m7a_browser(headless=not headless)
    existing = self.get_m7a_browsers(headless=headless)
if existing:
    options.debugger_address = f"127.0.0.1:{reconnect_port}"
    self.driver = webdriver_type(...)     # ← 重连旧浏览器并 return，不启动新的
```

上一轮 `stop_game()` 里 `driver.quit()` 有 15 秒超时，超时只 log 不处理
→ chrome 半死 → 下轮 `existing` 非空 → 重连坏 session → 截图 60 次全失败
→ `RuntimeError("截图超时")` → `except Exception` → `raise` → 进程退出
→ 容器 `restart: unless-stopped` 拉起 → 死循环。

**放大器**：容器 ENV `MARCH7TH_BROWSER_HEADLESS_ENABLE=true`，
而 config.yaml 里是 `false` → `close_all_m7a_browser(headless=not headless)`
的 `not headless = false`，**只关非 headless 那批**，上一轮的 headless chrome
存活被当成「现有浏览器」重连。

#### 根因 C：界面识别失败 1850 条

云游戏画面识别不出来，每 19.5 秒重试一次，持续约 28 分钟后放弃，
`当前界面：未知` → `发生错误 截图超时` → 立刻重开下一轮。

### 5.3 修复与验证

| 修复 | 说明 | 验证 |
|---|---|---|
| `browser_headless_enable: false → true` | 与 ENV 对齐 | invalid session id 归零 |
| 补丁 1：图片缓存 | LRU 512 条 | 内存不再暴涨 |
| 补丁 2：线程池 8 | `wait=True` | 峰值 3.7G → 1.86G |
| `mem_limit: 4g` | 留一倍余量 | 实测峰值 1.86G |
| `restart: on-failure:3` | 不无限重启 | 真崩最多 3 次 |
| 升级 v2026.11.8 | 含 #1315「修复云游戏流程无法连接已有会话」 | 见下 |
| `config_guard.py` | 每分钟校验 | 三种场景实测可恢复 |

**首轮完整成功日志（18:41:31–18:44:48）**

```
18:41:45 进入云游戏成功
18:42:57 smtp 通知
18:43:17 开拓力: 29/300
18:44:03 当前界面：未知            ← 仅 1 条（对比全天 1850 条）
18:44:36 委托奖励完成
18:44:40 每日实训奖励完成
18:44:48 关闭浏览器成功
内存: 1.254G → 1.855G(峰值) → 1.634G(回落) → 退出
RestartCount=0  OOMKilled=false
```

**18:41 之后全量核查**：`invalid session` / `截图超时` = **0 条**

### 5.4 修复前后对比

| 指标 | 修复前 | 修复后 |
|---|---|---|
| 内存峰值 | 5.4G（无上限）/ 3.7G（3g 上限） | **1.86G** |
| 内存形态 | python RSS 只涨不跌 | 涨 0.4G 后会回落 |
| 界面识别失败 | 1850 条/天 | 偶发 1 条/轮 |
| `invalid session id` | 60 条/轮 | **0** |
| 任务成功数 | 0 | 每轮完成 |
| 云游戏消耗 | 803 分钟/天白烧 | 跑完即关浏览器 |

---

## 第六章 监控与告警

### 6.1 面板健康看板

面板 → 概览页顶部 `.ahbar` 健康条，六档：

| level | 含义 |
|---|---|
| `off` | 告警未启用 |
| `setup` | 已启用但没填接收地址 |
| `crit` | 连续失败达阈值，或从未成功过 |
| `warn` | 最近一次失败 / 还在重试队列 |
| `ok` | 最近一次成功 |

### 6.2 容器健康（v1.28.2 新增）

容器 `healthcheck` 60 秒一次，连续 3 次失败 → `unhealthy` → 面板推
**critical「容器内部异常」**（kind = `unhealthy`）。

> **与 down 告警的区别**
>
> - `down`：容器 `Running=false`（进程没了）
> - `unhealthy`：容器 `Running=true` 但探针连续失败（进程还在但卡住/坏了）
>
> 两者独立记账（`alerted_down` / `alerted_unhealthy`），互不干扰。
> `run=False` 时不评估 unhealthy（归 down 管），避免同一故障推两条。

**边界处理**：容器正常退出后 Docker 会把最后一次探测结论
（通常 `unhealthy`）留在 `State.Health`。`container_health()` 在
`Running=False` 时把 health 归一为 `''`，避免把「容器根本没在跑」
显示成「容器活着但病了」。

### 6.3 ⚠️ 告警推送通道当前是坏的

`/www/wwwroot/m7a-panel/data/alert_state.json`：

```json
{
  "push_fail_streak": 32,
  "push_last_fail_msg": "未配置接收地址",
  "push_last_ok_at": 0
}
```

**从未推送成功过**。需要在面板配置页填接收地址（Bark / 企业微信等）。

> 面板侧 `alert_healthy` 会显示 `level=setup`、`label=未配置接收地址`，
> 但用户不看页面就永远不知道。**这是目前最大的监控盲区。**

### 6.4 建议补充的监控

- 云游戏剩余时长低于阈值时告警（当前每轮日志 `云游戏剩余时长：X 分钟`）
- 磁盘使用率（109G 已用 58%）
- 宿主 swap 使用率（4G swap 经常用 3.8G）

---

## 第七章 备份与回滚

### 7.1 compose 备份

```
/home/march7thassistant/docker-compose.yml.bak.optimize-20261111-201814
/home/march7thassistant/docker-compose.yml.bak.loop-20261111-221423
/home/march7thassistant/docker-compose.yml.bak.mem4g-20261111-184956
/home/march7thassistant/docker-compose.yml.bak.patch-20261111-181541
/home/march7thassistant/docker-compose.yml.bak.screen-20261111-183436
/home/march7thassistant/docker-compose.yml.bak.20261031-100200   ← 最原始
```

回滚：

```bash
sudo cp /home/march7thassistant/docker-compose.yml.bak.<时间戳> /home/march7thassistant/docker-compose.yml
sudo docker compose -f /home/march7thassistant/docker-compose.yml up -d --force-recreate
```

### 7.2 面板备份

```bash
ls -lt /www/wwwroot/m7a-panel/backups/
# pre-v1.28.2-20261111-205507.tar.gz   ← 当前回滚点
# pre-v1.28.0-20261107-114713.tar.gz
```

回滚：

```bash
sudo tar xzf /www/wwwroot/m7a-panel/backups/pre-v1.28.2-<时间戳>.tar.gz -C /www/wwwroot/m7a-panel
sudo systemctl restart m7a-panel
```

### 7.3 整机重装备份

`/home/huangsongping/m7a-redeploy-backup-20261111-121913/`

| 文件 | 大小 | 说明 |
|---|---|---|
| `config.yaml` | 45716 B | sha256 `5f4ae428...` |
| `docker-compose.yml` | 2741 B | sha256 `70728087...` |
| `UserProfile.tar.gz` | 54,333,802 B | 浏览器凭证（535 条目） |
| `config-history.tar.gz` | 126166 B | 9 份历史配置 |

### 7.4 浏览器凭证备份

```
/home/march7thassistant/cookies-backup-20261110-2145.tar.gz   153863 B
```

### 7.5 镜像回退

```bash
# 改回旧镜像
sudo docker compose -f /home/march7thassistant/docker-compose.yml down
# 编辑 compose 的 image: 行为 e47eeb59e812 对应的 tag，或直接用 ID
sudo docker compose -f /home/march7thassistant/docker-compose.yml up -d --force-recreate
```

---

## 第八章 故障速查表

| 现象 | 先查什么 | 常见原因 | 处理 |
|---|---|---|---|
| 容器不停重启 | `docker inspect` 看 `ExitCode` | `ExitCode=137` = OOM 或 SIGKILL | 看内核日志 `journalctl -k \| grep -i "out of memory"` |
| 内存爆到 4G 上限 | `docker stats` | 补丁没挂上 | `docker exec m7a python3 -c "import inspect; from module.screen.screen import Screen; print('_MAX_MATCH_WORKERS' in inspect.getsource(Screen.get_current_screen))"` |
| 日志刷屏 `invalid session id` | 带色码 grep 不到，先 sed 去色 | 上一轮 chrome 半死 | 停容器 → 确认 `ss -tlnp \| grep 922` 未占用 → 冷启动 |
| 界面识别一直失败 | 看云游戏画面 | 画面停在加载/登录页 | 面板预览里手动登录 |
| 配置被改回去了 | 面板保存后无效 | ENV 覆盖了 config.yaml | 改 compose，`--force-recreate` |
| config.yaml 变成几十行 | `config-guard.log` | 非原子写入被截断 | 守护会自动恢复，看日志确认 |
| 容器跑完就停 | `docker logs` 看最后一行 | `AFTER_FINISH=Exit` | 改 compose 为 `Loop` |
| 容器不自动跑 | `docker logs` 看有没有「将在明天X时Y分继续运行」 | 正常等待中 | 等，或改 `scheduled_time` |
| 云游戏时长莫名减少 | 统计日志 `云游戏剩余时长` 的下降 | 任务在跑 / 偶发失败重试 | 正常消耗；异常下降看是否有识别失败 |
| `[config] save_config os.replace 失败 EBUSY` | — | 单文件挂载的正常降级 | **无损，不用管**（守护会兜底） |
| 面板 9999 打不开 | `systemctl status m7a-panel` | 服务挂了 | `systemctl restart m7a-panel` |
| 面板显示「preview_server 不可达」 | `docker exec m7a ps \| grep preview` | 预览服务没起来 | 面板会自动 `docker exec -d` 拉起，60 秒节流 |
| 面板看不到容器健康 | `docker inspect` 有没有 `State.Health` | compose 没配 healthcheck | 加上 healthcheck 并 `--force-recreate` |
| 告警收不到 | 面板概览页 `.ahbar` 健康条 | 没填接收地址（当前就是这个） | 配置页填 Bark/企微地址 |

### 8.1 排查三件套

```bash
# 1. 容器状态
sudo docker inspect -f 'Status={{.State.Status}} ExitCode={{.State.ExitCode}} RestartCount={{.RestartCount}} Health={{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' m7a

# 2. 最近日志（去色）
sudo docker logs --tail 100 m7a 2>&1 | sed 's/\x1b\[[0-9;]*m//g' | grep -v "^[+|]"

# 3. 内核 OOM
sudo journalctl -k --since "today" | grep -i "out of memory" | tail -5
```

---

## 第九章 已知限制与未解问题

### 9.1 上游已知缺陷（我们打了补丁或绕开）

| 缺陷 | 状态 |
|---|---|
| 界面识别 57 线程不限流 | ✅ 补丁 2 |
| 图片无缓存 | ✅ 补丁 1 |
| `_connect_or_create_browser` 重连半死 chrome | ⚠️ v2026.11.8 修了连接逻辑，但 `driver.quit()` 超时仍只 log |
| config.yaml 单文件挂载非原子写入 | ⚠️ 上游知情不修，我们用 `config_guard.py` 兜底 |
| `close_all_m7a_browser(headless=not headless)` 逻辑依赖配置一致 | ⚠️ 必须保持 config.yaml 与 ENV 的 headless 一致 |

### 9.2 未解问题

1. **11-10 界面识别失败的真实原因**：云游戏画面到底停在什么状态？
   需要看画面才能定论。已通过 v2026.11.8 的 #1315 缓解，但未确认根因。

2. **每轮必发「发现新版本」SMTP**：current=v2026.11.7 vs 远端 v2026.11.8，
   升级后应已消失，需观察。

3. **container-export 偶发失败**：`从手机菜单进入商店失败` /
   `进入余烬兑换失败`，实测今天只失败 2 次，其它轮次正常。
   判定为云游戏切换画面时偶发 OCR 未命中，重试 8 次后放弃，
   **不影响核心任务，不修**。
   > 已排除：分辨率不匹配（CDP 实测 1920×1080 与硬编码一致）、
   > crop 坐标失真（`MENU_TEXT_CROP` 是比例坐标）、
   > 上游已知 issue（#1299 已于 11-01 修复，我们的镜像已含）。

4. **`after_finish` 两边不一致**：config.yaml 写 `Exit`，ENV 写 `Loop`。
   实际以 Loop 为准。若要彻底一致，需把 config.yaml:34 也改成 `Loop`。

5. **`scheduled_time` 官方默认 4:00，本机是 8:00**：这是有意配置，非缺陷。

6. **面板 `m7a-panel.service` 曾有「unit file changed on disk」警告**：
   2026-11-11 已执行 `systemctl daemon-reload` 消除。

### 9.3 容量风险

| 项 | 现状 | 风险 |
|---|---|---|
| 磁盘 | 109G 用 59G（58%） | 中（日志已加轮转，缓解） |
| 内存 | 8G，当前用 3.2G | 低 |
| Swap | 4G，经常用 3.8G | **中**（说明内存偏紧） |
| 云游戏时长 | 随消耗递减 | 高（烧光后要等回满或付费） |

---

## 第十章 变更记录

### 2026-11-11

| 时间 | 变更 | 影响 |
|---|---|---|
| 09:19 | 镜像 v2026.11.7 → v2026.11.8 | 含云游戏连接修复 |
| 12:19 | 整目录重装（用户确认） | 误删 `logs/preview_server.py` |
| 13:04 | 重建 `logs/preview_server.py` | 面板预览恢复 |
| 14:34 | `browser_headless_enable: false → true` | invalid session id 归零 |
| 16:47 | `restart: unless-stopped → on-failure:3` | 不再无限重启 |
| 18:14 | 补丁 1：图片缓存 | 内存不再暴涨 |
| 18:34 | 补丁 2：线程池 8 + wait=True | 峰值 3.7G → 1.86G |
| 18:41 | **首轮完整成功** | 验证修复有效 |
| 18:49 | `mem_limit: 6g → 4g`（用户要求） | 实测峰值 1.86G，够用 |
| 19:07 | `config_guard.py` + cron | 配置截断防护 |
| 20:18 | 日志轮转 20m×5 + healthcheck + TZ | 运维增强 |
| 20:55 | 面板 v1.28.2 部署 | 健康接入告警 + chunked 修复 |
| 21:29 | 面板 `daemon-reload` | 消除陈旧警告 |
| 22:14 | `AFTER_FINISH: Exit → Loop`（用户要求） | 恢复每天 8:00 自动跑 |

### 2026-11-10（大故障当天）

| 时间 | 变更 |
|---|---|
| 18:45 | `mem_limit: 3g`（首次止血） |
| 19:00 | `MARCH7TH_AFTER_FINISH: Loop → Exit`（后来证实是理解错误） |
| 19:00 | `MARCH7TH_LOG_LEVEL: DEBUG → INFO` |
| 21:45 | 清除浏览器登录凭证（用户要求） |
| 21:39 | 容器人工停止 |
| — | 全量日志分析与审计包制作 |

---

## 附录 A：常用命令速查

```bash
# === 状态 ===
sudo docker inspect -f 'Status={{.State.Status}} Health={{.State.Health.Status}} RestartCount={{.RestartCount}}' m7a
sudo docker stats --no-stream m7a
free -m
df -h /

# === 日志 ===
sudo docker logs --tail 200 m7a 2>&1 | sed 's/\x1b\[[0-9;]*m//g' | tail -50
sudo tail -100 /home/march7thassistant/logs/$(date +%Y-%m-%d).log

# === 配置 ===
sudo grep -nE "^after_finish|^log_level|^browser_headless|^loop_mode|^scheduled_time" /home/march7thassistant/config.yaml
sudo docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' m7a | grep MARCH7TH

# === 启停 ===
sudo docker compose -f /home/march7thassistant/docker-compose.yml up -d --force-recreate
sudo docker compose -f /home/march7thassistant/docker-compose.yml down

# === 面板 ===
sudo systemctl restart m7a-panel
sudo systemctl status m7a-panel
systemctl is-active m7a-panel && curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:9999/

# === 配置守护 ===
sudo tail -20 /home/march7thassistant/logs/config-guard.log
sudo crontab -l | grep config_guard

# === CDP 调试 ===
sudo docker exec m7a python3 -c "
import urllib.request, json
print(json.load(urllib.request.urlopen('http://127.0.0.1:9222/json/list')))"
```

## 附录 B：面板测试

```bash
# 建测试环境（本机 /tmp 会被清空，需重建）
cd /home/huangsongping/dsh-workspace/m7a-fix
python3 -m venv /tmp/auditvenv
/tmp/auditvenv/bin/pip install -r panel_migr/requirements.lock -r panel_migr/requirements-dev.lock

# 跑测试
cd panel_migr
/tmp/auditvenv/bin/python -m pytest -q
# 当前基线：1435 passed, 1 skipped

# ruff
/tmp/auditvenv/bin/python -m ruff check services/ tests/
```

> **坑**：不要装最新版依赖。必须按 `requirements.lock` 的锁定版本
> （fastapi 0.141.1 / starlette 1.3.1 / uvicorn 0.44.0 等），
> 盲目装最新版会导致 pytest 卡死 40+ 分钟。

---

*文档维护：每次改动后请在「第十章 变更记录」追加一行。*
