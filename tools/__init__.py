"""工具自动发现 —— import 本包即加载所有工具模块。

新增工具 = 在本目录新建 .py 文件并用 core.tools.tool 装饰器注册即可。
以 `_` 开头的模块会被跳过。
"""

import importlib
import pkgutil


def load_tools() -> None:
    for info in pkgutil.iter_modules(__path__):
        if info.name.startswith("_"):
            continue
        importlib.import_module(f"{__name__}.{info.name}")


load_tools()
