"""平台无关的消息事件封装。

`MessageEvent` 是插件/中间件/工具的统一输入。adapter 负责把平台原始数据
填进基础字段（文本、@列表、图片等），core 在这里算出派生字段
（触发判定 `to_me`、前缀剥离 `command_text`、会话键 `session_key` 等），
下游只读这些派生字段即可，无需感知平台。

所有 id 统一为 `str`（QQ 号也以字符串携带），方便跨平台。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from functools import cached_property
from typing import Any

from config import ADMIN_FEISHU, ADMIN_QQ, GROUP_TRIGGER
from core.bot import ImageInfo
from core.forwards import ForwardInfo


@dataclass
class MessageEvent:
    """一条平台无关的消息事件。

    基础字段由 adapter 填充；派生字段（cached_property）由 core 计算。
    """

    # ---- adapter 填充的基础字段 ----
    platform: str                      # "onebot" / "feishu" …
    message_type: str                  # "group" / "private"
    user_id: str                       # 发言人 id（QQ 号 / open_id）
    group_id: str = "0"                # 群 id，私聊为 "0"
    message_id: str = ""               # 消息 id，用于引用回复
    self_id: str = ""                  # bot 自己的 id
    nickname: str = ""                 # 发言人显示名
    sender_display: str = ""           # "昵称(id)" 形式，给模型分辨发言人
    text: str = ""                     # 纯文本正文
    at_list: list[str] = field(default_factory=list)  # 被 @ 的 id 列表
    images: list[ImageInfo] = field(default_factory=list)  # 图片信息
    is_at_bot: bool = False            # 是否 @ 了 bot
    raw: Any = None                    # 平台原始数据，调试用
    timestamp: float = field(default_factory=time.time)
    reply_to: str | None = None        # 被引用的原消息 id

    forwards: list[ForwardInfo] = field(default_factory=list)  # 尚未展开的合并转发入口
    poke_target: str | None = None     # 戳一戳的目标；通知没有可引用的 message_id
    synthetic: bool = False            # bot 自己合成的后台通知（如请求完成），不是真人发言

    # ---- 派生字段（cached_property）----
    # 注意：dataclass + cached_property 需把 __dict__ 当缓存，故不能用 slots。

    @property
    def has_image(self) -> bool:
        return bool(self.images)

    @cached_property
    def llm_text(self) -> str:
        """给 LLM 看的正文：文本、图片标记和未展开的转发入口。

        日志和纯文本调用使用此摘要；主对话用 llm_content 同时保存原图引用。
        """
        return self._render_text(register_refs=True)

    def _render_text(self, *, register_refs: bool) -> str:
        from core import forwards, images, stickers
        parts: list[str] = []
        if self.text:
            parts.append(self.text)
        for img in self.images:
            meaning = stickers.meaning_of(img.key)
            if meaning:
                parts.append(f"[表情包:{meaning}]")
            sid = images.register(img) if register_refs else images.short_id(img)
            parts.append(f"[图片#{sid}]" if sid else "[图片]")
        for forward in self.forwards:
            parts.append("\n" + forwards.placeholder(forward, self.session_key, register_ref=register_refs))
        body = "".join(parts).strip()
        if not body and self.message_type == "group" and self.is_at_bot:
            return "[单独 @ 机器人]"
        return body

    def llm_content(self, prefix: str = "", *, register_refs: bool = True) -> str | list[dict]:
        """同步登记图文，防抖前不等待下载；图片在发起 LLM 请求时解析。"""
        from core import images

        text = prefix + (self.llm_text if register_refs else self._render_text(register_refs=False))
        if not self.images:
            return text
        parts = [{"type": "text", "text": text}]
        for img in self.images:
            sid = images.short_id(img)  # 登记已在 llm_text 完成；预览只计算编号。
            if sid:
                parts.append({"type": "image_ref", "image_id": sid})
        return parts

    @property
    def is_admin(self) -> bool:
        admins = ADMIN_FEISHU if self.platform == "feishu" else ADMIN_QQ
        return self.user_id in admins

    @property
    def session_key(self) -> str:
        if self.message_type == "group":
            return f"{self.platform}_group_{self.group_id}"
        return f"{self.platform}_private_{self.user_id}"

    @cached_property
    def _trigger_prefix(self) -> str | None:
        """命中的群聊触发前缀（小写匹配），未命中返回 None。"""
        low = self.text.lower()
        for prefix in GROUP_TRIGGER:
            if low.startswith(prefix.lower()):
                return prefix
        return None

    @cached_property
    def to_me(self) -> bool:
        """私聊恒为 True；群聊需 @bot、戳 bot 或命中触发前缀。"""
        if self.message_type == "private":
            return True
        return self.is_at_bot or bool(self.self_id and self.poke_target == self.self_id) or self._trigger_prefix is not None

    @cached_property
    def command_text(self) -> str:
        """剥离群聊触发前缀后的正文（私聊或未命中前缀时即原文）。"""
        prefix = self._trigger_prefix
        if prefix is not None:
            return self.text[len(prefix):].strip()
        return self.text
