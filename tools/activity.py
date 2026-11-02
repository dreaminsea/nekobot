"""request_action：聊天里请后台活动去做事（如定时提醒）。

只提交请求、当场拿到接受/完成/拒绝；接受的请求做完后，系统会把结果作为后台通知
送回这个会话，模型那时再告诉对方，避免"嘴上答应了却没做"或提前说已完成。
"""

from core import agent, runtime
from core.tools import tool


def _description() -> str:
    loop = agent.current()
    actions = loop.describe_actions() if loop is not None else ""
    return (
        "请后台活动去做一件需要时间、或在聊天之外进行的事，例如定时提醒。"
        "工具会立即返回是否接受：接受只代表开始做，不要说已经完成，完成后系统会通知你再告诉对方；"
        "被拒绝就如实说明原因。只有对方确实提出这类需求时才调用。\n当前可请求的动作：\n"
        + (actions or "（现在没有运行中的后台活动）")
    )


@tool(
    name="request_action",
    description=_description,
    parameters={
        "type": "object",
        "properties": {
            "activity": {"type": "string", "description": "活动名，如 timer"},
            "action": {"type": "string", "description": "动作名，如 remind"},
            "args": {"type": "object", "description": "动作参数，按上面列出的参数名填写"},
        },
        "required": ["activity", "action"],
    },
)
async def request_action(activity: str, action: str, args: dict | None = None) -> str:
    loop = agent.current()
    if loop is None:
        return "[外层循环未运行，不能提交请求]"
    return await loop.request(runtime.get_event(), activity, action, args or {})
