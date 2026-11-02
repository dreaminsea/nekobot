"""进程内在线导出：短暂捕获内存/文件引用，后台复制和压缩，恢复仍使用离线入口。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
import errno
import json
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
import threading
import time

from core import backup, maintenance

_job_lock = threading.Lock()  # 放在 core，插件 /reload 不会清掉正在执行的任务状态。
COPY_ATTEMPTS = 3
SQLITE_TIMEOUT = 30.0


class BackupBusy(backup.BackupError):
    pass


class SnapshotChanged(backup.BackupError):
    pass


def running() -> bool:
    return _job_lock.locked()


def _runtime_state() -> tuple[Path, Path, dict[str, bytes]]:
    # 所有这些模块都在主事件循环中同步修改状态；序列化期间不 await。
    # 不调用 save()，这样 SESSION_PERSIST=False 也能导出内存会话，而不改变磁盘策略。
    from core import contacts, memory, session, sticker_learning, stickers, storage, timeline

    if not hasattr(session, "snapshot"):
        raise backup.BackupError("会话模块仍是旧版本，请先 /restart，再使用 /backup")
    state = {
        "sessions.json": session.snapshot(),
        "memories.json": memory.all(),
        "contacts.json": contacts._contacts,
        "stickers.json": stickers._stickers,
        "sticker_decisions.json": sticker_learning._decisions,
    }
    agent_state = timeline.snapshot()
    if agent_state is not None:
        state[timeline.FILE] = agent_state
    payloads = {name: json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8")
                for name, value in state.items()}
    return Path(storage.DATA_DIR).absolute(), Path(stickers._img_dir).absolute(), payloads


@dataclass
class PinnedFile:
    entry: backup.Entry
    path: Path | None = None
    fd: int | None = None


@dataclass
class Snapshot:
    temporary: tempfile.TemporaryDirectory
    plan: backup.Plan
    captured_at: str
    files: list[PinnedFile] = field(default_factory=list)
    databases: list[backup.Entry] = field(default_factory=list)
    directories: list[backup.Entry] = field(default_factory=list)

    @property
    def root(self) -> Path:
        return Path(self.temporary.name)

    def close(self) -> None:
        try:
            for item in self.files:
                if item.fd is not None:
                    os.close(item.fd)
                    item.fd = None
        finally:
            self.temporary.cleanup()


def _capture(root: Path, output: Path) -> Snapshot:
    """主事件循环调用；仅序列化、遍历和固定 inode，不复制大图片/数据库。"""
    captured_at = datetime.now(timezone.utc).isoformat()
    data, stickers, payloads = _runtime_state()
    plan = backup.collect(root, output=output, data_dir=str(data), sticker_dir=str(stickers),
                          sticker_records=json.loads(payloads["stickers.json"]))
    backup._check_output(plan, output)
    snapshot = Snapshot(tempfile.TemporaryDirectory(prefix=".nekobot-live-", dir=root), plan, captured_at)
    try:
        snapshot.directories = [entry for entry in plan.entries.values() if entry.directory]
        for entry in sorted(snapshot.directories, key=lambda e: e.path.count("/")):
            (snapshot.root / entry.path).mkdir(mode=0o700, exist_ok=True)
        overrides = {f"{plan.config_paths['DATA_DIR']}/{name}": content for name, content in payloads.items()}
        for name, content in overrides.items():
            path = snapshot.root / name
            path.write_bytes(content)
            path.chmod(0o600)
            plan.entries[name] = backup.Entry(name, path, path.lstat())
        for entry in list(plan.entries.values()):
            if entry.directory or entry.path in overrides:
                continue
            backup._no_links(entry.source)
            fd = os.open(entry.source, os.O_RDONLY | os.O_NOFOLLOW)
            keep_fd = False
            try:
                current = os.fstat(fd)
                if (not stat.S_ISREG(current.st_mode) or
                        (current.st_dev, current.st_ino) != (entry.info.st_dev, entry.info.st_ino)):
                    raise SnapshotChanged(f"捕获文件时来源被替换，请重试: {entry.path}")
                if os.pread(fd, 16, 0) == b"SQLite format 3\x00":
                    snapshot.databases.append(entry)
                    continue
                target = snapshot.root / entry.path
                try:
                    os.link(entry.source, target, follow_symlinks=False)
                    linked = target.lstat()
                    if (linked.st_dev, linked.st_ino) != (current.st_dev, current.st_ino):
                        target.unlink()
                        raise FileNotFoundError("source replaced while pinning")
                    snapshot.files.append(PinnedFile(entry, path=target))
                except OSError as exc:
                    if exc.errno not in {errno.EXDEV, errno.EPERM, errno.EACCES, errno.EOPNOTSUPP, errno.ENOENT, None}:
                        raise
                    # 外部目录可能跨文件系统；打开的描述符同样能保留原图，即使稍后被删。
                    snapshot.files.append(PinnedFile(entry, fd=fd))
                    keep_fd = True
            finally:
                if not keep_fd:
                    os.close(fd)
        # SQLite 在线备份已包含 WAL 中已提交的数据，恢复时不能再带入旧 WAL/SHM/journal。
        sidecars = {entry.path + suffix for entry in snapshot.databases for suffix in ("-wal", "-shm", "-journal")}
        for item in list(snapshot.files):
            if item.entry.path in sidecars:
                if item.fd is not None:
                    os.close(item.fd)
                if item.path is not None:
                    item.path.unlink()
                snapshot.files.remove(item)
                plan.entries.pop(item.entry.path)
        return snapshot
    except BaseException:
        snapshot.close()
        raise


def _copy_stable(item: PinnedFile, target: Path) -> None:
    """把固定的原 inode 脱离成独立副本；变动时重试，永不修改硬链接指向的原文件。"""
    owned_fd = item.fd is None
    fd = os.open(item.path, os.O_RDONLY | os.O_NOFOLLOW) if owned_fd else item.fd
    temporary = None
    try:
        temp_fd, temporary = tempfile.mkstemp(prefix=".copy-", dir=target.parent)
        with os.fdopen(temp_fd, "wb") as destination:
            for attempt in range(COPY_ATTEMPTS):
                before = os.fstat(fd)
                os.lseek(fd, 0, os.SEEK_SET)
                destination.seek(0)
                destination.truncate()
                remaining = before.st_size
                while remaining:
                    chunk = os.read(fd, min(backup.CHUNK, remaining))
                    if not chunk:
                        break
                    destination.write(chunk)
                    remaining -= len(chunk)
                after = os.fstat(fd)
                if not remaining and backup._stamp(before) == backup._stamp(after):
                    break
                if attempt == COPY_ATTEMPTS - 1:
                    raise SnapshotChanged(f"文件持续变化，未生成备份；稍后重试或停止该服务: {item.entry.path}")
                time.sleep(0.05)
            destination.flush()
        os.replace(temporary, target)
    finally:
        try:
            if temporary is not None:
                Path(temporary).unlink(missing_ok=True)
        finally:
            if owned_fd:
                os.close(fd)


def _copy_database(entry: backup.Entry, target: Path) -> None:
    """SQLite backup API 读取一致的已提交事务视图，不直接拷贝正在写入的数据库文件。"""
    backup._no_links(entry.source)
    current = entry.source.lstat()
    if (current.st_dev, current.st_ino) != (entry.info.st_dev, entry.info.st_ino):
        raise SnapshotChanged(f"数据库在快照期间被替换: {entry.path}")
    deadline = time.monotonic() + SQLITE_TIMEOUT

    def progress(status, remaining, total):
        if time.monotonic() > deadline:
            raise SnapshotChanged(f"数据库持续忙碌，在线快照超时: {entry.path}")

    source = sqlite3.connect(entry.source.as_uri() + "?mode=ro", uri=True, timeout=1.0)
    try:
        destination = sqlite3.connect(target)
        try:
            source.backup(destination, pages=256, progress=progress, sleep=0.05)
        finally:
            destination.close()
    finally:
        source.close()


def _finish(snapshot: Snapshot, output: Path) -> dict:
    """只在工作线程执行。清理也由线程负责，调用方取消不会删除尚在使用的快照。"""
    try:
        for item in snapshot.files:
            target = snapshot.root / item.entry.path
            _copy_stable(item, target)
        for entry in snapshot.databases:
            _copy_database(entry, snapshot.root / entry.path)
        originals = [item.entry for item in snapshot.files] + snapshot.databases + snapshot.directories
        for entry in sorted(originals, key=lambda e: e.path.count("/"), reverse=True):
            path = snapshot.root / entry.path
            metadata = backup._metadata(entry)
            # 至此文件已脱离原 inode，才能恢复权限/时间，避免改动真实数据。
            path.chmod(metadata["mode"])
            os.utime(path, ns=(metadata["mtime_ns"], metadata["mtime_ns"]))
            snapshot.plan.entries[entry.path] = backup.Entry(entry.path, path, path.lstat())
        return backup._write_plan(snapshot.plan, output, snapshot={
            "mode": "live", "captured_at": snapshot.captured_at,
            "sqlite_databases": [entry.path for entry in snapshot.databases],
        })
    finally:
        try:
            snapshot.close()
        finally:
            _job_lock.release()


async def export_running(root: Path, output: Path | None = None) -> tuple[Path, dict]:
    """在 bot 主事件循环内调用；整个 ZIP 完成后返回，其他消息可继续处理。"""
    if not _job_lock.acquire(blocking=False):
        raise BackupBusy("已有导出任务在运行，请等它完成")
    snapshot = None
    delegated = False
    try:
        root = root.resolve()
        owns_lock = getattr(maintenance, "held_by_this_process", lambda root: False)
        if not owns_lock(root):
            raise backup.BackupError("当前进程未持有 bot 维护锁；请先重启到新版 bot，再使用 /backup")
        output = backup._absolute(root, str(output)) if output else backup.default_output(root)
        for attempt in range(COPY_ATTEMPTS):
            try:
                snapshot = _capture(root, output)
                break
            except (FileNotFoundError, SnapshotChanged):
                if attempt == COPY_ATTEMPTS - 1:
                    raise
                await asyncio.sleep(0.05)
        future = asyncio.get_running_loop().run_in_executor(None, _finish, snapshot, output)
        delegated = True
        # 消息处理任务取消时仍由工作线程完成归档/清理，并一直持有任务锁。
        future.add_done_callback(lambda done: None if done.cancelled() else done.exception())
        manifest = await asyncio.shield(future)
        return output, manifest
    finally:
        if not delegated:
            try:
                if snapshot is not None:
                    snapshot.close()
            finally:
                _job_lock.release()
