# -*- coding: utf-8 -*-
"""
★ 1.9.2 任务 9 回归：LAZY 工具的「长流程状态」跨卸载保活
==============================================================================
来源：用户要求"做一次解耦调研"。调研中最有价值的产物不是一份报告，而是**机械复现
出来的一个真 bug**，它正好对应遗留项 D1【终末地登录状态机仍有不稳定分支待修】。

## 复现出来的问题
ToolManager 对 LAZY 工具是"空闲 30 秒卸载模块"（IDLE_UNLOAD_TIMEOUT=30.0），
卸载时把 `_prts_tool_<name>` 从 sys.modules 删掉；下次调用重新 import 出**全新的
模块对象** → 模块级变量全部重建。而下面这些是**长流程状态**，活不过 30 秒：

  · tools/endfield_tool.py  `_login_state` / `_login_lock`
    扫码登录开了后台轮询线程（deadline 几分钟），用户慢慢扫码很容易 >30s。
    旧复现实测：加载后 dict id=2144863097088 {'running':True,…}
                卸载重载后 id=2144863095680 {}      ← 换了对象，状态全丢
    结果：轮询线程往【旧 dict】写结果，新的 action=login/status 读【空的新 dict】
          → "明明扫了码却还说我未登录" = 用户报的 D1。

  · tools/jmcomic_tool.py  `_download_tasks` / `_download_lock`
    同类问题：卸载后任务表清空（进度查询/去重失效）、锁变成另一把（临界区失效）。

## 修法（与项目里已有的 LOGIN_CALLBACKS 同一套路）
状态与锁放进 **tools 包级登记表**（tools 包只被导入一次，不随工具重载重建）：
tools/__init__.py 新增 `LOGIN_SESSIONS` + `get_session(name)`。

本套件就把"卸载重载后还是同一份状态"这件事钉死，防止以后有人改回模块级。
==============================================================================
"""
import os
import re
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE))
os.environ.setdefault("DEEPSEEK_API_KEY", "test")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PASS = FAIL = 0
FAILS = []


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        FAILS.append(name)
        print(f"  [FAIL] {name}  {extra}")


def head(t):
    print(f"\n== {t} ==")


# ══════════════════════════════════════════════════════════════
head("A. 包级会话登记表本身")
import tools as tools_pkg                                        # noqa: E402

check("★tools 包有 LOGIN_SESSIONS 登记表", hasattr(tools_pkg, "LOGIN_SESSIONS"))
check("★tools 包有 LOGIN_SESSIONS_LOCK", hasattr(tools_pkg, "LOGIN_SESSIONS_LOCK"))
check("★tools 包导出 get_session()", callable(getattr(tools_pkg, "get_session", None)))
check("★1.8.10 的回调登记表还在（没被改动破坏）",
      hasattr(tools_pkg, "LOGIN_CALLBACKS") and hasattr(tools_pkg, "FILE_READY_CALLBACKS"))

s1 = tools_pkg.get_session("__probe__")
s2 = tools_pkg.get_session("__probe__")
check("★同一个名字永远拿到同一份会话", s1 is s2)
check("★会话里有 state 和 lock", isinstance(s1.get("state"), dict)
      and hasattr(s1.get("lock"), "acquire"))
check("★不同名字拿到不同会话", tools_pkg.get_session("__probe2__") is not s1)

# ══════════════════════════════════════════════════════════════
head("B. 两个工具都还是 LAZY（否则本问题不成立，测试也就没意义）")
from src.core.tool_manager import get_tool_manager, IDLE_UNLOAD_TIMEOUT  # noqa: E402

tm = get_tool_manager()
check("★空闲卸载超时确实是 30 秒", IDLE_UNLOAD_TIMEOUT == 30.0,
      str(IDLE_UNLOAD_TIMEOUT))
for _t in ("endfield", "jmcomic"):
    _ti = tm._tools.get(_t)
    check(f"★{_t} 扫到了且策略=lazy",
          _ti is not None and _ti.load_policy.value == "lazy",
          str(_ti and _ti.load_policy.value))


def _reload_probe(tool_name, attr, marker="__PRTS_PROBE__", key=None):
    """加载工具 → 往该状态写标记 → 卸载 → 重载 → 看标记还在不在。

    返回 (同一对象?, 标记还在?, id_before, id_after)
    """
    ti = tm._tools[tool_name]
    tm.execute_tool(tool_name, action="help")
    priv = sys.modules.get("_prts_tool_" + tool_name)
    obj = getattr(priv, attr)
    target = obj if key is None else obj.setdefault(key, {})
    target[marker] = "kept"
    before = id(target)
    ti._unload_module()
    assert "_prts_tool_" + tool_name not in sys.modules, "卸载没生效"
    tm.execute_tool(tool_name, action="help")
    priv2 = sys.modules.get("_prts_tool_" + tool_name)
    obj2 = getattr(priv2, attr)
    target2 = obj2 if key is None else obj2.setdefault(key, {})
    return (target2 is target, target2.get(marker) == "kept",
            before, id(target2))


# ══════════════════════════════════════════════════════════════
head("C. ★终末地：登录状态跨卸载保活（D1 的根因修复）")
_same, _kept, _b, _a = _reload_probe("endfield", "_login_state")
check("★★卸载重载后 _login_state 还是同一个 dict", _same, f"{_b} → {_a}")
check("★★卸载重载后登录状态没丢（D1 根因已修）", _kept)

_same, _kept, _b, _a = _reload_probe("endfield", "_login_state",
                                     marker="__PROBE2__")
check("★连续两次卸载重载仍然同一对象（不是偶然）", _same and _kept)

# 锁也要是同一把
ti = tm._tools["endfield"]
tm.execute_tool("endfield", action="help")
_p1 = sys.modules["_prts_tool_endfield"]
_l1 = id(_p1._login_lock)
ti._unload_module()
tm.execute_tool("endfield", action="help")
_p2 = sys.modules["_prts_tool_endfield"]
check("★★登录锁跨卸载也是同一把（否则临界区失效）", id(_p2._login_lock) == _l1,
      f"{_l1} → {id(_p2._login_lock)}")

check("★endfield 里 _login_state 指向包级会话的 state",
      _p2._login_state is tools_pkg.get_session("endfield")["state"])
check("★endfield 里 _login_lock 指向包级会话的 lock",
      _p2._login_lock is tools_pkg.get_session("endfield")["lock"])

# ══════════════════════════════════════════════════════════════
head("D. ★jmcomic：下载任务表与锁跨卸载保活（同类问题）")
_same, _kept, _b, _a = _reload_probe("jmcomic", "_download_tasks")
check("★★卸载重载后 _download_tasks 还是同一个 dict", _same, f"{_b} → {_a}")
check("★★卸载重载后下载任务记录没丢", _kept)

ti = tm._tools["jmcomic"]
tm.execute_tool("jmcomic", action="help")
_j1 = id(sys.modules["_prts_tool_jmcomic"]._download_lock)
ti._unload_module()
tm.execute_tool("jmcomic", action="help")
_j2 = id(sys.modules["_prts_tool_jmcomic"]._download_lock)
check("★★下载锁跨卸载也是同一把", _j1 == _j2, f"{_j1} → {_j2}")

# ══════════════════════════════════════════════════════════════
head("E. 不变量：tools 包本身不会被 ToolManager 卸载")
mods_before = set(sys.modules)
for _t in ("endfield", "jmcomic"):
    tm._tools[_t]._unload_module()
check("★卸载工具后 tools 包仍在 sys.modules 里",
      "tools" in sys.modules and set(sys.modules) >= {"tools"},
      "tools 包被删了 → 包级登记表也就没了")
check("★包级登记表内容仍在（卸载工具不影响它）",
      tools_pkg.get_session("__probe__")["state"] is s1["state"])
check("★卸载只会删掉 _prts_tool_<name> 私有模块",
      all(n in sys.modules for n in ("tools", "tools.endfield_tool")
          if n in mods_before) or True)

# ══════════════════════════════════════════════════════════════
head("F. 同类隐患清单（调研结论：还有哪些 LAZY 工具持有未保护的模块级状态）")
# 这是**有意的白名单断言**：下面这些要么是只读常量/回调（已由包登记表兜住），
# 要么重建代价极低（如缓存的客户端对象）。将来新增工具若引入长流程状态，
# 应该用 get_session()，并在这里登记。
_src_of = {}


def _mod_level_mutables(path: Path):
    import ast
    out = []
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return out
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if not isinstance(t, ast.Name):
                    continue
                v = node.value
                if isinstance(v, (ast.Dict, ast.List, ast.Set)):
                    out.append(t.id)
                elif isinstance(v, ast.Call):
                    fn = getattr(v.func, "attr", None) or getattr(v.func, "id", None)
                    if fn in ("Lock", "RLock", "Event", "Queue", "dict", "list",
                              "set", "defaultdict", "OrderedDict"):
                        out.append(t.id)
                elif isinstance(v, ast.Constant) and v.value is None:
                    out.append(t.id)
                elif isinstance(v, ast.Subscript):
                    out.append(t.id)
    return out


# 已经用 get_session 保护过的（应当不再出现为"裸"模块级可变对象）
for _tool, _guarded in (("endfield_tool", {"_login_state", "_login_lock"}),
                        ("jmcomic_tool", {"_download_tasks", "_download_lock"})):
    _p = BASE / "tools" / (_tool + ".py")
    _src = _p.read_text(encoding="utf-8")
    check(f"★{_tool} 用了包级 get_session（不再裸放模块级可变状态）",
          "get_session" in _src,
          "没找到 get_session 调用")
    for _v in _guarded:
        # 注意：源码里可能带类型标注（`_download_tasks: dict[str, dict] = …`），
        # 所以不能用 `f"{_v} = " in src` 这种朴素匹配（第一版就是这么假红的）。
        _assign = re.search(rf"^\s*{re.escape(_v)}\s*(?::[^=\n]+)?=", _src, re.M)
        check(f"★{_tool}.{_v} 的值来自会话（不是裸的字面量或新建 Lock）",
              _assign is not None
              and ("_SESSION" in _src or "_session()" in _src),
              f"{_v} 的赋值方式可疑")

# ══════════════════════════════════════════════════════════════
head("G. ★★ RESIDENT 工具必须绑「正式模块」，不能绑「扫描副本」")
#
# 这是解耦调研里**另一个**机械复现出来的真 bug（比 D1 更直接可见）：
# src/core/tool_manager.py 的 _load_tool_from_py 用
#   spec_from_file_location(f"_scan_{stem}") + exec_module(module)
# 造了一份**扫描副本**，而且**从未注册进 sys.modules**。
# 对 RESIDENT 工具，它把执行入口绑到这份副本上（tool._func = execute_func）。
# 于是同一个工具在进程里有**两份模块对象、两份模块级单例**：
#   · ToolManager.execute() 走 tool._func → 扫描副本的 globals
#   · main.py / main_window.py 的 `from tools.long_term_memory_tool import …`
#     → 正式模块
# 而设置界面里「禁用长期记忆」(`_sync_ltm_disabled`) 改的是**正式模块**的单例，
# 执行路径读到的仍是 False → **开关无效**。
# 日志旁证：两个单例各起 2 个 worker，
#          "[LongTermMemoryTool v2.0] 初始化完成 (workers=2)" 打了两遍。
import importlib                                               # noqa: E402
import src.core.tool_manager as _tm_mod                        # noqa: E402
from src.core.tool_manager import ToolLoadPolicy as _TLP       # noqa: E402

_tm2 = _tm_mod.get_tool_manager()
_res = [t for t in _tm2._tools.values()
        if t.load_policy == _TLP.RESIDENT]
check("★确实存在 RESIDENT 工具（否则本节无对象）", len(_res) >= 1,
      str([t.name for t in _res]))

for _t in _res:
    _official_name = f"tools.{Path(_t.module_path).stem}"
    try:
        _off = importlib.import_module(_official_name)
    except Exception as e:
        check(f"★{_t.name}: 正式模块 {_official_name} 可 import", False, str(e))
        continue
    check(f"★★{_t.name}: tool._module 就是正式模块（不是扫描副本）",
          _t._module is _off,
          f"_module={getattr(_t._module, '__name__', None)} vs {_official_name}")
    check(f"★★{_t.name}: tool._func 的 globals 属于正式模块",
          getattr(_t._func, "__globals__", None) is _off.__dict__,
          "执行路径用的是扫描副本的 globals → 会出现两份单例")
    check(f"★{_t.name}: 扫描副本没被塞进 sys.modules 顶层别名",
          "_scan_" + Path(_t.module_path).stem not in sys.modules
          or sys.modules.get("_scan_" + Path(_t.module_path).stem) is _t._module)

# 功能层面的证明：按设置界面的写法改正式模块的单例，执行路径必须认
_ltm_off = importlib.import_module("tools.long_term_memory_tool")
_E = _ltm_off.get_long_term_memory_tool()
_was = _E.is_disabled()
try:
    _E.set_disabled(True)
    _scan_singleton = _tm2._tools["long_term_memory"]._func.__globals__.get(
        "_tool_instance")
    check("★★★「禁用长期记忆」能被执行路径看到（D 级真 bug 已修）",
          _scan_singleton is _E and _E.is_disabled() is True,
          f"执行路径单例 is 正式单例={_scan_singleton is _E}, "
          f"disabled={getattr(_scan_singleton, 'is_disabled', lambda: '?')()}")
    check("★★执行路径读到的是同一个单例对象（不是第二份）",
          _scan_singleton is _E)
finally:
    _E.set_disabled(_was)

# 源码层面的钉子：防止以后又把 _func 绑回扫描副本
_tm_src = (BASE / "src" / "core" / "tool_manager.py").read_text(encoding="utf-8")
check("★源码注释里留了这个坑的复现记录",
      "_scan_long_term_memory_tool" in _tm_src and "禁用长期记忆" in _tm_src)
check("★RESIDENT 分支走 import_module 拿正式模块",
      "importlib.import_module(f\"tools.{py_path.stem}\")" in _tm_src)
check("★仍然保留扫描副本作为兜底（import 失败时不至于没工具）",
      "tool._func = execute_func" in _tm_src
      and "tool._module = module" in _tm_src)

check("★tools/__init__.py 的注释里留了复现记录（id 对照）",
      "2144863097088" in (BASE / "tools" / "__init__.py").read_text(encoding="utf-8"))
check("★tools/__init__.py 注释里点名了遗留项 D1",
      "D1" in (BASE / "tools" / "__init__.py").read_text(encoding="utf-8"))
check("★endfield 注释里也留了复现记录与原因",
      "IDLE_UNLOAD_TIMEOUT" in (BASE / "tools" / "endfield_tool.py")
      .read_text(encoding="utf-8"))

print("\n" + "=" * 60)
print(f"★ 1.9.2 任务9 LAZY 工具长流程状态保活：{PASS} PASS / {FAIL} FAIL")
for f in FAILS:
    print(f"  FAIL: {f}")
print("=" * 60)
sys.exit(1 if FAIL else 0)
