"""会话历史管理（含本地持久化）。"""

from __future__ import annotations

import asyncio
import os
import shutil
import time
from collections import defaultdict
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Callable

from config import DATA_DIR, GROUP_HISTORY_MESSAGES, HISTORY_TTL, MAX_HISTORY, SESSION_PERSIST
from core import storage, timeline
from core.log import log

if TYPE_CHECKING:
    from core.event import MessageEvent

_SESSION_FILE = "sessions.json"
_dirty = False


def mark_dirty() -> None:
    global _dirty
    _dirty = True


@dataclass
class Session:
    messages: list = field(default_factory=list)
    scope: str = field(default="", repr=False, compare=False, kw_only=True)  # 运行时关联，不改旧存储格式
    last_active: float = field(default_factory=time.time)
    # 串行化同一会话的并发处理（群聊多人同时触发时防止历史错乱）
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False, compare=False)
    group_mode: bool = False
    group_seq: int = 0
    group_cursor: int = 0
    generation: int = 0  # clear 后让在途生成失效，不持久化
    group_events: dict[int, MessageEvent] = field(default_factory=dict, repr=False, compare=False)
    group_inflight: int = field(default=0, repr=False, compare=False, kw_only=True)  # 正在生成的批次终点
    group_gate: asyncio.Task | None = field(default=None, repr=False, compare=False, kw_only=True)
    _changed: Callable[[], None] = field(default=mark_dirty, repr=False, compare=False, kw_only=True)

    def add_group_event(self, event: MessageEvent, *, register_refs: bool = True) -> bool:
        self.group_mode = True
        if event.message_id and any(
            m.get("_group", {}).get("id") == event.message_id for m in self.messages
        ):
            return False
        self.group_seq += 1
        self.group_events[self.group_seq] = event
        quoted_bot = bool(event.reply_to) and any(
            m.get("role") == "assistant" and m.get("_group", {}).get("id") == event.reply_to
            for m in self.messages
        )
        self.messages.append({
            "role": "user", "content": event.llm_content(register_refs=register_refs),
            "_group": {
                "seq": self.group_seq, "id": event.message_id or None,
                "sender": event.sender_display or event.user_id, "user_id": event.user_id,
                "time": event.timestamp, "mentions": list(event.at_list),
                "reply_to": event.reply_to, "directed": event.to_me or quoted_bot,
                "poke_target": event.poke_target,
            },
        })
        self.last_active = time.time()
        self._trim()
        self._changed()
        return True

    def add_group_reply(self, text: str, reply_to: str | None, message_id: str | None = None) -> None:
        self.messages.append({
            "role": "assistant", "content": text,
            "_group": {"id": message_id, "sender": "BOT", "time": time.time(), "reply_to": reply_to},
        })
        self.last_active = time.time()
        self._trim()
        self._changed()

    def finish_group_batch(self, through: int) -> None:
        self.group_cursor = max(self.group_cursor, through)
        self.group_events = {seq: event for seq, event in self.group_events.items() if seq > self.group_cursor}
        self._trim()
        self._changed()

    def add_user(self, content: str | list[dict]):
        self.messages.append({"role": "user", "content": content})
        self.last_active = time.time()
        self._trim()
        self._changed()

    def add_assistant(self, text: str):
        self.messages.append({"role": "assistant", "content": text})
        self.last_active = time.time()
        self._trim()
        self._changed()

    def add_reply(self, messages: list[dict], after: dict) -> None:
        """把完整工具往返插回其请求快照之后，避免下载/生成期间的新消息串轮。"""
        for i, message in enumerate(self.messages):
            if message is after:
                self.messages[i + 1:i + 1] = messages
                self.last_active = time.time()
                self._trim()
                self._changed()
                return
        # 原请求已被清空或裁掉时，不把旧回复重新写回新会话。

    def cancel_group_gate(self) -> None:
        if self.group_gate is not None:
            self.group_gate.cancel()
            self.group_gate = None

    def clear(self, *, experience_before: float | None = None):
        self.cancel_group_gate()
        if self.scope:
            timeline.clear_scope(self.scope, before=experience_before)
        self.messages.clear()
        self.group_cursor = self.group_seq
        self.group_inflight = 0
        self.group_events.clear()
        self.generation += 1
        self._changed()

    def is_expired(self) -> bool:
        return (time.time() - self.last_active) > HISTORY_TTL

    def _trim(self):
        if self.group_mode:
            # 已处理历史有界；尚未处理的消息不能被后来刷屏挤掉，由上下文构造器分批消费。
            old = [i for i, m in enumerate(self.messages)
                   if m.get("_group", {}).get("seq", 0) <= self.group_cursor]
            discard = set(old[:-max(1, GROUP_HISTORY_MESSAGES)])
            self.messages[:] = [m for i, m in enumerate(self.messages) if i not in discard]
            return
        # 按用户消息计算轮数；一轮携带其后的完整 assistant/tool 往返。
        # 工具调用再多也不额外挤占历史，连续到达但尚未回复的用户消息同样有界。
        starts = [i for i, message in enumerate(self.messages) if message.get("role") == "user"]
        keep = max(1, MAX_HISTORY)
        if len(starts) > keep:
            del self.messages[:starts[-keep]]


sessions: dict[str, Session] = defaultdict(Session)


def preview_session(key: str) -> Session:
    """取当前有效历史的独立副本，预演追加、裁剪和分批，不创建或清理真实会话。"""
    source = sessions.get(key)
    if source is None or source.is_expired():
        return Session(_changed=lambda: None)
    return replace(source, messages=list(source.messages), group_events=dict(source.group_events),
                   lock=asyncio.Lock(), group_gate=None, scope="", _changed=lambda: None)


def get_session(key: str) -> Session:
    s = sessions[key]
    s.scope = key
    if s.is_expired():
        s.clear(experience_before=time.time() - HISTORY_TTL)
    return s


# ------------------------------------------------------------
#  持久化
# ------------------------------------------------------------

# 旧版（单平台）会话键形如 `group_123` / `private_456`，升级多平台后应带平台前缀
# `onebot_group_123`。这里把旧键补上 `onebot_` 前缀做一次性迁移。
_LEGACY_PREFIXES = ("group_", "private_")


def _migrate_legacy_keys(data: dict) -> dict:
    """把旧版无平台前缀的会话键补上 `onebot_` 前缀。已是新格式则原样返回。"""
    changed = False
    new_data: dict = {}
    for key, val in data.items():
        # 形如 `group_123` / `private_456` 且不以已知平台名开头 → 旧键
        if any(key.startswith(p) for p in _LEGACY_PREFIXES):
            new_key = f"onebot_{key}"
            changed = True
        else:
            new_key = key
        # 冲突时新键优先（理论上不会发生）
        new_data[new_key] = val
    return new_data if changed else data


def load() -> None:
    """从磁盘恢复会话历史（lock 不持久化，由 default_factory 新建）。

    顺带把旧版无平台前缀的会话键迁移到 `onebot_` 前缀，并备份原文件。
    """
    if not SESSION_PERSIST:
        return
    data = storage.load_json(_SESSION_FILE, {})
    if not data:
        return

    migrated = _migrate_legacy_keys(data)
    if migrated is not data:  # 发生过迁移
        src = os.path.join(DATA_DIR, _SESSION_FILE)
        try:
            shutil.copy2(src, src + ".legacy.bak")
            log.info(f"会话键迁移：已备份旧文件为 {_SESSION_FILE}.legacy.bak")
        except Exception as e:
            log.warning(f"备份旧 sessions.json 失败({e!r})，仍继续迁移")
        storage.save_json(_SESSION_FILE, migrated)
        log.info(f"会话键迁移：{len(data)} 条旧键已补 `onebot_` 前缀")
        data = migrated

    for key, s in data.items():
        sessions[key] = Session(
            messages=s.get("messages", []),
            last_active=s.get("last_active", time.time()),
            group_mode=s.get("group_mode", "_group_" in key),
            group_seq=s.get("group_seq", 0),
            # 重启仅恢复背景，不主动补发关机前未处理的消息。
            group_cursor=s.get("group_seq", 0),
        )
        sessions[key]._trim()
    log.info(f"已恢复 {len(data)} 个会话")


def snapshot() -> dict:
    """当前非空会话的持久化表示；调用方须在主事件循环内立即序列化。"""
    return {
        key: {"messages": s.messages, "last_active": s.last_active,
              "group_mode": s.group_mode, "group_seq": s.group_seq, "group_cursor": s.group_cursor}
        for key, s in sessions.items()
        if s.messages
    }


def save() -> None:
    """把非空会话落盘。"""
    global _dirty
    if not SESSION_PERSIST:
        return
    storage.save_json(_SESSION_FILE, snapshot())
    _dirty = False


def flush_if_dirty() -> None:
    if _dirty:
        save()


load()

