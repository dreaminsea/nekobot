"""OneBot 消息段解析，以及单层合并转发的展示；这里不进行网络请求。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import xml.etree.ElementTree as ET
from typing import Any

from config import FORWARD_MESSAGE_CHARS
from core import forwards, images
from core.bot import ImageInfo
from core.forwards import ForwardInfo


def _unescape(value: str) -> str:
    # amp 必须最后处理，避免把转义后的 CQ 字面量再次解释为消息段。
    return value.replace("&#91;", "[").replace("&#93;", "]").replace("&#44;", ",").replace("&amp;", "&")


def segments(message: Any) -> list[dict]:
    """兼容数组和 CQ 码字符串；只解析当前层，不遍历 node/forward 的内容。"""
    if isinstance(message, list):
        return [s for s in message if isinstance(s, dict) and isinstance(s.get("data"), dict)]
    if not isinstance(message, str):
        return []
    out = []
    end = 0
    for match in re.finditer(r"\[CQ:([a-zA-Z0-9_]+)((?:,[^\]]*)?)\]", message):
        if match.start() > end:
            out.append({"type": "text", "data": {"text": _unescape(message[end:match.start()])}})
        data = {}
        for item in match[2].split(",")[1:]:
            key, sep, value = item.partition("=")
            if sep:
                data[key] = _unescape(value)
        out.append({"type": match[1], "data": data})
        end = match.end()
    if end < len(message):
        out.append({"type": "text", "data": {"text": _unescape(message[end:])}})
    return out


def extract_text(message: Any) -> str:
    return "".join(str(s["data"].get("text", "")) for s in segments(message) if s.get("type") == "text").strip()


def extract_at_target(message: Any) -> list[str]:
    return [str(s["data"]["qq"]) for s in segments(message)
            if s.get("type") == "at" and s["data"].get("qq")]


def extract_images(message: Any) -> list[ImageInfo]:
    out = []
    for seg in segments(message):
        if seg.get("type") not in {"image", "mface"}:
            continue
        data = seg["data"]
        file = data.get("file") or data.get("file_id")
        url = str(data.get("url") or "")
        key = os.path.splitext(os.path.basename(str(file)))[0] if file else hashlib.md5(url.encode()).hexdigest()
        out.append(ImageInfo(key=key, url=url, summary=str(data.get("summary") or "")))
    return out


def _card_forward(seg: dict) -> ForwardInfo | None:
    """QQ 的嵌套记录有时以聊天记录 JSON/XML 卡片出现，只读取资源入口。"""
    raw = seg["data"].get("data", "")
    if seg.get("type") == "json":
        try:
            card = json.loads(raw) if isinstance(raw, str) else raw
        except (ValueError, TypeError):
            return None
        if not isinstance(card, dict) or card.get("app") != "com.tencent.multimsg":
            return None
        meta = card.get("meta")
        detail = meta.get("detail") if isinstance(meta, dict) else None
        if isinstance(detail, dict) and detail.get("resid"):
            return ForwardInfo(id=str(detail["resid"]), title=str(detail.get("summary") or card.get("prompt") or ""))
    elif seg.get("type") == "xml" and isinstance(raw, str):
        # 聊天卡片不需要 DTD/实体声明。
        if "<!DOCTYPE" in raw.upper() or "<!ENTITY" in raw.upper():
            return None
        try:
            card = ET.fromstring(raw)
        except ET.ParseError:
            return None
        if card.get("serviceID") == "35" and card.get("m_resid"):
            return ForwardInfo(id=card.attrib["m_resid"], title=card.get("brief", ""))
    return None


def extract_forwards(message: Any, *, key: str = "") -> list[ForwardInfo]:
    out = []
    nodes = []
    for index, seg in enumerate(segments(message)):
        kind, data = seg.get("type"), seg["data"]
        if kind == "forward":
            content = data.get("content", data.get("messages"))
            fid = str(data.get("id") or "")
            if fid or isinstance(content, list):
                info = ForwardInfo(id=fid, title=str(data.get("summary") or data.get("title") or ""),
                                   # 空 content 有可能只是未展开占位；有 ID 时仍须按需读取。
                                   messages=content if isinstance(content, list) and (content or not fid) else None)
                if key:
                    info.key = f"{key}/forward/{index}"
                out.append(info)
        elif kind == "node":
            nodes.append(seg)
        elif kind in {"json", "xml"}:
            info = _card_forward(seg)
            if info is not None:
                out.append(info)
    if nodes:
        info = ForwardInfo(messages=nodes)
        if key:
            info.key = f"{key}/nodes"
        out.append(info)
    return out


def render_forward_message(raw: Any, index: int, session_key: str, parent_id: str) -> dict:
    """标准 node 和 NapCat message 两种返回结构；嵌套内容仍只给入口。"""
    if not isinstance(raw, dict):
        return {"index": index, "text": "[无法识别的转发节点]"}
    node = raw.get("data") if raw.get("type") == "node" else raw
    if not isinstance(node, dict):
        return {"index": index, "text": "[无法识别的转发节点]"}
    sender = node.get("sender") or {}
    if not isinstance(sender, dict):
        sender = {}
    # 部分 NapCat node 同时带空 content 和实际 message，不能被空占位遮住正文。
    body = node.get("content") or node.get("message") or []
    parts = segments(body)
    text = []
    for seg in parts:
        kind, data = seg.get("type"), seg["data"]
        if kind == "text":
            text.append(str(data.get("text", "")))
        elif kind == "at":
            text.append(f"[@{data.get('qq', '')}]")
        elif kind == "face":
            text.append(f"[QQ表情:{data.get('id', '')}]")
        elif kind == "reply":
            text.append(f"[引用原记录消息:{data.get('id', '')}]")
        elif kind not in {"image", "mface", "forward", "node"}:
            text.append(f"[{kind}]")
    body_text = "".join(text)
    nested = extract_forwards(parts, key=f"{parent_id}/{index}")
    return {
        "index": index,
        "user_id": str(sender.get("user_id") or node.get("user_id") or node.get("uin") or ""),
        "nickname": str(sender.get("card") or sender.get("nickname") or node.get("nickname") or node.get("name") or "")[:120],
        "time": node.get("time"),
        "text": body_text[:FORWARD_MESSAGE_CHARS],
        "text_truncated": len(body_text) > FORWARD_MESSAGE_CHARS,
        "images": [f"[图片#{images.register(img)}]" for img in extract_images(parts)],
        "forwards": [forwards.placeholder(info, session_key) for info in nested],
    }
