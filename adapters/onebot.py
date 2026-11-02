"""OneBot 11 (NapCat) 适配器 —— WS 长连接 + 消息段解析/发送。

把原 `core/onebot.py` 的发送能力与 `bot.py` 的 WS 连接循环合并成一个 adapter：
`OnebotAdapter.run` 维持与 NapCat 的正向 WS 连接，把每帧原始事件归一成平台无关的
`MessageEvent` 喂给 `on_event`；`OnebotBot` 负责把 `MessagePart` 翻译成 OneBot 段发回。
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Awaitable, Callable

import websockets

from config import FORWARD_REQUEST_TIMEOUT, NAPCAT_TOKEN, NAPCAT_WS_URL
from core.adapter import Adapter, register_adapter
from core.bot import Bot, ImageSeg, MessagePart, TextSeg, to_parts
from core.event import MessageEvent
from core.log import log
from adapters.onebot_errors import OnebotActionError
from adapters.onebot_messages import (
    extract_at_target as _extract_at_target,
    extract_forwards,
    extract_images as _extract_images,
    extract_text as _extract_text,
    segments,
)


# ============================================================
#  OneBot 11 消息段解析（入站）
# ============================================================

def _normalize_poke(raw: dict) -> MessageEvent | None:
    if raw.get("notice_type") != "notify" or raw.get("sub_type") != "poke":
        return None
    self_id = str(raw.get("self_id") or "")
    group_id = str(raw.get("group_id") or "0")
    # NapCat 私聊 user_id 是会话对象；sender_id 才是实际操作者。
    actor = raw.get("user_id") if group_id != "0" else raw.get("sender_id", raw.get("user_id"))
    user_id = str(actor or "")
    target = str(raw.get("target_id") or "")
    if not all(value.isdecimal() and int(value) > 0 for value in (self_id, user_id, target)):
        return None
    if user_id == self_id or (group_id == "0" and target != self_id):
        return None
    sender = raw.get("sender") or {}
    nickname = sender.get("card") or sender.get("nickname") or user_id
    target_name = f"bot({self_id})" if target == self_id else f"用户({target})"
    return MessageEvent(
        platform="onebot", message_type="group" if group_id != "0" else "private",
        user_id=user_id, group_id=group_id, self_id=self_id,
        nickname=nickname, sender_display=f"{nickname}({user_id})",
        text=f"[QQ戳一戳通知：{nickname}({user_id}) 戳了戳 {target_name}]",
        poke_target=target, raw=raw, timestamp=float(raw.get("time") or time.time()),
    )


def _normalize(raw: dict) -> MessageEvent | None:
    """把 OneBot 原始事件 dict 归一成平台无关 MessageEvent。

    支持聊天消息与戳一戳通知；其余事件返回 None。
    """
    if raw.get("post_type") == "notice":
        return _normalize_poke(raw)
    if raw.get("post_type") != "message":
        return None

    sender = raw.get("sender") or {}
    user_id = str(raw.get("user_id", ""))
    self_id = str(raw.get("self_id", ""))
    group_id = str(raw.get("group_id", "0"))
    message = segments(raw.get("message", ""))

    nickname = sender.get("card") or sender.get("nickname") or user_id
    at_list = _extract_at_target(message)

    return MessageEvent(
        platform="onebot",
        message_type=raw.get("message_type", ""),
        user_id=user_id,
        group_id=group_id,
        message_id=str(raw.get("message_id", "")),
        self_id=self_id,
        nickname=nickname,
        sender_display=f"{nickname}({user_id})",
        text=_extract_text(message),
        at_list=at_list,
        images=_extract_images(message),
        forwards=extract_forwards(message),
        is_at_bot=bool(self_id) and self_id in at_list,
        raw=raw,
        timestamp=float(raw.get("time") or time.time()),
        reply_to=next((str(s["data"]["id"]) for s in message
                       if s.get("type") == "reply" and s.get("data", {}).get("id") is not None), None)
        if isinstance(message, list) else None,
    )


# ============================================================
#  OneBot 发送端
# ============================================================

def _parts_to_segments(parts: list[MessagePart]) -> list[dict]:
    """把平台无关 MessagePart 列表翻成 OneBot 11 消息段。"""
    segs: list[dict] = []
    for p in parts:
        if isinstance(p, TextSeg):
            segs.append({"type": "text", "data": {"text": p.text}})
        elif isinstance(p, ImageSeg):
            segs.append({"type": "image", "data": {"file": p.file}})
    return segs


class OnebotBot(Bot):
    """封装单个 OneBot WS 连接的发送能力。"""

    platform = "onebot"
    confirms_delivery = True

    def __init__(self, ws):
        self.ws = ws
        self.self_id = ""  # bot 自己的 QQ，从收到的事件里缓存，供回环探测等用
        self._pending: dict[str, asyncio.Future] = {}  # echo -> future，等动作返回
        self._echo_seq = 0

    async def call_action(self, action: str, **params) -> None:
        """通用 OneBot 动作调用（只发不收返回）。"""
        await self.ws.send(json.dumps({"action": action, "params": params}))

    async def call_action_resp(self, action: str, timeout: float = 8.0, **params) -> dict | None:
        """调用动作并等返回（用 echo 关联响应帧）。超时/出错返回 None。"""
        self._echo_seq += 1
        echo = f"act-{self._echo_seq}"
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[echo] = fut
        try:
            await self.ws.send(json.dumps({"action": action, "params": params, "echo": echo}))
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            return None
        finally:
            self._pending.pop(echo, None)

    def resolve_resp(self, raw: dict) -> bool:
        """收到带 echo 的响应帧时调用：唤醒对应的等待者。返回是否命中。"""
        echo = raw.get("echo")
        fut = self._pending.get(echo) if echo else None
        if fut is not None and not fut.done():
            fut.set_result(raw)
            return True
        return False

    async def send_private_msg(self, user_id: str, parts: str | list[MessagePart]) -> None:
        result = await self.call_action_resp(
            "send_private_msg", user_id=int(user_id), message=_parts_to_segments(to_parts(parts))
        )
        if result is None:
            raise TimeoutError("OneBot 私聊发送结果未确认")
        if not isinstance(result, dict) or result.get("status") != "ok" or result.get("retcode", 0) != 0:
            raise OnebotActionError("send_private_msg", result)

    async def send_group_msg(
        self,
        group_id: str,
        parts: str | list[MessagePart],
        reply_to: str | None = None,
    ) -> str | None:
        segs: list[dict] = []
        if reply_to is not None:
            segs.append({"type": "reply", "data": {"id": str(reply_to)}})
        segs.extend(_parts_to_segments(to_parts(parts)))
        result = await self.call_action_resp("send_group_msg", group_id=int(group_id), message=segs)
        if result is None or result.get("status") != "ok" or result.get("retcode", 0) != 0:
            raise RuntimeError("OneBot 群消息未确认发送成功")
        mid = (result.get("data") or {}).get("message_id")
        return str(mid) if mid is not None else None

    async def send_poke(self, user_id: str, group_id: str | None = None) -> None:
        """发送一次群/好友戳一戳，等待动作确认；失败不自动重试。"""
        params = {"user_id": int(user_id)}
        action = "friend_poke"
        if group_id is not None:
            action = "group_poke"
            params["group_id"] = int(group_id)
        result = await self.call_action_resp(action, **params)
        if result is None:
            raise TimeoutError("戳一戳发送结果未确认")
        if not isinstance(result, dict) or result.get("status") != "ok" or result.get("retcode") != 0:
            raise OnebotActionError(action, result)
        data = result.get("data")
        if isinstance(data, dict) and data.get("result", 0) != 0:
            raise RuntimeError("QQ 未确认戳一戳成功")

    async def get_forward_messages(self, forward_id: str) -> list:
        """只取指定合并转发的当前层，兼容 NapCat 和标准 OneBot 返回结构。"""
        # NapCat 文档使用 message_id，OneBot 11 标准使用 id；均为资源 ID，不转整数。
        result = await self.call_action_resp(
            "get_forward_msg", timeout=FORWARD_REQUEST_TIMEOUT, message_id=forward_id, id=forward_id,
        )
        if result is None:
            raise TimeoutError(f"读取合并转发超时（{FORWARD_REQUEST_TIMEOUT:g} 秒）")
        if not isinstance(result, dict) or result.get("status") != "ok" or result.get("retcode", 0) != 0:
            raise OnebotActionError("get_forward_msg", result)
        data = result.get("data")
        if isinstance(data, dict):
            data = data.get("messages", data.get("message"))
        if not isinstance(data, list):
            raise ValueError("OneBot 合并转发返回格式错误")
        return data

    async def reply(self, event: MessageEvent, parts: str | list[MessagePart]) -> None:
        """按事件来源回复（群聊带引用）。"""
        if event.message_type == "group":
            await self.send_group_msg(event.group_id, parts, reply_to=event.message_id or None)
        else:
            await self.send_private_msg(event.user_id, parts)

    async def send_image(self, event: MessageEvent, path: str) -> None:
        """按事件来源发一张本地图片（表情包）。"""
        parts = [ImageSeg(f"file://{path}")]
        if event.message_type == "group":
            await self.send_group_msg(event.group_id, parts)
        else:
            await self.send_private_msg(event.user_id, parts)


# ============================================================
#  OneBot 适配器（连接循环）
# ============================================================

class OnebotAdapter(Adapter):
    """维持与 NapCat 的正向 WS 连接，归一事件喂给上层。"""

    platform = "onebot"

    def __init__(self):
        self._bot: OnebotBot | None = None

    @property
    def bot(self) -> Bot:
        if self._bot is None:
            raise RuntimeError("OneBot 连接尚未建立")
        return self._bot

    async def run(self, on_event: Callable[[MessageEvent], Awaitable[None]]) -> None:
        headers = {}
        if NAPCAT_TOKEN:
            headers["Authorization"] = f"Bearer {NAPCAT_TOKEN}"

        while True:
            try:
                log.info(f"正在连接 NapCat: {NAPCAT_WS_URL}")
                async with websockets.connect(
                    NAPCAT_WS_URL,
                    additional_headers=headers,
                    ping_interval=30,
                    ping_timeout=10,
                ) as ws:
                    log.info("✅ 已连接 NapCat，开始监听事件")
                    self._bot = OnebotBot(ws)
                    async for raw_frame in ws:
                        try:
                            raw = json.loads(raw_frame)
                        except json.JSONDecodeError:
                            log.warning(f"收到非 JSON 数据: {raw_frame[:100]}")
                            continue

                        # 动作响应帧（带 echo、无 post_type）：唤醒等待者
                        if "echo" in raw and self._bot.resolve_resp(raw):
                            continue

                        if raw.get("self_id"):
                            self._bot.self_id = str(raw["self_id"])
                        event = _normalize(raw)
                        if event is None:
                            post_type = raw.get("post_type")
                            if post_type == "notice":
                                log.debug(f"[通知] {raw.get('notice_type')}")
                            elif post_type == "request":
                                log.info(f"[请求] {raw.get('request_type')}: {raw}")
                            continue
                        # 每条事件单独跑，不阻塞接收循环
                        asyncio.create_task(on_event(event))

            except (websockets.ConnectionClosed, ConnectionRefusedError, OSError) as e:
                log.warning(f"连接断开/失败: {e}，5 秒后重连...")
                await asyncio.sleep(5)
            except Exception as e:
                log.error(f"未知错误: {e}，5 秒后重连...")
                await asyncio.sleep(5)


# 注册到适配器工厂表
register_adapter("onebot", OnebotAdapter)
