"""本地配置 —— 项目根目录的 `config.local.py`（已在 .gitignore），覆盖 config.py 中的默认值。

config.py 随代码更新，只放默认值；个人配置写在 config.local.py，/update 拉取代码不会冲突。
config.py 末尾调用 `load_local(globals())`：

1. 在默认值的副本里执行 config.local.py，所以本地文件能引用默认值和 env()，
   例如 `BOT_NAMES = BOT_NAMES + ["小喵"]`。其中顶层赋值的大写名字就是覆盖项。
2. config.py 里依赖被覆盖项的默认值按源码顺序重新计算，例如本地改了 LLM_MODEL，
   AMBIENT_REPLY_MODEL = LLM_MODEL 也跟着变；本地单独赋值的项保持本地的值。
3. 不在 config.py 里的名字照样生效，但会在 stderr 提示，方便发现拼写错误。

仅依赖标准库。
"""

import ast
from pathlib import Path
import re
import sys

LOCAL_FILE = Path(__file__).resolve().parent.parent / "config.local.py"

_SETTING = re.compile(r"[A-Z][A-Z0-9_]*")


def _assigned(tree: ast.Module) -> set[str]:
    return {node.id for node in ast.walk(tree)
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store) and _SETTING.fullmatch(node.id)}


def _rederive(namespace: dict, overrides: dict) -> dict:
    """按 config.py 的源码顺序，重新计算引用了已变化名字的顶层赋值（本地覆盖的项除外）。"""
    file = namespace.get("__file__")
    if not overrides or not file:
        return {}
    tree = ast.parse(Path(file).read_text(encoding="utf-8"), file)
    scope = {**namespace, **overrides}
    changed = set(overrides)
    result = {}
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
            target = stmt.targets[0]
        elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
            target = stmt.target
        else:
            continue
        if not isinstance(target, ast.Name) or target.id in overrides:
            continue
        if not changed & {n.id for n in ast.walk(stmt.value) if isinstance(n, ast.Name)}:
            continue
        value = eval(compile(ast.Expression(stmt.value), file, "eval"), scope)
        scope[target.id] = result[target.id] = value
        changed.add(target.id)
    return result


def load_local(namespace: dict, path: Path | None = None) -> dict:
    """执行本地配置，返回要更新到 config 模块的值；文件不存在时返回空字典。"""
    path = LOCAL_FILE if path is None else path
    try:
        source = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return {}
    tree = ast.parse(source, str(path))  # 语法错误直接抛出，报告本地文件的行号
    scope = {**namespace, "__name__": "config_local", "__file__": str(path)}
    exec(compile(tree, str(path), "exec"), scope)
    overrides = {name: scope[name] for name in sorted(_assigned(tree)) if name in scope}
    unknown = [name for name in overrides if name not in namespace]
    if unknown:
        print(f"[config] {path.name} 中的 {', '.join(unknown)} 不是 config.py 里的配置项，请检查拼写",
              file=sys.stderr)
    return {**overrides, **_rederive(namespace, overrides)}
