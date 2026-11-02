"""主人的 /content 指令：在来源会话的副本上预演首轮请求，只私发预览。"""

from dataclasses import replace
import json

from core import group_chat
from core.event import MessageEvent
from core.group_context import build_group_snapshot
from core.images import preview_messages
from core.llm import initial_payload, private_context
from core.middleware import middleware
from core.perms import is_master
from core.plugin import matches_blocking_command
from core.prompts import private_system
from core.session import preview_session


def _preview(event: MessageEvent) -> str:
    command = event.command_text.split(maxsplit=1)
    if (command and command[0] in {"/ignore", "/content"}) or matches_blocking_command(event):
        return "这条输入会由指令处理，没有聊天模型上下文。未执行该指令。"

    session = preview_session(event.session_key)
    note = ""
    if event.message_type == "group":
        if session.group_inflight:
            session.finish_group_batch(session.group_inflight)
            note = "上一批正在处理，以下仅包含此刻已知的历史。\n"
        if not session.add_group_event(event, register_refs=False):
            return "这条群消息的 ID 已在历史中；正常处理会跳过重复消息。"
        # 只预览包含假设输入的那一批；不移动真实游标，也不虚构前几批的回答。
        snapshot = build_group_snapshot(session)
        while snapshot.through < session.group_seq:
            session.finish_group_batch(snapshot.through)
            snapshot = build_group_snapshot(session)
            note = "前面还有待处理批次；此处按当前历史预览，未假设它们的回复。\n"
        recent, ambient = group_chat.reply_signals(event)
        if not snapshot.directed and not recent:
            note += ("这批消息是否实际触发请求取决于主动插话抽样。\n"
                     if ambient and group_chat.AMBIENT_PROBABILITY > 0 else
                     "按当前群聊条件，这批消息只作背景，不会发起请求。\n")
        gate = not snapshot.directed and (recent or (ambient and group_chat.AMBIENT_PROBABILITY > 0))
        if gate:
            note += "首轮为快速接话判断，通过后才请求正式回复。\n"
        elif group_chat.wants_dedup(snapshot):
            gate = True
            note += "本批直接互动都早于上一条回复，首轮为快速查重，未判为已回应才请求正式回复。\n"
        messages, options = group_chat.group_context(event, session, snapshot, recent, ambient,
                                                     gate=gate, preview_event=event)
    else:
        session.add_user(event.llm_content(register_refs=False))
        messages, options = private_context(event.session_key, private_system(event.sender_display),
                                            session.messages, preview_event=event)

    payload = initial_payload(preview_messages(messages), **options)
    body = json.dumps(payload, ensure_ascii=False, indent=2)
    return (f"上下文预览 · {event.session_key}\n{note}"
            "以下为首轮请求预览；候选图片以编号显示，实际附图取决于下载结果和大小限制。\n" + body)


@middleware(priority=40)  # 通过访问控制/限流后，先于表情学习和普通聊天截获。
async def content_command(event: MessageEvent, bot) -> bool:
    parts = event.command_text.split(maxsplit=1)
    if not parts or parts[0] != "/content":
        return True
    if not is_master(event):
        await bot.reply(event, "只有主人可以查看完整上下文。")
        return False
    if len(parts) == 1 and not (event.images or event.forwards):
        output = "用法：/content <想预览的消息>，也可以附图或合并转发。"
    else:
        output = _preview(replace(event, text=parts[1] if len(parts) > 1 else ""))
    # 尽量在换行处分段；超长单行也完整保留，不走 AI 回复分隔协议。
    chunks = []
    start = 0
    while len(output) - start > 2500:
        end = output.rfind("\n", start + 1250, start + 2500) + 1 or start + 2500
        chunks.append(output[start:end])
        start = end
    chunks.append(output[start:])
    for index, chunk in enumerate(chunks, 1):
        label = f"〔{index}/{len(chunks)}〕\n" if len(chunks) > 1 else ""
        await bot.send_private_msg(event.user_id, label + chunk)
    return False
