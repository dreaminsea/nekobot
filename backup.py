#!/usr/bin/env python3
"""完整状态导出/恢复入口：python backup.py --help（仅需 Python 标准库和 Git）。"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
import zipfile

from core.backup import collect, default_output, export_backup, inspect_backup, restore_backup
from core.maintenance import acquire_lock


def _summary(manifest: dict) -> None:
    files = [e for e in manifest["entries"] if e["kind"] == "file"]
    print(f"文件数: {len(files)}；原始大小: {sum(e['size'] for e in files):,} 字节")
    print("数据目录: " + manifest["config_paths"]["DATA_DIR"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="导出/检查/恢复 nekobot 的配置与全部本地数据")
    sub = parser.add_subparsers(dest="command", required=True)
    export = sub.add_parser("export", help="离线导出 ZIP；运行中请主人私聊 /backup")
    export.add_argument("output", nargs="?", type=Path, help="输出 ZIP，默认放在仓库 backups/ 下")
    export.add_argument("--dry-run", action="store_true", help="只列出将要备份的文件")
    export.add_argument("--data-dir", help="DATA_DIR 为动态表达式时显式指定实际目录")
    export.add_argument("--sticker-dir", help="STICKER_DIR 为动态表达式时显式指定实际目录")
    export.add_argument("--include", action="append", default=[], metavar="SOURCE=RELATIVE_PATH",
                        help="额外收集外部数据文件/目录，可重复使用")
    inspect = sub.add_parser("inspect", help="完整校验 ZIP 并显示概要，不执行配置")
    inspect.add_argument("archive", type=Path)
    inspect.add_argument("--list", action="store_true", help="同时列出文件路径")
    restore = sub.add_parser("restore", help="校验后恢复，覆盖前自动保存原数据；先停止相关服务")
    restore.add_argument("archive", type=Path)
    restore.add_argument("--dry-run", action="store_true", help="校验备份并预览将替换的路径")
    for command in (export, restore):
        command.add_argument("--root", type=Path, default=Path(__file__).resolve().parent,
                             help="nekobot Git 仓库根目录，默认为脚本所在仓库")
    args = parser.parse_args(argv)
    try:
        if args.command == "export":
            root = args.root.resolve()
            output = args.output.absolute() if args.output else default_output(root)
            options = dict(data_dir=args.data_dir, sticker_dir=args.sticker_dir, includes=tuple(args.include))
            if args.dry_run:
                with acquire_lock(root):
                    plan = collect(root, output=output, **options)
                print(f"将导出 {len(plan.entries)} 个文件/目录，共 {plan.size:,} 字节到 {output}")
                for name, entry in sorted(plan.entries.items()):
                    print(name + ("/" if entry.directory else ""))
            else:
                manifest = export_backup(root, output, **options)
                print(f"已导出: {output}")
                _summary(manifest)
                print("备份包含密钥、聊天和登录数据，文件权限为 0600；请妥善保管。")
        elif args.command == "inspect":
            manifest = inspect_backup(args.archive)
            print("备份格式及全部文件 SHA-256 校验通过。")
            _summary(manifest)
            if args.list:
                for entry in manifest["entries"]:
                    print(entry["path"] + ("/" if entry["kind"] == "directory" else ""))
        else:
            result = restore_backup(args.root, args.archive, dry_run=args.dry_run)
            _summary(result["manifest"])
            if args.dry_run:
                print("校验通过，将替换以下路径（数据目录整体替换）：")
                for name in result["paths"]:
                    print(name)
            else:
                print(f"已恢复到: {args.root.resolve()}")
                print(f"恢复前副本: {result['recovery']}")
                print("请核对连接地址/端口等机器配置后重新启动服务。")
        return 0
    except (OSError, ValueError, RuntimeError, zipfile.BadZipFile, NotImplementedError) as exc:
        print(f"备份操作失败: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("操作已取消。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
