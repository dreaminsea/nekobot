"""后台通知：把活动结果送回发起请求的会话，由原有群聊/私聊流程决定怎么说。

通知被包装成一条合成消息（`synthetic=True`）：群聊里发言人是固定的 NOTICE_USER，
真人账号不可能是这个 ID，提示词据此区分真假通知；私聊里沿用对方身份，正文标明来源。
"""

from __future__ import annotations

from core import perms, runtime
from core.event import MessageEvent
from core.log import log

NOTICE_USER = "agent-notice"
_ORIGIN_FIELDS = ("platform", "message_type", "user_id", "group_id", "message_id",
                  "session_key", "self_id", "sender_display")


def origin_of(event: MessageEvent) -> dict:
    return {key: str(getattr(event, key, "") or "") for key in _ORIGIN_FIELDS}


async def deliver(origin: dict, text: str) -> bool:
    """让原会话"收到"这条通知并照常决定回复；平台未连上时返回 False，不重试。"""
    from core.group_chat import handle_group_message
    from core.private_chat import handle_private_message

    bot = runtime.get_adapter_bot(origin.get("platform", ""))
    if bot is None:
        log.warning(f"[后台通知] {origin.get('platform')} 未连接，未送达 {origin.get('session_key')}")
        return False
    group = origin.get("message_type") == "group"
    event = MessageEvent(
        platform=origin["platform"], message_type="group" if group else "private",
        user_id=NOTICE_USER if group else origin["user_id"], group_id=origin.get("group_id") or "0",
        self_id=origin.get("self_id", ""), nickname="后台通知",
        sender_display="后台通知" if group else (origin.get("sender_display") or origin["user_id"]),
        text=f"【后台通知，不是{'群友' if group else '对方'}发的消息】{text}",
        is_at_bot=group, synthetic=True,
    )
    await (handle_group_message if group else handle_private_message)(event, bot)
    return True


def master_route():
    """按 QQ、飞书顺序返回已连上且配置了主人的 (platform, bot)；都没有返回 None。"""
    for platform in ("onebot", "feishu"):
        bot = runtime.get_adapter_bot(platform)
        if bot is not None and perms.master_id(platform):
            return platform, bot
    return None


async def notify_master(text: str) -> bool:
    """不经模型直接私聊主人，用于预算耗尽等运维提醒。"""
    route = master_route()
    if route is None:
        return False
    platform, bot = route
    try:
        await bot.send_private_msg(perms.master_id(platform), text)
    except Exception as exc:
        log.warning(f"[后台通知] 提醒主人失败: {type(exc).__name__}")
        return False
    return True
