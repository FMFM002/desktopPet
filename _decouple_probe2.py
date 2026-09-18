# -*- coding: utf-8 -*-
"""验证：
(1) 扫描阶段 LAZY 工具的 module 去向（_load_tool_from_py）
(2) UI 层 `from tools.X import ...` 之后，LAZY 卸载能否把它从 sys.modules 清掉
只读，不改动任何文件。
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pathlib import Path
from src.core.tool_manager import ToolManager, ToolLoadPolicy, ToolInfo

def snap():
    return sorted(k for k in sys.modules if "jmcomic" in k.lower())

class _L:
    def _get_policy(self, n):
        return ToolLoadPolicy.LAZY

class _R:
    def _get_policy(self, n):
        return ToolLoadPolicy.RESIDENT

lazy_info = ToolManager._load_tool_from_py(_L(), Path("tools/jmcomic_tool.py"))
print("1) LAZY 扫描结果: name=%s policy=%s  _module=%r  _func=%r"
      % (lazy_info.name, lazy_info.load_policy.value, lazy_info._module, lazy_info._func))
print("   → 扫描时 exec_module 已完整执行该模块，但对象被丢弃（_module=None）")

res_info = ToolManager._load_tool_from_py(_R(), Path("tools/long_term_memory_tool.py"))
print("2) RESIDENT 扫描结果: _module=%s (被保留下来当执行路径用)"
      % res_info._module.__name__)

print()
print("3) 卸载前 sys.modules 中含 jmcomic 的 key:", snap())
from tools.jmcomic_tool import set_file_ready_callback   # 复刻 main_window.py:403
print("4) UI 直接 import 后                    :", snap())

ti = ToolInfo(name="jmcomic", description="", parameters={},
              module_path=str(Path("tools/jmcomic_tool.py").resolve()),
              load_policy=ToolLoadPolicy.LAZY, manager=None)
ti._load_module()
print("5) ToolManager._load_module() 后        :", snap())
print("   _prts_tool_jmcomic is tools.jmcomic_tool ?",
      sys.modules.get("_prts_tool_jmcomic") is sys.modules.get("tools.jmcomic_tool"))
ti._unload_module()
print("6) ToolInfo._unload_module() 后         :", snap())
print("   'tools.jmcomic_tool' 还在 sys.modules 吗 ?",
      "tools.jmcomic_tool" in sys.modules)
print("   '_prts_tool_jmcomic' 还在 sys.modules 吗 ?",
      "_prts_tool_jmcomic" in sys.modules)
