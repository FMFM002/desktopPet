"""
工具管理器 v2.0
- 启动时扫描 tools/ 目录（不导入模块，只读元数据）
- 支持三种加载策略: RESIDENT(常驻) / LAZY(按需加载) / DISABLED(禁用)
- LAZY 工具调用后 30s 无复用则自动卸载模块释放内存
- 长期记忆工具默认 RESIDENT（监控系统频繁调用）
"""
import json
import logging
import importlib
import importlib.util
import sys
import threading
import time
from enum import Enum
from pathlib import Path
from typing import Optional

_log = logging.getLogger("ToolManager")

BASE_DIR = Path(__file__).resolve().parent.parent.parent
TOOLS_DIR = BASE_DIR / "tools"

# 空闲卸载超时（秒）
IDLE_UNLOAD_TIMEOUT = 30.0


class ToolLoadPolicy(str, Enum):
    """工具加载策略"""
    RESIDENT = "resident"   # 常驻内存，永不卸载
    LAZY = "lazy"           # 调用时加载，30s 空闲后卸载
    DISABLED = "disabled"   # 禁用，不加载


# 默认策略配置（可通过 config.json 的 tool_policies 覆盖）
DEFAULT_TOOL_POLICIES: dict[str, ToolLoadPolicy] = {
    "long_term_memory": ToolLoadPolicy.RESIDENT,  # 监控系统频繁调用
}
DEFAULT_POLICY = ToolLoadPolicy.LAZY


class ToolInfo:
    """工具信息"""

    def __init__(self, name: str, description: str, parameters: dict,
                 module_path: str, enabled: bool = True, icon: str = "?",
                 load_policy: ToolLoadPolicy = ToolLoadPolicy.LAZY,
                 manager: Optional['ToolManager'] = None):
        self.name = name
        self.description = description
        self.parameters = parameters
        self.module_path = module_path
        self.enabled = enabled
        self.icon = icon
        self.load_policy = load_policy
        self._manager = manager  # 反向引用，用于触发 post-load hooks

        # 运行时状态
        self._func = None          # execute 函数引用
        self._module = None        # 加载后的 module 对象
        self._last_used: float = 0.0  # 最后使用时间戳
        self._lock = threading.Lock()

    def to_openai_tool(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            }
        }

    def is_loaded(self) -> bool:
        return self._func is not None

    def _load_module(self):
        """加载模块（线程安全）

        ★ 1.6.0: 以前直接用工具名注册 sys.modules[self.name]，如果工具名恰好和
        第三方库同名，就会**把真库顶掉**。实测：jmcomic 工具注册成 sys.modules['jmcomic']，
        于是工具内部 `import jmcomic` 拿到的是工具自己 → "cannot import name
        'JmModuleConfig' from 'jmcomic'（指向 jmcomic_tool.py）" → 被误判成"库未安装"。
        现在改为注册到 _prts_tool_<name>，只有在名字没被占用时才额外保留旧别名。
        """
        with self._lock:
            if self._func is not None:
                return  # 已加载
            try:
                internal_name = f"_prts_tool_{self.name}"
                spec = importlib.util.spec_from_file_location(internal_name,
                                                              self.module_path)
                module = importlib.util.module_from_spec(spec)
                sys.modules[internal_name] = module
                # 兼容旧行为：仅当该名字还没被真实模块占用时才注册别名
                if self.name not in sys.modules:
                    sys.modules[self.name] = module
                else:
                    _log.debug(f"工具 [{self.name}] 与已加载模块同名，跳过别名注册"
                               f"（避免顶掉真实库）")
                spec.loader.exec_module(module)
                self._module = module
                # ★ 1.6.0: 兜底清掉同名占用 —— 扫描阶段曾把工具模块挂到过
                #   sys.modules['<工具名>']，会把真实第三方库顶掉（jmcomic 即如此）
                if sys.modules.get(self.name) is module:
                    try:
                        del sys.modules[self.name]
                        _log.debug(f"工具 [{self.name}] 清掉同名 sys.modules 占用")
                    except KeyError:
                        pass
                self._func = getattr(module, "execute", None) or getattr(module, "run", None)
                _log.info(f"工具 [{self.name}] 加载完成 (策略={self.load_policy.value})")
                # ★ 触发 post-load hooks（恢复模块级回调等状态）
                if self._manager:
                    self._manager._run_post_load_hooks(self.name)
            except Exception as e:
                _log.error(f"工具 {self.name} 加载失败: {e}")
                raise

    def _unload_module(self):
        """卸载模块释放内存（线程安全）"""
        with self._lock:
            if self._module is None:
                return
            _log.debug(f"工具 [{self.name}] 卸载（空闲超时）")
            self._func = None
            # ★ 1.6.0: 只清理我们自己的内部名与指向本模块的别名，
            #   绝不误删同名真实库
            for key in (f"_prts_tool_{self.name}", self.name):
                mod = sys.modules.get(key)
                if mod is self._module:
                    try:
                        del sys.modules[key]
                    except KeyError:
                        pass
            self._module = None
        # ★ 1.9.1: 卸载后钩子（锁外执行，钩子里可能要停进程/发请求，别占着工具锁）
        #   用途：像 comfyui 这种"工具背后挂着一个外部服务"的，
        #   要按**和其他工具一样的空闲卸载规则**把外部服务也收掉
        #   （用户原话："走其他tool一样的卸载规则。comfyUI和模型同加载同卸载，不用分开。"）
        if self._manager:
            self._manager._run_post_unload_hooks(self.name)

    def execute(self, **kwargs) -> str:
        """执行工具（按需加载 + 记录时间戳）"""
        # DISABLED 工具不执行
        if self.load_policy == ToolLoadPolicy.DISABLED:
            return f"[工具 {self.name} 已禁用]"

        if self._func is None:
            try:
                self._load_module()
            except Exception as e:
                return f"[工具加载失败: {self.name}] {e}"

        if self._func is None:
            return f"[工具 {self.name} 未定义 execute 函数]"

        # ★ 1.9.3 第 25 轮：这里只标"开始用"是不够的 —— 见下面 finally。
        self._last_used = time.time()

        try:
            result = self._func(**kwargs)
            return str(result) if result is not None else "执行完成"
        except TypeError as e:
            _log.error(f"工具 {self.name} 参数错误: {e}, kwargs={list(kwargs.keys())}")
            try:
                result = self._func()
                return str(result) if result is not None else "执行完成"
            except Exception as e2:
                _log.error(f"工具 {self.name} 无参数调用也失败: {e2}")
                return f"[工具参数错误: {self.name}] {e2}"
        except Exception as e:
            _log.error(f"工具 {self.name} 执行出错: {e}", exc_info=True)
            return f"[工具执行出错: {self.name}] {e}"
        finally:
            # ★★ 1.9.3 第 25 轮（真机 bug）：**调用结束也要重新盖章**。
            #   旧代码只在**调用前**盖一次 `_last_used`，于是一个**跑得比空闲阈值还久**
            #   的调用，一返回就已经"过期"了 —— 空闲检查线程 5 秒后一转就把它卸载。
            #   真机现场（logs/prts_20261004_025634.log）：
            #     03:03:39 `comfyui start`（内部要等 ComfyUI 起来，耗时 30 秒）
            #     03:04:09 返回"已就绪"
            #     03:04:12 `工具 [comfyui] 卸载（空闲超时）` → 卸载钩子**把刚拉起来的
            #              ComfyUI 关掉了** → 模型以为它还开着，结果一张图都没画成。
            #   盖在 finally 里：不管成功、失败、抛异常，都从"这次真的用完了"开始计时。
            self._last_used = time.time()

    def idle_seconds(self) -> float:
        """距离最后使用的空闲秒数"""
        if self._last_used == 0.0 or not self.is_loaded():
            return 0.0
        return time.time() - self._last_used


class ToolManager:
    """工具管理器 v2.0 — 支持加载策略 + 空闲自动卸载"""

    def __init__(self):
        self._tools: dict[str, ToolInfo] = {}
        self._idle_check_running = False
        self._idle_check_thread: Optional[threading.Thread] = None
        self._scan_lock = threading.Lock()

        # 从配置加载策略
        self._tool_policies: dict[str, ToolLoadPolicy] = {}
        self._load_policies_from_config()

        # 模块加载后回调注册表（tool_name → [callables]）
        # 用于在模块因 LAZY 策略卸载后重载时恢复模块级状态
        self._post_load_hooks: dict[str, list] = {}
        # ★ 1.9.1: 卸载后钩子（与 post_load 对称）—— 见 register_post_unload_hook
        self._post_unload_hooks: dict[str, list] = {}

        self.scan_tools()
        self._start_idle_checker()

    # ---- 策略配置 ----

    def _load_policies_from_config(self):
        """从 config.json 加载工具加载策略"""
        try:
            from src.utils.config import get_config
            cfg = get_config()
            if hasattr(cfg, '_raw_data') and 'tool_policies' in cfg._raw_data:
                raw = cfg._raw_data['tool_policies']
                for name, policy_str in raw.items():
                    if name == "_default":
                        continue
                    try:
                        self._tool_policies[name] = ToolLoadPolicy(policy_str)
                    except ValueError:
                        _log.warning(f"无效的加载策略 '{policy_str}' for {name}")
            elif hasattr(cfg, 'tool_policies'):
                for name, policy_str in cfg.tool_policies.items():
                    try:
                        self._tool_policies[name] = ToolLoadPolicy(policy_str)
                    except ValueError:
                        _log.warning(f"无效的加载策略 '{policy_str}' for {name}")
        except Exception as e:
            _log.debug(f"工具策略配置加载跳过: {e}")

    def _get_policy(self, tool_name: str) -> ToolLoadPolicy:
        """获取工具的加载策略（配置优先，其次默认策略，最后 DEFAULT_POLICY）"""
        if tool_name in self._tool_policies:
            return self._tool_policies[tool_name]
        if tool_name in DEFAULT_TOOL_POLICIES:
            return DEFAULT_TOOL_POLICIES[tool_name]
        return DEFAULT_POLICY

    def set_policy(self, tool_name: str, policy: ToolLoadPolicy) -> bool:
        """运行时修改工具的加载策略。仅当策略真正变化时才执行操作。"""
        if tool_name not in self._tools:
            return False
        old_policy = self._tools[tool_name].load_policy
        if old_policy == policy:
            return True  # 没变化，跳过

        self._tool_policies[tool_name] = policy
        self._tools[tool_name].load_policy = policy
        _log.info(f"工具 [{tool_name}] 策略变更: {old_policy.value} → {policy.value}")

        # 如果改为 DISABLED，立即卸载
        if policy == ToolLoadPolicy.DISABLED and self._tools[tool_name].is_loaded():
            self._tools[tool_name]._unload_module()

        # ★ 同步 LTM 单例的禁用状态
        self._sync_ltm_disabled(tool_name, policy)

        # 保存到配置文件
        self._save_policies()
        return True

    def _sync_ltm_disabled(self, tool_name: str, policy: ToolLoadPolicy):
        """同步长期记忆工具单例的禁用状态"""
        if tool_name == "long_term_memory":
            try:
                from tools.long_term_memory_tool import get_long_term_memory_tool
                ltm = get_long_term_memory_tool()
                ltm.set_disabled(policy == ToolLoadPolicy.DISABLED)
                _log.debug(f"LTM 单例状态已同步: disabled={policy == ToolLoadPolicy.DISABLED}")
            except Exception as e:
                _log.debug(f"LTM 单例同步跳过: {e}")

    def get_policy_map(self) -> dict[str, str]:
        """获取所有工具的当前策略（供设置窗口读取）"""
        result = {}
        for name, tool in self._tools.items():
            result[name] = tool.load_policy.value
        return result

    def register_post_load_hook(self, tool_name: str, hook: callable):
        """注册模块加载后回调。
        当 LAZY 工具被卸载后重新加载时，这些回调会被调用以恢复模块级状态
        （如 jmcomic 的 set_file_ready_callback）。
        """
        if tool_name not in self._post_load_hooks:
            self._post_load_hooks[tool_name] = []
        self._post_load_hooks[tool_name].append(hook)

    def _run_post_load_hooks(self, tool_name: str):
        """执行模块加载后回调"""
        for hook in self._post_load_hooks.get(tool_name, []):
            try:
                hook()
            except Exception as e:
                _log.debug(f"Post-load hook for [{tool_name}] failed: {e}")

    # ── ★ 1.9.1: 卸载后钩子（与 post_load 对称）──

    def register_post_unload_hook(self, tool_name: str, hook: callable):
        """注册模块**卸载后**回调。

        给"工具背后挂着一个外部服务/进程"的场景用：LAZY 工具空闲卸载时，
        外部服务也该跟着收掉 —— 这样它就自动获得了**和其他工具一样的生命周期**，
        不需要再自己搞一套空闲计时器。

        第一个用户是 comfyui（用户原话："走其他tool一样的卸载规则。
        comfyUI和模型同加载同卸载，不用分开。"）。
        """
        if tool_name not in self._post_unload_hooks:
            self._post_unload_hooks[tool_name] = []
        self._post_unload_hooks[tool_name].append(hook)

    def _run_post_unload_hooks(self, tool_name: str):
        """执行模块卸载后回调（包一层 try，钩子炸了不能影响卸载本身）"""
        hooks = self._post_unload_hooks.get(tool_name, [])
        for hook in hooks:
            try:
                # ★ 放后台线程跑：钩子里可能要停进程 / 发 HTTP，
                #   而 _unload_module 是在"空闲检查线程"或工具调用路径上被调的，
                #   不能让一次关机卡住整个工具系统。
                threading.Thread(target=hook, daemon=True,
                                 name=f"post-unload-{tool_name}").start()
            except Exception as e:
                _log.debug(f"Post-unload hook for [{tool_name}] failed: {e}")

    def _save_policies(self):
        """保存策略到 config.json"""
        try:
            from src.utils.config import get_config
            cfg = get_config()
            cfg.tool_policies = {k: v.value for k, v in self._tool_policies.items()}
            cfg.save()
        except Exception as e:
            _log.warning(f"保存工具策略失败: {e}")

    # ---- 扫描 ----

    def scan_tools(self):
        with self._scan_lock:
            self._tools.clear()

            if not TOOLS_DIR.exists():
                TOOLS_DIR.mkdir(parents=True, exist_ok=True)
                _log.warning("tools/ 目录为空,已创建")
                return

            _log.info(f"扫描工具目录: {TOOLS_DIR}")

            for item in sorted(TOOLS_DIR.iterdir()):
                if item.is_dir():
                    manifest_path = item / "manifest.json"
                    if manifest_path.exists():
                        try:
                            tool = self._load_tool_from_manifest(item, manifest_path)
                            if tool:
                                self._tools[tool.name] = tool
                                _log.info(f"  [+] {tool.name}: {tool.description[:40]} "
                                          f"[{tool.load_policy.value}]")
                        except Exception as e:
                            _log.error(f"  [!] 加载失败 {item.name}: {e}")
                elif item.suffix == ".py" and item.stem != "__init__":
                    try:
                        tool = self._load_tool_from_py(item)
                        if tool:
                            self._tools[tool.name] = tool
                            _log.info(f"  [+] {tool.name}: {tool.description[:40]} "
                                      f"[{tool.load_policy.value}]")
                    except Exception as e:
                        _log.error(f"  [!] {item.name}: {e}")

            _log.info(f"共扫描 {len(self._tools)} 个工具: {list(self._tools.keys())}")

    def _load_tool_from_manifest(self, dir_path: Path, manifest_path: Path) -> Optional[ToolInfo]:
        with open(manifest_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        name = data.get("name", dir_path.name)
        description = data.get("description", "未提供描述")
        parameters = data.get("parameters", {
            "type": "object", "properties": {}, "required": []})

        entry = data.get("entry", "__init__.py")
        module_path = dir_path / entry
        if not module_path.exists():
            module_path = dir_path / "main.py"
        if not module_path.exists():
            py_files = list(dir_path.glob("*.py"))
            module_path = py_files[0] if py_files else None
        if module_path is None:
            return None

        return ToolInfo(
            name=name, description=description, parameters=parameters,
            module_path=str(module_path),
            enabled=data.get("enabled", True), icon=data.get("icon", "?"),
            load_policy=self._get_policy(name),
            manager=self)

    def _load_tool_from_py(self, py_path: Path) -> Optional[ToolInfo]:
        """扫描 .py 工具文件 — 不导入模块，只读模块级常量"""
        try:
            # 轻量扫描：只解析模块级 TOOL_NAME/TOOL_DESCRIPTION/TOOL_PARAMETERS
            spec = importlib.util.spec_from_file_location(f"_scan_{py_path.stem}", str(py_path))
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)

            name = getattr(module, "TOOL_NAME", py_path.stem)
            description = getattr(module, "TOOL_DESCRIPTION", "未提供描述")
            parameters = getattr(module, "TOOL_PARAMETERS", {
                "type": "object", "properties": {}, "required": []})
            execute_func = getattr(module, "execute", None) or getattr(module, "run", None)

            # ★ 1.8.10: 没有 execute/run 的文件不是工具插件（例如 ltm_summarizer、
            #   reset_failed_ltm、run_ltm_summarize 只是脚本）。
            #   以前照样把它们注册成"无参数工具"，模型调了只会拿到
            #   "[工具 X 未定义 execute 函数]"，还可能反复重试 ——
            #   而且它们的名字会被写进 config.json 的 enabled_tools/tool_policies。
            if execute_func is None:
                _log.debug(f"跳过 {py_path.name}：没有 execute/run，不是工具插件")
                return None

            tool = ToolInfo(
                name=name, description=description, parameters=parameters,
                module_path=str(py_path), enabled=True,
                load_policy=self._get_policy(name),
                manager=self)

            # ★ 对于 RESIDENT 工具，立即预加载
            #
            # ★★ 1.9.2 修复（解耦调研机械复现出来的真 bug）：
            #   这里以前绑的是上面那份 `module` —— 它是
            #   `spec_from_file_location(f"_scan_{stem}")` 造出来的**扫描副本**，
            #   而且**从未注册进 sys.modules**。于是同一个工具在进程里存在**两份
            #   模块对象、两份模块级单例**：
            #       · `ToolManager.execute()` 走 `tool._func` → 扫描副本的 globals
            #       · `main.py` / `main_window.py` 的
            #         `from tools.long_term_memory_tool import …` → 正式模块
            #   而 `_sync_ltm_disabled()`（设置界面里那个「禁用长期记忆」开关）
            #   改的是**正式模块**的单例 → 执行路径读到的仍是 False。
            #   实测（复现脚本）：
            #       扫描副本 __name__ = _scan_long_term_memory_tool（不在 sys.modules）
            #       两份 get_long_term_memory_tool() is 同一实例 = False
            #       改正式单例 disabled=True 后：正式=True，执行路径=False
            #       → 「禁用长期记忆」对执行路径无效
            #   （日志旁证：两个单例各起 2 个 worker，"[LongTermMemoryTool v2.0]
            #     初始化完成 (workers=2)" 打了两遍。）
            #
            #   修法：RESIDENT 工具改绑**正式模块**（按 `tools.<文件名>` 正常 import）。
            #   这样 `tool._func` 与设置界面操作的是同一个模块对象。
            #   LAZY 工具不受影响 —— 它们本来就走 `_load_module()`，
            #   那份是注册进 sys.modules 的 `_prts_tool_<name>`，没有这个问题。
            if tool.load_policy == ToolLoadPolicy.RESIDENT:
                _official = None
                try:
                    _official = importlib.import_module(f"tools.{py_path.stem}")
                except Exception as e:
                    _log.debug(f"RESIDENT 工具 [{name}] 正式模块 import 失败"
                               f"（退回扫描副本）: {e}")
                if _official is not None and callable(
                        getattr(_official, "execute", None)):
                    tool._func = _official.execute
                    tool._module = _official
                    if _official is not module:
                        _log.debug(
                            f"RESIDENT 工具 [{name}] 已绑定正式模块 "
                            f"tools.{py_path.stem}（不用扫描副本 —— 否则会有"
                            f"两份单例，禁用开关失效）")
                else:
                    tool._func = execute_func
                    tool._module = module
                tool._last_used = time.time()

            return tool
        except Exception as e:
            _log.error(f"扫描工具 {py_path.name} 失败: {e}")
            return None

    # ---- 空闲卸载检查 ----

    def _start_idle_checker(self):
        """启动后台线程，定期检查并卸载空闲 LAZY 工具"""
        if self._idle_check_running:
            return
        # ★ 1.9.0: 用 Event 做**可唤醒**的等待（原来是裸 time.sleep(5)，见 _stop 的说明）
        self._idle_stop_event = threading.Event()
        self._idle_check_running = True
        self._idle_check_thread = threading.Thread(
            target=self._idle_check_loop, daemon=True, name="tool-idle-check")
        self._idle_check_thread.start()

    def _stop_idle_checker(self, timeout: float = 3.0):
        """★ 1.9.0: 请求**并等待**空闲检查线程真的退出。

        真机教训（2026-09-16，faulthandler 抓到的原始堆栈）：
            Windows fatal exception: access violation
              File "src/core/tool_manager.py", line 418 in _idle_check_loop
              File "threading.py", line 1012 in run
            Current thread ...: _test_a190.py, line 1698 in <module>   ← os._exit()
        这个线程是 daemon，主线程退出（或 os._exit）时它可能正好在 `time.sleep(5)`
        或遍历工具表 —— 两边抢着拆解释器 → 访问违例 → **Windows 弹出
        "python.exe - 应用程序错误" 模态框**，而框没人点，进程就永远不退出
        （这就是历次"跑测试挂死、控制台卡住、剩一堆 0 内存僵尸进程"的同一个根因）。

        现在：置停止标志 + `Event.set()` 立刻唤醒等待 + `join()` 等它真的走人，
        所以退出时不再有线程在背后跑。
        """
        self._idle_check_running = False
        ev = getattr(self, "_idle_stop_event", None)
        if ev is not None:
            try:
                ev.set()
            except Exception:
                pass
        th = getattr(self, "_idle_check_thread", None)
        if th is not None and th.is_alive():
            try:
                th.join(timeout)
            except Exception:
                pass

    def _idle_check_loop(self):
        """每 5s 检查一次，卸载空闲超时的 LAZY 工具（可被 stop 立刻唤醒）"""
        ev = getattr(self, "_idle_stop_event", None)
        while self._idle_check_running:
            # 可唤醒的等待：停止时立即返回，不再傻等满 5 秒
            if ev is not None:
                if ev.wait(5):
                    break
            else:
                time.sleep(5)
            if not self._idle_check_running:
                break
            try:
                for tool in list(self._tools.values()):
                    if (tool.load_policy == ToolLoadPolicy.LAZY
                            and tool.is_loaded()
                            and tool.idle_seconds() > IDLE_UNLOAD_TIMEOUT):
                        tool._unload_module()
            except Exception as e:      # 单次出错不该把整个线程带走
                _log.debug(f"空闲卸载检查出错（忽略）: {e}")

            # ★ 1.9.2（用户要求）：顺手做一次"大对象空闲回收"。
            #   用户原话（针对 easyocr 常驻 776MB）："这个直接一样的30s后卸载就行了"
            #   —— 就是挂在同一个空闲清扫线程上，用同一条 30 秒规则，
            #   不额外起线程（多起线程反而会抱住旧对象、让内存释放不掉）。
            self._sweep_idle_big_objects()

    def _sweep_idle_big_objects(self):
        """回收"用一次就长期占着"的大对象（目前只有 easyocr 的 Reader）。

        设计成"钩子式"：具体实现放在各自模块里（`release_idle_reader()`），
        这里只负责按空闲规则去问一句"你该释放了吗"。
        新增别的贵对象时，在这里加一行即可。
        """
        try:
            from src.core.llm_client import get_vision_client
            vc = get_vision_client()
            eng = getattr(vc, "_ocr_engine", None)
            if eng is not None and hasattr(eng, "release_idle_reader"):
                eng.release_idle_reader()
        except Exception as e:
            _log.debug(f"大对象空闲回收出错（忽略）: {e}")

    # ---- 公共 API ----

    def get_tool(self, name: str) -> Optional[ToolInfo]:
        return self._tools.get(name)

    def get_enabled_tools(self) -> list[ToolInfo]:
        return [t for t in self._tools.values()
                if t.enabled and t.load_policy != ToolLoadPolicy.DISABLED]

    def get_openai_tools(self) -> list[dict]:
        return [t.to_openai_tool() for t in self.get_enabled_tools()]

    def get_all_tools(self) -> list[ToolInfo]:
        return list(self._tools.values())

    def execute_tool(self, tool_name: str, **kwargs) -> str:
        _log.info(f"执行工具: {tool_name}({list(kwargs.keys())})")
        # ★★ 1.9.4（用户 2026-10-06："强化log，最好全量记录"）：
        #   主日志这里**只记参数名**（`list(kwargs.keys())`）、结果只记前 100 字，
        #   出问题时**看不到参数值和结果全貌**。所以另外往全量记录里写一份。
        try:
            from src.utils import transcript as _T
            _T.tool_call(tool_name, kwargs)
        except Exception:
            pass
        tool = self.get_tool(tool_name)
        if tool is None:
            return f"[错误] 工具 '{tool_name}' 未找到"
        if not tool.enabled or tool.load_policy == ToolLoadPolicy.DISABLED:
            return f"[错误] 工具 '{tool_name}' 已禁用"
        # ★ 1.9.0（你的要求）：action 级禁用 —— 设置 → 工具设置 → 某工具 → 设置
        #   里勾了"禁用"的 action，这里直接挡掉，并回一句能让 LLM 换法子的提示。
        try:
            from src.utils.config import get_config
            off = (getattr(get_config().tools, "disabled_actions", {}) or {}).get(tool_name) or []
            act = kwargs.get("action")
            if act and str(act) in [str(x) for x in off]:
                _log.info(f"工具 {tool_name} 的 action「{act}」已被设置禁用，拒绝执行")
                # ★ 1.9.0（你的批注）：要加一句"用户已禁用此功能，可以提醒用户" ——
                #   不然 LLM 会闷头想方设法试别的方法，也不和用户交流。
                return (f"[错误] 工具 '{tool_name}' 的动作 '{act}' 已被用户在设置里禁用"
                        f"（用户已禁用此功能）。不要尝试绕过或改用其他方法 —— "
                        f"直接告诉用户：这个功能已被他禁用，"
                        f"需要的话可在「设置 → 工具设置 → {tool_name} → 设置」里重新开启。")
        except Exception as e:
            _log.debug(f"检查 action 禁用失败: {e}")
        # ★★ 1.9.4（用户要求）：累计"本轮工具调用数" —— 这是**临时汇报的
        #   代码级门槛**（<3 次调用不许汇报，见 `progress_bus._MIN_TOOL_CALLS`）。
        try:
            from src.core import progress_bus as _PB
            _PB.note_tool_called()
        except Exception:
            pass
        result = tool.execute(**kwargs)
        _log.debug(f"工具 {tool_name} 返回: {str(result)[:100]}")
        # ★★ 全量记录：结果**完整**落盘（主日志只留前 100 字，翻不出问题）
        try:
            from src.utils import transcript as _T2
            _T2.tool_result(tool_name, result,
                            error=str(result).lstrip().startswith("[错误]"))
        except Exception:
            pass
        return result

    def shutdown(self):
        """关闭管理器，卸载所有 LAZY 工具"""
        self._stop_idle_checker()
        for tool in self._tools.values():
            if tool.load_policy == ToolLoadPolicy.LAZY and tool.is_loaded():
                tool._unload_module()
        _log.info("工具管理器已关闭")


# ============================================================================
# 全局单例
# ============================================================================
_tool_manager: Optional[ToolManager] = None
_tm_lock = threading.Lock()


def get_tool_manager() -> ToolManager:
    global _tool_manager
    with _tm_lock:
        if _tool_manager is None:
            _tool_manager = ToolManager()
        return _tool_manager
