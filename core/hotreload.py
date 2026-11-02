"""插件 / 工具热更新 —— 不重启进程重新加载 plugins/ 和 tools/。

做法：**原地**清空注册表（`_matchers`/`_middlewares`/`_tools`，必须 in-place 清，
保持别处 import 持有的引用仍指向同一对象）→ 从 `sys.modules` 移除 plugins.*/tools.*
子模块 → 重新 import 两个包，触发各模块体里的装饰器重新注册（含新增文件）。

失败安全：若重载中途某个模块导入报错（语法/导入错误），自动**回滚**到旧的注册表与
旧模块对象，保证正在跑的 bot 不被一次坏改动搞挂。

注意：只重载 plugins/tools；改了 core/* 或 config 仍需重启进程。
"""

from __future__ import annotations

import importlib
import sys

from core import middleware, plugin, tools
from core.log import log


def _is_ext(name: str) -> bool:
    return name in ("plugins", "tools") or name.startswith("plugins.") or name.startswith("tools.")


def reload_extensions() -> dict:
    """重载 plugins/tools，返回各注册表数量。失败则回滚并抛出异常。"""
    # 快照，供失败回滚
    old_matchers = list(plugin._matchers)
    old_mws = list(middleware._middlewares)
    old_tools = dict(tools._tools)
    old_mods = {n: m for n, m in sys.modules.items() if _is_ext(n)}

    try:
        plugin._matchers.clear()
        middleware._middlewares.clear()
        tools._tools.clear()
        for n in list(sys.modules):
            if _is_ext(n):
                del sys.modules[n]
        importlib.import_module("tools")
        importlib.import_module("plugins")
        counts = {
            "tools": len(tools._tools),
            "matchers": len(plugin._matchers),
            "middlewares": len(middleware._middlewares),
        }
        log.info(
            f"[热更新] 重载完成：工具 {counts['tools']}、命令/消息 {counts['matchers']}、中间件 {counts['middlewares']}"
        )
        return counts
    except Exception as e:
        log.error(f"[热更新] 重载失败，回滚到旧版本：{e!r}")
        # 回滚注册表（in-place）
        plugin._matchers[:] = old_matchers
        middleware._middlewares[:] = old_mws
        tools._tools.clear()
        tools._tools.update(old_tools)
        # 回滚 sys.modules：清掉半加载的，塞回旧模块对象
        for n in list(sys.modules):
            if _is_ext(n):
                del sys.modules[n]
        sys.modules.update(old_mods)
        raise
