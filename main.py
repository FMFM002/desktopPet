"""
普瑞赛斯桌宠 - 入口文件（重构 v2.0）
PRTS Desktop Pet

★ 模块级只做最小初始化（日志+path），避免 Windows spawn 子进程重复加载重量组件
"""
import sys
import os
import time
import logging
import traceback
import multiprocessing

# 确保项目根目录在 path 中
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)


def _is_main_process() -> bool:
    """判断是否为真正的主进程（非 multiprocessing spawn 子进程）"""
    return __name__ == "__main__"


# ====== 最早加载日志（轻量，主进程和子进程都可用） ======
from src.utils.logger import (setup_logging, get_logger, install_crash_hooks,
                              install_qt_message_handler, redirect_stderr_to_file)

# ★★ 1.9.1 顺序很重要（真机踩坑，务必保持）：
#   必须**先**把 C 层 fd 2 重定向到文件，**再**调 setup_logging()。
#   反过来的话：没有控制台时（pythonw / 被某些方式启动）fd 2 是空的，
#   于是 `setup_logging()` 打开的 `logs/prts_*.log` 会**占到 fd 2**；
#   随后我们再 `dup2(stderr文件, 2)`，就把日志文件的那个 fd 顶掉了 ——
#   结果 **logs/prts_*.log 永远是 0 字节**，等于整个日志系统失效。
#   （真机现象：14:07 那次启动后，所有 prts_*.log 都是 0 B。）
if _is_main_process():
    redirect_stderr_to_file("main")

log = setup_logging(verbose=_is_main_process())
# ★ 崩溃钩子：任何未捕获异常都会落盘到 logs/crash_main_*.log（含子线程）
install_crash_hooks("main")

# ══════════════════════════════════════════════════════════════
# ★★ 1.9.1: 把 **Qt 自己的报错**也接进我们的日志文件
# 实现放在 `src/utils/logger.install_qt_message_handler()`（主进程/子进程共用）。
# 动机见那里的长注释：`UpdateLayeredWindowIndirect failed ...` 这类关键线索
# 以前只打 stderr、从不进日志，害我白查了好几轮。
# ══════════════════════════════════════════════════════════════
if _is_main_process():
    install_qt_message_handler()

if _is_main_process():
    log.info("=" * 50)
    log.info("铃兰桌宠 v1.5.0 启动中...")
    log.info(f"项目目录: {BASE_DIR}")
    log.info(f"Python: {sys.version}")
    log.info("=" * 50)


# ====== 依赖检查 ======
def check_deps():
    """检查关键依赖（仅主进程）"""
    deps = {
        "PySide6": "UI框架",
        "openai": "LLM API",
        "PIL": "图像处理",
        "dotenv": "环境变量",
        "requests": "HTTP请求",
        "numpy": "数值计算",
    }
    optional = {
        "pynput": "键盘监控",
        "mss": "屏幕截图",
        "psutil": "系统监控",
        "win32gui": "窗口监控（原生）",
        "soundfile": "音频文件读写",
        "sounddevice": "音频设备（高级）",
    }

    for mod, desc in deps.items():
        try:
            __import__(mod)
            log.debug(f"  [OK] {desc} ({mod})")
        except ImportError:
            log.error(f"  [缺失!] {desc} ({mod}) - 请 pip install {mod}")

    for mod, desc in optional.items():
        try:
            __import__(mod)
            log.debug(f"  [OK] {desc} ({mod})")
        except ImportError:
            log.warning(f"  [可选缺失] {desc} ({mod}) - 相关功能将不可用")


# ====== 预初始化组件 ======
def init_components():
    """预初始化不依赖 QApplication 的组件（仅主进程）"""
    # 数据库 + 长期记忆
    try:
        from src.core.database import get_db
        db = get_db()
        log.info(f"SQLite 数据库已就绪: {db.raw.get_count()} raw, {db.ltm.count()} ltm")
    except Exception as e:
        log.warning(f"数据库初始化警告: {e}")

    # whisper.cpp 检测
    try:
        from src.core.whisper_local import WhisperCLI
        cli = WhisperCLI()
        if cli.available:
            log.info("Whisper.cpp CLI 本地 STT 可用")
        else:
            log.info("Whisper.cpp CLI 未配置（STT 将使用其他后端）")
    except Exception:
        log.debug("Whisper.cpp CLI 检测跳过")

    # 长期记忆 tool
    try:
        from tools.long_term_memory_tool import get_long_term_memory_tool
        ltm = get_long_term_memory_tool()
        log.info(f"长期记忆工具已注册: {ltm.get_name()}")
    except Exception as e:
        log.warning(f"长期记忆工具初始化: {e}")

    # 数据清洗 pipeline
    try:
        from src.core.data_pipeline import get_pipeline
        pipeline = get_pipeline()
        log.info("数据清洗 Pipeline 已就绪")
    except Exception as e:
        log.warning(f"Pipeline 初始化: {e}")

    # ★ 1.7.3: 长期记忆定期总结器（聚类合并 + 衰减清理）
    try:
        from tools.ltm_summarizer import get_ltm_summarizer
        sumzr = get_ltm_summarizer()
        sumzr.start()
        log.info("长期记忆定期总结器已启动")
    except Exception as e:
        log.warning(f"LTMSummarizer 初始化: {e}")


# ====== 清理缓存 ======
def clear_cache():
    """启动时清理所有 __pycache__ 和 .pyc 文件，确保代码修改生效"""
    import shutil
    cleaned = 0
    for root, dirs, _files in os.walk(BASE_DIR):
        # 跳过虚拟环境和外部依赖目录
        dirs[:] = [d for d in dirs if d not in ("venv", ".venv", "env", ".git", "node_modules", "data")]
        if "__pycache__" in dirs:
            pycache_path = os.path.join(root, "__pycache__")
            try:
                shutil.rmtree(pycache_path)
                cleaned += 1
            except Exception:
                pass
    if cleaned:
        log.info(f"清理了 {cleaned} 个 __pycache__ 目录")


# ====== 启动主程序 ======
def main():
    clear_cache()
    check_deps()
    log.info("依赖检查完成")
    init_components()

    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    try:
        # 高DPI
        QApplication.setHighDpiScaleFactorRoundingPolicy(
            Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
        )

        app = QApplication(sys.argv)
        app.setQuitOnLastWindowClosed(False)
        app.setApplicationName("铃兰桌宠")
        app.setOrganizationName("PRTS")
        log.info("QApplication 初始化完成")

        from src.ui.main_window import MainWindow
        log.info("MainWindow 导入完成")

        window = MainWindow()
        log.info("MainWindow 创建完成")

        window.run()
        log.info("主循环启动...")

        sys.exit(app.exec())

    except Exception as e:
        log.critical(f"启动失败: {e}")
        log.critical(traceback.format_exc())
        try:
            from PySide6.QtWidgets import QMessageBox, QApplication
            if QApplication.instance() is None:
                QApplication(sys.argv)
            QMessageBox.critical(None, "启动失败",
                                 f"程序启动失败:\n\n{e}\n\n详细信息请查看 logs/ 目录")
        except Exception:
            pass
        sys.exit(1)


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
