"""
QQ Bot — 手搓版喵~
通过 WebSocket 直连 NapCat (OneBot 11)，异步调用 LLM API 实现聊天。

依赖: pip install websockets httpx

NapCat 配置:
  在 WebUI → 网络配置 → 新建 → WebSocket 服务端
  端口 3001, 启用, token 和下面 NAPCAT_TOKEN 保持一致
"""

import asyncio
import json
import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field

import httpx
import websockets

# ============================================================
#  配置区 — 改这里就行
# ============================================================

NAPCAT_WS_URL = "ws://127.0.0.1:3001"  # NapCat 正向 WS 地址
NAPCAT_TOKEN = ""                        # NapCat 鉴权 token，没设就留空

LLM_BASE_URL = "https://api.deepseek.com"  # OpenAI 兼容接口
LLM_API_KEY = "sk-xxxxx"                       # 你的 API Key
LLM_MODEL = "deepseek-flash"                    # 模型名

SYSTEM_PROMPT = "你是一个有趣的 QQ 聊天机器人。回复简洁自然，像朋友聊天一样。"

MAX_HISTORY = 20          # 每个会话保留的最大轮数 (user+assistant 各算一轮)
HISTORY_TTL = 3600        # 会话超时(秒)，超时后清空历史
GROUP_TRIGGER = ["bot", "/chat"]  # 群聊触发前缀，私聊无需触发词
ADMIN_QQ = []             # 管理员 QQ 号列表 (int)

# ============================================================
#  日志
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("qqbot")

# ============================================================
#  会话历史管理
# ============================================================

@dataclass
class Session:
    messages: list = field(default_factory=list)
    last_active: float = field(default_factory=time.time)

    def add_user(self, text: str):
        self.messages.append({"role": "user", "content": text})
        self.last_active = time.time()
        self._trim()

    def add_assistant(self, text: str):
        self.messages.append({"role": "assistant", "content": text})
        self.last_active = time.time()
        self._trim()

    def clear(self):
        self.messages.clear()

    def is_expired(self) -> bool:
        return (time.time() - self.last_active) > HISTORY_TTL

    def _trim(self):
        # 保留最近 MAX_HISTORY 轮 (每轮 = 1 user + 1 assistant = 2 条)
        max_msgs = MAX_HISTORY * 2
        if len(self.messages) > max_msgs:
            self.messages = self.messages[-max_msgs:]

sessions: dict[str, Session] = defaultdict(Session)

def get_session(key: str) -> Session:
    s = sessions[key]
    if s.is_expired():
        s.clear()
    return s

# ============================================================
#  LLM 调用
# ============================================================

http_client = httpx.AsyncClient(timeout=60.0)

async def chat_completion(session_key: str, user_input: str) -> str:
    """调用 LLM，返回回复文本。"""
    session = get_session(session_key)
    session.add_user(user_input)

    payload = {
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            *session.messages,
        ],
        "temperature": 0.7,
        "thinking": {"type": "disabled"},
        "max_tokens": 1024,
    }

    try:
        resp = await http_client.post(
            f"{LLM_BASE_URL}/chat/completions",
            headers={
                "Authorization": f"Bearer {LLM_API_KEY}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
        resp.raise_for_status()
        data = resp.json()
        reply = data["choices"][0]["message"]["content"].strip()
        session.add_assistant(reply)
        return reply

    except httpx.HTTPStatusError as e:
        log.error(f"LLM API HTTP 错误: {e.response.status_code} {e.response.text[:200]}")
        return f"[LLM 请求失败: HTTP {e.response.status_code}]"
    except Exception as e:
        log.error(f"LLM 调用异常: {e}")
        return f"[LLM 调用出错: {type(e).__name__}]"

# ============================================================
#  OneBot 11 消息解析 & 发送
# ============================================================

def extract_text(message) -> str:
    """从 OneBot 11 消息段中提取纯文本。"""
    if isinstance(message, str):
        return message.strip()
    # array 格式
    parts = []
    for seg in message:
        if seg.get("type") == "text":
            parts.append(seg["data"]["text"])
    return "".join(parts).strip()

def extract_at_target(message) -> list[int]:
    """提取消息中 @的 QQ 号列表。"""
    if isinstance(message, str):
        return []
    return [
        int(seg["data"]["qq"])
        for seg in message
        if seg.get("type") == "at" and seg["data"].get("qq")
    ]

async def send_private_msg(ws, user_id: int, text: str):
    await ws.send(json.dumps({
        "action": "send_private_msg",
        "params": {"user_id": user_id, "message": text},
    }))

async def send_group_msg(ws, group_id: int, text: str, reply_to: int | None = None):
    # 可选: 引用回复原消息
    message = []
    if reply_to is not None:
        message.append({"type": "reply", "data": {"id": str(reply_to)}})
    message.append({"type": "text", "data": {"text": text}})

    await ws.send(json.dumps({
        "action": "send_group_msg",
        "params": {"group_id": group_id, "message": message},
    }))

# ============================================================
#  事件处理
# ============================================================

async def handle_private_message(ws, event: dict):
    """处理私聊消息。"""
    user_id = event["user_id"]
    raw_msg = event.get("message", "")
    text = extract_text(raw_msg)

    if not text:
        return

    # 内置命令
    if text in ("/clear", "/重置", "清空对话"):
        get_session(f"private_{user_id}").clear()
        await send_private_msg(ws, user_id, "对话已清空~")
        return

    log.info(f"[私聊] {user_id}: {text[:80]}")
    reply = await chat_completion(f"private_{user_id}", text)
    log.info(f"[回复] -> {reply[:80]}")
    await send_private_msg(ws, user_id, reply)

async def handle_group_message(ws, event: dict):
    """处理群聊消息 — 需要触发词或 @bot 才响应。"""
    group_id = event["group_id"]
    user_id = event["user_id"]
    message_id = event.get("message_id")
    self_id = event.get("self_id")
    raw_msg = event.get("message", "")
    text = extract_text(raw_msg)

    if not text:
        return

    # 检查是否 @了 bot
    at_list = extract_at_target(raw_msg)
    is_at_bot = self_id is not None and int(self_id) in at_list

    # 检查触发前缀
    triggered_by_prefix = False
    for prefix in GROUP_TRIGGER:
        if text.lower().startswith(prefix):
            text = text[len(prefix):].strip()
            triggered_by_prefix = True
            break

    if not is_at_bot and not triggered_by_prefix:
        return  # 没触发就不理

    # 内置命令
    if text in ("/clear", "/重置", "清空对话"):
        get_session(f"group_{group_id}").clear()
        await send_group_msg(ws, group_id, "群对话已清空~", reply_to=message_id)
        return

    log.info(f"[群聊 {group_id}] {user_id}: {text[:80]}")
    reply = await chat_completion(f"group_{group_id}", text)
    log.info(f"[回复] -> {reply[:80]}")
    await send_group_msg(ws, group_id, reply, reply_to=message_id)

async def handle_event(ws, event: dict):
    """事件路由。"""
    post_type = event.get("post_type")

    if post_type == "message":
        msg_type = event.get("message_type")
        if msg_type == "private":
            await handle_private_message(ws, event)
        elif msg_type == "group":
            await handle_group_message(ws, event)

    elif post_type == "meta_event":
        # 心跳 / 生命周期，忽略
        pass

    elif post_type == "notice":
        # 群成员变动、戳一戳等通知，按需扩展
        log.debug(f"[通知] {event.get('notice_type')}")

    elif post_type == "request":
        # 好友/群请求，按需扩展
        log.info(f"[请求] {event.get('request_type')}: {event}")

# ============================================================
#  WebSocket 主循环
# ============================================================

async def run():
    headers = {}
    if NAPCAT_TOKEN:
        headers["Authorization"] = f"Bearer {NAPCAT_TOKEN}"

    while True:
        try:
            log.info(f"正在连接 NapCat: {NAPCAT_WS_URL}")
            async with websockets.connect(
                NAPCAT_WS_URL,
                additional_headers=headers,
                ping_interval=30,
                ping_timeout=10,
            ) as ws:
                log.info("✅ 已连接 NapCat，开始监听事件")
                async for raw in ws:
                    try:
                        event = json.loads(raw)
                        # 不阻塞主循环，每个事件单独跑
                        asyncio.create_task(handle_event(ws, event))
                    except json.JSONDecodeError:
                        log.warning(f"收到非 JSON 数据: {raw[:100]}")

        except (websockets.ConnectionClosed, ConnectionRefusedError, OSError) as e:
            log.warning(f"连接断开/失败: {e}，5 秒后重连...")
            await asyncio.sleep(5)
        except Exception as e:
            log.error(f"未知错误: {e}，5 秒后重连...")
            await asyncio.sleep(5)

# ============================================================
#  入口
# ============================================================

if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        log.info("Bot 已停止 ~")