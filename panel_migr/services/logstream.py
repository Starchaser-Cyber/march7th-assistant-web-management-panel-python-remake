"""日志流式增量（M4）——tail 各实例最新日志文件，把新增行推入事件总线。

只跟踪「当前最新」日志文件（log_files 按 mtime 倒序首项）：
- 首次跟踪从文件尾部开始（只推之后新增）；
- 指针变化（新任务生成新日志文件）→ 从头分批读，把新文件内容推上去；
- 文件变小（截断/轮转）→ 归零从头读；
- 每轮最多读 64KB，未完成行留在缓冲下次拼接。

线程安全：_state_lock 串行（runner 单线程调用 + 测试直调，双保险）。
"""
from __future__ import annotations

import os
import threading

from services import events
from services.logs import latest_log_path

_MAX_BATCH = 64 * 1024

_state_lock = threading.Lock()
_state: dict[str, dict] = {}


def reset_state() -> None:
    with _state_lock:
        _state.clear()


def tail_tick(inst: dict) -> int:
    """读一轮增量并发布；返回本轮发布的事件数（0/1）。"""
    sid = str(inst.get("id") or inst.get("container") or "")
    p = latest_log_path(inst)
    with _state_lock:
        st = _state.get(sid)
        if p is None or not os.path.isfile(p):
            if st is not None:
                _state.pop(sid, None)
            return 0
        try:
            size = os.path.getsize(p)
        except OSError:
            return 0

        if st is None:
            _state[sid] = {"path": p, "off": size, "buf": b""}
            return 0                        # 首次跟踪：从尾部起，只推新增
        if st["path"] != p:
            st.update(path=p, off=0, buf=b"")   # 新日志文件 → 全文分批推
        if size < st["off"]:
            st["off"] = 0                   # 截断/轮转
            st["buf"] = b""
        if size == st["off"]:
            return 0

        try:
            with open(p, "rb") as f:
                f.seek(st["off"])
                chunk = f.read(min(size - st["off"], _MAX_BATCH))
        except OSError:
            return 0
        if not chunk:
            return 0

        data = st["buf"] + chunk
        parts = data.split(b"\n")
        st["buf"] = parts.pop()             # 末段可能不完整，留到下轮
        st["off"] += len(chunk)

    lines = [ln.decode("utf-8", "replace").rstrip("\r")
             for ln in parts if ln.strip()]
    if not lines:
        return 0
    events.publish("log", {"file": os.path.basename(p), "lines": lines}, inst=sid)
    return 1
