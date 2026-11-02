"""平台适配器包。

导入本包即触发各 adapter 的自动注册（把工厂登记进 `core.adapter`）。
新增平台：新建 `xxx.py`，实现 `Adapter`+`Bot`，在下面 import 一下即可。
"""

# 导入各 adapter 模块以触发 register_adapter 注册
from adapters import onebot as _onebot  # noqa: F401
from adapters import feishu as _feishu  # noqa: F401
