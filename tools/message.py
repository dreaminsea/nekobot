"""发消息工具 —— 让 bot 可以给任何人发私聊或群消息，可选指定从哪个平台发。

不同平台的 id 格式不同、互不相通：QQ/OneBot 用纯数字 QQ 号和群号；飞书用 open_id
（ou_xxx）和 chat_id（oc_xxx）。默认从当前对话所在平台发；要跨平台发请指定 platform。
明确拒绝发给主人：主人有专属的 contact_master 工具，不应走此通道。
"""

from core import runtime, timeline
from core.log import log
from core.perms import master_id
from core.tools import tool


@tool(
    name="send_message",
    description=(
        "给指定的人或群发一条消息。目标可以是 QQ 号/飞书 open_id（私聊）或群号/chat_id（群聊）。"
        "默认从当前对话平台发；要跨平台发请指定 platform（如群里有人在 QQ 让你飞书找人，就 platform='feishu'）。"
        "要给主人发消息必须用 contact_master 工具，不要用这个——这个会拒绝。"
        "对方正在私聊你时不要用，直接回复即可。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "user_id": {
                "type": "string",
                "description": "收消息的人 id(私聊) 或 群 id(群聊)。填对话里看到的 id 原样，别加前缀/后缀",
            },
            "message": {
                "type": "string",
                "description": "要发送的消息文本",
            },
            "is_group": {
                "type": "boolean",
                "description": "是不是发给群（true=群聊, false=私聊）",
            },
            "platform": {
                "type": "string",
                "description": "从哪个平台发：'onebot'(QQ) 或 'feishu'(飞书)。不填默认从当前对话平台发",
            },
        },
        "required": ["user_id", "message"],
    },
)
async def send_message(user_id: str, message: str, is_group: bool = False, platform: str = "") -> str:
    ev = runtime.get_event()
    plat = (platform or "").strip() or (ev.platform if ev is not None else "onebot")
    if plat not in ("onebot", "feishu"):
        return f"[未知平台 {plat!r}，可选 'onebot' 或 'feishu']"

    bot = runtime.get_adapter_bot(plat)
    if bot is None:
        return f"[暂时发不了消息：{plat} 适配器还没连上]"

    uid = user_id.strip()
    if not uid:
        return "[没给收件人 id]"

    # 拒绝发给主人：主人有专属的 contact_master，走那边带来源上下文
    mid = master_id(plat)
    if mid and uid == mid:
        return "[不能直接给主人发消息喵！要给主人发消息请调 contact_master 工具]"

    target = f"{plat}_{'group' if is_group else 'private'}_{uid}"
    try:
        if is_group:
            send = bot.send_group_msg(uid, message)
        else:
            send = bot.send_private_msg(uid, message)
        await timeline.track_send(
            ev, message, send, kind="message", target=target,
            confirmed=getattr(bot, "confirms_delivery", False),
        )
    except Exception as e:
        log.error(f"[代发] {plat}->{uid} 失败: {e!r}")
        return f"[发送失败: {type(e).__name__}]"

    where = f"{plat} 群 {uid}" if is_group else f"{plat} 私聊 {uid}"
    log.info(f"[代发] -> {where}: {message[:80]}")
    return f"已经把消息发到{where}啦~"
