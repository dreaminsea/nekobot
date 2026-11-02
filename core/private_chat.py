"""私聊入口：即时记历史，回复交给每会话的合批消费者。

插件和后台通知（core/notices.py）共用这一条路径。
"""

from __future__ import annotations

from core import coalesce, runtime, timeline
from core.bot import Bot
from core.event import MessageEvent
from core.llm import record_user, reply_turn
from core.log import log
from core.prompts import private_system
from core.replies import send_ai_reply


async def handle_private_message(event: MessageEvent, bot: Bot) -> None:
    text = event.llm_text  # 含 [表情包:含义]，让 bot 看懂表情
    if not text:
        return
    # 私聊：把对方身份写进系统提示，消息正文保持干净
    system_prompt = private_system(event.sender_display)
    log.info(f"[私聊] {event.sender_display}: {text[:80]}")
    # 即时记历史；回不回交给防抖合并（连发只回一次，且回时已看到整段消息）
    record_user(event.session_key, event.llm_content())
    timeline.received(event)

    async def _respond():
        runtime.set_context(event, bot)
        reply = await reply_turn(event.session_key, system_prompt)
        log.info(f"[回复] -> {reply[:80]}")
        await send_ai_reply(bot, event, reply)

    coalesce.feed(event.session_key, _respond, forced=True)
