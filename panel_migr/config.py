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
PANEL_VERSION = os.environ.get("M7A_PANEL_VERSION", "1.19")
UPDATE_ENABLED = True
UPDATE_TYPE = "github"           # gitea / github
UPDATE_HOST = os.environ.get("M7A_UPDATE_HOST", "https://github.com")
UPDATE_OWNER = os.environ.get("M7A_UPDATE_OWNER", "starchaser-cyber")
UPDATE_REPO = os.environ.get("M7A_UPDATE_REPO", "march7th-assistant-web-management-panel")
UPDATE_BRANCH = "main"
BACKUP_KEEP = 5                  # 版本备份保留份数

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

# ===== PHP session 共享（登录态互通的关键）=====
SESSION_SAVE_PATH = os.environ.get("M7A_SESSION_DIR", "/tmp")

# ===== 绞杀者网关 =====
# 未实现 / 未登录的请求转发回原 PHP 站点（nginx :9999）。
# X-M7A-Fwd 标记供 nginx if 排除，防止 8787→9999→8787 转发循环。
PHP_UPSTREAM = os.environ.get("M7A_PHP_UPSTREAM", "http://127.0.0.1:9999")
FWD_HEADER = "X-M7A-Fwd"
FWD_HEADER_VALUE = "1"

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
