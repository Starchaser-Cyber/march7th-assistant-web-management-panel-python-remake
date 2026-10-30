"""运行时可替换名字的统一入口（拆分后 monkeypatch 兼容层）。

为什么需要这一层：v1.24.4 及以前所有名字都在同一个模块里，测试用
`monkeypatch.setattr(U, "http_get", ...)` 打在模块对象上，内部调用自然看到新值。
v1.24.7 拆成包之后，`__init__.py` 的 re-export 只是**名字绑定**：子模块若写
`from .http import http_get`，函数对象在 import 时就固定下来，patch 包级名字对
内部调用完全无效（tests/test_v1221.py、test_v1243_security.py、
test_v1244_fix_update.py、test_update_health.py 会大面积失败）。

因此子模块一律不直接 import 兄弟模块的名字，而是在**调用点**通过本模块取包属性：

    from . import _rt
    _rt.http_get(url, timeout=5)      # 等价于 services.updateops.http_get(...)
    _rt.BACKUP_DIR                    # 等价于 services.updateops.BACKUP_DIR
    _rt.set("_LAST_BACKUP_ERR", msg)  # 写回包级可变状态

`__getattr__` 不做任何缓存，每次访问都回读包对象，所以 monkeypatch 立刻生效。
"""
from __future__ import annotations

import sys
from typing import Any

_PKG = __package__ or "services.updateops"


def __getattr__(name: str) -> Any:
    """按名字从 `services.updateops` 包对象取属性（PEP 562 模块级 __getattr__）。"""
    pkg = sys.modules.get(_PKG)
    if pkg is None:  # pragma: no cover - 只在包导入未完成时可能发生
        raise AttributeError(f"{_PKG} 尚未导入，无法解析 {name}")
    try:
        return getattr(pkg, name)
    except AttributeError:
        raise AttributeError(f"{_PKG} 没有名字 {name}") from None


def set(name: str, value: Any) -> None:  # noqa: A001 - 有意用内置名，调用处读作 _rt.set(...)
    """写回包级可变状态（测试直接读 `U._LAST_BACKUP_ERR` 必须能看到新值）。"""
    pkg = sys.modules.get(_PKG)
    if pkg is None:  # pragma: no cover
        raise RuntimeError(f"{_PKG} 尚未导入")
    setattr(pkg, name, value)
