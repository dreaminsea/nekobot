"""内置命令插件。"""

from core.bot import Bot
from core.event import MessageEvent
from core.plugin import MatchResult, on_command
from core.session import get_session


@on_command("clear", "重置", "清空对话", priority=10)
async def clear_session(event: MessageEvent, bot: Bot, match: MatchResult):
    get_session(event.session_key).clear()
    await bot.reply(event, "对话已清空~")
