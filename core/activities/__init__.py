"""内置后台活动。新活动在这里登记，按 `config.AGENT_ACTIVITIES` 自动启动。"""

from core.activities.timer import TimerActivity

BUILTIN = {TimerActivity.name: TimerActivity}
