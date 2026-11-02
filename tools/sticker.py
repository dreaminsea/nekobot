"""表情包工具 —— 让 AI 会发表情、能查能删。

发送/删除作用于全局表情库(core/stickers.py)；发送目标取当前会话(core.runtime)。
"""

from core import stickers, timeline
from core.log import log
from core.runtime import get_bot, get_event
from core.tools import tool

@tool(
    description=(
        "发一个表情包来回应。传含义/心情、标签或图片 id（如『无语』『开心』『狗头』），"
        "会根据已收藏表情的含义、标签和使用场景注释匹配。没有合适的就会告诉你。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "meaning": {"type": "string", "description": "想要的表情含义、标签、使用场景或图片 id。"},
        },
        "required": ["meaning"],
    },
)
async def send_sticker(meaning: str) -> str:
    bot = get_bot()
    event = get_event()
    if bot is None or event is None:
        return "[现在没法发表情]"
    item = stickers.find(meaning)
    if not item or not item.get("path"):
        return f"暂时没有「{meaning}」相关的表情包喵~"
    try:
        await timeline.track_send(
            event, f"发送表情：{item['meaning']}", bot.send_image(event, item["path"]), kind="sticker",
            confirmed=getattr(bot, "confirms_delivery", False),
        )
    except Exception as e:
        log.warning(f"发表情失败: {e!r}")
        return f"[发表情失败: {type(e).__name__}]"
    return f"（发了一个「{item['meaning']}」的表情包）"


@tool(description="查看可发送表情包的 id、含义、标签和使用场景注释。")
async def list_stickers() -> str:
    items = stickers.known_list()
    if not items:
        return "还没学会任何表情包喵~"
    return "\n".join(stickers.describe_entry(s) for s in items)


@tool(
    description="删掉存下来的表情/图片：按含义、标签、注释或 id 匹配删除。",
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "要删除的表情含义或 id。"},
        },
        "required": ["query"],
    },
)
async def forget_sticker(query: str) -> str:
    n = stickers.forget(query)
    log.info(f"[表情包-] 删除含「{query[:30]}」的 {n} 个")
    return f"删掉了 {n} 个相关的图喵~" if n else "没找到相关的图喵~"
