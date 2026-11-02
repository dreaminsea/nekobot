"""平台无关的 Bot 抽象 —— 统一各平台（QQ/飞书…）的发送能力。

各平台 adapter 实现具体的 `Bot` 子类，把 `MessagePart` 翻译成自己的消息格式。
插件 / 工具统一对着抽象 `Bot` 编程，不感知底层协议。

消息统一用「富消息段」表示：一段纯文本 `TextSeg` 或一张图片 `ImageSeg`。
为方便起见，所有发送方法都接受 `str`（视作单个 `TextSeg`）或
`list[MessagePart]`（按顺序拼成一条消息）。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Awaitable, Callable, Optional, Union

if TYPE_CHECKING:  # 避免循环导入
    from core.event import MessageEvent


# ------------------------------------------------------------
#  富消息段
# ------------------------------------------------------------

@dataclass
class TextSeg:
    """一段纯文本。"""
    text: str


@dataclass
class ImageSeg:
    """一张图片。`file` 可以是本地绝对路径或远程 url，由 adapter 决定怎么发。"""
    file: str


# 一条消息 = 若干段的有序列表
MessagePart = Union[TextSeg, ImageSeg]


@dataclass
class ImageInfo:
    """入站图片的统一描述，供表情包知识库等使用。

    `key` 是该图的稳定标识（OneBot 用 file 名去扩展名；飞书用 image_key），
    用来去重 / 查含义。`url` 是可下载地址（可能为空），`summary` 是平台附带的摘要。

    有些平台（如飞书）的入站图片没有可直接下载的 url，只有内部 key，需调接口取
    二进制。这种情况由 adapter 在 `fetch` 里注入一个「取字节」闭包；`core.stickers`
    下载时若 `url` 为空就改用 `fetch`。`fetch` 不参与持久化。
    """
    key: str
    url: str = ""
    summary: str = ""
    fetch: Optional[Callable[[], Awaitable[bytes]]] = field(default=None, compare=False, repr=False)


# ------------------------------------------------------------
#  Bot 抽象基类
# ------------------------------------------------------------

class Bot(ABC):
    """封装一个平台连接的发送能力。

    子类需实现 `platform` 属性与下列发送方法。所有 id 统一为 `str`。
    """

    platform: str
    confirms_delivery = False  # 旧/第三方 adapter 未声明时，只能记作提交，不能确认成功。

    @abstractmethod
    async def reply(self, event: "MessageEvent", parts: str | list[MessagePart]) -> None:
        """按事件来源回复（群聊默认带引用，私聊直接发）。"""

    @abstractmethod
    async def send_group_msg(
        self,
        group_id: str,
        parts: str | list[MessagePart],
        reply_to: str | None = None,
    ) -> str | None:
        """往指定群发消息，可选引用某条消息；返回平台消息ID（如可用），失败抛异常。"""

    @abstractmethod
    async def send_private_msg(
        self, user_id: str, parts: str | list[MessagePart]
    ) -> None:
        """往指定用户私发消息。"""

    @abstractmethod
    async def send_image(self, event: "MessageEvent", path: str) -> None:
        """按事件来源发送一张本地图片（表情包）。

        各平台发图差异大（OneBot 直接 file:// 段；飞书要先 upload 拿 image_key），
        故单独留作抽象方法，不在 `reply` 里用 `ImageSeg` 凑合。
        """


# ------------------------------------------------------------
#  小工具
# ------------------------------------------------------------

def to_parts(parts: str | list[MessagePart]) -> list[MessagePart]:
    """`str` 归一成 `[TextSeg(str)]`，`list` 原样返回。"""
    if isinstance(parts, str):
        return [TextSeg(parts)]
    return parts
