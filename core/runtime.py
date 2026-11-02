"""运行时上下文 —— 让工具等深层代码能拿到当前 event / Bot。

每条消息在自己的 asyncio task 里处理，contextvars 天然按 task 隔离，
所以工具内 get_event()/get_bot() 拿到的就是当前这条消息的上下文。
"""

from __future__ import annotations

import contextvars
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.bot import Bot
    from core.event import MessageEvent

_current_event: contextvars.ContextVar["MessageEvent | None"] = contextvars.ContextVar(
    "current_event", default=None
)
_current_bot: contextvars.ContextVar["Bot | None"] = contextvars.ContextVar(
    "current_bot", default=None
)


def set_context(event: "MessageEvent", bot: "Bot") -> None:
    _current_event.set(event)
    _current_bot.set(bot)


def get_event() -> "MessageEvent | None":
    return _current_event.get()


def get_bot() -> "Bot | None":
    return _current_bot.get()


# ------------------------------------------------------------
#  在线适配器登记表 —— 跨平台拿别的平台的 bot（如飞书命令探测 QQ 链路）
# ------------------------------------------------------------

_adapters: dict = {}


def set_adapters(adapters: dict) -> None:
    """bot.py 建好各平台 adapter 后登记进来。"""
    global _adapters
    _adapters = adapters


def get_adapter_bot(platform: str) -> "Bot | None":
    """按平台名拿已连上的 bot；未注册/未连上返回 None。"""
    adapter = _adapters.get(platform)
    if adapter is None:
        return None
    try:
        return adapter.bot
    except RuntimeError:  # 连接尚未建立
        return None
