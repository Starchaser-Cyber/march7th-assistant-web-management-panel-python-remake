"""路由注册：只读 GET（M1）+ 写型 GET/下载/key 回调（M2）+ POST（M2）+ 预览（M3）+ 事件化（M4）。"""
from routers.readonly import GET_HANDLERS as READONLY_GET_HANDLERS
from routers.get_write import WRITE_GET_HANDLERS, SPECIAL_GETS
from routers.preview import PREVIEW_GET_HANDLERS
from routers.events import EVENTS_GET_HANDLERS

# ajax 名 → 同步处理器（返回 Response 或 dict）
GET_HANDLERS = {
    **READONLY_GET_HANDLERS,
    **WRITE_GET_HANDLERS,
    **PREVIEW_GET_HANDLERS,
    **EVENTS_GET_HANDLERS,
}

__all__ = [
    "GET_HANDLERS", "SPECIAL_GETS",
    "READONLY_GET_HANDLERS", "WRITE_GET_HANDLERS", "PREVIEW_GET_HANDLERS",
    "EVENTS_GET_HANDLERS",
]
