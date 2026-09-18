# -*- coding: utf-8 -*-
"""独立验证子代理的发现：RESIDENT 工具被绑到「扫描副本」，
导致"禁用长期记忆"设置对执行路径无效。

不涉及网络/LLM：只比较两个 module 对象与两份单例的身份。
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DEEPSEEK_API_KEY", "test")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.core.tool_manager import get_tool_manager, ToolLoadPolicy  # noqa: E402

tm = get_tool_manager()
ti = tm._tools["long_term_memory"]
print("policy           =", ti.load_policy.value)

scan_mod = ti._module
print("_module.__name__ =", getattr(scan_mod, "__name__", None))
print("_module in sys.modules?  =",
      any(m is scan_mod for m in sys.modules.values()))
print("sys.modules 里有 _scan_long_term_memory_tool 吗? =",
      "_scan_long_term_memory_tool" in sys.modules)

import tools.long_term_memory_tool as official                 # noqa: E402
print("official.__name__        =", official.__name__)
print("扫描副本 is 正式模块 ?    =", scan_mod is official)

s_scan = scan_mod.get_long_term_memory_tool()
s_off = official.get_long_term_memory_tool()
print("两份 get_long_term_memory_tool() is 同一实例 ? =", s_scan is s_off)
print("两份 _tool_instance is 同一实例 ?              =",
      getattr(scan_mod, "_tool_instance", "N/A") is
      getattr(official, "_tool_instance", "N/A"))

print()
print("=== 核心验证：按 _sync_ltm_disabled 的写法改「正式模块」，看执行路径认不认 ===")
print("改之前： 正式单例 is_disabled =", s_off.is_disabled(),
      "  执行路径(扫描副本) is_disabled =", s_scan.is_disabled())

# 这正是 _sync_ltm_disabled 干的事（src/core/tool_manager.py:261-270）
official.get_long_term_memory_tool().set_disabled(True)

print("改之后： 正式单例 is_disabled =", s_off.is_disabled(),
      "  执行路径(扫描副本) is_disabled =", s_scan.is_disabled())
print()
if s_scan.is_disabled() is False and s_off.is_disabled() is True:
    print(">>> ★★问题成立：设置界面改的是「正式模块」的单例，")
    print(">>>   而 ToolManager 执行工具时走的是「扫描副本」的单例（仍为 False）")
    print(">>>   → 「禁用长期记忆」对执行路径无效")
else:
    print(">>> 问题不成立（两者一致）")

# 执行路径到底用哪份？直接看 _func 的 __globals__
print()
print("ti._func 的模块 globals 是扫描副本吗? =",
      ti._func.__globals__.get("_tool_instance") is
      getattr(scan_mod, "_tool_instance", None))
print("ti._func 的模块 globals 是正式模块吗? =",
      ti._func.__globals__.get("_tool_instance") is
      getattr(official, "_tool_instance", None))

# 恢复现场
official.get_long_term_memory_tool().set_disabled(False)

print()
print("=== 附带验证：LAZY 工具的扫描副本是白执行一遍后丢掉的吗 ===")
for nm in ("jmcomic", "pixiv"):
    t = tm._tools[nm]
    print(f"  {nm}: policy={t.load_policy.value} _module={t._module} _func={t._func}")
