"""读取密钥等不进 Git 的本地配置 —— 项目根目录的 `.env` + 进程环境变量。

格式与常见 dotenv 一致：`KEY=VALUE`，`#` 开头为注释，可选 `export ` 前缀，值可用单/双引号包起来；
未加引号的值里，空白后的 ` #` 视为行内注释。进程环境变量优先于 `.env`，方便 systemd / Docker 注入。

只解析成字典，不写回 `os.environ`：bot 会执行 NapCat 重启等外部命令，避免把密钥继承给子进程。
仅依赖标准库。
"""

import os
import re
from pathlib import Path

ENV_FILE = Path(__file__).resolve().parent.parent / ".env"

_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_INLINE_COMMENT = re.compile(r"\s+#.*$")


def parse(text: str, source: str = ".env") -> dict[str, str]:
    values: dict[str, str] = {}
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not _KEY.fullmatch(key):
            # 不回显整行，避免把写错格式的密钥打进日志。
            raise ValueError(f"{source} 第 {lineno} 行格式错误，应为 KEY=VALUE")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        else:
            value = _INLINE_COMMENT.sub("", value)
        values[key] = value
    return values


def _load() -> dict[str, str]:
    try:
        text = ENV_FILE.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return {}
    return parse(text, str(ENV_FILE))


_values = _load()


def env(key: str, default: str = "") -> str:
    """环境变量 > .env > default。显式写成空值（`KEY=`）也算已配置，返回空字符串。"""
    if key in os.environ:
        return os.environ[key]
    return _values.get(key, default)


def env_list(key: str, default: list[str] | None = None) -> list[str]:
    """逗号分隔的列表，如 `ADMIN_QQ=123,456`；未配置时返回 default。"""
    if key not in os.environ and key not in _values:
        return list(default or [])
    return [item.strip() for item in env(key).split(",") if item.strip()]
