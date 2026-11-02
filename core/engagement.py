"""会话活跃度 —— 记录 bot 最近在各群回过话的时间。

用于「连续对话跟进」：bot 在某群回复后的一段窗口内，群消息不用 @ 也直接续聊。
"""

import time

# group_id -> 上次 bot 回复的时间戳
_last_reply_at: dict[str, float] = {}


def mark(group_id: str) -> None:
    """记录 bot 刚在该群回过话（每次回复都刷新窗口）。"""
    _last_reply_at[group_id] = time.time()


def in_window(group_id: str, window: float) -> bool:
    """该群是否在跟进窗口内（bot 最近 window 秒内回过话）。"""
    ts = _last_reply_at.get(group_id)
    return ts is not None and (time.time() - ts) <= window
