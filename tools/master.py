"""找主人工具 —— 让 bot 在任何对话里都能主动私聊主人。

发给主人时会带上「来自哪个群 / 谁」的来源上下文，并把这条主动消息写进主人私聊
会话，保证主人随后回复时上下文连贯、不和正在进行的对话打架。
"""

from config import MASTER_FEISHU, MASTER_QQ
from core import notices, timeline
from core.log import log
from core.runtime import get_bot, get_event
from core.session import get_session
from core.tools import tool


def _master_of(platform: str) -> str:
    """当前平台的主人 id（QQ/飞书…）。未配置返回空串。"""
    if platform == "feishu":
        return MASTER_FEISHU
    return MASTER_QQ


@tool(
    name="contact_master",
    description=(
        "需要找主人的时候调用：想向主人汇报情况、求助、转达群友的留言，"
        "或主人不在场但需要让他知道某事时。会把消息私聊发给主人。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "message": {
                "type": "string",
                "description": "要对主人说的话，用你平时的语气，把事情说清楚。",
            },
        },
        "required": ["message"],
    },
)
async def contact_master(message: str) -> str:
    ev = get_event()
    bot = get_bot()
    platform = ev.platform if ev is not None else "onebot"
    if bot is None or ev is None:
        # 后台活动没有当前消息：改用任一已连上、配置了主人的平台。
        route = notices.master_route()
        if route is None:
            return "[暂时联系不上主人：当前没有可用连接]"
        platform, bot = route
    master = _master_of(platform)
    if not master:
        return "[暂时联系不上主人：未配置主人账号]"
    master_key = f"{platform}_private_{master}"

    # 来源上下文，连同正文一起发给主人，避免主人摸不着头脑、后续对话错乱
    if ev is not None and ev.message_type == "group":
        ctx = f"（我正在群 {ev.group_id} 和 {ev.sender_display} 聊天时来找你的）"
    elif ev is not None and ev.session_key != master_key:
        ctx = f"（我正在和 {ev.sender_display} 私聊时来找你的）"
    else:
        ctx = ""

    sent = f"{ctx}\n{message}".strip() if ctx else message

    try:
        await timeline.track_send(
            ev, sent, bot.send_private_msg(master, sent), kind="message", target=master_key,
            confirmed=getattr(bot, "confirms_delivery", False),
        )
    except Exception as e:
        log.error(f"contact_master 发送失败: {e!r}")
        return f"[发送给主人失败: {type(e).__name__}]"

    # 把这条主动消息写进主人私聊会话（作为 bot 说过的话），让主人回复时上下文连贯。
    # 若当前正在处理的就是主人私聊，则跳过——那一轮自己会写回 assistant，避免重复/错序。
    if ev is None or ev.session_key != master_key:
        get_session(master_key).add_assistant(sent)

    log.info(f"[找主人] -> {master}: {sent[:80]}")
    return "已经把消息悄悄发给主人啦~"
