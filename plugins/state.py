"""当前状态备注；不创建任务、不触发后台执行。"""

from core import timeline
from core.perms import is_master
from core.plugin import on_command


@on_command("state", priority=10)
async def state(event, bot, match):
    argument = match.text.strip()
    if not argument:
        text = timeline.as_prompt(event.session_key) or "共享经历已关闭或数据文件不可用。"
    elif event.message_type != "private" or not is_master(event):
        text = "状态备注只能由主人在私聊中修改。"
    elif not timeline.enabled() or timeline.snapshot() is None:
        text = "共享经历已关闭或数据文件不可用，未修改状态。"
    elif argument == "clear":
        timeline.clear_state()
        timeline.flush_if_dirty()
        text = "当前状态备注已清除。"
    else:
        action, _, note = argument.partition(" ")
        if action not in {"set", "share"}:
            text = "用法：/state 查看；/state set 备注（本会话）；/state share 备注（所有会话可见）；/state clear。备注不会启动任务。"
        else:
            try:
                timeline.set_state(event, note, shared=action == "share")
            except ValueError as exc:
                text = str(exc)
            else:
                timeline.flush_if_dirty()
                text = "已更新状态备注（所有会话可见）。" if action == "share" else "已更新状态备注（仅本会话可见）。"
    await bot.reply(event, text)
