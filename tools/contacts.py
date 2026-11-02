"""通讯录工具 —— 让 bot 记住人和群的 id，之后按名字发消息。

用法示例：群里有人说"帮我提醒张三下次带水"，bot 可以：
  list_contacts("张三") → 找到 id → send_message(user_id="123", message="群主让你下次带水")
或
  remember_contact(name="张三", platform="onebot", user_id="123") 先记下。
"""

from core.contacts import find, forget, list_all, remember, search
from core.tools import tool


@tool(
    name="remember_contact",
    description=(
        "把一个人或群记进通讯录，方便之后按名字找 id 发消息。"
        "已在通讯录里的会自动去重（同 platform+id 不重复记）。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "称呼/备注名，之后按这个名字找（如『张三』『技术群』）",
            },
            "platform": {
                "type": "string",
                "description": "哪个平台：'onebot'(QQ) 或 'feishu'(飞书)",
            },
            "user_id": {
                "type": "string",
                "description": "这个人的 id（QQ 号/飞书 open_id）或群的 id（群号/chat_id）",
            },
            "is_group": {
                "type": "boolean",
                "description": "这是不是群（true=群, false=人）",
            },
            "notes": {
                "type": "string",
                "description": "备注（可选，如『群主』『爱发猫图』）",
            },
        },
        "required": ["name", "platform", "user_id"],
    },
)
async def remember_contact(
    name: str, platform: str, user_id: str, is_group: bool = False, notes: str = ""
) -> str:
    return remember(name, platform, user_id, is_group, notes)


@tool(
    name="list_contacts",
    description=(
        "查通讯录：按名字或备注模糊搜索已记住的人/群。不传 query 列出全部。"
        "找到后可用 send_message 按名发消息（填 user_id 为通讯录里的 id）。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "搜索关键词（模糊匹配名字或备注）。不填列出全部",
            },
        },
    },
)
async def list_contacts(query: str = "") -> str:
    results = search(query)
    if not results:
        return f"通讯录里没有「{query}」相关的人或群喵~" if query else "通讯录是空的喵~"
    lines = []
    for e in results:
        tag = "群" if e.get("is_group") else "人"
        extra = f"（{e['notes']}）" if e.get("notes") else ""
        id_part = (e.get("id") or "")[:16]
        lines.append(f"- {e['name']} [{tag}] {e['platform']}:{id_part}{extra}")
    return "\n".join(lines)


@tool(
    name="forget_contact",
    description=(
        "从通讯录里删掉一个人或群。传名字或 id 都行。不会报错（找不到就提示）。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "要删的名字或 id",
            },
        },
        "required": ["query"],
    },
)
async def forget_contact(query: str) -> str:
    return forget(query)
