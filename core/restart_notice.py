"""仅 /restart 引发的新进程，在 QQ 可用后通知主人；普通启动不发送。"""

import asyncio
import os

import config
from config import MASTER_QQ, PLATFORMS
from core import runtime
from core.log import log

# 通过 execve 的环境传给新进程，不写入磁盘，避免之后的普通启动误发旧通知。
RESTART_NOTICE_ENV = "NEKOBOT_RESTART_NOTICE"
# /update 热重载插件时，旧进程中的 config 还没有这个新选项；仍须让新版 /restart 能加载。
RESTART_NOTICE_RETRY_SECONDS = getattr(config, "RESTART_NOTICE_RETRY_SECONDS", 5.0)


async def notify_after_restart() -> None:
    if os.environ.pop(RESTART_NOTICE_ENV, None) != "1":
        return
    if "onebot" not in PLATFORMS or not MASTER_QQ:
        log.warning("[重启通知] 未启用 QQ 或未配置 MASTER_QQ，无法通知主人")
        return

    while True:
        bot = runtime.get_adapter_bot("onebot")
        if bot is not None:
            try:
                # 私聊的普通发送接口只发不等回执；这里确认 OneBot 接受后才结束通知任务。
                result = await bot.call_action_resp(
                    "send_private_msg", user_id=int(MASTER_QQ),
                    message=[{"type": "text", "data": {"text": "重启完成，QQ 已重新连接，可以继续找我啦喵～"}}],
                )
                if result and result.get("status") == "ok" and result.get("retcode", 0) == 0:
                    log.info("[重启通知] 已通过 QQ 通知主人")
                    return
                log.warning("[重启通知] QQ 尚未确认发送成功，稍后重试")
            except Exception as exc:
                log.warning(f"[重启通知] QQ 发送失败，稍后重试: {exc!r}")
        await asyncio.sleep(RESTART_NOTICE_RETRY_SECONDS)
