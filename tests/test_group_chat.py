import asyncio
import json
import unittest
from unittest.mock import AsyncMock, Mock, patch

from config import GROUP_DECISION_PROMPT
from core import coalesce, engagement, group_chat, llm, runtime, session
from core.event import MessageEvent
from core.group_context import build_group_snapshot
from core.replies import GroupReply, parse_group_replies
from tests.test_replies import FakeBot


def event(mid='m1', text='hello', user='u1', **kwargs):
    return MessageEvent(platform='test', message_type='group', user_id=user,
                        group_id='g1', self_id='bot', message_id=mid, text=text,
                        sender_display=f'用户({user})', **kwargs)


def answer(messages=()):
    return llm.ChatResult(json.dumps({'messages': list(messages)}, ensure_ascii=False))


class ReplyProtocolTests(unittest.TestCase):
    def test_empty_and_multiple_independent_optional_targets(self):
        self.assertEqual(parse_group_replies('{"messages":[]}', set()), [])
        result = parse_group_replies(json.dumps({'messages': [
            {'text': '甲', 'reply_to': 'm1'}, {'text': '接着说'},
            {'text': '乙', 'reply_to': 'm2'}, {'text': '收尾', 'reply_to': None},
        ]}), {'m1', 'm2'})
        self.assertEqual(result, [GroupReply('甲', 'm1'), GroupReply('接着说'),
                                  GroupReply('乙', 'm2'), GroupReply('收尾')])

    def test_invalid_plan_is_rejected_in_full(self):
        invalid = ['不是JSON', '[]', '{"messages":null}', '{"messages":{},"debug":1}']
        invalid += [json.dumps({'messages': [{'text': '有效'}, item]}) for item in (
            {'text': '引用外群', 'reply_to': 'unknown'}, {'text': ''}, {'text': 123},
            {'text': 'a', 'reply_to': 1}, {'text': 'a', 'unexpected': 1}, 'hello',
        )]
        invalid += [json.dumps({'messages': [{'text': 'a'}] * 7})]
        for raw in invalid:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                parse_group_replies(raw, {'m1'})


class ContextTests(unittest.TestCase):
    def test_metadata_new_boundary_and_quote_ancestor(self):
        s = session.Session()
        s.add_group_event(event('old', '旧提问', at_list=['other']))
        s.finish_group_batch(1)
        s.add_group_reply('旧回答', 'old', 'bot-msg')
        s.add_group_event(event('new', '那这个呢', reply_to='bot-msg'))
        snapshot = build_group_snapshot(s)
        lines = [p['text'] for p in snapshot.messages[0]['content']][1:]
        records = [json.loads(line) for line in lines]
        self.assertEqual([r['范围'] for r in records], ['此前历史', '此前历史', '本轮新增'])
        self.assertEqual(records[0]['@'], ['other'])
        self.assertEqual(records[2]['引用'], 'bot-msg')
        self.assertIn('时间', records[0])
        self.assertEqual(snapshot.reply_ids, {'old', 'bot-msg', 'new'})

    def test_budget_splits_pending_without_losing_a_direct_question(self):
        s = session.Session()
        with patch.object(session, 'GROUP_HISTORY_MESSAGES', 2):
            for i in range(12):
                s.add_group_event(event(f'm{i}', '正文' * 200, is_at_bot=(i == 0)))
            self.assertEqual(len(s.messages), 12)
            covered = []
            with patch('core.group_context.GROUP_CONTEXT_CHARS', 2048):
                while s.group_cursor < s.group_seq:
                    snap = build_group_snapshot(s)
                    content = snap.messages[0]['content']
                    self.assertLessEqual(sum(len(p.get('text', '')) for p in content), 2048)
                    records = [json.loads(p['text']) for p in content[1:]]
                    covered.extend(r['id'] for r in records if r['范围'] == '本轮新增')
                    s.finish_group_batch(snap.through)
            self.assertEqual(covered, [f'm{i}' for i in range(12)])
            self.assertLessEqual(len(s.messages), 2)

    def test_messages_sent_before_a_later_bot_reply_are_marked(self):
        s = session.Session()
        s.add_group_event(event('q', '@猫猫 今天几号', 'a', is_at_bot=True))
        snap = build_group_snapshot(s)
        self.assertFalse(snap.directed_overlap)
        # 生成期间又有人问同一件事；回复发出后才处理下一批。
        s.group_inflight = snap.through
        s.add_group_event(event('again', '@猫猫 几号啊', 'b', is_at_bot=True))
        s.add_group_event(event('chat', '好饿', 'c'))
        s.add_group_reply('今天 11 月 2 日喵', 'q', 'bot-1')
        s.finish_group_batch(snap.through)
        s.add_group_event(event('after', '谢谢', 'a'))
        snap = build_group_snapshot(s)
        records = {r['id']: r for r in (json.loads(p['text']) for p in snap.messages[0]['content'][1:])}
        self.assertTrue(records['again']['先于BOT回复'])
        self.assertTrue(records['chat']['先于BOT回复'])
        self.assertNotIn('先于BOT回复', records['after'])
        self.assertNotIn('先于BOT回复', records['q'])
        self.assertTrue(snap.directed_overlap)

        s.add_group_event(event('fresh', '@猫猫 那明天呢', 'd', is_at_bot=True))
        self.assertFalse(build_group_snapshot(s).directed_overlap)

    def test_clear_invalidates_snapshot(self):
        s = session.Session()
        s.add_group_event(event())
        snap = build_group_snapshot(s)
        s.clear()
        self.assertFalse(group_chat._is_current(s, snap))
        self.assertIsNone(build_group_snapshot(s))

    def test_duplicate_incoming_id_is_not_a_second_event(self):
        s = session.Session()
        self.assertTrue(s.add_group_event(event()))
        self.assertFalse(s.add_group_event(event()))
        self.assertEqual(s.group_seq, 1)


class AsyncTasks(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.addAsyncCleanup(self.cleanup_tasks)

    async def cleanup_tasks(self):
        tasks = [p.task for p in coalesce._pending.values() if p.task]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        coalesce._pending.clear()


class CoalesceTests(AsyncTasks):
    async def test_slow_response_coalesces_all_new_inputs_once(self):
        started, release = asyncio.Event(), asyncio.Event()
        seen = []
        latest = 1
        async def respond():
            seen.append(latest)
            started.set()
            if len(seen) == 1:
                await release.wait()
        with patch.object(coalesce, 'GROUP_IDLE_DEBOUNCE', 0):
            coalesce.feed('g', respond, forced=False)
            worker = coalesce._pending['g'].task
            await started.wait()
            for i in range(2, 20):
                latest = i
                coalesce.feed('g', respond, forced=False)
                await asyncio.sleep(0)
                self.assertIs(coalesce._pending['g'].task, worker)
            release.set()
            await worker
        self.assertEqual(seen, [1, 19])
        self.assertFalse(coalesce.active('g'))

    async def test_continuous_traffic_has_a_maximum_wait(self):
        for forced in (False, True):
            with self.subTest(forced=forced):
                fired = asyncio.Event()
                async def respond():
                    fired.set()
                with patch.object(coalesce, 'REPLY_DEBOUNCE', 1), patch.object(coalesce, 'REPLY_MAX_WAIT', 0.03), \
                     patch.object(coalesce, 'GROUP_IDLE_DEBOUNCE', 1), patch.object(coalesce, 'GROUP_IDLE_MAX_WAIT', 0.03):
                    coalesce.feed('g', respond, forced=forced, sender='u')
                    async def flood():
                        while not fired.is_set():
                            await asyncio.sleep(0.005)
                            coalesce.feed('g', respond, forced=False, sender='u')
                    task = asyncio.create_task(flood())
                    await asyncio.wait_for(fired.wait(), 0.5)
                    await task
                    await coalesce._pending['g'].task

    async def test_others_chatting_does_not_delay_a_direct_reply_but_asker_follow_up_does(self):
        fired = []
        async def respond():
            fired.append(asyncio.get_running_loop().time())
        loop = asyncio.get_running_loop()
        with patch.object(coalesce, 'REPLY_DEBOUNCE', 0.05), patch.object(coalesce, 'REPLY_MAX_WAIT', 1), \
             patch.object(coalesce, 'GROUP_IDLE_DEBOUNCE', 1), patch.object(coalesce, 'GROUP_IDLE_MAX_WAIT', 1):
            # 先有闲聊按慢节奏等待；@ 到达后改从 @ 起按快节奏计时，旁人刷屏不再延长。
            coalesce.feed('g', respond, forced=False, sender='other')
            start = loop.time()
            coalesce.feed('g', respond, forced=True, sender='asker')
            for _ in range(10):
                await asyncio.sleep(0.02)
                coalesce.feed('g', respond, forced=False, sender='other')
            await coalesce._pending['g'].task
            self.assertLess(fired[0] - start, 0.15)

            fired.clear()
            coalesce.feed('g', respond, forced=True, sender='asker')
            start = loop.time()
            for _ in range(5):
                await asyncio.sleep(0.03)
                coalesce.feed('g', respond, forced=False, sender='asker')  # 提问者还在补充
            await coalesce._pending['g'].task
            self.assertGreaterEqual(fired[0] - start, 0.15 + 0.05)
        self.assertEqual(len(fired), 1)

    async def test_forced_responder_keeps_priority_and_other_groups_are_independent(self):
        forced, ambient, other = AsyncMock(), AsyncMock(), AsyncMock()
        with patch.object(coalesce, 'REPLY_DEBOUNCE', 0), patch.object(coalesce, 'GROUP_IDLE_DEBOUNCE', 0):
            coalesce.feed('g', forced, forced=True)
            coalesce.feed('g', ambient, forced=False)
            coalesce.feed('other', other, forced=False)
            await asyncio.gather(*(p.task for p in list(coalesce._pending.values())))
        forced.assert_awaited_once()
        ambient.assert_not_awaited()
        other.assert_awaited_once()


class GroupFlowTests(AsyncTasks):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.s = session.Session()
        self.bot = FakeBot()
        self.provider = Mock(chat=AsyncMock(return_value=answer()))
        for p in (
            patch.object(group_chat, 'get_session', return_value=self.s),
            patch.object(group_chat, 'get_provider', return_value=self.provider),
            patch.object(group_chat, 'has_tools', return_value=False),
            patch.object(group_chat, 'AMBIENT_CHAT', True),
            patch.object(group_chat, 'AMBIENT_GROUPS', ['g1']),
            patch.object(group_chat, 'AMBIENT_PROBABILITY', 1),
            patch.object(group_chat, 'FOLLOWUP_CHAT', False),
            patch.object(group_chat, 'GROUP_AMBIENT_COOLDOWN', 0),
            patch.object(group_chat, 'REPLY_MESSAGE_DELAY', 0),
            patch.object(coalesce, 'REPLY_DEBOUNCE', 0),
            patch.object(coalesce, 'GROUP_IDLE_DEBOUNCE', 0),
            patch.object(engagement, '_last_reply_at', {}),
        ):
            p.start()
            self.addCleanup(p.stop)

    async def drain(self):
        p = coalesce._pending.get('test_group_g1')
        if p:
            await asyncio.wait_for(p.task, 2)

    async def test_one_call_can_reply_to_multiple_people_and_without_quotes(self):
        self.provider.chat.return_value = answer([
            {'text': '给甲', 'reply_to': 'm1'}, {'text': '给乙', 'reply_to': 'm2'}, {'text': '自然接续'},
        ])
        await group_chat.handle_group_message(event('m1', user='a', is_at_bot=True), self.bot)
        await group_chat.handle_group_message(event('m2', user='b'), self.bot)
        await self.drain()
        self.provider.chat.assert_awaited_once()
        self.assertEqual(self.bot.sent, [('group', 'g1', '给甲', 'm1'),
                                        ('group', 'g1', '给乙', 'm2'), ('group', 'g1', '自然接续', None)])
        self.assertEqual(self.s.group_cursor, 2)
        self.assertEqual([m['content'] for m in self.s.messages[-3:]], ['给甲', '给乙', '自然接续'])

    async def test_skip_consumes_batch_without_assistant_or_engagement(self):
        self.provider.chat.return_value = llm.ChatResult('SKIP')
        await group_chat.handle_group_message(event(), self.bot)
        await self.drain()
        self.provider.chat.assert_awaited_once()
        self.assertIs(self.provider.chat.await_args.kwargs['thinking'], False)
        self.assertEqual(self.s.group_cursor, 1)
        self.assertEqual(len(self.s.messages), 1)
        self.assertEqual(self.bot.sent, [])
        self.assertFalse(engagement.in_window('test_group_g1', 150))
        await group_chat.handle_group_message(event('m2'), self.bot)
        await self.drain()
        payload = self.provider.chat.await_args.args[0]
        records = [json.loads(p['text']) for p in payload[-1]['content'][1:]]
        self.assertEqual([r['范围'] for r in records], ['此前历史', '本轮新增'])

    async def test_new_messages_during_generation_remain_new_and_history_uses_send_order(self):
        started, release = asyncio.Event(), asyncio.Event()
        async def slow(*args, **kwargs):
            if kwargs.get('thinking') is False:
                return llm.ChatResult('REPLY')
            started.set()
            await release.wait()
            return answer([{'text': '回答'}])
        self.provider.chat.side_effect = slow
        await group_chat.handle_group_message(event(is_at_bot=True), self.bot)
        await started.wait()
        await group_chat.handle_group_message(event('m2'), self.bot)
        await group_chat.handle_group_message(event('m3'), self.bot)
        release.set()
        await self.drain()
        self.assertEqual(self.provider.chat.await_count, 3)  # 正式回复、下一批判断、正式回复
        self.assertEqual([m.get('_group', {}).get('id') for m in self.s.messages[:4]], ['m1', 'm2', 'm3', None])
        second = self.provider.chat.await_args.args[0][-1]['content'][1:]
        new = [json.loads(p['text'])['id'] for p in second if json.loads(p['text'])['范围'] == '本轮新增']
        self.assertEqual(new, ['m2', 'm3'])

    async def test_each_batch_rebinds_tool_identity(self):
        started, release = asyncio.Event(), asyncio.Event()
        identities = []
        async def slow(*args, **kwargs):
            if kwargs.get('thinking') is False:
                return llm.ChatResult('REPLY')
            identities.append(runtime.get_event().user_id)
            started.set()
            await release.wait()
            return answer()
        self.provider.chat.side_effect = slow
        await group_chat.handle_group_message(event('m1', user='first', is_at_bot=True), self.bot)
        await started.wait()
        await group_chat.handle_group_message(event('m2', user='second'), self.bot)
        release.set()
        await self.drain()
        self.assertEqual(identities, ['first', 'second'])
        self.assertEqual(self.s.group_events, {})

    async def test_clear_between_output_bubbles_stops_remaining_messages(self):
        self.provider.chat.return_value = answer([{'text': '第一条'}, {'text': '不能再发'}])
        async def send(*args, **kwargs):
            self.s.clear()
            return 'bot-id'
        self.bot.send_group_msg = AsyncMock(side_effect=send)
        await group_chat.handle_group_message(event(is_at_bot=True), self.bot)
        await self.drain()
        self.bot.send_group_msg.assert_awaited_once()
        self.assertEqual(self.s.messages, [])

    async def test_followup_and_cooldown_are_signals_for_the_fast_decision(self):
        engagement.mark('test_group_g1')
        with patch.object(group_chat, 'FOLLOWUP_CHAT', True), patch.object(group_chat, 'GROUP_AMBIENT_COOLDOWN', 10):
            await group_chat.handle_group_message(event(), self.bot)
            await self.drain()
        prompt = self.provider.chat.await_args.args[0][0]['content']
        self.assertIn('最近有过回复：True', prompt)
        self.assertIn('本次允许主动插话：False', prompt)

    async def test_mention_preempts_inflight_gate_and_skips_oversized_ambient_backlog(self):
        started, cancelled = asyncio.Event(), asyncio.Event()
        direct_ids, dedup_ids = [], []

        async def respond(messages, **options):
            rows = [json.loads(p['text']) for p in messages[-1]['content'][1:]]
            new = [r['id'] for r in rows if r['指向bot'] and r['范围'] == '本轮新增']
            if options.get('thinking') is False:
                if not started.is_set():
                    started.set()
                    try:
                        await asyncio.Event().wait()
                    finally:
                        cancelled.set()
                # at1 与 at0 同时到达、只因预算分批；它排在 at0 的回复之前，先查重。
                dedup_ids.extend(new)
                return llm.ChatResult('REPLY')
            direct_ids.extend(new)
            return answer([{'text': runtime.get_event().user_id}])

        self.provider.chat.side_effect = respond
        with patch('core.group_context.GROUP_CONTEXT_CHARS', 2048):
            await group_chat.handle_group_message(event('ambient'), self.bot)
            await asyncio.wait_for(started.wait(), 1)
            for i in range(12):
                await group_chat.handle_group_message(event(f'bg{i}', '闲聊' * 200), self.bot)
            # 两个大提问各占一批，必须先处理最早的 @。
            for i in range(2):
                await group_chat.handle_group_message(event(f'at{i}', '提问' * 250, f'u{i}', is_at_bot=True), self.bot)
            await self.drain()
        self.assertTrue(cancelled.is_set())
        self.assertEqual(self.provider.chat.await_count, 4)  # 被打断的判断、at0、at1 查重、at1
        self.assertEqual(direct_ids, ['at0', 'at1'])
        self.assertEqual(dedup_ids, ['at1'])
        self.assertEqual([m[2] for m in self.bot.sent], ['u0', 'u1'])
        self.assertEqual(self.s.group_cursor, self.s.group_seq)

    async def test_gate_fails_quietly_and_releases_worker_on_timeout(self):
        async def stall(*args, **kwargs):
            await asyncio.Event().wait()
        self.provider.chat.side_effect = stall
        with patch.object(group_chat, 'GROUP_GATE_TIMEOUT', 0.01):
            await group_chat.handle_group_message(event('timeout'), self.bot)
            await self.drain()
        self.provider.chat.side_effect = [llm.ChatResult('REPLY', ok=False), llm.ChatResult('probably REPLY')]
        for mid in ('error', 'invalid'):
            await group_chat.handle_group_message(event(mid), self.bot)
            await self.drain()
        self.assertEqual(self.provider.chat.await_count, 3)
        self.assertFalse(self.bot.sent)
        self.assertIsNone(self.s.group_gate)
        self.assertEqual(self.s.group_cursor, self.s.group_seq)

    async def test_batch_probability_never_discards_a_direct_request(self):
        with patch.object(group_chat, 'AMBIENT_PROBABILITY', 0):
            await group_chat.handle_group_message(event('ambient'), self.bot)
            await group_chat.handle_group_message(event('direct', is_at_bot=True), self.bot)
            await self.drain()
        self.provider.chat.assert_awaited_once()

    async def test_clear_or_stop_while_generating_prevents_stale_send(self):
        for clear in (True, False):
            with self.subTest(clear=clear):
                self.s = session.Session()
                self.bot.sent.clear()
                started, release = asyncio.Event(), asyncio.Event()
                async def slow(*args, **kwargs):
                    started.set()
                    await release.wait()
                    return answer([{'text': '过时回答'}])
                self.provider.chat.side_effect = slow
                with patch.object(group_chat, 'get_session', return_value=self.s):
                    await group_chat.handle_group_message(event(is_at_bot=True), self.bot)
                    await started.wait()
                    if clear:
                        self.s.clear()
                    else:
                        self.s.add_group_event(event('cancel', '不用回了'))
                        self.provider.chat.side_effect = None
                        self.provider.chat.return_value = answer()
                    release.set()
                    await self.drain()
                self.assertEqual(self.bot.sent, [])

    async def test_invalid_output_and_api_failure_never_leak_protocol_or_error(self):
        for result in (llm.ChatResult('not JSON'), llm.ChatResult('[API error]', ok=False),
                       answer([{'text': '不要发', 'reply_to': 'unknown'}])):
            self.provider.chat.return_value = result
            await group_chat.handle_group_message(event(str(self.s.group_seq), is_at_bot=True), self.bot)
            await self.drain()
        self.assertEqual(self.bot.sent, [])
        self.assertEqual(self.s.group_cursor, self.s.group_seq)

    async def test_partial_send_failure_only_records_successful_messages(self):
        self.provider.chat.return_value = answer([{'text': '成功'}, {'text': '失败'}, {'text': '不应发送'}])
        self.bot.send_group_msg = AsyncMock(side_effect=['bot-1', RuntimeError('send failed')])
        await group_chat.handle_group_message(event(is_at_bot=True), self.bot)
        await self.drain()
        self.assertEqual(self.bot.send_group_msg.await_count, 2)
        self.assertEqual(self.s.messages[-1]['content'], '成功')
        self.assertEqual(self.s.messages[-1]['_group']['id'], 'bot-1')
        self.assertEqual(self.s.group_cursor, 1)

    async def test_reply_to_bot_is_direct_even_outside_ambient_groups(self):
        self.s.add_group_reply('旧回复', None, 'bot-1')
        with patch.object(group_chat, 'AMBIENT_CHAT', False):
            await group_chat.handle_group_message(event(reply_to='bot-1'), self.bot)
            await self.drain()
        self.provider.chat.assert_awaited_once()
        self.assertTrue(self.s.messages[-1]['_group']['directed'])

    async def test_outside_ambient_and_followup_only_records_background(self):
        with patch.object(group_chat, 'AMBIENT_CHAT', False):
            await group_chat.handle_group_message(event(), self.bot)
        self.assertFalse(coalesce.active('test_group_g1'))
        self.provider.chat.assert_not_awaited()
        self.assertEqual(self.s.group_cursor, 1)

    async def run_overlap(self, gate_result):
        """第一问生成期间有人重复提问；返回第二批的调用参数。"""
        started, release = asyncio.Event(), asyncio.Event()
        calls = []

        async def chat(messages, **options):
            calls.append((messages, options))
            if options.get('thinking') is False:
                if isinstance(gate_result, BaseException):
                    raise gate_result
                if gate_result is None:
                    await asyncio.Event().wait()
                return gate_result
            if len([c for c in calls if c[1].get('thinking') is not False]) == 1:
                started.set()
                await release.wait()
                return answer([{'text': '今天 11 月 2 日喵', 'reply_to': 'q'}])
            return answer([{'text': '再答一次'}])

        self.provider.chat.side_effect = chat
        await group_chat.handle_group_message(event('q', '今天几号', 'a', is_at_bot=True), self.bot)
        await started.wait()
        await group_chat.handle_group_message(event('again', '几号啊', 'b', is_at_bot=True), self.bot)
        release.set()
        await self.drain()
        self.assertEqual(self.s.group_cursor, self.s.group_seq)
        return calls[1:]

    async def test_repeat_question_sent_during_generation_is_deduplicated(self):
        rest = await self.run_overlap(llm.ChatResult('SKIP'))
        self.assertEqual(len(rest), 1)
        messages, options = rest[0]
        self.assertIs(options['thinking'], False)
        self.assertIn('上一条回复发出前发送', messages[0]['content'])
        rows = [json.loads(p['text']) for p in messages[-1]['content'][1:]]
        self.assertEqual([r['id'] for r in rows if r.get('先于BOT回复')], ['again'])
        self.assertEqual([m[2] for m in self.bot.sent], ['今天 11 月 2 日喵'])

    async def test_dedup_gate_failure_or_new_question_still_answers(self):
        for gate_result in (llm.ChatResult('REPLY'), llm.ChatResult('SKIP', ok=False),
                            llm.ChatResult('maybe'), RuntimeError('boom'), None):
            with self.subTest(gate=gate_result):
                self.s = session.Session()
                self.bot.sent.clear()
                with patch.object(group_chat, 'get_session', return_value=self.s), \
                     patch.object(group_chat, 'GROUP_GATE_TIMEOUT', 0.01):
                    rest = await self.run_overlap(gate_result)
                self.assertEqual([o.get('thinking') for _, o in rest], [False, None])
                self.assertEqual([m[2] for m in self.bot.sent], ['今天 11 月 2 日喵', '再答一次'])

    async def test_dedup_gate_can_be_disabled_and_skips_fresh_mentions(self):
        with patch.object(group_chat, 'GROUP_DEDUP_GATE', False):
            rest = await self.run_overlap(llm.ChatResult('SKIP'))
        self.assertEqual([o.get('thinking') for _, o in rest], [None])
        self.s.finish_group_batch(self.s.group_seq)
        self.provider.chat.reset_mock()
        self.provider.chat.side_effect = None
        self.provider.chat.return_value = answer([{'text': '明天 3 日'}])
        await group_chat.handle_group_message(event('fresh', '那明天呢', 'c', is_at_bot=True), self.bot)
        await self.drain()
        self.provider.chat.assert_awaited_once()
        self.assertIsNot(self.provider.chat.await_args.kwargs.get('thinking'), False)

    async def test_group_reply_uses_group_effort_and_keeps_a_stable_prompt_prefix(self):
        await group_chat.handle_group_message(event('m1', '问题一', is_at_bot=True), self.bot)
        await self.drain()
        await group_chat.handle_group_message(event('m2', '问题二', 'u2', is_at_bot=True), self.bot)
        await self.drain()
        first, second = (c.args[0][0]['content'] for c in self.provider.chat.await_args_list)
        self.assertEqual(self.provider.chat.await_args.kwargs['reasoning_effort'],
                         group_chat.GROUP_REASONING_EFFORT)
        stable = first.index(GROUP_DECISION_PROMPT) + len(GROUP_DECISION_PROMPT)
        self.assertEqual(first[:stable], second[:stable])
        self.assertGreater(first.index('本批包含直接互动'), stable)

    async def test_tool_results_survive_failed_final_output_without_becoming_bot_speech(self):
        self.provider.chat.return_value = llm.ChatResult('', [
            {'role': 'assistant', 'tool_calls': [{'id': 't', 'function': {'name': 'remember'}}]},
            {'role': 'tool', 'tool_call_id': 't', 'content': '记下了'},
        ], ok=False)
        await group_chat.handle_group_message(event(is_at_bot=True), self.bot)
        await self.drain()
        self.assertEqual(self.bot.sent, [])
        self.assertEqual(self.s.messages[-1]['role'], 'tool')
        self.assertIn('remember: 记下了', self.s.messages[-1]['content'])


if __name__ == '__main__':
    unittest.main()
