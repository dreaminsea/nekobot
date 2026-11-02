"""示例中间件 —— 演示如何写黑名单 / 限流 / 日志钩子。

默认全部关闭（ENABLED = False），改成 True 即生效。这是「怎么扩展」的范本。
"""

import time
from collections import defaultdict

from core.bot import Bot
from core.event import MessageEvent
from core.log import log
from core.middleware import middleware

ENABLED = False

# ---- 黑名单 ----
BLACKLIST: set[str] = set()

# ---- 简单限流：每人每 RATE_WINDOW 秒最多 RATE_MAX 条 ----
RATE_WINDOW = 10.0
RATE_MAX = 5
_hits: dict[str, list[float]] = defaultdict(list)


if ENABLED:

    @middleware(priority=10)
    async def block_blacklist(event: MessageEvent, bot: Bot) -> bool:
        if event.user_id in BLACKLIST:
            log.info(f"[拦截] 黑名单用户 {event.user_id}")
            return False
        return True

    @middleware(priority=20)
    async def rate_limit(event: MessageEvent, bot: Bot) -> bool:
        now = time.time()
        recent = [t for t in _hits[event.user_id] if now - t < RATE_WINDOW]
        if len(recent) >= RATE_MAX:
            log.info(f"[限流] 用户 {event.user_id} 触发限流")
            _hits[event.user_id] = recent
            return False
        recent.append(now)
        _hits[event.user_id] = recent
        return True

    @middleware(priority=30)
    async def access_log(event: MessageEvent, bot: Bot) -> bool:
        log.debug(f"[middleware] {event.message_type} from {event.user_id}: {event.text[:40]}")
        return True
