"""平台适配器抽象 —— 连接协议端、把平台原始事件归一成 `MessageEvent`。

`Adapter` 负责「连上平台 + 收事件」，把每条消息归一成平台无关的
`MessageEvent` 后交给上层 `on_event` 回调处理；`bot` 属性给出对应的发送能力。

新增平台只需：实现 `Adapter` + `Bot` 子类，注册到 `_ADAPTERS`。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Awaitable, Callable

from core.bot import Bot

if TYPE_CHECKING:  # 避免循环导入
    from core.event import MessageEvent

# 上层事件回调：拿到归一后的 MessageEvent 自己分发
EventCallback = Callable[["MessageEvent"], Awaitable[None]]


class Adapter(ABC):
    """平台适配器：维护连接、把平台事件转成 `MessageEvent` 喂给 `on_event`。"""

    platform: str

    @property
    @abstractmethod
    def bot(self) -> Bot:
        """当前连接对应的发送端。"""

    @abstractmethod
    async def run(self, on_event: EventCallback) -> None:
        """启动连接循环，直到被取消/异常。每条消息事件回调 `on_event`。"""


# ------------------------------------------------------------
#  注册表
# ------------------------------------------------------------

# platform 名 -> 工厂（延迟 import 避免循环导入）
_ADAPTERS: dict[str, Callable[[], Adapter]] = {}


def register_adapter(platform: str, factory: Callable[[], Adapter]) -> None:
    """注册一个平台适配器工厂。"""
    _ADAPTERS[platform] = factory


def create_adapter(platform: str) -> Adapter:
    """按平台名创建适配器实例。未注册则报错。"""
    if platform not in _ADAPTERS:
        raise ValueError(
            f"未知平台 {platform!r}，已注册: {list(_ADAPTERS) or '（无）'}"
        )
    return _ADAPTERS[platform]()
