"""把群聊事件转换为带新消息边界的有限时间线，保留引用关系和原图引用。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone

from config import GROUP_CONTEXT_CHARS
from core.session import Session


@dataclass
class GroupSnapshot:
    messages: list[dict]
    through: int
    generation: int
    reply_ids: set[str]
    directed: bool
    user_ids: set[str]
    trigger_seq: int
    # 本批的直接互动全部早于某条 BOT 回复：回复生成时没看到它们，发送者当时也没看到回复。
    directed_overlap: bool = False


def _render(message: dict, *, new: bool, limit: int, early: bool = False) -> tuple[str, list[dict]]:
    meta = message.get("_group", {})
    content = message.get("content") or ""
    refs = []
    if isinstance(content, list):
        refs = [dict(p) for p in content if p.get("type") == "image_ref"]
        content = "\n".join(p.get("text", "") for p in content if p.get("type") == "text")
    if len(content) > limit:
        content = content[:limit] + "…[本条正文过长，已截断]"
    stamp = meta.get("time")
    record = {"范围": "本轮新增" if new else "此前历史"}
    if early:
        record["先于BOT回复"] = True
    record.update({
        "id": meta.get("id"),
        "时间": datetime.fromtimestamp(stamp, timezone.utc).isoformat() if stamp else "未知",
        "发言人": "BOT" if message.get("role") == "assistant" else meta.get("sender", "旧记录"),
        "用户ID": meta.get("user_id"),
        "@": meta.get("mentions", []),
        "引用": meta.get("reply_to"),
        "指向bot": meta.get("directed", False),
        "事件": "戳一戳" if meta.get("poke_target") else "消息",
        "戳一戳目标": meta.get("poke_target"),
        "正文": content,
    })
    return json.dumps(record, ensure_ascii=False), refs


def build_group_snapshot(session: Session) -> GroupSnapshot | None:
    pending = [m for m in session.messages
               if m.get("role") == "user" and m.get("_group", {}).get("seq", 0) > session.group_cursor]
    if not pending:
        return None
    budget = max(2048, GROUP_CONTEXT_CHARS)
    # 单条特别长的正文只在请求副本中截断；存储仍保留原文。
    limit = max(256, budget // 4)
    pending_ids = {id(m) for m in pending}
    # 待处理消息之后出现的 BOT 回复一定是没看到它的旧批次生成的（时间线按到达/发送顺序存储）。
    last_reply = max((i for i, m in enumerate(session.messages) if m.get("role") == "assistant"), default=0)
    early = {id(m) for m in session.messages[:last_reply] if id(m) in pending_ids}
    rendered = {id(m): _render(m, new=id(m) in pending_ids, limit=limit, early=id(m) in early)
                for m in session.messages if m.get("content")}
    chosen = []
    used = 0
    # 若普通积压占满预算，把它们转为背景，直到本批能包含最早的直接互动。
    first_direct = next((m["_group"]["seq"] for m in pending if m["_group"].get("directed")), 0)
    for m in pending:
        size = len(rendered[id(m)][0])
        if chosen and used + size > budget * 0.65:
            if chosen[-1]["_group"]["seq"] >= first_direct:
                break
            chosen.clear()
            used = 0
        chosen.append(m)
        used += size
    through = chosen[-1]["_group"]["seq"]
    new_ids = {id(m) for m in chosen}
    directed = [m for m in chosen if m["_group"].get("directed")]
    old = [m for m in session.messages if id(m) in rendered and id(m) not in new_ids
           and m.get("_group", {}).get("seq", 0) <= through]
    by_id = {m.get("_group", {}).get("id"): m for m in old if m.get("_group", {}).get("id")}
    # 引用的原文与近期 bot 发言优先于普通旁观消息；最后仍按真实到达/发送顺序输出。
    priority = [by_id[m["_group"]["reply_to"]] for m in chosen
                if m["_group"].get("reply_to") in by_id]
    priority += [m for m in reversed(old) if m.get("role") == "assistant"][:4]
    priority += list(reversed(old))
    selected = set(new_ids)
    for m in priority:
        size = len(rendered[id(m)][0])
        if id(m) not in selected and used + size <= budget - 256:
            selected.add(id(m))
            used += size
    records = [m for m in session.messages if id(m) in selected]
    reply_ids = {m["_group"]["id"] for m in records if m.get("_group", {}).get("id")}
    parts = [{"type": "text", "text": "以下是群聊事件时间线（JSON行），只处理标记为本轮新增的消息；引用原文未呈现时不要猜测。"}]
    for m in records:
        text, refs = rendered[id(m)]
        if id(m) in pending_ids and id(m) not in new_ids:
            text, refs = _render(m, new=False, limit=limit)
        parts.append({"type": "text", "text": text})
        parts.extend(refs)  # 紧跟所属消息，保留 image_ref 的可信附件身份
    return GroupSnapshot(
        messages=[{"role": "user", "content": parts}], through=through,
        generation=session.generation, reply_ids=reply_ids,
        directed=bool(directed),
        user_ids={m["_group"]["user_id"] for m in chosen},
        trigger_seq=directed[-1]["_group"]["seq"] if directed else through,
        directed_overlap=bool(directed) and all(id(m) in early for m in directed),
    )
