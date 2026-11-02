"""进程级维护锁：bot 与离线备份/恢复不能同时读写状态。"""

from __future__ import annotations

import fcntl
import os
import weakref
from pathlib import Path

LOCK_NAME = ".nekobot.lock"
_held: dict[Path, tuple[int, weakref.ReferenceType]] = {}


def acquire_lock(root: Path):
    """持有返回的文件对象直到退出；不要删除锁文件，否则会出现两个锁 inode。"""
    root = root.resolve()
    fd = os.open(root / LOCK_NAME, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    handle = os.fdopen(fd, "r+")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        handle.close()
        raise RuntimeError("bot 或另一个备份/恢复进程正在运行，请先停止后重试") from exc
    _held[root] = (os.getpid(), weakref.ref(handle))
    return handle


def held_by_this_process(root: Path) -> bool:
    """在线导出只允许由已持有 bot 维护锁的进程发起，不能绕过另一个进程的锁。"""
    pid, ref = _held.get(root.resolve(), (None, lambda: None))
    handle = ref()
    return pid == os.getpid() and handle is not None and not handle.closed
