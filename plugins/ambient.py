"""未显式触发的群消息也进入统一群聊消费者，是否接话由整批上下文决定。"""

from core.bot import Bot
from core.event import MessageEvent
from core.group_chat import handle_group_message
from core.plugin import MatchResult, on_message


@on_message(scope="group", to_me=False, priority=200)
async def ambient(event: MessageEvent, bot: Bot, match: MatchResult):
    await handle_group_message(event, bot)
