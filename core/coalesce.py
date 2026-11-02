"""每个会话一个消费者：短静默合批、最长等待兜底，生成期间只累积下一批。

直接互动（forced）按 REPLY_DEBOUNCE / REPLY_MAX_WAIT，从第一条直接互动起计时；
之后只有被回应的人继续补充才延长静默窗口，其他人刷屏不会推迟回答。
未直接触发的批次（潜水、跟进）不赶时间，按 GROUP_IDLE_* 攒成更大的批次再判断，
避免同一话题被切成很多小批、每批都触发一次接话。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from config import GROUP_IDLE_DEBOUNCE, GROUP_IDLE_MAX_WAIT, REPLY_DEBOUNCE, REPLY_MAX_WAIT
from core.log import log

Responder = Callable[[], Awaitable[None]]


@dataclass
class _Pending:
    task: asyncio.Task | None = None
    respond: Responder | None = None
    forced: bool = False
    first_at: float = 0
    last_at: float = 0
    awaited: set[str] = field(default_factory=set)  # 本批直接互动的发送者，他们的补充会延长等待
    changed: asyncio.Event = field(default_factory=asyncio.Event)


_pending: dict[str, _Pending] = {}


def active(session_key: str) -> bool:
    return session_key in _pending


def queued(session_key: str) -> bool:
    p = _pending.get(session_key)
    return p is not None and p.respond is not None


def feed(session_key: str, respond: Responder, *, forced: bool, sender: str | None = None) -> None:
    p = _pending.setdefault(session_key, _Pending())
    now = asyncio.get_running_loop().time()
    if p.respond is None or (forced and not p.forced):
        # 新批次；或首次出现直接互动，改用直接互动的时限并从这条消息重新计时。
        p.first_at = p.last_at = now
    elif forced or not p.forced or (sender is not None and sender in p.awaited):
        p.last_at = now
    if forced and sender is not None:
        p.awaited.add(sender)
    if forced or not p.forced:
        p.respond = respond
    p.forced = p.forced or forced
    p.changed.set()
    if p.task is None:
        p.task = asyncio.create_task(_run(session_key, p))


def _deadline(p: _Pending) -> float:
    debounce, max_wait = (REPLY_DEBOUNCE, REPLY_MAX_WAIT) if p.forced else (GROUP_IDLE_DEBOUNCE, GROUP_IDLE_MAX_WAIT)
    return min(p.last_at + debounce, p.first_at + max_wait)


async def _run(session_key: str, p: _Pending) -> None:
    try:
        while p.respond is not None:
            while True:
                delay = _deadline(p) - asyncio.get_running_loop().time()
                if delay <= 0:
                    break
                p.changed.clear()
                try:
                    await asyncio.wait_for(p.changed.wait(), delay)
                except asyncio.TimeoutError:
                    break
            respond = p.respond
            p.respond = None
            p.forced = False
            p.awaited = set()
            try:
                await respond()
            except Exception as exc:
                log.error(f"[合并回复] 执行异常: {exc!r}")
            # await 期间的新消息只更新 p.respond；不会创建排队的第二个消费者。
    finally:
        if _pending.get(session_key) is p:
            del _pending[session_key]


async def close() -> None:
    """进程退出时停止消费者；不重放尚未发送的生成或待处理批次。"""
    tasks = [p.task for p in _pending.values() if p.task is not None]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    _pending.clear()
