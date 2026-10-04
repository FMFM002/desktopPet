"""
增强输入监控模块 — 剪贴板 / 窗口元数据 / 进程元数据

基于素材项目 input_monitor.py 架构，适配当前项目 BaseMonitor 模式。
"""
import os
import re
import time
import threading
import logging
from datetime import datetime
from typing import Optional, Callable

_log = logging.getLogger("InputMonitor")

# ★ 1.9.3 第 36 轮：复用 monitor.py 的"可打断睡眠"（见该类注释）
from src.core.monitor import StoppableSleep


# ============================================================================
# PII 过滤器
# ============================================================================
class PIIFilter:
    PATTERNS = {
        "email": re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}"),
        "credit_card": re.compile(r"\b(?:\d[ -]*?){13,16}\b"),
        "phone": re.compile(r"\b\d{3}[-.\s]?\d{3}[-.\s]?\d{4}\b"),
        "api_key": re.compile(r"(?:sk-|api[_-]?key[=:]\s*)[a-zA-Z0-9_-]{20,}", re.IGNORECASE),
        "ssn": re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    }
    REPLACEMENTS = {"email": "[EMAIL]", "credit_card": "[CREDIT_CARD]",
                     "phone": "[PHONE]", "api_key": "[API_KEY]", "ssn": "[SSN]"}

    @classmethod
    def filter(cls, text: str) -> str:
        for key, pattern in cls.PATTERNS.items():
            text = pattern.sub(cls.REPLACEMENTS[key], text)
        return text


# ============================================================================
# 剪贴板监控
# ============================================================================
class ClipboardMonitor(StoppableSleep):
    """剪贴板变化监控 — 轮询 + 快捷键触发"""

    def __init__(self, poll_interval: float = 1.0, preview_max_len: int = 200):
        self.poll_interval = poll_interval
        self.preview_max_len = preview_max_len
        self._running = False
        self._init_stop_event()
        self._thread: Optional[threading.Thread] = None
        self._callback: Optional[Callable[[str, dict], None]] = None
        self._last_text: Optional[str] = None
        self._name = "clipboard"

    def set_callback(self, cb: Callable[[str, dict], None]):
        self._callback = cb

    def start(self):
        if self._running:
            return
        self._running = True
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="mon-clipboard")
        self._thread.start()
        _log.info("[clipboard] 剪贴板监控启动")

    def stop(self):
        self._running = False
        # ★ 1.9.3 第 36 轮：**先叫醒它**再去 join —— 否则就是干等 time.sleep 走完
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)

    def _run(self):
        while self._running:
            try:
                text = self._get_clipboard_text()
                if text and text != self._last_text:
                    self._last_text = text
                    filtered = PIIFilter.filter(text)
                    self._emit({
                        "type": "clipboard_change",
                        "content_type": "text",
                        "content_preview": filtered[:self.preview_max_len],
                        "content_length": len(text),
                        "trigger": "poll",
                        "timestamp": datetime.now().isoformat(),
                    })
            except Exception as e:
                _log.debug(f"[clipboard] 轮询异常: {e}")
            # ★ 第 36 轮：可打断睡眠（原来 stop() 要干等这次睡眠走完）
            if self._nap(self.poll_interval):
                break

    @staticmethod
    def _get_clipboard_text() -> Optional[str]:
        try:
            import pyperclip
            return pyperclip.paste()
        except ImportError:
            # ★ 1.8.10: root.destroy() 必须在 finally —— 剪贴板为空时
            #   root.clipboard_get() 会抛 TclError，原来被 except 吞掉、
            #   Tk 解释器却不销毁。这个函数每秒被调一次（mon-clipboard 线程），
            #   于是每失败一次就泄漏一个 Tk root/窗口句柄。
            root = None
            try:
                import tkinter as tk
                root = tk.Tk()
                root.withdraw()
                return root.clipboard_get()
            except Exception:
                return None
            finally:
                if root is not None:
                    try:
                        root.destroy()
                    except Exception:
                        pass

    def _emit(self, data: dict):
        if self._callback:
            try:
                self._callback(self._name, data)
            except Exception as e:
                _log.error(f"[clipboard] 回调错误: {e}")


# ============================================================================
# 窗口元数据监控
# ============================================================================
class WindowMetadataMonitor(StoppableSleep):
    """活动窗口 + 进程元数据监控（含浏览器 URL 检测）"""

    def __init__(self, poll_interval: float = 0.5):
        self.poll_interval = poll_interval
        self._running = False
        self._init_stop_event()
        self._thread: Optional[threading.Thread] = None
        self._callback: Optional[Callable[[str, dict], None]] = None
        self._last_app: Optional[str] = None
        self._last_title: Optional[str] = None
        self._last_url: Optional[str] = None
        self._name = "window_metadata"
        self._uia = None  # 延迟加载 uiautomation

    def set_callback(self, cb: Callable[[str, dict], None]):
        self._callback = cb

    def start(self):
        if self._running:
            return
        self._running = True
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="mon-window")
        self._thread.start()
        _log.info("[window_metadata] 窗口元数据监控启动")

    def stop(self):
        self._running = False
        # ★ 1.9.3 第 36 轮：**先叫醒它**再去 join —— 否则就是干等 time.sleep 走完
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)

    def _run(self):
        while self._running:
            try:
                app_name, window_title, process_path, pid, browser_url = self._get_active_window_info()
                changed = False
                data = {"type": "window_update", "app_name": app_name, "window_title": window_title,
                        "process_path": process_path, "pid": pid, "browser_url": browser_url,
                        "timestamp": datetime.now().isoformat()}

                if app_name and app_name != self._last_app:
                    data["event"] = "app_switch"
                    changed = True
                    self._last_app = app_name

                if window_title and window_title != self._last_title:
                    data["event"] = "window_focus"
                    changed = True
                    self._last_title = window_title

                if browser_url and browser_url != self._last_url:
                    data["event"] = "url_change"
                    changed = True
                    self._last_url = browser_url

                if changed:
                    self._emit(data)
            except Exception as e:
                _log.debug(f"[window_metadata] 轮询异常: {e}")
            # ★ 第 36 轮：可打断睡眠（原来 stop() 要干等这次睡眠走完）
            if self._nap(self.poll_interval):
                break

    def _get_active_window_info(self) -> tuple:
        app_name, window_title, process_path, pid, browser_url = None, None, None, None, None
        try:
            if os.name == "nt":
                import win32gui, win32process
                hwnd = win32gui.GetForegroundWindow()
                window_title = win32gui.GetWindowText(hwnd)
                _, pid = win32process.GetWindowThreadProcessId(hwnd)
                try:
                    import psutil
                    proc = psutil.Process(pid)
                    app_name = proc.name()
                    process_path = proc.exe()
                except Exception:
                    app_name = f"pid:{pid}"
                # 浏览器 URL 检测
                browser_url = self._get_browser_url(app_name, window_title)
            else:
                import pygetwindow as gw
                win = gw.getActiveWindow()
                if win:
                    window_title = win.title
                    app_name = window_title
        except ImportError:
            pass
        except Exception:
            pass
        return app_name, window_title, process_path, pid, browser_url

    def _get_browser_url(self, app_name: str, window_title: str) -> Optional[str]:
        """检测浏览器当前 URL（Windows UIAutomation）"""
        if not app_name:
            return None
        browsers = {"chrome.exe": "Chrome", "msedge.exe": "Edge", "firefox.exe": "Firefox"}
        if app_name.lower() not in browsers:
            return None
        try:
            if self._uia is None:
                import uiautomation as uia
                self._uia = uia
            # 尝试从地址栏获取 URL
            control = self._uia.GetFocusedControl()
            if control:
                name = control.Name or ""
                if name.startswith("http") and ("://" in name or "www." in name):
                    return name.split()[0].rstrip("/")
                # 搜索祖先窗口中的地址栏
                parent = control.GetParentControl()
                for _ in range(10):
                    if parent is None:
                        break
                    try:
                        edit = parent.EditControl()
                        if edit and edit.Name and ("http" in edit.Name or "www." in edit.Name):
                            return edit.Name.strip()
                    except Exception:
                        pass
                    try:
                        parent = parent.GetParentControl()
                    except Exception:
                        break
        except ImportError:
            pass
        except Exception:
            pass
        return None

    def _emit(self, data: dict):
        if self._callback:
            try:
                self._callback(self._name, data)
            except Exception as e:
                _log.error(f"[window_metadata] 回调错误: {e}")


# ============================================================================
# 进程元数据监控
# ============================================================================
class ProcessMetadataMonitor(StoppableSleep):
    """运行进程追踪 — TOP CPU/内存、应用检测、启停事件"""

    CATEGORIES = {
        "browser": ["chrome", "firefox", "msedge", "brave", "opera", "safari"],
        "ide": ["code", "devenv", "idea", "pycharm", "webstorm", "vim", "nvim", "notepad++"],
        "game": ["steam", "epicgames", "genshin", "starrail", "wuwa", "endfield", "arknights",
                 "league", "valorant", "csgo", "dota", "minecraft", "overwatch"],
        "meeting": ["zoom", "teams", "wemeet", "dingtalk", "feishu", "discord", "slack", "skype"],
        "media": ["spotify", "netflix", "vlc", "potplayer", "bilibili", "qqmusic"],
        "office": ["excel", "word", "powerpoint", "wps", "pdf", "notion", "obsidian"],
    }

    def __init__(self, poll_interval: float = 5.0, top_n: int = 5):
        self.poll_interval = poll_interval
        self.top_n = top_n
        self._running = False
        self._init_stop_event()
        self._thread: Optional[threading.Thread] = None
        self._callback: Optional[Callable[[str, dict], None]] = None
        self._known_pids: set[int] = set()
        self._name = "process_metadata"

    def set_callback(self, cb: Callable[[str, dict], None]):
        self._callback = cb

    def start(self):
        if self._running:
            return
        self._running = True
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="mon-process")
        self._thread.start()
        _log.info("[process_metadata] 进程元数据监控启动")

    def stop(self):
        self._running = False
        # ★ 1.9.3 第 36 轮：**先叫醒它**再去 join —— 否则就是干等 time.sleep 走完
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)

    def _run(self):
        import psutil
        while self._running:
            try:
                current_pids = set()
                top_cpu = []
                top_mem = []
                categories_found = set()
                new_processes = []
                for proc in psutil.process_iter(['pid', 'name', 'cpu_percent', 'memory_percent']):
                    try:
                        info = proc.info
                        pid = info['pid']
                        name = info['name'] or ""
                        current_pids.add(pid)
                        # 新进程检测
                        if pid not in self._known_pids:
                            new_processes.append({"pid": pid, "name": name})
                        # CPU/MEM TOP
                        cpu = info['cpu_percent'] or 0
                        mem = info['memory_percent'] or 0
                        top_cpu.append((name, cpu, pid))
                        top_mem.append((name, mem, pid))
                        # 分类检测
                        name_lower = name.lower()
                        for cat, keywords in self.CATEGORIES.items():
                            if any(kw in name_lower for kw in keywords):
                                categories_found.add(cat)
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        continue

                # 排序
                top_cpu.sort(key=lambda x: x[1], reverse=True)
                top_mem.sort(key=lambda x: x[1], reverse=True)

                # 停止的进程
                stopped = self._known_pids - current_pids

                data = {
                    "type": "process_update",
                    "top_cpu": [{"name": n, "cpu": round(c, 1), "pid": p} for n, c, p in top_cpu[:self.top_n]],
                    "top_mem": [{"name": n, "mem": round(m, 1), "pid": p} for n, m, p in top_mem[:self.top_n]],
                    "active_categories": list(categories_found),
                    "new_processes": new_processes[:5],
                    "stopped_pids": len(stopped) if stopped else 0,
                    "timestamp": datetime.now().isoformat(),
                }
                self._emit(data)
                self._known_pids = current_pids
            except Exception as e:
                _log.debug(f"[process_metadata] 轮询异常: {e}")
            # ★ 第 36 轮：可打断睡眠（原来 stop() 要干等这次睡眠走完）
            if self._nap(self.poll_interval):
                break

    def _emit(self, data: dict):
        if self._callback:
            try:
                self._callback(self._name, data)
            except Exception as e:
                _log.error(f"[process_metadata] 回调错误: {e}")


# ============================================================================
# 统一增强输入监控器
# ============================================================================
class UnifiedInputMonitor:
    """统一增强输入监控管理器 — 组合 Clipboard + Window + Process"""

    def __init__(self):
        self.clipboard = ClipboardMonitor()
        self.window_meta = WindowMetadataMonitor()
        self.process_meta = ProcessMetadataMonitor()
        self._callback: Optional[Callable[[str, dict], None]] = None
        self._running = False

    def set_callback(self, cb: Callable[[str, dict], None]):
        self._callback = cb

    def start(self, clipboard: bool = True, window: bool = True, process: bool = True):
        if self._running:
            return
        if clipboard:
            self.clipboard.set_callback(self._forward)
            self.clipboard.start()
        if window:
            self.window_meta.set_callback(self._forward)
            self.window_meta.start()
        if process:
            self.process_meta.set_callback(self._forward)
            self.process_meta.start()
        self._running = True
        _log.info(f"增强输入监控启动: clipboard={clipboard}, window={window}, process={process}")

    def stop(self):
        self._running = False
        self.clipboard.stop()
        self.window_meta.stop()
        self.process_meta.stop()
        _log.debug("增强输入监控已停止")

    def _forward(self, source: str, data: dict):
        if self._callback:
            try:
                self._callback(source, data)
            except Exception as e:
                _log.error(f"增强输入监控回调错误: {e}")


# ============================================================================
# 全局单例
# ============================================================================
_input_monitor_instance: Optional[UnifiedInputMonitor] = None


def get_input_monitor() -> UnifiedInputMonitor:
    global _input_monitor_instance
    if _input_monitor_instance is None:
        _input_monitor_instance = UnifiedInputMonitor()
    return _input_monitor_instance
