import os
import runpy
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from core import restart_notice, session
from core.event import MessageEvent
from core.plugin import MatchResult
from plugins import admin


OK = {'status': 'ok', 'retcode': 0, 'data': {'message_id': 123}}


class RestartNoticeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = SimpleNamespace(call_action_resp=AsyncMock(return_value=OK))
        self.get_bot = Mock(return_value=self.bot)
        self.sleep = AsyncMock()
        for p in (
            patch.dict(os.environ, {restart_notice.RESTART_NOTICE_ENV: '1'}),
            patch.object(restart_notice, 'MASTER_QQ', '12345'),
            patch.object(restart_notice, 'PLATFORMS', ['onebot', 'feishu']),
            patch.object(restart_notice.runtime, 'get_adapter_bot', self.get_bot),
            patch.object(restart_notice.asyncio, 'sleep', self.sleep),
        ):
            p.start()
            self.addCleanup(p.stop)

    async def test_normal_startup_does_not_send_or_poll(self):
        os.environ.pop(restart_notice.RESTART_NOTICE_ENV)
        await restart_notice.notify_after_restart()
        self.get_bot.assert_not_called()
        self.bot.call_action_resp.assert_not_awaited()

    async def test_first_update_loads_notice_with_the_old_in_memory_config(self):
        with patch.dict(restart_notice.config.__dict__):
            restart_notice.config.__dict__.pop('RESTART_NOTICE_RETRY_SECONDS', None)
            module = runpy.run_path(restart_notice.__file__)
        self.assertEqual(module['RESTART_NOTICE_RETRY_SECONDS'], 5.0)

    async def test_waits_for_qq_then_sends_to_master_and_consumes_marker(self):
        self.get_bot.side_effect = [None, None, self.bot]
        await restart_notice.notify_after_restart()
        self.assertEqual(self.sleep.await_count, 2)
        self.bot.call_action_resp.assert_awaited_once()
        call = self.bot.call_action_resp.await_args
        self.assertEqual(call.args, ('send_private_msg',))
        self.assertEqual(call.kwargs['user_id'], 12345)
        self.assertIn('重启完成', call.kwargs['message'][0]['data']['text'])
        self.assertNotIn(restart_notice.RESTART_NOTICE_ENV, os.environ)
        # 相同进程内再次调用（例如误调/重连）不会重复发。
        await restart_notice.notify_after_restart()
        self.bot.call_action_resp.assert_awaited_once()

    async def test_retries_disconnect_timeout_and_explicit_failure(self):
        self.bot.call_action_resp.side_effect = [
            ConnectionError('disconnected'), None,
            {'status': 'failed', 'retcode': 100},
            {'status': 'ok', 'retcode': 100}, OK,
        ]
        await restart_notice.notify_after_restart()
        self.assertEqual(self.bot.call_action_resp.await_count, 5)
        self.assertEqual(self.sleep.await_count, 4)

    async def test_no_qq_or_master_does_not_loop_forever(self):
        with patch.object(restart_notice, 'PLATFORMS', ['feishu']):
            await restart_notice.notify_after_restart()
        os.environ[restart_notice.RESTART_NOTICE_ENV] = '1'
        with patch.object(restart_notice, 'MASTER_QQ', ''):
            await restart_notice.notify_after_restart()
        self.get_bot.assert_not_called()
        self.sleep.assert_not_awaited()

    async def test_restart_command_passes_marker_to_new_process_from_either_platform(self):
        for platform in ('onebot', 'feishu'):
            with self.subTest(platform=platform):
                event = MessageEvent(platform=platform, message_type='private', user_id='master')
                bot = SimpleNamespace(reply=AsyncMock())
                os.environ.pop(restart_notice.RESTART_NOTICE_ENV, None)
                with patch.object(admin, 'os') as proc_os, patch.object(session, 'save') as save:
                    proc_os.environ.copy.return_value = {'TEST_RESTART_ENV': 'preserved'}
                    # __wrapped__ 只绕过权限装饰器，测试真实重启命令，绝不调用真实 exec。
                    await admin.restart_cmd.__wrapped__(event, bot, MatchResult())
                    save.assert_called_once()
                    proc_os.execve.assert_called_once()
                    exe, argv, env = proc_os.execve.call_args.args
                    self.assertEqual(exe, admin.sys.executable)
                    self.assertEqual(argv, [admin.sys.executable, *admin.sys.argv])
                    self.assertEqual(env, {'TEST_RESTART_ENV': 'preserved', restart_notice.RESTART_NOTICE_ENV: '1'})
                self.assertNotIn(restart_notice.RESTART_NOTICE_ENV, os.environ)

    async def test_failed_exec_does_not_mark_the_old_process_as_restarted(self):
        os.environ.pop(restart_notice.RESTART_NOTICE_ENV)
        event = MessageEvent(platform='onebot', message_type='private', user_id='master')
        bot = SimpleNamespace(reply=AsyncMock())
        with patch.object(admin.os, 'execve', side_effect=OSError('exec failed')), \
             patch.object(session, 'save'):
            with self.assertRaises(OSError):
                await admin.restart_cmd.__wrapped__(event, bot, MatchResult())
        self.assertNotIn(restart_notice.RESTART_NOTICE_ENV, os.environ)
        await restart_notice.notify_after_restart()
        self.bot.call_action_resp.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
