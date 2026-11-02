import asyncio
import json
import re
import unittest
from collections import OrderedDict
from unittest.mock import AsyncMock, MagicMock, patch

from adapters.onebot import OnebotAdapter, OnebotBot, _normalize
from config import FORWARD_REQUEST_TIMEOUT
from core import forwards, images, plugin, runtime
from core.forwards import ForwardInfo
from core.group_context import build_group_snapshot
from core.session import Session
from core.tools import call_tool, get_tool_schemas
from tools.forward import read_forward


def seg(kind, **data):
    return {"type": kind, "data": data}


def incoming(message, **extra):
    return _normalize({
        "post_type": "message", "message_type": "group", "user_id": 1,
        "self_id": 4, "group_id": 2, "message_id": 3, "message": message,
        **extra,
    })


def poke(**extra):
    return {
        "post_type": "notice", "notice_type": "notify", "sub_type": "poke",
        "self_id": 4, "user_id": 1, "target_id": 4, "group_id": 2, "time": 123456,
        **extra,
    }


def ref(text):
    return re.search(r"合并转发#([a-f0-9]+)", text)[1]


def ok(messages, field="messages"):
    return {"status": "ok", "retcode": 0, "data": {field: messages}}


class PokeTests(unittest.IsolatedAsyncioTestCase):
    async def test_group_poke_is_directed_but_has_no_mention_or_quote_id(self):
        ev = _normalize(poke())
        self.assertTrue(ev.to_me)
        self.assertFalse(ev.is_at_bot)
        self.assertEqual(ev.at_list, [])
        self.assertEqual(ev.poke_target, "4")
        self.assertEqual(ev.timestamp, 123456)
        self.assertEqual(ev.session_key, "onebot_group_2")
        self.assertEqual(ev.message_id, "")
        self.assertIn("戳了戳 bot(4)", ev.llm_text)
        history = Session()
        history.add_group_event(ev)
        snapshot = build_group_snapshot(history)
        self.assertTrue(snapshot.directed)
        self.assertEqual(snapshot.reply_ids, set())
        record = json.loads(snapshot.messages[0]["content"][1]["text"])
        self.assertEqual(record["事件"], "戳一戳")
        self.assertEqual(record["戳一戳目标"], "4")
        bot = OnebotBot(None)
        bot.call_action_resp = AsyncMock(return_value=ok([]))
        await bot.reply(ev, "怎么啦")
        self.assertEqual(bot.call_action_resp.await_args.kwargs["message"],
                         [seg("text", text="怎么啦")])

    async def test_other_targets_are_background_and_self_pokes_are_ignored(self):
        self.assertFalse(_normalize(poke(target_id=8)).to_me)
        self.assertIsNone(_normalize(poke(user_id=4)))
        self.assertIsNone(_normalize(poke(notice_type="group_increase")))
        self.assertIsNone(_normalize(poke(target_id=0)))
        self.assertIsNone(_normalize(poke(user_id=None)))

    async def test_private_actor_uses_sender_id_and_no_self_reply_loop(self):
        ev = _normalize(poke(group_id=None, sender_id=7))
        self.assertEqual(ev.user_id, "7")
        self.assertEqual(ev.session_key, "onebot_private_7")
        self.assertTrue(ev.to_me)
        self.assertIsNone(_normalize(poke(group_id=None, sender_id=4, target_id=1)))
        self.assertIsNone(_normalize(poke(group_id=None, sender_id=4, target_id=4)))
        self.assertIsNone(_normalize(poke(group_id=None, target_id=8)))
        # 标准 OneBot 无 sender_id 时仍兼容。
        self.assertEqual(_normalize(poke(group_id=None)).user_id, "1")

    async def test_adapter_receive_loop_delivers_poke_notices(self):
        class Socket:
            def __aiter__(self):
                self.frames = iter([json.dumps(poke())])
                return self

            async def __anext__(self):
                try:
                    return next(self.frames)
                except StopIteration:
                    await asyncio.Future()

        connection = MagicMock()
        connection.__aenter__ = AsyncMock(return_value=Socket())
        connection.__aexit__ = AsyncMock(return_value=False)
        received = asyncio.Event()
        events = []

        async def on_event(ev):
            events.append(ev)
            received.set()

        adapter = OnebotAdapter()
        with patch("adapters.onebot.websockets.connect", return_value=connection):
            task = asyncio.create_task(adapter.run(on_event))
            try:
                await asyncio.wait_for(received.wait(), timeout=1)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].poke_target, "4")
        self.assertEqual(adapter.bot.self_id, "4")


class ForwardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        for patcher in (patch.object(forwards, "_registry", OrderedDict()),
                        patch.object(images, "_registry", OrderedDict())):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.bot = OnebotBot(None)
        self.bot.call_action_resp = AsyncMock(return_value=ok([]))

    def receive(self, message=None, **extra):
        ev = incoming(message if message is not None else [seg("forward", id="outer")], **extra)
        runtime.set_context(ev, self.bot)
        return ev, ref(ev.llm_text)

    async def test_forward_only_private_and_group_messages_are_visible_without_loading(self):
        for kind in ("group", "private"):
            ev, sid = self.receive(message_type=kind)
            self.assertEqual(ev.text, "")
            self.assertIn(sid, ev.llm_content())
            self.assertIn("未展开", ev.llm_text)
            self.assertEqual(ev.to_me, kind == "private")
            self.bot.call_action_resp.assert_not_awaited()
        schemas = {s["function"]["name"] for s in get_tool_schemas()}
        self.assertIn("read_forward", schemas)

    async def test_ignore_does_not_register_forward_or_enter_chat(self):
        ev = incoming([seg("text", text="/ignore "),
                       seg("forward", id="hidden", content=[{"message": "不能读取"}])])
        with patch.object(plugin, "run_middlewares", new_callable=AsyncMock) as middleware:
            await plugin.dispatch(ev, self.bot)
        middleware.assert_not_awaited()
        self.assertEqual(len(forwards._registry), 0)
        self.bot.call_action_resp.assert_not_awaited()

    async def test_model_tool_opens_only_requested_layer_and_child_requires_another_call(self):
        ev, sid = self.receive()
        parent = [{"sender": {"user_id": 9, "nickname": "甲"}, "time": 100,
                   "message": [seg("text", text="第一层"),
                               seg("forward", id="child"),
                               seg("image", file="image.jpg", url="https://example.test/image")]}]
        self.bot.call_action_resp.side_effect = [ok(parent), ok([{"message": "第二层"}])]
        with patch.object(images, "fetch_bytes", new_callable=AsyncMock) as fetch:
            first = json.loads(await call_tool("read_forward", {"forward_id": sid}))
            self.assertEqual(first["messages"][0]["text"], "第一层")
            self.assertEqual(first["messages"][0]["nickname"], "甲")
            self.assertEqual(first["messages"][0]["user_id"], "9")
            self.assertEqual(first["messages"][0]["time"], 100)
            self.assertNotIn("第二层", json.dumps(first, ensure_ascii=False))
            self.bot.call_action_resp.assert_awaited_once_with("get_forward_msg", timeout=FORWARD_REQUEST_TIMEOUT, message_id="outer", id="outer")
            child = ref(first["messages"][0]["forwards"][0])
            self.assertIsNotNone(images.get(re.search(r"图片#([^]]+)", first["messages"][0]["images"][0])[1]))
            fetch.assert_not_awaited()
            second = json.loads(await read_forward(child))
        self.assertEqual(second["messages"][0]["text"], "第二层")
        self.assertEqual(self.bot.call_action_resp.await_count, 2)
        self.assertEqual(runtime.get_event(), ev)  # 引用中的身份不能取代当前用户。

    async def test_inline_nested_nodes_stay_folded_and_open_without_network(self):
        nested = [seg("node", user_id="8", nickname="乙", content=[seg("text", text="内层秘密")])]
        body = [seg("forward", id="inline", content=[
            seg("node", user_id="7", nickname="甲", content=[seg("text", text="外层"), *nested])
        ])]
        ev, sid = self.receive(body)
        self.assertNotIn("外层", ev.llm_text)
        self.assertNotIn("内层秘密", ev.llm_text)
        first = json.loads(await read_forward(sid))
        row = first["messages"][0]
        self.assertEqual(row["nickname"], "甲")
        self.assertEqual(row["text"], "外层")
        self.assertNotIn("内层秘密", json.dumps(first, ensure_ascii=False))
        child = ref(row["forwards"][0])
        second = json.loads(await read_forward(child))
        self.assertEqual(second["messages"][0]["text"], "内层秘密")
        self.bot.call_action_resp.assert_not_awaited()
        # 重开父记录不产生新的子入口。
        again = json.loads(await read_forward(sid))
        self.assertEqual(again["messages"][0]["forwards"], row["forwards"])

    async def test_json_xml_cards_and_cq_strings_expose_real_forward_ids(self):
        card = {"app": "com.tencent.multimsg", "meta": {"detail": {"resid": "json-resource", "summary": "聊天记录"}}}
        xml = '<msg serviceID="35" m_resid="xml-resource" brief="聊天记录"/>'
        for message, resource in (([seg("json", data=json.dumps(card))], "json-resource"),
                                  ([seg("xml", data=xml)], "xml-resource"),
                                  ("[CQ:at,qq=4][CQ:forward,id=cq&#44;resource]", "cq,resource")):
            with self.subTest(resource=resource):
                ev, sid = self.receive(message)
                await read_forward(sid)
                self.assertEqual(self.bot.call_action_resp.await_args.kwargs["message_id"], resource)
        self.assertTrue(ev.to_me)
        literal = incoming("&#91;CQ:forward,id=fake&#93;")
        self.assertEqual(literal.forwards, [])
        self.assertEqual(literal.text, "[CQ:forward,id=fake]")

    async def test_malformed_cards_and_messages_do_not_crash(self):
        for message in (None, [None, 1, {"type": "text", "data": None}],
                        [seg("json", data="{")], [seg("json", data="[]")],
                        [seg("json", data=json.dumps({"app": "com.tencent.multimsg", "meta": None}))],
                        [seg("xml", data="<msg")], [seg("xml", data='<!DOCTYPE msg><msg/>')]):
            ev = incoming(message)
            self.assertEqual(ev.forwards, [])

    async def test_paginated_reads_use_cached_payload_and_preserve_both_node_formats(self):
        _, sid = self.receive()
        rows = [seg("node", user_id=str(i), nickname=f"用户{i}", content=f"第{i}条")
                for i in range(5)]
        self.bot.call_action_resp.return_value = ok(rows, field="message")
        first = json.loads(await read_forward(sid, limit=2))
        second = json.loads(await read_forward(sid, offset=first["next_offset"], limit=2))
        last = json.loads(await read_forward(sid, offset=second["next_offset"], limit=2))
        self.assertEqual([m["text"] for page in (first, second, last) for m in page["messages"]],
                         [f"第{i}条" for i in range(5)])
        self.assertIsNone(last["next_offset"])
        self.bot.call_action_resp.assert_awaited_once()

    async def test_page_budget_and_long_text_are_explicit(self):
        _, sid = self.receive()
        self.bot.call_action_resp.return_value = ok([{"message": "长" * 5000} for _ in range(5)])
        with patch("tools.forward.FORWARD_PAGE_CHARS", 5000):
            page = json.loads(await read_forward(sid))
        self.assertEqual(len(page["messages"]), 1)
        self.assertEqual(page["next_offset"], 1)
        self.assertTrue(page["messages"][0]["text_truncated"])

    async def test_failed_or_malformed_response_is_retryable_and_empty_is_valid(self):
        for response in (None, {"status": "failed", "retcode": 100}, {"status": "ok", "retcode": 100},
                         {"status": "ok", "data": {}}, {"status": "ok", "data": {"messages": None}}):
            with self.subTest(response=response):
                ev, sid = self.receive([seg("forward", id=f"failed-{response}")])
                self.bot.call_action_resp.return_value = response
                result = await read_forward(sid)
                self.assertIn("失败", result)
                self.assertIsNone(forwards.get(sid, ev.session_key).messages)
                self.bot.call_action_resp.return_value = ok([])
                retry = json.loads(await read_forward(sid))
                self.assertEqual(retry["total"], 0)
                self.assertIsNone(retry["next_offset"])

    async def test_cross_session_and_unknown_refs_cannot_fetch_and_inputs_are_checked(self):
        ev, sid = self.receive()
        other = incoming([seg("text", text=sid)], group_id=99)
        runtime.set_context(other, self.bot)
        self.assertIn("不在当前会话", await read_forward(sid))
        self.assertIn("不在当前会话", await read_forward("unknown"))
        runtime.set_context(ev, self.bot)
        for kwargs in ({"offset": -1}, {"offset": True}, {"limit": 0}, {"limit": 21}, {"limit": "2"}):
            self.assertIn("必须", await read_forward(sid, **kwargs))
        other.platform = "feishu"
        runtime.set_context(other, self.bot)
        self.assertIn("只能在 QQ", await read_forward(sid))
        self.bot.call_action_resp.assert_not_awaited()

    async def test_cyclic_remote_reference_never_recursively_fetches(self):
        _, sid = self.receive()
        self.bot.call_action_resp.return_value = ok([{"message": [seg("forward", id="outer")]}])
        first = json.loads(await read_forward(sid))
        self.assertEqual(ref(first["messages"][0]["forwards"][0]), sid)
        await read_forward(sid)
        self.bot.call_action_resp.assert_awaited_once()

    async def test_empty_content_placeholder_does_not_hide_node_message(self):
        _, sid = self.receive()
        self.bot.call_action_resp.return_value = ok([
            seg("node", user_id="7", nickname="甲", content=[], message=[
                seg("text", text="兼容字段中的正文"),
                seg("node", user_id="8", content=[], message=[seg("text", text="兼容字段中的嵌套")]),
            ]),
        ])
        page = json.loads(await read_forward(sid))
        self.assertEqual(page["messages"][0]["text"], "兼容字段中的正文")
        child = ref(page["messages"][0]["forwards"][0])
        nested = json.loads(await read_forward(child))
        self.assertEqual(nested["messages"][0]["text"], "兼容字段中的嵌套")
        self.bot.call_action_resp.assert_awaited_once()

    async def test_previous_turn_reference_and_parent_cache_survive_opening_children(self):
        ev, sid = self.receive()
        runtime.set_context(incoming([seg("text", text="看看刚才的记录")], message_id=10), self.bot)
        self.bot.call_action_resp.return_value = ok([
            {"message": [seg("forward", id=f"child-{i}") for i in range(3)]},
        ])
        with patch.object(forwards, "FORWARD_CONTEXT_MAX", 2):
            await read_forward(sid)
            self.assertIsNotNone(forwards.get(sid, ev.session_key))
            await read_forward(sid)
        self.bot.call_action_resp.assert_awaited_once()

    async def test_registry_expires_and_evicts_old_refs(self):
        with patch.object(forwards.time, "monotonic", return_value=0):
            sid = forwards.register(ForwardInfo(id="expired"), "session")
        with patch.object(forwards.time, "monotonic", return_value=forwards.HISTORY_TTL + 1):
            self.assertIsNone(forwards.get(sid, "session"))
        with patch.object(forwards, "FORWARD_CONTEXT_MAX", 2):
            first = forwards.register(ForwardInfo(id="first"), "session")
            forwards.register(ForwardInfo(id="second"), "session")
            third = forwards.register(ForwardInfo(id="third"), "session")
            self.assertIsNone(forwards.get(first, "session"))
            self.assertIsNotNone(forwards.get(third, "session"))


if __name__ == "__main__":
    unittest.main()
