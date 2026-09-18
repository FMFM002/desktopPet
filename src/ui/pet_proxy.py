"""
桌宠进程代理
- 在 MainWindow 中替代直接使用 PetAnimationWidget
- 管理与子进程的 Pipe IPC
- 提供与 PetAnimationWidget 兼容的信号 API
- 如果子进程启动失败（权限问题等），回退到主进程直接渲染
"""
import logging
import threading
from multiprocessing import Process, Pipe, freeze_support

from PySide6.QtCore import QObject, Signal, QTimer, QRect
from PySide6.QtGui import QGuiApplication

from src.ui.pet_process import run_pet_process

_log = logging.getLogger("PetProxy")

# ====== 子进程 / 直接渲染切换 ======
_DIRECT_RENDER = False  # False=子进程独立渲染, True=主进程直接渲染(兼容模式)


def _as_bool(v) -> bool:
    """★ 1.6.0: Qt 队列连接里 bool 参数偶发传成 str/None，这里统一兜底"""
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "y")
    return bool(v)


class PetProxy(QObject):
    """桌宠进程代理 — API 与 PetAnimationWidget 兼容"""

    # ★ 与 PetAnimationWidget 完全相同的信号
    behavior_changed = Signal(str)
    pet_moved = Signal()
    pet_clicked = Signal()
    file_dropped = Signal(str)  # ★ 拖拽文件到形象
    gif_once_finished = Signal()  # ★ 1.6.0: 一次性 GIF 播放完毕

    # ★ 1.6.0: 「直接渲染」模式下的线程编组 —— 形象线程调用这些信号，
    #   由 Qt 队列投递到主线程执行，避免跨线程操作 QMovie/QWidget 导致卡死。
    #   载荷统一用 JSON 字符串（纯 str 类型），绕开 Qt 队列对 Python 容器/数值的转换限制。
    _direct_command = Signal(str, str)
    _direct_result = Signal(str, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._ready = False
        self._pending: list[dict] = []
        self._direct_widget = None  # fallback 直接渲染的 widget
        self._send_lock = threading.Lock()  # ★ 多线程（形象线程）发送互斥

        # ★ 1.6.0: 直接渲染模式下，命令一律经队列投递到主线程（对象在 __init__ 所在线程）
        self._direct_command.connect(self._run_direct_command)
        self._direct_result.connect(self._on_direct_result)

        # 默认右下角位置（与 PetAnimationWidget 一致）
        screen = QGuiApplication.primaryScreen()
        if screen:
            sg = screen.geometry()
            self._last_geo = QRect(sg.right() - 160, sg.bottom() - 160, 128, 128)
        else:
            self._last_geo = QRect(1500, 800, 128, 128)

        # 尝试创建 Pipe + 子进程
        self._proc = None
        self._parent_conn = None
        self._poll_timer = QTimer(self)
        self._poll_timer.timeout.connect(self._poll_events)

        if _DIRECT_RENDER:
            self._start_direct_render()
        else:
            self._start_subprocess()

    # ─── 子进程模式 ───

    def _start_subprocess(self):
        """尝试启动桌面宠物子进程"""
        try:
            freeze_support()
            self._parent_conn, child_conn = Pipe()
            self._proc = Process(target=run_pet_process, args=(child_conn,), daemon=True)
            self._proc.start()
            self._poll_timer.start(42)  # 24fps 轮询
            _log.info("桌宠子进程已启动")
        except PermissionError as e:
            _log.warning(f"子进程启动失败(权限): {e}，回退到主进程直接渲染")
            self._start_direct_render()
        except Exception as e:
            _log.error(f"子进程启动失败: {e}，回退到主进程直接渲染")
            self._start_direct_render()

    def _start_direct_render(self):
        """在主进程中直接渲染动画（fallback 模式）"""
        try:
            from src.ui.pet_animation import PetAnimationWidget
            self._direct_widget = PetAnimationWidget()
            self._direct_widget.show()
            # 转发信号
            self._direct_widget.pet_clicked.connect(self.pet_clicked.emit)
            self._direct_widget.pet_moved.connect(self.pet_moved.emit)
            self._direct_widget.behavior_changed.connect(self.behavior_changed.emit)
            self._direct_widget.file_dropped.connect(self.file_dropped.emit)
            # ★ 1.6.0: 一次性 GIF 完成 → 经自己的事件队列回传，保证与命令队列同序
            self._direct_widget.gif_once_finished.connect(
                lambda: self._direct_result.emit("gif_once_finished", ""))
            self._ready = True
            _log.info("桌宠在主进程中直接渲染（fallback 模式）")
        except Exception as e:
            _log.error(f"直接渲染也失败了: {e}")
            self._ready = True  # 防止阻塞主窗口

    # ─── 发送命令 ───

    def _send(self, msg: dict):
        if self._direct_widget is not None:
            self._send_direct(msg)
            return
        with self._send_lock:
            try:
                if self._ready and self._parent_conn:
                    self._parent_conn.send(msg)
                else:
                    self._pending.append(msg)
            except Exception:
                pass

    def _send_direct(self, msg: dict):
        """★ 1.6.0: 直接渲染模式下，命令不直接调用 widget（形象线程 → Qt 会卡死），
        而是通过队列信号投递到主线程执行。"""
        import json as _json
        cmd = msg.get("cmd") or ""
        try:
            payload = _json.dumps(msg, ensure_ascii=False)
        except Exception:
            payload = "{}"
        self._direct_command.emit(cmd, payload)

    def _on_direct_result(self, cmd: str, _payload: str = ""):
        """直接渲染模式下 gif_once_finished 等事件的回传处理"""
        if cmd == "gif_once_finished":
            self.gif_once_finished.emit()

    def _run_direct_command(self, cmd: str, payload: str = ""):
        """主线程执行直接渲染命令"""
        w = self._direct_widget
        if w is None:
            return
        import json as _json
        try:
            msg = _json.loads(payload) if payload else {}
            if not isinstance(msg, dict):
                msg = {}
        except Exception:
            msg = {}
        try:
            if cmd == "set_behavior":
                w.set_behavior(str(msg.get("behavior") or "待机"))
            elif cmd == "set_character":
                w.set_character(str(msg.get("character") or "铃兰"))
            elif cmd == "set_gif_file":
                w.set_gif_file(str(msg.get("path") or ""), once=_as_bool(msg.get("once")))
            elif cmd == "set_scale":
                try:
                    w.set_scale(float(msg.get("scale")))
                except (TypeError, ValueError):
                    _log.debug(f"set_scale 参数无效({msg.get('scale')!r})，忽略")
            elif cmd == "show":
                w.show()
            elif cmd == "hide":
                w.hide()
        except Exception as e:
            _log.warning(f"直接渲染命令 {cmd} 执行失败: {e}")

    def set_behavior(self, behavior: str):
        if self._direct_widget is not None:
            self._send_direct({"cmd": "set_behavior", "behavior": behavior})
        else:
            self._send({"cmd": "set_behavior", "behavior": behavior})

    def set_character(self, name: str):
        if self._direct_widget is not None:
            self._send_direct({"cmd": "set_character", "character": name})
        else:
            self._send({"cmd": "set_character", "character": name})

    def set_gif_file(self, path: str, once: bool = False):
        """★ 1.5.0: 直接按文件路径切换 GIF（由形象线程调用，线程安全）
        ★ 1.6.0: once=True → 只播放一遍，播完回传 gif_once_finished 事件"""
        if self._direct_widget is not None:
            self._send_direct({"cmd": "set_gif_file", "path": path, "once": bool(once)})
        else:
            self._send({"cmd": "set_gif_file", "path": path, "once": bool(once)})

    def set_scale(self, scale: float):
        if self._direct_widget is not None:
            self._send_direct({"cmd": "set_scale", "scale": float(scale)})
        else:
            self._send({"cmd": "set_scale", "scale": float(scale)})

    def show(self):
        if self._direct_widget is not None:
            self._send_direct({"cmd": "show"})
        else:
            self._send({"cmd": "show"})

    def hide(self):
        if self._direct_widget is not None:
            self._send_direct({"cmd": "hide"})
        else:
            self._send({"cmd": "hide"})

    def close(self):
        """关闭子进程并等待退出"""
        if self._direct_widget is not None:
            self._direct_widget.close()
            self._poll_timer.stop()
            return
        try:
            self._send({"cmd": "quit"})
        except Exception:
            pass
        self._poll_timer.stop()
        if self._proc and self._proc.is_alive():
            self._proc.join(timeout=3)
            if self._proc.is_alive():
                self._proc.terminate()

    def geometry(self) -> QRect:
        """返回缓存的最新位置"""
        if self._direct_widget is not None:
            g = self._direct_widget.geometry()
            self._last_geo = QRect(g.x(), g.y(), g.width(), g.height())
        return self._last_geo

    # ─── 轮询子进程事件 ───

    def _poll_events(self):
        if self._direct_widget is not None or self._parent_conn is None:
            return
        while self._parent_conn.poll():
            try:
                msg = self._parent_conn.recv()
            except (EOFError, ConnectionError):
                _log.warning("桌宠进程连接断开")
                self._poll_timer.stop()
                return

            event = msg.get("event")
            # 更新缓存 geometry
            if "geo" in msg:
                g = msg["geo"]
                self._last_geo = QRect(g["x"], g["y"], g["w"], g["h"])

            if event == "ready":
                self._ready = True
                _log.info("桌宠进程就绪")
                # 刷新积压命令
                for p in self._pending:
                    try:
                        self._parent_conn.send(p)
                    except Exception:
                        pass
                self._pending.clear()

            elif event == "pet_clicked":
                self.pet_clicked.emit()
            elif event == "pet_moved":
                self.pet_moved.emit()
            elif event == "behavior_changed":
                self.behavior_changed.emit(msg["behavior"])
            elif event == "file_dropped":
                self.file_dropped.emit(msg.get("file_path", ""))
            elif event == "gif_once_finished":
                self.gif_once_finished.emit()
