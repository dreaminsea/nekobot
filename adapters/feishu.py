"""飞书 (Lark) 适配器 —— lark-oapi 长连接 (WebSocket) 实现。

`FeishuAdapter.run` 用 lark-oapi 的 ws.Client 维持与飞书的长连接，SDK 回调产出
`P2ImMessageReceiveV1` 事件后归一成平台无关的 `MessageEvent` 喂给 `on_event`；
`FeishuBot` 负责把富消息段 (`TextSeg`/`ImageSeg`) 翻成飞书消息发回。

与 OneBot 适配器对齐：群聊带引用回复、私聊直发、表情包学习/发送都支持。所有 id 统一
为 str（群用 chat_id，人用 open_id）。

集成要点（lark-oapi 1.6.x）：
  - `ws.Client.start()` 是阻塞的同步方法（内部用模块级 loop 跑），故丢到线程里跑
    （`asyncio.to_thread`），SDK 自带断线重连。
  - 事件回调 `_on_message` 是同步函数、在 ws 线程里被调用 → 只做归一 + 把 `on_event`
    用 `run_coroutine_threadsafe` 投回主 loop，立即返回，不阻塞收包。
  - 发送/下载用 SDK 的 async 方法（`acreate`/`areply`/`aget`/`arequest`），直接在主
    loop await，无需线程。

已知限制：
  - 发送者昵称退化为 open_id（receive 事件不含名字），暂不查 contact。
  - 飞书群里 bot 默认通常只在被 @ 时才收到消息（取决于应用事件权限），ambient/followup
    潜水接话在飞书可能受限——属平台权限行为，非代码问题。
  - 支持 image 消息和 post 富文本中的图片；自定义表情(sticker)消息类型暂不处理。
"""

from __future__ import annotations

import asyncio
import io
import json
import re
from functools import partial
from typing import Any, Awaitable, Callable

import httpx
import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    CreateImageRequest,
    CreateImageRequestBody,
    CreateMessageRequest,
    CreateMessageRequestBody,
    GetMessageResourceRequest,
    P2ImMessageReceiveV1,
    ReplyMessageRequest,
    ReplyMessageRequestBody,
)

from config import FEISHU_APP_ID, FEISHU_APP_SECRET, FEISHU_DOMAIN, FEISHU_LOG_LEVEL
from core.adapter import Adapter, register_adapter
from core.bot import Bot, ImageInfo, ImageSeg, MessagePart, TextSeg, to_parts
from core.event import MessageEvent
from core.log import log

# 下载外链图片用（发图时若 ImageSeg.file 是 url，先下载再上传）
_http = httpx.AsyncClient(timeout=30.0, follow_redirects=True)

# 飞书文本里 @人 的占位符形如 @_user_1，归一文本时剥掉
_MENTION_RE = re.compile(r"@_user_\d+")


# ============================================================
#  入站解析
# ============================================================

def _post_content(content: dict) -> dict:
    """接收事件通常直接给出正文，也兼容带语言包的 post。"""
    if "content" in content:
        return content
    for language in ("zh_cn", "en_us", *content):
        value = content.get(language)
        if isinstance(value, dict) and "content" in value:
            return value
    return {}


def _parse_image_keys(message: Any) -> list[str]:
    try:
        content = json.loads(message.content or "{}")
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(content, dict):
        return []
    if message.message_type == "image":
        keys = [content.get("image_key")]
    elif message.message_type == "post":
        keys = [el.get("image_key") for para in _post_content(content).get("content", [])
                for el in para if el.get("tag") == "img"]
    else:
        return []
    return list(dict.fromkeys(key for key in keys if isinstance(key, str) and key))


def _parse_text(message: Any) -> str:
    """从飞书消息 content(JSON 字符串) 提取纯文本（text / post）。"""
    raw = message.content or ""
    mt = message.message_type or ""
    try:
        c = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return ""
    if not isinstance(c, dict):
        return ""
    if mt == "text":
        return _MENTION_RE.sub("", c.get("text", "")).strip()
    if mt == "post":  # 富文本：content 是段落数组，逐元素取 text
        c = _post_content(c)
        out: list[str] = [c.get("title", "")]
        for para in c.get("content", []):
            for el in para:
                if el.get("tag") in ("text", "a"):
                    out.append(el.get("text", ""))
        return "".join(out).strip()
    return ""


# ============================================================
#  发送端
# ============================================================

class FeishuBot(Bot):
    """飞书发送端 —— 把富消息段翻成飞书消息发出。"""

    platform = "feishu"
    confirms_delivery = True

    def __init__(self, client: lark.Client, self_id: str):
        self.client = client
        self.self_id = self_id

    # ---- 底层发送 ----

    async def _create(self, rid_type: str, rid: str, msg_type: str, content: str) -> str | None:
        req = (
            CreateMessageRequest.builder()
            .receive_id_type(rid_type)
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(rid)
                .msg_type(msg_type)
                .content(content)
                .build()
            )
            .build()
        )
        resp = await self.client.im.v1.message.acreate(req)
        if not resp.success():
            raise RuntimeError(f"飞书发送失败 code={resp.code}")
        return resp.data.message_id if resp.data else None

    async def _reply(self, message_id: str, msg_type: str, content: str) -> str | None:
        req = (
            ReplyMessageRequest.builder()
            .message_id(message_id)
            .request_body(
                ReplyMessageRequestBody.builder().content(content).msg_type(msg_type).build()
            )
            .build()
        )
        resp = await self.client.im.v1.message.areply(req)
        if not resp.success():
            raise RuntimeError(f"飞书回复失败 code={resp.code}")
        return resp.data.message_id if resp.data else None

    async def _ensure_image_key(self, file: str) -> str | None:
        """把本地路径或 url 的图片上传到飞书，拿 image_key；失败返回 None。"""
        try:
            if file.startswith("http://") or file.startswith("https://"):
                resp = await _http.get(file)
                resp.raise_for_status()
                data = resp.content
            else:
                with open(file, "rb") as f:
                    data = f.read()
            req = (
                CreateImageRequest.builder()
                .request_body(
                    CreateImageRequestBody.builder()
                    .image_type("message")
                    .image(io.BytesIO(data))
                    .build()
                )
                .build()
            )
            up = await self.client.im.v1.image.acreate(req)
            if not up.success():
                log.warning(f"[飞书] 图片上传失败 code={up.code} msg={up.msg}")
                return None
            return up.data.image_key
        except Exception as e:
            log.warning(f"[飞书] 图片上传异常 {file[:60]}: {e!r}")
            return None

    async def _send_seg(self, rid_type: str, rid: str, seg: MessagePart, reply_to: str | None = None) -> str | None:
        """发一个消息段。`reply_to` 非空则用引用回复（仅首段）。"""
        if isinstance(seg, TextSeg):
            msg_type, content = "text", json.dumps({"text": seg.text}, ensure_ascii=False)
        elif isinstance(seg, ImageSeg):
            key = await self._ensure_image_key(seg.file)
            if not key:
                raise RuntimeError("飞书图片上传未成功，未发送")
            msg_type, content = "image", json.dumps({"image_key": key})
        else:
            return
        if reply_to:
            return await self._reply(reply_to, msg_type, content)
        else:
            return await self._create(rid_type, rid, msg_type, content)

    # ---- Bot 接口 ----

    async def send_private_msg(self, user_id: str, parts: str | list[MessagePart]) -> None:
        for seg in to_parts(parts):
            await self._send_seg("open_id", user_id, seg)

    async def send_group_msg(
        self, group_id: str, parts: str | list[MessagePart], reply_to: str | None = None
    ) -> str | None:
        # 飞书一条消息一个 msg_type，故按段拆发；首段在 reply_to 非空时用引用回复
        first_id = None
        for i, seg in enumerate(to_parts(parts)):
            mid = await self._send_seg("chat_id", group_id, seg, reply_to=reply_to if i == 0 else None)
            if i == 0:
                first_id = mid
        return first_id

    async def reply(self, event: MessageEvent, parts: str | list[MessagePart]) -> None:
        if event.message_type == "group":
            await self.send_group_msg(event.group_id, parts, reply_to=event.message_id)
        else:
            await self.send_private_msg(event.user_id, parts)

    async def send_image(self, event: MessageEvent, path: str) -> None:
        key = await self._ensure_image_key(path)
        if not key:
            raise RuntimeError("飞书图片上传未成功，未发送")
        content = json.dumps({"image_key": key})
        if event.message_type == "group":
            await self._create("chat_id", event.group_id, "image", content)
        else:
            await self._create("open_id", event.user_id, "image", content)


# ============================================================
#  适配器（长连接）
# ============================================================

def _log_level() -> lark.LogLevel:
    return getattr(lark.LogLevel, (FEISHU_LOG_LEVEL or "INFO").upper(), lark.LogLevel.INFO)


def _domain() -> str:
    """国内飞书(feishu) / 国际 Lark(lark) 端点。应用注册在哪个站点就连哪个。"""
    return lark.LARK_DOMAIN if (FEISHU_DOMAIN or "feishu").lower() == "lark" else lark.FEISHU_DOMAIN


class FeishuAdapter(Adapter):
    """维持与飞书的 ws 长连接，归一事件喂给上层。"""

    platform = "feishu"

    def __init__(self):
        self._bot: FeishuBot | None = None
        self._client: lark.Client | None = None
        self._self_id: str = ""
        self._loop: asyncio.AbstractEventLoop | None = None
        self._on_event: Callable[[MessageEvent], Awaitable[None]] | None = None

    @property
    def bot(self) -> Bot:
        if self._bot is None:
            raise RuntimeError("飞书连接尚未建立")
        return self._bot

    async def _fetch_bot_open_id(self) -> str:
        """查 bot 自己的 open_id，用于 @bot 判定。失败返回空串。"""
        req = (
            lark.BaseRequest.builder()
            .http_method(lark.HttpMethod.GET)
            .uri("/open-apis/bot/v3/info")
            .token_types({lark.AccessTokenType.TENANT})
            .build()
        )
        try:
            resp = await self._client.arequest(req)
            if not resp.success():
                log.warning(f"[飞书] 查 bot 信息失败 code={resp.code} msg={resp.msg}")
                return ""
            data = json.loads(resp.raw.content)
            return data.get("bot", {}).get("open_id", "")
        except Exception as e:
            log.warning(f"[飞书] 查 bot open_id 异常: {e!r}")
            return ""

    async def _download_image(self, message_id: str, image_key: str) -> bytes:
        """下载入站图片二进制（表情包入库用）。"""
        req = (
            GetMessageResourceRequest.builder()
            .message_id(message_id)
            .file_key(image_key)
            .type("image")
            .build()
        )
        resp = await self._client.im.v1.message_resource.aget(req)
        if not resp.success():
            raise RuntimeError(f"下载图片失败 code={resp.code} msg={resp.msg}")
        f = resp.file
        return f.read() if hasattr(f, "read") else bytes(f)

    def _normalize(self, data: P2ImMessageReceiveV1) -> MessageEvent | None:
        """飞书事件 → 平台无关 MessageEvent。"""
        msg = data.event.message if data.event else None
        if msg is None:
            return None

        message_type = "group" if (msg.chat_type or "") == "group" else "private"
        open_id = ""
        sender = data.event.sender
        if sender and sender.sender_id:
            open_id = sender.sender_id.open_id or ""
        chat_id = msg.chat_id or ""
        mid = msg.message_id or ""
        group_id = chat_id if message_type == "group" else "0"

        mentions = msg.mentions or []
        at_list = [m.id.open_id for m in mentions if m.id and m.id.open_id]

        images = [ImageInfo(key=key, url="", fetch=partial(self._download_image, mid, key))
                  for key in _parse_image_keys(msg)]

        nickname = open_id or "未知"
        return MessageEvent(
            platform="feishu",
            message_type=message_type,
            user_id=open_id,
            group_id=group_id,
            message_id=mid,
            self_id=self._self_id,
            nickname=nickname,
            sender_display=f"{nickname}({open_id})",
            text=_parse_text(msg),
            at_list=at_list,
            images=images,
            is_at_bot=bool(self._self_id) and self._self_id in at_list,
            raw=data,
            **({"timestamp": int(msg.create_time) / 1000} if getattr(msg, "create_time", None) else {}),
            reply_to=getattr(msg, "parent_id", None) or None,
        )

    def _on_message(self, data: P2ImMessageReceiveV1) -> None:
        """SDK 同步回调（ws 线程）：归一后投回主 loop，立即返回。"""
        try:
            event = self._normalize(data)
        except Exception as e:
            log.error(f"[飞书] 事件归一异常: {e!r}")
            return
        if event is None or self._loop is None or self._on_event is None:
            return
        asyncio.run_coroutine_threadsafe(self._on_event(event), self._loop)

    async def run(self, on_event: Callable[[MessageEvent], Awaitable[None]]) -> None:
        if not FEISHU_APP_ID or not FEISHU_APP_SECRET:
            raise RuntimeError("飞书未配置：请在 .env 填 FEISHU_APP_ID / FEISHU_APP_SECRET")

        domain = _domain()
        self._loop = asyncio.get_running_loop()
        self._on_event = on_event
        self._client = (
            lark.Client.builder()
            .app_id(FEISHU_APP_ID)
            .app_secret(FEISHU_APP_SECRET)
            .domain(domain)
            .build()
        )
        self._self_id = await self._fetch_bot_open_id()
        self._bot = FeishuBot(self._client, self._self_id)

        handler = (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(self._on_message)
            .build()
        )
        ws = lark.ws.Client(
            FEISHU_APP_ID,
            FEISHU_APP_SECRET,
            event_handler=handler,
            domain=domain,
            log_level=_log_level(),
        )
        log.info(f"飞书：建立长连接 domain={domain} self_id={self._self_id or '未知(查bot信息失败)'}")
        # start() 阻塞且用自带 loop+重连，丢到线程跑；连上/收事件的细节看 SDK 自身日志（DEBUG 更详细）
        await asyncio.to_thread(ws.start)


register_adapter("feishu", FeishuAdapter)
