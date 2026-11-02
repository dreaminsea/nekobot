import asyncio
import base64
import json
import os
import tempfile
import unittest
from collections import OrderedDict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from core import images, llm, replies, session, storage
from core.bot import ImageInfo
from core.event import MessageEvent

PNG = base64.b64decode(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRz8AAAAASUVORK5CYII='
)
TOOLS = [{'type': 'function', 'function': {'name': 'test_tool', 'parameters': {}}}]


def completion(text='看到了喵', *, reason='stop', reasoning=None, calls=None):
    message = {'role': 'assistant', 'content': text}
    if reasoning is not None:
        message['reasoning_content'] = reasoning
    if calls is not None:
        message['tool_calls'] = calls
    return {'choices': [{'finish_reason': reason, 'message': message}]}


def tool_call(arguments='{}', *, name='test_tool', call_id='call-1'):
    return {'id': call_id, 'type': 'function',
            'function': {'name': name, 'arguments': arguments}}


def image_blocks(payload):
    return [part for message in payload['messages']
            if isinstance(message.get('content'), list)
            for part in message['content'] if part.get('type') == 'image_url']


class MultimodalTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.history = session.Session()
        self.requests = []
        self.responses = []
        self.client = httpx.AsyncClient(transport=httpx.MockTransport(self.handle_request))
        self.addAsyncCleanup(self.client.aclose)
        patches = [
            patch.object(images, '_cache_dir', Path(self.tmp.name) / 'images'),
            patch.object(images, '_registry', OrderedDict()),
            patch.object(llm, 'http_client', self.client),
            patch.object(llm, 'LLM_API_KEY', 'test-key'),
            patch.object(llm, 'get_session', return_value=self.history),
            patch.object(llm, '_provider_cache', llm.OpenAICompatProvider()),
            patch.object(llm, 'has_tools', return_value=False),
            patch.object(storage, 'DATA_DIR', self.tmp.name),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def handle_request(self, request):
        self.assertEqual(str(request.url), 'https://api.deepseek.com/chat/completions')
        payload = json.loads(request.content)
        self.requests.append(payload)
        self.assertEqual(payload['model'], 'deepseek-flash')
        self.assertNotIn('image_ref', request.content.decode())
        response = self.responses.pop(0)
        if isinstance(response, httpx.Response):
            return response
        return httpx.Response(200, json=response)

    def event(self, key='img_v3_example_a', text='', **kwargs):
        fetch = AsyncMock(return_value=PNG)
        event = MessageEvent(
            platform='feishu', message_type='private', user_id='u1',
            text=text, images=[ImageInfo(key=key, fetch=fetch)], **kwargs,
        )
        return event, fetch

    async def test_image_only_goes_directly_to_main_model(self):
        event, fetch = self.event()
        llm.record_user('s', event.llm_content())
        self.responses = [completion()]
        with patch.object(llm, 'call_tool', new=AsyncMock()) as call:
            result = await llm.reply_turn('s', 'test', enable_tools=False)
        self.assertEqual(result, '看到了喵')
        call.assert_not_awaited()
        fetch.assert_awaited_once()
        blocks = image_blocks(self.requests[0])
        self.assertEqual(len(blocks), 1)
        self.assertEqual(base64.b64decode(blocks[0]['image_url']['url'].split(',')[1]), PNG)
        self.assertEqual(self.requests[0]['thinking'], {'type': 'enabled'})
        self.assertNotIn('data:image', json.dumps(self.history.messages))

    async def test_debounced_messages_keep_all_images_and_sender_text(self):
        first, _ = self.event(key='img_v3_same_prefix_a', text='第一张')
        second, _ = self.event(key='img_v3_same_prefix_b', text='第二张')
        self.assertNotEqual(images.short_id(first.images[0]), images.short_id(second.images[0]))
        llm.record_user('s', first.llm_content(prefix='甲(u1): '))
        llm.record_user('s', second.llm_content(prefix='乙(u2): '))
        llm.record_user('s', '比较这两张图')
        self.responses = [completion('两张图的比较')]
        await llm.reply_turn('s', 'test', enable_tools=False)
        self.assertEqual(len(image_blocks(self.requests[0])), 2)
        users = self.requests[0]['messages'][1:]
        self.assertTrue(users[0]['content'][0]['text'].startswith('甲(u1): 第一张'))
        self.assertTrue(users[1]['content'][0]['text'].startswith('乙(u2): 第二张'))
        self.assertEqual(users[2]['content'], '比较这两张图')

    async def test_group_decision_and_multiple_replies_use_one_multimodal_request(self):
        from core import coalesce, group_chat
        from tests.test_replies import FakeBot
        event, fetch = self.event()
        event.message_type, event.group_id, event.message_id = 'group', 'g1', 'm1'
        event.is_at_bot = True
        self.responses = [completion(json.dumps({'messages': [{'text': '接话'}, {'text': '第二条', 'reply_to': 'm1'}]}))]
        bot = FakeBot()
        with patch.object(group_chat, 'get_session', return_value=self.history), \
             patch.object(group_chat, 'has_tools', return_value=False), \
             patch.object(group_chat, 'REPLY_MESSAGE_DELAY', 0), \
             patch.object(coalesce, 'REPLY_DEBOUNCE', 0), \
             patch.object(coalesce, 'GROUP_IDLE_DEBOUNCE', 0):
            await group_chat.handle_group_message(event, bot)
            await coalesce._pending[event.session_key].task
        self.assertEqual(bot.sent, [('group', 'g1', '接话', None), ('group', 'g1', '第二条', 'm1')])
        self.assertEqual([len(image_blocks(p)) for p in self.requests], [1])
        self.assertEqual(self.requests[0]['response_format'], {'type': 'json_object'})
        self.assertEqual(self.requests[0]['thinking'], {'type': 'enabled'})
        self.assertEqual(self.requests[0]['reasoning_effort'], group_chat.GROUP_REASONING_EFFORT)
        fetch.assert_awaited_once()
        self.assertEqual([m['role'] for m in self.history.messages], ['user', 'assistant', 'assistant'])
        self.assertNotIn('"messages"', json.dumps(self.history.messages))

    async def test_ambient_gate_uses_only_text_then_thinking_reply_receives_image_and_tools(self):
        from core import coalesce, group_chat
        from tests.test_replies import FakeBot
        event, fetch = self.event(text='猫猫觉得这张图怎么样？')
        event.message_type, event.group_id = 'group', 'g1'
        bot = FakeBot()
        self.responses = [completion('REPLY'), completion('{"messages":[{"text":"很可爱"}]}')]
        with (
            patch.object(group_chat, 'get_session', return_value=self.history),
            patch.object(group_chat, 'AMBIENT_GROUPS', ['g1']),
            patch.object(group_chat, 'has_tools', return_value=True),
            patch.object(group_chat, 'get_tool_schemas', return_value=TOOLS),
            patch.object(coalesce, 'REPLY_DEBOUNCE', 0),
            patch.object(coalesce, 'GROUP_IDLE_DEBOUNCE', 0),
        ):
            await group_chat.handle_group_message(event, bot)
            await coalesce._pending[event.session_key].task
        gate, reply = self.requests
        self.assertEqual(gate['thinking'], {'type': 'disabled'})
        self.assertNotIn('tools', gate)
        self.assertNotIn('reasoning_effort', gate)
        self.assertFalse(image_blocks(gate))
        self.assertIn('[图片#', str(gate['messages']))
        self.assertEqual(reply['thinking'], {'type': 'enabled'})
        self.assertEqual(reply['reasoning_effort'], group_chat.GROUP_REASONING_EFFORT)
        self.assertEqual(reply['tools'], TOOLS)
        self.assertEqual(len(image_blocks(reply)), 1)
        fetch.assert_awaited_once()
        self.assertEqual(bot.sent, [('group', 'g1', '很可爱', None)])

    async def test_each_round_logs_latency_effort_cache_and_reasoning_usage(self):
        usage = {'prompt_tokens': 3000, 'prompt_cache_hit_tokens': 2560, 'completion_tokens': 420,
                 'completion_tokens_details': {'reasoning_tokens': 400}}
        self.responses = [{**completion(), 'usage': usage}, completion()]
        with self.assertLogs('qqbot', 'INFO') as logs:
            await llm.get_provider().chat([{'role': 'user', 'content': '你好'}], reasoning_effort='low')
            await llm.get_provider().chat([{'role': 'user', 'content': '你好'}], thinking=False)
        self.assertEqual(self.requests[0]['reasoning_effort'], 'low')
        first, second = [line for line in logs.output if '[LLM]' in line]
        self.assertRegex(first, r'\[LLM\] \d+\.\ds 思考=low 输入=3000\(缓存命中2560\) 输出=420\(推理400\)')
        self.assertRegex(second, r'\[LLM\] \d+\.\ds 思考=关$')

    async def test_json_tool_round_preface_does_not_contaminate_final_plan(self):
        final = json.dumps({'messages': [{'text': '工具处理完了'}]})
        self.responses = [completion('我来处理', calls=[tool_call()], reason='tool_calls'), completion(final)]
        with patch.object(llm, 'call_tool', new=AsyncMock(return_value='成功')) as call:
            result = await llm.get_provider().chat(
                [{'role': 'user', 'content': '操作'}], tools=TOOLS, response_format={'type': 'json_object'},
            )
        self.assertEqual(result.text, final)
        call.assert_awaited_once()
        self.assertEqual(len(self.requests), 2)
        self.assertTrue(all(p['response_format'] == {'type': 'json_object'} for p in self.requests))

    async def test_invalidated_generation_does_not_execute_returned_tools(self):
        self.responses = [completion(None, calls=[tool_call()], reason='tool_calls')]
        with patch.object(llm, 'call_tool', new=AsyncMock()) as call:
            result = await llm.get_provider().chat(
                [{'role': 'user', 'content': '操作'}], tools=TOOLS, is_current=lambda: False,
            )
        self.assertFalse(result.ok)
        call.assert_not_awaited()

    async def test_failed_image_keeps_text_and_reports_unavailable(self):
        event, fetch = self.event(text='这是什么')
        fetch.side_effect = RuntimeError('expired')
        llm.record_user('s', event.llm_content())
        self.responses = [completion('请重新发图')]
        await llm.reply_turn('s', 'test', enable_tools=False)
        self.assertEqual(image_blocks(self.requests[0]), [])
        text = json.dumps(self.requests[0], ensure_ascii=False)
        self.assertIn('这是什么', text)
        self.assertIn('原图不可用', text)

    async def test_plain_text_image_marker_is_not_loaded(self):
        event, fetch = self.event()
        sid = images.register(event.images[0])
        original = [{'role': 'user', 'content': f'我写的 [图片#{sid}]'}]
        self.assertEqual(await images.prepare_messages(original), original)
        fetch.assert_not_awaited()

    async def test_cache_and_lightweight_history_survive_restart(self):
        event, fetch = self.event()
        llm.record_user('s', event.llm_content())
        before = json.dumps(self.history.messages)
        self.responses = [completion()]
        await llm.reply_turn('s', 'test', enable_tools=False)
        with patch.object(session, 'sessions', {'s': self.history}):
            session.save()
        images._registry.clear()
        with patch.object(session, 'sessions', {}) as restored:
            session.load()
            messages = restored['s'].messages
        materialized = await images.prepare_messages(messages)
        self.assertEqual(len(image_blocks({'messages': materialized})), 1)
        fetch.assert_awaited_once()
        saved = Path(self.tmp.name, 'sessions.json').read_text()
        self.assertNotIn('data:image', saved)
        self.assertEqual(json.dumps(messages[:1]), before)

    async def test_image_limits_prioritize_newest_and_keep_reference(self):
        first, _ = self.event(key='a')
        second, _ = self.event(key='b')
        msgs = [{'role': 'user', 'content': e.llm_content()} for e in (first, second)]
        for limits in ({'IMAGE_MAX_PER_REQUEST': 1}, {'IMAGE_REQUEST_MAX_BYTES': len(PNG)}):
            with self.subTest(limits=limits), patch.multiple(images, **limits):
                prepared = await images.prepare_messages(msgs)
                self.assertEqual(len(image_blocks({'messages': prepared})), 1)
                self.assertEqual(prepared[0]['content'][-1]['type'], 'text')
                self.assertEqual(prepared[1]['content'][-1]['type'], 'image_url')
                self.assertEqual(msgs[0]['content'][-1]['type'], 'image_ref')

    async def test_qq_url_download_and_size_limit(self):
        downloaded = []
        def serve(request):
            downloaded.append(str(request.url))
            return httpx.Response(200, content=PNG)
        async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as client:
            img = ImageInfo(key='qq-a', url='https://image.example/test.png')
            with patch.object(images, '_http', client):
                self.assertEqual(await images.fetch_bytes(img), PNG)
                with patch.object(images, 'IMAGE_MAX_BYTES', len(PNG) - 1):
                    self.assertIsNone(await images.fetch_bytes(img))
        self.assertEqual(len(downloaded), 2)

    async def test_unsupported_image_and_expired_cache_are_text_fallbacks(self):
        event, fetch = self.event()
        fetch.return_value = b'<html>expired image</html>'
        msgs = [{'role': 'user', 'content': event.llm_content()}]
        self.assertEqual(image_blocks({'messages': await images.prepare_messages(msgs)}), [])
        fetch.return_value = PNG
        await images.prepare_messages(msgs)
        path = next(images._cache_dir.glob('*.bin'))
        os.utime(path, (1, 1))
        images._registry.clear()
        self.assertEqual(image_blocks({'messages': await images.prepare_messages(msgs)}), [])
        self.assertFalse(path.exists())

    async def test_cache_prunes_oldest_by_byte_budget(self):
        first, _ = self.event(key='a')
        second, _ = self.event(key='b')
        with patch.object(images, 'IMAGE_CACHE_MAX_BYTES', len(PNG)):
            await images._load_bytes(images.register(first.images[0]))
            old = next(images._cache_dir.glob('*.bin'))
            os.utime(old, (os.path.getmtime(old) - 5,) * 2)
            await images._load_bytes(images.register(second.images[0]))
        self.assertFalse(old.exists())
        self.assertEqual(len(list(images._cache_dir.glob('*.bin'))), 1)

    async def test_reasoning_and_tool_history_survive_next_turn(self):
        event, _ = self.event()
        llm.record_user('s', event.llm_content())
        self.responses = [
            completion(None, reason='tool_calls', reasoning='tool reasoning', calls=[tool_call()]),
            completion('完成', reasoning='final reasoning'),
            completion('继续'),
        ]
        with (
            patch.object(llm, 'has_tools', return_value=True),
            patch.object(llm, 'get_tool_schemas', return_value=TOOLS),
            patch.object(llm, 'call_tool', new=AsyncMock(return_value='done')) as call,
        ):
            await llm.reply_turn('s', 'test')
            llm.record_user('s', '然后呢')
            await llm.reply_turn('s', 'test')
        call.assert_awaited_once_with('test_tool', {})
        self.assertEqual([len(image_blocks(p)) for p in self.requests], [1, 1, 1])
        self.assertEqual(self.requests[0]['thinking'], {'type': 'enabled'})
        self.assertEqual(self.requests[0]['reasoning_effort'], 'high')
        self.assertEqual(self.requests[0]['max_tokens'], llm.LLM_THINKING_MAX_TOKENS)
        self.assertNotIn('temperature', self.requests[0])
        self.assertEqual(self.requests[1]['messages'][-2]['reasoning_content'], 'tool reasoning')
        assistants = [m for m in self.requests[-1]['messages'] if m['role'] == 'assistant']
        self.assertEqual([m['reasoning_content'] for m in assistants], ['tool reasoning', 'final reasoning'])
        self.assertEqual([m['role'] for m in self.history.messages],
                         ['user', 'assistant', 'tool', 'assistant', 'user', 'assistant'])

    async def test_length_and_empty_responses_never_save_empty_reply_or_execute_tools(self):
        for response in (
            completion(None, reason='length', calls=[tool_call()]),
            completion('部分正文', reason='length'),
            completion(None),
        ):
            with self.subTest(response=response):
                self.history.clear()
                self.history.add_user('test')
                self.responses = [response]
                with (
                    patch.object(llm, 'has_tools', return_value=True),
                    patch.object(llm, 'get_tool_schemas', return_value=TOOLS),
                    patch.object(llm, 'call_tool', new=AsyncMock()) as call,
                ):
                    text = await llm.reply_turn('s', 'test')
                self.assertTrue(text)
                self.assertEqual(len(self.history.messages), 1)
                call.assert_not_awaited()

    async def test_tool_round_text_reaches_sender_in_order_without_reasoning_or_tool_results(self):
        from tests.test_replies import FakeBot

        parts = ['这张图表示疑惑。', '也常用来表示没听懂。', '（跟着一起冒问号）']
        self.history.add_user('这张图是什么意思？')
        self.responses = [
            completion(parts[0], reason='tool_calls', reasoning='private reasoning',
                       calls=[tool_call(call_id='first')]),
            completion(parts[1], reason='tool_calls', calls=[tool_call(call_id='second')]),
            completion(parts[2]),
        ]
        with (
            patch.object(llm, 'has_tools', return_value=True),
            patch.object(llm, 'get_tool_schemas', return_value=TOOLS),
            patch.object(llm, 'call_tool', new=AsyncMock(return_value='internal tool result')),
            patch.object(replies, 'REPLY_MESSAGE_DELAY', 0),
        ):
            text = await llm.reply_turn('s', 'test')
            bot = FakeBot()
            event, _ = self.event(text='这张图是什么意思？')
            await replies.send_ai_reply(bot, event, text)

        self.assertEqual(replies.split_reply(text), parts)
        self.assertEqual(bot.sent, [
            ('reply', parts[0]), ('private', 'u1', parts[1]), ('private', 'u1', parts[2]),
        ])
        self.assertNotIn('private reasoning', text)
        self.assertNotIn('internal tool result', text)
        assistants = [m for m in self.history.messages if m['role'] == 'assistant']
        self.assertEqual([m['content'] for m in assistants], parts)
        self.assertEqual(assistants[0]['reasoning_content'], 'private reasoning')

    async def test_completed_tool_text_is_delivered_even_when_continuation_fails(self):
        for failure in (
            httpx.Response(503, text='unavailable'),
            completion('unfinished text', reason='length', calls=[tool_call()]),
            completion('unfinished text', reason='content_filter'),
        ):
            with self.subTest(failure=failure):
                self.history.clear()
                self.history.add_user('问题')
                self.responses = [
                    completion('已经生成的完整回答', reason='tool_calls', calls=[tool_call()]),
                    failure,
                ]
                with (
                    patch.object(llm, 'has_tools', return_value=True),
                    patch.object(llm, 'get_tool_schemas', return_value=TOOLS),
                    patch.object(llm, 'call_tool', new=AsyncMock(return_value='done')) as call,
                ):
                    text = await llm.reply_turn('s', 'test')
                self.assertEqual(replies.split_reply(text)[0], '已经生成的完整回答')
                self.assertNotIn('unfinished text', text)
                self.assertNotIn('unfinished text', json.dumps(self.history.messages))
                self.assertEqual([m['role'] for m in self.history.messages], ['user', 'assistant', 'tool'])
                call.assert_awaited_once()

    async def test_empty_final_response_keeps_completed_tool_round_text(self):
        self.responses = [
            completion('完整回答', reason='tool_calls', calls=[tool_call()]),
            completion(None),
        ]
        with patch.object(llm, 'call_tool', new=AsyncMock(return_value='done')):
            result = await llm.get_provider().chat([{'role': 'user', 'content': '问题'}], tools=TOOLS)
        self.assertTrue(result.ok)
        self.assertEqual(result.text, '完整回答')
        self.assertEqual([m['role'] for m in result.messages], ['assistant', 'tool'])

    async def test_sticker_limit_covers_batches_and_rounds_without_blocking_other_tools(self):
        sticker_schema = {'type': 'function', 'function': {'name': 'send_sticker', 'parameters': {}}}
        self.responses = [
            completion('正文', reason='tool_calls', calls=[
                tool_call('{"meaning":"疑惑"}', name='send_sticker', call_id='s1'),
                tool_call('{"meaning":"开心"}', name='send_sticker', call_id='s2'),
            ]),
            completion(None, reason='tool_calls', calls=[
                tool_call('{"meaning":"疑惑"}', name='send_sticker', call_id='s3'),
                tool_call(call_id='other'),
            ]),
            completion('结束'),
        ]
        with patch.object(llm, 'call_tool', new=AsyncMock(return_value='done')) as call:
            result = await llm.get_provider().chat(
                [{'role': 'user', 'content': '问题'}], tools=[sticker_schema, *TOOLS],
            )
        self.assertTrue(result.ok)
        self.assertEqual([c.args for c in call.await_args_list], [
            ('send_sticker', {'meaning': '疑惑'}), ('test_tool', {}),
        ])
        self.assertEqual(self.requests[1]['tools'], TOOLS)
        self.assertEqual(self.requests[2]['tools'], TOOLS)
        tool_results = [m for m in result.messages if m['role'] == 'tool']
        self.assertEqual([m['tool_call_id'] for m in tool_results], ['s1', 's2', 's3', 'other'])
        self.assertIn('不再重复', tool_results[1]['content'])
        self.assertIn('不再重复', tool_results[2]['content'])

    async def test_sticker_limit_resets_next_turn_and_requests_text_when_no_tools_remain(self):
        sticker_schema = {'type': 'function', 'function': {'name': 'send_sticker', 'parameters': {}}}
        # A failed send attempt also consumes the limit, avoiding retry spam.
        with patch.object(llm, 'call_tool', new=AsyncMock(return_value='[发表情失败]')) as call:
            for _ in range(2):
                self.responses = [
                    completion(None, reason='tool_calls', calls=[tool_call(name='send_sticker')]),
                    completion('文字回答'),
                ]
                result = await llm.get_provider().chat(
                    [{'role': 'user', 'content': '问题'}], tools=[sticker_schema],
                )
                self.assertTrue(result.ok)
                self.assertNotIn('tool_choice', self.requests[-2])
                self.assertEqual(self.requests[-1]['tool_choice'], 'none')
        self.assertEqual(call.await_count, 2)

    async def test_bad_tool_arguments_do_not_execute_default_action(self):
        for args in ('{"broken":', '[]'):
            self.responses = [completion(None, reason='tool_calls', calls=[tool_call(args)]), completion()]
            with patch.object(llm, 'call_tool', new=AsyncMock()) as call:
                result = await llm.get_provider().chat([{'role': 'user', 'content': 'test'}], tools=TOOLS)
            self.assertTrue(result.ok)
            call.assert_not_awaited()
            self.assertIn('JSON', self.requests[-1]['messages'][-1]['content'])

    async def test_final_answer_allowed_after_last_tool_round(self):
        self.responses = [completion(None, reason='tool_calls', calls=[tool_call()]), completion()]
        with patch.object(llm, 'LLM_MAX_TOOL_ROUNDS', 1), patch.object(llm, 'call_tool', new=AsyncMock(return_value='done')):
            result = await llm.get_provider().chat([{'role': 'user', 'content': 'test'}], tools=TOOLS)
        self.assertTrue(result.ok)
        self.assertEqual(result.text, '看到了喵')
        self.assertEqual(self.requests[-1]['tool_choice'], 'none')

    async def test_tool_round_limit_keeps_completed_text_without_executing_extra_tools(self):
        self.responses = [
            completion('完整正文', reason='tool_calls', calls=[tool_call()]),
            completion('未执行的动作', reason='tool_calls', calls=[tool_call(call_id='extra')]),
        ]
        with patch.object(llm, 'LLM_MAX_TOOL_ROUNDS', 1), patch.object(llm, 'call_tool', new=AsyncMock(return_value='done')) as call:
            result = await llm.get_provider().chat([{'role': 'user', 'content': '问题'}], tools=TOOLS)
        self.assertFalse(result.ok)
        self.assertEqual(replies.split_reply(result.text)[0], '完整正文')
        self.assertNotIn('未执行的动作', result.text)
        self.assertEqual(len(result.messages), 2)
        call.assert_awaited_once()

    async def test_reply_attaches_to_snapshot_before_new_incoming_message(self):
        started, release = asyncio.Event(), asyncio.Event()
        async def post(_payload):
            started.set()
            await release.wait()
            return completion('第一条的回答')
        self.history.add_user('第一条')
        with patch.object(llm.get_provider(), '_post', side_effect=post):
            task = asyncio.create_task(llm.reply_turn('s', 'test', enable_tools=False))
            await started.wait()
            self.history.add_user('新到的第二条')
            release.set()
            await task
        self.assertEqual([m['content'] for m in self.history.messages],
                         ['第一条', '第一条的回答', '新到的第二条'])

    async def test_cleared_session_does_not_restore_inflight_history(self):
        self.history.add_user('旧问题')
        anchor = self.history.messages[-1]
        self.history.clear()
        self.history.add_user('新问题')
        self.history.add_reply([{'role': 'assistant', 'content': '旧回答'}], after=anchor)
        self.assertEqual(self.history.messages, [{'role': 'user', 'content': '新问题'}])

    async def test_trim_never_orphans_tool_results(self):
        self.history.add_user('old')
        self.history.add_reply([
            {'role': 'assistant', 'content': None, 'tool_calls': [tool_call()]},
            {'role': 'tool', 'tool_call_id': 'call-1', 'content': 'done'},
            {'role': 'assistant', 'content': 'finished'},
        ], after=self.history.messages[-1])
        with patch.object(session, 'MAX_HISTORY', 1):
            self.history.add_user('new')
        self.assertEqual(self.history.messages, [{'role': 'user', 'content': 'new'}])

    async def test_history_limit_counts_user_turns_and_keeps_whole_tool_exchanges(self):
        with patch.object(session, 'MAX_HISTORY', 20):
            for i in range(22):
                self.history.add_user(f'问题{i}')
                generated = []
                for j in range(6 if i % 2 else 1):
                    call_id = f'{i}-{j}'
                    generated.extend([
                        completion(None, calls=[tool_call(call_id=call_id)])['choices'][0]['message'],
                        {'role': 'tool', 'tool_call_id': call_id, 'content': 'done'},
                    ])
                generated.append({'role': 'assistant', 'content': f'回答{i}'})
                self.history.add_reply(generated, after=self.history.messages[-1])
        self.assertEqual(
            [m['content'] for m in self.history.messages if m['role'] == 'user'],
            [f'问题{i}' for i in range(2, 22)],
        )
        self.assertEqual(self.history.messages[0], {'role': 'user', 'content': '问题2'})
        self.assertEqual(self.history.messages[-1]['content'], '回答21')
        call_ids = [tc['id'] for m in self.history.messages for tc in m.get('tool_calls', [])]
        result_ids = [m['tool_call_id'] for m in self.history.messages if m['role'] == 'tool']
        self.assertEqual(call_ids, result_ids)

    async def test_history_limit_also_applies_to_consecutive_user_messages(self):
        with patch.object(session, 'MAX_HISTORY', 3):
            for i in range(10):
                self.history.add_user(f'连发{i}')
        self.assertEqual([m['content'] for m in self.history.messages], ['连发7', '连发8', '连发9'])

    async def test_plugins_record_structured_images_before_debounce(self):
        from plugins import ambient, chat
        from core import group_chat, private_chat
        event, fetch = self.event()
        with patch.object(private_chat.coalesce, 'feed') as feed:
            await chat.chat(event, None, None)
        feed.assert_called_once()
        self.assertEqual(self.history.messages[-1]['content'][-1]['type'], 'image_ref')
        fetch.assert_not_awaited()
        event.message_type = 'group'
        event.group_id = 'g1'
        event.sender_display = '用户(u1)'
        with patch.object(group_chat, 'FOLLOWUP_CHAT', False), patch.object(group_chat, 'AMBIENT_CHAT', False), \
             patch.object(group_chat, 'get_session', return_value=self.history):
            await ambient.ambient(event, None, None)
        self.assertEqual(self.history.messages[-1]['content'][-1]['type'], 'image_ref')
        self.assertEqual(self.history.messages[-1]['_group']['sender'], '用户(u1)')

    async def test_known_sticker_still_attaches_original_image(self):
        event, _ = self.event()
        with patch('core.stickers.meaning_of', return_value='开心'):
            content = event.llm_content()
        self.assertIn('[表情包:开心]', content[0]['text'])
        self.assertIn(images.short_id(event.images[0]), content[0]['text'])
        self.responses = [completion()]
        await llm.get_provider().chat([{'role': 'user', 'content': content}])
        self.assertEqual(len(image_blocks(self.requests[0])), 1)

    async def test_feishu_rich_post_images_reach_main_request(self):
        from adapters.feishu import FeishuAdapter

        adapter = object.__new__(FeishuAdapter)
        adapter._self_id = 'bot'
        adapter._download_image = AsyncMock(return_value=PNG)
        post = {'title': '两张图', 'content': [[
            {'tag': 'text', 'text': '比较一下'},
            {'tag': 'img', 'image_key': 'img_v3_post_a'},
            {'tag': 'img', 'image_key': 'img_v3_post_b'},
        ]]}
        for content in (post, {'zh_cn': post}):
            with self.subTest(content=content):
                msg = SimpleNamespace(chat_type='p2p', chat_id='chat', message_id='mid',
                                      mentions=[], message_type='post', content=json.dumps(content))
                data = SimpleNamespace(event=SimpleNamespace(
                    message=msg, sender=SimpleNamespace(sender_id=SimpleNamespace(open_id='u1')),
                ))
                event = adapter._normalize(data)
                self.assertEqual(len(event.images), 2)
                self.assertIn('比较一下', event.text)
                self.responses = [completion()]
                await llm.get_provider().chat([{'role': 'user', 'content': event.llm_content()}])
                self.assertEqual(len(image_blocks(self.requests[-1])), 2)
        self.assertEqual(adapter._download_image.await_count, 2)
        adapter._download_image.assert_any_await('mid', 'img_v3_post_a')
        adapter._download_image.assert_any_await('mid', 'img_v3_post_b')

    async def test_onebot_multiple_image_segments_remain_attachments(self):
        from adapters.onebot import _normalize

        event = _normalize({
            'post_type': 'message', 'message_type': 'group', 'user_id': 1,
            'group_id': 2, 'self_id': 3, 'sender': {'nickname': '甲'},
            'message': [
                {'type': 'text', 'data': {'text': '这两张'}},
                {'type': 'image', 'data': {'file': 'one.png', 'url': 'https://image.example/1'}},
                {'type': 'mface', 'data': {'file': 'two.png', 'url': 'https://image.example/2'}},
            ],
        })
        self.assertEqual(event.text, '这两张')
        content = event.llm_content(prefix=event.sender_display + ': ')
        self.assertEqual(len(content), 3)
        self.assertEqual([p['type'] for p in content], ['text', 'image_ref', 'image_ref'])
        self.responses = [completion()]
        with patch.object(images, 'fetch_bytes', new=AsyncMock(return_value=PNG)) as fetch:
            await llm.get_provider().chat([{'role': 'user', 'content': content}])
        self.assertEqual(fetch.await_count, 2)
        self.assertEqual(len(image_blocks(self.requests[-1])), 2)

    async def test_completed_tool_action_is_saved_if_final_request_fails(self):
        self.history.add_user('执行一次')
        self.responses = [
            completion(None, reason='tool_calls', calls=[tool_call()]),
            httpx.Response(503, text='unavailable'),
        ]
        with (
            patch.object(llm, 'has_tools', return_value=True),
            patch.object(llm, 'get_tool_schemas', return_value=TOOLS),
            patch.object(llm, 'call_tool', new=AsyncMock(return_value='action done')) as call,
        ):
            result = await llm.reply_turn('s', 'test')
        self.assertIn('503', result)
        call.assert_awaited_once()
        self.assertEqual([m['role'] for m in self.history.messages], ['user', 'assistant', 'tool'])
        self.assertEqual(self.history.messages[-1]['content'], 'action done')

    async def test_json_format_is_forwarded_with_native_image_and_no_tools(self):
        event, _ = self.event()
        self.responses = [completion('{"is_sticker": false, "should_save": false}')]
        result = await llm.get_provider().chat(
            [{'role': 'user', 'content': event.llm_content()}], tools=None,
            thinking=False, max_tokens=768, response_format={'type': 'json_object'},
        )
        self.assertTrue(result.ok)
        self.assertFalse(json.loads(result.text)['should_save'])
        payload = self.requests[0]
        self.assertEqual(payload['response_format'], {'type': 'json_object'})
        self.assertEqual(payload['thinking'], {'type': 'disabled'})
        self.assertNotIn('tools', payload)
        self.assertEqual(len(image_blocks(payload)), 1)


if __name__ == '__main__':
    unittest.main()

