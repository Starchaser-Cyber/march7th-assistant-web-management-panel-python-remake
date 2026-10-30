"""V1.24.5 修复回归：Q-01（H.264 编码器探测）+ Q-05（ffmpeg stderr 排空）。

Q-01：`ffmpeg_available()` 旧版只判断二进制存在，硬编码 libx264。Alpine 官方包 /
静态构建常因 GPL 剔除 libx264（只带 libopenh264 / h264_vaapi）→ ffmpeg 起来后
立刻 exit 8，WS 以 code 1000（正常关闭）收尾 → 前端无限重连且不显示原因。
Q-05：`stderr=PIPE` 全项目零处读取，管道写满 64 KiB 后 ffmpeg 阻塞 → 预览冻结，
错误信息全丢。
"""
import asyncio
import json
import stat
import sys
import tempfile
import threading
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from fastapi.testclient import TestClient

import config as cfg
import services.preview as pv
from main import app
from routers import preview as pvr

client = TestClient(app)

_ENC_LIBX264 = " V....D libx264              libx264 H.264 / AVC / MPEG-4 AVC (codec h264)"
_ENC_OPENH264 = " V....D libopenh264           OpenH264 H.264 / AVC (codec h264)"
_ENC_VAAPI = " V....D h264_vaapi           H.264/AVC (VAAPI) (codec h264)"
_ENC_NONE = " V..... mpeg4                MPEG-4 part 2"
# vaapi/qsv 需要设备节点；测试里假定不存在，避免依赖宿主机 /dev/dri
_NO_DEV = {"h264_vaapi", "h264_qsv", "h264_nvenc", "h264_v4l2m2m"}

_secret_file = Path(tempfile.gettempdir()) / f"v1245_secret_{uuid.uuid4().hex[:8]}"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    pv.reset_state()
    pv.reset_codec_cache()
    monkeypatch.setattr(cfg, "PREVIEW_SECRET_FILE", str(_secret_file))
    # 硬件编码器设备节点视为不存在（除非用例显式放开）
    monkeypatch.setattr(pv, "_device_ok", lambda codec: codec not in _NO_DEV)
    yield
    pv.reset_state()
    pv.reset_codec_cache()


def _stub_encoders(monkeypatch, lines: list[str]) -> list[str]:
    """把 `ffmpeg -encoders` 打桩成给定输出；返回调用记录（验证缓存）。"""
    calls: list[str] = []

    def _run(cmd, timeout=120):
        calls.append(cmd)
        if "-encoders" in cmd:
            return {"code": 0, "out": "\n".join(lines)}
        if cmd.startswith("docker inspect"):
            return {"code": 0, "out": "127.0.0.1"}
        return {"code": 0, "out": ""}

    monkeypatch.setattr(pv, "run_cmd", _run)
    monkeypatch.setattr(pv.shutil, "which", lambda x: "/usr/bin/ffmpeg")
    return calls


async def _ok_health(container, base):
    return True


def _start_dummy_ws_upstream() -> int:
    """起一个只接受连接的假 preview_server WS（不推帧）：本文件只验证 ffmpeg 侧行为。"""
    import websockets

    box: dict = {}
    ev = threading.Event()

    async def handler(ws):
        try:
            await asyncio.sleep(30)
        except Exception:
            pass

    def run():
        async def main():
            srv = await websockets.serve(handler, "127.0.0.1", 0)
            box["port"] = srv.sockets[0].getsockname()[1]
            ev.set()
            await asyncio.Future()

        try:
            asyncio.run(main())
        except Exception:
            ev.set()

    threading.Thread(target=run, daemon=True).start()
    assert ev.wait(5), "假 WS 上游启动超时"
    return box["port"]


def _stub_upstream(monkeypatch) -> None:
    """把 docker 寻址与 health 探测都指到本地假 WS 上游。"""
    port = _start_dummy_ws_upstream()
    monkeypatch.setattr(cfg, "PREVIEW_UPSTREAM_PORT", port)
    monkeypatch.setattr(pv, "health_ok", _ok_health)


def _write_secret(text: str) -> None:
    _secret_file.write_text(text, encoding="utf-8")


def _out_codec(args: list[str]) -> str:
    """输出编码器 = **最后**一个 `-c:v`（第一个是输入端 `-c:v mjpeg` 解码器）。"""
    idx = [i for i, a in enumerate(args) if a == "-c:v"]
    assert idx, "参数里没有 -c:v"
    return args[idx[-1] + 1]


# ===== Q-01：编码器探测 =====

def test_h264_codec_none_when_ffmpeg_has_no_h264(monkeypatch):
    """只有 mpeg4 的 ffmpeg（Alpine 常见）→ 判定 H.264 不可用，而不是假装可用。"""
    _stub_encoders(monkeypatch, [_ENC_NONE])
    assert pv.h264_codec() is None
    assert pv.ffmpeg_available() is False


def test_ffmpeg_available_false_without_libx264(monkeypatch):
    """Q-01 核心回归：无 libx264 时 ffmpeg_available() 必须为 False。"""
    _stub_encoders(monkeypatch, [_ENC_NONE])
    # 二进制存在但没编 H.264 → 不可用（旧版只判 which，会返回 True）
    assert pv.shutil.which(cfg.FFMPEG_BIN) is not None
    assert pv.ffmpeg_available() is False


def test_h264_codec_falls_back_to_libopenh264(monkeypatch):
    """有 libopenh264 无 libx264 → 选 libopenh264 并生成对应参数。"""
    _stub_encoders(monkeypatch, [_ENC_OPENH264])
    assert pv.h264_codec() == "libopenh264"
    assert pv.ffmpeg_available() is True
    args = pv.build_ffmpeg_args(15)
    assert _out_codec(args) == "libopenh264"
    assert "libx264" not in args
    assert "fps=15," in " ".join(args)


def test_h264_codec_prefers_libx264(monkeypatch):
    """两者都有时优先 libx264（滤镜链与 V1.24.4 之前一致，不改变既有行为）。"""
    _stub_encoders(monkeypatch, [_ENC_OPENH264, _ENC_LIBX264])
    assert pv.h264_codec() == "libx264"
    args = pv.build_ffmpeg_args(30)
    assert _out_codec(args) == "libx264"
    assert args[args.index("-preset") + 1] == "veryfast"
    assert args[args.index("-g") + 1] == "30"


def test_h264_vaapi_skipped_without_device(monkeypatch):
    """探测到 h264_vaapi 但 /dev/dri 不存在 → 不选它（选了会一跑就崩）。"""
    _stub_encoders(monkeypatch, [_ENC_VAAPI])
    assert pv.h264_codec() is None


def test_h264_vaapi_used_when_device_present(monkeypatch):
    """设备节点存在时选 vaapi，并把 -vaapi_device 放到 -i 之前 + hwupload 滤镜。"""
    _stub_encoders(monkeypatch, [_ENC_VAAPI])
    monkeypatch.setattr(pv, "_device_ok", lambda codec: True)
    pv.reset_codec_cache()
    assert pv.h264_codec() == "h264_vaapi"
    args = pv.build_ffmpeg_args(15)
    assert _out_codec(args) == "h264_vaapi"
    assert args.index("-vaapi_device") < args.index("-i")
    assert "hwupload" in " ".join(args)


def test_encoders_probed_only_once(monkeypatch):
    """探测结果进程内缓存：多个 WS 不会每次都 fork ffmpeg。"""
    calls = _stub_encoders(monkeypatch, [_ENC_LIBX264])
    for _ in range(5):
        assert pv.ffmpeg_available() is True
    assert len([c for c in calls if "-encoders" in c]) == 1


def test_reset_codec_cache_forces_reprobe(monkeypatch):
    calls = _stub_encoders(monkeypatch, [_ENC_LIBX264])
    pv.h264_codec()
    pv.reset_codec_cache()
    pv.h264_codec()
    assert len([c for c in calls if "-encoders" in c]) == 2


def test_encoder_probe_failure_caches_empty(monkeypatch):
    """探测本身失败（ffmpeg 崩/超时）→ 缓存空列表、返回 None，且不重复 fork。"""
    calls: list[str] = []

    def _run(cmd, timeout=120):
        calls.append(cmd)
        return {"code": 127, "out": "ffmpeg: not found"}

    monkeypatch.setattr(pv, "run_cmd", _run)
    monkeypatch.setattr(pv.shutil, "which", lambda x: "/usr/bin/ffmpeg")
    assert pv.h264_codec() is None
    assert pv.h264_codec() is None
    assert len([c for c in calls if "-encoders" in c]) == 1


def test_ws264_closes_1013_when_no_h264_encoder(monkeypatch):
    """无 H.264 编码器时 WS 以 1013（非 1000）关闭并给出原因，前端才能提示。"""
    _stub_encoders(monkeypatch, [_ENC_NONE])
    _write_secret("s" * 48)
    with client.websocket_connect(
        "/m7a-preview/ws264?token=" + pv.day_token("s" * 48)
    ) as ws:
        msg = ws.receive_text()
        assert "H.264" in msg or "ffmpeg" in msg
        with pytest.raises(Exception) as ei:
            ws.receive_text()
    assert getattr(ei.value, "code", None) == 1013


# ===== Q-05：stderr 排空 =====

def _proc_with_stderr(lines: list[bytes]):
    """构造只有 stderr 的假 proc：StreamReader 预填内容，读完 EOF。"""
    async def _make():
        reader = asyncio.StreamReader()
        for ln in lines:
            reader.feed_data(ln)
        reader.feed_eof()

        class _P:
            stderr = reader
            returncode = 8

        return _P()

    return _make


def test_drain_err_keeps_tail_lines():
    """stderr 全量排空，只保留末尾 _ERR_TAIL_LINES 行。"""
    total = pvr._ERR_TAIL_LINES + 30
    lines = [f"line-{i}\n".encode() for i in range(total)]

    async def run():
        proc = await _proc_with_stderr(lines)()
        tail: list[str] = []
        await pvr._drain_err(proc, tail)
        return tail

    tail = asyncio.run(run())
    assert len(tail) == pvr._ERR_TAIL_LINES
    assert tail[-1] == f"line-{total - 1}"
    assert tail[0] == f"line-{total - pvr._ERR_TAIL_LINES}"


def test_drain_err_tolerates_missing_stderr():
    """proc 无 stderr 属性（或已关闭）时不抛异常。"""
    class _P:
        stderr = None

    tail: list[str] = []
    asyncio.run(pvr._drain_err(_P(), tail))
    assert tail == []


def test_drain_err_handles_non_utf8():
    """stderr 里的非 UTF-8 字节用 replace 解码，不能把协程炸掉。"""
    async def run():
        proc = await _proc_with_stderr([b"bad \xff\xfe bytes\n"])()
        tail: list[str] = []
        await pvr._drain_err(proc, tail)
        return tail

    tail = asyncio.run(run())
    assert len(tail) == 1 and "bad" in tail[0]


def test_exit_reason_includes_last_stderr_line():
    reason = pvr._exit_reason(8, ["junk", "Unknown encoder 'libx264'"])
    assert "8" in reason and "Unknown encoder 'libx264'" in reason
    assert len(pvr._exit_reason(1, ["x" * 500])) <= 200


def test_exit_reason_works_with_empty_tail():
    reason = pvr._exit_reason(8, [])
    assert "8" in reason and "ffmpeg" in reason


# ===== Q-01 + Q-05 端到端：ffmpeg 运行时异常退出 → 1013 + 原因 =====

def _fake_ffmpeg(tmp: Path, *, exit_code: int, stderr_text: str) -> Path:
    """造假 ffmpeg：`-encoders` 打印 libx264（让探测通过），其余情况立刻失败退出。"""
    script = tmp / "fake-ffmpeg.sh"
    script.write_text(
        "#!/bin/sh\n"
        'for a in "$@"; do\n'
        '  if [ "$a" = "-encoders" ]; then\n'
        f"    echo '{_ENC_LIBX264}'\n"
        "    exit 0\n"
        "  fi\n"
        "done\n"
        f"echo '{stderr_text}' >&2\n"
        f"exit {exit_code}\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


def test_ws264_runtime_ffmpeg_failure_closes_1013_with_reason(tmp_path, monkeypatch):
    """Q-05：ffmpeg 非零退出 → close 1013 + stderr 原因（旧版是 1000 + 空原因）。"""
    fake = _fake_ffmpeg(tmp_path, exit_code=8, stderr_text="Unknown encoder 'libx264'")
    monkeypatch.setattr(cfg, "FFMPEG_BIN", str(fake))
    pv.reset_codec_cache()
    _stub_upstream(monkeypatch)
    monkeypatch.setattr(
        pv, "run_cmd",
        lambda cmd, timeout=120: (
            {"code": 0, "out": _ENC_LIBX264} if "-encoders" in cmd
            else {"code": 0, "out": "127.0.0.1"}
        ),
    )
    _write_secret("s" * 48)

    with client.websocket_connect(
        "/m7a-preview/ws264?token=" + pv.day_token("s" * 48)
    ) as ws:
        msg = ws.receive_text()
        payload = json.loads(msg)
        assert "ffmpeg" in payload["error"] and "Unknown encoder" in payload["error"]
        with pytest.raises(Exception) as ei:
            ws.receive_text()
    assert getattr(ei.value, "code", None) == 1013


def test_ws264_clean_shutdown_still_closes_1000(tmp_path, monkeypatch):
    """正常收尾（ffmpeg exit 0）仍按 1000 关闭，不改动既有正常路径。"""
    fake = _fake_ffmpeg(tmp_path, exit_code=0, stderr_text="")
    monkeypatch.setattr(cfg, "FFMPEG_BIN", str(fake))
    pv.reset_codec_cache()
    _stub_upstream(monkeypatch)
    monkeypatch.setattr(
        pv, "run_cmd",
        lambda cmd, timeout=120: (
            {"code": 0, "out": _ENC_LIBX264} if "-encoders" in cmd
            else {"code": 0, "out": "127.0.0.1"}
        ),
    )
    _write_secret("s" * 48)

    with client.websocket_connect(
        "/m7a-preview/ws264?token=" + pv.day_token("s" * 48)
    ) as ws:
        with pytest.raises(Exception) as ei:
            ws.receive_text()
    assert getattr(ei.value, "code", None) == 1000


# ===== Q-05 端到端：stderr 超过内核管道缓冲（64 KiB）时不得阻塞转码 =====

def _fake_ffmpeg_noisy(tmp: Path) -> Path:
    """假 ffmpeg：先往 stderr 灌 4 MiB（远超 64 KiB 管道缓冲与 asyncio 流控水位），
    再写 stdout。

    若 stderr 不被排空，进程会在写满管道时阻塞 → stdout 永远没有字节
    → 预览冻结（Q-05 的现象）。用 4 MiB 是为了确定性：只灌 200 KiB 时，
    内核管道（64 KiB）+ asyncio StreamReader 的读入缓冲可能刚好把数据吞下，
    即使不排空也偶发通过。
    """
    script = tmp / "fake-ffmpeg-noisy"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "for a in sys.argv[1:]:\n"
        "    if a == '-encoders':\n"
        f"        print({_ENC_LIBX264!r})\n"
        "        sys.exit(0)\n"
        "line = ('x' * 200 + '\\n').encode()\n"
        "for _ in range(20000):\n"
        "    sys.stderr.buffer.write(line)\n"
        "sys.stderr.buffer.flush()\n"
        "sys.stdout.buffer.write(b'\\x00\\x00\\x00\\x20ftypisom')\n"
        "sys.stdout.buffer.flush()\n"
        "sys.exit(0)\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


def test_ws264_stderr_does_not_block_pipeline(tmp_path, monkeypatch):
    """Q-05 回归：stderr 写满 64 KiB 管道后 ffmpeg 仍能产出 stdout（不排空则永久阻塞）。"""
    fake = _fake_ffmpeg_noisy(tmp_path)
    monkeypatch.setattr(cfg, "FFMPEG_BIN", str(fake))
    pv.reset_codec_cache()
    _stub_upstream(monkeypatch)
    monkeypatch.setattr(
        pv, "run_cmd",
        lambda cmd, timeout=120: (
            {"code": 0, "out": _ENC_LIBX264} if "-encoders" in cmd
            else {"code": 0, "out": "127.0.0.1"}
        ),
    )
    _write_secret("s" * 48)
    tok = pv.day_token("s" * 48)

    box: dict = {}

    def _run():
        try:
            with client.websocket_connect(f"/m7a-preview/ws264?token={tok}") as ws:
                box["data"] = ws.receive_bytes()
        except Exception as e:      # noqa: BLE001 - 断言里统一报告
            box["err"] = e

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(15)
    assert not t.is_alive(), "ffmpeg stderr 未被排空 → 子进程阻塞在写管道，stdout 无输出"
    assert "err" not in box, f"WS 异常：{box.get('err')}"
    assert b"ftyp" in box["data"]
