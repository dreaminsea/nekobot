import asyncio
from collections import defaultdict
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import config
from adapters.onebot import OnebotBot
from core import coalesce, group_chat, llm, live_backup, perms, plugin, runtime, session, storage, timeline
from core import tools as tool_registry
from core.bot import ImageInfo
from core.event import MessageEvent
from core.forwards import ForwardInfo
from core.plugin import MatchResult
from core.replies import send_ai_reply
from plugins import admin, state
from tests.test_replies import FakeBot
from tools import message, poke


def event(kind="private", user="1", mid="m1", **kwargs):
    return MessageEvent(platform="onebot", message_type=kind, user_id=user, group_id="2",
                        self_id="99", message_id=mid, text="hello", **kwargs)


class TimelineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        for p in (
            patch.object(storage, "DATA_DIR", self.temp.name),
            patch.object(timeline, "_data", {"version": 1, "experiences": [], "current_state": None}),
            patch.object(timeline, "_dirty", False),
            patch.object(timeline, "_writable", True),
            patch.object(config, "AGENT_CONTEXT_ENABLED", True),
            patch.object(config, "AGENT_EXPERIENCES_MAX", 500),
            patch.object(config, "AGENT_CONTEXT_CHARS", 2400),
            patch.object(config, "SESSION_PERSIST", True),
            patch.object(session, "sessions", defaultdict(session.Session)),
            patch.object(coalesce, "_pending", {}),
            patch.object(coalesce, "REPLY_DEBOUNCE", 0),
            patch.object(coalesce, "GROUP_IDLE_DEBOUNCE", 0),
            patch.object(group_chat, "AMBIENT_CHAT", False),
            patch.object(group_chat, "FOLLOWUP_CHAT", False),
            patch.object(perms, "MASTER_QQ", "1"),
        ):
            p.start()
            self.addCleanup(p.stop)
        self.ev = event()
        self.bot = FakeBot()
        self.bot.confirms_delivery = True
        runtime.set_context(self.ev, self.bot)
        self.addCleanup(runtime.set_context, None, None)
        self.addAsyncCleanup(self.drain)

    async def drain(self):
        tasks = [p.task for p in coalesce._pending.values() if p.task]
        if tasks:
            await asyncio.wait_for(asyncio.gather(*tasks), 2)

    def rows(self):
        return timeline.snapshot()["experiences"]

    def test_record_scope_dedup_and_attachment_references_only(self):
        self.ev.images = [ImageInfo("test-image")]
        self.ev.forwards = [ForwardInfo(id="outer", messages=[{"text": "nested secret"}])]
        timeline.received(self.ev)
        timeline.received(self.ev)
        timeline.received(event(user="99", mid="echo"))
        self.assertEqual(len(self.rows()), 1)
        record = self.rows()[0]
        self.assertIn("[图片#", record["text"])
        self.assertIn("[合并转发#", record["text"])
        self.assertNotIn("nested secret", json.dumps(record))
        self.assertEqual(record["actor"], "1")
        self.assertEqual(record["message_id"], "m1")
        self.assertEqual(record["visible_to"], [self.ev.session_key])

    async def test_ignore_and_middleware_rejection_do_not_record(self):
        self.ev.text = "/ignore private secret"
        await plugin.dispatch(self.ev, self.bot)
        self.assertEqual(self.rows(), [])
        self.assertNotIn("llm_text", self.ev.__dict__)
        with patch.object(plugin, "run_middlewares", AsyncMock(return_value=False)):
            await plugin.dispatch(event(), self.bot)
        self.assertEqual(self.rows(), [])

    @patch.object(config, "AGENT_SHARE_EXPERIENCES", False)
    def test_no_private_group_or_platform_leak_and_explicit_shared_state(self):
        timeline.record(self.ev, "received", "private secret", "received")
        timeline.set_state(self.ev, "private state")
        other = event("group")
        for key in (other.session_key, "feishu_private_1", "onebot_private_3"):
            prompt = timeline.as_prompt(key)
            self.assertNotIn("private secret", prompt)
            self.assertNotIn("private state", prompt)
        timeline.set_state(self.ev, "public state", shared=True)
        public = timeline.as_prompt(other.session_key)
        self.assertIn("public state", public)
        self.assertNotIn("onebot_private_1", public)
        self.assertNotIn("private secret", public)

    @patch.object(config, "AGENT_SHARE_EXPERIENCES", False)
    async def test_cross_session_send_only_shares_delivered_content(self):
        target = "onebot_group_42"
        await timeline.track_send(self.ev, "delivered", AsyncMock(return_value="m2")(),
                                  target=target, confirmed=True)
        prompt = timeline.as_prompt(target)
        self.assertIn("delivered", prompt)
        self.assertNotIn(self.ev.session_key, prompt)
        self.assertNotIn('"trigger_id"', prompt)
        with self.assertRaises(TimeoutError):
            await timeline.track_send(self.ev, "failed secret", AsyncMock(side_effect=TimeoutError)(),
                                      target=target, confirmed=True)
        self.assertNotIn("failed secret", timeline.as_prompt(target))
        self.assertIn("unconfirmed", timeline.as_prompt(self.ev.session_key))

    async def test_partial_reply_and_cancellation_do_not_claim_success(self):
        bot = SimpleNamespace(confirms_delivery=True, reply=AsyncMock(),
                              send_private_msg=AsyncMock(side_effect=TimeoutError))
        with patch("core.replies.REPLY_MESSAGE_DELAY", 0), self.assertRaises(TimeoutError):
            await send_ai_reply(bot, self.ev, f"first\n{config.REPLY_SEPARATOR}\nsecond")
        done = [r["text"] for r in self.rows() if r["outcome"] == "confirmed"]
        self.assertEqual(done, ["first"])
        self.assertEqual(self.rows()[-1]["outcome"], "unconfirmed")
        with self.assertRaises(asyncio.CancelledError):
            await timeline.track_send(self.ev, "cancelled", AsyncMock(side_effect=asyncio.CancelledError)(), confirmed=True)
        self.assertEqual(self.rows()[-1]["outcome"], "unconfirmed")

    async def test_receipt_is_not_recorded_until_await_finishes(self):
        pending = asyncio.get_running_loop().create_future()
        task = asyncio.create_task(timeline.track_send(self.ev, "waiting for ack", pending, confirmed=True))
        await asyncio.sleep(0)
        self.assertEqual([r["outcome"] for r in self.rows()], ["attempted"])
        pending.set_result("sent-id")
        await task
        self.assertEqual(self.rows()[-1]["outcome"], "confirmed")
        self.assertEqual(self.rows()[-1]["message_id"], "sent-id")

    async def test_old_adapter_still_sends_without_claiming_confirmation(self):
        bot = FakeBot()  # 旧接口没有声明等待平台回执。
        await send_ai_reply(bot, self.ev, "old adapter")
        self.assertEqual(bot.sent, [("reply", "old adapter")])
        self.assertEqual(self.rows()[-1]["outcome"], "unconfirmed")

    async def test_send_message_and_poke_outcomes(self):
        bot = OnebotBot(None)
        bot.call_action_resp = AsyncMock(return_value={"status": "ok", "retcode": 0, "data": {"message_id": 9}})
        runtime.set_context(self.ev, bot)
        with patch.object(runtime, "get_adapter_bot", return_value=bot), patch.object(message, "master_id", return_value=""):
            self.assertIn("已经", await message.send_message("3", "sent to 3"))
        self.assertIn("sent to 3", timeline.as_prompt("onebot_private_3"))
        await poke.send_poke("4")
        self.assertEqual(self.rows()[-1]["kind"], "poke")
        self.assertEqual(self.rows()[-1]["outcome"], "confirmed")
        bot.call_action_resp.return_value = None
        await poke.send_poke("5")
        self.assertEqual(self.rows()[-1]["outcome"], "unconfirmed")

    async def test_tool_returns_are_not_claimed_as_success_or_saved_raw(self):
        tool = tool_registry.Tool("probe", "", {}, lambda: "[业务失败，私密结果]")
        with patch.dict(tool_registry._tools, {"probe": tool}):
            result = await tool_registry.call_tool("probe", {"secret_arg": 3})
        # 参数不匹配会走异常，不能伪造成功。
        self.assertIn("出错", result)
        self.assertEqual(self.rows()[-1]["outcome"], "unconfirmed")
        with patch.dict(tool_registry._tools, {"probe": tool}):
            result = await tool_registry.call_tool("probe", {})
        self.assertIn("业务失败", result)
        self.assertEqual(self.rows()[-1]["outcome"], "returned")
        self.assertNotIn("私密结果", json.dumps(self.rows(), ensure_ascii=False))

    def test_shared_mode_labels_other_sessions_and_hides_their_tool_noise(self):
        group = event("group", user="5", mid="g1", sender_display="小明(5)")
        group.text = "猫猫在干嘛"
        timeline.received(group)
        timeline.record(group, "tool", "web_search", "returned")
        timeline.record(self.ev, "received", "主人说今晚不在", "received")
        prompt = timeline.as_prompt(self.ev.session_key)
        self.assertIn("猫猫在干嘛", prompt)
        line = next(l for l in prompt.splitlines() if "猫猫在干嘛" in l)
        self.assertEqual(json.loads(line)["from"], "QQ群2")
        self.assertEqual(json.loads(line)["who"], "小明(5)")
        self.assertNotIn("web_search", prompt)
        group_prompt = timeline.as_prompt(group.session_key)
        self.assertIn("主人说今晚不在", group_prompt)
        self.assertIn("QQ私聊·1", group_prompt)

    def test_shared_mode_clear_forgets_own_rows_but_keeps_others(self):
        group = event("group", user="5", mid="g1")
        group.text = "group fact"
        timeline.received(group)
        timeline.record(self.ev, "received", "private fact", "received")
        timeline.record(self.ev, "message", "delivered", "confirmed", target=group.session_key)
        session.get_session(self.ev.session_key).clear()
        prompt = timeline.as_prompt(self.ev.session_key)
        self.assertIn("group fact", prompt)
        self.assertNotIn("private fact", prompt)
        self.assertNotIn("delivered", prompt)
        self.assertIn("delivered", timeline.as_prompt(group.session_key))
        self.assertNotIn("private fact", timeline.as_prompt(group.session_key))

    def test_budget_prefers_important_activity_notes_and_keeps_time_order(self):
        for i in range(30):
            timeline.record(event("group", user="5", mid=f"g{i}"), "received", f"chat {i:02}", "received")
        timeline.note("agent_timer", "提醒 #1 已完成", label="后台·定时提醒", importance=3)
        timeline.record(event("group", user="5", mid="late"), "received", "late chat", "received")
        with patch.object(config, "AGENT_CONTEXT_LINES", 3):
            prompt = timeline.as_prompt(self.ev.session_key)
        rows = [json.loads(l) for l in prompt.splitlines() if l.startswith("{\"time")]
        self.assertEqual([r["text"] for r in rows], ["chat 29", "提醒 #1 已完成", "late chat"])
        self.assertEqual(rows[1]["from"], "后台·定时提醒")

    def test_notes_respect_scoped_mode_and_synthetic_events_are_not_heard(self):
        with patch.object(config, "AGENT_SHARE_EXPERIENCES", False):
            timeline.note("agent_timer", "完成了", also_visible_to=[self.ev.session_key])
            self.assertIn("完成了", timeline.as_prompt(self.ev.session_key))
            self.assertNotIn("完成了", timeline.as_prompt("onebot_group_9"))
        timeline.received(event(mid="notice", synthetic=True))
        self.assertEqual([r["kind"] for r in self.rows()], ["activity"])

    def test_rows_saved_before_labels_existed_still_load_and_render(self):
        path = Path(self.temp.name) / timeline.FILE
        path.write_text(json.dumps({"version": 1, "current_state": None, "experiences": [{
            "time": time.time(), "kind": "received", "outcome": "received", "source": "onebot_group_7",
            "actor": "5", "text": "old row", "trigger_id": "x", "message_id": "x", "target": "",
            "visible_to": ["onebot_group_7"],
        }]}))
        timeline.load()
        line = next(l for l in timeline.as_prompt(self.ev.session_key).splitlines() if "old row" in l)
        self.assertEqual(json.loads(line)["from"], "QQ群7")

    def test_bounds_and_no_duplicate_history_but_failed_send_is_visible(self):
        with patch.object(config, "AGENT_EXPERIENCES_MAX", 3):
            for i in range(5):
                timeline.record(self.ev, "tool", str(i) * 900, "returned")
        self.assertEqual(len(self.rows()), 3)
        self.assertTrue(all(len(r["text"]) <= 480 for r in self.rows()))
        timeline.record(self.ev, "reply", "already in history", "unconfirmed")
        prompt = timeline.as_prompt(self.ev.session_key, [{"content": "already in history"}])
        self.assertIn("already in history", prompt)
        self.assertLessEqual(len(prompt), 2400)
        timeline.received(self.ev)
        self.assertNotIn('"text": "hello"', timeline.as_prompt(self.ev.session_key, [{"content": "hello"}]))
        with patch.object(config, "AGENT_CONTEXT_CHARS", 10):
            self.assertEqual(timeline.as_prompt(self.ev.session_key), "")

    def test_clear_does_not_resurrect_local_data_or_delete_delivered_fact(self):
        timeline.received(self.ev)
        timeline.record(self.ev, "message", "delivered", "confirmed", target="onebot_group_2")
        timeline.set_state(self.ev, "local note")
        session.get_session(self.ev.session_key).clear()
        prompt = timeline.as_prompt(self.ev.session_key)
        self.assertNotIn("hello", prompt)
        self.assertNotIn("delivered", prompt)
        self.assertNotIn("local note", prompt)
        self.assertIn("delivered", timeline.as_prompt("onebot_group_2"))
        timeline.set_state(self.ev, "shared note", shared=True)
        session.get_session(self.ev.session_key).clear()
        self.assertIn("shared note", timeline.as_prompt("onebot_group_2"))

    def test_expiry_hides_old_experiences_but_keeps_newly_received_cross_session_send(self):
        timeline.record(self.ev, "received", "expired", "received")
        timeline._data["experiences"][0]["time"] = time.time() - config.HISTORY_TTL - 2
        s = session.get_session(self.ev.session_key)
        s.last_active = time.time() - config.HISTORY_TTL - 2
        timeline.record(event("group"), "message", "new delivery", "confirmed", target=self.ev.session_key)
        session.get_session(self.ev.session_key)
        prompt = timeline.as_prompt(self.ev.session_key)
        self.assertNotIn("expired", prompt)
        self.assertIn("new delivery", prompt)

    def test_old_config_missing_new_options_and_disable(self):
        with patch.dict(config.__dict__):
            for name in ("AGENT_CONTEXT_ENABLED", "AGENT_EXPERIENCES_MAX", "AGENT_CONTEXT_CHARS"):
                config.__dict__.pop(name)
            timeline.received(self.ev)
            self.assertIn("hello", timeline.as_prompt(self.ev.session_key))
        with patch.object(config, "AGENT_CONTEXT_ENABLED", False):
            timeline.received(event(mid="m2"))
            self.assertEqual(timeline.as_prompt(self.ev.session_key), "")
        self.assertEqual(len(self.rows()), 1)

    def test_persistence_reload_and_old_sessions_format_untouched(self):
        old = Path(self.temp.name) / "sessions.json"
        old.write_text('{"private_1": {"messages": [{"role": "user", "content": "old"}]}}')
        original = old.read_bytes()
        timeline.load()  # 首次升级，没有 agent_state.json。
        self.assertEqual(self.rows(), [])
        timeline.received(self.ev)
        timeline.set_state(self.ev, "saved state", shared=True)
        timeline.flush_if_dirty()
        saved = timeline.snapshot()
        timeline.load()
        self.assertEqual(timeline.snapshot(), saved)
        self.assertEqual(old.read_bytes(), original)
        self.assertEqual(session.Session([], 123).last_active, 123)  # 保持原构造参数顺序。
        self.assertNotIn("scope", json.dumps(session.snapshot()))

    def test_future_or_broken_files_are_preserved_and_not_exported_as_empty(self):
        path = Path(self.temp.name) / timeline.FILE
        for body in ('broken json', '{"version":2,"experiences":[]}', '{"version":1,"experiences":[{}]}'):
            path.write_text(body)
            timeline.load()
            self.assertIsNone(timeline.snapshot())
            self.assertEqual(timeline.as_prompt(self.ev.session_key), "")
            timeline.received(self.ev)
            timeline.flush_if_dirty()
            self.assertEqual(path.read_text(), body)
            self.assertNotIn(timeline.FILE, live_backup._runtime_state()[2])

    def test_disabled_persistence_does_not_load_or_write_and_write_failure_is_retriable(self):
        timeline.received(self.ev)
        with patch.object(storage, "save_json", side_effect=OSError):
            timeline.flush_if_dirty()
        self.assertTrue(timeline._dirty)
        timeline.flush_if_dirty()
        self.assertFalse(timeline._dirty)
        saved = (Path(self.temp.name) / timeline.FILE).read_bytes()
        with patch.object(config, "SESSION_PERSIST", False):
            timeline.load()
            self.assertEqual(self.rows(), [])
            timeline.received(self.ev)
            timeline.flush_if_dirty()
            self.assertTrue(timeline._dirty)
        self.assertEqual((Path(self.temp.name) / timeline.FILE).read_bytes(), saved)

    async def test_state_commands_permissions_and_no_activity_start(self):
        await state.state(self.ev, self.bot, MatchResult(text="set private note"))
        self.assertEqual(timeline.snapshot()["current_state"]["visibility"], "session")
        await state.state(event("group"), self.bot, MatchResult(text="share forbidden"))
        await state.state(event(user="2"), self.bot, MatchResult(text="clear"))
        self.assertEqual(timeline.snapshot()["current_state"]["note"], "private note")
        await state.state(self.ev, self.bot, MatchResult(text="share public note"))
        self.assertIn("public note", timeline.as_prompt("onebot_group_2"))
        await state.state(self.ev, self.bot, MatchResult(text="clear"))
        self.assertIsNone(timeline.snapshot()["current_state"])
        self.assertEqual(coalesce._pending, {})
        timeline.set_state(self.ev, "retained when disabled")
        with patch.object(config, "AGENT_CONTEXT_ENABLED", False):
            await state.state(self.ev, self.bot, MatchResult(text="clear"))
        self.assertEqual(timeline.snapshot()["current_state"]["note"], "retained when disabled")

    async def test_private_chat_injects_latest_state_and_only_records_real_send(self):
        timeline.set_state(self.ev, "initial")
        provider = SimpleNamespace(chat=AsyncMock(return_value=llm.ChatResult("response")))
        with patch.object(llm, "get_provider", return_value=provider):
            await plugin.dispatch(self.ev, self.bot)
            timeline.set_state(self.ev, "latest state")  # 合批等待期间更新。
            await self.drain()
        request = provider.chat.await_args.args[0]
        self.assertIn("latest state", request[0]["content"])
        self.assertEqual(request[-1]["content"], "hello")
        self.assertEqual([r["kind"] for r in self.rows()], ["received", "reply", "reply"])
        self.assertEqual(self.rows()[-1]["outcome"], "confirmed")
        self.assertEqual(self.bot.sent, [("reply", "response")])

    async def test_background_group_record_does_not_trigger_new_replies(self):
        provider = SimpleNamespace(chat=AsyncMock(return_value=llm.ChatResult('{"messages":[]}')))
        group = event("group")
        with patch.object(group_chat, "get_provider", return_value=provider):
            await plugin.dispatch(group, self.bot)
            await self.drain()
        provider.chat.assert_not_awaited()
        self.assertEqual(len(self.rows()), 1)
        timeline.set_state(self.ev, "shared availability", shared=True)
        group = event("group", mid="m2", is_at_bot=True)
        with patch.object(group_chat, "get_provider", return_value=provider):
            await plugin.dispatch(group, self.bot)
            await self.drain()
        self.assertIn("shared availability", provider.chat.await_args.args[0][0]["content"])
        self.assertFalse(self.bot.sent)

    async def test_restart_flushes_latest_experiences_before_exec(self):
        with patch.object(admin, "os") as proc, patch.object(session, "save"), \
             patch.object(admin.asyncio, "sleep", AsyncMock()), patch.object(timeline, "flush_if_dirty") as flush:
            await admin.restart_cmd.__wrapped__(self.ev, self.bot, MatchResult())
        flush.assert_called_once()
        proc.execve.assert_called_once()


class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_onebot_private_and_images_require_ack_without_changing_api(self):
        bot = OnebotBot(None)
        bot.call_action_resp = AsyncMock(return_value={"status": "ok", "retcode": 0, "data": {"message_id": 2}})
        self.assertIsNone(await bot.send_private_msg("1", "hello"))
        for kind in ("private", "group"):
            await bot.send_image(event(kind), "/tmp/image.png")
            self.assertEqual(bot.call_action_resp.await_args.kwargs["message"],
                             [{"type": "image", "data": {"file": "file:///tmp/image.png"}}])
        for result in (None, {"status": "failed", "retcode": 100}):
            bot.call_action_resp.return_value = result
            with self.assertRaises(Exception):
                await bot.send_private_msg("1", "hello")
            for kind in ("private", "group"):
                with self.assertRaises(Exception):
                    await bot.send_image(event(kind), "/tmp/image.png")

    async def test_feishu_upload_failure_is_not_send_success(self):
        from adapters.feishu import FeishuBot
        from core.bot import ImageSeg
        bot = FeishuBot(None, "bot")
        bot._ensure_image_key = AsyncMock(return_value=None)
        for kind in ("private", "group"):
            with self.assertRaises(RuntimeError):
                await bot.send_image(event(kind), "/tmp/image.png")
        with self.assertRaises(RuntimeError):
            await bot.send_private_msg("user", [ImageSeg("/tmp/image.png")])
