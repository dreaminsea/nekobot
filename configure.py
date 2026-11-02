#!/usr/bin/env python3
"""命令行配置工具：python3 configure.py（仅需 Python 标准库；--help 查看子命令）。

- `.env`：逐项填写密钥与账号。按行更新，注释、顺序和其他键原样保留；不存在时以 `.env.example`
  为模板创建，权限 0600。取值与引号规则和 core/env.py 一致。
- 行为配置：`config.py` 只放随代码更新的默认值，改动写进不进 Git 的 `config.local.py`（只写改过的项），
  这样 /update 拉取代码不会冲突。两个文件都用 AST 读写，不会执行；改值时只替换值本身，注释保持不变。
  f-string、多行提示词等不是常量的配置可以先复制默认写法到 config.local.py，再用编辑器修改。
- 写入前重新解析校验，再经临时文件原子替换。改完需 /restart 或重启 bot 才生效。
"""

from __future__ import annotations

import argparse
import ast
from dataclasses import dataclass
import getpass
import json
import math
import operator
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parent
RESTART_HINT = "改动需 /restart 或重启 bot 才生效（/reload 不会重读配置）。"
KEEP = object()  # 交互输入时"回车保留当前值"
# core/env.py 用 str.splitlines() 分行，这些字符都会断行，值里不能出现。
_LINE_BREAKS = "\n\r\x0b\x0c\x1c\x1d\x1e\x85\N{LINE SEPARATOR}\N{PARAGRAPH SEPARATOR}"
_QQ = re.compile(r"[0-9]+")
_DEEPSEEK_URLS = ("https://api.deepseek.com", "https://api.deepseek.com/v1")


def atomic_write(path: Path, data: bytes, mode: int) -> None:
    """写临时文件后原子替换；跟随符号链接写到真实文件，尽量保留原属主。"""
    path = path.resolve()
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        try:
            st = path.stat()
            os.chown(tmp, st.st_uid, st.st_gid)
        except (OSError, AttributeError):
            pass
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _short(text: str, limit: int = 60) -> str:
    """单行预览：只取第一行，过长截断。"""
    first = text.splitlines()[0] if text else ""
    return first if first == text and len(first) <= limit else first[:limit].rstrip() + " …"


# ============================================================
#  .env —— 密钥与账号
# ============================================================

@dataclass(frozen=True)
class EnvKey:
    name: str
    help: str
    secret: bool = False
    platform: str = ""   # 只有 PLATFORMS 含该平台时向导才询问（env --all 全部询问）
    kind: str = "text"   # text / qq（纯数字）/ list / qq_list（逗号分隔）


SEARCH_KEY = "DEEPSEEK_SEARCH_API_KEY"
ENV_KEYS = (
    EnvKey("LLM_API_KEY", "模型 API Key（必填）", secret=True),
    EnvKey(SEARCH_KEY, "联网搜索（只调用 DeepSeek 官方接口）", secret=True),
    EnvKey("NAPCAT_TOKEN", "NapCat WebSocket token，与 NapCat 里设置的一致；NapCat 没设 token 就留空",
           secret=True, platform="onebot"),
    EnvKey("FEISHU_APP_ID", "飞书应用 App ID（cli_ 开头）", platform="feishu"),
    EnvKey("FEISHU_APP_SECRET", "飞书应用 App Secret", secret=True, platform="feishu"),
    EnvKey("MASTER_NAME", "主人昵称，会写进人设提示词"),
    EnvKey("MASTER_QQ", "主人 QQ 号：主人命令、联系主人、重启通知都靠它", platform="onebot", kind="qq"),
    EnvKey("BOT_QQ", "bot 自己的 QQ 号：NapCat 重启命令和日志路径由它生成", platform="onebot", kind="qq"),
    EnvKey("ADMIN_QQ", "QQ 管理员，逗号分隔", platform="onebot", kind="qq_list"),
    EnvKey("MASTER_FEISHU", "主人飞书 open_id（ou_ 开头；私聊 bot 后看日志 [私聊] (ou_xxx)）", platform="feishu"),
    EnvKey("ADMIN_FEISHU", "飞书管理员 open_id，逗号分隔", platform="feishu", kind="list"),
    EnvKey("AMBIENT_GROUPS", "允许潜水接话、学习表情包的群号，逗号分隔；留空 = 不在任何群潜水", kind="list"),
)
ENV_BY_NAME = {k.name: k for k in ENV_KEYS}

_ENV_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_ENV_INLINE_COMMENT = re.compile(r"\s+#.*$")


def _split_env_line(raw: str) -> tuple[str, str] | None:
    """按 core/env.py 的规则拆一行：返回 (键, 未处理的值)；空行/注释返回 None，格式错误抛 ValueError。"""
    line = raw.strip()
    if not line or line.startswith("#"):
        return None
    if line.startswith("export "):
        line = line[len("export "):].lstrip()
    key, sep, value = line.partition("=")
    key = key.strip()
    if not sep or not _ENV_KEY.fullmatch(key):
        raise ValueError
    return key, value


def _quoted(value: str) -> bool:
    return len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'"


def _env_decode(value: str) -> str:
    value = value.strip()
    return value[1:-1] if _quoted(value) else _ENV_INLINE_COMMENT.sub("", value)


def parse_env(text: str, name: str = ".env") -> dict[str, str]:
    values = {}
    for lineno, raw in enumerate(text.splitlines(), 1):
        try:
            item = _split_env_line(raw)
        except ValueError:
            # 不回显整行，避免把写错格式的密钥打到终端。
            raise ValueError(f"{name} 第 {lineno} 行格式错误，应为 KEY=VALUE；请先手动修正") from None
        if item:
            values[item[0]] = _env_decode(item[1])
    return values


def env_quote(value: str) -> str:
    """写成 core/env.py 能原样读回的形式；必要时加双引号（该格式不处理转义，引号内原样保留）。"""
    if any(ch in value for ch in _LINE_BREAKS):
        raise ValueError("值不能包含换行")
    plain = value == value.strip() and not re.search(r"\s#", value) and not _quoted(value)
    return value if plain else f'"{value}"'


def mask(value: str) -> str:
    if not value:
        return "（空）"
    return f"{value[:3]}****{value[-4:]}" if len(value) >= 16 else "****"


def env_display(name: str, value: str | None) -> str:
    if name == SEARCH_KEY:
        if value is None:
            return "未设置（自动：LLM_BASE_URL 是 DeepSeek 官方地址时复用 LLM_API_KEY）"
        if value == "":
            return "（空 = 关闭联网搜索）"
    if value is None:
        return "未设置"
    if value == "":
        return "（空）"
    key = ENV_BY_NAME.get(name)
    return mask(value) if key is None or key.secret else value  # 不认识的自定义键一律打码


def normalize_env_value(key: EnvKey, value: str) -> str:
    value = value.strip()
    if any(ch in value for ch in _LINE_BREAKS):
        raise ValueError("值不能包含换行")
    if key.kind in ("list", "qq_list"):
        items = [item.strip() for item in re.split(r"[,，]", value) if item.strip()]
        if key.kind == "qq_list" and not all(_QQ.fullmatch(item) for item in items):
            raise ValueError("QQ 号应为纯数字，多个用逗号分隔")
        return ",".join(items)
    if key.kind == "qq" and value and not _QQ.fullmatch(value):
        raise ValueError("QQ 号应为纯数字")
    return value


class EnvFile:
    """按行编辑 .env：只改动涉及的键，注释、空行和其他键原样保留。"""

    def __init__(self, path: Path, template: Path | None = None) -> None:
        self.path = path
        self.exists = path.exists()
        source = path if self.exists else template
        text = source.read_text(encoding="utf-8-sig") if source is not None and source.exists() else ""
        self.newline = "\r\n" if "\r\n" in text else "\n"
        self.lines = text.splitlines()
        self.values = parse_env(text, source.name if source is not None else path.name)
        self.original = dict(self.values)

    @property
    def changed(self) -> bool:
        return self.values != self.original

    def get(self, key: str) -> str | None:
        return self.values.get(key)

    def _hits(self, key: str) -> list[int]:
        return [i for i, raw in enumerate(self.lines) if (item := _split_env_line(raw)) and item[0] == key]

    def set(self, key: str, value: str | None) -> None:
        """写入 KEY=value；value 为 None 时删除该键，原位置留下空的占位注释 `# KEY=`。"""
        if not _ENV_KEY.fullmatch(key):
            raise ValueError(f"不合法的键名：{key}")
        if self.values.get(key, KEEP) == (KEEP if value is None else value):
            return
        hits = self._hits(key)
        if value is None:
            self.lines[hits[-1]] = f"# {key}="
            self.values.pop(key)
        else:
            new = self._format(key, value, self.lines[hits[-1]] if hits else None)
            if hits:
                self.lines[hits[-1]:hits[-1] + 1] = new
            else:
                placeholder = re.compile(rf"\s*#\s*(export\s+)?{key}\s*=\s*")
                spots = [i for i, raw in enumerate(self.lines) if placeholder.fullmatch(raw)]
                if spots:  # 复用模板里注释掉的占位行，如 `# DEEPSEEK_SEARCH_API_KEY=`
                    self.lines[spots[-1]:spots[-1] + 1] = new
                else:
                    self.lines.extend(new)
            self.values[key] = value
        for i in reversed(hits[:-1]):  # 更早的重复赋值本来就被最后一行覆盖，删掉免得误导
            del self.lines[i]

    @staticmethod
    def _format(key: str, value: str, old: str | None) -> list[str]:
        prefix = comment = ""
        if old is not None:
            if old.strip().startswith("export "):
                prefix = "export "
            raw = _split_env_line(old)[1].strip()
            if not _quoted(raw) and (m := re.search(r"\s+(#.*)$", raw)):
                comment = m.group(1)
        line = f"{prefix}{key}={env_quote(value)}"
        if not comment:
            return [line]
        inline = f"{line}  {comment}"
        if _env_decode(_split_env_line(inline)[1]) == value:
            return [inline]
        return [comment, line]  # 加了引号或空值时行内注释会改变取值，挪到上一行

    def render(self) -> str:
        return self.newline.join(self.lines) + self.newline if self.lines else ""

    def save(self) -> None:
        text = self.render()
        if parse_env(text, self.path.name) != self.values:
            raise RuntimeError(".env 写入前校验失败，未保存")
        mode = stat.S_IMODE(self.path.stat().st_mode) if self.exists else 0o600
        atomic_write(self.path, text.encode("utf-8"), mode)
        self.exists = True
        self.original = dict(self.values)

    def diff(self) -> list[str]:
        names = list(dict.fromkeys([*ENV_BY_NAME, *self.original, *self.values]))
        return [f"  {name}: {env_display(name, self.original.get(name))} → {env_display(name, self.values.get(name))}"
                for name in names if self.original.get(name, KEEP) != self.values.get(name, KEEP)]


# ============================================================
#  行为配置 —— config.py（默认值）+ config.local.py（本地覆盖）
# ============================================================

LOCAL_NAME = "config.local.py"
LOCAL_HEADER = (
    "# 本地配置：覆盖 config.py 中的默认值，只写需要修改的项。此文件已在 .gitignore，/update 不会改动它。\n"
    "# 可以引用 config.py 里的默认值，例如 BOT_NAMES = BOT_NAMES + [\"小喵\"]；密钥和账号仍写在 .env。\n"
    "# 改完需 /restart。也可以运行 python3 configure.py config 交互修改。\n\n"
)

# 交互菜单默认展示的常用项；其余用 a 切换到全部列表。
COMMON = (
    "PLATFORMS", "NAPCAT_WS_URL", "FEISHU_DOMAIN", "LLM_BASE_URL", "LLM_MODEL", "LLM_THINKING",
    "LLM_REASONING_EFFORT", "GROUP_REASONING_EFFORT", "GROUP_TRIGGER", "BOT_NAMES",
    "AMBIENT_CHAT", "FOLLOWUP_CHAT", "STICKER_LEARN", "NAPCAT_WATCH",
)
LABELS = {
    "PLATFORMS": "同时启用的平台",
    "LLM_BASE_URL": "OpenAI 兼容接口地址",
    "LLM_MODEL": "主对话、接话判断、图片理解统一使用的模型",
    "GROUP_TRIGGER": "群聊触发前缀",
    "BOT_NAMES": "群友对 bot 的称呼，帮助判断是不是在叫它",
    "AMBIENT_CHAT": "群聊潜水接话总开关（群号在 .env 的 AMBIENT_GROUPS）",
    "FOLLOWUP_CHAT": "回话后一段时间内不用 @ 也能续聊",
    "STICKER_LEARN": "表情包自动学习",
    "NAPCAT_WATCH": "QQ 掉线监控（需同时启用 onebot 和 feishu）",
}
CHOICES = {
    "FEISHU_DOMAIN": ("feishu", "lark"),
    "FEISHU_LOG_LEVEL": ("DEBUG", "INFO", "WARNING", "ERROR"),
    "LLM_REASONING_EFFORT": ("low", "high", "max"),
    "GROUP_REASONING_EFFORT": ("low", "high", "max"),
    "IMAGE_DETAIL": ("auto", "low", "high", "original"),
}
URL_PREFIXES = {"NAPCAT_WS_URL": ("ws://", "wss://"), "LLM_BASE_URL": ("http://", "https://")}
KIND_HINTS = {
    "bool": "y = 开启，n = 关闭",
    "int": "整数，也可写 8 * 1024 * 1024 这样的算式",
    "float": "数字",
    "str": "直接输入文本，不用加引号；- 表示空字符串",
    "list": "逗号分隔，如 a, b；- 表示空列表",
}
_TRUE = {"y", "yes", "true", "1", "on", "是", "开", "开启"}
_FALSE = {"n", "no", "false", "0", "off", "否", "关", "关闭"}
_SETTING_NAME = re.compile(r"[A-Z][A-Z0-9_]*")
_RULE = re.compile(r"#\s*={10,}")
_NUMBER_OPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod, ast.Pow: operator.pow,
}


def _number(node: ast.expr) -> int | float | None:
    """数字常量或由它们组成的四则/乘方算式（如 8 * 1024 * 1024）；不是数字返回 None，越界抛 ValueError。"""
    if isinstance(node, ast.Constant) and type(node.value) in (int, float):
        value = node.value
    elif isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        value = _number(node.operand)
        if value is None:
            return None
        value = -value if isinstance(node.op, ast.USub) else value
    elif isinstance(node, ast.BinOp) and type(node.op) in _NUMBER_OPS:
        left, right = _number(node.left), _number(node.right)
        if left is None or right is None:
            return None
        if isinstance(node.op, ast.Pow) and abs(right) > 64:
            raise ValueError("指数过大")
        value = _NUMBER_OPS[type(node.op)](left, right)
    else:
        return None
    if not isinstance(value, (int, float)) or (isinstance(value, float) and not math.isfinite(value)) \
            or abs(value) > 10 ** 18:
        raise ValueError("数值超出范围")
    return value


def py_literal(value: object) -> str:
    """生成 Python 源码字面量；字符串用双引号，与 config.py 的风格一致。"""
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)  # JSON 字符串转义是 Python 转义的子集
    if isinstance(value, list):
        return "[" + ", ".join(py_literal(item) for item in value) + "]"
    return repr(value)


def _same(a: object, b: object) -> bool:
    return type(a) is type(b) and a == b


@dataclass
class Setting:
    name: str
    stmt: ast.stmt
    node: ast.expr
    section: str = ""
    comment: str = ""
    kind: str = ""       # bool / int / float / str / list；空 = 不能逐项输入
    value: object = None
    env_key: str = ""    # 值来自 env("KEY") / env_list("KEY")
    reason: str = ""     # 不是常量的原因

    @property
    def label(self) -> str:
        return LABELS.get(self.name) or self.comment


class ConfigFile:
    """解析由顶层 `NAME = 值` 组成的配置文件（config.py / config.local.py）；不执行文件，文件不存在视为空。"""

    def __init__(self, path: Path, data: bytes | None = None) -> None:
        self.path = path
        if data is None:
            data = path.read_bytes() if path.exists() else b""
        self.data = data.removeprefix(b"\xef\xbb\xbf")  # ast 列号按去掉 BOM 后的文本计算
        tree = ast.parse(self.data.decode("utf-8"), filename=str(path))
        # ast 的列号是 UTF-8 字节偏移，按字节切分行与之对应（同 core/backup.py）
        lines = self.data.splitlines(keepends=True)
        self._starts = [0]
        for line in lines:
            self._starts.append(self._starts[-1] + len(line))
        self._lines = [line.decode("utf-8") for line in lines]
        self.bound: dict[str, int] = {}  # 顶层语句里每个名字被赋值的次数（含 if 块、+= 等）
        for stmt in tree.body:
            names = [stmt.name] if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) else [
                n.id for n in ast.walk(stmt) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)]
            for name in names:
                self.bound[name] = self.bound.get(name, 0) + 1
        sections = self._sections()
        self.settings: dict[str, Setting] = {}
        for stmt in tree.body:
            if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
                target = stmt.targets[0]
            elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
                target = stmt.target
            else:
                continue
            if not isinstance(target, ast.Name) or not _SETTING_NAME.fullmatch(target.id):
                continue
            setting = Setting(target.id, stmt, stmt.value, comment=self._trailing_comment(stmt.value),
                              section=next((t for line, t in reversed(sections) if line <= stmt.lineno), ""))
            if self.bound.get(target.id, 0) > 1:
                setting.reason = f"在 {path.name} 中被多处赋值"
            else:
                self._classify(setting)
            self.settings[target.id] = setting

    @staticmethod
    def _classify(s: Setting) -> None:
        node = s.node
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ("env", "env_list")
                and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
            s.env_key = node.args[0].value
            s.reason = f"值来自 .env 的 {s.env_key}"
            return
        try:
            number = _number(node)
        except (ValueError, ArithmeticError):
            number = None
        if number is not None:
            s.kind, s.value = type(number).__name__, number
            return
        try:
            value = ast.literal_eval(node)
        except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
            s.reason = "表达式或 f-string"
            return
        if isinstance(value, bool):
            s.kind = "bool"
        elif isinstance(value, str):
            if any(ch in value for ch in _LINE_BREAKS):
                s.reason = "多行文本"
                return
            s.kind = "str"
        elif isinstance(value, list) and all(isinstance(item, str) for item in value):
            s.kind = "list"
        else:
            s.reason = "暂不支持的类型"
            return
        s.value = value

    def _sections(self) -> list[tuple[int, str]]:
        """识别 `# ====` / `#  标题 —— 说明` / `# ====` 三行组成的分段标题。"""
        found = []
        lines = [line.strip() for line in self._lines]
        for i in range(len(lines) - 2):
            if (_RULE.fullmatch(lines[i]) and _RULE.fullmatch(lines[i + 2])
                    and lines[i + 1].startswith("#") and not _RULE.fullmatch(lines[i + 1])):
                found.append((i + 3, lines[i + 1].lstrip("#").strip().split(" —— ")[0].strip()))
        return found

    def _span(self, node: ast.AST) -> tuple[int, int]:
        return (self._starts[node.lineno - 1] + node.col_offset,
                self._starts[node.end_lineno - 1] + node.end_col_offset)

    def segment(self, node: ast.AST) -> str:
        start, end = self._span(node)
        return self.data[start:end].decode("utf-8")

    def _trailing_comment(self, node: ast.expr) -> str:
        line = self.data[self._starts[node.end_lineno - 1]:self._starts[node.end_lineno]]
        rest = line[node.end_col_offset:].decode("utf-8", "replace").strip()
        return rest[1:].strip() if rest.startswith("#") else ""

    def describe(self, s: Setting) -> str:
        source = self.segment(s.node)
        if s.kind in ("int", "float") and not isinstance(s.node, ast.Constant):
            return f"{_short(source)}（= {s.value!r}）"
        return _short(source)

    def edit(self, changes: dict[str, str | None], header: str = "") -> bytes:
        """按名字改写：新源码替换已有赋值的值，None 删除整条赋值，没有的项追加到末尾。

        写前重新解析校验：改动项的语法树符合预期，其余配置保持不变。
        """
        content = self.data
        spans, appends = [], []
        for name, source in changes.items():
            s = self.settings.get(name)
            if s is None:
                if source is not None:
                    appends.append(f"{name} = {source}\n")
            elif source is None:
                spans.append((self._starts[s.stmt.lineno - 1], self._starts[s.stmt.end_lineno], b""))
            else:
                spans.append((*self._span(s.node), source.encode("utf-8")))
        for start, end, new in sorted(spans, reverse=True):
            content = content[:start] + new + content[end:]
        if appends:
            if not content.strip():
                content = header.encode("utf-8")
            elif not content.endswith(b"\n"):
                content += b"\n"
            content += "".join(appends).encode("utf-8")
        try:
            check = ConfigFile(self.path, content)
        except SyntaxError as exc:
            raise RuntimeError(f"{self.path.name} 写入前校验失败（{exc.msg}），未保存") from None
        expected = {name: s.node for name, s in self.settings.items()}
        for name, source in changes.items():
            if source is None:
                expected.pop(name, None)
            else:
                expected[name] = ast.parse(source, mode="eval").body
        if check.settings.keys() != expected.keys() or any(
                ast.dump(check.settings[name].node) != ast.dump(node) for name, node in expected.items()):
            raise RuntimeError(f"{self.path.name} 写入前校验失败，未保存")
        return content


def _parse_file(path: Path) -> ConfigFile:
    try:
        return ConfigFile(path)
    except SyntaxError as exc:
        raise ValueError(f"{path.name} 第 {exc.lineno} 行语法错误，请先手动修正") from None


class Config:
    """config.py 的默认值 + config.local.py 的本地覆盖。本工具只写本地文件，不改 config.py。"""

    def __init__(self, root: Path) -> None:
        self.defaults = _parse_file(root / "config.py")
        self.local = _parse_file(root / LOCAL_NAME)
        platforms = []
        for adapter in sorted((root / "adapters").glob("*.py")):
            try:
                platforms += re.findall(r"""^register_adapter\(\s*["'](\w+)["']""",
                                        adapter.read_text(encoding="utf-8"), re.M)
            except (OSError, UnicodeDecodeError):
                continue
        self.platforms = tuple(dict.fromkeys(platforms)) or ("onebot", "feishu")

    def default(self, name: str) -> Setting | None:
        return self.defaults.settings.get(name)

    def override(self, name: str) -> Setting | None:
        return self.local.settings.get(name)

    def custom(self, name: str) -> bool:
        """本地文件里用了工具改不了的写法（if 块、+=、重复赋值），只能手动编辑。"""
        return name in self.local.bound and (self.local.bound[name] > 1 or name not in self.local.settings)

    def value(self, name: str) -> object:
        """生效的常量值；本地写了表达式等无法静态确定时返回 None。"""
        s = self.override(name) if name in self.local.bound else self.default(name)
        return s.value if s is not None and s.kind and not self.custom(name) else None

    def describe(self, name: str, pending: dict | None = None) -> str:
        d = self.default(name)
        if pending is not None and name in pending:
            if pending[name] is not None:
                return f"{_short(pending[name])}  [本地]"
            return f"{self.defaults.describe(d)}（默认）" if d else "（删除）"
        if self.custom(name):
            return f"（见 {LOCAL_NAME}）"
        o = self.override(name)
        if o is not None:
            secret = d is not None and d.env_key and ENV_BY_NAME.get(d.env_key, EnvKey("", "", secret=True)).secret
            return ("****" if secret else self.local.describe(o)) + "  [本地]"
        return self.defaults.describe(d) if d else "未设置"

    def blocked(self, name: str) -> str:
        """不能逐项输入修改的原因；可以修改时返回空字符串。"""
        d = self.default(name)
        if self.custom(name):
            return f"{LOCAL_NAME} 中的写法较复杂，请直接编辑该文件"
        if d is None:
            return f"不是 config.py 的配置项，请直接编辑 {LOCAL_NAME}"
        if d.env_key:
            return f"{d.reason}，请运行 python3 configure.py env 或在 .env 里设置"
        if not d.kind:
            if self.override(name) is not None:
                return f"已在 {LOCAL_NAME} 中设置，请直接编辑该文件"
            return f"默认值是{d.reason}，需要在 {LOCAL_NAME} 里手动赋值"
        return ""

    def tag(self, name: str) -> str:
        """列表里只读项的简短说明。"""
        d = self.default(name)
        if self.custom(name):
            return "本地写法较复杂"
        if d is None:
            return "不在 config.py 中"
        if d.env_key:
            return "在 .env 中设置"
        return "已在本地设置" if self.override(name) is not None else d.reason

    def choices(self, name: str) -> tuple[str, ...]:
        return self.platforms if name == "PLATFORMS" else CHOICES.get(name, ())

    def parse_value(self, name: str, text: str) -> tuple[object, str]:
        """按默认值的类型解析用户输入，返回 (新值, 要写入的源码)；不合法抛 ValueError。"""
        reason = self.blocked(name)
        if reason:
            raise ValueError(reason)
        s = self.default(name)
        text = text.strip()
        if any(ch in text for ch in _LINE_BREAKS):
            raise ValueError(f"不支持多行文本，请直接编辑 {LOCAL_NAME}")
        if s.kind == "bool":
            if text.lower() not in _TRUE | _FALSE:
                raise ValueError("请输入 y 或 n（true/false、1/0 也可以）")
            value = text.lower() in _TRUE
            return value, repr(value)
        if s.kind in ("int", "float"):
            try:
                node = ast.parse(text, mode="eval").body
                value = _number(node)
            except (SyntaxError, MemoryError, RecursionError):
                value = None
            except ArithmeticError:
                raise ValueError("算式无法计算") from None
            if value is None:
                raise ValueError("请输入数字，例如 30、0.5 或 8 * 1024 * 1024")
            if value < 0:
                raise ValueError("不能为负数")
            if s.kind == "int" and not isinstance(value, int):
                raise ValueError("这里需要整数")
            if s.kind == "float" and isinstance(value, int):
                value = float(value)
                return value, repr(value)
            return value, repr(value) if isinstance(node, ast.Constant) else ast.unparse(node)
        if s.kind == "list":
            if text.startswith("["):
                try:
                    value = ast.literal_eval(text)
                except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
                    value = None
                if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                    raise ValueError('列表格式应为 ["a", "b"]，元素都是字符串')
            else:
                value = [item.strip() for item in re.split(r"[,，]", text) if item.strip()]
        else:
            value = text
        choices = self.choices(name)
        bad = [item for item in (value if isinstance(value, list) else [value]) if choices and item not in choices]
        if bad:
            raise ValueError(f"不支持 {', '.join(map(repr, bad))}，可选：{' / '.join(choices)}")
        if name == "PLATFORMS" and not value:
            raise ValueError("至少启用一个平台")
        prefixes = URL_PREFIXES.get(name)
        if prefixes and not value.startswith(prefixes):
            raise ValueError(f"地址应以 {' 或 '.join(prefixes)} 开头")
        return value, py_literal(value)

    def change(self, name: str, value: object, source: str) -> object:
        """新值对应的本地改动：源码；None = 删掉本地覆盖、恢复默认；KEEP = 不用改。"""
        o = self.override(name)
        if _same(value, self.default(name).value):
            return None if o is not None else KEEP
        if o is not None and o.kind and _same(value, o.value):
            return KEEP
        return source

    def render(self, changes: dict[str, str | None]) -> bytes:
        return self.local.edit(changes, LOCAL_HEADER)

    def write(self, content: bytes) -> None:
        path = self.local.path
        source = path if path.exists() else self.defaults.path  # 新建时沿用 config.py 的权限
        atomic_write(path, content, stat.S_IMODE(source.stat().st_mode))


def load_config(root: Path) -> Config:
    return Config(root)


def _stage(pending: dict, name: str, change: object) -> None:
    if change is KEEP:
        pending.pop(name, None)
    else:
        pending[name] = change


# ============================================================
#  检查
# ============================================================

def check(env: EnvFile, cfg: Config | None) -> list[str]:
    """按 .env 和生效的常量配置给出常见问题；不读进程环境变量。"""
    def get(key: str) -> str:
        return env.values.get(key, "")

    def setting(name: str) -> object:
        return cfg.value(name) if cfg else None

    issues = []
    platforms = setting("PLATFORMS") or []
    if not get("LLM_API_KEY"):
        issues.append("未填写 LLM_API_KEY：模型请求会失败")
    if "onebot" in platforms:
        if not get("MASTER_QQ"):
            issues.append("未填写 MASTER_QQ：QQ 侧主人命令、联系主人、重启通知都不可用")
        if not get("BOT_QQ"):
            issues.append("未填写 BOT_QQ：无法生成 NapCat 重启命令和日志路径")
    if "feishu" in platforms:
        if not (get("FEISHU_APP_ID") and get("FEISHU_APP_SECRET")):
            issues.append("启用了 feishu，但 FEISHU_APP_ID / FEISHU_APP_SECRET 没填全；不用飞书就从 PLATFORMS 去掉")
        if not get("MASTER_FEISHU"):
            issues.append("未填写 MASTER_FEISHU：飞书侧主人命令和掉线告警不可用")
    for name in ("MASTER_QQ", "BOT_QQ", "ADMIN_QQ"):
        if not all(_QQ.fullmatch(item.strip()) for item in get(name).split(",") if item.strip()):
            issues.append(f"{name} 应为纯数字 QQ 号")
    if get("MASTER_FEISHU") and not get("MASTER_FEISHU").startswith("ou_"):
        issues.append("MASTER_FEISHU 通常是 ou_ 开头的 open_id")
    if setting("AMBIENT_CHAT") is True and not get("AMBIENT_GROUPS").replace(",", "").strip():
        issues.append("AMBIENT_CHAT 已开启，但 AMBIENT_GROUPS 为空：不会在任何群潜水接话")
    if setting("NAPCAT_WATCH") is True and "onebot" in platforms and "feishu" not in platforms:
        issues.append("NAPCAT_WATCH 需要同时启用 onebot 和 feishu（用飞书报告 QQ 掉线），当前不会运行")
    base = setting("LLM_BASE_URL")
    if SEARCH_KEY not in env.values and isinstance(base, str) and base.rstrip("/") not in _DEEPSEEK_URLS:
        issues.append(f"LLM_BASE_URL 不是 DeepSeek 官方地址，联网搜索默认关闭；需要搜索请设置 {SEARCH_KEY}")
    if cfg is not None:
        unknown = [n for n in cfg.local.bound if _SETTING_NAME.fullmatch(n) and n not in cfg.defaults.settings]
        if unknown:
            issues.append(f"{LOCAL_NAME} 中的 {', '.join(unknown)} 不是 config.py 的配置项，请检查拼写")
    try:
        if env.exists and stat.S_IMODE(env.path.stat().st_mode) & 0o077:
            issues.append(f".env 对其他用户可读，建议执行 chmod 600 {env.path}")
    except OSError:
        pass
    return issues


def print_checks(env: EnvFile, cfg: Config | None) -> None:
    issues = check(env, cfg)
    print("\n检查：" + ("" if issues else "未发现常见问题 ✓"))
    for issue in issues:
        print(f"  ⚠ {issue}")


# ============================================================
#  交互
# ============================================================

def ask(prompt: str) -> str:
    return input(prompt).strip()


def ask_secret(prompt: str) -> str:
    if sys.stdin.isatty():
        return getpass.getpass(prompt).strip()
    return input(prompt).strip()


def confirm(prompt: str, default: bool = True) -> bool:
    while True:
        answer = ask(prompt + (" [Y/n] " if default else " [y/N] ")).lower()
        if not answer:
            return default
        if answer in ("y", "yes", "是"):
            return True
        if answer in ("n", "no", "否"):
            return False


def _ask_env_key(env: EnvFile, key: EnvKey) -> object:
    print(f"\n{key.name}  {key.help}")
    print(f"  当前：{env_display(key.name, env.get(key.name))}")
    if key.name in os.environ:
        print("  注意：当前 shell 设置了同名环境变量，运行时它优先于 .env")
    secret = key.secret and sys.stdin.isatty()
    while True:
        text = (ask_secret if key.secret else ask)("  新值（回车保留，- 清空" + ("，输入不显示" if secret else "") + "）：")
        if not text:
            return KEEP
        try:
            return normalize_env_value(key, "" if text == "-" else text)
        except ValueError as exc:
            print(f"  ✗ {exc}")


def _ask_search_key(env: EnvFile) -> object:
    print(f"\n{SEARCH_KEY}  {ENV_BY_NAME[SEARCH_KEY].help}")
    print(f"  当前：{env_display(SEARCH_KEY, env.get(SEARCH_KEY))}")
    print("  1) 自动：LLM_BASE_URL 是 DeepSeek 官方地址时复用 LLM_API_KEY")
    print("  2) 单独填写一个 DeepSeek 官方 Key")
    print("  3) 关闭联网搜索")
    while True:
        choice = ask("  选择（回车保留）：")
        if not choice:
            return KEEP
        if choice == "1":
            return None
        if choice == "3":
            return ""
        if choice == "2":
            key = ask_secret("  Key：")
            if key:
                try:
                    return normalize_env_value(ENV_BY_NAME[SEARCH_KEY], key)
                except ValueError as exc:
                    print(f"  ✗ {exc}")
            continue
        print("  ✗ 请输入 1、2 或 3")


def _try_config(root: Path) -> Config | None:
    try:
        return load_config(root)
    except (OSError, ValueError):
        return None


def run_env(root: Path, ask_all: bool = False) -> None:
    env = EnvFile(root / ".env", root / ".env.example")
    cfg = _try_config(root)
    enabled = cfg.value("PLATFORMS") if cfg else None
    enabled = enabled if isinstance(enabled, list) else None  # 读不到 PLATFORMS 就全部询问
    print(f"== .env：密钥与账号（{env.path}）==")
    if not env.exists:
        print("还没有 .env，将以 .env.example 为模板创建（权限 0600）。")
    skipped = []
    for key in ENV_KEYS:
        if key.platform and enabled is not None and key.platform not in enabled and not ask_all:
            skipped.append(key.name)
            continue
        result = _ask_search_key(env) if key.name == SEARCH_KEY else _ask_env_key(env, key)
        if result is not KEEP:
            env.set(key.name, result)
    if skipped:
        print(f"\n未启用平台的项已跳过：{', '.join(skipped)}（python3 configure.py env --all 可全部填写）")
    if not env.changed:
        print("\n没有改动。")
        return
    print("\n将要写入：")
    print("\n".join(env.diff()))
    if not confirm("保存到 .env？"):
        print("未保存。")
        return
    env.save()
    print(f"已保存 {env.path}。{RESTART_HINT}")
    print_checks(env, cfg)


def _print_settings(cfg: Config, pending: dict, show_all: bool) -> list[str]:
    """打印列表并返回可编号修改的配置名（编号从 1 开始）。"""
    names = list(cfg.defaults.settings) if show_all else [n for n in COMMON if n in cfg.defaults.settings]
    extra = [n for n in cfg.local.bound if _SETTING_NAME.fullmatch(n) and n not in cfg.defaults.settings]
    width = max((len(n) for n in [*names, *extra]), default=0)
    numbered: list[str] = []
    section = None
    print(f"\n== 行为配置（{'全部' if show_all else '常用'}）：默认值在 config.py，改动写入 {LOCAL_NAME} ==")
    for name in names + (extra if show_all else []):
        d = cfg.default(name)
        if show_all and (d.section if d else f"{LOCAL_NAME} 中的其他项") != section:
            section = d.section if d else f"{LOCAL_NAME} 中的其他项"
            print(f"\n[{section or '其他'}]")
        mark = "*" if name in pending else " "
        if not cfg.blocked(name):
            numbered.append(name)
            label = f"  # {_short(d.label, 50)}" if d.label else ""
            print(f" {mark}{len(numbered):>3}) {name.ljust(width)} = {cfg.describe(name, pending)}{label}")
        else:
            print(f" {mark}   · {name.ljust(width)} = {_short(cfg.describe(name, pending), 40)}  （{cfg.tag(name)}）")
    if not show_all:
        print(f"\n人设 SYSTEM_PROMPT 等多行提示词：输入名字可把默认写法复制到 {LOCAL_NAME}，再用编辑器修改。")
    return numbered


def _edit_setting(cfg: Config, name: str, pending: dict) -> None:
    d = cfg.default(name)
    print(f"\n{name}  {d.label}")
    print(f"  当前：{cfg.describe(name, pending)}")
    overridden = pending[name] is not None if name in pending else cfg.override(name) is not None
    if overridden:
        print(f"  默认：{cfg.defaults.describe(d)}")
    choices = cfg.choices(name)
    if choices:
        print(f"  可选：{' / '.join(choices)}")
    hint = KIND_HINTS[d.kind] + ("；! 恢复默认" if overridden else "")
    while True:
        text = ask(f"  新值（{hint}；回车保留）：")
        if not text:
            return
        if text == "!" and overridden:
            _stage(pending, name, None if cfg.override(name) is not None else KEEP)
            print("  ✓ 恢复默认")
            return
        if text == "-" and d.kind in ("str", "list"):
            text = ""
        try:
            value, source = cfg.parse_value(name, text)
        except ValueError as exc:
            print(f"  ✗ {exc}")
            continue
        change = cfg.change(name, value, source)
        _stage(pending, name, change)
        if change is KEEP:
            print("  与当前值相同，不修改。")
        elif change is None:
            print("  ✓ 与默认值相同，保存时删除本地覆盖")
        else:
            print(f"  ✓ {name} = {_short(source)}")
        return


def _pick_readonly(cfg: Config, name: str, pending: dict) -> None:
    d = cfg.default(name)
    copyable = (d is not None and not d.kind and not d.env_key and name not in cfg.local.bound
                and name not in pending)
    if copyable and confirm(f"{name} 的默认值是{d.reason}，无法逐项输入。把默认写法复制到 {LOCAL_NAME}，"
                            "保存后再用编辑器修改？", default=False):
        pending[name] = cfg.defaults.segment(d.node)
        print(f"  ✓ 保存后编辑 {cfg.local.path} 中的 {name}")
    elif not copyable:
        print(f"✗ {name}：{cfg.blocked(name)}")


def _print_changes(cfg: Config, pending: dict) -> None:
    for name in pending:
        print(f"  {name}: {cfg.describe(name)} → {cfg.describe(name, pending)}")


def run_config(root: Path, show_all: bool = False) -> None:
    cfg = load_config(root)
    pending: dict[str, str | None] = {}
    numbered = _print_settings(cfg, pending, show_all)
    while True:
        cmd = ask("\n输入编号或配置名修改；a 切换常用/全部，l 重新列出，s 保存，q 退出：")
        low = cmd.lower()
        if not cmd:
            continue
        if low in ("a", "l"):
            show_all = not show_all if low == "a" else show_all
            numbered = _print_settings(cfg, pending, show_all)
        elif low == "s":
            if not pending:
                print("没有改动。")
                return
            print(f"\n将要写入 {LOCAL_NAME}：")
            _print_changes(cfg, pending)
            if confirm(f"保存到 {LOCAL_NAME}？"):
                cfg.write(cfg.render(pending))
                print(f"已保存 {cfg.local.path}。{RESTART_HINT}")
                return
        elif low == "q":
            if not pending or confirm("放弃未保存的改动？", default=False):
                return
        else:
            name = numbered[int(cmd) - 1] if cmd.isdigit() and 0 < int(cmd) <= len(numbered) else cmd.upper()
            if name not in cfg.defaults.settings and name not in cfg.local.bound:
                print("✗ 没有这个编号或配置项")
            elif cfg.blocked(name):
                _pick_readonly(cfg, name, pending)
            else:
                _edit_setting(cfg, name, pending)


def run_show(root: Path, show_all: bool = False) -> None:
    env = EnvFile(root / ".env")
    print(f"== .env（{env.path}{'' if env.exists else '，不存在'}）==")
    names = list(dict.fromkeys([*ENV_BY_NAME, *env.values]))
    width = max(map(len, names))
    for name in names:
        note = "（当前 shell 有同名环境变量，运行时优先）" if name in os.environ else ""
        print(f"  {name.ljust(width)} = {env_display(name, env.get(name))}{note}")
    try:
        cfg = load_config(root)
    except (OSError, ValueError) as exc:
        cfg = None
        print(f"\n✗ {exc}")
    if cfg is not None:
        _print_settings(cfg, {}, show_all)
    print_checks(env, cfg)


def _save_both(env: EnvFile, cfg: Config, changes: dict) -> None:
    """两个文件都校验通过后再依次写入，并打印改动。"""
    content = cfg.render(changes) if changes else None
    env_changed = env.changed
    if env_changed:
        lines = env.diff()
        env.save()
        print(f"已更新 {env.path}：")
        print("\n".join(lines))
    if content is not None:
        cfg.write(content)
        print(f"已更新 {cfg.local.path}：")
        _print_changes(cfg, changes)  # cfg 仍是写入前的状态，正好显示 旧值 → 新值
    print(RESTART_HINT if env_changed or content is not None else "没有改动。")


def run_set(root: Path, assignments: list[str]) -> None:
    env = EnvFile(root / ".env", root / ".env.example")
    cfg = load_config(root)
    env_names = set(ENV_BY_NAME) | set(env.values) | {s.env_key for s in cfg.defaults.settings.values() if s.env_key}
    pending: dict[str, str | None] = {}
    for index, item in enumerate(assignments, 1):
        name, sep, value = item.partition("=")
        name = name.strip()
        if not sep:
            raise ValueError(f"第 {index} 个参数应为 KEY=VALUE 形式")  # 不回显，可能是误贴的密钥
        d = cfg.default(name)
        try:
            if d is not None and not (d.env_key and name in env_names):
                _stage(pending, name, cfg.change(name, *cfg.parse_value(name, value)))
            elif name in env_names:
                env.set(name, normalize_env_value(ENV_BY_NAME.get(name, EnvKey(name, "", secret=True)), value))
            else:
                raise ValueError("未知配置项（python3 configure.py show --all 查看全部）")
        except ValueError as exc:
            raise ValueError(f"{name}：{exc}") from None
    _save_both(env, cfg, pending)


def run_unset(root: Path, names: list[str]) -> None:
    env = EnvFile(root / ".env")
    cfg = load_config(root)
    changes: dict[str, str | None] = {}
    for name in names:
        if cfg.custom(name):
            raise ValueError(f"{name}：{LOCAL_NAME} 中的写法较复杂，请直接编辑该文件")
        found = name in cfg.local.settings or name in env.values
        if name in cfg.local.settings:
            changes[name] = None  # 删掉本地覆盖，恢复 config.py 的默认值
        if name in env.values:
            env.set(name, None)
        if not found:
            print(f"{name} 本来就未设置。")
    _save_both(env, cfg, changes)


def run_migrate(root: Path, ref: str) -> None:
    """把 git stash（或任意提交）里对 config.py 的改动，转成 config.local.py 的覆盖项。"""
    def show(spec: str) -> bytes:
        try:
            return subprocess.run(["git", "-C", str(root), "show", spec], check=True, capture_output=True).stdout
        except (OSError, subprocess.CalledProcessError):
            raise ValueError(f"读不到 {spec}。先执行 git stash push -- config.py 暂存对 config.py 的改动，"
                             "更新代码后再运行 python3 configure.py migrate") from None
    try:
        old = ConfigFile(Path("config.py"), show(f"{ref}:config.py"))
        base = ConfigFile(Path("config.py"), show(f"{ref}^1:config.py"))
    except SyntaxError as exc:
        raise ValueError(f"{ref} 中的 config.py 第 {exc.lineno} 行语法错误") from None
    cfg = load_config(root)
    changes: dict[str, str | None] = {}
    notes = []
    for name, s in old.settings.items():
        b, d = base.settings.get(name), cfg.default(name)
        if (b is not None and ast.dump(b.node) == ast.dump(s.node)) or (
                d is not None and ast.dump(d.node) == ast.dump(s.node)):
            continue  # 没改过，或新版默认值已经一样
        if d is not None and d.env_key:
            notes.append(f"{name} 现在从 .env 读取，请运行 python3 configure.py env 填写")
        elif name in cfg.local.bound:
            notes.append(f"{name} 已在 {LOCAL_NAME} 中设置，保留现有写法")
        else:
            if d is None:
                notes.append(f"{name} 已不是 config.py 的配置项，仍会写入，请确认是否还需要")
            changes[name] = old.segment(s.node)
    for name, source in changes.items():
        print(f"  {name} = {_short(source)}")
    for note in notes:
        print(f"  ⚠ {note}")
    if not changes:
        print("没有需要迁移的配置改动。")
        return
    print(f"以上 {len(changes)} 项将写入 {LOCAL_NAME}（注释、import 等其他改动不会迁移）。")
    if not confirm(f"写入 {LOCAL_NAME}？"):
        print("未保存。")
        return
    cfg.write(cfg.render(changes))
    print(f"已保存 {cfg.local.path}。确认无误后可执行 git stash drop 丢弃暂存的改动。{RESTART_HINT}")


def run_menu(root: Path) -> None:
    actions = {"1": run_env, "2": run_config, "3": run_show}
    while True:
        print("\nnekobot 配置工具")
        print("  1) .env：密钥与账号")
        print(f"  2) 行为配置（写入 {LOCAL_NAME}）")
        print("  3) 查看当前配置并检查")
        print("  q) 退出")
        choice = ask("选择：").lower()
        if choice in ("q", "0"):
            return
        if choice in actions:
            try:
                actions[choice](root)
            except (OSError, ValueError, RuntimeError) as exc:
                print(f"✗ {exc}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=f"nekobot 配置工具：编辑 .env（密钥与账号）和 {LOCAL_NAME}（覆盖 config.py 默认值的行为配置）。"
                    "不带子命令进入交互菜单。")
    parser.add_argument("--root", type=Path, default=ROOT, help="nekobot 目录，默认为脚本所在目录")
    sub = parser.add_subparsers(dest="command")
    env = sub.add_parser("env", help="逐项填写 .env；不存在时从 .env.example 创建")
    env.add_argument("--all", action="store_true", help="未启用平台的项也询问")
    config = sub.add_parser("config", help=f"交互修改行为配置，改动写入 {LOCAL_NAME}")
    config.add_argument("--all", action="store_true", help="一开始就列出全部配置")
    show = sub.add_parser("show", help="查看当前配置（密钥打码）并检查常见问题")
    show.add_argument("--all", action="store_true", help="列出全部行为配置")
    set_ = sub.add_parser("set", help=f"非交互修改，自动判断写入 .env 还是 {LOCAL_NAME}",
                          epilog="例：python3 configure.py set LLM_MODEL=deepseek-chat PLATFORMS=onebot MASTER_QQ=10001。"
                                 "列表用逗号分隔；设成默认值会删掉本地覆盖。"
                                 "密钥写在命令行会留在 shell 历史里，建议用 env 交互填写。")
    set_.add_argument("assignments", nargs="+", metavar="KEY=VALUE")
    unset = sub.add_parser("unset", help=f"删除 .env 的键或 {LOCAL_NAME} 的覆盖项（恢复默认）")
    unset.add_argument("names", nargs="+", metavar="KEY")
    migrate = sub.add_parser("migrate", help=f"把 git stash 里对 config.py 的改动迁移到 {LOCAL_NAME}",
                             epilog="用法：git stash push -- config.py → git pull --ff-only → "
                                    "python3 configure.py migrate → git stash drop")
    migrate.add_argument("--ref", default="stash@{0}", help="含旧 config.py 改动的 stash 或提交，默认 stash@{0}")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    try:
        if args.command is None:
            run_menu(root)
        elif args.command == "env":
            run_env(root, args.all)
        elif args.command == "config":
            run_config(root, args.all)
        elif args.command == "show":
            run_show(root, args.all)
        elif args.command == "set":
            run_set(root, args.assignments)
        elif args.command == "unset":
            run_unset(root, args.names)
        else:
            run_migrate(root, args.ref)
        return 0
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"配置失败：{exc}", file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print("\n已取消，未保存的改动已丢弃。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
