#!/usr/bin/env python3
"""命令行配置工具：python3 configure.py（仅需 Python 标准库；--help 查看子命令）。

- `.env`：逐项填写密钥与账号。按行更新，注释、顺序和其他键原样保留；不存在时以 `.env.example`
  为模板创建，权限 0600。取值与引号规则和 core/env.py 一致。
- `config.py`：用 AST 定位顶层常量赋值，只替换值本身，行尾注释和其余代码不变；不会执行配置文件。
  f-string、env() 调用、多行提示词等不是常量的配置只展示，请直接编辑文件。
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
#  config.py —— 行为配置
# ============================================================

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
    node: ast.expr
    section: str = ""
    comment: str = ""
    kind: str = ""       # bool / int / float / str / list；空 = 不能用本工具修改
    value: object = None
    env_key: str = ""    # 值来自 env("KEY") / env_list("KEY")
    reason: str = ""     # 不能修改的原因

    @property
    def label(self) -> str:
        return LABELS.get(self.name) or self.comment


class ConfigFile:
    """config.py 的只读解析 + 按值替换。不执行文件，只认顶层 `NAME = 常量` 赋值。"""

    def __init__(self, path: Path, data: bytes | None = None) -> None:
        self.path = path
        self.data = path.read_bytes() if data is None else data
        tree = ast.parse(self.data.decode("utf-8"), filename=str(path))
        # ast 的列号是 UTF-8 字节偏移，按字节切分行与之对应（同 core/backup.py）
        lines = self.data.splitlines(keepends=True)
        self._starts = [0]
        for line in lines:
            self._starts.append(self._starts[-1] + len(line))
        self._lines = [line.decode("utf-8") for line in lines]
        self.settings: dict[str, Setting] = {}
        counts: dict[str, int] = {}
        for stmt in tree.body:
            bound = [stmt.name] if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) else [
                n.id for n in ast.walk(stmt) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)]
            for name in bound:
                counts[name] = counts.get(name, 0) + 1
        sections = self._sections()
        for stmt in tree.body:
            if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
                target = stmt.targets[0]
            elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
                target = stmt.target
            else:
                continue
            if not isinstance(target, ast.Name) or not _SETTING_NAME.fullmatch(target.id):
                continue
            setting = Setting(target.id, stmt.value, comment=self._trailing_comment(stmt.value),
                              section=next((t for line, t in reversed(sections) if line <= stmt.lineno), ""))
            if counts.get(target.id, 0) > 1:
                setting.reason = "在 config.py 中被多处赋值"
            else:
                self._classify(setting)
            self.settings[target.id] = setting
        platforms = []
        for adapter in sorted((path.parent / "adapters").glob("*.py")):
            try:
                platforms += re.findall(r"""^register_adapter\(\s*["'](\w+)["']""",
                                        adapter.read_text(encoding="utf-8"), re.M)
            except (OSError, UnicodeDecodeError):
                continue
        self.platforms = tuple(dict.fromkeys(platforms)) or ("onebot", "feishu")

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
            s.reason = "表达式或 f-string，不是常量"
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

    def _span(self, node: ast.expr) -> tuple[int, int]:
        return (self._starts[node.lineno - 1] + node.col_offset,
                self._starts[node.end_lineno - 1] + node.end_col_offset)

    def segment(self, node: ast.expr) -> str:
        start, end = self._span(node)
        return self.data[start:end].decode("utf-8")

    def _trailing_comment(self, node: ast.expr) -> str:
        line = self.data[self._starts[node.end_lineno - 1]:self._starts[node.end_lineno]]
        rest = line[node.end_col_offset:].decode("utf-8", "replace").strip()
        return rest[1:].strip() if rest.startswith("#") else ""

    def choices(self, name: str) -> tuple[str, ...]:
        return self.platforms if name == "PLATFORMS" else CHOICES.get(name, ())

    def describe(self, s: Setting) -> str:
        source = self.segment(s.node)
        if s.kind in ("int", "float") and not isinstance(s.node, ast.Constant):
            return f"{_short(source)}（= {s.value!r}）"
        return _short(source)

    def parse_value(self, s: Setting, text: str) -> tuple[object, str]:
        """把用户输入解析成 (新值, 要写回的源码)；不合法抛 ValueError。"""
        if not s.kind:
            hint = f"请在 .env 里设置 {s.env_key}" if s.env_key else "请直接编辑 config.py"
            raise ValueError(f"{s.reason}，{hint}")
        text = text.strip()
        if any(ch in text for ch in _LINE_BREAKS):
            raise ValueError("不支持多行文本，请直接编辑 config.py")
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
        choices = self.choices(s.name)
        bad = [item for item in (value if isinstance(value, list) else [value]) if choices and item not in choices]
        if bad:
            raise ValueError(f"不支持 {', '.join(map(repr, bad))}，可选：{' / '.join(choices)}")
        if s.name == "PLATFORMS" and not value:
            raise ValueError("至少启用一个平台")
        prefixes = URL_PREFIXES.get(s.name)
        if prefixes and not value.startswith(prefixes):
            raise ValueError(f"地址应以 {' 或 '.join(prefixes)} 开头")
        return value, py_literal(value)

    def render(self, changes: dict[str, tuple[object, str]]) -> bytes:
        """替换改动项的值源码，并重新解析校验：改动项取到预期值，其余配置保持不变。"""
        content = self.data
        spans = sorted(((*self._span(self.settings[name].node), source.encode("utf-8"))
                        for name, (_, source) in changes.items()), reverse=True)
        for start, end, source in spans:
            content = content[:start] + source + content[end:]
        try:
            check = ConfigFile(self.path, content)
        except SyntaxError as exc:
            raise RuntimeError(f"config.py 写入前校验失败（{exc.msg}），未保存") from None
        if check.settings.keys() != self.settings.keys():
            raise RuntimeError("config.py 写入前校验失败（配置项发生变化），未保存")
        for name, old in self.settings.items():
            new = check.settings[name]
            if name in changes:
                ok = new.kind == old.kind and _same(new.value, changes[name][0])
            else:
                ok = ast.dump(new.node) == ast.dump(old.node)
            if not ok:
                raise RuntimeError(f"config.py 写入前校验失败（{name}），未保存")
        return content

    def write(self, content: bytes) -> None:
        atomic_write(self.path, content, stat.S_IMODE(self.path.stat().st_mode))


def load_config(root: Path) -> ConfigFile:
    path = root / "config.py"
    try:
        return ConfigFile(path)
    except SyntaxError as exc:
        raise ValueError(f"config.py 第 {exc.lineno} 行语法错误，请先手动修正") from None


# ============================================================
#  检查
# ============================================================

def check(env: EnvFile, cfg: ConfigFile | None) -> list[str]:
    """按 .env 和 config.py 的常量值给出常见配置问题；不读进程环境变量。"""
    def get(key: str) -> str:
        return env.values.get(key, "")

    def setting(name: str) -> object:
        s = cfg.settings.get(name) if cfg else None
        return s.value if s is not None and s.kind else None

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
    try:
        if env.exists and stat.S_IMODE(env.path.stat().st_mode) & 0o077:
            issues.append(f".env 对其他用户可读，建议执行 chmod 600 {env.path}")
    except OSError:
        pass
    return issues


def print_checks(env: EnvFile, cfg: ConfigFile | None) -> None:
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


def run_env(root: Path, ask_all: bool = False) -> None:
    env = EnvFile(root / ".env", root / ".env.example")
    try:
        platforms = load_config(root).settings.get("PLATFORMS")
        enabled = platforms.value if platforms is not None and platforms.kind == "list" else None
    except (OSError, ValueError):
        enabled = None  # 读不到 PLATFORMS 就全部询问
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
    print_checks(env, _try_config(root))


def _try_config(root: Path) -> ConfigFile | None:
    try:
        return load_config(root)
    except (OSError, ValueError):
        return None


def _print_settings(cfg: ConfigFile, pending: dict, show_all: bool) -> list[Setting]:
    """打印列表并返回可编号修改的项（编号从 1 开始）。"""
    shown = list(cfg.settings.values()) if show_all else [cfg.settings[n] for n in COMMON if n in cfg.settings]
    width = max((len(s.name) for s in shown), default=0)
    numbered: list[Setting] = []
    section = None
    print("\n== config.py：" + ("全部配置" if show_all else "常用配置") + " ==")
    for s in shown:
        if show_all and s.section != section:
            section = s.section
            print(f"\n[{section or '其他'}]")
        if s.kind:
            numbered.append(s)
            mark = "*" if s.name in pending else " "
            value = _short(pending[s.name][1]) if s.name in pending else cfg.describe(s)
            label = f"  # {_short(s.label, 50)}" if s.label else ""
            print(f" {mark}{len(numbered):>3}) {s.name.ljust(width)} = {value}{label}")
        else:
            print(f"      · {s.name.ljust(width)} = {_short(cfg.segment(s.node), 40)}  （{s.reason}）")
    if not show_all:
        print("\n人设 SYSTEM_PROMPT 等多行提示词请直接编辑 config.py。")
    return numbered


def _edit_setting(cfg: ConfigFile, s: Setting, pending: dict) -> None:
    print(f"\n{s.name}  {s.label}")
    print(f"  当前：{_short(pending[s.name][1]) if s.name in pending else cfg.describe(s)}")
    choices = cfg.choices(s.name)
    if choices:
        print(f"  可选：{' / '.join(choices)}")
    while True:
        text = ask(f"  新值（{KIND_HINTS[s.kind]}；回车保留）：")
        if not text:
            return
        if text == "-" and s.kind in ("str", "list"):
            text = ""
        try:
            value, source = cfg.parse_value(s, text)
        except ValueError as exc:
            print(f"  ✗ {exc}")
            continue
        if _same(value, s.value):
            pending.pop(s.name, None)
            print("  与原值相同，不修改。")
        else:
            pending[s.name] = (value, source)
            print(f"  ✓ {s.name} = {_short(source)}")
        return


def run_config(root: Path, show_all: bool = False) -> None:
    cfg = load_config(root)
    pending: dict[str, tuple[object, str]] = {}
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
            print("\n将要写入：")
            for name, (_, source) in pending.items():
                print(f"  {name}: {cfg.describe(cfg.settings[name])} → {_short(source)}")
            if confirm("保存到 config.py？"):
                cfg.write(cfg.render(pending))
                print(f"已保存 {cfg.path}。{RESTART_HINT}")
                return
        elif low == "q":
            if not pending or confirm("放弃未保存的改动？", default=False):
                return
        else:
            s = numbered[int(cmd) - 1] if cmd.isdigit() and 0 < int(cmd) <= len(numbered) else \
                cfg.settings.get(cmd.upper())
            if s is None:
                print("✗ 没有这个编号或配置项")
            elif not s.kind:
                hint = f"请运行 python3 configure.py env 或在 .env 里设置 {s.env_key}" if s.env_key \
                    else "请直接编辑 config.py"
                print(f"✗ {s.name}：{s.reason}，{hint}")
            else:
                _edit_setting(cfg, s, pending)


def run_show(root: Path, show_all: bool = False) -> None:
    env = EnvFile(root / ".env")
    print(f"== .env（{env.path}{'' if env.exists else '，不存在'}）==")
    names = list(dict.fromkeys([*ENV_BY_NAME, *env.values]))
    width = max(map(len, names))
    for name in names:
        note = "（当前 shell 有同名环境变量，运行时优先）" if name in os.environ else ""
        print(f"  {name.ljust(width)} = {env_display(name, env.get(name))}{note}")
    cfg = _try_config(root)
    if cfg is None:
        print("\nconfig.py 无法解析，跳过。")
    else:
        _print_settings(cfg, {}, show_all)
    print_checks(env, cfg)


def run_set(root: Path, assignments: list[str]) -> None:
    env = EnvFile(root / ".env", root / ".env.example")
    cfg = load_config(root)
    env_names = set(ENV_BY_NAME) | set(env.values) | {s.env_key for s in cfg.settings.values() if s.env_key}
    pending: dict[str, tuple[object, str]] = {}
    for index, item in enumerate(assignments, 1):
        name, sep, value = item.partition("=")
        name = name.strip()
        if not sep:
            raise ValueError(f"第 {index} 个参数应为 KEY=VALUE 形式")  # 不回显，可能是误贴的密钥
        s = cfg.settings.get(name)
        try:
            if s is not None and (s.kind or name not in env_names):  # config.py 的常量优先：.env 里同名键 bot 不会读
                parsed = cfg.parse_value(s, value)
                if _same(parsed[0], s.value):
                    pending.pop(name, None)
                else:
                    pending[name] = parsed
            elif name in env_names:
                env.set(name, normalize_env_value(ENV_BY_NAME.get(name, EnvKey(name, "", secret=True)), value))
            else:
                raise ValueError("未知配置项（python3 configure.py show --all 查看全部）")
        except ValueError as exc:
            raise ValueError(f"{name}：{exc}") from None
    content = cfg.render(pending) if pending else None  # 两个文件都校验通过后再写
    env_changed = env.changed
    if env_changed:
        lines = env.diff()
        env.save()
        print(f"已更新 {env.path}：")
        print("\n".join(lines))
    if content is not None:
        before = {name: cfg.describe(cfg.settings[name]) for name in pending}
        cfg.write(content)
        print(f"已更新 {cfg.path}：")
        for name, (_, source) in pending.items():
            print(f"  {name}: {before[name]} → {_short(source)}")
    if env_changed or content is not None:
        print(RESTART_HINT)
    else:
        print("没有改动。")


def run_unset(root: Path, names: list[str]) -> None:
    env = EnvFile(root / ".env")
    if not env.exists:
        raise ValueError(f"{env.path} 不存在")
    for name in names:
        if name not in env.values:
            print(f"{name} 本来就未设置。")
        env.set(name, None)
    if env.changed:
        lines = env.diff()
        env.save()
        print(f"已更新 {env.path}：")
        print("\n".join(lines))
        print(RESTART_HINT)


def run_menu(root: Path) -> None:
    actions = {"1": run_env, "2": run_config, "3": run_show}
    while True:
        print("\nnekobot 配置工具")
        print("  1) .env：密钥与账号")
        print("  2) config.py：行为配置")
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
        description="nekobot 配置工具：编辑 .env（密钥与账号）和 config.py（行为配置）。不带子命令进入交互菜单。")
    parser.add_argument("--root", type=Path, default=ROOT, help="nekobot 目录，默认为脚本所在目录")
    sub = parser.add_subparsers(dest="command")
    env = sub.add_parser("env", help="逐项填写 .env；不存在时从 .env.example 创建")
    env.add_argument("--all", action="store_true", help="未启用平台的项也询问")
    config = sub.add_parser("config", help="交互修改 config.py 中的常量配置")
    config.add_argument("--all", action="store_true", help="一开始就列出全部配置")
    show = sub.add_parser("show", help="查看当前配置（密钥打码）并检查常见问题")
    show.add_argument("--all", action="store_true", help="列出 config.py 全部配置")
    set_ = sub.add_parser("set", help="非交互修改，自动判断写入 .env 还是 config.py",
                          epilog="例：python3 configure.py set LLM_MODEL=deepseek-chat 'PLATFORMS=onebot' MASTER_QQ=10001。"
                                 "列表用逗号分隔；密钥写在命令行会留在 shell 历史里，建议用 env 交互填写。")
    set_.add_argument("assignments", nargs="+", metavar="KEY=VALUE")
    unset = sub.add_parser("unset", help="从 .env 删除键，如删除 DEEPSEEK_SEARCH_API_KEY 恢复自动")
    unset.add_argument("names", nargs="+", metavar="KEY")
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
        else:
            run_unset(root, args.names)
        return 0
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"配置失败：{exc}", file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print("\n已取消，未保存的改动已丢弃。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
