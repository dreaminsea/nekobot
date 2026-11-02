"""
Bot 入口 —— 多平台可拓展版喵~

通过 adapter 层连接 IM 平台（OneBot/QQ、飞书…），归一成平台无关的 MessageEvent
后分发给插件处理。`config.PLATFORMS` 里列出的平台会同时并发运行，各 adapter 互相
隔离（一个断连/报错不影响其它）；事件按 `event.platform` 派回对应平台的 bot。
新增平台只需实现一个 adapter 并在 adapters/ 注册。

依赖: uv sync --locked；启动: uv run --locked bot.py

NapCat 配置（PLATFORMS 含 "onebot" 时）:
  在 WebUI → 网络配置 → 新建 → WebSocket 服务端
  端口 3001, 启用, token 和 .env 的 NAPCAT_TOKEN 保持一致
"""

import asyncio
from pathlib import Path
import signal

from core.maintenance import acquire_lock

# 必须在导入 config/session 等读取磁盘的模块前取锁；/restart 的 exec 会关闭旧锁。
if __name__ == "__main__":
    try:
        _instance_lock = acquire_lock(Path(__file__).resolve().parent)
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc

from config import LLM_API_KEY, NAPCAT_WATCH, PLATFORMS, SESSION_SAVE_INTERVAL
from core import log as logmod
from core import agent, coalesce, runtime, session, sticker_learning, timeline
from core.adapter import Adapter, create_adapter
from core.event import MessageEvent
from core.log import log
from core.plugin import dispatch
from core.restart_notice import notify_after_restart

logmod.setup()

# 导入工具/插件包触发自动发现与注册
import tools  # noqa: E402,F401
import plugins  # noqa: E402,F401
# 导入适配器包触发各平台 adapter 的注册
import adapters  # noqa: E402,F401


async def on_event(event: MessageEvent) -> None:
    """adapter 归一后的消息事件 → 插件分发（用事件来源平台的 bot 发送）。

    adapter 已把消息和支持的互动通知统一成 MessageEvent。
    """
    adapter = _adapters.get(event.platform)
    if adapter is None:
        log.warning(f"收到未注册平台的事件: {event.platform}")
        return
    try:
        loop = agent.current()
        if loop is not None and not loop.accepts_messages:
            return  # 停机期间的迟到回调不能在旧路径重新启动聊天任务。
        if loop is not None and loop.enabled:
            await loop.submit_message(event, adapter.bot)
        else:
            await dispatch(event, adapter.bot)
    except Exception as e:
        log.error(f"处理消息异常: {e!r}")


async def _persist_loop() -> None:
    """后台定时把聊天历史落盘。"""
    while True:
        await asyncio.sleep(SESSION_SAVE_INTERVAL)
        try:
            session.flush_if_dirty()
        except Exception as e:
            log.warning(f"会话落盘失败: {e!r}")
        timeline.flush_if_dirty()


_adapters: dict[str, Adapter] = {p: create_adapter(p) for p in PLATFORMS}
runtime.set_adapters(_adapters)  # 登记，供跨平台取 bot（如飞书命令探测 QQ 链路）


async def _run_adapter(platform: str, adapter: Adapter) -> None:
    """跑单个 adapter，吞掉异常做隔离——一个平台挂了不拖垮其它平台。"""
    try:
        log.info(f"启动平台适配器: {platform}")
        await adapter.run(on_event)
    except Exception as e:
        log.error(f"[{platform}] 适配器退出: {e!r}")


async def run() -> None:
    if not LLM_API_KEY:
        log.warning("未配置 LLM_API_KEY：请复制 .env.example 为 .env 并填写，模型请求会失败")
    loop = agent.start()
    tasks = []
    background = []
    try:
        sticker_learning.start()
        background.append(asyncio.create_task(_persist_loop()))
        if NAPCAT_WATCH and "feishu" in _adapters and "onebot" in _adapters:
            from core.napcat_watch import watch
            background.append(asyncio.create_task(watch(lambda: runtime.get_adapter_bot("feishu"))))
        background.append(asyncio.create_task(notify_after_restart()))
        tasks = [asyncio.create_task(_run_adapter(p, a)) for p, a in _adapters.items()]
        await asyncio.gather(*tasks)
    finally:
        for task in [*tasks, *background]:
            task.cancel()
        await asyncio.gather(*tasks, *background, return_exceptions=True)
        await loop.close()
        await coalesce.close()
        await sticker_learning.close()


def _stop_on_signal(signum, frame) -> None:
    # 服务管理器的 SIGTERM 也走 finally，让最近的会话落盘后再做离线备份。
    raise KeyboardInterrupt


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, _stop_on_signal)
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        log.info("Bot 已停止 ~")
    finally:
        try:
            session.save()  # 退出前再落盘一次
        finally:
            timeline.flush_if_dirty()
