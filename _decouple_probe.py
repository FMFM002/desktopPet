# -*- coding: utf-8 -*-
"""经验验证：ToolManager._load_tool_from_py 产生的模块对象 vs `import tools.X`；
以及 long_term_memory_tool 单例是否被复制。只读，不改动任何文件。"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pathlib import Path

from src.core.tool_manager import ToolManager, ToolLoadPolicy


class Stub:
    def _get_policy(self, name):
        return ToolLoadPolicy.RESIDENT


info = ToolManager._load_tool_from_py(Stub(), Path("tools/long_term_memory_tool.py"))
print("1) 扫描得到的 module.__name__      =", info._module.__name__)
print("2) 该 module 在 sys.modules 里吗?   =",
      any(v is info._module for v in sys.modules.values()))
print("3) 该 module 的 sys.modules key 列表 =",
      [k for k, v in sys.modules.items() if v is info._module])
print("4) name 解析为                      =", info.name, "| policy =", info.load_policy.value)
print("5) tool._module is tool._func.__globals__ 所属模块? =",
      info._func.__globals__ is info._module.__dict__)

i_scan = info._module.get_long_term_memory_tool()

import tools.long_term_memory_tool as real
i_real = real.get_long_term_memory_tool()

print()
print("6) `import tools.long_term_memory_tool` 的对象 is 扫描对象? =", real is info._module)
print("7) sys.modules['tools.long_term_memory_tool'] 存在?       =",
      "tools.long_term_memory_tool" in sys.modules)
print("8) 两个 get_long_term_memory_tool() 返回值 is 同一个实例? =", i_scan is i_real)
print("9) 扫描副本的 _tool_instance is 正式模块的 _tool_instance?  =",
      info._module._tool_instance is real._tool_instance)

# 复刻 tool_manager.py:261-267 的 _sync_ltm_disabled
info._module.get_long_term_memory_tool().set_disabled(False)
real.get_long_term_memory_tool().set_disabled(True)
print()
print("10) 按 tool_manager._sync_ltm_disabled 的写法(改正式模块)置 disabled=True")
print("    · 正式模块单例 is_disabled()      =", i_real.is_disabled())
print("    · 执行路径(扫描副本)单例 is_disabled() =", i_scan.is_disabled())
print("    → execute() 走的是扫描副本，禁用开关没生效:", i_scan.is_disabled() is False)

# 同样的机制检查 ToolInfo._load_module（LAZY 路径）
print()
info2 = ToolManager._load_tool_from_py(Stub(), Path("tools/meme_tool.py"))
print("11) meme_tool 扫描 module 名 =", info2._module.__name__,
      "| 在 sys.modules?", [k for k, v in sys.modules.items() if v is info2._module])
import tools.meme_tool as real_meme
print("12) `import tools.meme_tool` is 扫描对象? =", real_meme is info2._module)
