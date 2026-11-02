"""AI 文本回复的分条与发送。

模型用 ``REPLY_SEPARATOR`` 标出消息气泡边界；这里在平台发送前移除分隔符并逐条发送。
群聊的首条仍引用触发消息，后续气泡直接续发，避免每条都重复引用。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

from config import GROUP_REPLY_MAX_MESSAGES, REPLY_MESSAGE_DELAY, REPLY_SEPARATOR

if TYPE_CHECKING:
    from core.bot import Bot
    from core.event import MessageEvent


@dataclass(frozen=True)
class GroupReply:
    text: str
    reply_to: str | None = None


def parse_group_replies(text: str, allowed_ids: set[str]) -> list[GroupReply]:
    """先验证整个计划再发送；协议错误不能作为聊天正文发出去。"""
    data = json.loads(text)
    if not isinstance(data, dict) or set(data) != {"messages"}:
        raise ValueError("群聊输出必须是只含 messages 的对象")
    items = data["messages"]
    if not isinstance(items, list) or len(items) > GROUP_REPLY_MAX_MESSAGES:
        raise ValueError("messages 必须为有界数组")
    replies = []
    for item in items:
        if not isinstance(item, dict) or set(item) - {"text", "reply_to"}:
            raise ValueError("回复条目格式错误")
        body = item.get("text")
        target = item.get("reply_to")
        if not isinstance(body, str) or not body.strip() or REPLY_SEPARATOR in body:
            raise ValueError("每个条目必须包含一条非空完整正文")
        if target is not None and (not isinstance(target, str) or target not in allowed_ids):
            raise ValueError("reply_to 必须是当前上下文中的消息ID或null")
        replies.append(GroupReply(body.strip(), target))
    return replies


def split_reply(text: str) -> list[str]:
    """按独占一行的分隔符拆回复；代码块里的同名文本不拆。"""
    if not text or not text.strip():
        return []

    messages: list[str] = []
    current: list[str] = []
    fence: str | None = None

    for line in text.strip().splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            fence = None if fence == "```" else ("```" if fence is None else fence)
        elif stripped.startswith("~~~"):
            fence = None if fence == "~~~" else ("~~~" if fence is None else fence)

        if fence is None and stripped == REPLY_SEPARATOR:
            message = "\n".join(current).strip()
            if message:
                messages.append(message)
            current = []
            continue

        current.append(line)

    message = "\n".join(current).strip()
    if message:
        messages.append(message)
    return messages


async def send_ai_reply(
    bot: "Bot",
    event: "MessageEvent",
    text: str,
    *,
    reply_first: bool = True,
) -> int:
    """发送模型回复，返回实际发出的消息条数。

    ``reply_first`` 为真时首条走 ``bot.reply``（群聊会引用原消息）；其余消息始终直发。
    潜水主动接话传假，所有消息都不引用。
    """
    from core import timeline

    messages = split_reply(text)
    if not messages:
        return 0

    for index, message in enumerate(messages):
        if index > 0 and REPLY_MESSAGE_DELAY > 0:
            await asyncio.sleep(REPLY_MESSAGE_DELAY)

        if index == 0 and reply_first:
            send = bot.reply(event, message)
        elif event.message_type == "group":
            send = bot.send_group_msg(event.group_id, message)
        else:
            send = bot.send_private_msg(event.user_id, message)
        await timeline.track_send(event, message, send, confirmed=getattr(bot, "confirms_delivery", False))
    return len(messages)
