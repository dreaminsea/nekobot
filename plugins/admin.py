"""远程运维命令（主人私聊）—— 不用 ssh 上服务器就能更新/回退/热重载。

- `/reload`        热重载 plugins/ 和 tools/（不重启进程）
- `/git <args>`    在仓库里跑 git，如 `/git status`、`/git log --oneline -5`、
                   `/git checkout <commit>`；若 HEAD 变了会自动热重载
- `/update`        `git pull --ff-only` + 热重载（远程拉取最新代码）
- `/rollback [n]`  `git reset --hard HEAD~n`（默认 1）+ 热重载（回退）
- `/backup`        在线导出完整状态到本地 ZIP（别名 /export）
- `/restart`       重启整个进程，QQ 恢复后私聊主人通知完成
- `/napcat`        重启 NapCat（跑 NAPCAT_RESTART_CMD）；若快速登录失败把二维码发给你

热重载只重新加载 plugins/tools；改了核心代码或 Python/依赖配置要 `/restart` 才生效。
`/update` 会自动判断改动是否涉及 core/config 并提示。从飞书发这些命令尤其方便——
QQ 掉线时也能远程修。
"""

import asyncio
import os
from pathlib import Path
import shlex
import sys

from config import (
    NAPCAT_LOG_PATH,
    NAPCAT_QR_PATH,
    NAPCAT_RELOGIN_ERROR,
    NAPCAT_RESTART_CMD,
    NAPCAT_RESTART_WAIT,
)
from core.bot import ImageSeg, TextSeg
from core.event import MessageEvent
from core.hotreload import reload_extensions
from core.log import log
from core.perms import master_only
from core.plugin import MatchResult, on_command
from core.restart_notice import RESTART_NOTICE_ENV

# 仓库根 = plugins/ 的上一级
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


async def _run_git(args: list[str], timeout: float = 60.0) -> tuple[int, str]:
    """在仓库里跑 git（不经 shell，安全）。返回 (exit_code, 合并输出)。"""
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", _REPO, *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except FileNotFoundError:
        return 127, "找不到 git 命令"
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return 124, f"git {' '.join(args)} 超时({int(timeout)}s)——可能在等远程认证"
    return proc.returncode or 0, (out or b"").decode("utf-8", "replace").strip()


async def _head() -> str:
    rc, out = await _run_git(["rev-parse", "--short", "HEAD"])
    return out if rc == 0 else ""


async def _run_shell(cmd: str, timeout: float = 60.0) -> tuple[int, str]:
    """跑一条 shell 命令（用于 NapCat 重启命令），返回 (exit_code, 合并输出)。"""
    proc = await asyncio.create_subprocess_shell(
        cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return 124, f"命令超时({int(timeout)}s)"
    return proc.returncode or 0, (out or b"").decode("utf-8", "replace").strip()


def _reload_info() -> str:
    c = reload_extensions()
    return f"工具 {c['tools']}、命令/消息 {c['matchers']}、中间件 {c['middlewares']}"


def _trunc(s: str, n: int = 1500) -> str:
    return s if len(s) <= n else s[:n] + "\n…(已截断)"


# 改动涉及这些就需要 /restart（热重载不覆盖 core/config/依赖/入口）
def _needs_restart(changed_files: list[str]) -> bool:
    return any(
        f.startswith("core/") or f in (
            "config.py", "bot.py", "requirements.txt", "pyproject.toml", "uv.lock", ".python-version",
        )
        for f in changed_files
    )


@on_command("reload", scope="private", priority=1)
@master_only
async def reload_cmd(event: MessageEvent, bot, match: MatchResult):
    await bot.reply(event, "正在热重载 plugins / tools …")
    try:
        info = _reload_info()
    except Exception as e:
        log.error(f"[运维] 热重载失败: {e!r}")
        await bot.reply(event, f"❌ 热重载失败（已回滚到旧版本，bot 仍在跑）：\n{e!r}")
        return
    await bot.reply(event, f"✅ 已热重载：{info}")


@on_command("git", scope="private", priority=1)
@master_only
async def git_cmd(event: MessageEvent, bot, match: MatchResult):
    args = shlex.split(match.text)
    if not args:
        await bot.reply(event, "用法：/git <参数>，如 /git status、/git log --oneline -5")
        return
    before = await _head()
    rc, out = await _run_git(args)
    after = await _head()
    msg = f"$ git {' '.join(args)}  (exit={rc})\n{_trunc(out) or '(无输出)'}"
    if after and after != before:  # HEAD 变了 → 自动热重载
        try:
            msg += f"\n\nHEAD {before}→{after}，已自动热重载：{_reload_info()}"
        except Exception as e:
            msg += f"\n\nHEAD 已变但热重载失败（已回滚扩展）：{e!r}"
    await bot.reply(event, msg)


@on_command("update", scope="private", priority=1)
@master_only
async def update_cmd(event: MessageEvent, bot, match: MatchResult):
    await bot.reply(event, "正在 git pull --ff-only …")
    before = await _head()
    rc, out = await _run_git(["pull", "--ff-only"])
    if rc != 0:
        await bot.reply(event, f"❌ git pull 失败：\n{_trunc(out)}")
        return
    after = await _head()
    if after == before:
        await bot.reply(event, f"已是最新（HEAD {after}），无更新。")
        return
    _, changed = await _run_git(["diff", "--name-only", before, after])
    restart_hint = (
        "\n⚠️ 改动涉及 core/config/依赖，发 /restart 重启进程才完全生效"
        if _needs_restart(changed.splitlines())
        else ""
    )
    try:
        await bot.reply(event, f"✅ 已更新 {before}→{after}，热重载：{_reload_info()}{restart_hint}")
    except Exception as e:
        await bot.reply(event, f"⚠️ 已拉到 {after} 但热重载失败（已回滚扩展）：{e!r}\n发 /restart 重启进程试试")


@on_command("rollback", scope="private", priority=1)
@master_only
async def rollback_cmd(event: MessageEvent, bot, match: MatchResult):
    n = match.text.strip() or "1"
    if not n.isdigit():
        await bot.reply(event, "用法：/rollback [步数]，如 /rollback 1")
        return
    before = await _head()
    rc, out = await _run_git(["reset", "--hard", f"HEAD~{n}"])
    if rc != 0:
        await bot.reply(event, f"❌ 回退失败：\n{_trunc(out)}")
        return
    after = await _head()
    try:
        await bot.reply(event, f"✅ 已回退 {before}→{after}，热重载：{_reload_info()}")
    except Exception as e:
        await bot.reply(event, f"⚠️ 已回退到 {after} 但热重载失败（已回滚扩展）：{e!r}")


@on_command("backup", "export", scope="private", priority=1)
@master_only
async def backup_cmd(event: MessageEvent, bot, match: MatchResult):
    from core import live_backup

    if match.text.strip():
        await bot.reply(event, "用法：/backup（或 /export），完成后会回复本地 ZIP 路径。")
        return
    if live_backup.running():
        await bot.reply(event, "已有导出任务在运行，请等它完成喵~")
        return
    await bot.reply(event, "正在捕获当前状态并后台导出，完成后告诉你保存位置喵~")
    try:
        path, manifest = await live_backup.export_running(Path(_REPO))
    except Exception as exc:
        log.warning(f"[备份] 在线导出失败: {exc!r}")
        await bot.reply(event, f"导出失败：{exc}")
        return
    count = sum(entry["kind"] == "file" for entry in manifest["entries"])
    await bot.reply(event, f"备份完成：{count} 个文件\n{path}\n"
                          "ZIP 保存在运行 bot 的机器上，包含密钥和私聊，请妥善保管。")


@on_command("restart", scope="private", priority=1)
@master_only
async def restart_cmd(event: MessageEvent, bot, match: MatchResult):
    from core import live_backup
    if live_backup.running():
        await bot.reply(event, "正在导出备份，请等完成后再 /restart 喵~")
        return
    await bot.reply(event, "正在重启进程…（重启后各平台会自动重连，稍等几秒）")
    log.warning("[运维] 收到 /restart，即将替换进程并在 QQ 恢复后通知主人")
    try:
        from core import session
        session.save()  # 落盘，别丢会话
    except Exception as e:
        log.warning(f"[运维] 重启前落盘失败: {e!r}")
    await asyncio.sleep(1)  # 留点时间把回复发出去
    # 回复和等待期间可能刚启动导出，exec 前必须再次检查，之后不再 await。
    if live_backup.running():
        await bot.reply(event, "等待期间开始了备份导出，本次重启已取消；完成后再 /restart 喵~")
        return
    from core import timeline
    timeline.flush_if_dirty()  # exec 不执行 bot.py 的 finally；保存等待期间的新经历。
    # 原地用同样的命令重启本进程（替换进程映像，等于全新加载所有代码）
    restart_env = os.environ.copy()
    restart_env[RESTART_NOTICE_ENV] = "1"
    os.execve(sys.executable, [sys.executable, *sys.argv], restart_env)


@on_command("napcat", "restartnapcat", scope="private", priority=1)
@master_only
async def napcat_cmd(event: MessageEvent, bot, match: MatchResult):
    """重启 NapCat（跑 NAPCAT_RESTART_CMD）；快速登录失败则把登录二维码发给主人。"""
    if not NAPCAT_RESTART_CMD:
        await bot.reply(event, "未配置 NAPCAT_RESTART_CMD，没法重启 NapCat")
        return

    await bot.reply(event, f"正在重启 NapCat：`{NAPCAT_RESTART_CMD}`")
    try:
        log_pos = os.path.getsize(NAPCAT_LOG_PATH)
    except OSError:
        log_pos = 0

    rc, out = await _run_shell(NAPCAT_RESTART_CMD)
    if rc != 0:
        await bot.reply(event, f"❌ 重启命令失败 (exit={rc})：\n{_trunc(out)}")
        return

    await bot.reply(event, f"已执行重启，等 {NAPCAT_RESTART_WAIT}s 看登录结果…")
    await asyncio.sleep(NAPCAT_RESTART_WAIT)

    new_log = ""
    try:
        with open(NAPCAT_LOG_PATH, "r", errors="ignore") as f:
            f.seek(log_pos)
            new_log = f.read()
    except OSError as e:
        log.debug(f"[运维] 读 NapCat 日志失败: {e!r}")

    if NAPCAT_RELOGIN_ERROR and NAPCAT_RELOGIN_ERROR in new_log:
        if os.path.exists(NAPCAT_QR_PATH):
            await bot.reply(
                event,
                [TextSeg("快速登录失败，扫码登录 QQ（约 2 分钟内有效，过期发 /login）：\n"), ImageSeg(NAPCAT_QR_PATH)],
            )
        else:
            await bot.reply(event, f"快速登录失败，但找不到二维码文件：{NAPCAT_QR_PATH}")
    else:
        await bot.reply(event, "✅ NapCat 已重启（日志无快速登录错误，应已自动登录）")
