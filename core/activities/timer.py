"""定时提醒：最简单的后台活动，不调用模型，用来跑通请求 → 完成 → 回原会话汇报。"""

from __future__ import annotations

import asyncio
from contextvars import Context
from dataclasses import dataclass
import time

from core.activity import Action, Activity, Request, Verdict

MAX_PENDING = 20
MIN_SECONDS = 10
MAX_SECONDS = 24 * 3600


def human(seconds: float) -> str:
    seconds = max(0, round(seconds))
    if seconds < 60:
        return f"{seconds} 秒"
    if seconds < 3600:
        return f"{round(seconds / 60)} 分钟"
    hours, rest = divmod(seconds, 3600)
    minutes = round(rest / 60)
    return f"{hours} 小时" + (f" {minutes} 分钟" if minutes else "")


@dataclass
class _Pending:
    task: asyncio.Task
    due: float
    request: Request


class TimerActivity(Activity):
    name = "timer"
    title = "定时提醒"
    actions = (
        Action("remind", "过 seconds 秒后提醒 text 写的事，到点后回到发起的会话提醒对方", {
            "seconds": {"type": "integer", "minimum": MIN_SECONDS, "maximum": MAX_SECONDS},
            "text": {"type": "string", "description": "要提醒的内容"},
        }, everyone=True),
        Action("cancel", "取消一个还没到点的提醒（发起人或主人）", {
            "request_id": {"type": "integer", "description": "提醒的请求编号"},
        }, everyone=True),
    )

    def __init__(self) -> None:
        super().__init__()
        self._pending: dict[int, _Pending] = {}

    def accept(self, request: Request) -> Verdict:
        if request.action == "remind":
            return self._remind(request)
        if request.action == "cancel":
            return self._cancel(request)
        return Verdict.rejected(f"不支持 {request.action}")

    def _remind(self, request: Request) -> Verdict:
        seconds, text = request.args.get("seconds"), request.args.get("text")
        if type(seconds) is not int or not MIN_SECONDS <= seconds <= MAX_SECONDS:
            return Verdict.rejected(f"seconds 需为 {MIN_SECONDS} 到 {MAX_SECONDS} 的整数")
        if not isinstance(text, str) or not text.strip() or len(text) > 200:
            return Verdict.rejected("text 需为 1 到 200 字的提醒内容")
        if len(self._pending) >= MAX_PENDING:
            return Verdict.rejected(f"已有 {MAX_PENDING} 个提醒在等，先取消一些")
        text = text.strip()
        task = asyncio.get_running_loop().create_task(self._wait(request.id, seconds, text), context=Context())
        self._pending[request.id] = _Pending(task, time.time() + seconds, request)
        return Verdict.accepted(f"{human(seconds)}后提醒：{text}")

    def _cancel(self, request: Request) -> Verdict:
        rid = request.args.get("request_id")
        pending = self._pending.get(rid) if type(rid) is int else None
        if pending is None:
            return Verdict.rejected("没有这个等待中的提醒")
        origin, owner = request.origin, pending.request.origin
        same_person = (origin.get("platform"), origin.get("user_id")) == (owner.get("platform"), owner.get("user_id"))
        from core.perms import master_id
        if not same_person and origin.get("user_id") != master_id(origin.get("platform", "")):
            return Verdict.rejected("只有发起提醒的人或主人可以取消")
        pending.task.cancel()
        del self._pending[rid]
        # 取消的人就是发起人时，这次回复已经说明，不再额外通知。
        self.ctx.finish(rid, False, f"提醒被 {request.requester} 取消了", notify=not same_person)
        return Verdict.done(f"已取消提醒 #{rid}")

    async def _wait(self, request_id: int, seconds: int, text: str) -> None:
        await asyncio.sleep(seconds)
        self._pending.pop(request_id, None)
        self.ctx.finish(request_id, True, f"提醒时间到：{text}")

    async def stop(self) -> None:
        tasks = [p.task for p in self._pending.values()]
        self._pending.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def status(self) -> str:
        if not self._pending:
            return "没有等待中的提醒"
        nearest = min(p.due for p in self._pending.values())
        return f"{len(self._pending)} 个提醒在等，最近一个约 {human(nearest - time.time())}后"
