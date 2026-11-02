import unittest
from unittest.mock import AsyncMock, Mock, patch

from core import plugin, runtime
from core.bot import ImageInfo
from core.event import MessageEvent


class IgnoreMessageTests(unittest.IsolatedAsyncioTestCase):
    async def test_ignore_bypasses_all_observers_and_handlers_in_group_and_private(self):
        for kind in ('group', 'private'):
            for text in ('/ignore 这条消息bot看不到', '/ignore', '/ignore\n多行内容',
                         '  /ignore\t内容', 'bot /ignore 内容', '/chat /ignore 内容'):
                for mentioned in (False, True):
                    with self.subTest(kind=kind, text=text, mentioned=mentioned):
                        event = MessageEvent(
                            platform='onebot', message_type=kind, user_id='user', group_id='group',
                            self_id='bot', text=text, is_at_bot=mentioned,
                            images=[ImageInfo(key='ignored-image')],
                        )
                        matcher = Mock()
                        with patch.object(plugin, 'run_middlewares', new_callable=AsyncMock) as middleware, \
                             patch.object(plugin, '_matchers', [matcher]), \
                             patch.object(runtime, 'set_context') as context:
                            await plugin.dispatch(event, Mock())
                        # 所有会记录历史、排回复、学习图片的入口都在这些下游路径里。
                        middleware.assert_not_awaited()
                        matcher.check.assert_not_called()
                        context.assert_not_called()
                        self.assertNotIn('llm_text', event.__dict__)

    async def test_similar_words_or_ignore_inside_body_are_not_suppressed(self):
        for text in ('ignore 内容', '/ignored 内容', '/ignore_this 内容',
                     '这句话中间提到 /ignore', '请看\n/ignore 内容', '普通消息', ''):
            with self.subTest(text=text):
                event = MessageEvent(platform='onebot', message_type='group', user_id='user', text=text)
                handler = AsyncMock()
                matcher = plugin.Matcher(handler=handler, rule=lambda e: plugin.MatchResult(), to_me=False)
                with patch.object(plugin, 'run_middlewares', new=AsyncMock(return_value=True)) as middleware, \
                     patch.object(plugin, '_matchers', [matcher]):
                    await plugin.dispatch(event, Mock())
                middleware.assert_awaited_once()
                handler.assert_awaited_once()


if __name__ == '__main__':
    unittest.main()
