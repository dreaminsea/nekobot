"""可迁移的完整状态备份；离线入口与进程内快照共用归档和恢复格式。"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from datetime import datetime, timezone
import fnmatch
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import posixpath
import shutil
import stat
import subprocess
import tempfile
import uuid
import zipfile

from core.maintenance import LOCK_NAME, acquire_lock

FORMAT = "nekobot-backup"
VERSION = 1
CHUNK = 1024 * 1024
MANIFEST_LIMIT = 32 * CHUNK
# 新增文件在第一次 commit 前也属于程序本身，不应被当作用户数据。
OWN_FILES = {"backup.py", "core/backup.py", "core/maintenance.py", "core/live_backup.py",
             "tests/test_backup.py", "tests/test_live_backup.py"}
SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", "node_modules", "backups",
             ".pytest_cache", ".mypy_cache", ".ruff_cache", ".cache"}
# 仓库根目录本身也可能被用作虚拟环境；只在存在 pyvenv.cfg 时排除这些项目。
VENV_ROOT_ITEMS = {"pyvenv.cfg", "bin", "lib", "lib64", "include", "Scripts", "Lib", "Include"}
CONFIG_FILES = {"config.py", "compose.yaml", "compose.yml", "compose.override.yaml",
                "compose.override.yml", "docker-compose.yaml", "docker-compose.yml",
                "docker-compose.override.yaml", "docker-compose.override.yml", ".env"}


# 覆盖 config.py 默认值的本地配置（core/local_config.py），DATA_DIR / STICKER_DIR 也可能写在这里。
LOCAL_CONFIG = "config.local.py"


class BackupError(ValueError):
    pass


def _name(value: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise BackupError(f"非法备份路径: {value!r}")
    parts = value.split("/")
    if any(p in {"", ".", ".."} or ":" in p for p in parts):
        raise BackupError(f"非法备份路径: {value!r}")
    if any(p in {".git", LOCK_NAME} or p.startswith(".nekobot-") for p in parts):
        raise BackupError(f"不能备份或恢复内部路径: {value}")
    if parts[0] == "backups":
        raise BackupError("backups/ 保留给备份和恢复前副本，不能作为数据目标")
    return value


def _config_file(name: str) -> bool:
    path = PurePosixPath(name)
    return (name in CONFIG_FILES or
            (len(path.parts) == 1 and (name.startswith(".env.") or name.startswith("config."))) or
            path.parts[0] in {"config", "configs"})


def _tracked(root: Path) -> set[str]:
    try:
        top = subprocess.run(["git", "-C", str(root), "rev-parse", "--show-toplevel"],
                             check=True, capture_output=True).stdout.decode().strip()
        if Path(top).resolve() != root:
            raise BackupError("--root 必须指向 nekobot 仓库根目录")
        result = subprocess.run(["git", "-C", str(root), "ls-files", "-z", "--cached"],
                                check=True, capture_output=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise BackupError("需要 Git 工作副本来区分源码和本地数据；请在克隆的 nekobot 仓库中运行") from exc
    return set(os.fsdecode(result.stdout).split("\0")) - {""}


def _protected(root: Path) -> set[str]:
    return {name for name in _tracked(root) if not _config_file(name)} | OWN_FILES


def _check_source(name: str, protected: set[str], *, tree: bool = False) -> None:
    _name(name)
    if any(name == p or name.startswith(p + "/") or (tree and p.startswith(name + "/"))
           for p in protected):
        raise BackupError(f"备份不能覆盖仓库源码: {name}")


def _parse(text: str, name: str) -> ast.Module:
    try:
        return ast.parse(text)
    except SyntaxError as exc:
        raise BackupError(f"{name} 语法错误，无法定位数据配置") from exc


def _assignments(text: str, name: str = "config.py") -> dict[str, ast.expr | None]:
    tree = _parse(text, name)
    found = {}
    for node in tree.body:
        targets = node.targets if isinstance(node, ast.Assign) else (
            [node.target] if isinstance(node, ast.AnnAssign) else [])
        for target in targets:
            if isinstance(target, ast.Name) and target.id in {"DATA_DIR", "STICKER_DIR"}:
                if target.id in found or node.value is None or len(targets) != 1:
                    raise BackupError(f"{name} 中 {target.id} 必须使用单独、唯一的顶层赋值")
                found[target.id] = node.value
    if name == LOCAL_CONFIG:
        # 本地文件可能在 if 等语句里赋值，静态无法确定取值：记为 None，需要显式指定目录。
        stores = [n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)]
        for key in ("DATA_DIR", "STICKER_DIR"):
            if stores.count(key) > (key in found):
                found[key] = None
    return found


def _uses_local(text: str) -> bool:
    """config.py 是否在末尾加载 config.local.py；旧版 config.py 不会读取本地文件。"""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return False
    return any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "load_local"
               for n in ast.walk(tree))


def _settings(root: Path, data_dir: str | None, sticker_dir: str | None) -> dict[str, str]:
    path = root / "config.py"
    _no_links(path)
    if not stat.S_ISREG(path.lstat().st_mode):
        raise BackupError("config.py 必须是普通文件")
    text = path.read_text(encoding="utf-8")
    sources = [("config.py", _assignments(text))]
    local = root / LOCAL_CONFIG
    if _uses_local(text) and os.path.lexists(local):
        _no_links(local)
        if not stat.S_ISREG(local.lstat().st_mode):
            raise BackupError(f"{LOCAL_CONFIG} 必须是普通文件")
        sources.insert(0, (LOCAL_CONFIG, _assignments(local.read_text(encoding="utf-8-sig"), LOCAL_CONFIG)))
    result = {}
    for key, override, default in (("DATA_DIR", data_dir, "data"),
                                    ("STICKER_DIR", sticker_dir, "stickers")):
        found = next(((name, nodes[key]) for name, nodes in sources if key in nodes), None)
        if override is not None:
            value = override
        elif found is None:
            value = default
        else:
            try:
                if found[1] is None:
                    raise ValueError
                value = ast.literal_eval(found[1])
            except (ValueError, TypeError) as exc:
                flag = "--data-dir" if key == "DATA_DIR" else "--sticker-dir"
                raise BackupError(f"{found[0]} 中 {key} 不是字符串常量，请用 {flag} 显式指定；"
                                  "备份不会执行配置代码") from exc
        if not isinstance(value, str) or not value:
            raise BackupError(f"{key} 必须是非空目录字符串")
        result[key] = value
    return result


def _absolute(root: Path, value: str) -> Path:
    # 不 resolve 符号链接；所有路径都必须在读写前经过 lstat 检查。
    return Path(os.path.abspath(root / value))


def _no_links(path: Path) -> None:
    for parent in reversed((path, *path.parents)):
        if parent.is_symlink():
            raise BackupError(f"不跟随符号链接，请使用真实数据目录: {parent}")


def _is_virtualenv(path: Path) -> bool:
    # 按环境标记识别任意名称的 venv，不能跟随目录或标记文件的软链接。
    try:
        return (stat.S_ISDIR(path.lstat().st_mode) and
                stat.S_ISREG((path / "pyvenv.cfg").lstat().st_mode))
    except (FileNotFoundError, NotADirectoryError):
        return False


def _stamp(s: os.stat_result) -> tuple:
    return s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns, s.st_mode


@dataclass
class Entry:
    path: str
    source: Path | None
    info: os.stat_result | None

    @property
    def directory(self) -> bool:
        return self.info is None or stat.S_ISDIR(self.info.st_mode)


@dataclass
class Plan:
    root: Path
    entries: dict[str, Entry] = field(default_factory=dict)
    replace_dirs: list[str] = field(default_factory=list)
    config_paths: dict[str, str] = field(default_factory=dict)
    sticker_paths: dict[str, str] = field(default_factory=dict)

    @property
    def size(self) -> int:
        return sum(e.info.st_size for e in self.entries.values() if not e.directory)


def collect(root: Path, *, output: Path | None = None, data_dir: str | None = None,
            sticker_dir: str | None = None, includes: tuple[str, ...] = (),
            sticker_records: dict | None = None) -> Plan:
    root = root.resolve()
    protected = _protected(root)
    settings = _settings(root, data_dir, sticker_dir)
    plan = Plan(root)
    data = _absolute(root, settings["DATA_DIR"])
    stickers = _absolute(data, settings["STICKER_DIR"])
    if data == root or data in root.parents or stickers == root or stickers in root.parents:
        raise BackupError("数据目录不能是仓库根目录或其父目录")
    def vacant(base: Path, preferred: str) -> str:
        name = preferred
        number = 1
        while os.path.lexists(base / name):
            number += 1
            name = f"{preferred}-{number}"
        return name

    # 外部 bot 数据可与仓库内现存的 data/napcat 共存；映射时不能混合两个数据根。
    data_name = data.relative_to(root).as_posix() if data.is_relative_to(root) else vacant(root, "data")
    if stickers == data:
        sticker_name = data_name
    elif stickers.is_relative_to(data):
        sticker_name = f"{data_name}/{stickers.relative_to(data).as_posix()}"
    elif stickers.is_relative_to(root):
        sticker_name = stickers.relative_to(root).as_posix()
    else:
        sticker_name = f"{data_name}/{vacant(data, 'stickers')}"
    for name in (data_name, sticker_name):
        _check_source(name, protected, tree=True)
    plan.config_paths = {"DATA_DIR": data_name, "STICKER_DIR": posixpath.relpath(sticker_name, data_name)}

    def add(source: Path | None, name: str, snapshot: os.stat_result | None = None) -> None:
        _check_source(name, protected)
        info = (snapshot or source.lstat()) if source is not None else None
        if info is not None and stat.S_ISLNK(info.st_mode):
            raise BackupError(f"不支持符号链接，请改用真实文件或目录: {source}")
        if info is not None and not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
            raise BackupError(f"不支持 socket、管道或设备等特殊文件: {source}")
        old = plan.entries.get(name)
        if old is not None:
            if old.source != source:
                raise BackupError(f"两个来源映射到同一路径: {name}")
            return
        plan.entries[name] = Entry(name, source, info)

    def walk(source: Path, name: str, *, full: bool) -> None:
        if output is not None and source == output:
            return
        if source.name == ".git":
            return
        if not full:
            if (source.name in SKIP_DIRS or source.name == LOCK_NAME or
                    source.name.startswith(".nekobot-") or name in OWN_FILES or
                    fnmatch.fnmatch(source.name, "nekobot-backup-*.zip") or
                    fnmatch.fnmatch(source.name, "*.py[co]") or source.name.endswith(".egg-info")):
                return
            if source in {data, stickers}:
                return
            if name in protected or _is_virtualenv(source):
                return
        if source.is_dir() and not source.is_symlink():
            # 含源码的父目录只是容器，不能整体替换；仍扫描其未跟踪的子项。
            snapshot = source.lstat()
            children = sorted(source.iterdir())
            before = len(plan.entries)
            for child in children:
                walk(child, f"{name}/{child.name}", full=full)
            if full or len(plan.entries) > before or not children:
                add(source, name, snapshot)
        else:
            add(source, name)

    root_is_virtualenv = _is_virtualenv(root)
    for child in sorted(root.iterdir()):
        if root_is_virtualenv and child.name in VENV_ROOT_ITEMS:
            continue
        walk(child, child.name, full=False)
    for source, name in ((data, data_name), (stickers, sticker_name)):
        _no_links(source)
        if source.exists():
            if not source.is_dir():
                raise BackupError(f"数据目录不是目录: {source}")
            walk(source, name, full=True)
        elif name not in plan.entries:
            add(None, name)
    for spec in includes:
        source_value, sep, destination = spec.rpartition("=")
        if not sep:
            raise BackupError("--include 格式为 源路径=备份中的相对目录")
        source = _absolute(root, source_value)
        _no_links(source)
        _check_source(destination, protected, tree=True)
        if not source.exists():
            raise BackupError(f"额外数据路径不存在: {source}")
        walk(source, destination, full=True)

    # 旧版表情索引可能指向 DATA_DIR/STICKER_DIR 以外的本地文件，也一并收集。
    index = data / "stickers.json"
    if sticker_records is not None or index.is_file():
        records = sticker_records if sticker_records is not None else json.loads(index.read_text(encoding="utf-8"))
        if not isinstance(records, dict):
            raise BackupError("stickers.json 应为对象")
        by_source = {e.source: e.path for e in plan.entries.values() if not e.directory}
        for key, item in records.items():
            value = item.get("path") if isinstance(item, dict) else None
            if not isinstance(value, str) or not value:
                continue
            source = _absolute(root, value)
            if not source.is_file():
                source = stickers / Path(value).name
            _no_links(source)
            if not source.is_file():
                raise BackupError(f"表情索引引用的图片不存在: {value}")
            existing = by_source.get(source)
            if existing is None:
                digest = hashlib.sha256(str(source).encode()).hexdigest()[:16]
                existing = f"{sticker_name}/imported/{digest}{source.suffix}"
                add(source, existing)
                by_source[source] = existing
            plan.sticker_paths[key] = existing

    # 补齐映射/导入文件的父目录。完整的本地数据子树恢复时整体替换，避免混入旧状态。
    for name in list(plan.entries):
        for parent in PurePosixPath(name).parents:
            if str(parent) != "." and str(parent) not in plan.entries:
                add(None, str(parent))
    for name, entry in sorted(plan.entries.items(), key=lambda pair: (pair[0].count("/"), pair[0])):
        if entry.directory and not any(p == name or p.startswith(name + "/") for p in protected):
            if not any(name.startswith(p + "/") for p in plan.replace_dirs):
                plan.replace_dirs.append(name)
    return plan


def _metadata(entry: Entry) -> dict:
    info = entry.info
    return {"path": entry.path, "kind": "directory" if entry.directory else "file",
            "mode": stat.S_IMODE(info.st_mode) & 0o777 if info else 0o700,
            "mtime_ns": info.st_mtime_ns if info else 0}


def _unchanged(entry: Entry) -> None:
    if entry.source is not None and _stamp(entry.source.lstat()) != _stamp(entry.info):
        raise BackupError(f"备份期间文件发生变化，请停止 bot/NapCat 后重试: {entry.path}")


def _check_output(plan: Plan, output: Path) -> None:
    if output.exists():
        raise BackupError(f"备份文件已存在，不会覆盖: {output}")
    if any(e.directory and e.source and output.is_relative_to(e.source) for e in plan.entries.values()):
        raise BackupError("备份输出不能放在待备份的数据目录中，请使用 backups/ 或仓库外目录")
    _no_links(output)


def _write_plan(plan: Plan, output: Path, *, validate=None, snapshot: dict | None = None) -> dict:
    """将稳定的 Plan 写成原子 ZIP；调用者负责进程锁或进程内快照的生命周期。"""
    _check_output(plan, output)
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".nekobot-backup-", dir=output.parent)
    manifest = {"format": FORMAT, "version": VERSION,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "replace_dirs": plan.replace_dirs, "config_paths": plan.config_paths,
                "sticker_paths": plan.sticker_paths, "entries": []}
    if snapshot is not None:
        manifest["snapshot"] = snapshot
    try:
        with os.fdopen(fd, "w+b") as stream:
            with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
                for entry in sorted(plan.entries.values(), key=lambda e: e.path):
                    _unchanged(entry)
                    meta = _metadata(entry)
                    if not entry.directory:
                        digest = hashlib.sha256()
                        size = 0
                        source_fd = os.open(entry.source, os.O_RDONLY | os.O_NOFOLLOW)
                        with os.fdopen(source_fd, "rb") as source, archive.open(
                                "files/" + entry.path, "w", force_zip64=True) as target:
                            if _stamp(os.fstat(source.fileno())) != _stamp(entry.info):
                                raise BackupError(f"备份期间文件发生变化: {entry.path}")
                            while chunk := source.read(CHUNK):
                                target.write(chunk)
                                digest.update(chunk)
                                size += len(chunk)
                        _unchanged(entry)
                        meta.update(size=size, sha256=digest.hexdigest())
                    manifest["entries"].append(meta)
                for entry in plan.entries.values():
                    _unchanged(entry)
                if validate is not None:
                    validate()
                archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False))
            stream.flush()
            os.fsync(stream.fileno())
        # link 的目标必须不存在，避免检查后另一进程创建文件而被覆盖。
        os.link(temporary, output)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return manifest


def _validate_plan(plan: Plan, output: Path, options: dict) -> None:
    root = plan.root
    # 根目录新增/删除本地文件也要发现，输出和临时文件不会进入清单。
    final = collect(root, output=output, **options)
    if (set(final.entries) != set(plan.entries) or
            final.config_paths != plan.config_paths or final.sticker_paths != plan.sticker_paths):
        raise BackupError("备份期间数据清单发生变化，请停止相关服务后重试")
    for name, entry in plan.entries.items():
        current = final.entries[name]
        if ((entry.info is None) != (current.info is None) or
                (entry.info is not None and _stamp(entry.info) != _stamp(current.info))):
            raise BackupError(f"备份期间文件发生变化: {name}")


def export_backup(root: Path, output: Path, **options) -> dict:
    root = root.resolve()
    output = _absolute(root, str(output))
    with acquire_lock(root):
        plan = collect(root, output=output, **options)
        return _write_plan(plan, output, validate=lambda: _validate_plan(plan, output, options))


def _read_manifest(archive: zipfile.ZipFile) -> dict:
    names = archive.namelist()
    if len(names) != len(set(names)):
        raise BackupError("备份含有重复 ZIP 条目")
    try:
        info = archive.getinfo("manifest.json")
    except KeyError as exc:
        raise BackupError("不是 nekobot 备份：缺少 manifest.json") from exc
    if info.file_size > MANIFEST_LIMIT:
        raise BackupError("备份清单过大")
    manifest = json.loads(archive.read(info))
    if (not isinstance(manifest, dict) or manifest.get("format") != FORMAT or
            type(manifest.get("version")) is not int or manifest["version"] != VERSION):
        raise BackupError("不支持的备份格式或版本")
    entries = manifest.get("entries")
    if not isinstance(entries, list) or not entries:
        raise BackupError("备份条目清单无效")
    indexed = {}
    expected = {"manifest.json"}
    for entry in entries:
        if not isinstance(entry, dict):
            raise BackupError("备份条目无效")
        name = _name(entry.get("path"))
        if name in indexed or not isinstance(entry.get("kind"), str) or entry["kind"] not in {"file", "directory"}:
            raise BackupError(f"重复或无效的条目: {name}")
        for key, limit in (("mode", 0o777), ("mtime_ns", 2**63 - 1)):
            if type(entry.get(key)) is not int or not 0 <= entry[key] <= limit:
                raise BackupError(f"无效的文件属性: {name}")
        indexed[name] = entry
        if entry["kind"] == "file":
            member = "files/" + name
            expected.add(member)
            if type(entry.get("size")) is not int or entry["size"] < 0:
                raise BackupError(f"无效的文件大小: {name}")
            digest = entry.get("sha256")
            if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
                raise BackupError(f"无效的 SHA-256: {name}")
            try:
                info = archive.getinfo(member)
            except KeyError as exc:
                raise BackupError(f"缺少文件: {name}") from exc
            filetype = stat.S_IFMT(info.external_attr >> 16)
            if info.file_size != entry["size"] or filetype not in {0, stat.S_IFREG}:
                raise BackupError(f"文件类型/大小不匹配: {name}")
    if set(names) != expected:
        raise BackupError("ZIP 内容与清单不一致")
    for name in indexed:
        for parent in PurePosixPath(name).parents:
            if str(parent) != "." and indexed.get(str(parent), {}).get("kind") != "directory":
                raise BackupError(f"文件父目录缺失或类型冲突: {name}")
    directories = manifest.get("replace_dirs")
    if not isinstance(directories, list) or len(directories) != len(set(map(str, directories))):
        raise BackupError("目录替换清单无效")
    for name in directories:
        _name(name)
        if indexed.get(name, {}).get("kind") != "directory" or any(
                name.startswith(other + "/") for other in directories if isinstance(other, str) and other != name):
            raise BackupError("目录替换清单重叠或缺失")
    settings = manifest.get("config_paths")
    if not isinstance(settings, dict) or set(settings) != {"DATA_DIR", "STICKER_DIR"}:
        raise BackupError("备份缺少数据目录配置")
    data_name = _name(settings["DATA_DIR"])
    sticker_value = settings["STICKER_DIR"]
    if not isinstance(sticker_value, str) or not sticker_value or "\\" in sticker_value:
        raise BackupError("表情目录配置无效")
    sticker_name = _name(posixpath.normpath(posixpath.join(data_name, sticker_value)))
    if (indexed.get(data_name, {}).get("kind") != "directory" or
            indexed.get(sticker_name, {}).get("kind") != "directory" or
            indexed.get("config.py", {}).get("kind") != "file"):
        raise BackupError("备份缺少配置或数据目录")
    paths = manifest.get("sticker_paths")
    if not isinstance(paths, dict):
        raise BackupError("表情路径清单无效")
    for name in paths.values():
        if indexed.get(_name(name), {}).get("kind") != "file":
            raise BackupError("表情图片未包含在备份中")
    return manifest


def _unpack(archive: zipfile.ZipFile, manifest: dict, stage: Path | None = None) -> None:
    for entry in sorted(manifest["entries"], key=lambda e: (e["path"].count("/"), e["path"])):
        path = stage / entry["path"] if stage is not None else None
        if entry["kind"] == "directory":
            if path is not None:
                path.mkdir(mode=0o700, exist_ok=True)
            continue
        digest = hashlib.sha256()
        size = 0
        with archive.open("files/" + entry["path"]) as source:
            target = path.open("xb") if path is not None else None
            try:
                while chunk := source.read(CHUNK):
                    size += len(chunk)
                    if size > entry["size"]:
                        raise BackupError(f"文件大小不匹配: {entry['path']}")
                    digest.update(chunk)
                    if target is not None:
                        target.write(chunk)
            finally:
                if target is not None:
                    target.close()
        if size != entry["size"] or digest.hexdigest() != entry["sha256"]:
            raise BackupError(f"文件校验失败: {entry['path']}")


def inspect_backup(path: Path) -> dict:
    with zipfile.ZipFile(path) as archive:
        manifest = _read_manifest(archive)
        _unpack(archive, manifest)
        return manifest


def _relocate(stage: Path, root: Path, manifest: dict) -> None:
    config = stage / "config.py"
    local = stage / LOCAL_CONFIG
    # 恢复后的 config.py 会读取 config.local.py 时，数据目录以本地文件为准，改写也写进本地文件，
    # 保持随代码更新的 config.py 不变；本地文件不在备份里时（不会被安装）仍写进 config.py。
    use_local = (_uses_local(config.read_text(encoding="utf-8")) and
                 any(e["path"] == LOCAL_CONFIG and e["kind"] == "file" for e in manifest["entries"]))
    nodes = {path: _assignments(path.read_text(encoding="utf-8-sig" if path == local else "utf-8"), path.name)
             for path in ((local, config) if use_local else (config,))}
    target = local if use_local else config
    edits = []
    append = []
    for key, value in manifest["config_paths"].items():
        path = next((p for p in nodes if key in nodes[p]), None)
        if path is None:
            append.append(f"\n{key} = {value!r}\n")
            continue
        node = nodes[path][key]
        # 相对路径保持原始格式/注释；绝对路径或动态配置才需要改写。
        try:
            unchanged = node is not None and ast.literal_eval(node) == value
        except (ValueError, TypeError):
            unchanged = False
        if unchanged:
            continue
        if path == target and node is not None:
            edits.append((node, repr(value).encode("utf-8")))
        else:  # 默认值在 config.py，或本地写法无法改写：在目标文件末尾重新赋值覆盖
            append.append(f"\n{key} = {value!r}\n")
    content = target.read_bytes().removeprefix(b"\xef\xbb\xbf")  # 列偏移按去掉 BOM 后的文本计算
    lines = content.splitlines(keepends=True)
    spans = []
    for node, value in edits:
        start = sum(map(len, lines[:node.lineno - 1])) + node.col_offset
        end = sum(map(len, lines[:node.end_lineno - 1])) + node.end_col_offset
        spans.append((start, end, value))
    for start, end, value in sorted(spans, reverse=True):
        content = content[:start] + value + content[end:]
    target.write_bytes(content + "".join(append).encode("utf-8"))
    index = stage / manifest["config_paths"]["DATA_DIR"] / "stickers.json"
    if manifest["sticker_paths"]:
        records = json.loads(index.read_text(encoding="utf-8"))
        if not isinstance(records, dict):
            raise BackupError("stickers.json 应为对象")
        for key, value in manifest["sticker_paths"].items():
            if key not in records or not isinstance(records[key], dict):
                raise BackupError("表情路径清单与索引不一致")
            records[key]["path"] = str(root / value)
        index.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")


def _remove(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def restore_backup(root: Path, archive_path: Path, *, dry_run: bool = False) -> dict:
    root = root.resolve()
    with acquire_lock(root), zipfile.ZipFile(archive_path) as archive:
        manifest = _read_manifest(archive)
        protected = _protected(root)
        directories = manifest["replace_dirs"]
        entries = {e["path"]: e for e in manifest["entries"]}
        operations = sorted(directories + [e["path"] for e in entries.values()
                            if e["kind"] == "file" and not any(e["path"].startswith(d + "/") for d in directories)])
        for name, entry in entries.items():
            _check_source(name, protected)
            _no_links(root / name)
            target = root / name
            if target.exists() and target.is_dir() != (entry["kind"] == "directory"):
                raise BackupError(f"目标存在文件/目录类型冲突: {name}")
            if target.exists() and not (target.is_dir() or target.is_file()):
                raise BackupError(f"目标不是普通文件或目录: {name}")
        for name in directories:
            _check_source(name, protected, tree=True)
        archive_path = archive_path.resolve()
        if any(archive_path == root / name or archive_path.is_relative_to(root / name) for name in operations):
            raise BackupError("请把待恢复的 ZIP 移到 backups/ 或仓库外，避免替换其所在数据目录")
        result = {"manifest": manifest, "paths": operations, "recovery": None}
        if dry_run:
            _unpack(archive, manifest)
            return result
        size = sum(e.get("size", 0) for e in entries.values())
        if size > shutil.disk_usage(root).free:
            raise BackupError("磁盘空间不足以暂存并校验完整备份")
        with tempfile.TemporaryDirectory(prefix=".nekobot-restore-", dir=root) as temp:
            stage = Path(temp)
            _unpack(archive, manifest, stage)
            _relocate(stage, root, manifest)
            for entry in sorted(entries.values(), key=lambda e: e["path"].count("/"), reverse=True):
                path = stage / entry["path"]
                os.chmod(path, entry["mode"])
                os.utime(path, ns=(entry["mtime_ns"], entry["mtime_ns"]))
            backups = root / "backups"
            _no_links(backups)
            backups.mkdir(mode=0o700, exist_ok=True)
            recovery = Path(tempfile.mkdtemp(prefix="before-restore-", dir=backups))
            original = recovery / "original"
            original.mkdir(mode=0o700)
            journal = {"format": "nekobot-restore-recovery", "paths": operations,
                       "created": [name for name in operations if not (root / name).exists()],
                       "complete": False}
            journal_path = recovery / "recovery.json"
            journal_path.write_text(json.dumps(journal, ensure_ascii=False, indent=2), encoding="utf-8")
            changed = []
            created_parents = []
            try:
                for name in operations:
                    target = root / name
                    _no_links(target)
                    for parent in reversed(target.parents):
                        if parent.is_relative_to(root) and not parent.exists():
                            parent.mkdir(mode=0o700)
                            created_parents.append(parent)
                    old = original / name
                    state = [target, old, False, False]
                    changed.append(state)
                    if target.exists():
                        old.parent.mkdir(parents=True, exist_ok=True)
                        os.replace(target, old)
                        state[2] = True
                    os.replace(stage / name, target)
                    state[3] = True
                journal["complete"] = True
                journal_path.write_text(json.dumps(journal, ensure_ascii=False, indent=2), encoding="utf-8")
            except BaseException:
                for target, old, moved, installed in reversed(changed):
                    if installed:
                        _remove(target)
                    if moved:
                        os.replace(old, target)
                for parent in reversed(created_parents):
                    parent.rmdir()
                raise
            result["recovery"] = str(recovery)
            return result


def default_output(root: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return root / "backups" / f"nekobot-backup-{stamp}-{uuid.uuid4().hex[:8]}.zip"
