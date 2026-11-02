"""一个事件收件箱、一项分步后台工作和若干后台活动；聊天仍交给原有 dispatch/coalesce。

活动（core/activity.py）由主人用 /agent start|stop 控制，或按 `AGENT_ACTIVITIES` 自动启动。
聊天通过 request_action 提交请求：这里检查权限并交给活动当场决定；接受的请求完成后，
结果记入经历流，并作为后台通知送回发起的会话。活动调用模型前要经过每小时预算。
"""

from __future__ import annotations

import asyncio
from collections import deque
from contextvars import Context
import math
import time
from typing import Awaitable, Callable
import uuid

import config
from core import timeline
from core.activity import Activity, ActivityContext, Request
from core.log import log

ACTIVE = {"running", "pausing", "paused", "stopping"}
LABELS = {
    "running": "进行中", "pausing": "等待当前步骤结束后暂停", "paused": "已暂停后续步骤",
    "stopping": "正在停止，等待当前步骤退出", "stopped": "已停止后续步骤",
    "completed": "已完成", "failed": "失败", "interrupted": "进程中断，未自动恢复",
}
Step = Callable[[int], Awaitable[str | None]]
_current: AgentLoop | None = None


class AgentLoop:
    def __init__(self, dispatch=None, *, enabled=True, max_steps=100, timeout=300.0, llm_per_hour=60):
        if dispatch is None:
            from core.plugin import dispatch
        self._dispatch = dispatch
        self.enabled = enabled
        self.max_steps = max(1, int(max_steps))
        self.timeout = float(timeout)
        if not math.isfinite(self.timeout) or self.timeout <= 0:
            raise ValueError("后台工作时限必须是正数")
        self._queue = asyncio.Queue()
        self._runner = None
        self._handlers: set[asyncio.Task] = set()
        self._work_task = None
        self._resume = asyncio.Event()
        self._step_active = False
        self._stop_requested = False
        self._closing = False
        self._closed = False
        self._accepting = False
        self._activities: dict[str, Activity] = {}
        self._running: set[str] = set()
        self._autostart: list[str] = []
        self._requests: dict[int, Request] = {}
        self._next_request = 1
        self.llm_per_hour = max(0, int(llm_per_hour))
        self._llm_calls: deque[float] = deque()
        self._budget_warned = -math.inf
        self._work = timeline.work_snapshot()
        # 启动时只恢复事实摘要，不恢复协程、等待条件或旧操作。
        if self._work and self._work["status"] in ACTIVE:
            self._update_work(status="interrupted", result="上个进程未完成；需明确重新启动，不自动重试。")
            timeline.flush_if_dirty()

    def start(self) -> None:
        if self._closed:
            raise RuntimeError("已关闭的循环不能重新使用")
        if self.enabled and self._runner is None:
            self._accepting = True
            timeline.set_status_provider(self.status_lines)
            self._runner = asyncio.create_task(self._run(), name="agent-loop", context=Context())

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def accepts_messages(self) -> bool:
        return not (self._closing or self._closed) and (self._accepting or not self.enabled)

    async def _request(self, kind, payload):
        if not self._accepting:
            return "外层循环未运行或正在关闭，未接受请求。"
        result = asyncio.get_running_loop().create_future()
        self._queue.put_nowait((kind, payload, result))
        return await asyncio.shield(result)

    async def submit_message(self, event, bot) -> None:
        # 保持回调完成语义：等待该消息 dispatch 结束，不等聊天模型生成结束。
        await self._request("message", (event, bot))

    async def control(self, action: str, event) -> str:
        return await self._request("control", (action, event))

    # ---------------- 后台活动 ----------------

    def register(self, activity: Activity, *, autostart: bool = False) -> None:
        activity.ctx = ActivityContext(self, activity)
        self._activities[activity.name] = activity
        if autostart and activity.name not in self._autostart:
            self._autostart.append(activity.name)

    async def control_activity(self, action: str, name: str, event) -> str:
        return await self._request("activity", (action, name, event))

    async def request(self, event, activity: str, action: str, args: dict) -> str:
        """聊天提交的请求；当场返回接受/完成/拒绝，不等待事情做完。"""
        return await self._request("request", (event, activity, action, args))

    def finish(self, activity: str, request_id: int, ok: bool, text: str, *, notify: bool = True) -> None:
        if self._closed or not self._runner or self._runner.done():
            return
        self._queue.put_nowait(("finish", (activity, request_id, ok, str(text), notify), None))

    def allow_llm(self, activity: str) -> bool:
        """按小时滑动窗口限制后台活动的模型调用；用完时每小时提醒主人一次。"""
        now = time.monotonic()
        while self._llm_calls and now - self._llm_calls[0] >= 3600:
            self._llm_calls.popleft()
        if len(self._llm_calls) < self.llm_per_hour:
            self._llm_calls.append(now)
            return True
        if now - self._budget_warned >= 3600:
            self._budget_warned = now
            text = f"后台活动本小时的模型调用（{self.llm_per_hour} 次）已用完，暂停思考，聊天不受影响。"
            log.warning(f"[Agent] {text}（触发：{activity}）")
            timeline.note("agent", text, label="后台·主循环", importance=2)
            from core.notices import notify_master
            self._spawn(notify_master(text))
        return False

    def describe_actions(self) -> str:
        lines = []
        for name in sorted(self._running):
            activity = self._activities[name]
            for action in activity.actions:
                params = ", ".join(action.parameters) or "无参数"
                who = "所有人可请求" if action.everyone else "仅主人可请求"
                lines.append(f"- {name}.{action.name}({params})：{action.description}（{activity.title}，{who}）")
        return "\n".join(lines)

    def status_lines(self, key: str) -> list[str]:
        """注入各会话的自我状态：正在进行的活动和尚未完成的请求。"""
        if self._closed:
            return []
        lines = []
        for name in sorted(self._running):
            activity = self._activities[name]
            state = activity.status()
            lines.append(f"后台活动「{activity.title}」运行中" + (f"：{state}" if state else ""))
        for request in sorted(self._requests.values(), key=lambda r: r.id)[:5]:
            here = "，在本会话发起" if request.origin.get("session_key") == key else ""
            lines.append(f"进行中的请求 #{request.id} {request.activity}.{request.action}（来自 {request.requester}{here}）")
        return lines

    def _spawn(self, coro) -> None:
        task = asyncio.get_running_loop().create_task(coro, context=Context())
        self._handlers.add(task)
        task.add_done_callback(self._handlers.discard)

    async def _start_activity(self, name: str) -> str:
        activity = self._activities.get(name)
        if activity is None:
            return f"没有名为 {name} 的后台活动。可用：{'、'.join(sorted(self._activities)) or '无'}"
        if name in self._running:
            return f"「{activity.title}」已在运行。"
        try:
            await asyncio.wait_for(activity.start(), 10)
        except Exception as exc:
            log.error(f"[Agent] 活动 {name} 启动失败: {type(exc).__name__}: {exc}")
            return f"「{activity.title}」启动失败：{type(exc).__name__}"
        self._running.add(name)
        log.info(f"[Agent] 活动 {name} 已启动")
        return f"「{activity.title}」已启动。"

    async def _stop_activity(self, name: str) -> str:
        activity = self._activities.get(name)
        if activity is None or name not in self._running:
            return f"{name} 没有在运行。"
        self._running.discard(name)  # 先停止接收新请求
        try:
            await asyncio.wait_for(activity.stop(), 10)
        except Exception as exc:
            log.error(f"[Agent] 活动 {name} 停止异常: {type(exc).__name__}")
        for request in [r for r in self._requests.values() if r.activity == name]:
            self._finish(name, request.id, False, "后台活动已停止，这个请求取消了", True)
        log.info(f"[Agent] 活动 {name} 已停止")
        return f"「{activity.title}」已停止。"

    async def _activity_control(self, action, name, event) -> str:
        if not self._authorized(event):
            return "后台活动只能由主人在私聊中启停。"
        if action == "start":
            return await self._start_activity(name)
        if action == "stop":
            return await self._stop_activity(name)
        return "未知控制命令。"

    def _arbitrate(self, event, name, action_name, args) -> str:
        if event is None:
            return "没有来源会话，未提交请求。"
        activity = self._activities.get(name)
        if activity is None or name not in self._running:
            return f"后台活动 {name} 没有在运行，未提交请求。"
        action = next((a for a in activity.actions if a.name == action_name), None)
        if action is None:
            names = "、".join(a.name for a in activity.actions)
            return f"「{activity.title}」没有 {action_name} 这个动作，可用：{names}。"
        if not isinstance(args, dict):
            return "参数需要是对象，未提交请求。"
        from core.notices import origin_of
        from core.perms import is_master
        if not action.everyone and not is_master(event):
            return f"「{activity.title}」的 {action_name} 只有主人能请求，未提交。"
        request = Request(self._next_request, name, action_name, dict(args), origin_of(event))
        try:
            verdict = activity.accept(request)
        except Exception as exc:
            log.error(f"[Agent] 活动 {name} 处理请求异常: {type(exc).__name__}: {exc}")
            return "活动处理请求时出错，未提交。"
        if verdict.status == "rejected":
            return f"未接受：{verdict.message}"
        self._next_request += 1
        origin = request.origin["session_key"]
        label = f"后台·{activity.title}"
        if verdict.status == "done":
            timeline.note(f"agent_{name}", f"{request.requester} 的请求已完成：{verdict.message}",
                          label=label, also_visible_to=[origin])
            return f"已完成：{verdict.message}"
        self._requests[request.id] = request
        timeline.note(f"agent_{name}", f"接受了 {request.requester} 的请求 #{request.id}：{verdict.message}",
                      label=label, also_visible_to=[origin])
        log.info(f"[Agent] 接受请求 #{request.id} {name}.{action_name} 来自 {origin}")
        return (f"已接受请求 #{request.id}：{verdict.message}。现在还没做完，不要说已经完成；"
                "完成后系统会通知这个会话，届时再告诉对方。")

    def _finish(self, name, request_id, ok, text, notify) -> None:
        request = self._requests.get(request_id)
        if request is None or request.activity != name:
            return  # 重复或过期的完成通知
        del self._requests[request_id]
        activity = self._activities[name]
        outcome = "已完成" if ok else "没能完成"
        text = text[:480]
        timeline.note(f"agent_{name}", f"请求 #{request_id}（{request.requester}）{outcome}：{text}",
                      label=f"后台·{activity.title}", importance=2,
                      also_visible_to=[request.origin["session_key"]])
        log.info(f"[Agent] 请求 #{request_id} {outcome}")
        if not notify or self._closing:
            return
        quoted = request.origin.get("message_id")
        notice = (f"请求 #{request_id}（{activity.title}.{request.action}，来自 {request.requester}"
                  + (f"，原消息ID {quoted}" if quoted else "") + f"）{outcome}：{text}")
        self._spawn(self._notify(request.origin, notice))

    async def _notify(self, origin, text) -> None:
        from core.notices import deliver
        try:
            await deliver(origin, text)
        except Exception as exc:
            log.error(f"[Agent] 后台通知送达异常: {type(exc).__name__}")

    async def start_work(self, event, step: Step, *, steps: int, name="后台工作", goal="") -> str:
        return await self._request("start", (event, step, steps, name, goal))

    async def demo(self, event, *, steps=5, interval=1.0) -> str:
        if not math.isfinite(interval) or not 0.05 <= interval <= 60:
            return "演示间隔需为 0.05 到 60 秒。"

        async def step(index):
            await asyncio.sleep(interval)
            return f"第 {index + 1} 次等待完成。"

        return await self.start_work(event, step, steps=steps, name="离线分步演示",
                                     goal=f"完成 {steps} 次等待，不调用模型或外部服务。")

    def status(self, event=None) -> str:
        text = self._work_status(event)
        names = sorted(self._activities)
        if names:
            text += "\n后台活动：" + "；".join(
                f"{self._activities[n].title}({n}) " + (
                    "运行中" + (f"，{self._activities[n].status()}" if self._activities[n].status() else "")
                    if n in self._running else "未运行")
                for n in names
            )
        if self._requests:
            text += "\n进行中的请求：" + "；".join(
                f"#{r.id} {r.activity}.{r.action}（{r.requester}）" for r in sorted(self._requests.values(), key=lambda r: r.id)
            )
        return text

    def _work_status(self, event=None) -> str:
        mode = "外层循环运行中" if self._accepting else "外层循环已关闭，使用原聊天入口" if not self.enabled else "外层循环未运行"
        if not self._work:
            return mode + "；当前没有后台工作。"
        from core.perms import is_master
        private = event is not None and event.message_type == "private" and is_master(event)
        local = event is not None and event.session_key == self._work["source"]["session_key"]
        work = self._work
        text = f"{mode}。工作 {work['id'][:8]}：{LABELS[work['status']]}，进度 {work['step']}/{work['total']}。"
        if private or local:
            text += f"\n{work['name']}：{work['goal']}\n最近结果：{work['result'] or '尚无'}"
        return text

    @staticmethod
    def _authorized(event) -> bool:
        from core.perms import is_master
        return event is not None and event.message_type == "private" and is_master(event)

    def _update_work(self, **changes) -> None:
        updated = {**self._work, **changes, "updated_at": time.time()}
        timeline.set_work(updated)
        self._work = updated

    def _start_work(self, event, step, steps, name, goal) -> str:
        if not self._authorized(event):
            return "后台工作只能由主人在私聊中控制。"
        if self._closing:
            return "循环正在关闭，未启动工作。"
        if (self._work and self._work["status"] in ACTIVE) or (self._work_task and not self._work_task.done()):
            return "已有一项工作未结束，未启动第二项。"
        if type(steps) is not int or not 1 <= steps <= self.max_steps or not callable(step):
            return f"工作步数需为 1 到 {self.max_steps} 的整数。"
        if not timeline.work_writable():
            return "状态文件不可用，未启动工作；请先检查 agent_state.json。"
        work = {
            "id": uuid.uuid4().hex, "name": str(name)[:120], "goal": str(goal)[:480],
            "status": "running", "step": 0, "total": steps, "timeout": self.timeout,
            "result": "", "created_at": time.time(), "updated_at": time.time(),
            "source": {key: str(getattr(event, key, "") or "") for key in (
                "platform", "message_type", "user_id", "group_id", "message_id", "session_key"
            )},
        }
        timeline.set_work(work)
        self._work = work
        timeline.flush_if_dirty()
        self._resume.set()
        self._stop_requested = False
        self._work_task = asyncio.create_task(
            self._run_work(self._work["id"], step, steps), name="agent-work", context=Context(),
        )
        self._work_task.add_done_callback(
            lambda task, work_id=self._work["id"]: self._work_finished(task, work_id)
        )
        log.info(f"[Agent] 启动工作 {self._work['id'][:8]} 来源={event.session_key} 步数={steps}")
        return f"已开始工作 {self._work['id'][:8]}，最多 {steps} 步、{self.timeout:g} 秒（包含暂停等待）；尚未完成。"

    def _control(self, action, event) -> str:
        if action == "status":
            return self.status(event)
        if not self._authorized(event):
            return "后台工作只能由主人在私聊中控制。"
        if not self._work or self._work["status"] not in ACTIVE:
            return "当前没有可控制的工作；重启前中断的工作需明确重新启动。"
        if self._work["status"] == "stopping":
            return "正在等待当前步骤退出，不能继续或启动下一项工作。"
        if action == "pause":
            self._resume.clear()
            self._update_work(status="pausing" if self._step_active else "paused")
        elif action == "resume":
            self._resume.set()
            self._update_work(status="running")
        elif action == "stop":
            self._stop_requested = True
            self._resume.set()
            self._update_work(status="stopping")
            if self._work_task:
                self._work_task.cancel()
        else:
            return "未知控制命令。"
        timeline.flush_if_dirty()
        return self.status(event)

    def _work_finished(self, task, work_id) -> None:
        # 刚创建就被 stop 的协程尚未进入 try/finally，也必须产生真实终态。
        if task.cancelled():
            status = "stopped" if self._stop_requested and not self._closing else "interrupted"
            result = "工作已取消，未执行后续步骤。"
        elif task.exception() is not None:
            status, result = "failed", "工作协程异常退出；结果未确认。"
        else:
            return
        if not self._closed and self._runner and not self._runner.done():
            self._queue.put_nowait(("result", (work_id, status, result), None))

    async def _run_work(self, work_id: str, step: Step, steps: int) -> None:
        status, result = "completed", "所有步骤已完成。"
        deadline = asyncio.get_running_loop().time() + self.timeout
        try:
            async with asyncio.timeout(self.timeout):
                for index in range(steps):
                    await self._resume.wait()
                    if self._stop_requested:
                        status = "interrupted" if self._closing else "stopped"
                        result = "已停止后续步骤。"
                        break
                    self._step_active = True
                    try:
                        detail = await step(index)
                    finally:
                        self._step_active = False
                    detail = str(detail or "步骤完成。")[:480]
                    result = f"所有步骤已完成。最后一步：{detail}"[:480]
                    # 每一步都经收件箱确认，确保控制请求在下一步之前有处理机会。
                    await self._request("progress", (work_id, index + 1, detail))
                    if self._stop_requested:
                        status = "interrupted" if self._closing else "stopped"
                        result = "已停止后续步骤。"
                        break
                    if asyncio.get_running_loop().time() >= deadline:
                        raise TimeoutError
        except asyncio.CancelledError:
            status = "stopped" if self._stop_requested and not self._closing else "interrupted"
            result = "已停止后续步骤；取消时未返回的步骤结果未确认。"
        except TimeoutError:
            status, result = "failed", "达到工作总时限，未完成；不自动重试。"
        except Exception as exc:
            status, result = "failed", f"步骤异常：{type(exc).__name__}；不自动重试。"
        finally:
            self._step_active = False
        if not self._closed and self._runner and not self._runner.done():
            self._queue.put_nowait(("result", (work_id, status, result), None))

    @staticmethod
    def _resolve(future, value=None) -> None:
        if future is not None and not future.done():
            future.set_result(value)

    async def _deliver(self, event, bot, receipt) -> None:
        try:
            await self._dispatch(event, bot)
        except Exception as exc:
            log.error(f"[Agent] 消息处理异常: {type(exc).__name__}")
        finally:
            self._resolve(receipt)

    async def _run(self) -> None:
        try:
            for name in self._autostart:
                await self._start_activity(name)
            while True:
                kind, payload, receipt = await self._queue.get()
                try:
                    if kind == "message":
                        task = asyncio.create_task(self._deliver(*payload, receipt), context=Context())
                        self._handlers.add(task)
                        task.add_done_callback(self._handlers.discard)
                        task.add_done_callback(lambda _, receipt=receipt: self._resolve(receipt))
                    elif kind in {"start", "control"}:
                        result = self._start_work(*payload) if kind == "start" else self._control(*payload)
                        self._resolve(receipt, result)
                    elif kind == "request":
                        self._resolve(receipt, self._arbitrate(*payload))
                    elif kind == "activity":
                        self._resolve(receipt, await self._activity_control(*payload))
                    elif kind == "finish":
                        self._finish(*payload)
                        self._resolve(receipt)
                    elif kind in {"progress", "result"}:
                        work_id, value, detail = payload
                        if self._work and self._work["id"] == work_id and self._work["status"] in ACTIVE:
                            if kind == "progress":
                                status = self._work["status"]
                                if status == "pausing":
                                    status = "paused"
                                self._update_work(step=value, result=detail, status=status)
                            else:
                                self._update_work(status=value, result=detail)
                                timeline.flush_if_dirty()
                                log.info(f"[Agent] 工作 {work_id[:8]} 结束：{value}")
                        self._resolve(receipt)
                    else:
                        raise ValueError("unknown agent event")
                except Exception as exc:
                    log.error(f"[Agent] 事件处理异常: {type(exc).__name__}")
                    self._resolve(receipt, "事件处理失败，未自动重试。")
                finally:
                    self._queue.task_done()
        finally:
            self._accepting = False
            while not self._queue.empty():
                _, _, receipt = self._queue.get_nowait()
                self._resolve(receipt, "循环已关闭，未处理请求。")
                self._queue.task_done()

    async def wait_idle(self) -> None:
        """等待消息分发完成；不等待后台工作或合批中的模型调用。"""
        await self._queue.join()
        if self._handlers:
            await asyncio.gather(*tuple(self._handlers), return_exceptions=True)
        await self._queue.join()

    async def close(self) -> None:
        if self._closed:
            return
        self._closing = True
        self._accepting = False
        self._stop_requested = True
        self._resume.set()
        if self._work_task and not self._work_task.done():
            self._work_task.cancel()
            # 第三方步骤不响应取消时不能谎称已停，也不能无限阻塞退出。
            await asyncio.wait({self._work_task}, timeout=2)
        if self._runner and not self._runner.done():
            await self._queue.join()
        for name in sorted(self._running):
            await self._stop_activity(name)  # _closing 时只记经历，不再发通知
        self._requests.clear()
        timeline.set_status_provider(None)
        tasks = [*self._handlers, *([self._runner] if self._runner else [])]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.wait(tasks, timeout=2)
        if self._work and self._work["status"] in ACTIVE:
            self._update_work(status="interrupted", result="进程关闭，未完成步骤结果未确认；不自动恢复。")
        timeline.flush_if_dirty()
        self._closed = True


def current() -> AgentLoop | None:
    return _current


def start() -> AgentLoop:
    global _current
    if _current is None or _current.closed:
        _current = AgentLoop(
            enabled=bool(getattr(config, "AGENT_LOOP_ENABLED", True)),
            max_steps=getattr(config, "AGENT_WORK_MAX_STEPS", 100),
            timeout=getattr(config, "AGENT_WORK_TIMEOUT", 300.0),
            llm_per_hour=getattr(config, "AGENT_LLM_CALLS_PER_HOUR", 60),
        )
        from core.activities import BUILTIN
        autostart = set(getattr(config, "AGENT_ACTIVITIES", ["timer"]))
        for name, cls in BUILTIN.items():
            _current.register(cls(), autostart=name in autostart)
        for name in autostart - set(BUILTIN):
            log.warning(f"[Agent] 未知的后台活动 {name!r}，已忽略")
        _current.start()
    return _current
