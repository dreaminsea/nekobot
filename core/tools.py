"""工具调用 (function calling) —— 工具注册表，第四个扩展点喵~

用 @tool 注册一个函数，LLM 即可在对话中按需调用它。函数同步/异步都行，
返回值会被转成字符串回传给模型。`parameters` 用标准 JSON Schema 描述入参，
省略则表示无参数。

示例::

    from core.tools import tool

    @tool(description="获取指定城市的天气", parameters={
        "type": "object",
        "properties": {"city": {"type": "string", "description": "城市名"}},
        "required": ["city"],
    })
    async def get_weather(city: str) -> str:
        return f"{city} 晴 25℃"

工具默认属于 "chat" 场景（群聊/私聊回复）。后台活动的工具用 `scope="xxx"` 注册，
只在该活动的模型调用里出现，不挤占聊天请求；`scope` 也可以是多个场景的元组。
`description` 可以是无参函数，生成 schema 时再取值（例如列出当前可请求的活动）。
"""

from __future__ import annotations

import asyncio
import inspect
import json
from dataclasses import dataclass, field
from typing import Callable

from core import runtime, timeline
from core.log import log

_EMPTY_PARAMS = {"type": "object", "properties": {}}


@dataclass
class Tool:
    name: str
    description: str | Callable[[], str]
    parameters: dict
    func: Callable
    scopes: frozenset[str] = field(default_factory=lambda: frozenset({"chat"}))

    @property
    def schema(self) -> dict:
        """OpenAI tools 数组里的一项。"""
        description = self.description() if callable(self.description) else self.description
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": description,
                "parameters": self.parameters,
            },
        }


# 工具注册表
_tools: dict[str, Tool] = {}


def tool(name: str | None = None, description: str | Callable[[], str] = "", parameters: dict | None = None,
         scope: str | tuple[str, ...] = "chat"):
    def deco(func: Callable) -> Callable:
        tname = name or func.__name__
        _tools[tname] = Tool(
            name=tname,
            description=description or (func.__doc__ or "").strip(),
            parameters=parameters or dict(_EMPTY_PARAMS),
            func=func,
            scopes=frozenset((scope,) if isinstance(scope, str) else scope),
        )
        log.info(f"已注册工具: {tname}")
        return func

    return deco


def get_tool_schemas(scope: str = "chat") -> list[dict]:
    return [t.schema for t in _tools.values() if scope in t.scopes]


def has_tools(scope: str = "chat") -> bool:
    return any(scope in t.scopes for t in _tools.values())


async def call_tool(name: str, arguments: dict) -> str:
    """按名调用工具，统一返回字符串（异常也转成字符串好让模型恢复）。"""
    t = _tools.get(name)
    if t is None:
        return f"[未知工具: {name}]"
    event = runtime.get_event()
    timeline.record(event, "tool", name, "attempted")
    try:
        result = t.func(**arguments)
        if inspect.isawaitable(result):
            result = await result
        output = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
        timeline.record(event, "tool", name, "returned")
        return output
    except asyncio.CancelledError:
        timeline.record(event, "tool", name, "unconfirmed")
        raise
    except Exception as e:
        timeline.record(event, "tool", name, "unconfirmed")
        log.error(f"工具 {name} 执行异常: {e!r}")
        return f"[工具 {name} 出错: {type(e).__name__}: {e}]"
