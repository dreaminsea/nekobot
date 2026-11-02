import unittest
from unittest.mock import AsyncMock, patch

from config import REPLY_MESSAGE_DELAY, REPLY_SEPARATOR
from core.bot import Bot
from core.event import MessageEvent
from core.replies import send_ai_reply, split_reply


class FakeBot(Bot):
    platform = "test"

    def __init__(self):
        self.sent = []

    async def reply(self, event, parts):
        self.sent.append(("reply", parts))

    async def send_group_msg(self, group_id, parts, reply_to=None):
        self.sent.append(("group", group_id, parts, reply_to))

    async def send_private_msg(self, user_id, parts):
        self.sent.append(("private", user_id, parts))

    async def send_image(self, event, path):
        self.sent.append(("image", path))


class SplitReplyTests(unittest.TestCase):
    def test_splits_separator_on_its_own_line(self):
        text = f"第一句\n{REPLY_SEPARATOR}\n第二句"
        self.assertEqual(split_reply(text), ["第一句", "第二句"])

    def test_does_not_split_separator_inside_code_fence(self):
        text = f"```text\n{REPLY_SEPARATOR}\n```"
        self.assertEqual(split_reply(text), [text])

    def test_ignores_empty_parts(self):
        text = f"{REPLY_SEPARATOR}\n第一句\n{REPLY_SEPARATOR}\n{REPLY_SEPARATOR}"
        self.assertEqual(split_reply(text), ["第一句"])


class SendReplyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        patcher = patch("core.replies.asyncio.sleep", new_callable=AsyncMock)
        self.sleep = patcher.start()
        self.addCleanup(patcher.stop)

    async def test_group_quotes_only_first_message(self):
        bot = FakeBot()
        event = MessageEvent(
            platform="test", message_type="group", user_id="u1", group_id="g1"
        )

        count = await send_ai_reply(
            bot, event, f"第一句\n{REPLY_SEPARATOR}\n第二句"
        )

        self.assertEqual(count, 2)
        self.assertEqual(bot.sent, [
            ("reply", "第一句"),
            ("group", "g1", "第二句", None),
        ])
        self.sleep.assert_awaited_once_with(REPLY_MESSAGE_DELAY)

    async def test_ambient_messages_are_all_unquoted(self):
        bot = FakeBot()
        event = MessageEvent(
            platform="test", message_type="group", user_id="u1", group_id="g1"
        )

        await send_ai_reply(
            bot,
            event,
            f"第一句\n{REPLY_SEPARATOR}\n第二句",
            reply_first=False,
        )

        self.assertEqual(bot.sent, [
            ("group", "g1", "第一句", None),
            ("group", "g1", "第二句", None),
        ])
        self.sleep.assert_awaited_once_with(REPLY_MESSAGE_DELAY)

    async def test_single_message_has_no_delay(self):
        bot = FakeBot()
        event = MessageEvent(
            platform="test", message_type="private", user_id="u1"
        )

        await send_ai_reply(bot, event, "只有一句")

        self.assertEqual(bot.sent, [("reply", "只有一句")])
        self.sleep.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
