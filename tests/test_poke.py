import unittest
from unittest.mock import AsyncMock

from adapters.onebot import OnebotBot, _normalize
from core import runtime
from core.event import MessageEvent
from core.tools import call_tool, get_tool_schemas
from tools.poke import send_poke


OK = {"status": "ok", "retcode": 0, "data": None}


class PokeAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_group_and_private_use_their_own_actions_with_confirmed_success(self):
        bot = OnebotBot(None)
        bot.call_action_resp = AsyncMock(return_value=OK)
        await bot.send_poke("7", group_id="2")
        bot.call_action_resp.assert_awaited_once_with("group_poke", group_id=2, user_id=7)
        bot.call_action_resp.reset_mock()
        bot.call_action_resp.return_value = {"status": "ok", "retcode": 0, "data": {"result": 0}}
        await bot.send_poke("7")
        bot.call_action_resp.assert_awaited_once_with("friend_poke", user_id=7)

    async def test_rejection_timeout_and_bad_response_never_succeed_or_retry(self):
        bot = OnebotBot(None)
        for response in (None, {}, [], {"status": "ok"},
                         {"status": "failed", "retcode": 100},
                         {"status": "ok", "retcode": 100},
                         {"status": "ok", "retcode": 0, "data": {"result": 1}}):
            for group_id in (None, "2"):
                with self.subTest(response=response, group_id=group_id):
                    bot.call_action_resp = AsyncMock(return_value=response)
                    with self.assertRaises((RuntimeError, TimeoutError)):
                        await bot.send_poke("7", group_id=group_id)
                    self.assertEqual(bot.call_action_resp.await_count, 1)


class PokeToolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = OnebotBot(None)
        self.bot.call_action_resp = AsyncMock(return_value=OK)
        self.event = MessageEvent(
            platform="onebot", message_type="group", user_id="1", group_id="2", self_id="4",
        )
        runtime.set_context(self.event, self.bot)

    async def test_registered_tool_defaults_to_current_sender_and_supports_other_group_member(self):
        schema = next(s["function"] for s in get_tool_schemas() if s["function"]["name"] == "send_poke")
        self.assertIn("user_id", schema["parameters"]["properties"])
        self.assertNotIn("group_id", schema["parameters"]["properties"])
        self.bot.call_action_resp.assert_not_awaited()
        result = await call_tool("send_poke", {})
        self.assertIn("已在群 2戳了戳 QQ 1", result)
        self.bot.call_action_resp.assert_awaited_once_with("group_poke", group_id=2, user_id=1)
        self.bot.call_action_resp.reset_mock()
        await send_poke(" 7 ")
        self.bot.call_action_resp.assert_awaited_once_with("group_poke", group_id=2, user_id=7)

    async def test_private_defaults_to_partner_and_accepts_explicit_friend(self):
        self.event.message_type = "private"
        await send_poke()
        self.bot.call_action_resp.assert_awaited_once_with("friend_poke", user_id=1)
        self.bot.call_action_resp.reset_mock()
        await send_poke("9")
        self.bot.call_action_resp.assert_awaited_once_with("friend_poke", user_id=9)

    async def test_poke_back_is_optional_and_self_notice_is_ignored(self):
        for group_id in (None, 2):
            with self.subTest(group_id=group_id):
                self.bot.call_action_resp.reset_mock()
                raw = {"post_type": "notice", "notice_type": "notify", "sub_type": "poke",
                       "self_id": 4, "user_id": 1, "target_id": 4, "group_id": group_id}
                if group_id is None:
                    raw["sender_id"] = 7  # 私聊 actor 与会话对象字段分开。
                event = _normalize(raw)
                runtime.set_context(event, self.bot)
                self.bot.call_action_resp.assert_not_awaited()
                await send_poke()
                action = self.bot.call_action_resp.await_args
                self.assertEqual(action.kwargs["user_id"], 7 if group_id is None else 1)
                own = {**raw, "target_id": int(event.user_id)}
                own["sender_id" if group_id is None else "user_id"] = 4
                self.assertIsNone(_normalize(own))

    async def test_invalid_ids_do_not_send(self):
        for user_id in (None, True, 7, "0", "-1", "1.0", "@7", "all", "１２"):
            with self.subTest(user_id=user_id):
                self.assertIn("必须", await send_poke(user_id))
        self.event.group_id = "0"
        self.assertIn("群号无效", await send_poke("7"))
        self.bot.call_action_resp.assert_not_awaited()

    async def test_no_context_other_platform_or_missing_connection_do_not_send(self):
        for event, bot in ((None, self.bot), (self.event, None)):
            runtime.set_context(event, bot)
            self.assertIn("只能在 QQ", await send_poke())
        self.event.platform = "feishu"
        runtime.set_context(self.event, self.bot)
        self.assertIn("只能在 QQ", await send_poke())
        self.bot.call_action_resp.assert_not_awaited()

    async def test_failure_and_uncertain_delivery_are_not_reported_as_success(self):
        for response in (None, {"status": "failed", "retcode": 1200}):
            with self.subTest(response=response):
                self.bot.call_action_resp = AsyncMock(return_value=response)
                result = await send_poke()
                self.assertIn("未确认", result)
                self.assertNotIn("已在", result)
                self.assertEqual(self.bot.call_action_resp.await_count, 1)
        self.bot.call_action_resp = AsyncMock(side_effect=ConnectionError("closed"))
        self.assertIn("可能已经发送", await send_poke())
        self.bot.call_action_resp.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
