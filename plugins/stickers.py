"""白名单群自动学习表情；保留 /meaning 与 /pending 兼容旧库，不主动询问人。"""

from config import AMBIENT_GROUPS, STICKER_LEARN
from core import sticker_learning, stickers
from core.bot import ImageSeg, MessagePart, TextSeg
from core.event import MessageEvent
from core.middleware import middleware
from core.perms import master_only
from core.plugin import MatchResult, on_command


@middleware(priority=50)
async def capture_stickers(event: MessageEvent, bot) -> bool:
    if (
        STICKER_LEARN
        and event.message_type == "group"
        and event.group_id in AMBIENT_GROUPS
        and event.has_image
        and event.self_id
        and event.user_id != event.self_id
    ):
        for image in event.images:
            sticker_learning.enqueue(image, event.group_id, event.text)
    return True


@on_command("meaning", scope="private", priority=10)
@master_only
async def set_meaning(event: MessageEvent, bot, match: MatchResult):
    parts = match.text.split(maxsplit=1)
    if len(parts) < 2:
        await bot.reply(event, "用法：/meaning <id> <含义>（可选的人工修正）")
        return
    short, meaning = parts
    if stickers.set_meaning(short, meaning):
        await bot.reply(event, f"记住啦：{short} = {meaning}")
    else:
        await bot.reply(event, f"没找到唯一匹配的表情喵，请使用完整 id：{short}")


@on_command("pending", scope="private", priority=10)
@master_only
async def list_pending(event: MessageEvent, bot, match: MatchResult):
    items = stickers.pending()
    if not items:
        await bot.reply(event, "没有尚未注释的旧表情记录喵~")
        return
    await bot.reply(event, f"旧库有 {len(items)} 条尚无含义的记录（无需人工回复）：")
    for item in items[:20]:
        parts: list[MessagePart] = []
        ref = stickers.path_of(item["key"]) or item.get("url")
        if ref:
            parts.append(ImageSeg(ref))
        status = ("自动判断不适合收藏，已保留原记录" if item.get("auto_review") == "skipped"
                  else "等待后台注释或上次识别未成功")
        parts.append(TextSeg(f"{stickers.display_id(item['key'])}：{status}"))
        await bot.reply(event, parts)
