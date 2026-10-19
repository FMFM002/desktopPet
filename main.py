"""
普瑞赛斯桌宠 - 入口文件（重构 v2.0）
PRTS Desktop Pet

★ 模块级只做最小初始化（日志+path），避免 Windows spawn 子进程重复加载重量组件
"""
import sys
import os
import traceback
import multiprocessing

# 确保项目根目录在 path 中
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

# ★★ 1.9.4 exe：**窗口版冻结程序没有控制台** → PyInstaller 会把
#   `sys.stdout` / `sys.stderr` 设成 `None`。
#   而 `src/utils/logger.setup_logging()` 会挂一个 `StreamHandler(sys.stdout)`；
#   StreamHandler 在 `stream is None` 时会回落到 `sys.stderr`，**它也是 None** →
#   每条日志都触发 `logging` 内部的 "Logging error"（在真机上表现为日志噪音，
#   严重时在异常路径上抛 AttributeError）。
#   所以这里在**最早时机**把两个流换成 devnull —— 只影响控制台输出，
#   文件日志（logs/prts_*.log）完全不受影响。
for _name in ("stdout", "stderr"):
    if getattr(sys, _name, None) is None:
        try:
            setattr(sys, _name, open(os.devnull, "w", encoding="utf-8"))
        except Exception:
            pass

# ★★ 1.9.4 exe：`--selftest` —— **启动自检**。
#   为什么要有它：exe 是 `console=False`（没有黑窗口），双击没反应时
#   **外面看不到任何报错**。这个模式会把"能不能起来"的结论写进
#   `logs/selftest_*.txt`（含每项通过/失败与原因），然后自己退出：
#       · 退出码 0 = 自检全过；3 = 有必检项没过（详见报告）
#   它也是打包后**唯一能自动化验证**的手段 —— 不用盯着屏幕等形象出现。
_SELFTEST = "--selftest" in sys.argv
_SELFTEST_MS = int(os.environ.get("PRTS_SELFTEST_MS", "8000"))

# ★★ 1.9.4：**在任何人 import 内置的 src.genie_tts 之前**先把模型目录告诉它。
#   为什么必须这么早：`src/genie_tts/Resources.py` 发现 `GENIE_DATA_DIR` 没设时，
#   会**弹交互式询问**"要不要从 HuggingFace 下载模型？"（打包时实测刷屏几十行，
#   还会顺手去下 nltk 数据）。tts_engine 里虽然也设置了，但那要等它被 import ——
#   **导入顺序一变就踩到**。这里在最早的时机兜住。
os.environ.setdefault(
    "GENIE_DATA_DIR",
    os.path.join(BASE_DIR, "src", "genie_tts", "assets", "genie", "GenieData"))
os.environ.setdefault("HF_HUB_ENABLE_PROGRESS_BAR", "0")

# ★★ 1.9.4：**首次运行没有 `.env` 时，从随包的 `.env.preset` 复制一份**。
#   用户 2026-10-05 拍板（选 C）："这部分key作为出厂默认预设。熟人使用的小项目，
#   不担心被刷爆。" —— 预设的是**辅助服务**（和风天气 / Tavily / 终末地），
#   让熟人拿到就能直接用天气与搜索，不必自己注册三个服务。
#   ★ **LLM 的 Key 不在预设里**（首次配置引导会问它，填进用户自己的 .env）。
#   ★ 位置必须在**任何 dotenv 加载之前**（这里正好是 `BASE_DIR` 之后），
#     否则这一次启动读到的仍是空的。
#   ★ 只用 os/shutil，不 import 项目模块 —— main.py 模块级要保持最小初始化
#     （子进程 spawn 时也会走到这里）。
try:
    _env_path = os.path.join(BASE_DIR, ".env")
    _preset_path = os.path.join(BASE_DIR, ".env.preset")
    if not os.path.exists(_env_path) and os.path.exists(_preset_path):
        import shutil as _shutil
        _shutil.copyfile(_preset_path, _env_path)
except Exception:
    pass          # 复制失败不影响启动（用户手填也能用）


def _is_main_process() -> bool:
    """判断是否为真正的主进程（非 multiprocessing spawn 子进程）"""
    return __name__ == "__main__"


# ====== 最早加载日志（轻量，主进程和子进程都可用） ======
from src.utils.logger import setup_logging, install_crash_hooks, install_qt_message_handler, redirect_stderr_to_file

# ★★ 1.9.1 顺序很重要（真机踩坑，务必保持）：
#   必须**先**把 C 层 fd 2 重定向到文件，**再**调 setup_logging()。
#   反过来的话：没有控制台时（pythonw / 被某些方式启动）fd 2 是空的，
#   于是 `setup_logging()` 打开的 `logs/prts_*.log` 会**占到 fd 2**；
#   随后我们再 `dup2(stderr文件, 2)`，就把日志文件的那个 fd 顶掉了 ——
#   结果 **logs/prts_*.log 永远是 0 字节**，等于整个日志系统失效。
#   （真机现象：14:07 那次启动后，所有 prts_*.log 都是 0 B。）
if _is_main_process():
    redirect_stderr_to_file("main")

# ★★ 1.9.2（真机静默崩溃后补的诊断）：
#   用户 23:06 遇到"聊天窗口消失 + 小托盘退出 + 形象变孤儿（管道关闭）"，
#   但**既没有 Python 堆栈、也没有 faulthandler 输出** —— 主进程是静默硬崩溃
#   （在 Qt 里访问已销毁的 C++ 对象就是这个症状）。
#   为了让"下次再崩"一定能留下证据，这里开 faulthandler 并写到独立文件：
#     · 段错误 / 访问违例 / abort 都会把**当前所有线程的 C 级堆栈**落盘；
#     · 独立文件，不会被形象进程的刷屏淹掉；
#     · 顺手加一个 SIGTERM/abort 的兜底 dump。
try:
    import faulthandler as _fh
    try:
        from src.utils.logger import LOG_DIR as _LOGDIR
    except Exception:
        from pathlib import Path as _P
        _LOGDIR = _P(__file__).resolve().parent / "logs"
    _LOGDIR.mkdir(parents=True, exist_ok=True)
    _fh_log = open(_LOGDIR / "main_faulthandler.log", "a", buffering=1,
                   encoding="utf-8", errors="replace")
    _fh.enable(file=_fh_log, all_threads=True)
    try:
        import signal as _sig
        for _s in ("SIGABRT", "SIGSEGV", "SIGFPE", "SIGILL", "SIGBUS"):
            if hasattr(_sig, _s):
                _fh.register(getattr(_sig, _s), file=_fh_log, all_threads=True)
    except Exception:
        pass
except Exception:
    pass

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
    # ★★ 1.9.4：版本号从**唯一来源** `src/version.py` 取，不再写字面量。
    #   这里原来硬编码 "铃兰桌宠 v1.5.0 启动中..."，而 README 标题写的是 v1.8.9
    #   —— 两个数字互相矛盾、且都落后于实际版本。
    from src.version import STARTUP_LINE
    log.info(STARTUP_LINE)
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
        # ★ 1.9.2（R60，用户只做候选 A）：sounddevice 懒加载 ——
        #   它只服务 STT，而 STT 默认关闭；无条件 import 要付 8.8MB/171ms。
        #   真要用到（STT/音频播放）时自然会被 import，这里不再预检。
        # "sounddevice": "音频设备（高级）",
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
def _selftest_checks(window) -> dict:
    """自检要看的那些事。返回 `{名字: {ok, detail, required}}`。

    ★ 分"必检"与"仅供参考"两类：**首启时 `.env` 里没有 LLM Key 是正常的**
      （用户还没填），不能因此判自检失败。
    """
    from pathlib import Path
    import re
    # ★ 用**程序自己那套**路径解析（不是再从 __file__ 推一遍）——
    #   这样自检同时验证了 `runtime.app_dir()`：exe 里它必须等于 exe 目录。
    from src.utils.runtime import app_dir

    out = {}

    def chk(name, ok, detail="", required=True):
        out[name] = {"ok": bool(ok), "detail": str(detail)[:400],
                     "required": bool(required)}

    base = app_dir()
    frozen = bool(getattr(sys, "frozen", False))
    # ★ frozen 只是"这条报告是从哪种形态出来的"，不是通过条件（源码运行应当是 False）
    chk("frozen", frozen, f"exe={sys.executable}", required=False)
    if frozen:
        chk("base_dir_is_exe_dir",
            base.resolve() == Path(sys.executable).resolve().parent, str(base))
    # 随包的"数据类"文件（DATAS 里那些，缺了就是打包漏了）
    for rel in (".env.example", ".env.preset", "config.default.json",
                "README.md", "requirements.txt",
                "data/China-City-List-latest.csv",
                "data/桌宠形象", "data/memes", "data/assets",
                # ★★ 1.9.4：出厂预设的"我的头像"。漏了 → 用户看到的是"文档"占位图
                "data/avatars", "data/avatars/user_avatar.jpg",
                "data/自建工具模板",
                "tools", "tools/model_manager.py",
                "tools/_vendor/bili_pixiv_toolkit.py"):
        p = base / rel
        chk(f"exists:{rel}", p.exists(), p)
    # 运行期自动生成的东西（首启会自己建）
    env = base / ".env"
    chk("env_created", env.is_file(), env)
    _has_key = False
    if env.is_file():
        txt = env.read_text(encoding="utf-8", errors="replace")
        # ★ 必须**按行首**匹配 `KEY=`：`.env.preset` 的注释里就出现了
        #   "DEEPSEEK_API_KEY" 这个词，用 `in` 会把"注释里提过"误判成"已配置"。
        _has_key = re.search(r"(?mi)^\s*DEEPSEEK_API_KEY\s*=\s*\S", txt) is not None
        # ★ 这条专门验 `.env.preset` 有没有被打进包（交接文档缺口 1）
        chk("env_preset_merged", "WEATHER_API_KEY" in txt and "TAVILY_API_KEY" in txt,
            "天气/搜索的出厂 KEY 是否随包")
        chk("env_has_llm_key", _has_key,
            "首启还没填 Key 是正常的（向导会问）", required=False)
    chk("logs_dir", (base / "logs").is_dir(), base / "logs")
    chars = list((base / "data" / "桌宠形象").glob("*/settings.json"))
    chk("characters", bool(chars), f"{len(chars)} 个角色")
    chk("window_created", window is not None)
    try:
        from PySide6.QtWidgets import QApplication
        chk("qt_platform", True, QApplication.platformName())
        chk("top_level_widgets", len(QApplication.topLevelWidgets()) > 0,
            len(QApplication.topLevelWidgets()))
    except Exception as e:                       # noqa: BLE001
        chk("qt_platform", False, f"{type(e).__name__}: {e}")
    try:
        # ★ 形象有两条路（pet_proxy.py）：子进程渲染 / 主进程直接渲染（fallback）。
        #   两种都算"形象起来了"，别把 fallback 判成失败。
        _pet = getattr(window, "pet", None)
        _proc = getattr(_pet, "_proc", None)
        _direct = getattr(_pet, "_direct_widget", None)
        chk("pet_ready",
            bool((_proc is not None and _proc.is_alive())
                 or _direct is not None or getattr(_pet, "_ready", False)),
            f"子进程={'alive' if (_proc is not None and _proc.is_alive()) else '无'}"
            f" / 直接渲染={'有' if _direct is not None else '无'}")
    except Exception as e:                       # noqa: BLE001
        chk("pet_ready", False, f"{type(e).__name__}: {e}")
    try:
        chk("tray_ready", getattr(window, "_tray", None) is not None,
            type(getattr(window, "_tray", None)).__name__)
    except Exception as e:                       # noqa: BLE001
        chk("tray_ready", False, f"{type(e).__name__}: {e}")
    # ★ 工具是**按文件路径**扫出来的：`tools/*.py` 没进包 → 这里会是 0 个
    try:
        from src.core.tool_manager import get_tool_manager
        _tm = get_tool_manager()
        _n = len(getattr(_tm, "_tools", {}) or {})
        chk("tools_registered", _n > 0, f"{_n} 个工具")
    except Exception as e:                       # noqa: BLE001
        chk("tools_registered", False, f"{type(e).__name__}: {e}")
    # ★★ 「会话真的建起来了没」——**只有填了 LLM Key 才检**。
    #   没填 Key 时 `session-create` 会按设计报 Missing credentials（首次配置引导的工作），
    #   那不是 exe 的毛病，不该判失败；但填了 Key 还建不起来，就一定是坏的。
    try:
        chk("session_ready", hasattr(window, "session"),
            "有 Key 时必须成功创建会话" if _has_key else "首启未填 Key → 按设计等向导",
            required=_has_key)
    except Exception as e:                       # noqa: BLE001
        chk("session_ready", False, f"{type(e).__name__}: {e}")
    try:
        chk("chat_ready", getattr(window, "chat", None) is not None, "聊天窗口")
    except Exception as e:                       # noqa: BLE001
        chk("chat_ready", False, f"{type(e).__name__}: {e}")
    # ★★ 1.9.4：「一键开通 B站/P站」= playwright + 它的**浏览器驱动**
    #   （`driver/node.exe` + `driver/package/cli.js`）。这两个是**数据文件**，
    #   PyInstaller 不会自动带 —— 所以这里必须验它们真的在 exe 目录里。
    try:
        import playwright                                   # noqa: F401
        from playwright._impl._driver import compute_driver_executable
        _node, _cli = compute_driver_executable()
        chk("playwright_driver",
            Path(_node).is_file() and Path(_cli).is_file(),
            f"node={_node} / cli={_cli}")
    except ImportError:
        # 精简版 exe 会把 playwright 排掉 —— 那不算失败，但要**说清楚**
        chk("playwright_driver", False,
            "playwright 未随包（精简版），「一键开通」不可用", required=False)
    except Exception as e:                       # noqa: BLE001
        chk("playwright_driver", False, f"{type(e).__name__}: {e}")
    return out


def _arm_selftest(app, window) -> None:
    """`--selftest`：等界面跑稳 → 检查 → 写 `logs/selftest_*.txt` → 自动退出。"""
    import json
    from datetime import datetime
    from pathlib import Path
    from PySide6.QtCore import QTimer

    def _finish():
        try:
            checks = _selftest_checks(window)
        except Exception as e:                   # noqa: BLE001
            checks = {"selftest_crashed": {"ok": False, "required": True,
                                           "detail": f"{type(e).__name__}: {e}"}}
        bad = [k for k, v in checks.items() if v["required"] and not v["ok"]]
        ok = not bad
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        try:
            from src.version import __version__
            _ver = __version__
        except Exception:                        # noqa: BLE001
            _ver = "?"
        report = {
            "version": _ver,
            "time": datetime.now().isoformat(timespec="seconds"),
            "exe": sys.executable,
            "frozen": bool(getattr(sys, "frozen", False)),
            "base_dir": BASE_DIR,
            "python": sys.version,
            "ok": ok,
            "failed_required": bad,
            "checks": checks,
        }
        path = Path(BASE_DIR) / "logs" / f"selftest_{ts}.txt"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                            encoding="utf-8")
        except Exception as e:                   # noqa: BLE001
            log.error(f"SELFTEST: 写报告失败: {e}")
        if ok:
            log.info(f"SELFTEST: OK（{len(checks)} 项全过）→ {path}")
        else:
            log.error(f"SELFTEST: FAIL → {path}；没过的是: {bad}")
        for k, v in checks.items():
            if v["required"] and not v["ok"]:
                log.error(f"SELFTEST FAIL: {k} = {v['detail']}")
        # ★ 退出码：0 = 全过；3 = 必检项没过（自动化脚本据此判断）
        app.exit(0 if ok else 3)

    log.info(f"SELFTEST: 启动自检，{_SELFTEST_MS} ms 后出结论")
    # ★★ 自检模式额外开一个**看门狗**：每 20 秒把所有线程的 C 级堆栈 dump 到
    #   `logs/main_stderr.log`。用途是排查"exe 起来了但卡住不动"——
    #   那种情况没有异常、日志也停在半句，只有线程堆栈能说清卡在哪。
    try:
        import faulthandler as _fh
        _fh.dump_traceback_later(20, repeat=True)
    except Exception:                            # noqa: BLE001
        pass
    QTimer.singleShot(max(1000, _SELFTEST_MS), _finish)


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
        # ★★ 1.9.4（用户 2026-10-19）：**应用图标 = 托盘图标**。
        #   为什么必须在这里设、不能等 MainWindow：
        #     · 首次配置引导（FirstRunWizard）是在 MainWindow **之前**弹出的，
        #       那时还没 `setWindowIcon` → 引导窗口的任务栏图标是 Qt 默认的（难看）；
        #     · 托盘那边虽然也会 `app.setWindowIcon(...)`，但那是"主窗口建好之后"。
        #   ⇒ 在 QApplication 一建好就设，之后所有窗口/对话框自动继承。
        #   ★ 图标文件是 `data/icon.ico`（由 `tools/make_app_icon.py` 生成，
        #     与 exe 里嵌的是**同一份**，见 PRTS.spec 的 `EXE(icon=...)`）。
        try:
            from pathlib import Path as _P
            from PySide6.QtGui import QIcon
            _icon_path = _P(BASE_DIR) / "data" / "icon.ico"
            if _icon_path.is_file():
                app.setWindowIcon(QIcon(str(_icon_path)))
                log.info("应用图标已设置: data/icon.ico")
            else:
                log.warning("找不到 data/icon.ico（任务栏图标会用系统默认）")
        except Exception as _e:
            log.warning(f"设置应用图标失败（不影响使用）: {_e}")
        log.info("QApplication 初始化完成")

        # ★★ 1.9.4：**首次运行**弹一次「首次配置引导」。
        #   · 判据保守（should_show → 只差 LLM Key 才算首次）→ **老用户绝不会被打扰**；
        #   · 放在 MainWindow() **之前** —— 用户填的 Key 才能在主窗口建 LLM 客户端之前生效；
        #   · 整体 try 包住：引导本身出任何问题都**不许拖垮启动**（它只是"更好用"，不是"没它不行"）。
        try:
            from src.ui.first_run_wizard import FirstRunWizard, should_show
            if _SELFTEST:
                # ★ 自检不能卡在模态向导上（fresh 目录里 should_show() 一定是 True）
                log.info("SELFTEST: 跳过首次配置引导")
            elif should_show():
                log.info("检测到尚未配置 LLM Key → 弹出首次配置引导")
                FirstRunWizard().exec()
                log.info("首次配置引导结束")
        except Exception as _e:
            log.warning(f"首次配置引导失败（不阻断启动）: {_e}")

        from src.ui.main_window import MainWindow
        log.info("MainWindow 导入完成")

        window = MainWindow()
        log.info("MainWindow 创建完成")

        if _SELFTEST:
            _arm_selftest(app, window)

        window.run()
        log.info("主循环启动...")

        _rc = app.exec()
        if _SELFTEST:
            log.info(f"SELFTEST: 界面已退出（返回码 {_rc}）")
        sys.exit(_rc)

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
