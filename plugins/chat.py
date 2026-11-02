"""默认 LLM 聊天插件 —— 兜底处理所有指向 bot 的消息。

priority 较大，排在内置命令之后；to_me=True 使群聊需要 @bot 或触发前缀，私聊恒响应。
"""

from core.bot import Bot
from core.event import MessageEvent
from core.group_chat import handle_group_message
from core.plugin import MatchResult, on_message
from core.private_chat import handle_private_message


@on_message(priority=100, to_me=True)
async def chat(event: MessageEvent, bot: Bot, match: MatchResult):
    if not event.llm_text:  # 含 [表情包:含义]，让 bot 看懂表情
        return
    if event.message_type == "group":
        await handle_group_message(event, bot)
    else:
        await handle_private_message(event, bot)
