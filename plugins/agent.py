"""外层循环控制：后台活动启停、分步工作的暂停/继续/停止和离线演示。"""

from core import agent
from core.plugin import on_command

USAGE = ("用法：/agent status | start <活动> | stop <活动> | pause | resume | stop；"
         "主人私聊可 /agent demo [步数] [间隔秒]。")


@on_command("agent", priority=10)
async def agent_command(event, bot, match):
    loop = agent.current()
    if loop is None:
        await bot.reply(event, "外层循环尚未启动；更新 core 后需要完整 /restart。")
        return
    args = match.text.split()
    action = args[0] if args else "status"
    if action == "status" and len(args) <= 1:
        text = loop.status(event)
    elif action in {"start", "stop"} and len(args) == 2:
        text = await loop.control_activity(action, args[1], event)
    elif action in {"pause", "resume", "stop"} and len(args) == 1:
        text = await loop.control(action, event)
    elif action == "demo" and len(args) <= 3:
        try:
            steps = int(args[1]) if len(args) > 1 else 5
            interval = float(args[2]) if len(args) > 2 else 1.0
        except ValueError:
            text = "用法：/agent demo [整数步数] [每步等待秒数]。"
        else:
            text = await loop.demo(event, steps=steps, interval=interval)
    else:
        text = USAGE
    await bot.reply(event, text)
