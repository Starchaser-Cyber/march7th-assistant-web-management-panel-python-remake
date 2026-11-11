# 三月七小助手 运维维护手册

> **这份文档要取代的**：原版 [README](README.md) 里那几段零散的「常见问题 / 故障排查」，
> 以及上一位维护者留下的 [TROUBLESHOOTING.md](TROUBLESHOOTING.md)（只覆盖 H.264 预览一件事）。
>
> **它覆盖什么**：从「第一次把 m7a 跑起来」到「三更半夜容器被 OOM 杀掉」的完整运维知识。
> 不是功能说明书，是**出事时能照着敲的排查手册**。
>
> 所有命令、路径、数字都在真实生产环境验证过（2026-11-11 现场核对），不是抄来的。

---

## 目录

- [第 0 章 五分钟快速上手](#第-0-章-五分钟快速上手)
- [第 1 章 系统全貌](#第-1-章-系统全貌)
- [第 2 章 部署与升级](#第-2-章-部署与升级)
- [第 3 章 日常运维](#第-3-章-日常运维)
- [第 4 章 配置管理（最大的坑在这里）](#第-4-章-配置管理最大的坑在这里)
- [第 5 章 内存与性能](#第-5-章-内存与性能)
- [第 6 章 云游戏与浏览器](#第-6-章-云游戏与浏览器)
- [第 7 章 监控与告警](#第-7-章-监控与告警)
- [第 8 章 面板（m7a_panel）](#第-8-章-面板m7a_panel)
- [第 9 章 游戏画面预览](#第-9-章-游戏画面预览)
- [第 10 章 日志体系](#第-10-章-日志体系)
- [第 11 章 备份与回滚](#第-11-章-备份与回滚)
- [第 12 章 故障速查表（按现象查）](#第-12-章-故障速查表按现象查)
- [第 13 章 故障案例库](#第-13-章-故障案例库)
- [第 14 章 已知缺陷清单](#第-14-章-已知缺陷清单)
- [第 15 章 容量与风险](#第-15-章-容量与风险)
- [第 16 章 变更记录](#第-16-章-变更记录)
- [附录 A 常用命令速查](#附录-a-常用命令速查)
- [附录 B 上游代码补丁说明](#附录-b-上游代码补丁说明)
- [附录 C 面板开发与测试](#附录-c-面板开发与测试)
- [附录 D 术语表](#附录-d-术语表)

---

## 第 0 章 五分钟快速上手

### 0.1 如果容器出问题了，先跑这三条

```bash
# ① 容器活着吗
sudo docker inspect -f 'Status={{.State.Status}} ExitCode={{.State.ExitCode}} RestartCount={{.RestartCount}} Health={{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' m7a

# ② 最近日志（必须去 ANSI 色码，否则关键字 grep 不到）
sudo docker logs --tail 100 m7a 2>&1 | sed 's/\x1b\[[0-9;]*m//g' | grep -v "^[+|]"

# ③ 是否被 OOM 杀过
sudo journalctl -k --since "today" | grep -i "out of memory" | tail -5
```

### 0.2 如果不知道从哪查

跳 [第 12 章 故障速查表](#第-12-章-故障速查表按现象查)，
按「现象」列找，每一行都给了「先查什么 → 常见原因 → 处理」。

### 0.3 三条最容易踩的坑（先记住）

| 坑 | 一句话 |
|---|---|
| **环境变量覆盖配置文件** | compose 里的 `MARCH7TH_*` 优先级高于 `config.yaml`，改配置页面可能无效 |
| **日志带 ANSI 色码** | `grep "invalid session id"` 会 0 命中，必须先 `sed 's/\x1b\[[0-9;]*m//g'` |
| **改 compose 不 recreate 不生效** | 必须 `up -d --force-recreate`，否则内存限制 / ENV 全是旧的 |

---

## 第 1 章 系统全貌

### 1.1 三个组件

```
┌───────────────────────────────────────────────────────────────────┐
│ 宿主机（8G RAM + 4G swap，/dev/sdb1 109G）                         │
│                                                                    │
│  ┌─────────────────────────┐    ┌──────────────────────────────┐  │
│  │ m7a 容器                 │    │ m7a-panel（systemd 服务）     │  │
│  │ 三月七小助手本体          │◄───┤ 端口 9999，root 运行          │  │
│  │ 镜像 v2026.11.8          │    │ 读日志 / 控容器 / 推告警      │  │
│  │ /home/march7thassistant  │    │ /www/wwwroot/m7a-panel        │  │
│  │                          │    └──────────────────────────────┘  │
│  │ ├ docker-init (tini)     │                                      │
│  │ └ python main.py         │    ┌──────────────────────────────┐  │
│  │    └ chrome (CDP 9222)   │    │ 下载页（serve.py，非托管）    │  │
│  └─────────────────────────┘    │ 端口 18090                    │  │
│                                  └──────────────────────────────┘  │
└───────────────────────────────────────────────────────────────────┘
```

**三者关系**

- 小助手容器：真正干活的，进云游戏跑任务
- 面板：旁观者 + 控制端，读容器日志/状态，能启停容器，能在线更新
- 下载页：发布包的公开下载入口，与运维无关

### 1.2 路径速查

| 用途 | 路径 | 属主 |
|---|---|---|
| 小助手根目录 | `/home/march7thassistant` | `www:www` |
| compose 文件 | `/home/march7thassistant/docker-compose.yml` | `www:www` |
| 主配置 | `/home/march7thassistant/config.yaml` | `www:www` |
| 日志总文件 | `/home/march7thassistant/logs/YYYY-MM-DD.log` | root（容器写） |
| 单轮运行日志 | `/home/march7thassistant/logs/runs/<yyyyMMdd-HHmmss-xxxx>.log` | root |
| 浏览器凭证 | `/home/march7thassistant/3rdparty/WebBrowser/UserProfile/` | `www:www` |
| 上游补丁 | `/home/march7thassistant/patch/m7a/` | `www:www` |
| 配置守护 | `/home/march7thassistant/config_guard.py` | `www:www` |
| 配置良好副本 | `/home/march7thassistant/config-backup/` | root |
| 预览服务脚本 | `/home/march7thassistant/logs/preview_server.py` | root |
| 预览令牌 | `/home/march7thassistant/logs/preview_secret` | root |
| 面板线上代码 | `/www/wwwroot/m7a-panel/panel_migr` | root |
| 面板仓库 | `/home/huangsongping/dsh-workspace/m7a-fix` | 用户 |
| 面板回滚包 | `/www/wwwroot/m7a-panel/backups/` | root |
| 面板数据 | `/www/wwwroot/m7a-panel/data/` | root |
| 下载页 | `/home/huangsongping/dsh-workspace/m7a-dl` | 用户 |

### 1.3 端口

| 端口 | 服务 | 监听 | 对外 |
|---|---|---|---|
| 9999 | m7a-panel | `0.0.0.0:9999` | 是（反代后） |
| 18090 | 下载页 | `0.0.0.0:18090` | 是 |
| 8000 | 其它服务（无关） | — | — |
| 9222 | 容器内 chrome CDP | 容器内 | 否 |
| 9223 | 容器内 preview_server | 容器内 `172.17.0.x` | 否 |

> 9222/9223 都在容器网络内，宿主 `ss -tlnp` 能看到 `*:9223` 但不可从外网访问。

### 1.4 镜像与版本

```
镜像:  ghcr.nju.edu.cn/moesnow/march7thassistant:latest
ID:    sha256:4253b8cddbb164e82e209b4a2a9a590046daa4133def53d2779e1f90c2407d82
版本:  v2026.11.8
上游:  17ec9ddc82208aa2f2e01f4deaa20527ff176490
构建:  2026-11-07T20:45:38Z
```

**怎么查当前版本**

```bash
# 方法一（推荐）：读 OCI label
sudo docker image inspect -f '{{index .Config.Labels "org.opencontainers.image.version"}} / {{index .Config.Labels "org.opencontainers.image.revision"}}' \
  $(sudo docker inspect -f '{{.Image}}' m7a)

# 方法二：读镜像内文件
sudo docker run --rm --entrypoint sh ghcr.nju.edu.cn/moesnow/march7thassistant:latest \
  -c 'cat /m7a/assets/config/version.txt'
```

> **注意**：镜像内没有 `.git`，只能靠 OCI label 或 `version.txt` 定位版本。

**保留的可回退旧镜像**

| 镜像 ID | 版本 | 说明 |
|---|---|---|
| `4253b8cddbb1` | v2026.11.8 | **当前使用** |
| `e47eeb59e812` | v2026.11.7 | 上一版，可回退 |
| `f2b8b0f21588` | v2026.10.31 系列 | 更旧 |

### 1.5 ⚠️ sudo 用法的三个坑

```bash
# 正确写法
echo '你的密码' | sudo -S -p '' <命令>
```

| 坑 | 现象 | 正确做法 |
|---|---|---|
| heredoc 组合命令 | `sudo tee <<EOF` 失败，触发「3 次错误密码尝试」锁定 | 先 `write` 到 `/tmp`，再 `sudo cp` |
| 权限 | `/home/march7thassistant` 属主 `www:www`，非 sudo 无写权限 | 全部操作加 `sudo -S` |
| 容器内 `/tmp` 不共享 | 容器写到 `/tmp` 的文件宿主机看不到 | 写到 `/m7a/logs/`（= 宿主 `logs/`） |

### 1.6 环境基线

| 项 | 值 |
|---|---|
| 发行版 | Ubuntu 24.04.4 LTS |
| 内核 | 7.0.0-38-generic |
| CPU | Intel i3-4160（2 核 4 线程） |
| 内存 | 7856 MB + 4095 MB swap（`/swap.img`） |
| 磁盘 | `/dev/sdb1` 109G，已用约 58% |
| Docker | cgroup v2 + systemd driver，data-root `/srv/st-stack/docker` |

---

## 第 2 章 部署与升级

### 2.1 首次部署清单

按顺序做，每步都能单独验证：

```bash
# ① 建目录
sudo mkdir -p /home/march7thassistant/{logs,3rdparty/WebBrowser/UserProfile,patch/m7a,config-backup}
sudo chown -R www:www /home/march7thassistant

# ② 放 compose（见 2.2）
sudo cp docker-compose.yml /home/march7thassistant/

# ③ 首次启动（自动生成默认 config.yaml）
sudo docker compose -f /home/march7thassistant/docker-compose.yml up -d --force-recreate

# ④ 配置 config.yaml（面板或直接编辑）
#    重点：cloud_game_enable / scheduled_time / 账号

# ⑤ 挂配置守护
sudo crontab -e   # 加一行，见 4.5

# ⑥ 验证
sudo docker logs --tail 50 m7a 2>&1 | sed 's/\x1b\[[0-9;]*m//g'
```

### 2.2 参考 compose（本机生产版）

```yaml
services:
  m7a:
    image: ghcr.nju.edu.cn/moesnow/march7thassistant:latest
    container_name: m7a
    restart: on-failure:3          # 真崩最多 3 次，不无脑重启
    init: true                     # tini 回收僵尸
    shm_size: 1g
    mem_limit: 4g                  # 实测峰值 1.86G，留一倍余量
    memswap_limit: 4g
    environment:
      - MARCH7TH_LOG_LEVEL=INFO
      - MARCH7TH_AFTER_FINISH=Loop
      - MARCH7TH_BROWSER_HEADLESS_ENABLE=true
      - MARCH7TH_BROWSER_HEADLESS_RESTART_ON_NOT_LOGGED_IN=false
      - TZ=Asia/Shanghai
    volumes:
      - ./config.yaml:/m7a/config.yaml
      - ./logs:/m7a/logs
      - ./3rdparty/WebBrowser/UserProfile:/m7a/3rdparty/WebBrowser/UserProfile
      # 上游补丁（见附录 B）
      - ./patch/m7a/utils/image_utils.py:/m7a/utils/image_utils.py:ro
      - ./patch/m7a/module/screen/screen.py:/m7a/module/screen/screen.py:ro
    logging:
      driver: json-file
      options:
        max-size: "20m"
        max-file: "5"
    healthcheck:
      test: ["CMD-SHELL", "pgrep -f 'python main.py' >/dev/null || exit 1"]
      interval: 60s
      timeout: 10s
      retries: 3
      start_period: 120s
```

**每一行的理由**

| 配置 | 为什么 |
|---|---|
| `restart: on-failure:3` | `unless-stopped` 会在死循环时无限重启；`on-failure:3` 崩 3 次就停住，让故障可见 |
| `init: true` | 容器内 chrome 会留僵尸进程，tini 回收 |
| `mem_limit: 4g` | 不设上限时 python 能涨到 5.4G 拖垮宿主；4g 是实测峰值的两倍 |
| `max-size 20m × 5` | json-file 默认**无上限**，会吃满 109G 磁盘 |
| `healthcheck pgrep` | m7a 没有 HTTP 端口，只能用进程判活 |
| `TZ=Asia/Shanghai` | 日志时间、`scheduled_time` 都依赖本地时区 |

### 2.3 升级镜像

```bash
# ① 记录当前版本（出问题要知道回退到哪）
sudo docker inspect -f '{{.Image}}' m7a

# ② 备份 compose
sudo cp /home/march7thassistant/docker-compose.yml \
        /home/march7thassistant/docker-compose.yml.bak.$(date +%Y%m%d-%H%M%S)

# ③ 拉新镜像
sudo docker compose -f /home/march7thassistant/docker-compose.yml pull

# ④ 重建（必须带 --force-recreate）
sudo docker compose -f /home/march7thassistant/docker-compose.yml up -d --force-recreate

# ⑤ 验证版本
sudo docker image inspect -f '{{index .Config.Labels "org.opencontainers.image.version"}}' $(sudo docker inspect -f '{{.Image}}' m7a)
```

### 2.4 ⚠️ 升级后必须检查补丁是否还兼容

补丁是按**文件路径挂载覆盖**的，镜像升级后如果上游改了这个文件的函数签名，
补丁会静默失效（甚至可能让程序起不来）。

```bash
# 检查补丁 2（线程池限流）是否真的加载了
sudo docker exec -w /m7a m7a python3 -c "
import inspect
from module.screen.screen import Screen
src = inspect.getsource(Screen.get_current_screen)
print('_MAX_MATCH_WORKERS' in src, 'wait=True' in src)"
# 期望输出：True True

# 检查补丁 1（图片缓存）
sudo docker exec -w /m7a m7a python3 -c "
import inspect
from utils.image_utils import ImageUtils
src = inspect.getsource(ImageUtils)
print('_CACHE' in src and '_cache_key' in src)"
# 期望输出：True
```

> **验证方法的坑**：不要用 `PYTHONPATH` 覆盖 `module` 包来测试 ——
> `module/screen/__init__.py` 里的 `from module.logger import log` 会失败。
> 一律用 `docker exec -w /m7a` 进容器验证。

### 2.5 整目录重装（谨慎）

**什么时候用**：目录结构损坏、凭证彻底失效、想从零开始。

**会丢什么**：`logs/` 里的 `preview_server.py` 与 `preview_secret`
（**2026-11-11 就这样丢过一次**），以及所有历史日志。

```bash
# ① 备份（缺一不可）
sudo tar czf /path/backup/config.yaml.tar.gz -C /home/march7thassistant config.yaml
sudo tar czf /path/backup/compose.tar.gz -C /home/march7thassistant docker-compose.yml
sudo tar czf /path/backup/UserProfile.tar.gz -C /home/march7thassistant 3rdparty/WebBrowser/UserProfile
sudo cp -a /home/march7thassistant/logs/preview_server.py /home/march7thassistant/logs/preview_secret /path/backup/

# ② 停并删
sudo docker compose -f /home/march7thassistant/docker-compose.yml down
sudo rm -rf /home/march7thassistant

# ③ 重建骨架
sudo mkdir -p /home/march7thassistant/{logs,3rdparty/WebBrowser/UserProfile}
sudo chown -R www:www /home/march7thassistant

# ④ 放回配置与凭证
# ⑤ 启动
# ⑥ 恢复预览服务（见第 9 章）
```

---

## 第 3 章 日常运维

### 3.1 启停容器

```bash
# 启动 / 应用 compose 变更（必须 --force-recreate）
sudo docker compose -f /home/march7thassistant/docker-compose.yml up -d --force-recreate

# 停止并移除
sudo docker compose -f /home/march7thassistant/docker-compose.yml down

# 只重启不重建（不推荐，ENV 变更不生效）
sudo docker restart m7a
```

> **为什么必须 `--force-recreate`**：
> `docker compose up -d` 在镜像和 compose 都"看起来没变"时不会重建容器。
> 改了 `mem_limit` 或 `environment` 后直接 `up -d`，容器还是旧的 `HostConfig`。
> **验证方法**：
> ```bash
> sudo docker inspect -f 'Memory={{.HostConfig.Memory}}' m7a
> # 改 4g 后应显示 4294967296
> ```

### 3.2 运行模式：Loop 定时

**当前配置**（compose ENV `MARCH7TH_AFTER_FINISH=Loop` + config.yaml `scheduled_time: 8:00`）：

```
┌──────────┐    ┌────────────┐    ┌─────────────┐    ┌──────────┐
│ 8:00 开跑 │───►│ 进云游戏   │───►│ 跑任务      │───►│ 关浏览器 │
└──────────┘    └────────────┘    └─────────────┘    └────┬─────┘
      ▲                                                    │
      │            ┌──────────────────────┐                │
      └────────────┤ 每分钟轮询等到明天8:00├◄───────────────┘
                   └──────────────────────┘
```

**实测日志**

```
22:18:39,465 | INFO | 关闭浏览器成功
22:18:39,556 | INFO | 将在明天8时0分继续运行
22:18:40,382 | INFO | smtp 通知发送完成
```

**等待期间的状态**

```
Status=running  Health=healthy  RestartCount=0
内存 374.8MiB / 4GiB    CPU 0.10%
进程树: docker-init → python main.py （chrome 已关闭）
```

> **关键**：`stop_game()` 在等待前就执行了，**浏览器已关，不占云游戏时长**。
> 这是官方设计，见 `/m7a/tasks/game/__init__.py:311 after_finish_is_loop()`。

### 3.3 各 `after_finish` 取值的行为

| 值 | 行为 | 适用 |
|---|---|---|
| `Loop` | 跑完 → 关浏览器 → 等到 `scheduled_time` → 自动开跑 | **每天定时跑（当前）** |
| `Exit` | 跑完 → 退出进程，容器停住 | 手动触发、跑一次 |
| `None` | 跑完 → 什么都不做（进程挂着） | 调试 |
| `Shutdown` / `Sleep` / `Hibernate` | 关机 / 睡眠 / 休眠宿主机 | 桌面环境 |
| `Restart` / `Logoff` | 重启 / 注销 | 桌面环境 |
| `RunScript` | 跑自定义脚本 | 高级 |

**官方术语对照**（`/m7a/assets/docs/Termux.md:115` 原文）：

> 默认情况下，启动脚本在任务完成后会自动退出。**如果需要循环运行（例如每天定时执行）**，
> 编辑 `start-m7a.sh`，将 `MARCH7TH_AFTER_FINISH=Exit` 改为 `MARCH7TH_AFTER_FINISH=Loop`。

### 3.4 手动触发一次任务

```bash
# 方式一：容器跑着在等明天，直接重启进程（注意会打断当前等待）
sudo docker restart m7a

# 方式二：跑子任务（不进主流程）
sudo docker exec m7a python main.py daily
sudo docker exec m7a python main.py power
sudo docker exec m7a python main.py currencywars
sudo docker exec m7a python main.py divergent
sudo docker exec m7a python main.py notify
```

> 子任务列表见官方 Docker.md；`daily` 日常、`power` 清体力、
> `currencywars` 拟造花萼、`divergent` 差分宇宙、`notify` 仅推送。

### 3.5 浏览器登录

凭证存在 `3rdparty/WebBrowser/UserProfile/Integrated/Default/`，
包括 cookies、Local Storage、Login Data。

**什么时候要重新登录**

- 云游戏进不去，日志报「账号在另一处重复登录」
- 日志反复「未识别出任何界面」且画面停在登录页
- 清除过 cookies

**怎么登录**

1. 面板 → 「游戏画面预览」 → 看到画面
2. 手动点登录 → 扫码 / 输入验证码
3. 凭证会自动写入 `UserProfile`，长期有效

> 2026-11-10 曾按用户要求清除过一次凭证，
> 备份在 `/home/march7thassistant/cookies-backup-20261110-2145.tar.gz`（153863 B）。

### 3.6 容器内执行命令

```bash
# 容器内没有 curl，用 python
sudo docker exec m7a python3 -c "
import urllib.request, json
print(json.load(urllib.request.urlopen('http://127.0.0.1:9222/json/list')))"

# 不进容器读镜像文件
sudo docker run --rm --entrypoint sh ghcr.nju.edu.cn/moesnow/march7thassistant:latest \
  -c 'cat /m7a/assets/config/version.txt'
```

---

## 第 4 章 配置管理（最大的坑在这里）

### 4.1 ⚠️ 环境变量优先级高于配置文件

上游 `module/config/config.py:123` 的 `_ENV_OVERRIDE_MAP` 注释原文：

> **环境变量优先级高于配置文件**

**真实踩过的后果**：

```
9-27 起用户在面板把 after_finish 设为 Exit
但 compose 里一直是 MARCH7TH_AFTER_FINISH=Loop
→ 容器永远读到 Loop，用户在页面怎么改都没用
```

**排查方法**

```bash
# ① 配置文件写的什么
sudo grep -nE "^after_finish|^log_level|^browser_headless|^loop_mode|^scheduled_time" \
  /home/march7thassistant/config.yaml

# ② 容器实际读到的什么
sudo docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' m7a | grep MARCH7TH
```

**两边不一致时以环境变量为准，且必须改 compose。**

### 4.2 环境变量覆盖表（官方 Docker.md:170-180）

| 环境变量 | 说明 | 官方默认 |
|---|---|---|
| `MARCH7TH_CLOUD_GAME_ENABLE` | 启用云游戏模式 | `true` |
| `MARCH7TH_BROWSER_HEADLESS_ENABLE` | 无头浏览器模式 | `true` |
| `MARCH7TH_BROWSER_HEADLESS_RESTART_ON_NOT_LOGGED_IN` | 未登录时重启浏览器 | `false` |
| `MARCH7TH_BROWSER_DOWNLOAD_USE_MIRROR` | 浏览器下载走镜像 | `false` |
| `MARCH7TH_AFTER_FINISH` | 任务完成后操作 | **`Loop`** |
| `MARCH7TH_LOG_LEVEL` | 日志等级 | - |
| `MARCH7TH_NO_RUN_IMMEDIATELY` | 启动时禁用立即运行 | `false` |

**追加一条本项目的教训**：`MARCH7TH_BROWSER_HEADLESS_ENABLE` 必须与
config.yaml 的 `browser_headless_enable` **保持一致**，否则会引发死循环
（见 [13.2 案例 B](#132-案例-b死循环invalid-session-id)）。

### 4.3 当前生效配置对照

| 配置项 | config.yaml | compose ENV | 实际生效 |
|---|---|---|---|
| `after_finish` | `Exit`（第 34 行） | `MARCH7TH_AFTER_FINISH=Loop` | **Loop** |
| `log_level` | `INFO`（第 8 行） | `MARCH7TH_LOG_LEVEL=INFO` | INFO |
| `browser_headless_enable` | `true`（第 79 行） | `MARCH7TH_BROWSER_HEADLESS_ENABLE=true` | true |
| `loop_mode` | `scheduled`（第 397 行） | — | scheduled |
| `scheduled_time` | `8:00`（第 400 行） | — | 每天 8:00 |

> `after_finish` 两边不一致是**已知且有意为之**：以 Loop 为准实现「每天 8 点自动跑」。
> 若要彻底一致，把 config.yaml:34 也改成 `Loop`。

### 4.4 config.yaml 非原子写入（已知缺陷 + 我们的兜底）

**缺陷来源**：compose 把 config.yaml 当**单文件**挂载：

```yaml
- ./config.yaml:/m7a/config.yaml
```

`os.replace()` 换 inode 时撞上挂载点报 `EBUSY`（**永久性错误，不重试**），
上游降级为**原地覆盖写入**。上游注释原文（`module/config/config.py:419`）：

```
# 1. Docker 单文件挂载的 config.yaml：rename 撞上挂载点报 EBUSY（永久性，未重试）；
# 2. Windows 共享冲突重试用尽（WinError 5/32）或其他替换错误。
# 降级为原地覆盖写入（非原子，写入中途被中断会截断配置文件），
# 因此降级前先把当前配置复制一份留底。
```

**为什么不改成挂目录**

`module/config/__init__.py:8` 的 `CONFIG_PATH = "./config.yaml"` 是**相对路径**，
解析到 `/m7a/config.yaml`。改挂 `./config:/m7a/config` 会让容器内
`/m7a/config.yaml` 消失 → 程序重新生成默认配置 → **356 个字段全丢**。
实测验证过，不可行。

**风险量化**

降级写入用 `shutil.copyfileobj` 拷 45KB，耗时 **0.1~1 毫秒**。
只有这个窗口内被 SIGKILL 才会截断。当前峰值 1.86G / 上限 4g，OOM 概率已很低。

**日志长这样（正常现象，无损）**

```
WARNING | [config] save_config os.replace 失败 EBUSY，降级为原地覆盖写入
```

> **不要把它当故障处理**，它是设计好的降级路径。

### 4.5 配置完整性守护（`config_guard.py`）

**位置**：`/home/march7thassistant/config_guard.py`（5576 B）

**难在哪**：截断到 5000 字节的 config.yaml **仍然语法合法**
（能解析出 39 个字段，`yaml.safe_load` 不报错）。只判「能否解析」会漏掉。

**三重判定**（任一不满足即恢复）

1. 能解析成 dict
2. 顶层字段数 ≥ 历史良好副本的最小值
3. 体积 ≥ 基线 60%

**良好副本**：按 sha256 去重存 `config-backup/config.yaml.<sha16>`，保留 20 份。
备份自身须可解析且 `len(d) >= 50` 才参与基线计算。

**cron**（root crontab）

```
* * * * * /usr/bin/python3 /home/march7thassistant/config_guard.py >/dev/null 2>&1
```

> **cron 的坑**：脚本 `log()` 既写文件又 `print`，
> 如果 cron 再 `>>` 到同一个日志文件，每行会出现两次。
> **必须 `>/dev/null 2>&1`。**

**实测三种损坏场景均恢复成功**

| 场景 | 检出依据 | 结果 |
|---|---|---|
| 截断到 5000B（合法 YAML，39 字段） | 字段数 39 < 基线 356 | ✅ 恢复 45429B / 356 字段 |
| 截断到 100B（非法 YAML） | 无法解析为 dict | ✅ 恢复 |
| 文件被删 | 不存在 | ✅ 恢复 |

**基线**：45429 B / 356 字段 / sha256 前缀 `a63893fcb23874e9`

**查看守护日志**

```bash
sudo tail -20 /home/march7thassistant/logs/config-guard.log
# 正常输出：2026-11-11 22:28:01 OK config.yaml 完整 (45429B, 356 字段, 基线 356 字段/45429B)
```

---

## 第 5 章 内存与性能

### 5.1 本机实测内存曲线

| 阶段 | 内存 | 说明 |
|---|---|---|
| 启动空转 | 310 MB | 等登录 / 等 scheduled_time |
| 进入云游戏 | 1.25 GB | chrome + 页面 |
| 界面识别 + 模板匹配 | **1.86 GB（峰值）** | 最吃内存的阶段 |
| 战斗 | 约 2.7 GB | chrome 渲染 |
| 跑完等明天 | **375 MB** | 浏览器已关 |

> **修复前峰值是 5.4 GB**（无上限）/ 3.7 GB（3g 上限）。
> 打了补丁后降到 1.86 GB，见 5.2。

### 5.2 内存暴涨的两个根因（已修复）

#### 根因 1：57 线程并行，不等待

`/m7a/module/screen/screen.py:139 get_current_screen()` 原生代码：

```python
executor = ThreadPoolExecutor(max_workers=len(self.screen_map))   # ← 57 个线程
futures = [executor.submit(find_screen, name, screen) for name, screen in self.screen_map.items()]
for future in as_completed(futures):
    if future.result() is not None:
        executor.shutdown(wait=False, cancel_futures=True)   # ← wait=False！
        break
else:
    executor.shutdown(wait=False)                            # ← wait=False！
```

`shutdown(wait=False)` 不等待 → 函数返回时线程还在跑 → 下一轮再开一批 → **跨轮堆积**。

**内核日志直接点名**：

```
ThreadPoolExecu invoked oom-killer: gfp_mask=0x440dc0(GFP_KERNEL_ACCOUNT|__GFP_ZERO|__GFP_COMP), order=0, oom_score_adj=0
```

**我们的补丁**（附录 B.2）：限制 8 线程 + `wait=True`。

#### 根因 2：图片无缓存

`/m7a/utils/image_utils.py:10 ImageUtils.read_image()` 每次调用都
`np.fromfile()` + `cv2.imdecode()`，**全文件没有任何缓存**。

界面识别每秒调用约 **490 次**（49 个模板 × 10 次重试 × 每秒 1 帧）。

**我们的补丁**（附录 B.1）：类级 LRU 缓存 512 条。

### 5.3 「为什么以前没问题」

这是最容易被误判的一条，完整取证书：

| 疑点 | 核实结果 |
|---|---|
| 是升级引入的吗？ | **不是**。v2026.11.7 与 v2026.11.8 的 `screen.py` md5 完全相同 `a7d423abf13630f5087a117dcb89453f` |
| 多线程逻辑何时引入？ | `bfc0899a 2026-01-13` 加的 `mem.available > 2G` 判断；`312dc15b 2026-04-09` 把 `Thread` 换成 `ThreadPoolExecutor` |
| 11-10 之前跑得起来吗？ | 跑得起来。`2026-11-10.log.gz` 里 `grep -a "任务完成"` 有 **48 条** |
| 那为什么 11-10 崩了？ | **因为 11-10 18:45 加了 `mem_limit: 3g`**。无上限时 python 涨到 5.4G 靠宿主余量勉强跑完；加了上限就变成「一超 cgroup 天花板必死」 |

> **结论**：同一份有缺陷代码，无上限时靠宿主余量勉强跑，加上限后必然暴露。
> 加内存限制没有错，错在**没先修根因**。

### 5.4 内存监控

```bash
# 实时
sudo docker stats --no-stream m7a

# 持续采样（每 5 秒，写文件，别刷屏）
for i in $(seq 1 60); do
  sudo docker stats --no-stream --format '{{.MemUsage}} {{.CPUPerc}}' m7a
  sleep 5
done > /tmp/mem-monitor.log 2>&1
```

**健康标准**

| 指标 | 正常 | 警告 | 危险 |
|---|---|---|---|
| 峰值内存 | < 2.5G | 2.5–3.5G | > 3.5G |
| 内存形态 | 涨后会回落 | 持平不降 | 只涨不跌 |
| swap 使用 | < 2G | 2–3.5G | > 3.5G |

---

## 第 6 章 云游戏与浏览器

### 6.1 云游戏流程

```
启动 chrome（CDP 9222）
  → 打开 https://sr.mihoyo.com/cloud
  → 等待进入云游戏（最长 6 分钟）
  → 初始化 OCR
  → 逐界面识别与操作
  → 任务完成后关闭浏览器
```

### 6.2 ⚠️ `invalid session id` 死循环（2026-11-11 排查出的）

**现象**

```
截图失败：Message: invalid session id; For documentation on this error, please visit:
https://www.selenium.dev/documentation/webdriver/troubleshooting/errors#invalidsessionidexception
```
每秒 1 条，持续 60 秒（共 60 条），然后「截图超时」→ 进程退出 → 容器重启 → 再循环。

**⚠️ 为什么 grep 不到**

日志带 ANSI 色码，直接 `grep "invalid session id"` 会 **0 命中**。
必须先 `sed 's/\x1b\[[0-9;]*m//g'`。

**根因链**（`module/game/cloud.py:405-435` `_connect_or_create_browser`）：

```
① 上一轮 stop_game() 里 driver.quit() 有 15 秒超时
   → 超时只 log_debug 就继续
② chrome 半死（进程还在，session 已废）
③ 下一轮 _connect_or_create_browser()：
     if not connect_only:
         self.close_all_m7a_browser(headless=not headless)
         existing = self.get_m7a_browsers(headless=headless)
     if existing:                                   # ← 发现残留 chrome
         options.debugger_address = f"127.0.0.1:{reconnect_port}"
         self.driver = webdriver_type(...)          # ← 重连坏 session
         return                                     # ← 不启动新的
④ 截图 60 次全部 invalid session id
⑤ RuntimeError("截图超时") → except Exception → raise
⑥ 进程退出 → 容器 restart → 死循环
```

**放大器**：容器 ENV `MARCH7TH_BROWSER_HEADLESS_ENABLE=true`，
而 config.yaml 里是 `false` →

```python
close_all_m7a_browser(headless=not headless)
# headless 来自 ENV = true → not headless = false
# → 只关「非 headless」的那批
# → 上一轮的 headless chrome 存活，被当成「现有浏览器」重连
```

**修复**

```bash
# ① 配置对齐（关键）
sudo sed -i '79s/browser_headless_enable: false/browser_headless_enable: true/' \
  /home/march7thassistant/config.yaml

# ② 冷启动（清掉残留 chrome）
sudo docker compose -f /home/march7thassistant/docker-compose.yml down
ss -tlnp | grep 922          # ← 必须为空
sudo docker compose -f /home/march7thassistant/docker-compose.yml up -d
```

**验证**

```bash
sudo docker logs --tail 200 m7a 2>&1 | sed 's/\x1b\[[0-9;]*m//g' | grep -c "invalid session id"
# 期望：0
```

### 6.3 界面识别失败

**现象**

```
ERROR | 未识别出任何界面，请确保游戏画面干净，按ESC后重试
```

每 19.5 秒一条，持续约 28 分钟，然后 `当前界面：未知` → `发生错误 截图超时` → 重开下一轮。
2026-11-10 全天 **1850 条**，任务成功数 **0**。

**可能原因**（按概率排序）

| 原因 | 怎么确认 | 处理 |
|---|---|---|
| 画面停在登录页 | 面板预览看画面 | 手动登录 |
| 画面停在加载页 | 面板预览看画面 | 等，或提高云游戏带宽 |
| 分辨率不对 | `docker exec m7a python3 -c "..."` 抓图看尺寸 | 应为 1920×1080 |
| 全屏模式不对 | 看 config.yaml `cloud_game_fullscreen_enable` | 应为 `true` |
| 云游戏连接 bug | 看镜像版本 | 升级到 v2026.11.8+（含 #1315 修复） |

**抓当前画面看看到底停在哪**

```bash
sudo docker exec m7a python3 -c "
from websocket import create_connection
import urllib.request, json, base64
targets = json.load(urllib.request.urlopen('http://127.0.0.1:9222/json/list'))
page = [t for t in targets if t.get('type')=='page'][0]
ws = create_connection(page['webSocketDebuggerUrl'], timeout=15, suppress_origin=True)
ws.send(json.dumps({'id':1,'method':'Page.captureScreenshot','params':{'format':'png'}}))
r = json.loads(ws.recv())
open('/m7a/logs/_debug_screen.png','wb').write(base64.b64decode(r['result']['data']))
print('written /m7a/logs/_debug_screen.png')"
# 宿主机上就是 /home/march7thassistant/logs/_debug_screen.png
```

> **坑**：容器内 `/tmp` 与宿主 `/tmp` 不共享，必须写到 `/m7a/logs/`。
> **坑**：CDP WebSocket 直连会被 403 拒绝，必须 `suppress_origin=True`。

### 6.4 端口冲突

**现象**

```
WARNING | 端口 9222 无法绑定（[Errno 98] Address already in use），正在查找可用端口...
INFO | 将使用端口 9223 启动浏览器
```

**原因**：上一轮 chrome 没清干净。

**处理**

```bash
sudo docker compose -f /home/march7thassistant/docker-compose.yml down
ss -tlnp | grep 922          # 必须为空；有残留就 kill
sudo docker compose -f /home/march7thassistant/docker-compose.yml up -d
```

### 6.5 云游戏时长

**每轮日志都有**

```
INFO | 云游戏剩余时长：615 分钟（付费：45 分钟，免费：570 分钟）
```

**2026-11-10 的教训**：当天烧掉 **803 分钟**（08:04 的 809 → 21:39 的 6），
全部浪费在界面识别失败的重试上，**0 个任务成功**。

**监控建议**

```bash
# 提取历史云游戏时长变化
sudo grep -h "云游戏剩余时长" /home/march7thassistant/logs/*.log | tail -20
```

> 时长烧光的后果：日志报「云游戏时长不足」，任务无法执行。
> 免费时长每月重置，付费时长需购买。

---

## 第 7 章 监控与告警

### 7.1 面板健康看板

面板 → 概览页顶部 `.ahbar` 健康条，六档：

| level | 含义 | 用户该做什么 |
|---|---|---|
| `off` | 告警未启用 | 去配置页启用 |
| `setup` | 已启用但没填接收地址 | **填地址** |
| `crit` | 连续失败达阈值，或从未成功过 | 检查推送配置 |
| `warn` | 最近一次失败 / 还在重试队列 | 观察 |
| `ok` | 最近一次成功 | 无 |

### 7.2 容器健康告警（V1.28.2 新增）

**容器 `healthcheck`** 60 秒一次，连续 3 次失败 → `unhealthy` →
面板推 **critical「容器内部异常」**（kind = `unhealthy`）。

**与 down 告警的区别**

| 告警 | 触发条件 | 语义 |
|---|---|---|
| `down` | `Running=false` | 进程没了 |
| `unhealthy` | `Running=true` 但探针连续失败 | 进程还在，但卡住/坏了 |

两者独立记账（`alerted_down` / `alerted_unhealthy`），互不干扰。
`run=False` 时不评估 unhealthy（归 down 管），避免同一故障推两条。

**边界处理**：容器正常退出后 Docker 会把最后一次探测结论
（通常 `unhealthy`）留在 `State.Health`。`container_health()` 在
`Running=False` 时把 health 归一为 `''`，避免把「容器根本没在跑」
显示成「容器活着但病了」。

### 7.3 ⚠️ 告警推送通道当前是坏的（最大盲区）

```bash
sudo cat /www/wwwroot/m7a-panel/data/alert_state.json
```

```json
{
  "push_fail_streak": 32,
  "push_last_fail_msg": "未配置接收地址",
  "push_last_ok_at": 0
}
```

**从未推送成功过**。需要在面板配置页填接收地址（Bark / 企业微信等）。

> 面板健康条会显示 `level=setup`、`label=未配置接收地址`，
> 但用户不看页面就永远不知道。**这是目前最大的监控盲区。**

**配置步骤**

1. 面板 → 告警配置
2. 选通道类型（Bark / 企业微信 / Webhook）
3. 填地址
4. 点「测试推送」验证
5. 回概览页确认健康条变 `ok`

### 7.4 建议补充的监控

| 项 | 阈值建议 | 怎么看 |
|---|---|---|
| 云游戏剩余时长 | < 60 分钟告警 | 日志 grep `云游戏剩余时长` |
| 磁盘使用率 | > 85% 告警 | `df -h /` |
| swap 使用率 | > 90% 告警 | `free -m` |
| 容器重启次数 | > 3 次/小时告警 | `docker inspect .RestartCount` |
| `invalid session id` 出现 | 出现即告警 | 日志 grep（去色后） |
| 界面识别失败 | > 50 条/小时告警 | 日志 grep |

---

## 第 8 章 面板（m7a_panel）

### 8.1 服务管理

```bash
# 状态（注意 systemd unit 名）
sudo systemctl status m7a-panel
systemctl is-active m7a-panel

# 重启
sudo systemctl restart m7a-panel

# 消除「unit file changed on disk」警告
sudo systemctl daemon-reload

# 验证端口
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:9999/
```

**⚠️ venv 路径的坑**

```
正确: /www/wwwroot/m7a-panel/panel_migr/.venv/bin/python
错误: /www/wwwroot/m7a-panel/.venv/bin/python
```

`systemd` 的 `ExecStart` 用的是 `panel_migr/.venv`，venv 在 `panel_migr` 子目录下。

### 8.2 面板访问不了怎么办

按顺序查：

```bash
# ① 服务活着吗
systemctl is-active m7a-panel

# ② 端口在听吗
ss -tlnp | grep 9999

# ③ 本机能 curl 通吗
curl -I http://127.0.0.1:9999/

# ④ 看 journal
sudo journalctl -u m7a-panel --since "10 min ago" --no-pager | tail -50
```

**常见原因**

| 现象 | 原因 | 处理 |
|---|---|---|
| `active` 但浏览器打不开 | 监听 `127.0.0.1`，只能本机访问 | 配 Nginx 反代，或改 `M7A_LISTEN_HOST=0.0.0.0` |
| 页面 401 | 未登录 | 正常，登录即可 |
| `ajax=status` 返回 `{"auth": false}` | **正常设计**，未登录就是这样 | 不用管 |
| WebSocket 连不上 | Nginx 没配 upgrade 头 | 见 8.4 |

### 8.3 面板读不到日志 / 数据不刷新

**原因**：日志文件属主是 root（容器以 root 写），面板也是 root，**不影响**。
若面板改成非 root 运行，需要：

```bash
sudo chown -R root:www /home/march7thassistant/logs
sudo chmod -R g+r /home/march7thassistant/logs
```

**权限问题的排查陷阱**

```bash
# 如果你以普通用户跑面板代码验证，会报 PermissionError [Errno 13]
# 这不是权限配置错，是你没 sudo
sudo -S bash -c 'cd /www/wwwroot/m7a-panel && M7A_PANEL_DIR=/www/wwwroot/m7a-panel \
  ./panel_migr/.venv/bin/python your_script.py'
```

### 8.4 Nginx 反代配置（WebSocket 必须）

```nginx
location / {
    proxy_pass http://127.0.0.1:9999;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_read_timeout 3600s;    # WebSocket 长连接
}
```

**缺了 `Upgrade` / `Connection` 两个头 → WebSocket 升级失败 → 页面数据不刷新。**

### 8.5 在线更新

```bash
# 备份当前面板
sudo tar czf /www/wwwroot/m7a-panel/backups/pre-update-$(date +%Y%m%d-%H%M%S).tar.gz \
  -C /www/wwwroot/m7a-panel panel_migr

# 面板里点「立即更新」，或手动替换文件后
sudo systemctl restart m7a-panel
```

**已知坑**：CI/非 root 环境里 `systemctl restart` 会报
`Interactive authentication required.`，V1.28.1 起会降级为
`skipped=True` 并提示「请以 root 执行 systemctl restart m7a-panel 后生效」，
不再整单回滚。

### 8.6 会话与登录

| 项 | 默认值 | 说明 |
|---|---|---|
| 会话目录 | `BASE_DIR/sessions` | 可 `M7A_SESSION_DIR` 覆盖 |
| 文件权限 | `0660` | 可 `M7A_SESSION_MODE` 调整 |
| TTL | 3600 秒（自**最后一次读取**） | V1.28.1 修复了「活跃会话被误删」 |

> ⚠️ **安全**：不要把 `M7A_SESSION_DIR` 指向 `/tmp` 等全局可写目录。
> session 文件里就是登录态，任何本机账号都能伪造管理员登录。
> 确需共享时用「专用目录 + 同组 + `0660`」。

---

## 第 9 章 游戏画面预览

### 9.1 工作原理

```
面板浏览器 ──WebSocket──► 面板服务 ──HTTP/WS──► 容器内 preview_server:9223
                                                      │
                                                      ▼ CDP
                                              chrome:9222
                                                      │
                                                      ▼
                                              Page.captureScreenshot
```

**关键组件**

| 组件 | 位置 | 说明 |
|---|---|---|
| `preview_server.py` | `/m7a/logs/` | **不在发布包里**，靠挂载提供 |
| `preview_secret` | `/m7a/logs/` | 令牌种子，面板与容器共享 |
| CDP 端口 | 容器内 9222 | chrome 调试端口 |
| 上游端口 | 容器内 9223 | preview_server 监听 |

### 9.2 ⚠️ preview_server.py 是"非交付物"

**这是最容易丢的东西。** 2026-11-11 整目录重装时丢过一次。

- 面板 `main.py:160` 注释原文：「容器内 preview_server（不在交付包内、无法审计）」
- 面板 `backups/` 全部 17 个发布包（v1.19–1.28）**都没有** `preview_server.py`
- 三个镜像里 `/m7a/logs` 目录**根本不存在**，完全靠挂载

**结论**：它从来不在发布包里，丢了只能重建。

### 9.3 面板侧契约（重建时必须遵守）

| 项 | 值 | 源码位置 |
|---|---|---|
| 脚本路径 | `/m7a/logs/preview_server.py` | `config.py:109` |
| 令牌文件 | `{小助手目录}/logs/preview_secret` | `config.py:106` |
| 上游端口 | 9223 | `config.py:108` |
| 令牌算法 | `hmac_sha256(secret, YYYYMMDD).hexdigest()[:32]` | `services/preview.py:51` |
| 拉起方式 | `docker exec -d <container> python3 <script>` | `services/preview.py` |
| 拉起节流 | 60 秒 | `_ENSURE_GAP` |
| 失败文案 | `preview_server 不可达（容器内服务未就绪，已尝试拉起）` | — |

**上游接口**

| 接口 | 用途 |
|---|---|
| `GET /health` | 健康检查 |
| `GET /stream` | MJPEG 流 |
| `WS /ws?token=&res=&quality=` | JPEG 帧流（二进制裸 JPEG） |
| `WS /ws264?token=&res=&fps=` | H.264 帧流 |

### 9.4 重建 preview_server（如果丢了）

容器内可用的库：

| 库 | 状态 |
|---|---|
| aiohttp | ✅ 3.14.4 |
| Pillow | ✅ 13.0.0 |
| numpy / cv2 | ✅ 2.4.6 / 4.13.0 |
| **websockets** | ❌ **没有** |
| websocket-client | ✅ 1.9.0 |

> **关键约束**：**容器内没有 `websockets` 库**，必须用 `aiohttp` 做 WebSocket 服务端。

参考实现要点：

1. 用 `aiohttp.web` 起服务，端口 `PREVIEW_PORT`（默认 9223）
2. `CDPFrameSource.discover()` 探 `9222,9223` 的 `/json/list`，取 `type=page`（优先 mihoyo.com）
3. 走 `Page.captureScreenshot` 抓 JPEG
4. Pillow 缩放到 `{"720p":1280, "480p":854, "360p":640}`
5. `MAX_JPEG_BYTES = 260*1024` 兜底降质
6. `MIN_FRAME_INTERVAL = 0.06`
7. `check_token()` 兼容昨天令牌（跨零点不断流）
8. `ensure_secret()` 缺失时自动生成

### 9.5 预览故障排查

| 现象 | 检查 | 处理 |
|---|---|---|
| 一直转圈 | 容器在跑吗 | `docker ps` |
| 提示令牌无效 | 系统时间一致吗 | 令牌按日轮换，时钟偏差会失败 |
| `preview_server 不可达` | 进程在吗 | `docker exec m7a ps aux \| grep preview` |
| 面板自动拉起失败 | 手动起 | `docker exec -d m7a python3 /m7a/logs/preview_server.py` |
| H.264 档不可用 | 宿主有 ffmpeg 吗 | `ffmpeg -encoders \| grep libx264` |
| 画面卡顿 | 带宽/编码档位 | 降到 480p / 换 JPEG 档 |

**手动验证**

```bash
# ① 容器内服务活着吗
sudo docker exec m7a python3 -c "
import urllib.request, json
print(json.load(urllib.request.urlopen('http://127.0.0.1:9223/health')))"

# ② 面板能拿到上游地址吗
sudo -S bash -c 'cd /www/wwwroot/m7a-panel && M7A_PANEL_DIR=/www/wwwroot/m7a-panel \
  ./panel_migr/.venv/bin/python -c "
import sys; sys.path.insert(0, \"./panel_migr\")
from services import preview as pv
print(pv.get_upstream(\"m7a\"))"'

# ③ 令牌对不对
sudo -S bash -c 'cd /www/wwwroot/m7a-panel && M7A_PANEL_DIR=/www/wwwroot/m7a-panel \
  ./panel_migr/.venv/bin/python -c "
import sys; sys.path.insert(0, \"./panel_migr\")
from services import preview as pv
print(pv.preview_token())"'
```

> `/m7a-preview/health` 返回 401 是**预期的**（反代前要求登录）。

---

## 第 10 章 日志体系

### 10.1 日志位置

| 类型 | 路径 | 说明 |
|---|---|---|
| 总日志 | `logs/YYYY-MM-DD.log` | 当天所有轮次 |
| 单轮日志 | `logs/runs/<yyyyMMdd-HHmmss-xxxx>.log` | 一次运行一个文件 |
| 配置守护 | `logs/config-guard.log` | 每分钟一条 |
| 容器 stdout | `docker logs m7a` | 与总日志内容一致 |
| 面板日志 | `journalctl -u m7a-panel` | 面板自身 |

### 10.2 ⚠️ 日志带 ANSI 色码（必须去色）

**这是最容易白费半小时的坑。**

```bash
# ❌ 错误：0 命中
sudo grep "invalid session id" /home/march7thassistant/logs/2026-11-11.log

# ✅ 正确：先去掉色码
sudo docker logs --tail 200 m7a 2>&1 | sed 's/\x1b\[[0-9;]*m//g' | grep "invalid session id"

# ✅ 或者用 grep -a（处理二进制）
sudo grep -a "invalid session id" /home/march7thassistant/logs/2026-11-11.log
```

### 10.3 日志轮转

```yaml
logging:
  driver: json-file
  options:
    max-size: "20m"
    max-file: "5"
```

**每个容器最多占 100MB 磁盘**（20m × 5）。

> **不配会怎样**：json-file 默认**无上限**。
> 2026-11-10 单日日志 15.8MB / 134,834 行（LOG_LEVEL=DEBUG 时）。
> 109G 磁盘装不下几个月的 DEBUG 日志。

**验证**

```bash
sudo docker inspect -f '{{.HostConfig.LogConfig.Type}} {{index .HostConfig.LogConfig.Config "max-size"}} x {{index .HostConfig.LogConfig.Config "max-file"}}' m7a
# 期望：json-file 20m x 5
```

### 10.4 日志等级

| 等级 | 单日体积（本机实测） | 何时用 |
|---|---|---|
| `INFO` | 约 1–2 MB | **日常（当前）** |
| `DEBUG` | **15.8 MB** | 排查时临时开 |

> 开 DEBUG 一定要改回 INFO，否则一天 15MB，且 grep 时噪音极大。

### 10.5 常用日志分析命令

```bash
# 今天有多少轮（每次「正在启动 chrome 浏览器」算一轮）
sudo grep -ac "正在启动 chrome 浏览器" /home/march7thassistant/logs/$(date +%Y-%m-%d).log

# 有多少次成功
sudo grep -ac "任务完成" /home/march7thassistant/logs/$(date +%Y-%m-%d).log

# 错误统计
sudo grep -a "ERROR" /home/march7thassistant/logs/$(date +%Y-%m-%d).log | \
  sed -E 's/.*ERROR \| //' | sort | uniq -c | sort -rn | head -20

# 云游戏时长变化
sudo grep -a "云游戏剩余时长" /home/march7thassistant/logs/$(date +%Y-%m-%d).log | tail -20

# 逐小时错误分布
sudo grep -a "ERROR" /home/march7thassistant/logs/$(date +%Y-%m-%d).log | \
  sed -E 's/^([0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}):.*/\1时/' | sort | uniq -c
```

> **⚠️ `grep "完成"` 会匹配 release note 里的超长文本刷屏**，
> 应该用更精确的 `grep "任务完成"` 或 `grep "INFO.*完成"`。

---

## 第 11 章 备份与回滚

### 11.1 五类回滚点

| 类型 | 位置 | 恢复方式 |
|---|---|---|
| compose | `docker-compose.yml.bak.*` | `cp` 回去 + recreate |
| 面板 | `/www/wwwroot/m7a-panel/backups/*.tar.gz` | `tar xzf` + restart |
| 整机 | `/home/huangsongping/m7a-redeploy-backup-*` | 手工恢复 |
| 凭证 | `cookies-backup-*.tar.gz` | `tar xzf` |
| 镜像 | docker images（ID 级） | 改 compose 的 image |

### 11.2 compose 回滚

```bash
# 列出所有备份
sudo ls -1t /home/march7thassistant/docker-compose.yml.bak.*

# 当前已有的（生产环境）
# docker-compose.yml.bak.loop-20261111-221423       ← AFTER_FINISH 改 Loop 前
# docker-compose.yml.bak.optimize-20261111-201814   ← 日志/健康检查前
# docker-compose.yml.bak.mem4g-20261111-184956      ← mem 改 4g 前
# docker-compose.yml.bak.patch-20261111-181541      ← 加补丁前
# docker-compose.yml.bak.screen-20261111-183436
# docker-compose.yml.bak.20261031-100200            ← 最原始

# 回滚
sudo cp /home/march7thassistant/docker-compose.yml.bak.<时间戳> \
        /home/march7thassistant/docker-compose.yml
sudo docker compose -f /home/march7thassistant/docker-compose.yml up -d --force-recreate
```

### 11.3 面板回滚

```bash
sudo ls -lt /www/wwwroot/m7a-panel/backups/
# pre-v1.28.2-20261111-205507.tar.gz   ← 当前回滚点（19.5 MB）
# pre-v1.28.0-20261107-114713.tar.gz

sudo tar xzf /www/wwwroot/m7a-panel/backups/pre-v1.28.2-<时间戳>.tar.gz -C /www/wwwroot/m7a-panel
sudo systemctl restart m7a-panel
```

### 11.4 整机重装备份

`/home/huangsongping/m7a-redeploy-backup-20261111-121913/`

| 文件 | 大小 | 说明 |
|---|---|---|
| `config.yaml` | 45716 B | sha256 `5f4ae428...` |
| `docker-compose.yml` | 2741 B | sha256 `70728087...` |
| `UserProfile.tar.gz` | 54,333,802 B | 浏览器凭证（535 条目） |
| `config-history.tar.gz` | 126166 B | 9 份历史配置 |

### 11.5 镜像回退

```bash
# ① 停
sudo docker compose -f /home/march7thassistant/docker-compose.yml down

# ② 改 compose 的 image 为旧版本（用 ID 或 tag）
#    image: ghcr.nju.edu.cn/moesnow/march7thassistant@sha256:<旧ID>

# ③ 起
sudo docker compose -f /home/march7thassistant/docker-compose.yml up -d --force-recreate
```

### 11.6 配置回滚（从良好副本恢复）

```bash
# 守护脚本自动存的好副本
sudo ls -lt /home/march7thassistant/config-backup/

# 手动恢复某一份
sudo cp /home/march7thassistant/config-backup/config.yaml.<sha16> \
        /home/march7thassistant/config.yaml
sudo docker compose -f /home/march7thassistant/docker-compose.yml restart m7a
```

---

## 第 12 章 故障速查表（按现象查）

### 12.1 容器类

| 现象 | 先查什么 | 常见原因 | 处理 |
|---|---|---|---|
| 容器不停重启 | `docker inspect` 看 `ExitCode` | `137` = OOM 或 SIGKILL | `journalctl -k \| grep -i "out of memory"` |
| 容器跑完就停 | `docker logs` 最后一行 | `AFTER_FINISH=Exit` | 改 compose 为 `Loop` |
| 容器不自动跑 | 日志有没有「将在明天X时Y分继续运行」 | 正常等待中 | 等，或改 `scheduled_time` |
| 容器启动就死 | `docker logs` 前 50 行 | 配置损坏 / 镜像缺文件 | 见 4.4 配置守护 |
| 容器 health=unhealthy | `docker logs` | python 卡住 / 反复报错 | 看日志定位，必要时重启 |
| `OOMKilled=true` | `journalctl -k` | 内存暴涨 | 见第 5 章 |
| 容器内命令找不到 | — | 镜像是精简版 | 用 `python3` 不用 `curl` |

### 12.2 任务执行类

| 现象 | 先查什么 | 常见原因 | 处理 |
|---|---|---|---|
| 界面识别一直失败 | 面板预览看画面 | 停在登录/加载页 | 手动登录 |
| `invalid session id` 刷屏 | **先去掉 ANSI 色码再 grep** | 上一轮 chrome 半死 | 见 6.2 |
| 截图超时 | 同上 | 同 invalid session id | 同上 |
| 端口 9222 被占用 | `ss -tlnp \| grep 922` | 残留 chrome | 冷启动 |
| 云游戏时长不足 | 日志 grep `云游戏剩余时长` | 时长烧光 | 等重置 / 买时长 |
| 账号重复登录 | 日志 grep `另一处重复登录` | 别处登录了同一账号 | 退出其它设备 |
| 从手机菜单进入商店失败 | 是否每轮都有 | 偶发 OCR 未命中 | **通常不修**（重试 8 次会放弃，不影响核心任务） |
| 每日实训奖励完成但没刷体力 | `开拓力: X/300` | 体力 < 40 | 正常，等回满 |

### 12.3 配置类

| 现象 | 先查什么 | 常见原因 | 处理 |
|---|---|---|---|
| 面板保存后不生效 | `docker inspect` 的 Env | ENV 覆盖了 config.yaml | 改 compose + recreate |
| config.yaml 变成几十行 | `config-guard.log` | 非原子写入被截断 | 守护会自动恢复 |
| `save_config os.replace 失败 EBUSY` | — | 单文件挂载的正常降级 | **无损，不用管** |
| 改完 compose 没变化 | `docker inspect` 的 HostConfig | 没 recreate | `up -d --force-recreate` |
| TZ 不对 | `docker exec m7a date` | 没设 `TZ` | 加 `TZ=Asia/Shanghai` |

### 12.4 面板类

| 现象 | 先查什么 | 常见原因 | 处理 |
|---|---|---|---|
| 面板打不开 | `systemctl is-active` | 服务挂了 | `systemctl restart` |
| `active` 但打不开 | `ss -tlnp \| grep 9999` | 监听 127.0.0.1 | 配反代或改 HOST |
| 登录后被弹回 | 会话目录权限 | 不同用户运行 | 见 8.6 |
| 数据不刷新 | 浏览器 console | WebSocket 升级失败 | Nginx 加 upgrade 头 |
| `ajax=status` 返回 `{"auth": false}` | — | **正常设计** | 不用管 |
| 监控图表无数据 | `data/monitor_*.json` | 文件损坏 | 会自动备份重建 |
| 在线更新失败 | 回滚点 | 无 systemd 权限 | root 手动 restart |
| 面板看不到容器健康 | `docker inspect` 有 `State.Health` 吗 | 没配 healthcheck | 加配置 |

### 12.5 预览类

| 现象 | 先查什么 | 常见原因 | 处理 |
|---|---|---|---|
| 一直转圈 | `docker ps` | 容器没跑 | 启动容器 |
| `preview_server 不可达` | `docker exec m7a ps \| grep preview` | 进程没起 | 手动 `docker exec -d` |
| 令牌无效 | `date` | 时钟偏差 | 校时 |
| 预览 401 | — | 反代前要求登录 | **正常** |
| H.264 不可用 | 宿主 `ffmpeg -encoders` | 没装 libx264 | 装 ffmpeg 或用 JPEG 档 |

### 12.6 系统类

| 现象 | 先查什么 | 常见原因 | 处理 |
|---|---|---|---|
| 磁盘满 | `df -h /` | 日志没轮转 | 加 logging 配置 |
| swap 用满 | `free -m` | 内存紧张 | 加内存 / 减并发 |
| 宿主机变慢 | `top` | python RSS 高 | 见第 5 章 |
| 重启后容器没起 | `systemctl is-enabled docker` | docker 服务没自启 | `systemctl enable docker` |

---

## 第 13 章 故障案例库

### 13.1 案例 A：内存暴涨导致 OOM 被杀

**时间**：2026-11-10 17:35 – 18:28

**现象**

```
16:54:16  1.29G → 16:54:32 1.19G → 16:54:49 2.03G → OOM
16:54:58  Memory cgroup out of memory: Killed process 1802123 (python)
          total-vm:9061056kB, anon-rss:3742500kB
```

当天共 26 次内核 OOM（21 次 python + 5 次 chrome）。

**根因**：见 5.2（57 线程并行 + 图片无缓存 + 无内存上限）

**修复**：

1. 补丁 1：图片缓存
2. 补丁 2：线程池 8 + `wait=True`
3. `mem_limit: 4g`

**验证**：

```
修复前峰值 3.7G / 5.4G → 修复后 1.86G
内存形态：只涨不跌 → 涨 0.4G 后回落
```

**教训**：加内存限制之前必须先修内存泄漏。否则「止血」会变成「窒息」。

### 13.2 案例 B：死循环（invalid session id）

**时间**：2026-11-11 13:40 – 18:41

**现象**

```
13:40  RestartCount=13
13:51  RestartCount=20 → 21
每轮 60–70 秒，日志只有「开始运行→启动chrome→进云游戏成功→完成」
容器 ID 未变（不是 restart，是同进程内又跑一轮）
```

**决定性证据**（必须先去 ANSI 色码才能看到）

```bash
sudo docker logs m7a 2>&1 | sed 's/\x1b\[[0-9;]*m//g' | grep -c "invalid session id"
# 60（每秒 1 条 × 60 秒）
```

**根因链**：见 6.2

**修复**：

1. `browser_headless_enable: false → true`（配置对齐）
2. 冷启动清残留 chrome
3. `restart: unless-stopped → on-failure:3`（不无限重启）

**验证**：18:41 之后 `invalid session id` / `截图超时` = **0 条**

**教训**：
- 配置两边不一致会引发隐蔽故障
- `restart: unless-stopped` 会掩盖故障（无限重启让问题看起来像正常）
- **日志带 ANSI 色码会让 grep 静默失败**

### 13.3 案例 C：Loop 被误认为"病"

**时间**：2026-11-11 全天

**误判过程**

1. 我最初把 `MARCH7TH_AFTER_FINISH=Loop` 当成死循环根源
2. 建议改成 `Exit`（跑一次就退）
3. 用户质疑：「原版镜像不是设置好时间后就明天自动上线打吗？」

**取证后的真相**

- 官方 `Docker.md:178` 里 `AFTER_FINISH` 默认就是 **`Loop`**
- `Termux.md:115` 明写「**如果需要每天定时执行**，就设 Loop」
- `after_finish_is_loop()` 在等待前先 `stop_game()`，**不占云游戏时长**
- **Loop 本身没病**，是底层三大故障叠加让「等明天」变成「崩了立刻重启再崩」

**修复**：改回 `Loop`，保留所有防护（`on-failure:3` / `mem_limit` / 补丁）

**验证**

```
22:18:39 关闭浏览器成功
22:18:39 将在明天8时0分继续运行
容器 running / healthy / 375MB / CPU 0.11%
```

**教训**：**官方默认值往往是对的**。遇到故障先查「底层为什么坏」，
而不是改掉上层策略。改策略只是让故障表现换个样子。

### 13.4 案例 D：配置被截断

**时间**：2026-11-11 19:07

**现象**：`config.yaml` 可能变成 39 个字段（约 5KB），程序起不来或行为异常。

**根因**：单文件挂载 + 非原子写入（见 4.4）

**修复**：`config_guard.py` + 每分钟 cron

**验证**：三种损坏场景（5000B / 100B / 删除）全部自动恢复

### 13.5 案例 E：误删 preview_server.py

**时间**：2026-11-11 12:19

**现象**：面板预览报 `preview_server 不可达（容器内服务未就绪，已尝试拉起）`

**根因**：整目录重装时 `rm -rf /home/march7thassistant`，
连 `logs/` 里的 `preview_server.py` 和 `preview_secret` 一起删了。

**取证**：翻遍 17 个发布包、3 个镜像、全盘 find，**都找不到**（从来不在发布包里）

**修复**：重建（见 9.4）

**教训**：`logs/` 里不只有日志。重装前必须 `ls -la logs/` 看一眼。

---

## 第 14 章 已知缺陷清单

### 14.1 上游缺陷（我们打了补丁或绕开）

| 缺陷 | 状态 | 我们的处理 |
|---|---|---|
| 界面识别 57 线程不限流 | ✅ 已补丁 | 补丁 2（附录 B.2） |
| 图片无缓存 | ✅ 已补丁 | 补丁 1（附录 B.1） |
| `_connect_or_create_browser` 重连半死 chrome | ⚠️ 部分缓解 | v2026.11.8 修了连接逻辑，但 `driver.quit()` 超时仍只 log |
| config.yaml 单文件挂载非原子写入 | ⚠️ 上游知情不修 | `config_guard.py` 兜底 |
| `close_all_m7a_browser(headless=not headless)` 依赖配置一致 | ⚠️ 需人工保证 | 保持 config.yaml 与 ENV 的 headless 一致 |
| `preview_server.py` 不在发布包 | ⚠️ 设计如此 | 靠挂载提供，需自行备份 |

### 14.2 面板缺陷（已修 / 未修）

| 缺陷 | 状态 |
|---|---|
| 活跃会话被 TTL 误删（V28-01） | ✅ V1.28.1 |
| python-multipart 6 个 PYSEC（V28-03） | ✅ V1.28.1（→ 0.0.33） |
| 日志聚合条点进去条数对不上（V28-04/09） | ✅ V1.28.1 |
| 告警健康看板原因过期（V28-05） | ✅ V1.28.1 |
| 自更新在无 root 环境整单回滚（V28-07） | ✅ V1.28.1 |
| `sock_json` 不解 chunked | ✅ V1.28.2 |
| 容器健康状态不接入告警 | ✅ V1.28.2 |
| 版本号三处不一致 | ✅ V1.28.2 统一到 1.28.2 |

### 14.3 未解问题

| # | 问题 | 影响 | 已知信息 |
|---|---|---|---|
| 1 | 11-10 界面识别失败的真实原因 | 高 | 需看画面才能定论；v2026.11.8 #1315 缓解但未确认根因 |
| 2 | 告警通道从未推送成功 | **高（盲区）** | `push_fail_streak: 32`，原因是「未配置接收地址」 |
| 3 | `driver.quit()` 超时只 log 不处理 | 中 | 上游行为，我们能做的只是保持 headless 配置一致 |
| 4 | 从手机菜单进入商店偶发失败 | 低 | 今天只失败 2 次，其它轮次正常，不影响核心任务 |
| 5 | `after_finish` 两边不一致 | 低 | 有意为之（以 Loop 为准），已在文档标注 |
| 6 | `scheduled_time` 官方默认 4:00，本机 8:00 | 无 | 有意配置 |

---

## 第 15 章 容量与风险

### 15.1 当前容量

| 项 | 总量 | 已用 | 比例 | 风险 |
|---|---|---|---|---|
| 磁盘 `/dev/sdb1` | 109 G | 59 G | 58% | 中（日志已加轮转，缓解） |
| 内存 | 7856 MB | 约 3.2 G | 41% | 低 |
| Swap | 4095 MB | 约 3.8 G | **93%** | **中**（说明内存偏紧） |
| 云游戏时长 | 随消耗递减 | — | — | **高**（烧光后要等重置或付费） |

### 15.2 趋势判断

- **磁盘**：日志已轮转（每容器 100MB 上限），面板数据增长缓慢，短期无忧
- **内存**：swap 用 93% 说明物理内存紧。当前峰值 1.86G / 上限 4g，还有余量
- **云游戏时长**：这是**最稀缺的资源**。2026-11-10 一天烧了 803 分钟。
  修复后每轮正常消耗，但一旦再出现识别失败重试，会快速烧光

### 15.3 扩容建议（如果将来要加负载）

| 现象 | 建议 |
|---|---|
| swap 持续 > 95% | 加内存到 16G |
| 峰值内存 > 3.5G | 先查补丁是否失效，再考虑加内存 |
| 磁盘 > 85% | 清理旧日志 / 旧镜像（`docker image prune`）|
| 需要同时跑多个实例 | 每个实例至少 2G 内存 |

---

## 第 16 章 变更记录

### 2026-11-11

| 时间 | 变更 | 影响 |
|---|---|---|
| 09:19 | 镜像 v2026.11.7 → v2026.11.8 | 含云游戏连接修复（#1315） |
| 12:19 | 整目录重装 | ⚠️ 误删 `logs/preview_server.py` |
| 13:04 | 重建 `preview_server.py` | 面板预览恢复 |
| 14:34 | `browser_headless_enable: false → true` | invalid session id 归零 |
| 16:47 | `restart: unless-stopped → on-failure:3` | 不再无限重启 |
| 18:14 | 补丁 1：图片缓存 | 内存不再暴涨 |
| 18:34 | 补丁 2：线程池 8 + `wait=True` | 峰值 3.7G → 1.86G |
| **18:41** | **首轮完整成功** | **验证修复有效** |
| 18:49 | `mem_limit: 6g → 4g` | 实测峰值 1.86G，够用 |
| 19:07 | `config_guard.py` + cron | 配置截断防护 |
| 20:18 | 日志轮转 + healthcheck + TZ | 运维增强 |
| 20:55 | 面板 V1.28.2 部署 | 健康接入告警 + chunked 修复 |
| 21:29 | 面板 `daemon-reload` | 消除陈旧警告 |
| 22:14 | `AFTER_FINISH: Exit → Loop` | 恢复每天 8:00 自动跑 |
| 22:35 | 本文档初版 | — |

### 2026-11-10（大故障当天）

| 时间 | 变更 |
|---|---|
| 18:45 | `mem_limit: 3g`（首次止血） |
| 19:00 | `MARCH7TH_LOG_LEVEL: DEBUG → INFO` |
| 19:00 | `MARCH7TH_AFTER_FINISH: Loop → Exit`（后来证实是理解错误） |
| 21:39 | 容器人工停止（云游戏只剩 6 分钟） |
| 21:45 | 清除浏览器登录凭证（用户要求） |
| 全天 | 生成审计包与日志分析 |

---

## 附录 A 常用命令速查

```bash
# ═══════ 状态 ═══════
sudo docker inspect -f 'Status={{.State.Status}} ExitCode={{.State.ExitCode}} RestartCount={{.RestartCount}} Health={{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' m7a
sudo docker stats --no-stream m7a
free -m
df -h /

# ═══════ 日志 ═══════
sudo docker logs --tail 200 m7a 2>&1 | sed 's/\x1b\[[0-9;]*m//g' | grep -v "^[+|]" | tail -50
sudo tail -100 /home/march7thassistant/logs/$(date +%Y-%m-%d).log
sudo grep -ac "正在启动 chrome" /home/march7thassistant/logs/$(date +%Y-%m-%d).log

# ═══════ 配置 ═══════
sudo grep -nE "^after_finish|^log_level|^browser_headless|^loop_mode|^scheduled_time" /home/march7thassistant/config.yaml
sudo docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' m7a | grep MARCH7TH
sudo tail -20 /home/march7thassistant/logs/config-guard.log

# ═══════ 启停 ═══════
sudo docker compose -f /home/march7thassistant/docker-compose.yml up -d --force-recreate
sudo docker compose -f /home/march7thassistant/docker-compose.yml down
sudo docker compose -f /home/march7thassistant/docker-compose.yml restart m7a

# ═══════ 面板 ═══════
sudo systemctl restart m7a-panel
sudo systemctl status m7a-panel
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:9999/
sudo journalctl -u m7a-panel --since "10 min ago" --no-pager | tail -50

# ═══════ 预览 ═══════
sudo docker exec m7a python3 -c "import urllib.request,json; print(json.load(urllib.request.urlopen('http://127.0.0.1:9223/health')))"
sudo docker exec -d m7a python3 /m7a/logs/preview_server.py

# ═══════ 排障 ═══════
sudo journalctl -k --since "today" | grep -i "out of memory" | tail -5
ss -tlnp | grep 922
sudo docker exec m7a ps -eo pid,comm,args --sort=-rss | head -10
```

---

## 附录 B 上游代码补丁说明

> **为什么打补丁**：上游的内存缺陷会让容器在 4G 上限下必然 OOM。
> 补丁通过 compose 的只读挂载注入，**不改镜像**，升级镜像后自动保留。

### B.1 补丁 1：图片缓存

**文件**：`/home/march7thassistant/patch/m7a/utils/image_utils.py`（10646 B）

**原文件备份**：`image_utils.py.orig`（8360 B / 199 行）

**治的病**：`read_image()` 每次 `np.fromfile` + `cv2.imdecode`，
界面识别每秒调用约 490 次，CPU 与内存双重浪费。

**改法**

```python
import threading

_CACHE_LOCK = threading.RLock()
_CACHE = {}
_CACHE_MAX = 512

@classmethod
def _cache_key(cls, path, flags):
    st = os.stat(path)
    return (os.path.abspath(path), st.st_mtime_ns, st.st_size, int(flags))

@classmethod
def _cache_get(cls, key): ...
@classmethod
def _cache_put(cls, key, value): ...   # 超限淘汰最早 1/4
@classmethod
def clear_cache(cls): ...
```

`read_image()` 改成：先算 key → 查缓存 → 命中直接返回 → 未命中才解码 → 存缓存。

**验证**

```bash
sudo docker exec -w /m7a m7a python3 -c "
from utils.image_utils import ImageUtils
import glob
p = glob.glob('/m7a/assets/images/screen/*.png')[0]
img1 = ImageUtils.read_image(p)
img2 = ImageUtils.read_image(p)
print('缓存命中返回同一对象:', img2 is img1)"
# 期望：True
```

**拼装这个补丁时的坑**：不能用 `cat` 直接拼接（会重复 `if local_max_val > max_val:`
导致 `SyntaxError: expected an indented block`）。
正确做法是定位到原文件特定行号切割再合并。

### B.2 补丁 2：线程池限流

**文件**：`/home/march7thassistant/patch/m7a/module/screen/screen.py`（20622 B）

**原文件备份**：`screen.py.orig`（19682 B）

**治的病**：`get_current_screen()` 每轮开 57 个线程，`shutdown(wait=False)` 不等待
→ 线程跨轮堆积 → 内存暴涨。

**改法**

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

**为什么是 8 而不是 57**

- i3-4160 只有 2 核 4 线程，57 个线程纯属排队等 GIL
- 8 个线程足够在 1 秒内跑完 49 个模板匹配
- 实测：8 线程下界面识别速度无明显下降，内存峰值 3.7G → 1.86G

**验证**

```bash
sudo docker exec -w /m7a m7a python3 -c "
import inspect
from module.screen.screen import Screen
src = inspect.getsource(Screen.get_current_screen)
print('_MAX_MATCH_WORKERS:', '_MAX_MATCH_WORKERS' in src)
print('wait=True:', 'wait=True' in src)"
# 期望：True True
```

### B.3 补丁的维护要点

| 情况 | 处理 |
|---|---|
| 镜像升级 | 跑一次 2.4 的兼容性检查 |
| 上游改了函数名/签名 | 补丁会 `ImportError` 或静默失效，需重新对照新源码做 |
| 想临时停用补丁 | 注释掉 compose 里对应挂载行，`--force-recreate` |
| 想彻底回退 | 用 `.orig` 文件覆盖，或删掉挂载行 |

---

## 附录 C 面板开发与测试

### C.1 环境搭建

```bash
cd /home/huangsongping/dsh-workspace/m7a-fix

# 建 venv（/tmp 会被清空，需重建）
python3 -m venv /tmp/auditvenv

# 装依赖（必须用 lock 文件，不要装最新版）
/tmp/auditvenv/bin/pip install -r panel_migr/requirements.lock \
                              -r panel_migr/requirements-dev.lock
```

> ⚠️ **不要装最新版依赖**。必须按 lock 文件的锁定版本
> （fastapi 0.141.1 / starlette 1.3.1 / uvicorn 0.44.0 等）。
> 盲目装最新版会导致 pytest **卡死 40+ 分钟**。

### C.2 跑测试

```bash
cd panel_migr
/tmp/auditvenv/bin/python -m pytest -q
# 当前基线：1435 passed, 1 skipped

# 只跑新增的
/tmp/auditvenv/bin/python -m pytest tests/test_v1282_*.py -q

# ruff
/tmp/auditvenv/bin/python -m ruff check services/ tests/
```

### C.3 部署到线上

```bash
# ① 备份
sudo tar czf /www/wwwroot/m7a-panel/backups/pre-v$(date +%Y%m%d-%H%M%S).tar.gz \
  -C /www/wwwroot/m7a-panel panel_migr

# ② 拷贝改动文件
for f in services/sysmetrics.py services/monitor.py services/alerts.py \
         static/panel.js static/panel.css; do
  sudo cp /home/huangsongping/dsh-workspace/m7a-fix/panel_migr/$f \
          /www/wwwroot/m7a-panel/panel_migr/$f
done

# ③ 重启
sudo systemctl restart m7a-panel

# ④ 验证
systemctl is-active m7a-panel
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:9999/
```

### C.4 已知的测试环境限制

| 测试 | 为什么失败 | 影响 |
|---|---|---|
| `test_apply_zip_keeps_existing_venv` | 会真实调用 `systemctl restart`，非 root 报 `Interactive authentication required.` | 无（环境限制） |
| `test_kernel_guard_blocks_ocreate_reference` | 需要「他人属主」的文件，普通用户造不出来 | 无（环境限制） |

---

## 附录 D 术语表

| 术语 | 含义 |
|---|---|
| **m7a** | March7thAssistant 的简称，三月七小助手 |
| **云游戏** | 米哈游官方的星穹铁道云游戏，小助手通过浏览器操作它 |
| **CDP** | Chrome DevTools Protocol，chrome 的调试协议，小助手用它截图/点击 |
| **cgroup** | Linux 控制组，Docker 的资源限制靠它实现 |
| **OOM** | Out Of Memory，Linux 内核在内存耗尽时杀进程 |
| **chunked** | HTTP 分块传输编码，Docker Engine API 一律用它 |
| **EBUSY** | 设备/资源忙，这里是 `os.replace()` 撞上挂载点 |
| **inode** | 文件在 Unix 里的唯一编号，`os.replace` 换的是 inode |
| **headless** | 无头浏览器，不显示界面，适合容器 |
| **MJPEG** | Motion JPEG，每帧都是一张 JPEG 的流 |
| **scheduled** | 定时模式，`loop_mode` 的取值之一 |
| **after_finish** | 任务完成后做什么（Loop / Exit / Shutdown 等） |

---

## 反馈

这份文档还在完善。如果发现：
- 命令跑不通
- 漏掉了你遇到的情况
- 数字和你的环境对不上

请开 issue 或提 PR。

**文档与代码同步维护**：每次改配置/升级镜像/修 bug，
请在本文件 [第 16 章 变更记录](#第-16-章-变更记录) 追加一行。
