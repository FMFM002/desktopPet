"""
桌宠独立渲染进程
- 仅负责 GIF/PNG 动画渲染
- 通过 multiprocessing.Pipe 接收主进程指令
- 完全独立于主进程，不阻塞主进程事件循环
"""
import sys
import logging
from multiprocessing.connection import Connection

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import QApplication

from src.ui.pet_animation import PetAnimationWidget
from src.utils.logger import setup_logging, install_crash_hooks

_log = logging.getLogger("PetProcess")


def _get_geo(pet: PetAnimationWidget) -> dict:
    g = pet.geometry()
    return {"x": g.x(), "y": g.y(), "w": g.width(), "h": g.height()}


def _ws_value(pet: PetAnimationWidget):
    """★ 2026-09-18 00:30：**windowState() 返回的是 Qt 标志对象，不能直接 int()**。

    我上一轮为了让"show 了却没出现"能定位，在这里写了 `int(pet.windowState())`
    —— 结果 PySide6 直接抛 `TypeError: int() argument must be ... not 'WindowState'`，
    而这个异常发生在 `_check_commands` 里、**没有任何捕获** → 整个形象子进程
    崩掉（logs/crash_pet_*.log 有原始堆栈，23:59:02）。
    表现就是"形象凭空消失"（而且再也不会自己回来，因为进程已经死了）。
    现在取 `.value`（拿不到就退回 repr），永远不抛。
    """
    try:
        ws = pet.windowState()
        return getattr(ws, "value", ws)
    except Exception:
        return "?"


def _is_exposed(pet: PetAnimationWidget):
    """★ 2026-09-17 22:55: 窗口到底有没有真的暴露在屏幕上。

    `isVisible()` 只表示"Qt 认为它可见"——**被最小化、被全屏程序盖住、
    分层窗口合成失效**这几种情况下它都可能是 True。`isExposed()` 更接近真相。
    打日志用；拿不到就返回 None（不参与判断，免得误判）。
    """
    try:
        wh = pet.windowHandle()
        return None if wh is None else bool(wh.isExposed())
    except Exception:
        return None


def _release_foreground_lock():
    """★ 1.9.0: 主动把前台权让出去，好让主进程能激活聊天窗口。

    背景（W1）：形象跑在这个独立子进程里，你点形象时**前台权在本进程**；
    主进程随后调 activateWindow() 会被 Windows 的 SetForegroundWindow
    规则拒绝 —— 表现就是聊天窗口光标在闪、但键盘输入进不去。
    前台进程可以主动放权：AllowSetForegroundWindow(ASFW_ANY = -1) 之后，
    任何进程都能把窗口设到前台。必须在**点击的这一刻**调（此时本进程仍是
    前台），不能延后。
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.user32.AllowSetForegroundWindow(-1)   # ASFW_ANY
    except Exception as e:
        _log.debug(f"AllowSetForegroundWindow 失败: {e}")


def run_pet_process(conn: Connection):
    """桌宠进程入口 — 由 multiprocessing.Process 以 spawn 方式启动"""
    # ★★ 1.9.1: **形象窗口相关的 Qt 报错都在这个子进程里**，必须截住。
    #   真机教训：`UpdateLayeredWindowIndirect failed ... (参数错误。)` 这一行
    #   是"形象不显示"的决定性证据，但它以前只打 stderr、从不进日志，
    #   害我们绕了好几轮；而 qInstallMessageHandler 在这套 PySide6 上实测收不到，
    #   所以改成**在操作系统层面把 fd 2 重定向到 logs/pet_stderr.log**（最可靠）。
    #   ★★ 顺序同样重要：必须**先重定向 stderr、再 setup_logging()**，
    #      否则没有控制台时 `prts_*.log` 会占到 fd 2、被 dup2 顶掉 → 日志永远 0 字节。
    #      （真机踩过：14:07 那次启动所有 prts_*.log 都是 0 B。）
    from src.utils.logger import redirect_stderr_to_file, install_qt_message_handler
    redirect_stderr_to_file("pet")

    # ★ 子进程独立日志 + 崩溃钩子（app.exec() 未包 try，崩溃靠 excepthook 落盘）
    setup_logging(verbose=False)
    install_crash_hooks("pet")
    install_qt_message_handler()
    _log.info("[形象] Qt/原生 stderr 已重定向到 logs/pet_stderr.log")

    # 必须创建自己的 QApplication（独立进程）
    app = QApplication(sys.argv)

    pet = PetAnimationWidget()
    pet.show()

    # ★ 转发 pet 信号 → pipe（每个事件都附带当前 geometry）
    def _on_pet_clicked():
        # ★ 1.9.0: 点击瞬间先放前台权，主进程才抢得到（否则只能闪光标）
        _release_foreground_lock()
        conn.send({"event": "pet_clicked", "geo": _get_geo(pet)})

    pet.pet_clicked.connect(_on_pet_clicked)
    pet.pet_moved.connect(lambda: conn.send({"event": "pet_moved", "geo": _get_geo(pet)}))
    pet.behavior_changed.connect(
        lambda b: conn.send({"event": "behavior_changed", "behavior": b, "geo": _get_geo(pet)}))
    pet.file_dropped.connect(
        lambda fp: conn.send({"event": "file_dropped", "file_path": fp, "geo": _get_geo(pet)}))
    # ★ 1.6.0: 一次性 GIF（启动默认）播放完毕 → 通知形象线程切入待机
    pet.gif_once_finished.connect(
        lambda: conn.send({"event": "gif_once_finished", "geo": _get_geo(pet)}))

    # 发送 ready 信号
    conn.send({"event": "ready", "geo": _get_geo(pet)})

    # ★ 1.9.0: 形象"自愈"看门狗。
    #   真机现象：形象偶尔**凭空消失**（子进程活着、日志里没有任何 hide 命令），
    #   用托盘的隐藏/显示切一下才回来。最可能是 Windows 上「无边框 + 半透明
    #   + 置顶」分层窗口偶发合成失效（整窗变透明），也可能是最后一帧变空。
    #   不管是哪种，**重新 show + 重画当前帧**都能救回来。
    #   ⚠ 必须区分"用户主动隐藏"（托盘/菜单）—— 那种情况**绝不能**自动弹回来，
    #   否则"隐藏桌宠"功能就废了，所以用 _user_hidden 记着。
    _state = {"user_hidden": False, "healed": 0}

    def _watchdog():
        try:
            if _state["user_hidden"]:
                return
            # ★ 接管（2026-09-17 22:30）：**"被最小化"也要算不可见**。
            #   真机现象（你报的）：开外部图片查看器 → 关掉 → 形象一起没了。
            #   Qt 里**最小化的窗口 isVisible() 仍然是 True**，所以老看门狗
            #   `if not pet.isVisible()` 永远不触发 → 形象再也不回来。
            #   现在连 windowState 一起查，并把最小化状态复位。
            _min = pet.isMinimized()
            if _min or not pet.isVisible():
                _state["healed"] += 1
                _why = "被最小化" if _min else "窗口不可见"
                _log.warning(f"[形象自愈] {_why}（没人主动隐藏它）→ 复位并 show() "
                             f"第 {_state['healed']} 次")
                try:
                    pet.setWindowState(pet.windowState() & ~Qt.WindowMinimized)
                except Exception:
                    pass
                pet.show()
                try:
                    pet.raise_()
                except Exception:
                    pass
            # ★★ 1.9.1（用户 13:24 的场景描述）：这里原来只调 `repaint_current()`，
            #   而**"同一张 pixmap 再设一遍"对 Qt 来说是空操作 → 不会重绘**，
            #   所以它救不了"分层表面没被呈现"这种情况。
            #   真机证据：出问题时 isVisible()=True、isMinimized()=False，
            #   于是上面那个 if **从不成立**、又只做了个无效重画
            #   → 用户场景 A"等待也不回来"就是这个原因。
            #   现在改成走 pet 的**真·强制重绘**（clear→repaint→设回→repaint），
            #   它会真的把像素重新推给分层表面。
            try:
                pet.force_repaint()
            except AttributeError:
                pet.repaint_current()      # 老接口兜底（万一 pet 不是那个类）
            # ★★★ 1.9.1 根本性修复（用户："能不能从根本解决"）
            #   真机日志说明：形象不可见时**应用侧一切正常** ——
            #     visible=True layered=True topmost=True，而且 GIF 每帧都在重画
            #     （等于每秒几十次 UpdateLayeredWindow），屏幕上却什么都没有。
            #   → 所以"反复重画"是没用的：DWM 已经不再呈现这个窗口了。
            #     唯一有效的动作是**窗口级的"重新显示"**（用户用托盘显隐验证过）。
            #   → 既然**应用侧根本检测不到这个失效**（状态和画面都是对的），
            #     那就**不依赖检测**：每次自检（3 秒）都做一次
            #     `SWP_SHOWWINDOW` 重新声明 —— 对已可见的窗口视觉无感（不闪），
            #     但如果 DWM 把它丢了，最多 3 秒就会被重新拉回来。
            #     "等待也不回来"从此变成"最多等 3 秒自己回来"。
            if not _state["user_hidden"]:
                try:
                    pet.reassert_window()
                except AttributeError:
                    pass
        except Exception as e:
            _log.debug(f"[形象自愈] 失败: {e}")

    # ★ 24fps 轮询管道命令（足够流畅，不浪费 CPU）
    def _check_commands():
        # ★ 2026-09-18：**整个循环包一层** —— 任何一条命令处理里抛异常，
        #   以前会直接冒到 Qt 定时器回调 → 未捕获 → **形象子进程整个崩掉**
        #   （我那条 int(windowState()) 就是这么把形象打死的）。
        #   现在最坏情况只是这条命令失败，进程活着、下一帧继续。
        try:
            _check_commands_inner()
        except Exception as _e:
            _log.error(f"[形象] 处理命令时出错（已兜住，进程继续）: {_e}", exc_info=True)

    def _check_commands_inner():
        while conn.poll():
            msg = conn.recv()
            cmd = msg.get("cmd")
            if cmd == "set_behavior":
                pet.set_behavior(msg["behavior"])
            elif cmd == "set_character":
                pet.set_character(msg["character"])
            elif cmd == "set_gif_file":
                pet.set_gif_file(msg.get("path", ""), once=bool(msg.get("once", False)))
            elif cmd == "set_scale":
                try:
                    pet.set_scale(float(msg.get("scale", 1.0)))
                except Exception:
                    pass
            elif cmd == "show":
                _state["user_hidden"] = False
                # ★★ 2026-09-17 22:55（真机日志定位，见日志本轮记录）：
                #   你的日志里，主进程连发 6 次 show 命令，子进程**每次都收到了**
                #   （`收到 show 命令 → 显示`），但形象就是不出现；一直到最后
                #   一次 hide + show 之后才回来。
                #   原因：对一个 **Qt 认为已经可见** 的窗口再调 show() 是**空操作**
                #   —— 而此刻它恰恰处于"Qt 说可见、实际没被合成"的状态
                #   （分层窗口偶发失效；自愈看门狗也没触发，因为 isVisible()/
                #   isMinimized() 都是正常的）。
                #   所以这里改成**强制重新映射**：先 hide 再 show，让原生窗口
                #   重新创建/映射一次 —— 这是唯一被日志证实有效的动作。
                _was = pet.isVisible()
                if _was:
                    pet.hide()
                pet.show()
                try:
                    pet.raise_()
                except Exception:
                    pass
                pet.repaint_current()
                # 把**窗口真实状态**打出来：下次再出问题，"show 了却没出现"那一刻
                # 它是可见/最小化/被暴露(isExposed)/几何多少，日志里就全有了。
                _log.info(f"[形象] 收到 show 命令 → "
                          f"{'强制重新映射(hide+show)' if _was else '首次显示'} "
                          f"| visible={pet.isVisible()} minimized={pet.isMinimized()} "
                          f"state={_ws_value(pet)} "
                          f"exposed={_is_exposed(pet)} geo={_get_geo(pet)}")
            elif cmd == "hide":
                _state["user_hidden"] = True      # 主动隐藏 → 看门狗不许自动救
                _log.info("[形象] 收到 hide 命令 → 隐藏（看门狗不再自动救）")
                pet.hide()
            elif cmd == "quit":
                app.quit()
                return
            # ★ 1.9.0: 收到任何指令都顺手确认一下"我还在不在" ——
            #   比等 3 秒看门狗更及时（形象偶发"凭空消失"时的即时自愈）。
            if not _state["user_hidden"] and (not pet.isVisible() or pet.isMinimized()):
                _log.warning("[形象自愈] 收到指令时发现窗口不可见/被最小化 → 复位并 show()")
                try:
                    pet.setWindowState(pet.windowState() & ~Qt.WindowMinimized)
                except Exception:
                    pass
                pet.show()

    timer = QTimer()
    timer.timeout.connect(_check_commands)
    timer.start(42)  # ~24fps

    watchdog = QTimer()
    watchdog.timeout.connect(_watchdog)
    watchdog.start(3000)   # 每 3 秒自检一次

    try:
        app.exec()
    except Exception as e:
        _log.critical(f"桌宠进程事件循环崩溃: {e}", exc_info=True)
        raise
