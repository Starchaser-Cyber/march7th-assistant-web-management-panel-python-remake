"""M3 游戏画面预览收编：/m7a-preview/ 反代 + 自愈 + H.264 极致档管线。

收编对象（原外挂 4 层）：
1. socat 宿主中继 —— 由本模块的动态容器 IP 反代取代（容器重建 IP 变化自愈）；
2. preview-relay systemd 服务 —— 不再需要；
3. 宿主 crontab 每分钟 pgrep+docker exec 拉起 —— 收编为 ensure_upstream() 面板自愈；
4. 容器内 preview_server 进程 —— 保留为 CDP 帧源（Chrome 只监听容器 localhost，
   宿主无法直连 CDP），但拉起与寻址全部由面板托管，不再是独立外挂体系。

帧源链路：浏览器 → nginx /m7a-preview/ → 本面板(:8787) → docker inspect 容器IP → :9223
token 与 preview_server v3.1 一致：HMAC-SHA256(secret, UTC"yyyymmdd") 前 32 位，按天轮换。
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import re
import shutil
import time
from pathlib import Path

import httpx

import config
from services.shell import run_cmd

_IP_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")

_UP_TTL = 5.0        # /health 探测成功缓存秒数
_ENSURE_GAP = 60.0   # 自愈（docker exec 拉起）节流秒数

_resolve_cache: dict[str, tuple[str, float]] = {}
_health_cache: dict[str, float] = {}
_last_ensure: dict[str, float] = {}


def reset_state() -> None:
    """测试用：清空模块级缓存。"""
    _resolve_cache.clear()
    _health_cache.clear()
    _last_ensure.clear()


# ===== token（与 PHP preview_token()/preview_server day_token() 算法逐字节一致）=====

def day_token(secret: str, day: str | None = None) -> str:
    d = day or time.strftime("%Y%m%d", time.gmtime())
    return hmac.new(secret.encode(), d.encode(), hashlib.sha256).hexdigest()[:32]


def preview_secret() -> str:
    path = (
        Path(config.PREVIEW_SECRET_FILE)
        if config.PREVIEW_SECRET_FILE
        else Path(config.DEFAULT_DIR) / "logs" / "preview_secret"
    )
    try:
        return path.read_text("utf-8").strip()
    except OSError:
        return ""


def preview_token() -> dict:
    """GET ?ajax=preview_token —— 与 PHP preview_token() 返回结构一致。"""
    s = preview_secret()
    if not s:
        return {"ok": False, "msg": "预览服务未部署（服务器缺少 preview_secret）"}
    return {"ok": True, "token": day_token(s)}


# ===== 上游发现与自愈 =====

def resolve_upstream(container: str) -> str | None:
    """docker inspect 解析容器 IPv4；60s 缓存，失败不缓存（下次重试）。"""
    now = time.time()
    hit = _resolve_cache.get(container)
    if hit and now - hit[1] < 60:
        return hit[0]
    r = run_cmd(
        "docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "
        + container,
        timeout=10,
    )
    ip = (r["out"] or "").strip().splitlines()[-1].strip() if r["code"] == 0 else ""
    if not _IP_RE.fullmatch(ip):
        return None
    _resolve_cache[container] = (ip, now)
    return ip


def upstream_base(container: str) -> str | None:
    ip = resolve_upstream(container)
    return f"http://{ip}:{config.PREVIEW_UPSTREAM_PORT}" if ip else None


async def health_ok(container: str, base: str) -> bool:
    now = time.time()
    if _health_cache.get(container, 0) > now - _UP_TTL:
        return True
    try:
        async with httpx.AsyncClient(timeout=2.0, trust_env=False) as c:
            r = await c.get(base + "/health")
        if r.status_code == 200:
            _health_cache[container] = time.time()
            return True
    except httpx.HTTPError:
        pass
    return False


def ensure_upstream(container: str) -> bool:
    """自愈：docker exec -d 拉起容器内 preview_server（60s 节流）。"""
    now = time.time()
    if now - _last_ensure.get(container, 0) < _ENSURE_GAP:
        return False
    _last_ensure[container] = now
    r = run_cmd(
        f"docker exec -d {container} python3 {config.PREVIEW_SERVER_SCRIPT}",
        timeout=15,
    )
    return r["code"] == 0


async def get_upstream(container: str) -> tuple[str | None, str]:
    """返回 (base_url, err_msg)。探测失败 → 自愈一次 → 复检；仍失败返回 None。"""
    base = upstream_base(container)
    if base is None:
        return None, "容器不可达（docker inspect 失败或容器未运行）"
    if await health_ok(container, base):
        return base, ""
    ensure_upstream(container)
    await asyncio.sleep(0.8)
    _health_cache.pop(container, None)
    if await health_ok(container, base):
        return base, ""
    return None, "preview_server 不可达（容器内服务未就绪，已尝试拉起）"


# ===== H.264 极致档（ffmpeg: JPEG 帧 → H.264 fMP4 → 浏览器 MSE）=====

FFMPEG_ARGS = [
    "-hide_banner", "-loglevel", "error",
    "-f", "image2pipe", "-c:v", "mjpeg", "-framerate", "15", "-i", "pipe:0",
    "-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency",
    "-pix_fmt", "yuv420p", "-g", "15", "-keyint_min", "15", "-sc_threshold", "0",
    "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
    "-f", "mp4",
    "-movflags", "frag_keyframe+empty_moov+default_base_moof",
    "pipe:1",
]


def ffmpeg_available() -> bool:
    return shutil.which(config.FFMPEG_BIN) is not None
