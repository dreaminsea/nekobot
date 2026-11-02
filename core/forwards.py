"""合并转发的会话内引用：只登记入口，内容由工具按需读取，不递归展开。"""

from __future__ import annotations

import hashlib
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

from config import FORWARD_CONTEXT_MAX, HISTORY_TTL


@dataclass
class ForwardInfo:
    id: str = ""                       # 平台资源 ID；内联记录可以没有
    title: str = ""
    messages: list[Any] | None = None   # None 表示尚未读取；[] 表示空记录
    key: str = field(default_factory=lambda: uuid.uuid4().hex)


@dataclass
class _Entry:
    session_key: str
    info: ForwardInfo
    touched: float


_registry: OrderedDict[str, _Entry] = OrderedDict()


def _prune() -> None:
    cutoff = time.monotonic() - HISTORY_TTL
    for sid, entry in list(_registry.items()):
        if entry.touched < cutoff:
            del _registry[sid]
    while len(_registry) > max(1, FORWARD_CONTEXT_MAX):
        _registry.popitem(last=False)


def _reference_id(info: ForwardInfo, session_key: str) -> str:
    key = "remote:" + info.id if info.id else "inline:" + info.key
    return hashlib.sha256(f"{session_key}\0{key}".encode()).hexdigest()[:16]


def register(info: ForwardInfo, session_key: str) -> str:
    """引用绑定会话；相同资源重复出现时复用已读取内容。"""
    _prune()
    sid = _reference_id(info, session_key)
    previous = _registry.get(sid)
    if previous is not None and info.messages is None:
        info.messages = previous.info.messages
    _registry[sid] = _Entry(session_key, info, time.monotonic())
    _registry.move_to_end(sid)
    _prune()
    return sid


def get(sid: str, session_key: str) -> ForwardInfo | None:
    _prune()
    entry = _registry.get(sid)
    if entry is None or entry.session_key != session_key:
        return None
    entry.touched = time.monotonic()
    _registry.move_to_end(sid)
    return entry.info


def placeholder(info: ForwardInfo, session_key: str, *, register_ref: bool = True) -> str:
    sid = register(info, session_key) if register_ref else _reference_id(info, session_key)
    title = " ".join(str(info.title).split())[:120]
    label = f"[合并转发#{sid}]"
    if title:
        label += f"（{title}）"
    return label + "（内容未展开，可按需调用 read_forward）"
