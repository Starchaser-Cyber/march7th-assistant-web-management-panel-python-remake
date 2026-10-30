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
import shlex
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
    # 本地日期（与容器 preview_server day_token() 的 time.strftime('%Y%m%d') 口径一致）
    d = day or time.strftime("%Y%m%d")
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
    # v1.24.3 安全加固（M7A-05）：容器名与其他 25 处调用点一致做 shell 引用
    r = run_cmd(
        "docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "
        + shlex.quote(container),
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
    # v1.24.3 安全加固（M7A-05）：同上，补 shell 引用
    r = run_cmd(
        f"docker exec -d {shlex.quote(container)} python3 "
        f"{shlex.quote(str(config.PREVIEW_SERVER_SCRIPT))}",
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

FPS_CHOICES = (15, 30)   # v1.20 帧率选项（不做 60：云游戏源达不到，徒增解码负担）
DEFAULT_FPS = 15


def normalize_fps(v) -> int:
    """白名单化前端 fps 参数：非法值回落 15。"""
    try:
        n = int(v)
    except (TypeError, ValueError):
        return DEFAULT_FPS
    return n if n in FPS_CHOICES else DEFAULT_FPS


# ===== H.264 编码器探测（Q-01）=====
# libx264 因 GPL 在 Alpine 官方包与多数静态构建里常被剔除（只带 libopenh264 /
# h264_vaapi）。旧版硬编码 libx264 → ffmpeg 起来后立刻 exit 8，前端只看到 1000
# 正常关闭 → 无限重连，用户完全不知道原因。

# 候选顺序：软件编码器优先（滤镜链与旧版一致），硬件编码器需要对应设备节点
_ENC_CANDIDATES = (
    "libx264",
    "libopenh264",
    "h264_vaapi",
    "h264_nvenc",
    "h264_qsv",
    "h264_v4l2m2m",
)

# 硬件编码器需要的设备节点（不存在就不选它，避免"探测到了但一跑就崩"）
_HW_DEVICES = {
    "h264_vaapi": ("/dev/dri/renderD128",),
    "h264_qsv": ("/dev/dri/renderD128",),
    "h264_nvenc": ("/dev/nvidia0", "/dev/nvidiactl"),
    "h264_v4l2m2m": ("/dev/video0", "/dev/video11", "/dev/video12"),
}

_ENC_CACHE: list[str] | None = None


def reset_codec_cache() -> None:
    """测试用：清空编码器探测缓存（进程内只 fork 一次 ffmpeg -encoders）。"""
    global _ENC_CACHE
    _ENC_CACHE = None


def _encoders() -> list[str]:
    """`ffmpeg -encoders` 输出行（带缓存；探测失败缓存空列表，避免每次 WS 都 fork）。"""
    global _ENC_CACHE
    if _ENC_CACHE is None:
        try:
            r = run_cmd(f'"{config.FFMPEG_BIN}" -hide_banner -encoders', timeout=20)
            out = (r.get("out") or "") if r.get("code") == 0 else ""
        except Exception:
            out = ""
        _ENC_CACHE = out.splitlines()
    return _ENC_CACHE


def _device_ok(codec: str) -> bool:
    """硬件编码器：设备节点存在才认为可用。"""
    devs = _HW_DEVICES.get(codec)
    if not devs:
        return True
    return any(Path(d).exists() for d in devs)


def h264_codec() -> str | None:
    """探测可用的 H.264 编码器名；一个都没有 → None（H.264 档不可用）。"""
    lines = _encoders()
    if not lines:
        return None
    for c in _ENC_CANDIDATES:
        if any(c in line for line in lines) and _device_ok(c):
            return c
    return None


def _codec_pre_input(codec: str) -> list:
    """编码器要求放在 `-i` **之前**的参数（vaapi 需要先初始化设备）。"""
    if codec == "h264_vaapi":
        return ["-vaapi_device", _HW_DEVICES["h264_vaapi"][0]]
    return []


def _codec_args(codec: str) -> list:
    """按编码器给出 `-c:v` 及其私有参数（-preset/-tune 是 libx264 专属）。"""
    if codec == "libx264":
        return ["-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency"]
    if codec == "libopenh264":
        return ["-c:v", "libopenh264", "-b:v", "4M"]
    if codec == "h264_nvenc":
        return ["-c:v", "h264_nvenc", "-preset", "p1", "-tune", "ull"]
    return ["-c:v", codec]


def _codec_vf(codec: str, fps: int) -> str:
    """按编码器给出滤镜链：软件编码器吃 yuv420p，vaapi/qsv 需要 hwupload。"""
    base = f"fps={fps},scale=trunc(iw/2)*2:trunc(ih/2)*2"
    if codec == "h264_vaapi":
        return base + ",format=nv12,hwupload"
    if codec == "h264_qsv":
        return base + ",format=nv12,hwupload=extra_hw_frames=64"
    return base


def build_ffmpeg_args(fps: int = DEFAULT_FPS) -> list:
    """按帧率生成 ffmpeg 转码参数（编码器由 h264_codec() 探测，不再硬编码 libx264）。

    -use_wallclock_as_timestamps 1：输入时间轴锁墙钟。上游 CDP 帧到达率受游戏实际
    帧率影响（7~30fps 波动）；旧版声明式 -framerate 15 在上游到 30fps 时 5s 墙钟会
    产出 10.67s 媒体时间（延迟持续累积），实测新版在 7/15/30fps 任意到达率下媒体
    时长均 ≈ 墙钟时长。
    -vf fps=N：把输出节奏定死为 N 帧/秒（实验表明该滤镜必需）。
    -g/-keyint_min=N：每个 fMP4 chunk 都以关键帧开始，端到端延迟 ≈ 1 个关键帧间隔。
    """
    fps = normalize_fps(fps)
    # 探测不到编码器时仍按 libx264 出参：调用方（routers/preview.py）已用
    # ffmpeg_available() 拦截，这里只保证返回的参数结构合法、可单测。
    codec = h264_codec() or "libx264"
    args = ["-hide_banner", "-loglevel", "error"]
    args += _codec_pre_input(codec)
    args += [
        "-f", "image2pipe", "-c:v", "mjpeg",
        "-use_wallclock_as_timestamps", "1", "-framerate", str(fps), "-i", "pipe:0",
    ]
    args += _codec_args(codec)
    if codec in ("libx264", "libopenh264", "h264_nvenc", "h264_v4l2m2m"):
        args += ["-pix_fmt", "yuv420p"]
    args += [
        "-g", str(fps), "-keyint_min", str(fps), "-sc_threshold", "0",
        "-vf", _codec_vf(codec, fps),
        "-f", "mp4",
        "-movflags", "frag_keyframe+empty_moov+default_base_moof",
        "pipe:1",
    ]
    return args


def ffmpeg_available() -> bool:
    """ffmpeg 可执行 **且** 编入了可用的 H.264 编码器（Q-01：只判 which 不够）。"""
    return shutil.which(config.FFMPEG_BIN) is not None and h264_codec() is not None
