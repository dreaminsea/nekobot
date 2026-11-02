"""插件 / 命令系统 —— 框架核心扩展点。

用装饰器注册消息处理器，dispatch 按优先级分发。handler 统一签名::

    async def handler(event: MessageEvent, bot: Bot, match: MatchResult) -> None

示例::

    @on_command("clear", "重置", priority=10)
    async def _(event, bot, match):
        ...

    @on_message(priority=100, to_me=True)
    async def _(event, bot, match):
        text = match.text   # 已剥离命令/前缀的正文
        ...
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

from core import runtime
from core.bot import Bot
from core.event import MessageEvent
from core.log import log
from core.middleware import run_middlewares


@dataclass
class MatchResult:
    """命中结果，传给 handler。"""

    text: str = ""                       # 处理后的正文（命令实参 / 前缀剥离后）
    groups: tuple = ()                   # 正则分组
    raw: str = ""                        # 命中前的 command_text


Rule = Callable[[MessageEvent], Optional[MatchResult]]
Handler = Callable[[MessageEvent, Bot, MatchResult], Awaitable[None]]


@dataclass
class Matcher:
    handler: Handler
    rule: Rule
    priority: int = 100
    scope: str = "all"            # all | private | group
    to_me: bool = True            # 群聊是否要求 @bot / 触发前缀
    admin_only: bool = False
    block: bool = True            # 命中并处理后是否停止分发

    name: str = ""
    is_command: bool = False

    def check(self, event: MessageEvent) -> MatchResult | None:
        if self.scope != "all" and event.message_type != self.scope:
            return None
        if self.to_me and not event.to_me:
            return None
        if self.admin_only and not event.is_admin:
            return None
        return self.rule(event)


# 全局注册表，按 priority 升序
_matchers: list[Matcher] = []


def _register(rule: Rule, **kw):
    def deco(func: Handler) -> Handler:
        m = Matcher(handler=func, rule=rule, name=func.__name__, **kw)
        _matchers.append(m)
        _matchers.sort(key=lambda x: x.priority)
        return func

    return deco


# ------------------------------------------------------------
#  规则工厂
# ------------------------------------------------------------

def _command_rule(names: tuple[str, ...]) -> Rule:
    # 支持 /name 与 name 两种写法
    def rule(event: MessageEvent) -> MatchResult | None:
        text = event.command_text
        for name in names:
            for prefix in (f"/{name}", name):
                if text == prefix or text.startswith(prefix + " "):
                    return MatchResult(text=text[len(prefix):].strip(), raw=text)
        return None

    return rule


def _prefix_rule(prefixes: tuple[str, ...]) -> Rule:
    def rule(event: MessageEvent) -> MatchResult | None:
        text = event.command_text
        for p in prefixes:
            if text.startswith(p):
                return MatchResult(text=text[len(p):].strip(), raw=text)
        return None

    return rule


def _keyword_rule(words: tuple[str, ...]) -> Rule:
    def rule(event: MessageEvent) -> MatchResult | None:
        text = event.command_text
        if any(w in text for w in words):
            return MatchResult(text=text, raw=text)
        return None

    return rule


def _regex_rule(pattern: str) -> Rule:
    compiled = re.compile(pattern)
    def rule(event: MessageEvent) -> MatchResult | None:
        m = compiled.search(event.command_text)
        if m:
            return MatchResult(text=event.command_text, groups=m.groups(), raw=event.command_text)
        return None

    return rule


def _message_rule() -> Rule:
    def rule(event: MessageEvent) -> MatchResult | None:
        # 图片和合并转发可独立成消息；只检查附件存在，不在匹配阶段展开内容。
        group_mention = event.message_type == "group" and event.is_at_bot
        if not (event.text or event.has_image or event.forwards or group_mention):
            return None
        return MatchResult(text=event.command_text, raw=event.command_text)

    return rule


# ------------------------------------------------------------
#  注册装饰器
# ------------------------------------------------------------

def on_command(*names: str, **kw):
    return _register(_command_rule(names), is_command=True, **kw)


def matches_blocking_command(event: MessageEvent) -> bool:
    """只检查命令路由；上下文预览不能执行被预演的命令。"""
    return any(m.is_command and m.block and m.check(event) is not None for m in _matchers)


def on_prefix(*prefixes: str, **kw):
    return _register(_prefix_rule(prefixes), **kw)


def on_keyword(*words: str, **kw):
    return _register(_keyword_rule(words), **kw)


def on_regex(pattern: str, **kw):
    return _register(_regex_rule(pattern), **kw)


def on_message(**kw):
    return _register(_message_rule(), **kw)


# ------------------------------------------------------------
#  分发
# ------------------------------------------------------------

async def dispatch(event: MessageEvent, bot: Bot) -> None:
    """先拦截 /ignore，再跑中间件与匹配器；忽略内容连表情学习也不能进入。"""
    command = event.command_text.lstrip().split(maxsplit=1)
    if command and command[0] == "/ignore":
        return  # 群聊不要求 @，私聊也生效；不记录、不回复、不改变待处理批次。
    runtime.set_context(event, bot)  # 让工具等深层代码能拿到当前 event/bot
    if not await run_middlewares(event, bot):
        return
    if event.forwards:
        log.info(
            f"[合并转发] 已接收 {event.session_key} 消息 {event.message_id or '-'}，"
            f"入口数={len(event.forwards)}，指向bot={event.to_me}（内容未展开）"
        )

    for m in _matchers:
        match = m.check(event)
        if match is None:
            continue
        try:
            await m.handler(event, bot, match)
        except Exception as e:
            log.error(f"插件 {m.name} 处理异常: {e!r}")
        if m.block:
            break
