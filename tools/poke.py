"""QQ 戳一戳工具；由模型决定是否调用，发送场景取当前对话。"""

from adapters.onebot_errors import OnebotActionError
from core import runtime, timeline
from core.log import log
from core.tools import tool


def _valid_id(value: str) -> bool:
    return isinstance(value, str) and value.isascii() and value.isdecimal() and int(value) > 0


@tool(
    name="send_poke",
    description=(
        "在 QQ 戳一戳某个人，可用于自然互动或戳回对方，由你根据聊天需要决定是否使用。"
        "不传 user_id 默认戳本轮发言人/戳你的那个人；群聊中在当前群戳指定成员，"
        "私聊中向指定好友发送戳一戳。目标 QQ 号应从上下文获得，不要猜测。"
        "不必每次被戳都戳回，不要连续重复戳人；返回成功时已实际发送，无需再用 send_message 代发。"
        "超时或连接异常时结果未确认，不要立即重试，以免重复戳人。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "user_id": {
                "type": "string",
                "description": "目标 QQ 号；省略则使用本轮发言人的 QQ。群聊中目标须在当前群，私聊中须为好友。",
            },
        },
    },
)
async def send_poke(user_id: str = "") -> str:
    event, bot = runtime.get_event(), runtime.get_bot()
    if event is None or bot is None or event.platform != "onebot" or bot.platform != "onebot":
        return "[只能在 QQ 对话中发送戳一戳]"
    if not callable(getattr(bot, "send_poke", None)):
        return "[运行中的 QQ 适配器尚未加载戳一戳能力，请更新代码并完整重启 bot；仅 /reload 不会更新适配器]"
    if event.message_type not in {"group", "private"}:
        return "[当前会话不支持戳一戳]"
    if not isinstance(user_id, str):
        return "[user_id 必须是 QQ 号字符串]"
    uid = user_id.strip() or event.user_id
    if not _valid_id(uid):
        return "[目标 QQ 号必须是正整数字符串]"
    group_id = event.group_id if event.message_type == "group" else None
    if group_id is not None and not _valid_id(group_id):
        return "[当前群号无效，未发送戳一戳]"

    try:
        await timeline.track_send(
            event, f"戳一戳 QQ {uid}", bot.send_poke(uid, group_id=group_id), kind="poke",
            confirmed=getattr(bot, "confirms_delivery", False),
        )
    except OnebotActionError as exc:
        log.warning(f"[戳一戳] {exc}")
        return f"[戳一戳未确认成功：{exc}。不要宣称已戳到对方，也不要自动重复发送]"
    except (TimeoutError, ConnectionError, OSError) as exc:
        log.warning(f"[戳一戳] 发送结果未确认: {type(exc).__name__}")
        return "[戳一戳发送结果未确认，可能已经发送；不要宣称成功或立即重试]"
    except Exception as exc:
        log.warning(f"[戳一戳] 发送未确认成功: {type(exc).__name__}")
        return "[戳一戳未确认成功；不要宣称已戳到对方，也不要自动重复发送]"

    where = f"群 {group_id}" if group_id is not None else "私聊"
    log.info(f"[戳一戳] {where} -> {uid}")
    return f"已在{where}戳了戳 QQ {uid}。"
