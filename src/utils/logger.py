"""
统一日志系统
- 控制台输出 + 文件输出
- 按模块分类
"""
import os
import sys
import threading
import traceback
import logging
from pathlib import Path
from datetime import datetime

LOG_DIR = Path(__file__).resolve().parent.parent.parent / "logs"

# 日志格式
CONSOLE_FMT = logging.Formatter(
    "[%(asctime)s] [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S"
)
FILE_FMT = logging.Formatter(
    "[%(asctime)s] [%(name)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)


def setup_logging(level: int = logging.DEBUG, verbose: bool = True,
                  quiet_modules: bool = True):
    """初始化日志系统

    Args:
        level: 全局日志级别
        verbose: 是否输出启动横幅（子进程设为 False 减小日志噪音）
        quiet_modules: 是否压制第三方库 DEBUG 日志
    """
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    # 避免重复添加 handler（子进程可能已被 spawn 机制添加过）
    if root.handlers:
        # ★ 1.8.3: 顺手 close 掉——只 clear() 不 close 会留下没人管的打开句柄
        for _h in list(root.handlers):
            try:
                _h.close()
            except Exception:
                pass
        root.handlers.clear()
    root.setLevel(level)

    # ★ 1.8.3: 文件 handler 先注册，控制台后注册。
    #   日志按 handler 顺序同步写出；控制台若因 stdout 管道无人读取而阻塞，
    #   排在后面的 handler 就一条都写不进去 → 出现过"进程活着但日志 0 字节"。
    #   先写文件保证磁盘上一定有记录。
    log_file = LOG_DIR / f"prts_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(FILE_FMT)
    root.addHandler(file_handler)

    # 控制台
    console = logging.StreamHandler(sys.stdout)
    if verbose:
        console.setLevel(level)
    else:
        console.setLevel(logging.WARNING)  # 子进程只输出 WARNING+
    console.setFormatter(CONSOLE_FMT)
    root.addHandler(console)

    # 压制第三方库的海量 DEBUG 日志
    if quiet_modules:
        for noisy_lib in ["openai", "openai._base_client", "httpcore", "httpx",
                          "urllib3", "pynput"]:
            logging.getLogger(noisy_lib).setLevel(logging.WARNING)

    print(f"[LOG] 日志文件: {log_file}")
    return root


def redirect_stderr_to_file(name: str = "sub"):
    """★★ 1.9.1: 把**进程级 stderr（C 层的 fd 2）**重定向到一个文件。

    为什么需要它（实测结论，别删）：
      我先试了 `qInstallMessageHandler`（见下面那个函数），但在这套 PySide6 上
      **收不到** —— 合成的 `qWarning` 和真实的 Qt 警告都没进日志（实测过）。
      而 `UpdateLayeredWindowIndirect failed ... (参数错误。)` 这条关键证据
      是 Qt 内部用 `qErrnoWarning` 打到 **stderr** 的：
        · 它不进 logs/prts_*.log（我 grep 一直是 0 命中，白白绕了好几轮）；
        · 用 pythonw 启动时没有控制台，它直接消失。
      → 最可靠的办法是**在操作系统层面截住 fd 2**（`os.dup2`），
        这样无论 Qt 走不走消息处理器、无论有没有控制台，都能落盘。

    产物：`logs/<name>_stderr.log`（追加写；Qt 这类消息可能每帧刷屏，所以它是
    独立文件，不会把主日志淹掉）。
    """
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        path = LOG_DIR / f"{name}_stderr.log"
        f = open(path, "a", encoding="utf-8", errors="replace", buffering=1)
        try:
            os.dup2(f.fileno(), 2)      # ★ C 层 stderr（Qt 走这条）
        except Exception as e:
            logging.getLogger("Logger").debug(f"dup2 stderr 失败: {e}")
        sys.stderr = f                  # Python 层也指过去
        return str(path)
    except Exception as e:
        logging.getLogger("Logger").debug(f"重定向 stderr 失败: {e}")
        return ""


def get_logger(name: str) -> logging.Logger:
    """获取模块日志器"""
    return logging.getLogger(name)


def install_qt_message_handler():
    """★★ 1.9.1: 把 **Qt 自己的报错**转发进 logging（标签 `[Qt]`）。

    为什么必须做（真机血泪，值得单独记一笔）：
      "形象凭空消失"这件事查了好几轮，最后起决定作用的证据是用户从**控制台**
      抄过来的一行 Qt 内部报错：
          UpdateLayeredWindowIndirect failed for ptDst=(2337,1245),
          size=(197x197), dirty=(197x197 0,0) (参数错误。)
      它**只打到 stderr，从不进 logs/prts_*.log**，后果是：
        · 我在日志里 grep 它的关键字，命中数**永远是 0**，一直以为没这回事；
        · 用 pythonw 启动（没有控制台）时它直接消失 —— 关键线索被丢掉。

      接上之后，这类底层失败会带 `[Qt]` 标签写进日志。
      ★ 但这类消息可能**每帧刷屏**（一秒几十条），所以做了**折叠限流**：
        同一条（按前 60 字归类）只记第一次，之后每 30 秒汇总一次"又出现 N 次"。
      ★ 主进程和形象子进程**都要装** —— 窗口相关的报错都在子进程里。
    """
    try:
        from PySide6.QtCore import qInstallMessageHandler, QtMsgType
    except Exception as e:
        logging.getLogger("Qt").debug(f"PySide6 不可用，跳过 Qt 消息接管: {e}")
        return False

    _qt_log = logging.getLogger("Qt")
    _level = {
        QtMsgType.QtDebugMsg: logging.DEBUG,
        QtMsgType.QtInfoMsg: logging.INFO,
        QtMsgType.QtWarningMsg: logging.WARNING,
        QtMsgType.QtCriticalMsg: logging.ERROR,
        QtMsgType.QtFatalMsg: logging.CRITICAL,
    }
    seen = {}
    last = {"t": 0.0}

    def _handler(mode, ctx, msg):
        try:
            lvl = _level.get(mode, logging.INFO)
            key = (msg or "")[:60]
            seen[key] = seen.get(key, 0) + 1
            now = time.time()
            if seen[key] == 1:
                _qt_log.log(lvl, f"[Qt] {msg}")
            elif now - last["t"] > 30:
                last["t"] = now
                _qt_log.log(lvl, f"[Qt] （上面这类消息又出现 {seen[key] - 1} 次）{msg}")
        except Exception:
            pass

    try:
        qInstallMessageHandler(_handler)
        _qt_log.info("[Qt] 已接管 Qt 消息 → 会写进本日志（标签 [Qt]，同类自动折叠）")
        return True
    except Exception as e:
        logging.getLogger("Qt").debug(f"安装 Qt 消息处理器失败: {e}")
        return False


def install_crash_hooks(process_name: str = "main"):
    """安装全局未捕获异常钩子（主进程 + 子进程通用）。

    - 任何未捕获异常都会同时写入 logging 文件（logs/prts_*.log）
      和独立的 logs/crash_<进程>_<pid>.log，便于崩溃后定位根因。
    - 子进程的 app.exec() 未被 try/except 包裹时也能被捕获。
    """
    def _write_crash(title: str, exc_type, exc, tb):
        msg = "".join(traceback.format_exception(exc_type, exc, tb)) if exc else str(exc)
        try:
            logging.getLogger("CRASH").critical(f"{title}\n{msg}")
        except Exception:
            pass
        try:
            crash_file = LOG_DIR / f"crash_{process_name}_{os.getpid()}.log"
            with open(crash_file, "a", encoding="utf-8") as f:
                f.write(f"[{datetime.now()}] {title}\n{msg}\n{'=' * 60}\n")
        except Exception:
            pass

    def _excepthook(exc_type, exc, tb):
        _write_crash("Uncaught exception", exc_type, exc, tb)

    sys.excepthook = _excepthook

    if hasattr(threading, "excepthook"):
        def _thread_hook(args):
            _write_crash(
                f"Uncaught in thread {getattr(args, 'thread', None)}",
                getattr(args, "exc_type", None),
                getattr(args, "exc_value", None),
                getattr(args, "exc_traceback", None),
            )
        threading.excepthook = _thread_hook
