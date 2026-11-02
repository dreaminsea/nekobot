"""通讯录 —— 让 bot 记住人和群的 id，方便后续按名字找人发消息。

跨平台：每条记录有 name（查找用）、platform、id、is_group、notes，按 name 全匹配查找。
数据持久化到 data/contacts.json。
"""

from __future__ import annotations

from config import DATA_DIR

from core import storage
from core.log import log

_INDEX_FILE = "contacts.json"

# name -> {name, platform, id, is_group, notes, ts}
_contacts: dict[str, dict] = {}


def load() -> None:
    global _contacts
    data = storage.load_json(_INDEX_FILE, {})
    if isinstance(data, dict):
        _contacts = data
    if _contacts:
        log.info(f"已恢复 {len(_contacts)} 个通讯录条目")


def save() -> None:
    storage.save_json(_INDEX_FILE, _contacts)


def find(name: str) -> dict | None:
    """按名精确查找，返回条目或 None。"""
    return _contacts.get(name.strip())


def search(query: str) -> list[dict]:
    """按名/备注模糊搜索。空查询返回全部。"""
    q = query.strip().lower()
    if not q:
        return list(_contacts.values())
    results = []
    for e in _contacts.values():
        if q in e.get("name", "").lower() or q in e.get("notes", "").lower():
            results.append(e)
    return results


def remember(name: str, platform: str, contact_id: str, is_group: bool = False, notes: str = "") -> str:
    """登记一条通讯录。返回提示。"""
    name = name.strip()
    if not name:
        return "得给个名字喵，不然记不住"
    old = _contacts.get(name)
    if old and old.get("id") == contact_id and old.get("platform") == platform:
        # 同名片更新备注
        if notes and old.get("notes") != notes:
            old["notes"] = notes
            save()
            return f"已更新「{name}」的备注"
        return f"「{name}」已经记过了喵（{_fmt(old)}）"
    _contacts[name] = {
        "name": name,
        "platform": platform,
        "id": contact_id.strip(),
        "is_group": is_group,
        "notes": notes.strip(),
        "ts": _now(),
    }
    save()
    log.info(f"[通讯录] 记住 {name} = {platform}:{contact_id}")
    return f"记住啦：{name} = {_fmt(_contacts[name])}"


def forget(query: str) -> str:
    """删除命中条目（可按名或 id）。"""
    q = query.strip()
    if not q:
        return "要删谁喵？给个名字或 id"
    for name, e in list(_contacts.items()):
        if name == q or (e.get("id") or "").strip() == q:
            del _contacts[name]
            save()
            return f"已忘记 {name}（{_fmt(e)}）"
    return f"没找到「{q}」喵~"


def list_all() -> str:
    """给系统提示/模型看的简要列表。"""
    if not _contacts:
        return "通讯录是空的。"
    lines = []
    for e in _contacts.values():
        tag = "群" if e.get("is_group") else "人"
        extra = f"（{e['notes']}）" if e.get("notes") else ""
        id_part = (e.get("id") or "")[:16]
        lines.append(f"- {e['name']} [{tag}] {e['platform']}:{id_part}{extra}")
    return "\n".join(lines)


def _fmt(e: dict) -> str:
    tag = "群" if e.get("is_group") else ""
    return f"{e['platform']} {tag} {e['id']}"


def _now() -> float:
    import time
    return time.time()


load()
