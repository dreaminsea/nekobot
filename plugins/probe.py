"""QQ 链路探测 —— 找一个能区分「在线 / 被静默踢下线」的信号。

背景：NapCat 被风控踢下线时不报错、自身状态查询也可能照常报在线；给自己发私聊也
不会回环。所以这里改成「动作返回诊断」：一次性调用几个只读 OneBot 动作，把成功数据
或失败原因摘要打回给主人，同时检查适配器能力、NapCat 版本与 Packet 基础连接。

用法（主人私聊 `/probe`，飞书/QQ 都能触发，探的都是 QQ 链路）：
  - QQ 在线时发一次，记录各动作的返回；
  - QQ 被踢下线后再发一次（从**飞书**发，因为 QQ 收不到了），对比哪个动作会
    超时 / 报错 / 返回变化 —— 那就是可用的掉线判定信号。

确认信号后，再把它接成 napcat_watch 的定时自动探测。
"""

import json
import os

from adapters.onebot_errors import OnebotActionError
from config import BOT_QQ, NAPCAT_QR_PATH
from core import runtime
from core.bot import ImageSeg, TextSeg
from core.event import MessageEvent
from core.log import log
from core.perms import master_only
from core.plugin import MatchResult, on_command

# 待诊断的只读动作（带参数的用 lambda 接 self_id）。no_cache 强制真去服务器拿，最可能暴露掉线。
def _actions(self_id: str) -> list[tuple[str, dict]]:
    acts: list[tuple[str, dict]] = [
        ("get_version_info", {}),
        ("nc_get_packet_status", {}),
        ("get_status", {}),
        ("get_login_info", {}),
        ("get_online_clients", {"no_cache": True}),
    ]
    if self_id:
        acts.append(("get_stranger_info", {"user_id": int(self_id), "no_cache": True}))
    return acts


@on_command("probe", "ping", scope="private", priority=5)
@master_only
async def start_probe(event: MessageEvent, bot, match: MatchResult):
    log.info(f"[探测] 收到 /probe 来自 {event.platform}:{event.user_id}")
    ob = runtime.get_adapter_bot("onebot")
    if ob is None:
        await bot.reply(event, "QQ(onebot) 适配器还没连上，没法探测喵")
        return
    self_id = BOT_QQ or getattr(ob, "self_id", "")

    await bot.reply(event, "开始探测 QQ 链路，逐个调动作中…")
    capabilities = [
        f"{label}={'已加载' if callable(getattr(ob, method, None)) else '未加载，请完整重启 bot'}"
        for label, method in (("合并转发读取", "get_forward_messages"), ("主动戳人", "send_poke"))
    ]
    lines: list[str] = ["QQ 适配器：" + "；".join(capabilities)]
    for action, params in _actions(self_id):
        try:
            resp = await ob.call_action_resp(action, timeout=8.0, **params)
        except Exception as exc:
            lines.append(f"• {action}: 连接/调用异常（{type(exc).__name__}）")
            continue
        if resp is None:
            lines.append(f"⏱ {action}: 超时/无响应")
            log.info(f"[探测] {action} -> 超时")
            continue
        if not isinstance(resp, dict) or resp.get("status") != "ok" or resp.get("retcode", 0) != 0:
            detail = str(OnebotActionError(action, resp))
            lines.append(f"• {detail}")
            log.info(f"[探测] {detail}")
            continue
        status = resp.get("status")
        retcode = resp.get("retcode")
        data = resp.get("data")
        # data 可能很大，截断
        data_str = json.dumps(data, ensure_ascii=False)
        if len(data_str) > 300:
            data_str = data_str[:300] + "…"
        lines.append(f"• {action}: status={status} retcode={retcode} data={data_str}")
        log.info(f"[探测] {action} -> status={status} retcode={retcode} data={data_str}")

    lines.append("Packet 状态查询仅检查基础连接，不能保证发包可用；以戳一戳等动作的实际返回为准。")
    await bot.reply(event, "QQ 链路探测结果：\n" + "\n".join(lines))


@on_command("login", scope="private", priority=5)
@master_only
async def push_login_qr(event: MessageEvent, bot, match: MatchResult):
    """把 NapCat 当前的登录二维码发给主人（掉线重登扫码用）。"""
    log.info(f"[登录] 收到 /login 来自 {event.platform}:{event.user_id}")
    if not os.path.exists(NAPCAT_QR_PATH):
        await bot.reply(
            event,
            f"找不到登录二维码喵：{NAPCAT_QR_PATH}\n（NapCat 可能没在等登录——已在线，或还没生成二维码）",
        )
        return
    await bot.reply(
        event, [TextSeg("NapCat 登录二维码（约 2 分钟内有效，过期再发 /login）：\n"), ImageSeg(NAPCAT_QR_PATH)]
    )
    log.info("[登录] 已发送登录二维码")
