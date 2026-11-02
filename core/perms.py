"""命令权限 —— 主人判定 + master_only 装饰器。

把分散在各插件里的「是不是主人」判定统一到这里；`master_only` 装饰命令处理器，非主人
调用时**也给一条反馈**（而不是静默 return），免得命令调了没动静摸不着头脑。
"""

from __future__ import annotations

import functools
from typing import TYPE_CHECKING

from config import MASTER_FEISHU, MASTER_QQ

if TYPE_CHECKING:
    from core.event import MessageEvent


def master_id(platform: str) -> str:
    """该平台的主人 id（飞书 open_id / QQ 号）。未配置返回空串。"""
    return MASTER_FEISHU if platform == "feishu" else MASTER_QQ


def is_master(event: "MessageEvent") -> bool:
    mid = master_id(event.platform)
    return bool(mid) and event.user_id == mid


def master_only(func):
    """命令处理器装饰器：限主人用；非主人调用回一条提示（含其 id，便于配置）。"""

    @functools.wraps(func)
    async def wrapper(event: "MessageEvent", bot, match):
        if not is_master(event):
            await bot.reply(event, f"这个命令只有主人能用喵~（你的 id：{event.user_id}）")
            return
        return await func(event, bot, match)

    return wrapper
