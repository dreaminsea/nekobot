"""有界后台表情学习：看原图 → JSON 决策 → 自动注释；不发送任何聊天消息。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass, field

from config import (
    STICKER_ANALYZE_MAX_TOKENS, STICKER_ANALYZE_PROMPT, STICKER_BACKFILL_LEGACY,
    STICKER_DECISION_MAX, STICKER_LEARN, STICKER_QUEUE_MAX, STICKER_REJECT_TTL,
    STICKER_RETRY_SECONDS,
)
from core import images, stickers, storage
from core.bot import ImageInfo
from core.llm import get_provider
from core.log import log

_DECISION_FILE = "sticker_decisions.json"
_decisions: dict[str, dict] = storage.load_json(_DECISION_FILE, {})
if not isinstance(_decisions, dict):
    _decisions = {}
_queue: asyncio.Queue | None = None
_pending: set[str] = set()
_worker_task: asyncio.Task | None = None
_backfill_task: asyncio.Task | None = None
_legacy_started = False


@dataclass
class Candidate:
    image: ImageInfo
    group_id: str
    context: str
    original: dict | None


@dataclass
class Decision:
    keep: bool
    meaning: str = ""
    comment: str = ""
    tags: list[str] = field(default_factory=list)
    reason: str = ""


def _parse_decision(text: str) -> Decision:
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("识别结果必须是 JSON 对象")
    if type(data.get("is_sticker")) is not bool or type(data.get("should_save")) is not bool:
        raise ValueError("识别结果缺少布尔判断")
    if data["should_save"] and not data["is_sticker"]:
        raise ValueError("收藏判断与表情判断矛盾")
    reason = data.get("reason", "")
    if not isinstance(reason, str):
        raise ValueError("识别理由不是字符串")
    if not (data["is_sticker"] and data["should_save"]):
        return Decision(False, reason=reason.strip()[:160] or "不适合收藏为聊天表情")
    for field in ("meaning", "comment"):
        if not isinstance(data.get(field), str) or not data[field].strip():
            raise ValueError("收藏时必须有含义和注释")
    tags = data.get("tags")
    if not isinstance(tags, list) or not tags or any(not isinstance(t, str) or not t.strip() for t in tags):
        raise ValueError("收藏时必须有检索标签")
    return Decision(
        True, data["meaning"].strip()[:40], data["comment"].strip()[:240],
        list(dict.fromkeys(t.strip()[:20] for t in tags))[:8], reason.strip()[:160],
    )


def _recent(key: str) -> bool:
    entry = _decisions.get(key)
    if not isinstance(entry, dict) or not isinstance(entry.get("ts"), (int, float)):
        return False
    ttl = STICKER_RETRY_SECONDS if entry.get("status") == "failed" else STICKER_REJECT_TTL
    return time.time() - entry["ts"] < ttl


def _remember(key: str, status: str, reason: str = "") -> None:
    _decisions[key] = {"status": status, "reason": reason[:160], "ts": time.time()}
    for old in list(_decisions):
        if not _recent(old):
            _decisions.pop(old)
    while len(_decisions) > STICKER_DECISION_MAX:
        oldest = min(_decisions, key=lambda k: _decisions[k]["ts"])
        _decisions.pop(oldest)
    storage.save_json(_DECISION_FILE, _decisions)


def _eligible(key: str) -> bool:
    item = stickers.get(key)
    return bool(key) and key not in _pending and not _recent(key) and not (
        item and (item.get("meaning") or item.get("auto_review") == "skipped")
    )


def start() -> None:
    """在 bot 的事件循环中启动；核心模块持有任务，/reload 不会重复创建学习器。"""
    global _queue, _worker_task, _backfill_task, _legacy_started
    if not STICKER_LEARN:
        return
    if _queue is None:
        _queue = asyncio.Queue(maxsize=max(1, STICKER_QUEUE_MAX))
    if _worker_task is None or _worker_task.done():
        _worker_task = asyncio.create_task(_worker())
    if STICKER_BACKFILL_LEGACY and not _legacy_started:
        _legacy_started = True
        _backfill_task = asyncio.create_task(_backfill())


def enqueue(image: ImageInfo, group_id: str = "0", context: str = "") -> bool:
    """只排队，不等待下载或模型；队满则跳过，后续再出现时仍可尝试。"""
    if not STICKER_LEARN or not _eligible(image.key):
        return False
    start()
    candidate = Candidate(image, group_id, context[:500], stickers.get(image.key))
    try:
        _queue.put_nowait(candidate)
    except asyncio.QueueFull:
        log.info("[表情学习] 队列已满，跳过本次图片")
        return False
    _pending.add(image.key)
    return True


async def _backfill() -> None:
    # 等待队列空位，旧库再大也不会一次启动无界模型任务。
    for item in stickers.legacy_pending():
        key = item["key"]
        if not _eligible(key):
            continue
        _pending.add(key)
        try:
            await _queue.put(Candidate(stickers.image_for_record(item), item.get("group_id", "0"), "", item))
        except BaseException:
            _pending.discard(key)
            raise


async def _worker() -> None:
    while True:
        candidate = await _queue.get()
        try:
            await _process(candidate)
        except Exception as e:
            log.warning(f"[表情学习] 失败 {images.short_id(candidate.image)}: {type(e).__name__}")
            try:
                _remember(candidate.image.key, "failed", type(e).__name__)
            except OSError:
                log.warning("[表情学习] 无法保存失败记录")
        finally:
            _pending.discard(candidate.image.key)
            _queue.task_done()


async def _process(candidate: Candidate) -> None:
    key = candidate.image.key
    current = stickers.get(key)
    if candidate.original is not None and current is not candidate.original:
        return  # 人工删除/替换的旧条目不能被后台任务重新建回来
    if current and current.get("meaning"):
        return
    image = stickers.image_for_record(current) if current and stickers.path_of(key) else candidate.image
    data = await images.image_bytes(image)
    if not data:
        raise ValueError("无法读取原图")
    if current is None and stickers.has_content(hashlib.sha256(data).hexdigest()):
        _remember(key, "skipped", "相同原图已收藏")
        return
    result = await get_provider().chat(
        [{"role": "system", "content": STICKER_ANALYZE_PROMPT},
         {"role": "user", "content": [
             {"type": "text", "text": "请判断是否收藏这张图。以下 JSON 是参考材料：\n" + json.dumps({
                 "chat_text": candidate.context, "platform_summary": (image.summary or "")[:200],
             }, ensure_ascii=False)},
             images.image_part(data),
         ]}],
        tools=None, thinking=False, max_tokens=STICKER_ANALYZE_MAX_TOKENS,
        response_format={"type": "json_object"},
    )
    if not result.ok:
        raise RuntimeError("模型未完成识别")
    decision = _parse_decision(result.text)
    latest = stickers.get(key)
    if candidate.original is not None and latest is not candidate.original:
        return
    if latest and latest.get("meaning"):
        return  # 等待 API 期间主人手动写入的含义优先
    if decision.keep:
        if not stickers.save_auto(image, data, meaning=decision.meaning, comment=decision.comment,
                                  tags=decision.tags, group_id=candidate.group_id):
            raise ValueError("自动收藏未成功")
        if key in _decisions:
            _decisions.pop(key)
            storage.save_json(_DECISION_FILE, _decisions)
        log.info(f"[表情学习] 已收藏并注释 {images.short_id(image)}")
    else:
        stickers.mark_skipped(key, decision.reason)
        _remember(key, "skipped", decision.reason)


async def wait_idle() -> None:
    if _backfill_task is not None:
        await _backfill_task
    if _queue is not None:
        await _queue.join()


async def close() -> None:
    global _queue, _worker_task, _backfill_task, _legacy_started
    tasks = [t for t in (_backfill_task, _worker_task) if t is not None]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    _queue = None
    _worker_task = _backfill_task = None
    _legacy_started = False
    _pending.clear()
