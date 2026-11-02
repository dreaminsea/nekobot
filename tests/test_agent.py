import asyncio
from collections import defaultdict
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import config
import plugins
from core import agent, coalesce, engagement, group_chat, llm, perms, plugin, runtime, session, storage, timeline
from core.plugin import MatchResult
from plugins.agent import agent_command
from tests.test_timeline import event
from tests.test_replies import FakeBot


async def until(condition):
    async with asyncio.timeout(1):
        while not condition():
            await asyncio.sleep(0)


class AgentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        for p in (
            patch.object(storage, "DATA_DIR", self.temp.name),
            patch.object(timeline, "_data", {"version": 1, "experiences": [], "current_state": None}),
            patch.object(timeline, "_dirty", False), patch.object(timeline, "_writable", True),
            patch.object(config, "SESSION_PERSIST", True),
            patch.object(config, "AGENT_CONTEXT_ENABLED", True),
            patch.object(perms, "MASTER_QQ", "1"), patch.object(agent, "_current", None),
            patch.object(coalesce, "_pending", {}), patch.object(coalesce, "REPLY_DEBOUNCE", 0),
            patch.object(coalesce, "GROUP_IDLE_DEBOUNCE", 0),
            patch.object(session, "sessions", defaultdict(session.Session)),
            patch.object(engagement, "_last_reply_at", {}), patch.object(runtime, "_adapters", {}),
            patch.object(group_chat, "AMBIENT_CHAT", False), patch.object(group_chat, "FOLLOWUP_CHAT", False),
        ):
            p.start()
            self.addCleanup(p.stop)
        self.ev = event()
        self.bot = FakeBot()
        self.bot.confirms_delivery = True
        self.loops = []
        self.addAsyncCleanup(self.cleanup_loops)
        runtime.set_context(self.ev, self.bot)
        self.addCleanup(runtime.set_context, None, None)

    async def cleanup_loops(self):
        for loop in self.loops:
            await loop.close()
        await coalesce.close()

    def loop(self, dispatch=None, **kwargs):
        loop = agent.AgentLoop(dispatch, **kwargs)
        self.loops.append(loop)
        agent._current = loop
        loop.start()
        return loop

    async def idle_chat(self, loop):
        await loop.wait_idle()
        tasks = [p.task for p in coalesce._pending.values() if p.task]
        if tasks:
            await asyncio.wait_for(asyncio.gather(*tasks), 1)

    def work(self):
        return timeline.work_snapshot()

    async def test_work_advances_without_messages_and_does_not_inherit_chat_identity(self):
        seen = []

        async def step(index):
            seen.append((index, runtime.get_event(), runtime.get_bot()))
            return "done"

        loop = self.loop(AsyncMock())
        with patch.object(llm, "get_provider") as provider:
            response = await loop.start_work(self.ev, step, steps=3)
            self.assertIn("尚未完成", response)
            await until(lambda: self.work()["status"] == "completed")
            provider.assert_not_called()
        self.assertEqual(seen, [(0, None, None), (1, None, None), (2, None, None)])
        self.assertEqual(self.work()["step"], 3)
        self.assertIs(runtime.get_event(), self.ev)

    async def test_idle_loop_waits_without_llm_or_busy_polling(self):
        dispatch = AsyncMock()
        with patch.object(llm, "get_provider") as provider:
            loop = self.loop(dispatch)
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            self.assertEqual(loop._queue.qsize(), 0)
            self.assertFalse(loop._runner.done())
            provider.assert_not_called()
            dispatch.assert_not_awaited()
            self.assertIsNone(self.work())

    async def test_long_command_does_not_block_other_chat_or_work(self):
        entered, release, fast = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def dispatch(ev, bot):
            self.assertIsNone(runtime.get_event())
            runtime.set_context(ev, bot)
            if ev.user_id == "1":
                entered.set()
                await release.wait()
                self.assertIs(runtime.get_event(), self.ev)
            else:
                fast.set()

        loop = self.loop(dispatch)
        slow = asyncio.create_task(loop.submit_message(self.ev, self.bot))
        await entered.wait()
        await loop.submit_message(event(user="2"), self.bot)
        self.assertTrue(fast.is_set())
        await loop.start_work(self.ev, AsyncMock(return_value="ok"), steps=2)
        await until(lambda: self.work()["status"] == "completed")
        self.assertFalse(slow.done())
        release.set()
        await slow

    async def test_pause_takes_effect_at_step_boundary_and_resume_continues(self):
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []

        async def step(index):
            calls.append(index)
            if index == 0:
                entered.set()
                await release.wait()
            return "step done"

        loop = self.loop(AsyncMock())
        await loop.start_work(self.ev, step, steps=3)
        await entered.wait()
        self.assertIn("等待当前步骤", await loop.control("pause", self.ev))
        self.assertEqual(self.work()["status"], "pausing")
        release.set()
        await until(lambda: self.work()["step"] == 1)
        self.assertEqual(self.work()["status"], "paused")
        await loop.submit_message(event(user="2"), self.bot)
        self.assertEqual(calls, [0])
        await loop.control("resume", self.ev)
        await until(lambda: self.work()["status"] == "completed")
        self.assertEqual(calls, [0, 1, 2])

    async def test_stop_waits_for_cancel_resistant_step_and_never_runs_next(self):
        entered, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        calls = []

        async def step(index):
            calls.append(index)
            entered.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                cancelled.set()
                await release.wait()
            return "late result"

        loop = self.loop(AsyncMock())
        await loop.start_work(self.ev, step, steps=3)
        await entered.wait()
        self.assertIn("正在停止", await loop.control("stop", self.ev))
        await cancelled.wait()
        self.assertEqual(self.work()["status"], "stopping")
        self.assertIn("未启动第二项", await loop.start_work(self.ev, AsyncMock(), steps=1))
        self.assertIn("等待当前步骤", await loop.control("resume", self.ev))
        release.set()
        await until(lambda: self.work()["status"] == "stopped")
        self.assertEqual(calls, [0])

    async def test_stop_before_coroutine_starts_reaches_terminal_state(self):
        loop = self.loop(AsyncMock())
        step = AsyncMock()
        starting = asyncio.create_task(loop.start_work(self.ev, step, steps=2))
        stopping = asyncio.create_task(loop.control("stop", self.ev))
        await asyncio.gather(starting, stopping)
        await until(lambda: self.work()["status"] == "stopped")
        step.assert_not_awaited()

    async def test_stop_while_paused_and_stale_progress_cannot_change_next_work(self):
        loop = self.loop(AsyncMock())
        await loop.start_work(self.ev, AsyncMock(return_value="ok"), steps=3)
        await loop.control("pause", self.ev)
        first_id = self.work()["id"]
        await loop.control("stop", self.ev)
        await until(lambda: self.work()["status"] == "stopped")
        await loop.start_work(self.ev, AsyncMock(return_value="new"), steps=1)
        await until(lambda: self.work()["status"] == "completed")
        snapshot = self.work()
        await loop._request("progress", (first_id, 100, "stale private data"))
        await loop._request("result", (first_id, "failed", "stale result"))
        self.assertEqual(self.work(), snapshot)

    async def test_limits_failure_and_timeout_are_not_success_and_do_not_retry(self):
        loop = self.loop(AsyncMock(), max_steps=3, timeout=0.02)
        step = AsyncMock(side_effect=ValueError("private error detail"))
        self.assertIn("1 到 3", await loop.start_work(self.ev, step, steps=4))
        self.assertIsNone(self.work())
        await loop.start_work(self.ev, step, steps=2)
        await until(lambda: self.work()["status"] == "failed")
        step.assert_awaited_once()
        self.assertNotIn("private error detail", self.work()["result"])

        async def blocked(index):
            await asyncio.Future()

        await loop.start_work(self.ev, blocked, steps=1)
        await until(lambda: self.work()["status"] == "failed")
        self.assertIn("总时限", self.work()["result"])
        self.assertEqual(self.work()["step"], 0)

    async def test_cancel_resistant_step_cannot_bypass_time_budget(self):
        calls = []

        async def step(index):
            calls.append(index)
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                return "late completion"

        loop = self.loop(AsyncMock(), timeout=0.01)
        await loop.start_work(self.ev, step, steps=3)
        await until(lambda: self.work()["status"] == "failed")
        self.assertEqual(calls, [0])
        self.assertIn("总时限", self.work()["result"])

    async def test_bad_internal_event_does_not_corrupt_work_or_stop_consumer(self):
        loop = self.loop(AsyncMock())
        self.assertIn("失败", await loop._request("unknown", None))
        await loop.start_work(self.ev, AsyncMock(return_value="ok"), steps=2)
        await loop.control("pause", self.ev)
        await until(lambda: self.work()["status"] == "paused")
        before = self.work()
        self.assertIn("失败", await loop._request("progress", (before["id"], 900, "bad progress")))
        self.assertEqual(self.work(), before)
        self.assertEqual(loop._work, before)
        await loop.control("resume", self.ev)
        await until(lambda: self.work()["status"] == "completed")

    async def test_foreign_users_and_group_cannot_control_work(self):
        loop = self.loop(AsyncMock())
        for ev in (event(user="2"), event("group")):
            self.assertIn("主人在私聊", await loop.start_work(ev, AsyncMock(), steps=1))
            self.assertIn("主人在私聊", await loop.control("pause", ev))
        self.assertIsNone(self.work())
        for value in (float("nan"), float("inf"), -1, 0, 100):
            self.assertIn("间隔", await loop.demo(self.ev, interval=value))

    async def test_work_state_is_scoped_and_legacy_note_and_clear_stay_independent(self):
        loop = self.loop(AsyncMock())
        timeline.set_state(self.ev, "manual note")
        await loop.start_work(self.ev, AsyncMock(return_value="private step result"), steps=1,
                              name="private name", goal="private goal")
        await until(lambda: self.work()["status"] == "completed")
        prompt = timeline.as_prompt("onebot_group_2")
        self.assertIn("completed", prompt)
        for secret in ("private name", "private goal", "manual note", "onebot_private_1"):
            self.assertNotIn(secret, prompt)
            self.assertNotIn(secret, loop.status(event("group")))
        local = timeline.as_prompt(self.ev.session_key)
        self.assertIn("private goal", local)
        self.assertIn("manual note", local)
        session.get_session(self.ev.session_key).clear()
        self.assertEqual(self.work()["status"], "completed")
        self.assertIsNone(timeline.snapshot()["current_state"])

    async def test_old_file_loads_and_restart_marks_work_interrupted_even_if_disabled(self):
        timeline.received(self.ev)
        timeline.set_state(self.ev, "saved note")
        old = timeline.snapshot()
        timeline.flush_if_dirty()
        timeline.load()
        self.assertEqual(timeline.snapshot(), old)
        loop = self.loop(AsyncMock())
        entered = asyncio.Event()

        async def step(index):
            entered.set()
            await asyncio.Future()

        await loop.start_work(self.ev, step, steps=2)
        await entered.wait()
        crashed = timeline.snapshot()
        await loop.close()
        storage.save_json(timeline.FILE, crashed)
        timeline.load()
        fresh = self.loop(AsyncMock(), enabled=False)
        self.assertEqual(self.work()["status"], "interrupted")
        self.assertIsNone(fresh._work_task)
        self.assertEqual(timeline.snapshot()["experiences"], old["experiences"])
        self.assertEqual(timeline.snapshot()["current_state"], old["current_state"])
        self.assertEqual(timeline.snapshot()["version"], 1)
        self.assertIn("未运行", await fresh.control("resume", self.ev))

    async def test_shutdown_cancels_handlers_work_and_consumer(self):
        entered, work_entered = asyncio.Event(), asyncio.Event()

        async def dispatch(ev, bot):
            entered.set()
            await asyncio.Future()

        async def step(index):
            work_entered.set()
            await asyncio.Future()

        loop = self.loop(dispatch)
        message = asyncio.create_task(loop.submit_message(self.ev, self.bot))
        await entered.wait()
        await loop.start_work(self.ev, step, steps=2)
        await work_entered.wait()
        await loop.close()
        await message
        self.assertTrue(loop._runner.done())
        self.assertTrue(loop._work_task.done())
        self.assertFalse(loop._handlers)
        self.assertEqual(self.work()["status"], "interrupted")
        await loop.close()  # 重复清理无副作用。
        self.assertIn("未运行", await loop.control("resume", self.ev))

    async def test_process_shutdown_keeps_late_cancel_result_interrupted(self):
        entered, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def step(index):
            entered.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                cancelled.set()
                await release.wait()
            return "late completion"

        loop = self.loop(AsyncMock())
        await loop.start_work(self.ev, step, steps=2)
        await entered.wait()
        closing = asyncio.create_task(loop.close())
        await cancelled.wait()
        self.assertFalse(loop.accepts_messages)
        release.set()
        await closing
        self.assertEqual(self.work()["status"], "interrupted")

    async def test_dispatch_exception_does_not_kill_loop_or_retry(self):
        dispatch = AsyncMock(side_effect=[RuntimeError("bad event"), None])
        loop = self.loop(dispatch)
        await loop.submit_message(self.ev, self.bot)
        await loop.submit_message(event(user="2"), self.bot)
        self.assertEqual(dispatch.await_count, 2)
        self.assertFalse(loop._runner.done())

    async def test_ignore_stays_outside_history_and_background_state(self):
        loop = self.loop()
        self.ev.text = "/ignore secret"
        await loop.submit_message(self.ev, self.bot)
        await loop.wait_idle()
        self.assertEqual(timeline.snapshot()["experiences"], [])
        self.assertFalse(session.sessions)
        self.assertIsNone(self.work())
        self.assertNotIn("llm_text", self.ev.__dict__)

    async def test_private_and_group_chat_still_work_during_paused_background_work(self):
        loop = self.loop()
        entered, release = asyncio.Event(), asyncio.Event()

        async def step(index):
            entered.set()
            await release.wait()

        await loop.start_work(self.ev, step, steps=2)
        await entered.wait()
        await loop.control("pause", self.ev)
        release.set()
        await until(lambda: self.work()["status"] == "paused")
        provider = SimpleNamespace(chat=AsyncMock(return_value=llm.ChatResult("private answer")))
        with patch.object(llm, "get_provider", return_value=provider):
            await loop.submit_message(self.ev, self.bot)
            await self.idle_chat(loop)
        self.assertIn("paused", provider.chat.await_args.args[0][0]["content"])
        self.assertEqual(self.bot.sent, [("reply", "private answer")])
        provider.chat.return_value = llm.ChatResult('{"messages":[{"text":"group answer"}]}')
        with patch.object(group_chat, "get_provider", return_value=provider):
            await loop.submit_message(event("group", mid="g1", is_at_bot=True), self.bot)
            await self.idle_chat(loop)
        self.assertEqual(self.bot.sent[-1][2], "group answer")
        self.assertEqual(self.work()["status"], "paused")

    async def test_agent_command_goes_through_same_queue_without_deadlock(self):
        loop = self.loop()
        self.ev.text = "/agent demo 2 0.05"
        await asyncio.wait_for(loop.submit_message(self.ev, self.bot), 1)
        self.assertIn("已开始", self.bot.sent[0][1])
        await until(lambda: self.work()["status"] == "completed")
        self.assertEqual(len(self.bot.sent), 1)  # 自动回报在后续阶段接入。
        await agent_command(self.ev, self.bot, MatchResult(text="status"))
        self.assertIn("已完成", self.bot.sent[-1][1])

    async def test_missing_loop_on_first_hot_update_gives_restart_hint(self):
        await agent_command(self.ev, self.bot, MatchResult(text="status"))
        self.assertIn("/restart", self.bot.sent[-1][1])

    async def test_hot_reload_does_not_spawn_or_replace_agent(self):
        from core import hotreload, middleware, tools
        loop = self.loop(AsyncMock())
        runner = loop._runner
        matchers, middlewares, registered = list(plugin._matchers), list(middleware._middlewares), dict(tools._tools)
        modules = {name: mod for name, mod in sys.modules.items() if hotreload._is_ext(name)}
        try:
            hotreload.reload_extensions()
            self.assertIs(agent.current(), loop)
            self.assertIs(loop._runner, runner)
        finally:
            plugin._matchers[:] = matchers
            middleware._middlewares[:] = middlewares
            tools._tools.clear()
            tools._tools.update(registered)
            for name in list(sys.modules):
                if hotreload._is_ext(name):
                    del sys.modules[name]
            sys.modules.update(modules)

    def bot_namespace(self, fake_adapter):
        namespace = {"__name__": "bot_loop_test", "__file__": str(Path(__file__).resolve().parents[1] / "bot.py")}
        with patch.object(config, "PLATFORMS", ["onebot"]), patch.object(config, "NAPCAT_WATCH", False), \
             patch("core.adapter.create_adapter", return_value=fake_adapter), patch("core.log.setup"):
            exec(compile(Path(namespace["__file__"]).read_text(), namespace["__file__"], "exec"), namespace)
        return namespace

    async def test_bot_routes_each_message_once_with_enable_switch_and_old_config(self):
        adapter = SimpleNamespace(bot=self.bot)
        namespace = self.bot_namespace(adapter)
        for enabled in (True, False):
            agent._current = None
            with patch.object(config, "AGENT_LOOP_ENABLED", enabled):
                loop = agent.start()
                self.loops.append(loop)
                with patch.object(plugin, "dispatch", AsyncMock()) as new, patch.dict(namespace, {"dispatch": AsyncMock()}) as ns:
                    # Agent 已捕获函数；明确注入观察入口，原路径仍由 bot 的 dispatch 负责。
                    loop._dispatch = new
                    await ns["on_event"](self.ev)
                    self.assertEqual(new.await_count, int(enabled))
                    self.assertEqual(ns["dispatch"].await_count, int(not enabled))
            await loop.close()
            with patch.dict(namespace, {"dispatch": AsyncMock()}) as ns:
                await ns["on_event"](self.ev)
                ns["dispatch"].assert_not_awaited()
        agent._current = None
        with patch.dict(config.__dict__):
            for key in ("AGENT_LOOP_ENABLED", "AGENT_WORK_MAX_STEPS", "AGENT_WORK_TIMEOUT"):
                config.__dict__.pop(key)
            loop = agent.start()
            self.loops.append(loop)
            self.assertTrue(loop.enabled)
            self.assertIs(agent.start(), loop)

    async def test_bot_run_owns_and_closes_agent_and_existing_background_tasks(self):
        started = asyncio.Event()

        async def run_adapter(callback):
            started.set()
            await asyncio.Future()

        adapter = SimpleNamespace(bot=self.bot, run=run_adapter)
        namespace = self.bot_namespace(adapter)
        with patch("core.sticker_learning.start"), patch("core.sticker_learning.close", AsyncMock()) as learning_close, \
             patch.dict(namespace, {"notify_after_restart": AsyncMock()}):
            task = asyncio.create_task(namespace["run"]())
            await started.wait()
            loop = agent.current()
            self.loops.append(loop)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertTrue(loop.closed)
            learning_close.assert_awaited_once()
            self.assertFalse(coalesce._pending)
