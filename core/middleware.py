"""中间件 / 钩子链 —— 在插件分发前统一执行。

用法::

    from core.middleware import middleware

    @middleware(priority=10)
    async def block_blacklist(event, bot) -> bool:
        if event.user_id in BLACKLIST:
            return False   # 返回 False 拦截，终止后续中间件与插件分发
        return True

返回 None 视为 True（放行）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Awaitable, Callable

if TYPE_CHECKING:
    from core.bot import Bot
    from core.event import MessageEvent

Middleware = Callable[["MessageEvent", "Bot"], Awaitable[bool | None]]

# (priority, middleware)，priority 升序执行
_middlewares: list[tuple[int, Middleware]] = []


def middleware(priority: int = 100):
    def deco(func: Middleware) -> Middleware:
        _middlewares.append((priority, func))
        _middlewares.sort(key=lambda x: x[0])
        return func

    return deco


async def run_middlewares(event: "MessageEvent", bot: "Bot") -> bool:
    """依次执行中间件，任一返回 False 即短路。全部放行返回 True。"""
    for _, mw in _middlewares:
        result = await mw(event, bot)
        if result is False:
            return False
    return True
