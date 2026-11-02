"""原生图文输入：轻量图片引用 → 有界本地缓存 → OpenAI image_url 内容块。"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import re
import time
from collections import OrderedDict
from copy import deepcopy
from pathlib import Path

import httpx

from config import (
    DATA_DIR, HISTORY_TTL, IMAGE_CACHE_MAX_BYTES, IMAGE_CONTEXT_MAX, IMAGE_DETAIL,
    IMAGE_MAX_BYTES, IMAGE_MAX_PER_REQUEST, IMAGE_PROMPT, IMAGE_REQUEST_MAX_BYTES,
    IMAGE_TIMEOUT,
)
from core.bot import ImageInfo
from core.log import log

_http = httpx.AsyncClient(timeout=IMAGE_TIMEOUT, follow_redirects=True)
_registry: OrderedDict[str, ImageInfo] = OrderedDict()
_cache_dir = Path(DATA_DIR) / "images"


def short_id(img: ImageInfo) -> str:
    # 飞书 image_key 有共同前缀，不能直接截取前八位。
    key = img.key or img.url
    return hashlib.sha256(key.encode()).hexdigest()[:16] if key else ""


def register(img: ImageInfo) -> str:
    sid = short_id(img)
    if sid:
        _registry[sid] = img  # 同图再次到达时刷新下载 URL / 飞书 fetch 闭包
        _registry.move_to_end(sid)
        while len(_registry) > IMAGE_CONTEXT_MAX:
            _registry.popitem(last=False)
    return sid


def get(image_id: str) -> ImageInfo | None:
    return _registry.get((image_id or "").strip())


def _guess_mime(data: bytes) -> str | None:
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    return None


async def fetch_bytes(img: ImageInfo) -> bytes | None:
    """QQ URL 流式限量下载；飞书通过 adapter 的鉴权闭包取图。"""
    try:
        if img.url:
            async with _http.stream("GET", img.url) as resp:
                resp.raise_for_status()
                data = bytearray()
                async for chunk in resp.aiter_bytes():
                    if len(data) + len(chunk) > IMAGE_MAX_BYTES:
                        return None
                    data.extend(chunk)
                return bytes(data)
        if img.fetch is not None:
            data = await asyncio.wait_for(img.fetch(), timeout=IMAGE_TIMEOUT)
            return data if len(data) <= IMAGE_MAX_BYTES else None
    except Exception as e:
        log.warning(f"[图片] 下载失败 {short_id(img)}: {type(e).__name__}")
    return None


def _prune_cache() -> None:
    """按 TTL、文件数和总字节数清理，仅管理本模块生成的缓存文件。"""
    files = []
    now = time.time()
    for path in _cache_dir.glob("*.bin"):
        if not re.fullmatch(r"[0-9a-f]{16}", path.stem):
            continue
        stat = path.stat()
        if now - stat.st_mtime > HISTORY_TTL:
            path.unlink(missing_ok=True)
        else:
            files.append((stat.st_mtime, stat.st_size, path))
    files.sort(reverse=True)
    total = 0
    for index, (_, size, path) in enumerate(files):
        total += size
        if index >= IMAGE_CONTEXT_MAX or total > IMAGE_CACHE_MAX_BYTES:
            path.unlink(missing_ok=True)


async def _load_bytes(sid: str) -> bytes | None:
    if not re.fullmatch(r"[0-9a-f]{16}", sid):
        return None
    path = _cache_dir / f"{sid}.bin"
    try:
        _prune_cache()
        if path.is_file() and path.stat().st_size <= IMAGE_MAX_BYTES:
            data = path.read_bytes()
            if _guess_mime(data):
                path.touch()
                return data
    except OSError as e:
        log.warning(f"[图片] 读取缓存失败: {type(e).__name__}")

    img = get(sid)
    data = await fetch_bytes(img) if img else None
    if not data or len(data) > IMAGE_MAX_BYTES or not _guess_mime(data):
        return None
    try:
        _cache_dir.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(data)
        tmp.replace(path)
        _prune_cache()
    except OSError as e:
        log.warning(f"[图片] 写入缓存失败: {type(e).__name__}")
    return data


async def image_bytes(img: ImageInfo) -> bytes | None:
    """按同一下载/格式/大小限制获取并缓存原图，供自动表情学习复用。"""
    return await _load_bytes(register(img))


def image_part(data: bytes) -> dict:
    """把已校验的原图转成内容块，保证识别用图与最终保存的字节一致。"""
    mime = _guess_mime(data)
    if not mime or len(data) > IMAGE_MAX_BYTES:
        raise ValueError("图片格式不支持或大小超限")
    url = f"data:{mime};base64,{base64.b64encode(data).decode()}"
    return {"type": "image_url", "image_url": {"url": url, "detail": IMAGE_DETAIL}}


def _select_refs(messages: list[dict]) -> tuple[list[dict], list[tuple[int, int, str]]]:
    """复制消息并按张数选最近的附件；仅处理结构化 image_ref。"""
    result = deepcopy(messages)
    refs = []
    for i, message in enumerate(result):
        if message.get("role") != "user" or not isinstance(message.get("content"), list):
            continue
        for j, part in enumerate(message["content"]):
            if part.get("type") == "image_ref":
                refs.append((i, j, part.get("image_id", "")))

    start = max(0, len(refs) - max(0, IMAGE_MAX_PER_REQUEST))
    for i, j, sid in refs[:start]:
        result[i]["content"][j] = {"type": "text", "text": f"[图片#{sid}]（本轮未附原图，可用 look_image 回看）"}
    return result, refs[start:]


def preview_messages(messages: list[dict]) -> list[dict]:
    """离线预览保留候选原图编号；下载结果和字节预算由真实请求决定。"""
    return _select_refs(messages)[0]


async def prepare_messages(messages: list[dict]) -> list[dict]:
    result, selected = _select_refs(messages)
    ids = list(dict.fromkeys(sid for _, _, sid in selected))
    loaded = dict(zip(ids, await asyncio.gather(*(_load_bytes(sid) for sid in ids))))
    total = 0
    for i, j, sid in reversed(selected):
        label = f"[图片#{sid}]"
        data = loaded.get(sid)
        if not data:
            note = "原图不可用：可能已过期、格式不支持或超过单图大小限制"
        elif total + len(data) > IMAGE_REQUEST_MAX_BYTES:
            note = "本轮图片总大小超限，可用 look_image 单独回看"
        else:
            total += len(data)
            result[i]["content"][j] = image_part(data)
            continue
        result[i]["content"][j] = {"type": "text", "text": f"{label}（{note}）"}
    return result


async def describe_id(image_id: str, question: str = "") -> str:
    """旧图回看工具复用主模型和请求配置，无独立视觉模型/对话线程。"""
    from core.llm import get_provider

    if not await _load_bytes(image_id):
        return "[看不到这张图：图片已过期、过大或格式不支持，请重新发图]"
    messages = [{"role": "user", "content": [
        {"type": "text", "text": question.strip() or IMAGE_PROMPT},
        {"type": "image_ref", "image_id": image_id},
    ]}]
    result = await get_provider().chat(messages)
    return result.text


async def describe(img: ImageInfo, question: str = "") -> str:
    return await describe_id(register(img), question)
