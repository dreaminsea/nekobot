"""NapCat 掉线监控 —— 探测 QQ 链路，掉线则飞书告警 + 重启 + 推登录二维码。

检测信号（实测有效）：OneBot 动作 `get_stranger_info(BOT_QQ, no_cache=True)` 强制去
服务器拿数据——在线时正常返回，QQ 被风控踢下线时**超时无响应**。连续多次无响应即判定
掉线（NapCat 自身 get_status/get_login_info 仍会谎报在线，故不能用那些）。

掉线后：飞书私聊主人告警 →（可选）跑 shell 命令重启 NapCat → NapCat 重新登录会把
二维码写到 `NAPCAT_QR_PATH`，监控读取该 PNG 推到飞书，主人扫码即恢复 → 恢复后再报一条。
"""

from __future__ import annotations

import asyncio
import os
from typing import Callable

from config import (
    BOT_QQ,
    MASTER_FEISHU,
    NAPCAT_LOG_PATH,
    NAPCAT_QR_PATH,
    NAPCAT_RELOGIN_ERROR,
    NAPCAT_RESTART_CMD,
    NAPCAT_RESTART_WAIT,
    NAPCAT_WATCH_AUTO_RESTART,
    NAPCAT_WATCH_FAILS,
    NAPCAT_WATCH_INTERVAL,
)
from core import runtime
from core.bot import Bot, ImageSeg, TextSeg
from core.log import log


async def _alive(ob) -> bool:
    """QQ 链路是否还活着：get_stranger_info(no_cache) 有正常返回=在线，超时/异常=掉线。"""
    self_id = BOT_QQ or getattr(ob, "self_id", "")
    if not self_id:
        return True  # 拿不到 bot 自己的 QQ，无法判定，先当在线避免误报
    try:
        resp = await ob.call_action_resp(
            "get_stranger_info", timeout=8.0, user_id=int(self_id), no_cache=True
        )
    except Exception as e:
        log.debug(f"[掉线监控] 探测异常: {e!r}")
        return False
    return resp is not None and resp.get("status") == "ok"


async def _run_restart() -> bool:
    """跑 shell 命令重启 NapCat，返回是否退出码为 0。"""
    try:
        proc = await asyncio.create_subprocess_shell(
            NAPCAT_RESTART_CMD,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await proc.communicate()
        if proc.returncode != 0:
            log.warning(f"[掉线监控] 重启命令退出码 {proc.returncode}: {(out or b'')[:200]!r}")
        return proc.returncode == 0
    except Exception as e:
        log.warning(f"[掉线监控] 重启命令执行异常: {e!r}")
        return False


def _log_size() -> int:
    try:
        return os.path.getsize(NAPCAT_LOG_PATH)
    except OSError:
        return 0


def _read_log_since(pos: int) -> str:
    """读 NapCat 日志从 pos 到末尾的新增内容（判断重启后快速登录是否失败）。"""
    try:
        with open(NAPCAT_LOG_PATH, "r", errors="ignore") as f:
            f.seek(pos)
            return f.read()
    except OSError as e:
        log.debug(f"[掉线监控] 读日志失败: {e!r}")
        return ""


async def _notify(bot: Bot, parts) -> None:
    if not MASTER_FEISHU:
        log.warning("[掉线监控] 未配置 MASTER_FEISHU，跳过飞书告警")
        return
    try:
        await bot.send_private_msg(MASTER_FEISHU, parts)
    except Exception as e:
        log.warning(f"[掉线监控] 飞书发送失败: {e!r}")


async def _push_qr(bot: Bot) -> None:
    """把 NapCat 登录二维码图片推给主人。"""
    if not os.path.exists(NAPCAT_QR_PATH):
        await _notify(bot, f"⚠️ 找不到登录二维码文件：{NAPCAT_QR_PATH}")
        return
    await _notify(
        bot,
        [TextSeg("扫这个码重新登录 QQ（约 2 分钟内有效，过期发 /login 再要一张）：\n"), ImageSeg(NAPCAT_QR_PATH)],
    )
    log.info("[掉线监控] 已推送登录二维码到飞书")


async def watch(get_feishu_bot: Callable[[], Bot | None]) -> None:
    """监控主循环。`get_feishu_bot()` 返回已连上的飞书 Bot（未就绪返回 None）。"""
    online = True            # 上次是否在线
    fails = 0                # 连续探测失败次数

    log.info(f"[掉线监控] 启动：每 {NAPCAT_WATCH_INTERVAL}s 探测一次，连续 {NAPCAT_WATCH_FAILS} 次无响应判定掉线")
    while True:
        await asyncio.sleep(NAPCAT_WATCH_INTERVAL)

        fb = get_feishu_bot()
        ob = runtime.get_adapter_bot("onebot")
        if fb is None or ob is None:
            continue  # 飞书或 QQ 适配器还没就绪

        if await _alive(ob):
            if not online:
                await _notify(fb, "✅ QQ 已重新登录上线啦~")
                log.info("[掉线监控] QQ 已恢复在线")
            online = True
            fails = 0
            continue

        # 无响应
        fails += 1
        if fails < NAPCAT_WATCH_FAILS:
            log.info(f"[掉线监控] 探测无响应（{fails}/{NAPCAT_WATCH_FAILS}），继续观察")
            continue
        if not online:
            continue  # 已处理过这次掉线，等恢复；恢复由上面的 _alive 分支报

        # 连续失败达阈值 → 确认掉线（每次掉线只处理一次）
        online = False
        log.warning("[掉线监控] 确认 QQ 掉线")
        if not (NAPCAT_WATCH_AUTO_RESTART and NAPCAT_RESTART_CMD):
            await _notify(fb, "⚠️ QQ 掉线了喵！请去服务器重启 NapCat，再发 /login 获取二维码扫码。")
            continue

        await _notify(fb, "⚠️ QQ 掉线了喵！正在 `napcat restart` 重启…")
        log_pos = _log_size()
        ok = await _run_restart()
        if not ok:
            await _notify(fb, "⚠️ 重启命令执行失败，去服务器看下喵")
            continue

        # 等 NapCat 起来 + 尝试快速登录，再读日志判断结果
        await asyncio.sleep(NAPCAT_RESTART_WAIT)
        new_log = _read_log_since(log_pos)
        if NAPCAT_RELOGIN_ERROR and NAPCAT_RELOGIN_ERROR in new_log:
            await _notify(fb, "⚠️ 快速登录失败（身份已失效），需要扫码登录：")
            await _push_qr(fb)
        else:
            await _notify(fb, "已重启，正在等待重新登录…（若快速登录成功会自动恢复；需扫码可发 /login）")
