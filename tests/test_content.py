import asyncio
from collections import OrderedDict, defaultdict
from copy import deepcopy
from dataclasses import replace
import json
import unittest
from unittest.mock import AsyncMock, patch

import config
import plugins  # noqa: F401 -- exercise actual dispatch order, including sticker capture
import tools  # noqa: F401
from core import coalesce, contacts, engagement, forwards, group_chat, images, llm
from core import memory, perms, plugin, session, sticker_learning, stickers, timeline
from core.bot import ImageInfo
from core.event import MessageEvent
from tests.test_replies import FakeBot


def event(text, kind="private", **kwargs):
    return MessageEvent(platform="onebot", message_type=kind, user_id="owner", group_id="g",
                        self_id="bot-id", message_id="incoming", sender_display="主人(owner)",
                        text=text, **kwargs)


class ContentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.bot = FakeBot()
        for p in (
            patch.object(perms, "MASTER_QQ", "owner"),
            patch.object(perms, "MASTER_FEISHU", "ou_owner"),
            patch.object(session, "sessions", defaultdict(session.Session)),
            patch.object(session, "_dirty", False),
            patch.object(timeline, "_data", {"version": 1, "experiences": [], "current_state": None}),
            patch.object(timeline, "_dirty", False),
            patch.object(timeline, "_writable", True),
            patch.object(config, "AGENT_CONTEXT_ENABLED", True),
            patch.object(engagement, "_last_reply_at", {}),
            patch.object(coalesce, "_pending", {}),
            patch.object(coalesce, "REPLY_DEBOUNCE", 0),
            patch.object(coalesce, "GROUP_IDLE_DEBOUNCE", 0),
            patch.object(group_chat, "AMBIENT_CHAT", False),
            patch.object(group_chat, "FOLLOWUP_CHAT", False),
            patch.object(images, "_registry", OrderedDict()),
            patch.object(forwards, "_registry", OrderedDict()),
            patch.object(memory, "_memories", [{"content": "全局记忆"}]),
            patch.object(stickers, "available_meanings", return_value="可用表情"),
            patch.object(contacts, "list_all", return_value="测试通讯录"),
            patch.object(llm, "_provider_cache", llm.OpenAICompatProvider()),
        ):
            self.enterContext(p)
        self.post = self.enterContext(patch.object(llm.OpenAICompatProvider, "_post", new_callable=AsyncMock))
        self.enqueue = self.enterContext(patch.object(sticker_learning, "enqueue"))

    async def inspect(self, command):
        self.bot.sent.clear()
        before = deepcopy(session.snapshot()), timeline.snapshot(), session._dirty, timeline._dirty
        await plugin.dispatch(command, self.bot)
        self.assertEqual((session.snapshot(), timeline.snapshot(), session._dirty, timeline._dirty), before)
        self.post.assert_not_awaited()
        self.enqueue.assert_not_called()
        self.assertEqual(coalesce._pending, {})
        self.assertTrue(self.bot.sent)
        parts = []
        for i, (channel, recipient, chunk) in enumerate(self.bot.sent, 1):
            self.assertEqual((channel, recipient), ("private", command.user_id))
            if len(self.bot.sent) > 1:
                label = f"〔{i}/{len(self.bot.sent)}〕\n"
                self.assertTrue(chunk.startswith(label))
                chunk = chunk[len(label):]
            parts.append(chunk)
        return "".join(parts)

    async def compare_to_chat(self, normal):
        shown = await self.inspect(replace(normal, text="/content " + normal.text))
        preview = json.loads(shown[shown.index("\n{") + 1:])
        self.post.return_value = {"choices": [{"finish_reason": "stop", "message": {
            "content": '{"messages":[]}' if normal.message_type == "group" else "回复",
        }}]}
        await plugin.dispatch(normal, self.bot)
        await asyncio.wait_for(coalesce._pending[normal.session_key].task, 2)
        self.post.assert_awaited_once()
        self.assertEqual(preview, self.post.await_args.args[0])
        return preview

    async def test_private_text_matches_post_including_trimmed_history_tool_round_and_scoped_state(self):
        normal = event("第一行\n第二行")
        s = session.get_session(normal.session_key)
        s.add_user("该被裁掉")
        s.add_user("上个问题")
        s.messages.extend([
            {"role": "assistant", "content": None, "tool_calls": [
                {"type": "function", "id": "t", "function": {"name": "example", "arguments": "{}"}},
            ]},
            {"role": "tool", "tool_call_id": "t", "content": "历史工具结果"},
        ])
        timeline.set_state(normal, "当前私聊状态")
        timeline.record(normal, "tool", "会被新经历挤出", "returned")
        timeline.record(normal, "tool", "可见经历", "returned")
        with patch.object(session, "MAX_HISTORY", 2), patch.object(config, "AGENT_EXPERIENCES_MAX", 2), \
             patch.object(llm, "LLM_ENABLE_TOOLS", True), patch.object(llm, "LLM_THINKING", True):
            payload = await self.compare_to_chat(normal)
        self.assertEqual([m["role"] for m in payload["messages"]], ["system", "user", "assistant", "tool", "user"])
        self.assertEqual(payload["messages"][-1]["content"], normal.text)
        self.assertIn("可见经历", payload["messages"][0]["content"])
        self.assertIn("当前私聊状态", payload["messages"][0]["content"])
        self.assertIn("主人(owner)", payload["messages"][0]["content"])
        self.assertNotIn("该被裁掉", str(payload))
        self.assertNotIn("会被新经历挤出", str(payload))
        self.assertIn("tools", payload)
        self.assertNotIn("reasoning_content", s.messages[1])

    async def test_ambient_preview_matches_fast_gate(self):
        with patch.object(group_chat, "AMBIENT_CHAT", True), patch.object(group_chat, "AMBIENT_GROUPS", ["g"]):
            payload = await self.compare_to_chat(event("猫猫在吗", "group"))
        self.assertEqual(payload["thinking"], {"type": "disabled"})
        self.assertNotIn("tools", payload)
        self.assertNotIn("全局记忆", str(payload))

    async def test_mention_sent_before_last_reply_previews_dedup_gate(self):
        s = session.get_session(event("", "group").session_key)
        # 朋友的 @ 仍待处理，且排在 BOT 回复之前（发送时 BOT 正在生成上一条回复）。
        s.add_group_event(replace(event("今天几号", "group", is_at_bot=True), user_id="friend", message_id="early"))
        s.add_group_reply("今天 2 号喵", None, "bot-msg")
        shown = await self.inspect(event("/content 谢谢", "group"))
        self.assertIn("快速查重", shown)
        preview = json.loads(shown[shown.index("\n{") + 1:])
        self.assertEqual(preview["thinking"], {"type": "disabled"})
        self.assertIn("上一条回复发出前发送", preview["messages"][0]["content"])
        rows = [json.loads(p["text"]) for p in preview["messages"][1]["content"][1:]]
        self.assertEqual([r["id"] for r in rows if r.get("先于BOT回复")], ["early"])

    async def test_group_matches_post_with_pending_messages_and_reply_to_bot(self):
        normal = event("接着说", "group", reply_to="bot-msg", at_list=["friend"])
        s = session.get_session(normal.session_key)
        s.add_group_reply("旧回答", None, "bot-msg")
        s.add_group_event(replace(normal, message_id="pending", user_id="friend", text="待处理", reply_to=None))
        timeline.set_state(event("私聊"), "不属于这个群")
        with patch.object(group_chat, "LLM_ENABLE_TOOLS", False):
            payload = await self.compare_to_chat(normal)
        self.assertNotIn("tools", payload)
        self.assertEqual(payload["response_format"], {"type": "json_object"})
        system = payload["messages"][0]["content"]
        for value in ["全局记忆", "可用表情", "测试通讯录", "直接互动：True"]:
            self.assertIn(value, system)
        self.assertNotIn("不属于这个群", system)
        records = [json.loads(p["text"]) for p in payload["messages"][1]["content"][1:]]
        self.assertEqual([r["范围"] for r in records], ["此前历史", "本轮新增", "本轮新增"])
        self.assertEqual((records[-1]["引用"], records[-1]["@"], records[-1]["正文"]), ("bot-msg", ["friend"], "接着说"))

    async def test_attachments_never_learn_register_fetch_or_expand_even_without_group_mention(self):
        image = ImageInfo(key="pic", fetch=AsyncMock())
        old = ImageInfo(key="old", fetch=AsyncMock())
        command = event("/content", "group", images=[old, image],
                        forwards=[forwards.ForwardInfo(id="f", messages=[{"text": "内层秘密"}])])
        with patch("plugins.stickers.AMBIENT_GROUPS", ["g"]), patch("plugins.stickers.STICKER_LEARN", True), \
             patch.object(images, "IMAGE_MAX_PER_REQUEST", 1):
            output = await self.inspect(command)
        self.assertIn("只作背景", output)
        self.assertIn(images.short_id(image), output)
        self.assertIn(f"[图片#{images.short_id(old)}]（本轮未附原图", output)
        self.assertIn("[合并转发#", output)
        self.assertNotIn("内层秘密", output)
        image.fetch.assert_not_awaited()
        old.fetch.assert_not_awaited()
        self.assertFalse(images._registry or forwards._registry or session.sessions)

    async def test_other_users_do_not_receive_or_trigger_context(self):
        for kind in ("group", "private"):
            for platform, user in (("onebot", "other"), ("feishu", "owner")):
                self.bot.sent.clear()
                command = replace(event("/content hello", kind), platform=platform, user_id=user)
                await plugin.dispatch(command, self.bot)
                self.assertEqual(self.bot.sent, [("reply", "只有主人可以查看完整上下文。")])
        self.assertFalse(session.sessions or coalesce._pending)
        self.post.assert_not_awaited()

    async def test_usage_and_nested_commands_do_not_execute(self):
        self.assertIn("用法", await self.inspect(event("/content")))
        for body in ("/clear", "/ignore 消息", "/restart", "/content 问题"):
            self.assertIn("未执行", await self.inspect(event("bot /content\n" + body)))
        self.assertFalse(session.sessions)

    async def test_expired_history_and_private_note_are_not_restored_or_cleared_by_preview(self):
        normal = event("新消息")
        s = session.get_session(normal.session_key)
        s.add_user("过期历史")
        timeline.set_state(normal, "过期状态")
        s.last_active = timeline._data["current_state"]["updated_at"] = 1
        payload = await self.compare_to_chat(normal)
        self.assertNotIn("过期", payload["messages"][0]["content"])
        self.assertEqual(len(payload["messages"]), 2)

    async def test_budgeted_group_preview_reaches_hypothetical_input_without_consuming_pending_batch(self):
        command = event("/content 末尾的消息", "group")
        s = session.get_session(command.session_key)
        for i in range(8):
            s.add_group_event(replace(command, message_id=str(i), text="长消息" * 120))
        with patch("core.group_context.GROUP_CONTEXT_CHARS", 2048):
            text = await self.inspect(command)
        self.assertIn("前面还有待处理批次", text)
        self.assertIn("末尾的消息", text)
        self.assertEqual(s.group_cursor, 0)
        self.assertEqual(len(s.group_events), 8)

    async def test_preview_during_generation_keeps_inflight_messages_out_of_next_batch(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def slow(payload):
            started.set()
            await release.wait()
            return {"choices": [{"message": {"content": '{"messages":[]}'}}]}

        self.post.side_effect = slow
        old = event("上一批", "group", is_at_bot=True)
        await plugin.dispatch(old, self.bot)
        task = coalesce._pending[old.session_key].task
        try:
            await asyncio.wait_for(started.wait(), 2)
            await plugin.dispatch(replace(old, text="/content 下一批", message_id="next", is_at_bot=False), self.bot)
            output = "".join(m[2].split("〕\n", 1)[-1] for m in self.bot.sent)
            preview = json.loads(output[output.index("\n{") + 1:])
            rows = [json.loads(p["text"]) for p in preview["messages"][1]["content"][1:]]
            self.assertEqual([(r["正文"], r["范围"]) for r in rows], [("上一批", "此前历史"), ("下一批", "本轮新增")])
            self.assertEqual(session.get_session(old.session_key).group_cursor, 0)
            self.post.assert_awaited_once()
        finally:
            release.set()
            await asyncio.wait_for(task, 2)
