"""插件自动发现 —— import 本包即加载所有插件模块。

新增插件 = 在本目录新建一个 .py 文件并用 core.plugin / core.middleware 的装饰器
注册即可，无需改这里。以 `_` 开头的模块会被跳过。
"""

import importlib
import pkgutil

from core.log import log


def load_plugins() -> None:
    for info in pkgutil.iter_modules(__path__):
        if info.name.startswith("_"):
            continue
        importlib.import_module(f"{__name__}.{info.name}")
        log.info(f"已加载插件: {info.name}")


load_plugins()
