import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from adapters.onebot import OnebotBot
from adapters.onebot_errors import OnebotActionError
from config import FORWARD_REQUEST_TIMEOUT
from core import forwards, runtime
from core.event import MessageEvent
from core.forwards import ForwardInfo
from core.plugin import MatchResult
from core.tools import call_tool
from tools.forward import read_forward
from tools.poke import send_poke


# NapCat 的 PacketSendAvailableDepends.check 通过 websocketHandle 返回的结构。
PACKET_FAILURE = {
    "status": "failed", "retcode": 1400, "data": None,
    "message": "packetBackend发包能力不可用，请检查packetBackend状态！错误堆栈信息：access_token=do-not-expose",
    "wording": "packetBackend发包能力不可用",
    "echo": "fixture",
}


class ActionErrorTests(unittest.TestCase):
    def test_protocol_errors_retain_codes_and_classify_without_leaking_stack(self):
        cases = [
            (PACKET_FAILURE, "发包能力不可用"),
            ({"retcode": 1400, "message": "packetBackend不可用"}, "packetBackend 不可用"),
            ({"retcode": 1404, "message": "Unsupported API in current implementation"}, "未提供此动作"),
            ({"retcode": 1400, "message": "Schema compilation error: Expected string"}, "请求参数"),
            ({"retcode": 1200, "message": "消息已过期或者为内层消息，无法获取转发消息"}, "内层记录"),
            ({"retcode": 1200, "message": "ResId无效: 找不到相关的聊天记录"}, "已失效"),
            ({"retcode": 1200, "message": "operation timed out"}, "内部操作超时"),
        ]
        for response, expected in cases:
            with self.subTest(expected=expected):
                error = OnebotActionError("group_poke", response)
                self.assertIn(expected, str(error))
                self.assertIn(str(response["retcode"]), str(error))
                self.assertNotIn("access_token", str(error))
                self.assertNotIn("do-not-expose", str(error))
        for response in (None, [], {}, {"status": ["bad"], "retcode": "do-not-expose", "message": "do-not-expose"}):
            error = str(OnebotActionError("get_forward_msg", response))
            self.assertIn("retcode=未知", error)
            self.assertNotIn("do-not-expose", error)


class DiagnosticFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.event = MessageEvent(platform="onebot", message_type="group", user_id="1", group_id="2", self_id="4")
        self.bot = OnebotBot(None)
        runtime.set_context(self.event, self.bot)
        patcher = patch.object(forwards, "_registry", forwards.OrderedDict())
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_real_action_echo_path_delivers_packet_failure_to_tool(self):
        requests = []

        async def send(frame):
            request = json.loads(frame)
            requests.append(request)
            self.bot.resolve_resp({**PACKET_FAILURE, "echo": request["echo"]})

        self.bot.ws = SimpleNamespace(send=send)
        with self.assertLogs("qqbot", level="WARNING") as logs:
            result = await call_tool("send_poke", {})
        self.assertEqual(requests[0]["action"], "group_poke")
        self.assertEqual(requests[0]["params"], {"user_id": 1, "group_id": 2})
        self.assertEqual(len(requests), 1)
        self.assertEqual(self.bot._pending, {})
        self.assertIn("发包能力不可用", result)
        self.assertIn("retcode=1400", result)
        self.assertNotIn("do-not-expose", result + "".join(logs.output))
        self.assertNotIn("已在", result)

    async def test_forward_failure_is_specific_and_cache_remains_retryable(self):
        info = ForwardInfo(id="resource")
        sid = forwards.register(info, self.event.session_key)
        self.bot.call_action_resp = AsyncMock(return_value={
            "status": "failed", "retcode": 1200,
            "wording": "消息已过期或者为内层消息，无法获取转发消息",
        })
        result = await read_forward(sid)
        self.assertIn("内层记录", result)
        self.assertIn("retcode=1200", result)
        self.assertIsNone(info.messages)
        self.bot.call_action_resp.assert_awaited_once_with(
            "get_forward_msg", timeout=FORWARD_REQUEST_TIMEOUT, message_id="resource", id="resource",
        )
        self.bot.call_action_resp.return_value = {"status": "ok", "retcode": 0, "data": {"messages": []}}
        self.assertEqual(json.loads(await read_forward(sid))["messages"], [])

    async def test_timeout_reports_the_read_timeout_separately_from_expired_record(self):
        sid = forwards.register(ForwardInfo(id="resource"), self.event.session_key)
        self.bot.call_action_resp = AsyncMock(return_value=None)
        result = await read_forward(sid)
        self.assertIn("超时", result)
        self.assertIn(f"{FORWARD_REQUEST_TIMEOUT:g} 秒", result)
        self.assertNotIn("已失效", result)

    async def test_empty_inline_placeholder_with_remote_id_still_fetches_on_demand(self):
        from adapters.onebot import _normalize
        event = _normalize({
            "post_type": "message", "message_type": "private", "user_id": 1, "self_id": 4,
            "message": [{"type": "forward", "data": {"id": "resource", "content": []}}],
        })
        runtime.set_context(event, self.bot)
        self.assertIsNone(event.forwards[0].messages)
        sid = forwards.register(event.forwards[0], event.session_key)
        self.bot.call_action_resp = AsyncMock(return_value={
            "status": "ok", "retcode": 0, "data": {"messages": [{"message": "真实内容"}]},
        })
        self.bot.call_action_resp.assert_not_awaited()
        first = json.loads(await read_forward(sid))
        self.assertEqual(first["messages"][0]["text"], "真实内容")
        await read_forward(sid)
        self.bot.call_action_resp.assert_awaited_once()

    async def test_hot_reloaded_tools_report_old_adapter_requires_restart(self):
        old_bot = SimpleNamespace(platform="onebot")
        runtime.set_context(self.event, old_bot)
        self.assertIn("完整重启", await send_poke())
        sid = forwards.register(ForwardInfo(id="resource"), self.event.session_key)
        self.assertIn("完整重启", await read_forward(sid))


class ProbeTests(unittest.IsolatedAsyncioTestCase):
    async def test_owner_can_inspect_version_and_packet_failure_without_server_logs(self):
        from plugins.probe import start_probe
        event = MessageEvent(platform="feishu", message_type="private", user_id="owner")
        bot = SimpleNamespace(reply=AsyncMock())
        ob = OnebotBot(None)

        async def response(action, **params):
            if action == "get_version_info":
                return {"status": "ok", "retcode": 0, "data": {"app_name": "NapCat", "app_version": "4.18.41"}}
            if action == "nc_get_packet_status":
                return PACKET_FAILURE
            return {"status": "ok", "retcode": 0, "data": None}

        ob.call_action_resp = AsyncMock(side_effect=response)
        with patch("core.perms.master_id", return_value="owner"), \
             patch("plugins.probe.BOT_QQ", ""), \
             patch.object(runtime, "get_adapter_bot", return_value=ob):
            await start_probe(event, bot, MatchResult())
        actions = [args.args[0] for args in ob.call_action_resp.await_args_list]
        self.assertIn("get_version_info", actions)
        self.assertIn("nc_get_packet_status", actions)
        self.assertFalse(any("poke" in action for action in actions))
        result = bot.reply.await_args.args[1]
        self.assertIn("4.18.41", result)
        self.assertIn("retcode=1400", result)
        self.assertIn("发包能力不可用", result)
        self.assertIn("不能保证", result)
        self.assertIn("合并转发读取=已加载", result)
        self.assertNotIn("do-not-expose", result)

    async def test_probe_still_rejects_non_owner_before_reading_qq_state(self):
        from plugins.probe import start_probe
        event = MessageEvent(platform="onebot", message_type="private", user_id="visitor")
        bot = SimpleNamespace(reply=AsyncMock())
        with patch("core.perms.master_id", return_value="owner"), \
             patch.object(runtime, "get_adapter_bot") as adapter:
            await start_probe(event, bot, MatchResult())
        adapter.assert_not_called()
        self.assertIn("只有主人", bot.reply.await_args.args[1])


if __name__ == "__main__":
    unittest.main()
