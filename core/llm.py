"""LLM 调用 —— provider 抽象 + OpenAI 兼容实现（含 tool calling）。

加新模型供应商 = 新建一个 LLMProvider 子类并注册进 _PROVIDERS。
"""

from __future__ import annotations

import json
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Callable

import httpx

from config import (
    LLM_API_KEY,
    LLM_BASE_URL,
    LLM_ENABLE_TOOLS,
    LLM_MAX_TOKENS,
    LLM_MAX_TOOL_ROUNDS,
    LLM_MODEL,
    LLM_PROVIDER,
    LLM_TEMPERATURE,
    LLM_THINKING,
    LLM_REASONING_EFFORT,
    LLM_THINKING_MAX_TOKENS,
    LLM_TIMEOUT,
    REPLY_SEPARATOR,
    SYSTEM_PROMPT,
)
from core.log import log
from core import timeline
from core.session import get_session
from core.tools import call_tool, get_tool_schemas, has_tools

http_client = httpx.AsyncClient(timeout=LLM_TIMEOUT)


@dataclass
class ChatResult:
    text: str
    # 仅本次生成的 assistant/tool 消息，不含输入图片/base64。
    messages: list[dict] = field(default_factory=list)
    ok: bool = True


def initial_payload(messages, tools=None, model=None, *, thinking=None, max_tokens=None, response_format=None,
                    reasoning_effort=None):
    """组装首轮请求正文，不解析附件、访问网络或执行工具。"""
    use_thinking = LLM_THINKING if thinking is None else thinking
    if tools and use_thinking:
        # 旧版文字历史可能没有推理字段；不要修改原会话中的字典。
        messages = [{"reasoning_content": "", **m} if m.get("role") == "assistant" else m for m in messages]
    payload = {
        "model": model or LLM_MODEL,
        "messages": messages,
        "thinking": {"type": "enabled" if use_thinking else "disabled"},
        "max_tokens": max_tokens if max_tokens is not None else (
            LLM_THINKING_MAX_TOKENS if use_thinking else LLM_MAX_TOKENS
        ),
    }
    if use_thinking:
        payload["reasoning_effort"] = reasoning_effort or LLM_REASONING_EFFORT
    else:
        payload["temperature"] = LLM_TEMPERATURE
    if tools:
        payload["tools"] = tools
    if response_format is not None:
        payload["response_format"] = response_format
    return payload


class LLMProvider(ABC):
    """LLM 供应商抽象。

    messages 为 OpenAI 风格的对话列表；tools 为工具 schema 列表（None 表示不带工具）。
    """

    @abstractmethod
    async def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        model: str | None = None,
        *,
        thinking: bool | None = None,
        max_tokens: int | None = None,
        response_format: dict | None = None,
        is_current: Callable[[], bool] | None = None,
        reasoning_effort: str | None = None,
    ) -> ChatResult: ...


class OpenAICompatProvider(LLMProvider):
    """OpenAI 兼容接口（deepseek / openai / 本地 vLLM 等）。"""

    async def _post(self, payload: dict) -> dict:
        started = time.monotonic()
        resp = await http_client.post(
            f"{LLM_BASE_URL.rstrip('/')}/chat/completions",
            headers={
                "Authorization": f"Bearer {LLM_API_KEY}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
        resp.raise_for_status()
        data = resp.json()
        _log_usage(payload, data, time.monotonic() - started)
        return data

    async def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        model: str | None = None,
        *,
        thinking: bool | None = None,
        max_tokens: int | None = None,
        response_format: dict | None = None,
        is_current: Callable[[], bool] | None = None,
        reasoning_effort: str | None = None,
    ) -> ChatResult:
        from core.images import prepare_messages

        generated: list[dict] = []
        visible_parts: list[str] = []
        sticker_attempted = False

        def failure(text: str) -> ChatResult:
            # 保留已完成的工具操作，避免下一轮遗忘其副作用；不保存空/截断正文。
            # 之前完整生成的正文仍需发送，否则历史会包含用户没收到的回答。
            return ChatResult(f"\n{REPLY_SEPARATOR}\n".join([*visible_parts, text]), generated, ok=False)

        # LLM_MAX_TOOL_ROUNDS=0 视为不限，但保留硬上限防失控死循环烧钱
        max_rounds = LLM_MAX_TOOL_ROUNDS if LLM_MAX_TOOL_ROUNDS > 0 else 50
        try:
            base = initial_payload(await prepare_messages(messages), tools, model, thinking=thinking,
                                   max_tokens=max_tokens, response_format=response_format,
                                   reasoning_effort=reasoning_effort)
            msgs = base["messages"]
            for round_index in range(max_rounds + 1):
                payload = base.copy()
                if tools:
                    round_tools = [t for t in tools if not (
                        sticker_attempted and t.get("function", {}).get("name") == "send_sticker"
                    )]
                    payload["tools"] = round_tools or tools
                    if not round_tools or round_index >= max_rounds:
                        # 保留工具 schema 以兼容已有工具历史，但这次只请求文字收尾。
                        payload["tool_choice"] = "none"
                data = await self._post(payload)
                if is_current is not None and not is_current():
                    return ChatResult("", generated, ok=False)
                choice = data["choices"][0]
                finish_reason = choice.get("finish_reason")
                if finish_reason == "length":
                    log.warning("LLM 输出达到长度限制，未执行本轮工具或保存截断回复")
                    return failure("[这次生成的回复过长，被截断了，请再试一次喵~]")
                if finish_reason not in (None, "stop", "tool_calls"):
                    log.warning(f"LLM 输出未完成: {finish_reason}")
                    return failure("[这次没能完成回复，请稍后再试喵~]")
                raw = choice["message"]
                message = {k: raw[k] for k in ("content", "reasoning_content", "tool_calls") if k in raw}
                message["role"] = "assistant"
                tool_calls = message.get("tool_calls")
                text = (message.get("content") or "").strip()

                if not tool_calls:
                    if text:
                        visible_parts.append(text)
                        generated.append(message)
                    if not visible_parts:
                        return failure("[模型没有返回正文，请稍后再试喵~]")
                    return ChatResult(f"\n{REPLY_SEPARATOR}\n".join(visible_parts), generated)

                if not tools:
                    return failure("[模型返回了未启用的工具调用]")
                if round_index >= max_rounds:
                    log.warning(f"工具调用超过 {max_rounds} 轮，已中止")
                    return failure("[工具调用轮数超限喵~]")

                # 模型要求调用工具：把这一轮回复（含 tool_calls）接回对话，再请求一轮。
                # 普通聊天保留中途正文；JSON 协议只交付最终对象，避免前言破坏解析。
                if text and response_format is None:
                    visible_parts.append(text)
                msgs.append(message)
                generated.append(message)
                allowed_names = {t.get("function", {}).get("name") for t in tools}
                for tc in tool_calls:
                    if is_current is not None and not is_current():
                        return ChatResult("", generated, ok=False)
                    fn = tc.get("function", {})
                    name = fn.get("name", "")
                    try:
                        args = json.loads(fn.get("arguments") or "{}")
                        if not isinstance(args, dict):
                            raise ValueError("工具参数必须为对象")
                    except (ValueError, TypeError):
                        result = "[工具参数不是合法 JSON 对象，请修正后再调用]"
                    else:
                        if name not in allowed_names:
                            result = "[本次请求未提供此工具]"
                        elif name == "send_sticker" and sticker_attempted:
                            result = "[本轮已经尝试发送表情，不再重复发送。请用文字回答用户的问题。]"
                        else:
                            if name == "send_sticker":
                                # 包括发送失败的尝试；同一响应的多个调用也受此限制。
                                sticker_attempted = True
                            log.info(f"[工具调用] {name}")
                            result = await call_tool(name, args)
                    tool_message = {
                        "role": "tool",
                        "tool_call_id": tc.get("id"),
                        "content": result,
                    }
                    msgs.append(tool_message)
                    generated.append(tool_message)

        except httpx.HTTPStatusError as e:
            log.error(
                f"LLM API HTTP 错误: {e.response.status_code} {e.response.text[:200]}"
            )
            return failure(f"[LLM 请求失败: HTTP {e.response.status_code}]")
        except Exception as e:
            log.error(f"LLM 调用异常: {e}")
            return failure(f"[LLM 调用出错: {type(e).__name__}]")


def _log_usage(payload: dict, data: dict, elapsed: float) -> None:
    """每轮请求记一行耗时与用量，便于区分是排队、思考过长还是缓存未命中导致的延迟。"""
    usage = data.get("usage") if isinstance(data, dict) else None
    usage = usage if isinstance(usage, dict) else {}
    thinking = payload.get("thinking", {}).get("type") == "enabled"
    parts = [f"{elapsed:.1f}s", f"思考={payload.get('reasoning_effort', '?') if thinking else '关'}"]
    if "prompt_tokens" in usage:
        hit = usage.get("prompt_cache_hit_tokens")
        parts.append(f"输入={usage['prompt_tokens']}" + (f"(缓存命中{hit})" if hit is not None else ""))
    if "completion_tokens" in usage:
        details = usage.get("completion_tokens_details")
        reasoning = details.get("reasoning_tokens") if isinstance(details, dict) else None
        parts.append(f"输出={usage['completion_tokens']}" + (f"(推理{reasoning})" if reasoning else ""))
    log.info("[LLM] " + " ".join(parts))


# provider 注册表 —— 新增供应商在此登记
_PROVIDERS: dict[str, type[LLMProvider]] = {
    "openai_compat": OpenAICompatProvider,
}

_provider_cache: LLMProvider | None = None


def get_provider() -> LLMProvider:
    global _provider_cache
    if _provider_cache is None:
        cls = _PROVIDERS.get(LLM_PROVIDER)
        if cls is None:
            raise ValueError(
                f"未知的 LLM_PROVIDER: {LLM_PROVIDER!r}，可选: {list(_PROVIDERS)}"
            )
        _provider_cache = cls()
    return _provider_cache


def record_user(session_key: str, user_input: str | list[dict]) -> None:
    """把一条用户消息即时记入会话历史（不调用 LLM）。

    asyncio 单线程下同步追加，不会与 responder 的同步快照交错，故无需加锁。
    连发合并：每条消息都先记历史累积上下文，回不回由 core.coalesce 防抖决定。
    """
    get_session(session_key).add_user(user_input)


def private_context(session_key: str, system_prompt: str, history: list, *, enable_tools: bool = True,
                    preview_event=None):
    recent = timeline.as_prompt(session_key, history, preview_event=preview_event)
    messages = [{"role": "system", "content": system_prompt + recent}, *history]
    tools = get_tool_schemas() if (enable_tools and LLM_ENABLE_TOOLS and has_tools()) else None
    return messages, {"tools": tools}


async def reply_turn(
    session_key: str,
    system_prompt: str = SYSTEM_PROMPT,
    *,
    enable_tools: bool = True,
) -> str:
    """基于已有历史回一次（不再 add_user）。取历史→调用→写回，在会话锁内串行。"""
    session = get_session(session_key)
    async with session.lock:
        history = list(session.messages)
        messages, options = private_context(session_key, system_prompt, history, enable_tools=enable_tools)
        result = await get_provider().chat(messages, **options)
        if history and result.messages:
            session.add_reply(result.messages, after=history[-1])
    return result.text

