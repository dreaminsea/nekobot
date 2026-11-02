import asyncio
import json
import unittest
from collections import OrderedDict, defaultdict
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import tools  # noqa: F401; load tool schemas as bot.py does at startup
import plugins  # noqa: F401; exercise the registered chat and ambient handlers
from adapters.onebot import OnebotAdapter, OnebotBot, _normalize
from core import coalesce, engagement, forwards, group_chat, llm, plugin, session


def incoming(message_type="group", message=None, **extra):
    return {
        "post_type": "message", "message_type": message_type, "self_id": 4,
        "user_id": 1, "group_id": 2, "message_id": 3,
        "message": message if message is not None else [
            {"type": "forward", "data": {"id": "outer"}},
        ],
        **extra,
    }


class ForwardDispatchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.provider = SimpleNamespace(chat=AsyncMock(return_value=llm.ChatResult('{"messages":[]}')))
        self.bot = OnebotBot(None)
        self.bot.send_group_msg = AsyncMock(return_value="bot-reply")
        self.bot.send_private_msg = AsyncMock()
        self.bot.get_forward_messages = AsyncMock()
        for patcher in (
            patch.object(session, "sessions", defaultdict(session.Session)),
            patch.object(forwards, "_registry", OrderedDict()),
            patch.object(coalesce, "_pending", {}),
            patch.object(coalesce, "REPLY_DEBOUNCE", 0),
            patch.object(coalesce, "GROUP_IDLE_DEBOUNCE", 0),
            patch.object(engagement, "_last_reply_at", {}),
            patch.object(group_chat, "get_provider", return_value=self.provider),
            patch.object(llm, "get_provider", return_value=self.provider),
            patch.object(group_chat, "AMBIENT_CHAT", False),
            patch.object(group_chat, "FOLLOWUP_CHAT", False),
            patch.object(group_chat, "LLM_ENABLE_TOOLS", True),
            patch.object(llm, "LLM_ENABLE_TOOLS", True),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addAsyncCleanup(self.cancel_pending)

    async def cancel_pending(self):
        tasks = [p.task for p in coalesce._pending.values() if p.task]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def drain(self):
        tasks = [p.task for p in coalesce._pending.values() if p.task]
        if tasks:
            await asyncio.wait_for(asyncio.gather(*tasks), 1)

    async def dispatch(self, raw):
        event = _normalize(raw)
        await plugin.dispatch(event, self.bot)
        await self.drain()
        return event

    async def test_forward_only_private_reaches_model_and_replies_without_auto_opening(self):
        self.provider.chat.return_value = llm.ChatResult("收到这条合并转发了")
        with self.assertLogs("qqbot", level="INFO") as logs:
            event = await self.dispatch(incoming("private"))
        self.assertIn("[合并转发] 已接收", "\n".join(logs.output))
        self.assertEqual(len(session.get_session(event.session_key).messages), 1)
        self.provider.chat.assert_awaited_once()
        request = self.provider.chat.await_args
        self.assertIn("[合并转发#", request.args[0][-1]["content"])
        self.assertIn("read_forward", {t["function"]["name"] for t in request.kwargs["tools"]})
        self.bot.send_private_msg.assert_awaited_once_with("1", "收到这条合并转发了")
        self.bot.get_forward_messages.assert_not_awaited()

    async def test_group_background_forward_is_visible_when_later_mentioned(self):
        with self.assertLogs("qqbot", level="INFO") as logs:
            event = await self.dispatch(incoming())
        self.assertIn("已写入上下文；仅记录背景", "\n".join(logs.output))
        history = session.get_session(event.session_key)
        self.assertEqual(len(history.messages), 1)
        self.assertIn("[合并转发#", history.messages[0]["content"])
        self.provider.chat.assert_not_awaited()
        self.bot.send_group_msg.assert_not_awaited()
        await self.dispatch(incoming(message_id=5, message=[
            {"type": "at", "data": {"qq": "4"}},
            {"type": "text", "data": {"text": "看看刚才的聊天记录"}},
        ]))
        self.provider.chat.assert_awaited_once()
        records = [json.loads(p["text"]) for p in self.provider.chat.await_args.args[0][-1]["content"][1:]]
        self.assertEqual([r["id"] for r in records], ["3", "5"])
        self.assertEqual(records[0]["范围"], "此前历史")
        self.assertIn("[合并转发#", records[0]["正文"])
        self.bot.get_forward_messages.assert_not_awaited()

    async def test_mention_plus_forward_without_text_can_trigger_group_reply(self):
        self.provider.chat.return_value = llm.ChatResult('{"messages":[{"text":"看到了","reply_to":"3"}]}')
        event = await self.dispatch(incoming(message=[
            {"type": "at", "data": {"qq": "4"}},
            {"type": "forward", "data": {"id": "outer"}},
        ]))
        self.assertEqual(event.text, "")
        self.assertTrue(event.to_me)
        self.provider.chat.assert_awaited_once()
        self.bot.send_group_msg.assert_awaited_once_with("2", "看到了", reply_to="3")
        self.bot.get_forward_messages.assert_not_awaited()

    async def test_ambient_group_considers_forward_and_model_can_skip(self):
        self.provider.chat.return_value = llm.ChatResult("SKIP")
        with patch.object(group_chat, "AMBIENT_CHAT", True), \
             patch.object(group_chat, "AMBIENT_GROUPS", ["2"]), \
             patch.object(group_chat, "AMBIENT_PROBABILITY", 1):
            await self.dispatch(incoming())
        self.provider.chat.assert_awaited_once()
        self.bot.send_group_msg.assert_not_awaited()
        self.bot.get_forward_messages.assert_not_awaited()

    async def test_bare_group_mention_goes_straight_to_reply(self):
        self.provider.chat.return_value = llm.ChatResult('{"messages":[{"text":"在呢"}]}')
        await self.dispatch(incoming(message=[{"type": "at", "data": {"qq": "4"}}]))
        self.provider.chat.assert_awaited_once()
        self.assertIn("tools", self.provider.chat.await_args.kwargs)
        self.bot.send_group_msg.assert_awaited_once_with("2", "在呢", reply_to=None)

    async def test_empty_or_ignored_messages_do_not_create_history_or_open_forward(self):
        for kind in ("private", "group"):
            for message in ([], [{"type": "at", "data": {"qq": "9"}}], [
                {"type": "text", "data": {"text": "/ignore "}},
                {"type": "forward", "data": {"id": "ignored"}},
            ]):
                await self.dispatch(incoming(kind, message=message))
        self.assertEqual(dict(session.sessions), {})
        self.assertEqual(len(forwards._registry), 0)
        self.provider.chat.assert_not_awaited()
        self.bot.get_forward_messages.assert_not_awaited()

    async def test_websocket_forward_runs_through_dispatch_to_private_context(self):
        self.provider.chat.return_value = llm.ChatResult("收到")
        delivered = asyncio.Event()
        adapter = OnebotAdapter()

        class Socket:
            def __init__(self):
                self.frames = asyncio.Queue()
                self.frames.put_nowait(json.dumps(incoming("private")))
                self.send = AsyncMock(side_effect=self.ack)

            async def ack(self, frame):
                request = json.loads(frame)
                await self.frames.put(json.dumps({
                    "echo": request["echo"], "status": "ok", "retcode": 0,
                    "data": {"message_id": 4},
                }))

            def __aiter__(self):
                return self

            async def __anext__(self):
                return await self.frames.get()

        socket = Socket()
        connection = MagicMock()
        connection.__aenter__ = AsyncMock(return_value=socket)
        connection.__aexit__ = AsyncMock(return_value=False)

        async def on_event(event):
            await plugin.dispatch(event, adapter.bot)
            delivered.set()

        with patch("adapters.onebot.websockets.connect", return_value=connection):
            task = asyncio.create_task(adapter.run(on_event))
            try:
                await asyncio.wait_for(delivered.wait(), 1)
                await self.drain()
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.provider.chat.assert_awaited_once()
        socket.send.assert_awaited_once()
        sent = json.loads(socket.send.await_args.args[0])
        self.assertEqual(sent["action"], "send_private_msg")
        self.assertEqual(sent["params"]["user_id"], 1)
