"""群聊入口：消息合批、快速接话判断、正式回复与记账。"""

from __future__ import annotations

import asyncio
import random
import time

from config import (
    AMBIENT_CHAT, AMBIENT_GROUPS, AMBIENT_PROBABILITY, AMBIENT_REPLY_MODEL,
    FOLLOWUP_CHAT, FOLLOWUP_WINDOW, GROUP_AMBIENT_COOLDOWN, GROUP_CANCEL_PHRASES,
    GROUP_DEDUP_GATE, GROUP_DEDUP_PROMPT, GROUP_GATE_PROMPT, GROUP_GATE_TIMEOUT,
    GROUP_REASONING_EFFORT, LLM_ENABLE_TOOLS, REPLY_MESSAGE_DELAY,
)
from core import coalesce, engagement, runtime, timeline
from core.bot import Bot
from core.event import MessageEvent
from core.group_context import GroupSnapshot, build_group_snapshot
from core.llm import get_provider
from core.log import log
from core.prompts import group_reply_system, identity_hint
from core.replies import parse_group_replies
from core.session import Session, get_session, mark_dirty
from core.tools import get_tool_schemas, has_tools


def _is_current(session: Session, snapshot: GroupSnapshot) -> bool:
    if session.generation != snapshot.generation:
        return False
    # 新来的明确停止要求使在途回答失效；普通聊天不取消，避免刷屏时永远发不出去。
    for message in session.messages:
        meta = message.get("_group", {})
        if meta.get("seq", 0) <= snapshot.through:
            continue
        if not (meta.get("directed") or meta.get("user_id") in snapshot.user_ids):
            continue
        body = message.get("content", "")
        if isinstance(body, list):
            body = "".join(p.get("text", "") for p in body if p.get("type") == "text")
        if body.strip().strip("。！!~～") in GROUP_CANCEL_PHRASES:
            return False
    return True


def _remember_tools(session: Session, messages: list[dict]) -> None:
    """工具协议只用于当前调用；历史只保留实际执行结果，避免遗忘已发生的操作。"""
    names = {}
    for message in messages:
        for call in message.get("tool_calls", []):
            names[call.get("id")] = call.get("function", {}).get("name", "工具")
        if message.get("role") == "tool":
            name = names.get(message.get("tool_call_id"), "工具")
            session.messages.append({
                "role": "tool", "content": f"{name}: {message.get('content', '')}",
                "_group": {"sender": "工具执行记录", "time": time.time()},
            })
    session._trim()
    mark_dirty()


def reply_signals(event: MessageEvent) -> tuple[bool, bool]:
    key = event.session_key
    recent = FOLLOWUP_CHAT and engagement.in_window(key, FOLLOWUP_WINDOW)
    ambient_now = (AMBIENT_CHAT and event.group_id in AMBIENT_GROUPS
                   and not engagement.in_window(key, GROUP_AMBIENT_COOLDOWN))
    return recent, ambient_now


def wants_dedup(snapshot: GroupSnapshot) -> bool:
    """直接互动都发生在上一条回复生成期间时，先快速查重，避免同一件事再答一遍。"""
    return GROUP_DEDUP_GATE and snapshot.directed and snapshot.directed_overlap


def group_context(event: MessageEvent, session: Session, snapshot: GroupSnapshot, recent: bool, ambient_now: bool,
                  *, gate: bool = False, preview_event=None):
    policy = (
        f"本批包含直接互动：{snapshot.directed}。最近有过回复：{recent}。"
        f"本次允许主动插话：{ambient_now}。不允许主动插话时，只回应对你的直接互动或真正跟进。"
    )
    if gate:
        # 时间线文本已有图片编号、表情含义和引用关系；只有正式回复才下载原图。
        # 直接互动只会走查重判断；潜水/跟进走接话判断。固定规则在前，便于前缀缓存。
        parts = [p for p in snapshot.messages[0]["content"] if p["type"] == "text"]
        rule = GROUP_DEDUP_PROMPT if snapshot.directed else GROUP_GATE_PROMPT
        return [
            {"role": "system", "content": "\n".join((identity_hint(event.self_id), rule, policy))},
            {"role": "user", "content": parts},
        ], {"model": AMBIENT_REPLY_MODEL, "thinking": False, "tools": None, "max_tokens": 8}
    system = group_reply_system(event.self_id, policy, session_key=event.session_key,
                                history=session.messages, preview_event=preview_event)
    tools = get_tool_schemas() if LLM_ENABLE_TOOLS and has_tools() else None
    return [{"role": "system", "content": system}, *snapshot.messages], {
        "tools": tools, "model": AMBIENT_REPLY_MODEL, "response_format": {"type": "json_object"},
        "reasoning_effort": GROUP_REASONING_EFFORT,
    }


async def _gate(event: MessageEvent, session: Session, snapshot: GroupSnapshot,
                recent: bool, ambient_now: bool, *, interruptible: bool) -> bool | None:
    """快速判断：True 接话，False 跳过，None 失败、超时、输出不合法或被打断。

    潜水/跟进判断可被同群新来的直接互动打断；查重判断不可打断，否则会把直接提问当成已处理。
    """
    messages, options = group_context(event, session, snapshot, recent, ambient_now, gate=True)
    task = asyncio.create_task(get_provider().chat(messages, **options))
    if interruptible:
        session.group_gate = task
    try:
        async with asyncio.timeout(GROUP_GATE_TIMEOUT):
            result = await task
        if interruptible and session.group_gate is not task:
            return None  # 即使判断刚好返回，已收到的明确提问也优先。
        word = result.text.strip().upper() if result.ok else ""
        return True if word == "REPLY" else False if word == "SKIP" else None
    except TimeoutError:
        log.info(f"[群聊 {event.group_id}] 快速判断超时")
        return None
    except Exception as exc:
        log.warning(f"[群聊 {event.group_id}] 快速判断异常: {type(exc).__name__}")
        return None
    except asyncio.CancelledError:
        if asyncio.current_task().cancelling() or session.group_gate is task:
            raise  # 进程退出时继续取消外层消费者
        return None  # 同群直接互动或清空会话只打断快速判断
    finally:
        if session.group_gate is task:
            session.group_gate = None


def _pending_directed(session: Session) -> bool:
    return any(m.get("_group", {}).get("directed") for m in session.messages
               if m.get("_group", {}).get("seq", 0) > session.group_cursor)


async def handle_group_message(event: MessageEvent, bot: Bot) -> None:
    if not event.llm_text or event.user_id == event.self_id:
        return
    key = event.session_key
    session = get_session(key)
    if not session.add_group_event(event):
        return
    timeline.received(event)
    directed = session.messages[-1]["_group"]["directed"]
    if directed:
        session.cancel_group_gate()
    ambient_allowed = AMBIENT_CHAT and event.group_id in AMBIENT_GROUPS
    followup = FOLLOWUP_CHAT and engagement.in_window(key, FOLLOWUP_WINDOW)
    should_consider_reply = directed or ambient_allowed or followup or coalesce.active(key)
    if event.forwards:
        decision = "参与回复判定" if should_consider_reply else "仅记录背景（未满足回复触发条件）"
        log.info(f"[合并转发] 群 {event.group_id} 消息 {event.message_id or '-'} 已写入上下文；{decision}")
    if not should_consider_reply:
        session.finish_group_batch(session.group_seq)
        return

    async def respond():
        async with session.lock:
            snapshot = build_group_snapshot(session)
            if snapshot is None:
                return
            # 消费者/闭包可能由更早消息创建；权限、附件、工具来源必须绑定当前批次。
            trigger = session.group_events[snapshot.trigger_seq]
            runtime.set_context(trigger, bot)
            session.group_inflight = snapshot.through
            try:
                recent, ambient_now = reply_signals(trigger)
                if not snapshot.directed:
                    if not recent and (not ambient_now or random.random() >= AMBIENT_PROBABILITY):
                        return
                    if not await _gate(trigger, session, snapshot, recent, ambient_now, interruptible=True):
                        return
                elif wants_dedup(snapshot):
                    # 只有明确判为 SKIP 才跳过；失败或超时照常回答。
                    if await _gate(trigger, session, snapshot, recent, ambient_now, interruptible=False) is False:
                        log.info(f"[群聊 {event.group_id}] 本批直接互动早于上条回复且已被回应，跳过")
                        return
                if not _is_current(session, snapshot):
                    return
                messages, options = group_context(trigger, session, snapshot, recent, ambient_now)
                result = await get_provider().chat(
                    messages, **options, is_current=lambda: _is_current(session, snapshot),
                )
                if session.generation != snapshot.generation:
                    return
                _remember_tools(session, result.messages)
                if not result.ok or not _is_current(session, snapshot):
                    log.info(f"[群聊 {event.group_id}] 本批生成失败或已失效，不发正文")
                    return
                try:
                    replies = parse_group_replies(result.text, snapshot.reply_ids)
                except (ValueError, TypeError):
                    log.warning(f"[群聊 {event.group_id}] 回复协议不合法，不发送")
                    return
                sent = 0
                for i, reply in enumerate(replies):
                    if i and REPLY_MESSAGE_DELAY > 0:
                        await asyncio.sleep(REPLY_MESSAGE_DELAY)
                    if not _is_current(session, snapshot):
                        break
                    mid = await timeline.track_send(
                        trigger, reply.text, bot.send_group_msg(event.group_id, reply.text, reply_to=reply.reply_to),
                        confirmed=getattr(bot, "confirms_delivery", False),
                    )
                    sent += 1
                    if session.generation == snapshot.generation:
                        session.add_group_reply(reply.text, reply.reply_to, mid)
                        engagement.mark(key)
                log.info(f"[群聊 {event.group_id}] 已发送 {sent} 条（计划 {len(replies)} 条）")
            finally:
                if session.generation == snapshot.generation:
                    # skip、失败和部分发送都消费该批，不自动重试已发生的工具/发送操作。
                    session.finish_group_batch(snapshot.through)
                    session.group_inflight = 0
                if session.group_seq > session.group_cursor and not coalesce.queued(key):
                    coalesce.feed(key, respond, forced=_pending_directed(session))

    coalesce.feed(key, respond, forced=directed, sender=event.user_id)
