"""全局长期记忆 —— bot 的一本笔记，跨会话共享、持久化到磁盘。

AI 通过记忆工具(tools/memory.py)主动记/查/删；这些记忆会自动注入到每次回复的
system prompt，让它"始终记得"。
"""

import time

from config import MEMORY_MAX
from core import storage
from core.log import log

_MEMORY_FILE = "memories.json"

# 每项: {"content": str, "ts": float}
_memories: list[dict] = []


def load() -> None:
    global _memories
    data = storage.load_json(_MEMORY_FILE, [])
    if isinstance(data, list):
        _memories = data
    if _memories:
        log.info(f"已恢复 {len(_memories)} 条记忆")


def save() -> None:
    storage.save_json(_MEMORY_FILE, _memories)


def add(content: str) -> None:
    content = content.strip()
    if not content:
        return
    _memories.append({"content": content, "ts": time.time()})
    # 超出上限丢最旧
    if len(_memories) > MEMORY_MAX:
        del _memories[: len(_memories) - MEMORY_MAX]
    save()


def all() -> list[dict]:
    return list(_memories)


def remove(query: str) -> int:
    """删除内容包含 query 的记忆，返回删除条数。"""
    global _memories
    query = query.strip()
    if not query:
        return 0
    before = len(_memories)
    _memories = [m for m in _memories if query not in m["content"]]
    removed = before - len(_memories)
    if removed:
        save()
    return removed


def as_prompt() -> str:
    """渲染成注入 system prompt 的记忆块；无记忆返回空串。"""
    if not _memories:
        return ""
    lines = "\n".join(f"- {m['content']}" for m in _memories)
    return f"\n【你记住的长期记忆】\n{lines}\n（以上是你之前用记忆工具记下的事情，回复时可参考。）"


load()
