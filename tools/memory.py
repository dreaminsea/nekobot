"""记忆工具 —— 让 AI 主动记/查/删长期记忆（全局一本笔记）。

记下的东西会自动出现在之后每次回复的提示里，所以平时不必特意去查；
list/forget 主要用于管理。
"""

from core import memory
from core.log import log
from core.tools import tool


@tool(
    description=(
        "把一件你想长期记住的事写进记忆（全局，跨群跨对话都记得）。"
        "比如群友的喜好、约定、重要事实。一次记一条，写清楚、自带主语。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "content": {"type": "string", "description": "要记住的一句话，清晰完整。"},
        },
        "required": ["content"],
    },
)
async def remember(content: str) -> str:
    memory.add(content)
    log.info(f"[记忆+] {content[:60]}")
    return f"记住啦：{content}"


@tool(description="查看你目前记住的所有长期记忆。")
async def list_memories() -> str:
    items = memory.all()
    if not items:
        return "还没有记住任何东西喵~"
    return "\n".join(f"{i + 1}. {m['content']}" for i, m in enumerate(items))


@tool(
    description="删除记忆：删掉内容包含指定文字的长期记忆。",
    parameters={
        "type": "object",
        "properties": {
            "content": {"type": "string", "description": "要忘记的记忆里包含的文字。"},
        },
        "required": ["content"],
    },
)
async def forget(content: str) -> str:
    n = memory.remove(content)
    log.info(f"[记忆-] 删除含「{content[:30]}」的 {n} 条")
    return f"忘记了 {n} 条相关记忆~" if n else "没找到相关记忆喵~"
