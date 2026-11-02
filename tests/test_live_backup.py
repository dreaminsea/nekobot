import asyncio
from collections import defaultdict
import errno
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import threading
import unittest
import venv
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import zipfile

from core import backup, contacts, live_backup, maintenance, memory, perms, plugin, session, sticker_learning, stickers, storage, timeline
from core.event import MessageEvent
from core.maintenance import acquire_lock, held_by_this_process
from core.plugin import MatchResult
from plugins import admin


class LiveBackupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.source = self.repo('source')
        self.target = self.repo('target')
        self.archive = self.base / 'live.zip'
        self.data = self.source / 'data'
        self.write(self.source, '.env', 'TOKEN=test-private-token')
        self.write(self.source, 'data/sessions.json', '{"old":"disk state"}')
        self.picture = self.write(self.source, 'data/stickers/cat.png', b'\x89PNG\r\n\x1a\ncat')
        self.write(self.source, 'data/images/recent.bin', b'recent cached image')
        self.write(self.source, 'data/stickers.json', 'broken disk index; memory is authoritative')
        self.write(self.source, 'data/napcat/config.json', '{"port":3001}')
        for context in (
            patch.object(storage, 'DATA_DIR', str(self.data)),
            patch.object(stickers, '_img_dir', str(self.data / 'stickers')),
            patch.object(stickers, '_stickers', {'cat': {'key': 'cat', 'path': str(self.picture), 'meaning': '开心'}}),
            patch.object(memory, '_memories', [{'content': '当前记忆', 'ts': 1}]),
            patch.object(contacts, '_contacts', {'friend': {'name': 'friend', 'id': '42'}}),
            patch.object(sticker_learning, '_decisions', {'skip': {'status': 'skipped'}}),
            patch.object(session, 'sessions', defaultdict(session.Session)),
            patch.object(session, 'SESSION_PERSIST', False),
            patch.object(session, '_dirty', False),
            patch.object(timeline, '_data', {'version': 1, 'experiences': [], 'current_state': None}),
            patch.object(timeline, '_dirty', False),
            patch.object(timeline, '_writable', True),
        ):
            context.start()
            self.addCleanup(context.stop)
        session.sessions['onebot_private_42'].add_user('刚收到但还没落盘的消息')
        timeline.set_state(MessageEvent(platform='onebot', message_type='private', user_id='42'), '尚未落盘的状态')
        timeline.set_work({
            'id': 'work-id', 'name': '离线工作', 'goal': '测试快照', 'status': 'paused',
            'step': 1, 'total': 3, 'timeout': 300.0, 'result': '第一步完成',
            'created_at': 1.0, 'updated_at': 2.0,
            'source': {'platform': 'onebot', 'message_type': 'private', 'user_id': '42',
                       'group_id': '0', 'message_id': 'm1', 'session_key': 'onebot_private_42'},
        })

    def repo(self, name):
        root = self.base / name
        root.mkdir()
        subprocess.run(['git', 'init', '-q', str(root)], check=True)
        self.write(root, 'config.py', 'DATA_DIR = "data"\nSTICKER_DIR = "stickers"\n')
        self.write(root, 'bot.py', '# repository source')
        subprocess.run(['git', '-C', str(root), 'add', '.'], check=True)
        return root

    def write(self, root, name, content):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content if isinstance(content, bytes) else content.encode())
        return path

    def payload(self, name):
        with zipfile.ZipFile(self.archive) as archive:
            return archive.read('files/' + name)

    async def test_in_memory_state_exported_even_when_persistence_disabled(self):
        previous = (self.data / 'sessions.json').read_bytes()
        with acquire_lock(self.source):
            path, manifest = await live_backup.export_running(self.source, self.archive)
            with self.assertRaises(RuntimeError):
                backup.restore_backup(self.source, self.archive)
        self.assertEqual(path, self.archive)
        self.assertEqual(manifest['snapshot']['mode'], 'live')
        self.assertEqual(backup.inspect_backup(path), manifest)
        history = json.loads(self.payload('data/sessions.json'))
        self.assertEqual(history['onebot_private_42']['messages'][0]['content'], '刚收到但还没落盘的消息')
        self.assertEqual(json.loads(self.payload('data/memories.json'))[0]['content'], '当前记忆')
        self.assertIn('friend', json.loads(self.payload('data/contacts.json')))
        self.assertIn('skip', json.loads(self.payload('data/sticker_decisions.json')))
        self.assertEqual(json.loads(self.payload('data/agent_state.json'))['current_state']['note'], '尚未落盘的状态')
        self.assertEqual(json.loads(self.payload('data/agent_state.json'))['work']['step'], 1)
        self.assertFalse((self.data / 'agent_state.json').exists())
        self.assertEqual((self.data / 'sessions.json').read_bytes(), previous)
        self.assertFalse(session.SESSION_PERSIST)
        self.assertTrue(session._dirty)
        backup.restore_backup(self.target, path)
        self.assertEqual(json.loads((self.target / 'data/agent_state.json').read_text())['current_state']['note'], '尚未落盘的状态')
        self.assertEqual(json.loads((self.target / 'data/agent_state.json').read_text())['work']['source']['session_key'], 'onebot_private_42')
        record = json.loads((self.target / 'data/stickers.json').read_text())['cat']
        self.assertEqual(record['path'], str(self.target / 'data/stickers/cat.png'))
        self.assertEqual(Path(record['path']).read_bytes(), self.picture.read_bytes())
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertFalse(list(self.source.glob('.nekobot-live-*')))

    async def test_live_export_with_virtualenv_in_repository_root(self):
        venv.EnvBuilder(with_pip=False, symlinks=True).create(self.source)
        with acquire_lock(self.source):
            path, manifest = await live_backup.export_running(self.source, self.archive)
        self.assertEqual(backup.inspect_backup(path), manifest)
        names = {e['path'] for e in manifest['entries']}
        self.assertNotIn('bin/python', names)
        self.assertNotIn('pyvenv.cfg', names)
        history = json.loads(self.payload('data/sessions.json'))
        self.assertEqual(history['onebot_private_42']['messages'][0]['content'], '刚收到但还没落盘的消息')
        self.assertEqual(self.payload('data/stickers/cat.png'), self.picture.read_bytes())
        self.assertTrue((self.source / 'bin/python').is_symlink())
        self.assertFalse(live_backup.running())

    async def test_chat_mutations_and_picture_deletion_can_continue_during_export(self):
        entered = threading.Event()
        release = threading.Event()
        finish = live_backup._finish

        def wait_then_finish(snapshot, output):
            entered.set()
            release.wait(5)
            return finish(snapshot, output)

        with acquire_lock(self.source), patch.object(live_backup, '_finish', side_effect=wait_then_finish):
            task = asyncio.create_task(live_backup.export_running(self.source, self.archive))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 3))
                # 导出等待工作线程时，事件循环仍能接收消息和完成记忆/表情操作。
                session.sessions['onebot_private_42'].add_user('快照之后的新消息')
                memory.add('快照之后的新记忆')
                work = timeline.work_snapshot()
                timeline.set_work({**work, 'step': 2, 'result': '快照之后的新进度'})
                self.assertEqual(stickers.forget('cat'), 1)
                self.assertFalse(self.picture.exists())
                self.assertTrue(live_backup.running())
                with self.assertRaises(live_backup.BackupBusy):
                    await live_backup.export_running(self.source)
            finally:
                release.set()
                await task
        history = json.loads(self.payload('data/sessions.json'))
        self.assertEqual(len(history['onebot_private_42']['messages']), 1)
        self.assertEqual(len(json.loads(self.payload('data/memories.json'))), 1)
        self.assertIn('cat', json.loads(self.payload('data/stickers.json')))
        self.assertEqual(self.payload('data/stickers/cat.png'), b'\x89PNG\r\n\x1a\ncat')
        self.assertEqual(len(session.sessions['onebot_private_42'].messages), 2)
        self.assertEqual(timeline.work_snapshot()['step'], 2)
        self.assertEqual(json.loads(self.payload('data/agent_state.json'))['work']['step'], 1)
        self.assertFalse(live_backup.running())

    async def test_sqlite_online_backup_includes_committed_wal_and_excludes_uncommitted_rows(self):
        db_path = self.data / 'napcat/login.db'
        connection = sqlite3.connect(db_path)
        self.addCleanup(connection.close)
        connection.execute('PRAGMA journal_mode=WAL')
        connection.execute('PRAGMA wal_autocheckpoint=0')
        connection.execute('CREATE TABLE messages (text TEXT)')
        connection.execute('INSERT INTO messages VALUES (?)', ('已提交到 WAL',))
        connection.commit()
        connection.execute('INSERT INTO messages VALUES (?)', ('尚未提交',))
        self.assertTrue(Path(str(db_path) + '-wal').exists())
        with acquire_lock(self.source):
            _, manifest = await live_backup.export_running(self.source, self.archive)
        self.assertEqual(manifest['snapshot']['sqlite_databases'], ['data/napcat/login.db'])
        names = {e['path'] for e in manifest['entries']}
        self.assertNotIn('data/napcat/login.db-wal', names)
        self.assertNotIn('data/napcat/login.db-shm', names)
        restored = self.write(self.base, 'restored.db', self.payload('data/napcat/login.db'))
        with sqlite3.connect(restored) as reader:
            self.assertEqual(reader.execute('SELECT text FROM messages').fetchall(), [('已提交到 WAL',)])
            self.assertEqual(reader.execute('PRAGMA integrity_check').fetchone(), ('ok',))
        connection.rollback()
        self.assertEqual(connection.execute('SELECT text FROM messages').fetchall(), [('已提交到 WAL',)])

    async def test_cross_filesystem_fallback_keeps_deleted_picture_via_open_descriptor(self):
        link = os.link
        entered = threading.Event()
        release = threading.Event()
        snapshots = []
        finish = live_backup._finish

        def cross_device(source, target, **kwargs):
            if '.nekobot-live-' in str(target):
                raise OSError(errno.EXDEV, 'cross-device link')
            return link(source, target, **kwargs)

        def wait_then_finish(snapshot, output):
            snapshots.append(snapshot)
            entered.set()
            release.wait(5)
            return finish(snapshot, output)

        with acquire_lock(self.source), patch.object(live_backup.os, 'link', side_effect=cross_device), \
                patch.object(live_backup, '_finish', side_effect=wait_then_finish):
            task = asyncio.create_task(live_backup.export_running(self.source, self.archive))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 3))
                self.picture.unlink()
            finally:
                release.set()
                await task
        self.assertEqual(self.payload('data/stickers/cat.png'), b'\x89PNG\r\n\x1a\ncat')
        self.assertTrue(all(item.fd is None for item in snapshots[0].files))
        self.assertFalse(snapshots[0].root.exists())

    async def test_changing_external_file_fails_cleanly_and_next_export_can_run(self):
        copy = live_backup._copy_stable
        read = os.read
        source = self.source / '.env'

        def changing_read(fd, size):
            chunk = read(fd, size)
            with source.open('ab') as writer:
                writer.write(b'concurrent update')
            return chunk

        def unstable(item, target):
            if item.entry.path == '.env':
                with patch.object(live_backup.os, 'read', side_effect=changing_read):
                    return copy(item, target)
            return copy(item, target)

        with acquire_lock(self.source):
            with patch.object(live_backup, '_copy_stable', side_effect=unstable):
                with self.assertRaisesRegex(live_backup.SnapshotChanged, '持续变化'):
                    await live_backup.export_running(self.source, self.archive)
            self.assertFalse(self.archive.exists())
            self.assertFalse(live_backup.running())
            self.assertFalse(list(self.source.glob('.nekobot-live-*')))
            await live_backup.export_running(self.source, self.archive)
        backup.inspect_backup(self.archive)

    async def test_cancelled_caller_does_not_remove_working_snapshot_or_unlock_early(self):
        entered = threading.Event()
        release = threading.Event()
        finished = threading.Event()
        finish = live_backup._finish

        def wait_then_finish(snapshot, output):
            entered.set()
            release.wait(5)
            try:
                return finish(snapshot, output)
            finally:
                finished.set()

        with acquire_lock(self.source), patch.object(live_backup, '_finish', side_effect=wait_then_finish):
            task = asyncio.create_task(live_backup.export_running(self.source, self.archive))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 3))
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertTrue(live_backup.running())
                with self.assertRaises(live_backup.BackupBusy):
                    await live_backup.export_running(self.source)
            finally:
                release.set()
                self.assertTrue(await asyncio.to_thread(finished.wait, 3))
        self.assertFalse(live_backup.running())
        backup.inspect_backup(self.archive)
        self.assertFalse(list(self.source.glob('.nekobot-live-*')))

    async def test_lock_ownership_and_capture_failure_release_job_guard(self):
        self.assertFalse(held_by_this_process(self.source))
        with self.assertRaisesRegex(backup.BackupError, '维护锁'):
            await live_backup.export_running(self.source, self.archive)
        self.assertFalse(live_backup.running())
        with acquire_lock(self.source):
            self.assertTrue(held_by_this_process(self.source))
            with patch.object(live_backup, '_runtime_state', side_effect=OSError('simulated failure')):
                with self.assertRaises(OSError):
                    await live_backup.export_running(self.source, self.archive)
            self.assertFalse(live_backup.running())
            await live_backup.export_running(self.source, self.archive)
        self.assertFalse(held_by_this_process(self.source))

    async def test_old_running_modules_receive_a_restart_hint(self):
        with acquire_lock(self.source), patch.dict(maintenance.__dict__):
            maintenance.__dict__.pop('held_by_this_process')
            with self.assertRaisesRegex(backup.BackupError, '重启'):
                await live_backup.export_running(self.source, self.archive)
        with acquire_lock(self.source), patch.dict(session.__dict__):
            session.__dict__.pop('snapshot')
            with self.assertRaisesRegex(backup.BackupError, '/restart'):
                await live_backup.export_running(self.source, self.archive)
        self.assertFalse(live_backup.running())

    async def test_snapshot_copy_failure_preserves_original_file_permissions(self):
        original_mode = self.picture.stat().st_mode & 0o777
        copy = live_backup._copy_stable

        def fail(item, target):
            if item.entry.path == 'data/stickers/cat.png':
                with patch.object(live_backup.tempfile, 'mkstemp', side_effect=OSError('disk full')):
                    return copy(item, target)
            return copy(item, target)

        with acquire_lock(self.source), patch.object(live_backup, '_copy_stable', side_effect=fail):
            with self.assertRaisesRegex(OSError, 'disk full'):
                await live_backup.export_running(self.source, self.archive)
        self.assertEqual(self.picture.stat().st_mode & 0o777, original_mode)
        self.assertEqual(self.picture.read_bytes(), b'\x89PNG\r\n\x1a\ncat')
        self.assertEqual(self.picture.stat().st_nlink, 1)
        self.assertFalse(list(self.source.glob('.nekobot-live-*')))
        self.assertFalse(live_backup.running())

    async def test_database_timeout_and_plain_non_sqlite_databases(self):
        self.write(self.source, 'data/napcat/opaque.db', b'not SQLite; preserve raw bytes')
        self.write(self.source, 'data/napcat/opaque.db-wal', b'opaque sidecar')
        with acquire_lock(self.source):
            await live_backup.export_running(self.source, self.archive)
        self.assertEqual(self.payload('data/napcat/opaque.db-wal'), b'opaque sidecar')
        self.archive.unlink()
        with sqlite3.connect(self.data / 'napcat/actual.db') as db:
            db.execute('CREATE TABLE data (value)')
        with acquire_lock(self.source), patch.object(live_backup, 'SQLITE_TIMEOUT', -1):
            with self.assertRaisesRegex(live_backup.SnapshotChanged, '超时'):
                await live_backup.export_running(self.source, self.archive)
        self.assertFalse(self.archive.exists())
        self.assertFalse(live_backup.running())


class BackupCommandTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.bot = SimpleNamespace(reply=AsyncMock())
        self.event = MessageEvent(platform='onebot', message_type='private', user_id='owner', text='/backup')
        self.master = patch.object(perms, 'MASTER_QQ', 'owner')
        self.master.start()
        self.addCleanup(self.master.stop)

    async def test_master_command_reports_real_path_and_aliases_are_private_only(self):
        result = (Path('/local/backups/result.zip'), {'entries': [{'kind': 'file'}, {'kind': 'directory'}]})
        with patch.object(live_backup, 'export_running', new=AsyncMock(return_value=result)) as export:
            await admin.backup_cmd(self.event, self.bot, MatchResult())
            export.assert_awaited_once()
        message = self.bot.reply.await_args.args[1]
        self.assertIn('备份完成：1 个文件', message)
        self.assertIn('/local/backups/result.zip', message)
        matcher = next(m for m in plugin._matchers if m.handler is admin.backup_cmd)
        for alias in ('/backup', '/export'):
            self.assertIsNotNone(matcher.check(MessageEvent(platform='onebot', message_type='private', user_id='owner', text=alias)))
            self.assertIsNone(matcher.check(MessageEvent(platform='onebot', message_type='group', user_id='owner', text=alias, is_at_bot=True)))

    async def test_non_master_and_invalid_arguments_never_start_export(self):
        with patch.object(live_backup, 'export_running', new=AsyncMock()) as export:
            stranger = MessageEvent(platform='onebot', message_type='private', user_id='stranger', text='/backup')
            await admin.backup_cmd(stranger, self.bot, MatchResult())
            self.assertIn('只有主人', self.bot.reply.await_args.args[1])
            await admin.backup_cmd(self.event, self.bot, MatchResult(text='/arbitrary/path'))
            self.assertIn('用法', self.bot.reply.await_args.args[1])
            export.assert_not_awaited()

    async def test_busy_request_and_restart_do_not_interrupt_active_export(self):
        with patch.object(live_backup, 'running', return_value=True), \
                patch.object(live_backup, 'export_running', new=AsyncMock()) as export, \
                patch.object(admin.os, 'execve') as restart:
            await admin.backup_cmd(self.event, self.bot, MatchResult())
            await admin.restart_cmd(self.event, self.bot, MatchResult())
            export.assert_not_awaited()
            restart.assert_not_called()
        self.assertIn('完成后再 /restart', self.bot.reply.await_args.args[1])

    async def test_export_starting_during_restart_delay_prevents_exec(self):
        with patch.object(live_backup, 'running', side_effect=[False, True]), \
                patch.object(admin.os, 'execve') as restart, \
                patch.object(admin.asyncio, 'sleep', new=AsyncMock()), \
                patch.object(session, 'save'):
            await admin.restart_cmd(self.event, self.bot, MatchResult())
            restart.assert_not_called()
        self.assertIn('本次重启已取消', self.bot.reply.await_args.args[1])

    async def test_export_failure_is_not_reported_as_success(self):
        with patch.object(live_backup, 'export_running', new=AsyncMock(side_effect=OSError('disk full'))):
            await admin.backup_cmd(self.event, self.bot, MatchResult())
        message = self.bot.reply.await_args.args[1]
        self.assertIn('导出失败', message)
        self.assertNotIn('备份完成', message)


if __name__ == '__main__':
    unittest.main()
