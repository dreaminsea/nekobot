import asyncio
from collections import defaultdict
import json
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import config
from core import agent, coalesce, engagement, group_chat, llm, notices, perms, runtime, session, storage, timeline
from core import tools as tool_registry
from core.activities import timer
from core.activity import Action, Activity, Verdict
from core.plugin import MatchResult
from plugins.agent import agent_command
from tests.test_replies import FakeBot
from tests.test_timeline import event
from tools import activity as activity_tool
from tools import master


class Echo(Activity):
    name = "echo"
    title = "回声"
    actions = (Action("now", "立刻完成", everyone=True), Action("later", "稍后完成"))

    def __init__(self):
        super().__init__()
        self.started = self.stopped = 0

    async def start(self):
        self.started += 1

    async def stop(self):
        self.stopped += 1

    def status(self):
        return "随时待命"

    def accept(self, request):
        if request.action == "now":
            return Verdict.done("回声：" + str(request.args.get("text", "")))
        return Verdict.accepted("稍后回声")


class ActivityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.bot = FakeBot()
        self.bot.confirms_delivery = True
        for p in (
            patch.object(storage, "DATA_DIR", self.temp.name),
            patch.object(timeline, "_data", {"version": 1, "experiences": [], "current_state": None}),
            patch.object(timeline, "_dirty", False), patch.object(timeline, "_writable", True),
            patch.object(timeline, "_status_provider", None),
            patch.object(config, "SESSION_PERSIST", True), patch.object(config, "AGENT_CONTEXT_ENABLED", True),
            patch.object(perms, "MASTER_QQ", "1"), patch.object(perms, "MASTER_FEISHU", ""),
            patch.object(agent, "_current", None),
            patch.object(coalesce, "_pending", {}), patch.object(coalesce, "REPLY_DEBOUNCE", 0),
            patch.object(coalesce, "GROUP_IDLE_DEBOUNCE", 0),
            patch.object(session, "sessions", defaultdict(session.Session)),
            patch.object(engagement, "_last_reply_at", {}),
            patch.object(runtime, "_adapters", {"onebot": SimpleNamespace(bot=self.bot)}),
            patch.object(group_chat, "AMBIENT_CHAT", False), patch.object(group_chat, "FOLLOWUP_CHAT", False),
            patch.object(group_chat, "GROUP_DEDUP_GATE", False),
        ):
            p.start()
            self.addCleanup(p.stop)
        self.master = event()  # 主人私聊（QQ 1）
        self.member = event("group", user="5", mid="g1", sender_display="小明(5)")
        self.loops = []
        self.addAsyncCleanup(self.cleanup)
        runtime.set_context(None, None)

    async def cleanup(self):
        for loop in self.loops:
            await loop.close()
        await coalesce.close()

    async def loop(self, *activities, **kwargs):
        loop = agent.AgentLoop(AsyncMock(), **kwargs)
        for activity in activities:
            loop.register(activity, autostart=True)
        self.loops.append(loop)
        agent._current = loop
        loop.start()
        await asyncio.sleep(0)
        return loop

    async def settle(self, loop):
        await loop.wait_idle()
        tasks = [p.task for p in coalesce._pending.values() if p.task]
        if tasks:
            await asyncio.wait_for(asyncio.gather(*tasks), 2)

    def notes(self):
        return [r["text"] for r in timeline.snapshot()["experiences"] if r["kind"] == "activity"]

    async def test_arbitration_checks_running_action_permission_and_args(self):
        echo = Echo()
        loop = await self.loop(echo)
        self.assertIn("没有在运行", await loop.request(self.member, "timer", "remind", {}))
        self.assertIn("没有 nope", await loop.request(self.member, "echo", "nope", {}))
        self.assertIn("只有主人", await loop.request(self.member, "echo", "later", {}))
        self.assertIn("参数需要是对象", await loop.request(self.member, "echo", "now", "x"))
        self.assertIn("没有来源会话", await loop.request(None, "echo", "now", {}))
        self.assertEqual(await loop.request(self.member, "echo", "now", {"text": "hi"}), "已完成：回声：hi")
        answer = await loop.request(self.master, "echo", "later", {})
        self.assertIn("已接受请求 #2", answer)
        self.assertIn("不要说已经完成", answer)
        lines = loop.status_lines(self.master.session_key)
        self.assertIn("后台活动「回声」运行中：随时待命", lines)
        self.assertTrue(any("#2 echo.later" in l and "本会话发起" in l for l in lines))
        prompt = timeline.as_prompt(self.member.session_key)
        self.assertIn("随时待命", prompt)
        self.assertIn("接受了", prompt)

    async def test_reminder_reports_back_to_the_group_that_asked(self):
        loop = await self.loop(timer.TimerActivity())
        reply = json.dumps({"messages": [{"text": "小明，该喝水啦", "reply_to": "g1"}]}, ensure_ascii=False)
        provider = SimpleNamespace(chat=AsyncMock(return_value=llm.ChatResult(reply)))
        session.get_session(self.member.session_key).add_group_event(self.member)
        with patch.object(timer.asyncio, "sleep", AsyncMock()), patch.object(group_chat, "get_provider", return_value=provider):
            answer = await loop.request(self.member, "timer", "remind", {"seconds": 600, "text": "喝水"})
            self.assertIn("10 分钟后提醒：喝水", answer)
            await self.settle(loop)
            await self.settle(loop)
        self.assertEqual(self.bot.sent, [("group", "2", "小明，该喝水啦", "g1")])
        request = provider.chat.await_args.args[0]
        self.assertIn(notices.NOTICE_USER, request[0]["content"])
        timeline_text = json.dumps(request[1:], ensure_ascii=False)
        self.assertIn("提醒时间到：喝水", timeline_text)
        self.assertIn("原消息ID g1", timeline_text)
        kinds = [r["kind"] for r in timeline.snapshot()["experiences"]]
        self.assertNotIn("received", kinds)  # 合成通知不当作有人说话
        self.assertTrue(any("已完成" in n for n in self.notes()))
        self.assertEqual(loop.status_lines(self.member.session_key), ["后台活动「定时提醒」运行中：没有等待中的提醒"])

    async def test_private_notice_keeps_the_person_and_marks_the_source(self):
        loop = await self.loop(Echo())
        provider = SimpleNamespace(chat=AsyncMock(return_value=llm.ChatResult("好啦")))
        await loop.request(self.master, "echo", "later", {})
        with patch.object(llm, "get_provider", return_value=provider):
            loop.finish("echo", 1, True, "回声来了")
            await self.settle(loop)
            await self.settle(loop)
        self.assertEqual(self.bot.sent, [("reply", "好啦")])
        history = provider.chat.await_args.args[0]
        self.assertIn("【后台通知，不是对方发的消息】", history[-1]["content"])
        self.assertIn("回声来了", history[-1]["content"])
        loop.finish("echo", 1, True, "重复的完成通知")
        await self.settle(loop)
        self.assertEqual(len(self.bot.sent), 1)

    async def test_cancel_rules_and_who_gets_notified(self):
        loop = await self.loop(timer.TimerActivity())
        deliver = AsyncMock()
        with patch.object(notices, "deliver", deliver):
            await loop.request(self.member, "timer", "remind", {"seconds": 60, "text": "a"})
            await loop.request(self.member, "timer", "remind", {"seconds": 60, "text": "b"})
            stranger = event("group", user="6", mid="g2")
            self.assertIn("只有发起提醒的人或主人", await loop.request(stranger, "timer", "cancel", {"request_id": 1}))
            self.assertEqual(await loop.request(self.member, "timer", "cancel", {"request_id": 1}), "已完成：已取消提醒 #1")
            await self.settle(loop)
            deliver.assert_not_awaited()  # 发起人自己取消，当场的回复已经说明
            self.assertIn("已完成", await loop.request(self.master, "timer", "cancel", {"request_id": 2}))
            await self.settle(loop)
        deliver.assert_awaited_once()
        origin, text = deliver.await_args.args
        self.assertEqual(origin["session_key"], self.member.session_key)
        self.assertIn("取消", text)
        self.assertIn("没有这个", await loop.request(self.member, "timer", "cancel", {"request_id": 2}))
        for bad in ({"seconds": 5, "text": "x"}, {"seconds": True, "text": "x"}, {"seconds": 60, "text": ""}):
            self.assertIn("未接受", await loop.request(self.member, "timer", "remind", bad))

    async def test_master_controls_activities_and_stop_cancels_pending_requests(self):
        echo = Echo()
        loop = await self.loop()
        loop.register(echo)
        await agent_command(self.member, self.bot, MatchResult(text="start echo"))
        self.assertIn("只能由主人", self.bot.sent[-1][1])
        await agent_command(self.master, self.bot, MatchResult(text="start nope"))
        self.assertIn("没有名为 nope", self.bot.sent[-1][1])
        await agent_command(self.master, self.bot, MatchResult(text="start echo"))
        self.assertIn("已启动", self.bot.sent[-1][1])
        await agent_command(self.master, self.bot, MatchResult(text="status"))
        self.assertIn("回声(echo) 运行中，随时待命", self.bot.sent[-1][1])
        await loop.request(self.master, "echo", "later", {})
        deliver = AsyncMock()
        with patch.object(notices, "deliver", deliver):
            await agent_command(self.master, self.bot, MatchResult(text="stop echo"))
            await self.settle(loop)
        self.assertIn("已停止", self.bot.sent[-1][1])
        self.assertEqual(echo.stopped, 1)
        self.assertIn("取消", deliver.await_args.args[1])
        self.assertIn("没有在运行", await loop.request(self.master, "echo", "now", {}))

    async def test_shutdown_stops_activities_without_sending_notices(self):
        echo = Echo()
        loop = await self.loop(echo)
        self.assertEqual(echo.started, 1)
        await loop.request(self.master, "echo", "later", {})
        with patch.object(notices, "deliver", AsyncMock()) as deliver:
            await loop.close()
        deliver.assert_not_awaited()
        self.assertEqual(echo.stopped, 1)
        self.assertEqual(loop.status_lines(self.master.session_key), [])
        self.assertIsNone(timeline._status_provider)
        self.assertTrue(any("取消" in n for n in self.notes()))

    async def test_llm_budget_is_hourly_and_warns_master_once(self):
        loop = await self.loop(llm_per_hour=2)
        with patch.object(notices, "notify_master", AsyncMock()) as notify:
            self.assertEqual([loop.allow_llm("echo") for _ in range(4)], [True, True, False, False])
            await self.settle(loop)
        notify.assert_awaited_once()
        self.assertEqual(sum("用完" in n for n in self.notes()), 1)
        loop._llm_calls = type(loop._llm_calls)(t - 3600 for t in loop._llm_calls)
        self.assertTrue(loop.allow_llm("echo"))

    async def test_request_tool_lists_running_actions_and_uses_current_message(self):
        schema = next(s for s in tool_registry.get_tool_schemas() if s["function"]["name"] == "request_action")
        self.assertIn("没有运行中的后台活动", schema["function"]["description"])
        self.assertIn("外层循环未运行", await activity_tool.request_action("timer", "remind"))
        await self.loop(timer.TimerActivity())
        schema = next(s for s in tool_registry.get_tool_schemas() if s["function"]["name"] == "request_action")
        self.assertIn("timer.remind(seconds, text)", schema["function"]["description"])
        self.assertIn("所有人可请求", schema["function"]["description"])
        runtime.set_context(self.member, self.bot)
        self.assertIn("已接受请求 #1", await activity_tool.request_action("timer", "remind", {"seconds": 30, "text": "x"}))

    async def test_tool_scopes_keep_activity_tools_out_of_chat(self):
        tool = tool_registry.Tool("dig", "挖", {}, lambda: "ok", frozenset({"game"}))
        both = tool_registry.Tool("look", "看", {}, lambda: "ok", frozenset({"game", "chat"}))
        with patch.dict(tool_registry._tools, {"dig": tool, "look": both}):
            chat_names = {s["function"]["name"] for s in tool_registry.get_tool_schemas()}
            game_names = {s["function"]["name"] for s in tool_registry.get_tool_schemas("game")}
            self.assertNotIn("dig", chat_names)
            self.assertIn("look", chat_names)
            self.assertEqual(game_names, {"dig", "look"})
            self.assertFalse(tool_registry.has_tools("nothing"))
        registered = dict(tool_registry._tools)
        try:
            tool_registry.tool(name="scoped", scope=("a", "b"))(lambda: "ok")
            self.assertEqual(tool_registry._tools["scoped"].scopes, frozenset({"a", "b"}))
        finally:
            tool_registry._tools.clear()
            tool_registry._tools.update(registered)

    @patch.object(master, "MASTER_QQ", "1")
    async def test_contact_master_without_a_current_message_uses_a_connected_platform(self):
        self.assertEqual(await master.contact_master("后台有事"), "已经把消息悄悄发给主人啦~")
        self.assertEqual(self.bot.sent, [("private", "1", "后台有事")])
        with patch.object(runtime, "_adapters", {}):
            self.assertIn("联系不上", await master.contact_master("x"))

    async def test_module_start_registers_builtin_and_autostarts_configured(self):
        with patch.object(config, "AGENT_ACTIVITIES", ["timer", "unknown"]):
            loop = agent.start()
        self.loops.append(loop)
        await asyncio.sleep(0)
        await loop.wait_idle()
        self.assertEqual(loop._running, {"timer"})
        await loop.close()
        agent._current = None
        with patch.object(config, "AGENT_ACTIVITIES", []):
            loop = agent.start()
        self.loops.append(loop)
        await loop.wait_idle()
        self.assertEqual(loop._running, set())
        self.assertIn("timer", loop._activities)


if __name__ == "__main__":
    unittest.main()
