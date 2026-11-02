"""后台活动接口：主循环负责启停、仲裁请求，并把结果送回发起请求的会话。

一个活动（Activity）是聊天之外持续进行的事，例如定时提醒，以后可以是玩游戏。
聊天里的模型不能直接操作活动，只能通过 request_action 提交请求：主循环检查权限后
交给 `accept()` 当场给出接受/完成/拒绝；接受的请求完成时，活动调用 `ctx.finish()`，
主循环记入经历流并让原会话按自己的方式把结果告诉对方。
"""

from __future__ import annotations

from dataclasses import dataclass, field
import time
from typing import TYPE_CHECKING

from core import timeline

if TYPE_CHECKING:
    from core.agent import AgentLoop


@dataclass(frozen=True)
class Action:
    """活动对聊天开放的一种请求。`parameters` 是 JSON Schema 的 properties。"""

    name: str
    description: str
    parameters: dict = field(default_factory=dict)
    everyone: bool = False  # False 时只有主人能请求


@dataclass
class Request:
    id: int
    activity: str
    action: str
    args: dict
    origin: dict  # 发起会话：platform/message_type/user_id/group_id/message_id/session_key/self_id/sender_display
    created_at: float = field(default_factory=time.time)

    @property
    def requester(self) -> str:
        return self.origin.get("sender_display") or self.origin.get("user_id") or "未知"


@dataclass(frozen=True)
class Verdict:
    status: str  # accepted=之后完成并通知；done=已当场完成；rejected=未接受
    message: str

    @classmethod
    def accepted(cls, message: str) -> "Verdict":
        return cls("accepted", message)

    @classmethod
    def done(cls, message: str) -> "Verdict":
        return cls("done", message)

    @classmethod
    def rejected(cls, message: str) -> "Verdict":
        return cls("rejected", message)


class ActivityContext:
    """主循环交给活动的能力；活动只通过它汇报，不直接碰聊天。"""

    def __init__(self, loop: "AgentLoop", activity: "Activity"):
        self._loop = loop
        self._activity = activity

    @property
    def source(self) -> str:
        return f"agent_{self._activity.name}"

    def finish(self, request_id: int, ok: bool, text: str, *, notify: bool = True) -> None:
        """请求完成或失败；同一请求只算第一次。notify=False 时只记经历不回原会话。"""
        self._loop.finish(self._activity.name, request_id, ok, text, notify=notify)

    def note(self, text: str, importance: int = 1) -> None:
        timeline.note(self.source, text, label=f"后台·{self._activity.title}", importance=importance)

    def allow_llm(self) -> bool:
        """调用模型前先问预算；返回 False 时本轮不要思考。"""
        return self._loop.allow_llm(self._activity.name)


class Activity:
    name = ""
    title = ""
    actions: tuple[Action, ...] = ()

    def __init__(self) -> None:
        self.ctx: ActivityContext | None = None

    async def start(self) -> None:
        """开始运行；耗时工作放到自己的任务里，不要阻塞主循环。"""

    async def stop(self) -> None:
        """停止并取消自己的任务；未完成的请求由主循环统一标记为取消。"""

    def status(self) -> str:
        """一句话现状，注入所有会话的自我状态。"""
        return ""

    def accept(self, request: Request) -> Verdict:
        """同步、快速地决定是否接受；接受后在自己的任务里执行。"""
        return Verdict.rejected("这个活动不接受请求")
