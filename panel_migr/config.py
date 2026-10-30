"""m7a_panel 迁移版（Python/FastAPI）公共配置。

所有常量与 index.php 的 define() 一一对齐；改动需同步 PHP 侧。
环境变量覆盖用于本地测试与部署差异化。
"""
import os
from pathlib import Path

# panel_migr/ 部署在 PHP 面板目录下：BASE_DIR 上级 = 面板目录
# （index.php、data/、.instances.php、.panel_config.php 所在处）
BASE_DIR = Path(os.environ.get(
    "M7A_PANEL_DIR", Path(__file__).resolve().parent.parent
)).resolve()
DATA_DIR = BASE_DIR / "data"

# ===== 与 index.php define() 对齐 =====
SKEY = "m7a_panel_auth"          # 登录态 session 键
CSRF_KEY = "m7a_panel_csrf"      # CSRF session 键（M2 使用）
DEFAULT_DIR = "/home/march7thassistant"
DEFAULT_CONTAINER = "m7a"

HISTORY_IDLE_SECONDS = 90        # 日志静默超过该秒数视为任务已结束
HISTORY_KEEP = 200               # 历史最多保留条数
SCHEDULE_WINDOW_SECONDS = 1800   # 计划任务补跑窗口（M2+）

# ===== 面板自身更新（与 index.php define() 对齐，M4 可用环境变量覆盖）=====
PANEL_VERSION = os.environ.get("M7A_PANEL_VERSION", "1.24.5")
UPDATE_ENABLED = True
UPDATE_TYPE = "github"           # gitea / github
_UPDATE_HOST_RAW = os.environ.get("M7A_UPDATE_HOST", "https://github.com").strip().rstrip("/")
# v1.24.3 安全加固（M7A-11）：更新源必须走 https，非 https 一律回落到官方默认值，
# 避免环境变量被改成明文/内网地址后「官方更新」变成投递恶意包的通道。
UPDATE_HOST = _UPDATE_HOST_RAW if _UPDATE_HOST_RAW.startswith("https://") else "https://github.com"
UPDATE_OWNER = os.environ.get("M7A_UPDATE_OWNER", "starchaser-cyber")
UPDATE_REPO = os.environ.get("M7A_UPDATE_REPO", "march7th-assistant-web-management-panel-python-remake")
UPDATE_BRANCH = "main"
BACKUP_KEEP = 5                  # 版本备份保留份数（index_*.php 与 panel_*.zip 合并计数）

# ===== 任务白名单（与 index.php $TASKS 对齐：key → 标签）=====
TASKS = {
    "main": "全量运行",
    "daily": "日常任务",
    "power": "清体力",
    "notify": "测试通知",
    "divergentloop": "差分宇宙",
    "universe": "模拟宇宙",
    "currencywars": "货币战争",
    "currencywarsloop": "货币战争循环",
}

# 危险动作：PHP 前置里 in_array($action, ...) → alert_quiet(600)
DANGEROUS_ACTIONS = {
    "restart", "update", "update_image", "stop_task", "stop_loop", "stop", "do_update",
}

INSTANCES_FILE = BASE_DIR / ".instances.php"
PANEL_CONFIG_FILE = BASE_DIR / ".panel_config.php"
PASS_FILE = BASE_DIR / ".panel_pass.php"

# ===== session 存储（登录态）=====
# v1.24.3 安全加固（M7A-02）：默认目录从 world-writable 的 /tmp 移入面板私有目录，
# 文件权限 0666 → 0660。原先 /tmp/sess_<sid> 为 0666，任何本地账号都能往有效 sid
# 里追加 `m7a_panel_auth|b:1;` 伪造管理员登录（面板以 root 运行 → 本地提权）。
# 若仍需与 PHP-fpm(www) 共享登录态：把两端 session.save_path 指向同一私有目录，
# 并 chgrp www；确需 0666 时显式设 M7A_SESSION_MODE=666（不推荐）。
SESSION_SAVE_PATH = os.environ.get("M7A_SESSION_DIR", str(BASE_DIR / "sessions"))
try:
    SESSION_FILE_MODE = int(os.environ.get("M7A_SESSION_MODE", "660"), 8) & 0o777
except ValueError:
    SESSION_FILE_MODE = 0o660

# ===== M6 后端整合（Python 唯一后端）=====
# v1.22 起 Python 为唯一后端：未匹配的页面/GET 由 Python 本地渲染，不再转发回旧站点。
# FWD_ENABLE=1 时恢复转发链路（回滚开关，用于极端情况兜底）。
# 说明：面板对外访问入口（含 9999 端口）由反代层 proxy_pass 到本服务，端口本身保留。
FWD_ENABLE = os.environ.get("M7A_FWD_ENABLE", "0") != "0"

# 转发兜底仍保留上游常量（FWD_ENABLE=1 时使用），默认不走。
PHP_UPSTREAM = os.environ.get("M7A_PHP_UPSTREAM", "http://127.0.0.1:9999")
FWD_HEADER = "X-M7A-Fwd"
FWD_HEADER_VALUE = "1"

# ===== 告警状态机增强（重试 / 退避 / 去重）=====
ALERT_RETRY_DELAYS = (30, 60, 120)   # 推送失败后的指数退避重试间隔（秒），最多 3 次
ALERT_MAX_RETRY = len(ALERT_RETRY_DELAYS)
ALERT_BACKOFF_STREAK = 3             # 连续失败达到该次数进入冷静期
ALERT_BACKOFF_SECONDS = 300          # 冷静期时长（秒），期内不再推送、仅记事件
ALERT_DEDUP_WINDOW = 120             # 同 kind 告警去重窗口（秒），窗口内不重复推送

# ===== 服务监听 =====
LISTEN_HOST = os.environ.get("M7A_LISTEN_HOST", "127.0.0.1")
LISTEN_PORT = int(os.environ.get("M7A_LISTEN_PORT", "8787"))

# ===== M3 游戏画面预览（收编 /m7a-preview/ 外挂层）=====
# 容器内 preview_server v3.1（CDP 帧源）监听容器 0.0.0.0:9223；
# 面板动态解析容器 IP 反代之（socat / preview-relay systemd / 宿主 cron 拉起由此收编）。
PREVIEW_SECRET_FILE = os.environ.get("M7A_PREVIEW_SECRET") or None
# None → {DEFAULT_DIR}/logs/preview_secret（与 PHP preview_secret() 路径一致）
PREVIEW_UPSTREAM_PORT = int(os.environ.get("M7A_PREVIEW_UP_PORT", "9223"))
PREVIEW_SERVER_SCRIPT = os.environ.get(
    "M7A_PREVIEW_SCRIPT", "/m7a/logs/preview_server.py"
)
FFMPEG_BIN = os.environ.get("M7A_FFMPEG", "ffmpeg")

# M4 事件化：后台 runner 开关（测试置 0 关闭）与保底采样间隔（秒）
EVENTS_BG = os.environ.get("M7A_EVENTS_BG", "1") != "0"
try:
    SAMPLE_IV = max(1, int(os.environ.get("M7A_SAMPLE_IV", "5")))
except ValueError:
    SAMPLE_IV = 5

# 时区对齐：PHP date() 时区与系统时区若不一致会导致零点/显示偏差。
# 部署时用 M7A_TZ 显式锁定（默认 None = 系统时区）。
LOCAL_TZ = os.environ.get("M7A_TZ") or None

# ===== M2 告警升级（v1.24）=====
# 告警分级：info / warn / critical；不同级别可配不同通道，也支持按级别静音。
ALERT_SEVERITIES = ("info", "warn", "critical")

# 磁盘空间预警：剩余百分比低于 warn 发 warn、低于 crit 发 critical（面板内可调）。
DISK_WARN_PERCENT = 15               # 默认：剩余 < 15% 记 WARN
DISK_CRIT_PERCENT = 8                # 默认：剩余 <  8% 记 CRITICAL
DISK_PROBE_PATH = os.environ.get("M7A_DISK_PATH", "/")   # 探针挂载点（默认系统盘）

# 每日健康晨报：默认开启，每天本地时间 08:00 发一次（面板内可调/可关）。
MORNING_REPORT_ENABLE = True
MORNING_REPORT_TIME = "08:00"

# 上游本体更新提醒：定时查 GitHub Release 比对本地本体版本，仅提醒不自动升级。
UPSTREAM_REPO = "moesnow/March7thAssistant"
UPSTREAM_API = "https://api.github.com/repos/moesnow/March7thAssistant/releases/latest"
UPSTREAM_RELEASE_URL = "https://github.com/moesnow/March7thAssistant/releases/latest"
UPSTREAM_CHECK_IV = 6 * 3600         # 版本检查间隔（秒），实际是否发起由面板状态节流
UPSTREAM_HTTP_TIMEOUT = 6            # 上游接口超时（秒），失败静默降级不乱重试

# 磁盘探针巡检间隔（秒）；每日晨报每分钟判一次「是否到点」，不需要额外常量。
DISK_CHECK_IV = 6 * 60
