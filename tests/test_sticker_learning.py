import asyncio
import json
import tempfile
import unittest
from collections import OrderedDict
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from core import images, llm, sticker_learning as learning, stickers, storage
from core.bot import ImageInfo, ImageSeg
from core.event import MessageEvent
from core.plugin import MatchResult
from tests.test_multimodal import PNG


KEEP = {
    'is_sticker': True, 'should_save': True, 'meaning': '开心庆祝',
    'comment': '猫咪举杯，表达开心和祝贺，适合在朋友完成目标时发送。',
    'tags': ['开心', '祝贺', '举杯'], 'reason': '可重复用于聊天回应',
}
SKIP = {'is_sticker': False, 'should_save': False, 'reason': '普通截图，不适合当表情包'}


def answer(data=KEEP, *, ok=True):
    return llm.ChatResult(json.dumps(data, ensure_ascii=False), ok=ok)


class StickerLearningTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await learning.close()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.provider = Mock(chat=AsyncMock(return_value=answer()))
        for p in (
            patch.object(storage, 'DATA_DIR', self.tmp.name),
            patch.object(stickers, '_img_dir', str(self.root / 'stickers')),
            patch.object(stickers, '_stickers', {}),
            patch.object(images, '_cache_dir', self.root / 'images'),
            patch.object(images, '_registry', OrderedDict()),
            patch.object(learning, '_decisions', {}),
            patch.object(learning, 'STICKER_LEARN', True),
            patch.object(learning, 'STICKER_BACKFILL_LEGACY', False),
            patch.object(learning, 'get_provider', return_value=self.provider),
        ):
            p.start()
            self.addCleanup(p.stop)
        self.addAsyncCleanup(learning.close)

    def image(self, key='new-sticker'):
        return ImageInfo(key=key, fetch=AsyncMock(return_value=PNG))

    def old(self, key='legacy-001', meaning=None, **extra):
        path = self.root / 'stickers' / f'{key}.png'
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(PNG)
        item = dict(key=key, path=str(path), url='https://expired.example/image', meaning=meaning,
                    summary='旧平台摘要', group_id='old-group', ts=123, **extra)
        stickers._stickers[key] = item
        return item

    async def idle(self):
        await asyncio.wait_for(learning.wait_idle(), timeout=3)

    async def test_selected_image_saved_with_annotation_and_native_image_request(self):
        image = self.image()
        self.assertTrue(learning.enqueue(image, 'group-a', '庆祝一下'))
        await self.idle()
        item = stickers.get(image.key)
        self.assertEqual(item['meaning'], KEEP['meaning'])
        self.assertEqual(item['comment'], KEEP['comment'])
        self.assertEqual(item['tags'], KEEP['tags'])
        self.assertEqual(item['source'], 'auto')
        self.assertEqual(item['group_id'], 'group-a')
        self.assertEqual(Path(item['path']).read_bytes(), PNG)
        image.fetch.assert_awaited_once()
        call = self.provider.chat.await_args
        self.assertIsNone(call.kwargs['tools'])
        self.assertFalse(call.kwargs['thinking'])
        self.assertEqual(call.kwargs['response_format'], {'type': 'json_object'})
        self.assertEqual(call.args[0][1]['content'][1]['type'], 'image_url')
        saved = json.loads((self.root / 'stickers.json').read_text())
        self.assertEqual(saved[image.key]['comment'], KEEP['comment'])
        self.assertEqual(stickers.find('举杯')['key'], image.key)
        self.assertEqual(stickers.find('朋友完成目标')['key'], image.key)
        self.assertIn(KEEP['comment'], stickers.available_meanings())

    async def test_photo_and_unwanted_sticker_are_not_collected(self):
        for i, data in enumerate((SKIP, dict(SKIP, is_sticker=True))):
            self.provider.chat.return_value = answer(data)
            image = self.image(f'ignored-{i}')
            self.assertTrue(learning.enqueue(image))
            await self.idle()
            self.assertIsNone(stickers.get(image.key))
            self.assertEqual(learning._decisions[image.key]['status'], 'skipped')
            self.assertFalse(learning.enqueue(image))
        self.assertFalse((self.root / 'stickers').exists())

    async def test_rejection_cache_persists_and_expires(self):
        self.provider.chat.return_value = answer(SKIP)
        image = self.image()
        learning.enqueue(image)
        await self.idle()
        learning._decisions.clear()
        learning._decisions.update(storage.load_json('sticker_decisions.json', {}))
        self.assertFalse(learning.enqueue(image))
        learning._decisions[image.key]['ts'] = 0
        self.provider.chat.return_value = answer()
        self.assertTrue(learning.enqueue(image))
        await self.idle()
        self.assertEqual(self.provider.chat.await_count, 2)
        self.assertIsNotNone(stickers.get(image.key))
        self.assertNotIn(image.key, learning._decisions)

    async def test_malformed_response_never_creates_an_unannotated_entry(self):
        bad = [
            {'is_sticker': 'true', 'should_save': True},
            dict(KEEP, comment=''), dict(KEEP, tags=[]),
            dict(KEEP, tags=['好', 1]), dict(KEEP, is_sticker=False),
        ]
        for i, data in enumerate(bad):
            key = f'bad-{i}'
            self.provider.chat.return_value = answer(data)
            learning.enqueue(self.image(key))
            await self.idle()
            self.assertIsNone(stickers.get(key))
            self.assertEqual(learning._decisions[key]['status'], 'failed')
            self.assertFalse(learning.enqueue(self.image(key)))
        self.assertFalse((self.root / 'stickers').exists())

    async def test_api_failure_can_retry_after_backoff(self):
        image = self.image()
        self.provider.chat.return_value = answer(ok=False)
        learning.enqueue(image)
        await self.idle()
        self.assertFalse(learning.enqueue(image))
        learning._decisions[image.key]['ts'] = 0
        self.provider.chat.return_value = answer()
        self.assertTrue(learning.enqueue(image))
        await self.idle()
        self.assertIsNotNone(stickers.get(image.key))

    async def test_download_failure_does_not_call_model_or_store(self):
        image = self.image()
        image.fetch.side_effect = RuntimeError('expired')
        learning.enqueue(image)
        await self.idle()
        self.provider.chat.assert_not_awaited()
        self.assertIsNone(stickers.get(image.key))
        self.assertEqual(learning._decisions[image.key]['status'], 'failed')

    async def test_saved_bytes_are_deduplicated_across_different_platform_keys(self):
        learning.enqueue(self.image('qq-file'))
        await self.idle()
        learning.enqueue(self.image('img_v3_feishu'))
        await self.idle()
        self.provider.chat.assert_awaited_once()
        self.assertEqual(len(stickers._stickers), 1)
        self.assertIn('相同原图', learning._decisions['img_v3_feishu']['reason'])

    async def test_inflight_deduplication_and_bounded_queue(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def chat(*args, **kwargs):
            entered.set()
            await release.wait()
            return answer()
        self.provider.chat.side_effect = chat
        with patch.object(learning, 'STICKER_QUEUE_MAX', 1):
            image = self.image('one')
            learning.enqueue(image)
            await entered.wait()
            self.assertFalse(learning.enqueue(image))
            self.assertTrue(learning.enqueue(self.image('two')))
            self.assertFalse(learning.enqueue(self.image('three')))
            self.assertNotIn('three', learning._pending)
            release.set()
            await self.idle()
        self.assertEqual(learning._pending, set())

    async def test_existing_manual_meaning_and_legacy_shape_are_preserved(self):
        item = self.old(meaning='主人原来教的含义', extra_metadata={'keep': True})
        stickers.save()
        stickers.load()
        with patch.object(learning, 'STICKER_BACKFILL_LEGACY', True):
            learning.start()
            await self.idle()
        self.provider.chat.assert_not_awaited()
        self.assertEqual(stickers.get(item['key']), item)
        self.assertEqual(stickers.find('原来教的')['path'], item['path'])
        self.assertIn('主人原来教的含义', stickers.available_meanings())

    async def test_startup_backfills_old_unknown_image_without_replacing_file(self):
        item = self.old(extra_metadata='keep')
        old_path = item['path']
        with patch.object(learning, 'STICKER_BACKFILL_LEGACY', True):
            learning.start()
            await self.idle()
        self.provider.chat.assert_awaited_once()
        annotated = stickers.get(item['key'])
        self.assertEqual(annotated['meaning'], KEEP['meaning'])
        self.assertEqual(annotated['comment'], KEEP['comment'])
        self.assertEqual(annotated['path'], old_path)
        self.assertEqual(annotated['ts'], 123)
        self.assertEqual(annotated['group_id'], 'old-group')
        self.assertEqual(annotated['extra_metadata'], 'keep')
        self.assertEqual(Path(old_path).read_bytes(), PNG)
        self.assertEqual(stickers.pending(), [])

    async def test_legacy_non_sticker_is_marked_but_never_deleted(self):
        item = self.old()
        self.provider.chat.return_value = answer(SKIP)
        with patch.object(learning, 'STICKER_BACKFILL_LEGACY', True):
            learning.start()
            await self.idle()
        self.assertTrue(Path(item['path']).is_file())
        self.assertIsNone(stickers.meaning_of(item['key']))
        self.assertEqual(stickers.get(item['key'])['auto_review'], 'skipped')
        self.assertEqual(stickers.legacy_pending(), [])
        self.assertEqual(len(stickers.pending()), 1)

    async def test_relocated_legacy_file_is_used_without_remote_download(self):
        item = self.old()
        name = Path(item['path']).name
        item['path'] = '/old/server/stickers/' + name
        with patch.object(learning, 'STICKER_BACKFILL_LEGACY', True):
            learning.start()
            await self.idle()
        self.assertEqual(stickers.path_of(item['key']), str(self.root / 'stickers' / name))
        self.assertEqual(stickers.meaning_of(item['key']), KEEP['meaning'])

    async def test_manual_edit_during_analysis_wins(self):
        item = self.old()
        async def chat(*args, **kwargs):
            self.assertTrue(stickers.set_meaning('legacy-', '人工修正'))
            return answer()
        self.provider.chat.side_effect = chat
        learning.enqueue(stickers.image_for_record(item))
        await self.idle()
        self.assertEqual(stickers.meaning_of(item['key']), '人工修正')
        self.assertEqual(stickers.get(item['key'])['source'], 'manual')
        self.assertNotIn('comment', stickers.get(item['key']))

    async def test_deleted_legacy_entry_is_not_resurrected_by_background_model(self):
        item = self.old()
        async def chat(*args, **kwargs):
            self.assertEqual(stickers.forget(item['key']), 1)
            return answer()
        self.provider.chat.side_effect = chat
        learning.enqueue(stickers.image_for_record(item))
        await self.idle()
        self.assertIsNone(stickers.get(item['key']))
        self.assertFalse(Path(item['path']).exists())

    async def test_old_short_ids_and_new_ids_both_work_without_ambiguous_match(self):
        first = self.old(key='old_unique_a', meaning='旧含义')
        self.assertTrue(stickers.set_meaning('old_uni', '修正后的旧含义'))
        self.old(key='img_v3_common_a')
        second = self.old(key='img_v3_common_b')
        self.assertFalse(stickers.set_meaning('img_v3_', '不能随便选第一张'))
        self.assertTrue(stickers.set_meaning(stickers.display_id(second['key']), '精确修正'))
        self.assertEqual(stickers.meaning_of(second['key']), '精确修正')
        self.assertEqual(stickers.find(stickers.display_id(first['key']))['key'], first['key'])

    async def test_disk_failure_does_not_leave_a_fake_saved_entry(self):
        with patch.object(stickers, 'save', side_effect=OSError('disk full')):
            learning.enqueue(self.image())
            await self.idle()
        self.assertEqual(stickers._stickers, {})
        self.assertEqual(list((self.root / 'stickers').glob('*.png')), [])
        self.assertEqual(learning._decisions['new-sticker']['status'], 'failed')

    async def test_disabled_learning_neither_queues_nor_backfills(self):
        self.old()
        with patch.object(learning, 'STICKER_LEARN', False), patch.object(learning, 'STICKER_BACKFILL_LEGACY', True):
            learning.start()
            self.assertFalse(learning.enqueue(self.image()))
        self.assertIsNone(learning._worker_task)
        self.provider.chat.assert_not_awaited()

    async def test_capture_learns_without_master_account_or_outgoing_messages(self):
        from plugins import stickers as plugin
        bot = Mock(reply=AsyncMock(), send_private_msg=AsyncMock(), send_image=AsyncMock())
        event = MessageEvent(platform='onebot', message_type='group', user_id='u1', group_id='g1',
                             self_id='bot', text='哈哈', images=[self.image()])
        with patch.object(plugin, 'AMBIENT_GROUPS', ['g1']), patch.object(plugin, 'STICKER_LEARN', True):
            self.assertTrue(await plugin.capture_stickers(event, bot))
            await self.idle()
        self.assertIsNotNone(stickers.get('new-sticker'))
        self.assertEqual(bot.mock_calls, [])

    async def test_capture_keeps_existing_group_scope_and_ignores_self(self):
        from plugins import stickers as plugin
        with patch.object(plugin, 'AMBIENT_GROUPS', ['g1']), patch.object(plugin.sticker_learning, 'enqueue') as enqueue:
            for kind, group, user in [('private', 'g1', 'u1'), ('group', 'g2', 'u1'), ('group', 'g1', 'bot')]:
                event = MessageEvent(platform='onebot', message_type=kind, user_id=user, group_id=group,
                                     self_id='bot', images=[self.image()])
                self.assertTrue(await plugin.capture_stickers(event, Mock()))
        enqueue.assert_not_called()

    async def test_pending_command_is_a_local_status_view_not_a_request_to_teach(self):
        from plugins import stickers as plugin
        item = self.old()
        bot = Mock(reply=AsyncMock(), send_private_msg=AsyncMock())
        event = MessageEvent(platform='onebot', message_type='private', user_id='master')
        await plugin.list_pending.__wrapped__(event, bot, MatchResult())
        bot.send_private_msg.assert_not_awaited()
        parts = bot.reply.await_args.args[1]
        self.assertIsInstance(parts[0], ImageSeg)
        self.assertEqual(parts[0].file, item['path'])
        self.assertNotIn('/meaning', str(parts))

    async def test_selection_tools_show_annotations_and_send_existing_file(self):
        from tools import sticker
        learning.enqueue(self.image())
        await self.idle()
        listing = await sticker.list_stickers()
        self.assertIn(KEEP['comment'], listing)
        self.assertIn('举杯', listing)
        bot = Mock(send_image=AsyncMock())
        event = object()
        with patch.object(sticker, 'get_bot', return_value=bot), patch.object(sticker, 'get_event', return_value=event):
            await sticker.send_sticker('举杯')
            missing = await sticker.send_sticker('完全不存在的检索词')
        bot.send_image.assert_awaited_once_with(event, stickers.path_of('new-sticker'))
        self.assertNotIn('主人', missing)


if __name__ == '__main__':
    unittest.main()
