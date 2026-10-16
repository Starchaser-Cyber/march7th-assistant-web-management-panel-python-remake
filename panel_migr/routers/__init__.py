"""routers 包的包级导出。

main.py 顶层导入 `from routers import GET_HANDLERS, SPECIAL_GETS`
需要此文件把子模块里的名字提升到包级；否则 Python 3 无法把 routers 识别为包。
"""
from __future__ import annotations

from routers.readonly import GET_HANDLERS  # noqa: F401
from routers.get_write import SPECIAL_GETS  # noqa: F401
