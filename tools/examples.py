"""示例工具 —— 演示如何给 LLM 加 function calling 能力。

默认启用、零外部依赖，可直接试。不需要就删掉本文件。
"""

import random
from datetime import datetime

from core.tools import tool


@tool(description="获取当前的日期和时间")
def get_current_time() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


@tool(
    description="掷骰子，返回 1 到 sides 之间的随机整数",
    parameters={
        "type": "object",
        "properties": {
            "sides": {"type": "integer", "description": "骰子面数，默认 6"},
        },
    },
)
def roll_dice(sides: int = 6) -> str:
    return str(random.randint(1, max(1, sides)))
