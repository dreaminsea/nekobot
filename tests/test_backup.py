"""在隔离 Git 工作副本里验证完整状态迁移、拒绝非法归档和故障回滚。"""

import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import time
import unittest
import venv
from unittest.mock import patch
import zipfile

from core import backup
from core.maintenance import acquire_lock

REPO = Path(__file__).resolve().parents[1]
CONFIG = 'DATA_DIR = "data"\nSTICKER_DIR = "stickers"\nAPI_KEY = "test-key"\n'


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.source = self.repo('source')
        self.target = self.repo('target')
        self.archive = self.base / 'state.zip'

    def repo(self, name):
        root = self.base / name
        root.mkdir()
        subprocess.run(['git', 'init', '-q', str(root)], check=True)
        for name, content in {
            'config.py': CONFIG,
            'bot.py': '# repository code\n',
            'core/existing.py': '# core source\n',
            '.gitignore': 'data/\n.env*\n.venv/\nbackups/\n',
            'compose.yaml': 'services: {}\n',
            'config/service.json': '{"default": true}',
            'README.md': '# source documentation\n',
        }.items():
            self.write(root, name, content)
        subprocess.run(['git', '-C', str(root), 'add', '.'], check=True)
        return root

    def write(self, root, name, content):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content if isinstance(content, bytes) else content.encode())
        return path

    def fill(self):
        payloads = {
            '.env': 'TOKEN=test-secret\n',
            '.env.local': 'LOCAL=test-local\n',
            'config/service.json': '{"port": 3001}',
            'data/sessions.json': '{"onebot_private_1":{"messages":[{"role":"user","content":"你好"}]}}',
            'data/memories.json': '[{"content":"喜欢猫","ts":123}]',
            'data/contacts.json': '{"朋友":{"platform":"onebot","id":"1"}}',
            'data/sticker_decisions.json': '{"skipped":{"status":"skipped"}}',
            'data/stickers/cat.png': b'\x89PNG\r\n\x1a\n\x00\xffbinary',
            'data/images/cache.bin': b'cached-image\x00',
            'data/napcat/config/webui.json': '{"token":"test-napcat"}',
            'data/napcat/qq/login.db': b'fake-database\x00\xff',
            'data/napcat/plugins/custom.js': 'local plugin',
            'data/future-format/blob.bin': b'unknown future data',
            'plugins/local_extension.py': '# locally added extension',
            'notes/中文.txt': '本地笔记',
        }
        for name, content in payloads.items():
            self.write(self.source, name, content)
        (self.source / 'data/empty').mkdir()
        self.write(self.source, 'data/stickers.json', json.dumps({
            'cat': {'path': str(self.source / 'data/stickers/cat.png'), 'meaning': '开心', 'tags': ['猫']}
        }, ensure_ascii=False))
        os.chmod(self.source / '.env', 0o600)
        os.chmod(self.source / 'data/stickers/cat.png', 0o640)
        return payloads

    def export(self, **options):
        return backup.export_backup(self.source, self.archive, **options)

    def rewrite(self, change, extra=None):
        with zipfile.ZipFile(self.archive) as archive:
            content = {name: archive.read(name) for name in archive.namelist()}
        manifest = json.loads(content['manifest.json'])
        change(manifest, content)
        content['manifest.json'] = json.dumps(manifest).encode()
        with zipfile.ZipFile(self.archive, 'w', zipfile.ZIP_DEFLATED) as archive:
            for name, data in content.items():
                archive.writestr(name, data)
            if extra:
                info, data = extra
                archive.writestr(info, data)

    def add_entry(self, manifest, content, name, data=b'attack'):
        manifest['entries'].append(dict(path=name, kind='file', mode=0o600, mtime_ns=0,
                                       size=len(data), sha256=hashlib.sha256(data).hexdigest()))
        content['files/' + name] = data

    def test_round_trip_covers_all_state_and_rebases_stickers_while_source_still_exists(self):
        payloads = self.fill()
        self.write(self.source, 'bot.py', '# edited repository code should not travel')
        for excluded in ('.venv/library.py', '__pycache__/config.cpython.pyc', 'backups/old.zip',
                         'nekobot-backup-old.zip', 'core/__pycache__/state.pyc', 'backup.py'):
            self.write(self.source, excluded, 'excluded')
        self.write(self.target, '.env', 'old-token')
        self.write(self.target, 'data/stale.json', 'stale data must disappear')
        original_config = (self.target / 'config.py').read_bytes()
        manifest = self.export()
        self.assertEqual(backup.inspect_backup(self.archive), manifest)
        self.assertEqual(stat.S_IMODE(self.archive.stat().st_mode), 0o600)
        names = {e['path'] for e in manifest['entries']}
        self.assertNotIn('bot.py', names)
        self.assertNotIn('backup.py', names)
        self.assertNotIn('.git', names)
        self.assertNotIn('backups/old.zip', names)
        self.assertNotIn('.venv/library.py', names)
        result = backup.restore_backup(self.target, self.archive)
        for name in payloads:
            self.assertEqual((self.target / name).read_bytes(), (self.source / name).read_bytes(), name)
        self.assertEqual((self.target / 'bot.py').read_text(), '# repository code\n')
        self.assertFalse((self.target / 'data/stale.json').exists())
        self.assertTrue((self.target / 'data/empty').is_dir())
        self.assertEqual(stat.S_IMODE((self.target / '.env').stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((self.target / 'data/stickers/cat.png').stat().st_mode), 0o640)
        records = json.loads((self.target / 'data/stickers.json').read_text())
        self.assertEqual(records['cat']['path'], str(self.target / 'data/stickers/cat.png'))
        before = Path(result['recovery']) / 'original'
        self.assertEqual((before / '.env').read_text(), 'old-token')
        self.assertEqual((before / 'config.py').read_bytes(), original_config)
        self.assertTrue((before / 'data/stale.json').exists())
        self.assertTrue(json.loads((Path(result['recovery']) / 'recovery.json').read_text())['complete'])

    def test_virtualenv_in_repository_root_is_excluded_without_losing_state(self):
        payloads = self.fill()
        venv.EnvBuilder(with_pip=False, symlinks=True).create(self.source)
        venv.EnvBuilder(with_pip=False, symlinks=True).create(self.target)
        original_target_config = (self.target / 'pyvenv.cfg').read_bytes()
        for name in ('lib/python/site-packages/dependency.py', 'include/header.h',
                     'Lib/dependency.py', 'Include/header.h', 'Scripts/activate'):
            self.write(self.source, name, 'environment dependency')
        self.assertTrue((self.source / 'bin/python').is_symlink())
        manifest = self.export()
        names = {e['path'] for e in manifest['entries']}
        for name in ('bin', 'lib', 'lib64', 'include', 'Lib', 'Include', 'Scripts', 'pyvenv.cfg'):
            self.assertFalse(any(p == name or p.startswith(name + '/') for p in names), name)
        backup.restore_backup(self.target, self.archive)
        for name, content in payloads.items():
            self.assertEqual((self.target / name).read_bytes(), (self.source / name).read_bytes(), name)
        self.assertEqual((self.target / 'pyvenv.cfg').read_bytes(), original_target_config)
        self.assertTrue((self.target / 'bin/python').is_symlink())

    def test_custom_named_virtualenv_is_excluded_but_ordinary_bin_and_lib_are_kept(self):
        venv.EnvBuilder(with_pip=False, symlinks=True).create(self.source / 'tools/python-env')
        self.write(self.source, 'bin/helper.sh', 'local helper')
        self.write(self.source, 'lib/user-state.json', '{"state": true}')
        names = {e['path'] for e in self.export()['entries']}
        self.assertFalse(any(p.startswith('tools/python-env') for p in names))
        self.assertIn('bin/helper.sh', names)
        self.assertIn('lib/user-state.json', names)

    def test_virtualenv_marker_does_not_skip_explicit_data_or_includes(self):
        self.fill()
        for name in ('pyvenv.cfg', 'bin/records.json', 'lib/records.json'):
            self.write(self.source / 'data', name, 'user data')
        extra = self.base / 'extra'
        self.write(extra, 'pyvenv.cfg', 'explicitly included config')
        self.write(extra, 'bin/records.json', 'explicitly included data')
        names = {e['path'] for e in self.export(includes=(f'{extra}=extra',))['entries']}
        for name in ('data/pyvenv.cfg', 'data/bin/records.json', 'data/lib/records.json',
                     'extra/pyvenv.cfg', 'extra/bin/records.json'):
            self.assertIn(name, names)
        self.archive.unlink()
        (self.source / 'data/bin/python').symlink_to(sys.executable)
        with self.assertRaisesRegex(backup.BackupError, '符号链接'):
            self.export()
        self.assertFalse(self.archive.exists())

    def test_symlinked_virtualenv_marker_is_not_followed_and_error_is_specific(self):
        marker = self.write(self.base, 'outside/pyvenv.cfg', 'home = /usr/bin')
        (self.source / 'pyvenv.cfg').symlink_to(marker)
        (self.source / 'bin').mkdir()
        (self.source / 'bin/python').symlink_to(sys.executable)
        with self.assertRaisesRegex(backup.BackupError, '符号链接') as caught:
            self.export()
        self.assertNotIn('停止', str(caught.exception))
        self.assertIn('bin/python', str(caught.exception))
        self.assertFalse(self.archive.exists())

    def test_external_data_stickers_legacy_images_and_extra_mount(self):
        data = self.base / 'external-data'
        stickers = self.base / 'external-stickers'
        legacy = self.write(self.base, 'legacy/smile.gif', b'GIF89aimage')
        self.write(data, 'memories.json', '["outside repo"]')
        self.write(stickers, 'cat.png', b'PNGimage')
        self.write(data, 'stickers.json', json.dumps({
            'cat': {'path': str(stickers / 'cat.png')}, 'legacy': {'path': str(legacy)}
        }))
        self.write(self.source, 'config.py', f'# 中文注释\nDATA_DIR: str = {str(data)!r} # 保留\n'
                   f'STICKER_DIR = {str(stickers)!r}\nraise RuntimeError("must never execute")\n')
        extra = self.write(self.base, 'bridge/state.db', b'bridge-data').parent
        self.export(includes=(f'{extra}=services/bridge',))
        backup.restore_backup(self.target, self.archive)
        settings = backup._settings(self.target, None, None)
        self.assertEqual(settings, {'DATA_DIR': 'data', 'STICKER_DIR': 'stickers'})
        self.assertIn('# 保留', (self.target / 'config.py').read_text())
        self.assertEqual((self.target / 'services/bridge/state.db').read_bytes(), b'bridge-data')
        records = json.loads((self.target / 'data/stickers.json').read_text())
        for key, content in [('cat', b'PNGimage'), ('legacy', b'GIF89aimage')]:
            path = Path(records[key]['path'])
            self.assertTrue(path.is_relative_to(self.target))
            self.assertEqual(path.read_bytes(), content)

    def test_external_state_does_not_collide_with_local_napcat_or_old_stickers(self):
        data = self.base / 'outside-bot-data'
        stickers = self.base / 'outside-pictures'
        self.write(self.source, 'data/napcat/config.json', 'local napcat state')
        self.write(data, 'stickers/old.png', b'old picture')
        self.write(stickers, 'new.png', b'new picture')
        self.write(self.source, 'config.py', f'DATA_DIR = {str(data)!r}\nSTICKER_DIR = {str(stickers)!r}\n')
        self.export()
        backup.restore_backup(self.target, self.archive)
        self.assertEqual((self.target / 'data/napcat/config.json').read_text(), 'local napcat state')
        self.assertEqual((self.target / 'data-2/stickers/old.png').read_bytes(), b'old picture')
        self.assertEqual((self.target / 'data-2/stickers-2/new.png').read_bytes(), b'new picture')
        self.assertEqual(backup._settings(self.target, None, None),
                         {'DATA_DIR': 'data-2', 'STICKER_DIR': 'stickers-2'})

    def test_local_config_overrides_data_dir_and_receives_relocation(self):
        data = self.base / 'local-data'
        self.write(data, 'memories.json', '["from local config"]')
        defaults = ('from core.local_config import load_local\nDATA_DIR = "data"\nSTICKER_DIR = "stickers"\n'
                    'globals().update(load_local(globals()))\n')
        for root in (self.source, self.target):
            self.write(root, 'config.py', defaults)
        self.write(self.source, 'config.local.py', f'# 本地\nDATA_DIR = {str(data)!r}  # 外部目录\n')
        self.assertEqual(backup._settings(self.source, None, None)['DATA_DIR'], str(data))
        self.export()
        backup.restore_backup(self.target, self.archive)
        # 改写只发生在本地文件里，随代码更新的 config.py 保持原样
        self.assertEqual((self.target / 'config.py').read_text(), defaults)
        self.assertEqual((self.target / 'config.local.py').read_text(), "# 本地\nDATA_DIR = 'data'  # 外部目录\n")
        self.assertEqual((self.target / 'data/memories.json').read_text(), '["from local config"]')
        self.assertEqual(backup._settings(self.target, None, None)['DATA_DIR'], 'data')

    def test_local_config_is_ignored_by_old_config_and_complex_values_need_flags(self):
        self.write(self.source, 'config.local.py', 'DATA_DIR = "elsewhere"\n')
        self.assertEqual(backup._settings(self.source, None, None)['DATA_DIR'], 'data')  # 旧版 config.py 不读本地文件
        self.write(self.source, 'config.py', CONFIG + 'globals().update(load_local(globals()))\n')
        self.write(self.source, 'config.local.py', 'if True:\n    DATA_DIR = "elsewhere"\n')
        with self.assertRaisesRegex(backup.BackupError, 'config.local.py'):
            backup._settings(self.source, None, None)
        self.assertEqual(backup._settings(self.source, 'state', None)['DATA_DIR'], 'state')

    def test_custom_nested_relative_data_and_sticker_directories(self):
        self.write(self.source, 'config.py', 'DATA_DIR = "state/bot"\nSTICKER_DIR = "../pictures"\n')
        self.write(self.source, 'state/bot/memories.json', '["custom"]')
        self.write(self.source, 'state/pictures/cat.png', b'picture')
        self.export()
        backup.restore_backup(self.target, self.archive)
        self.assertEqual((self.target / 'state/bot/memories.json').read_text(), '["custom"]')
        self.assertEqual((self.target / 'state/pictures/cat.png').read_bytes(), b'picture')
        self.assertEqual(backup._settings(self.target, None, None)['STICKER_DIR'], '../pictures')

    def test_empty_initial_state_replaces_stale_data(self):
        self.export()
        self.write(self.target, 'data/old.json', 'stale')
        backup.restore_backup(self.target, self.archive)
        self.assertFalse((self.target / 'data/old.json').exists())
        self.assertTrue((self.target / 'data/stickers').is_dir())

    def test_dynamic_config_requires_explicit_path_without_executing_code(self):
        self.write(self.source, 'config.py', 'DATA_DIR = get_data_dir()\nSTICKER_DIR = "stickers"\n')
        with self.assertRaisesRegex(backup.BackupError, '--data-dir'):
            self.export()
        self.write(self.source, 'state/custom', b'data')
        self.export(data_dir='state')
        backup.restore_backup(self.target, self.archive)
        self.assertEqual(backup._settings(self.target, None, None)['DATA_DIR'], 'state')

    def test_dry_run_and_cli_inspect_do_not_modify_data(self):
        self.fill()
        self.export()
        before = (self.target / 'config.py').read_bytes()
        preview = backup.restore_backup(self.target, self.archive, dry_run=True)
        self.assertIn('data', preview['paths'])
        self.assertIsNone(preview['recovery'])
        self.assertEqual((self.target / 'config.py').read_bytes(), before)
        self.assertFalse((self.target / 'data').exists())
        self.assertFalse((self.target / 'backups').exists())
        result = subprocess.run([sys.executable, '-B', str(REPO / 'backup.py'), 'inspect',
                                 str(self.archive), '--list'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('data/memories.json', result.stdout)
        self.assertNotIn('test-secret', result.stdout)

    def test_corruption_is_detected_before_any_replacement(self):
        self.fill()
        self.export()
        self.rewrite(lambda m, c: c.__setitem__('files/.env', b'x' * len(c['files/.env'])))
        self.write(self.target, '.env', 'keep me')
        with self.assertRaisesRegex(backup.BackupError, '校验失败'):
            backup.restore_backup(self.target, self.archive)
        self.assertEqual((self.target / '.env').read_text(), 'keep me')
        self.assertFalse((self.target / 'data').exists())
        self.assertFalse((self.target / 'backups').exists())
        self.assertFalse(list(self.target.glob('.nekobot-restore-*')))

    def test_rejects_traversal_absolute_and_internal_paths(self):
        for name in ('../escape', '/tmp/escape', 'C:/escape', 'data\\escape', '.git/config', '.nekobot.lock',
                     'backups/escape', 'data//bad', 'data/./bad'):
            with self.subTest(name=name):
                self.archive.unlink(missing_ok=True)
                self.export()
                self.rewrite(lambda m, c: self.add_entry(m, c, name))
                with self.assertRaises(backup.BackupError):
                    backup.restore_backup(self.target, self.archive)
        self.assertFalse((self.base / 'escape').exists())

    def test_rejects_repository_overwrite_even_with_valid_checksum(self):
        self.export()
        self.rewrite(lambda m, c: self.add_entry(m, c, 'bot.py'))
        with self.assertRaisesRegex(backup.BackupError, '仓库源码'):
            backup.restore_backup(self.target, self.archive)
        self.assertEqual((self.target / 'bot.py').read_text(), '# repository code\n')

    def test_rejects_missing_extra_duplicate_and_unsupported_entries(self):
        changes = [
            lambda m, c: c.pop('files/config.py'),
            lambda m, c: c.__setitem__('unexpected', b'bytes'),
            lambda m, c: m['entries'].append(m['entries'][0]),
            lambda m, c: m.__setitem__('version', 99),
            lambda m, c: m['replace_dirs'].append('data/stickers'),
        ]
        for change in changes:
            self.archive.unlink(missing_ok=True)
            self.export()
            self.rewrite(change)
            with self.assertRaises(backup.BackupError):
                backup.inspect_backup(self.archive)

    def test_rejects_zip_symlink_payload_and_duplicate_member(self):
        self.export()
        with zipfile.ZipFile(self.archive) as archive:
            items = {name: archive.read(name) for name in archive.namelist()}
        with zipfile.ZipFile(self.archive, 'w') as archive:
            for name, data in items.items():
                info = zipfile.ZipInfo(name)
                if name == 'files/config.py':
                    info.external_attr = (stat.S_IFLNK | 0o777) << 16
                archive.writestr(info, data)
        with self.assertRaises(backup.BackupError):
            backup.inspect_backup(self.archive)
        with zipfile.ZipFile(self.archive, 'a') as archive:
            with self.assertWarns(UserWarning):
                archive.writestr('manifest.json', items['manifest.json'])
        with self.assertRaisesRegex(backup.BackupError, '重复 ZIP'):
            backup.inspect_backup(self.archive)

    def test_source_and_destination_symlinks_are_never_followed(self):
        outside = self.base / 'outside'
        outside.mkdir()
        self.write(outside, 'important', 'untouched')
        (self.source / 'data').symlink_to(outside, target_is_directory=True)
        with self.assertRaises(backup.BackupError):
            self.export()
        (self.source / 'data').unlink()
        self.export()
        (self.target / 'data').symlink_to(outside, target_is_directory=True)
        with self.assertRaises(backup.BackupError):
            backup.restore_backup(self.target, self.archive)
        self.assertEqual((outside / 'important').read_text(), 'untouched')

    def test_lock_refuses_running_bot_and_parallel_maintenance(self):
        with acquire_lock(self.source):
            with self.assertRaisesRegex(RuntimeError, '正在运行'):
                self.export()
        self.export()
        with acquire_lock(self.target):
            with self.assertRaisesRegex(RuntimeError, '正在运行'):
                backup.restore_backup(self.target, self.archive)
        self.assertFalse((self.target / 'data').exists())

    def test_failed_install_rolls_back_prior_replacements(self):
        self.fill()
        self.export()
        self.write(self.target, '.env', 'old secret')
        self.write(self.target, 'data/old', 'old data')
        before = (self.target / 'config.py').read_bytes()
        replace = os.replace

        def fail_once(source, target):
            if Path(source).name == 'data' and Path(source).parent.name.startswith('.nekobot-restore-'):
                raise OSError('simulated disk failure')
            return replace(source, target)

        with patch.object(backup.os, 'replace', side_effect=fail_once):
            with self.assertRaisesRegex(OSError, 'disk failure'):
                backup.restore_backup(self.target, self.archive)
        self.assertEqual((self.target / '.env').read_text(), 'old secret')
        self.assertEqual((self.target / 'config.py').read_bytes(), before)
        self.assertEqual((self.target / 'data/old').read_text(), 'old data')
        self.assertFalse((self.target / '.env.local').exists())
        self.assertFalse((self.target / 'data/sessions.json').exists())

    def test_export_refuses_changing_files_and_leaves_no_partial_archive(self):
        self.write(self.source, '.env', 'old')
        metadata = backup._metadata

        def mutate(entry):
            result = metadata(entry)
            if entry.path == '.env':
                entry.source.write_text('concurrent update')
            return result

        with patch.object(backup, '_metadata', side_effect=mutate):
            with self.assertRaisesRegex(backup.BackupError, '发生变化'):
                self.export()
        self.assertFalse(self.archive.exists())
        self.assertFalse(list(self.base.glob('.nekobot-backup-*')))

    def test_no_self_inclusion_overwrite_or_file_directory_conflict(self):
        self.fill()
        with self.assertRaisesRegex(backup.BackupError, '待备份的数据目录'):
            backup.export_backup(self.source, self.source / 'data/backup.zip')
        self.export()
        original = self.archive.read_bytes()
        with self.assertRaisesRegex(backup.BackupError, '已存在'):
            self.export()
        self.assertEqual(self.archive.read_bytes(), original)
        (self.target / '.env').mkdir()
        with self.assertRaisesRegex(backup.BackupError, '类型冲突'):
            backup.restore_backup(self.target, self.archive)
        self.assertTrue((self.target / '.env').is_dir())

    def test_export_detects_new_root_files_during_snapshot(self):
        metadata = backup._metadata

        def add_file(entry):
            result = metadata(entry)
            if entry.path == 'config.py':
                self.write(self.source, 'new-state.json', 'concurrent new data')
            return result

        with patch.object(backup, '_metadata', side_effect=add_file):
            with self.assertRaisesRegex(backup.BackupError, '清单发生变化'):
                self.export()
        self.assertFalse(self.archive.exists())

    def test_legacy_sticker_fallback_and_missing_image_error(self):
        self.write(self.source, 'data/stickers/cat.png', b'legacy-image')
        self.write(self.source, 'data/stickers.json', json.dumps({'cat': {'path': '/old/missing/cat.png'}}))
        self.export()
        backup.restore_backup(self.target, self.archive)
        record = json.loads((self.target / 'data/stickers.json').read_text())['cat']
        self.assertEqual(Path(record['path']).read_bytes(), b'legacy-image')
        self.archive.unlink()
        (self.source / 'data/stickers/cat.png').unlink()
        with self.assertRaisesRegex(backup.BackupError, '图片不存在'):
            self.export()

    def test_bot_locks_before_reading_any_configuration(self):
        self.write(self.source, 'bot.py', (REPO / 'bot.py').read_bytes())
        self.write(self.source, 'config.py', 'raise RuntimeError("config was executed")')
        env = dict(os.environ, PYTHONPATH=str(REPO), PYTHONDONTWRITEBYTECODE='1')
        with acquire_lock(self.source):
            result = subprocess.run([sys.executable, '-B', str(self.source / 'bot.py')],
                                    env=env, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('正在运行', result.stderr)
        self.assertNotIn('config was executed', result.stderr)

    def test_sigterm_saves_session_and_releases_lock_without_network(self):
        self.write(self.source, 'bot.py', (REPO / 'bot.py').read_bytes())
        runner = r"""
import asyncio
from pathlib import Path
import runpy
import sys
import types
root = Path(sys.argv[1])
def module(name, **values):
    mod = types.ModuleType(name)
    mod.__dict__.update(values)
    sys.modules[name] = mod
async def noop(*args):
    pass
class Adapter:
    bot = object()
    async def run(self, callback):
        (root / 'started').touch()
        await asyncio.Event().wait()
module('config', LLM_API_KEY='test-key', NAPCAT_WATCH=False, PLATFORMS=['test'], SESSION_SAVE_INTERVAL=999)
module('core.log', setup=lambda: None, log=types.SimpleNamespace(info=lambda *a: None, warning=lambda *a: None, error=lambda *a: None))
module('core.runtime', set_adapters=lambda *a: None)
module('core.session', save=lambda: (root / 'saved').write_text('unsaved conversation'), flush_if_dirty=lambda: None)
module('core.timeline', flush_if_dirty=lambda: (root / 'agent-saved').write_text('recent experiences'), work_snapshot=lambda: None,
       set_status_provider=lambda *a: None)
module('core.coalesce', close=noop)
module('core.sticker_learning', start=lambda: None, close=noop)
module('core.adapter', Adapter=Adapter, create_adapter=lambda *a: Adapter())
module('core.event', MessageEvent=object)
module('core.plugin', dispatch=noop)
module('core.restart_notice', notify_after_restart=noop)
for name in ['tools', 'plugins', 'adapters']:
    module(name)
runpy.run_path(str(root / 'bot.py'), run_name='__main__')
"""
        env = dict(os.environ, PYTHONPATH=str(REPO), PYTHONDONTWRITEBYTECODE='1')
        process = subprocess.Popen([sys.executable, '-B', '-c', runner, str(self.source)],
                                   env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 5
            while not (self.source / 'started').exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue((self.source / 'started').exists(), 'isolated bot did not start')
            with self.assertRaises(RuntimeError):
                with acquire_lock(self.source):
                    pass
            process.terminate()
            out, err = process.communicate(timeout=5)
            self.assertEqual(process.returncode, 0, out + err)
            self.assertEqual((self.source / 'saved').read_text(), 'unsaved conversation')
            self.assertEqual((self.source / 'agent-saved').read_text(), 'recent experiences')
            with acquire_lock(self.source):
                pass
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=5)

    def test_cli_reports_invalid_zip_cleanly(self):
        self.archive.write_bytes(b'not zip')
        result = subprocess.run([sys.executable, '-B', str(REPO / 'backup.py'), 'inspect', str(self.archive)],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn('备份操作失败', result.stderr)
        self.assertNotIn('Traceback', result.stderr)


if __name__ == '__main__':
    unittest.main()
