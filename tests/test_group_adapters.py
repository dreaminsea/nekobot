import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from adapters.onebot import OnebotBot, _normalize


class OnebotGroupTests(unittest.IsolatedAsyncioTestCase):
    async def test_inbound_preserves_time_mentions_and_reply_target(self):
        ev = _normalize({
            'post_type': 'message', 'message_type': 'group', 'user_id': 1, 'group_id': 2,
            'message_id': 3, 'self_id': 4, 'time': 123456,
            'message': [{'type': 'reply', 'data': {'id': 99}},
                        {'type': 'at', 'data': {'qq': 4}},
                        {'type': 'text', 'data': {'text': '继续'}}],
        })
        self.assertEqual(ev.reply_to, '99')
        self.assertEqual(ev.at_list, ['4'])
        self.assertEqual(ev.timestamp, 123456)
        self.assertTrue(ev.to_me)
        self.assertEqual(ev.text, '继续')

    async def test_each_outgoing_message_has_optional_quote_and_confirmed_id(self):
        bot = OnebotBot(None)
        bot.call_action_resp = AsyncMock(return_value={'status': 'ok', 'retcode': 0, 'data': {'message_id': 123}})
        self.assertEqual(await bot.send_group_msg('2', '引用', reply_to='99'), '123')
        self.assertEqual(bot.call_action_resp.await_args.kwargs['message'], [
            {'type': 'reply', 'data': {'id': '99'}}, {'type': 'text', 'data': {'text': '引用'}},
        ])
        await bot.send_group_msg('2', '不引用')
        self.assertEqual(bot.call_action_resp.await_args.kwargs['message'], [{'type': 'text', 'data': {'text': '不引用'}}])

    async def test_send_timeout_or_failure_is_not_reported_as_success(self):
        bot = OnebotBot(None)
        for result in (None, {'status': 'failed', 'retcode': 100}, {'status': 'ok', 'retcode': 100}):
            bot.call_action_resp = AsyncMock(return_value=result)
            with self.assertRaises(RuntimeError):
                await bot.send_group_msg('2', '文本')


class FeishuGroupTests(unittest.IsolatedAsyncioTestCase):
    async def test_normalization_preserves_thread_parent_and_timestamp(self):
        from adapters.feishu import FeishuAdapter
        adapter = FeishuAdapter()
        message = SimpleNamespace(chat_type='group', chat_id='g', message_id='m',
                                  mentions=[], message_type='text', content='{"text":"继续"}',
                                  create_time='123456000', parent_id='parent')
        data = SimpleNamespace(event=SimpleNamespace(message=message, sender=None))
        ev = adapter._normalize(data)
        self.assertEqual(ev.reply_to, 'parent')
        self.assertEqual(ev.timestamp, 123456)

    async def test_feishu_group_returns_platform_id_and_raises_on_failure(self):
        from adapters.feishu import FeishuBot
        good = SimpleNamespace(success=lambda: True, data=SimpleNamespace(message_id='sent'))
        bad = SimpleNamespace(success=lambda: False, code=123)
        api = SimpleNamespace(acreate=AsyncMock(return_value=good), areply=AsyncMock(return_value=good))
        bot = FeishuBot(SimpleNamespace(im=SimpleNamespace(v1=SimpleNamespace(message=api))), 'bot')
        self.assertEqual(await bot.send_group_msg('g', '文本'), 'sent')
        self.assertEqual(await bot.send_group_msg('g', '文本', reply_to='m'), 'sent')
        api.acreate.return_value = bad
        with self.assertRaises(RuntimeError):
            await bot.send_group_msg('g', '文本')
        api.areply.return_value = bad
        with self.assertRaises(RuntimeError):
            await bot.send_group_msg('g', '文本', reply_to='m')
