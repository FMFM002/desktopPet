"""
桌宠动画组件
- 启动时扫描 data/ 目录
- 按 角色名/行为/帧文件 结构组织
- 支持 GIF 和多帧 PNG
- 支持拖拽移动
"""
import logging
import time
from pathlib import Path
from typing import Optional
from PySide6.QtCore import Qt, QTimer, Signal, QPoint
from PySide6.QtGui import QPixmap, QMovie, QMouseEvent, QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import QLabel, QWidget, QVBoxLayout, QApplication

_log = logging.getLogger("PetAnim")

# ★★★ 1.9.2：nativeEvent 用的 MSG 头结构体 —— **必须在模块级只建一次**。
#
# 为什么不能写在函数里（这是子进程内存缓慢上升的真凶，已量化复现）：
#   CPython 的 `ctypes.POINTER(T)` 会把结果缓存进**模块级永久字典**
#   `ctypes._pointer_type_cache`，**key 就是 T、强引用**。
#   所以如果每次调用都新建一个 `_MsgHdr` 类再对它取 POINTER，
#   每来一条原生窗口消息就会往那个缓存里塞一条**永不回收**的条目：
#       实测（独立 CPython 3.12.10，20000 次）：
#         对象图 +103.16 MB（≈5408 B/次）
#         RSS +112 MB（≈5883 B/次）
#         `_pointer_type_cache` 20 → 20021 条，gc.collect() 后仍是 20021
#   而 nativeEvent 会被频繁调用（看门狗每 3 秒重绘一次，每次重绘都产生原生消息），
#   于是形象子进程内存从 42MB 慢慢往上爬 —— 用户报的正是这个现象。
#   （注意：用户原先怀疑的"切换 GIF 留内存"**不成立**，旧的 QMovie 清理链是完整的。）
import ctypes as _ctypes


class _MsgHdr(_ctypes.Structure):
    """只需要 MSG 的前两个字段（hwnd, message）—— 定义得越少越不容易踩对齐坑。"""
    _fields_ = [("hwnd", _ctypes.c_void_p), ("message", _ctypes.c_uint)]


_MSG_HDR_PTR = _ctypes.POINTER(_MsgHdr)
_NATIVE_ERR_LOGGED = False    # ★ R73：nativeEvent 失败只报一次，防止刷屏      # ★ 只建一次，之后反复 cast 复用它


BASE_DIR = Path(__file__).resolve().parent.parent.parent
DATA_DIR = BASE_DIR / "data"
# ★ 1.5.0: 形象目录迁移 data/<角色>/ → data/桌宠形象/<角色>/
CHARACTERS_DIR = DATA_DIR / "桌宠形象"


class PetAnimationWidget(QWidget):
    """桌宠形象组件"""

    behavior_changed = Signal(str)
    request_behavior = Signal(str)  # 跨线程安全的行为切换信号
    pet_moved = Signal()            # 拖拽移动后发出，用于重定位聊天窗口
    pet_clicked = Signal()          # 点击桌宠时发出
    file_dropped = Signal(str)      # ★ 拖拽文件到形象
    gif_once_finished = Signal()    # ★ 1.6.0: 「只播放一遍」的 GIF 播放完毕
    BEHAVIORS = ["待机", "说话", "思考", "执行"]

    PET_SIZE = 128  # 默认尺寸

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_NoSystemBackground)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, False)
        self.setAcceptDrops(True)  # ★ 接受拖拽文件

        self._characters: dict[str, dict] = {}
        self._current_character = "铃兰"
        self._current_behavior = "待机"
        self._current_frame_idx = 0
        self._drag_pos: Optional[QPoint] = None

        self._movie: Optional[QMovie] = None
        self._last_file = ""             # ★ 1.9.0: 最近一次加载的 GIF/图片（自愈用）
        # ★ 1.6.0: GIF 播放控制（once=只播放一遍）
        self._loop_gif = True
        self._gif_loops_done = 0
        self._gif_frames = 0
        self._gif_last_frame = -1
        self._gif_once_notified = False
        self._behavior_override = ""
        self._label = QLabel(self)
        self._label.setAlignment(Qt.AlignCenter)
        self._label.setStyleSheet("background: transparent;")
        self._label.setMinimumSize(64, 64)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._label)

        self._frame_timer = QTimer(self)
        self._frame_timer.timeout.connect(self._next_frame)
        self._frame_interval = 100

        # ★ 1.9.3 第 33 轮：缩放后帧的缓存（修"会抽帧"，见 _show_single_frame）。
        #   key = (图片路径 或 GIF帧cacheKey, 宽, 高) → 已缩放的 QPixmap。
        #   只放缩放后的小图，源图不驻留。
        self._pix_cache: dict = {}

        # ★ 1.9.1: "形象消失"自愈（详见 src/ui/win_window.py 的完整说明）
        #   Win32 的前台窗口事件在**别的线程**回调 → 只置标志；
        #   这个定时器在主线程把它取出来处理（1.6 秒节流，别为了切窗口就狂重绘）。
        self._heal_pending = False
        self._heal_reason = ""
        self._heal_strong = False        # ★ 是否要做重度自愈（hide→show 重建表面）
        # ★ 上一次轮询时"前台窗口是不是全屏" —— 用它检测**退出全屏**那一刻。
        #   只检测"进入全屏"是不够的：真机上形象是在**退出**查看器时消失的。
        self._was_fg_fullscreen = False
        # ★ 1.9.1（19:38 日志实锤）：图片查看器（照片 app）是**普通窗口**
        #   （真机 (114,114,1920x1117)），全屏判据罩不住它 —— 它的进/出
        #   是独立的重度自愈触发器。
        self._was_fg_viewer = False
        self._last_heal = 0.0
        self._heal_timer = QTimer(self)
        self._heal_timer.setInterval(700)
        self._heal_timer.timeout.connect(self._check_heal)
        self._win32_ready = False

        # 跨线程安全：request_behavior 信号通过 Qt 队列确保主线程执行
        self.request_behavior.connect(self.set_behavior)

        self.scan_assets()

        # 默认大小和位置：屏幕右下角
        self.resize(self.PET_SIZE, self.PET_SIZE)
        self._move_to_default_position()

        _log.info(f"桌宠组件就绪: {len(self._characters)} 个角色")

    def _move_to_default_position(self):
        """将桌宠放到屏幕右下角"""
        try:
            from PySide6.QtGui import QGuiApplication
            screen = QGuiApplication.primaryScreen()
            if screen:
                geo = screen.availableGeometry()
                self.move(
                    geo.right() - self.width() - 20,
                    geo.bottom() - self.height() - 60
                )
                _log.debug(f"桌宠定位: 右下角 ({self.x()}, {self.y()})")
        except Exception:
            self.move(100, 100)

    def scan_assets(self):
        self._characters.clear()

        root = CHARACTERS_DIR if CHARACTERS_DIR.exists() else DATA_DIR
        if not root.exists():
            _log.warning(f"形象目录不存在,创建中: {root}")
            root.mkdir(parents=True, exist_ok=True)
            return

        for char_dir in sorted(root.iterdir()):
            if not char_dir.is_dir() or char_dir.name.startswith("."):
                continue
            char_name = char_dir.name
            self._characters[char_name] = {}
            # ★ 递归扫描：任何子目录下的 GIF 都按「行为目录/文件名」两级索引
            for f in sorted(char_dir.rglob("*")):
                if not f.is_file() or f.suffix.lower() not in (".gif", ".png", ".webp"):
                    continue
                behavior = f.parent.name if f.parent != char_dir else "默认"
                self._characters[char_name].setdefault(behavior, []).append(str(f))
            _log.debug(f"  角色 '{char_name}': {list(self._characters[char_name].keys())}")

        for name, behaviors in self._characters.items():
            _log.info(f"  角色 '{name}': {list(behaviors.keys())}")

    def get_characters(self) -> list[str]:
        return list(self._characters.keys())

    def get_behaviors(self, character: str = None) -> list[str]:
        char = character or self._current_character
        if char in self._characters:
            return list(self._characters[char].keys())
        return []

    def set_character(self, name: str):
        if name in self._characters:
            self._current_character = name
            self._current_behavior = ""  # 重置，避免 set_behavior 的 early return 跳过加载
            self.set_behavior("待机")
            _log.debug(f"切换角色: {name}")
        else:
            _log.warning(f"角色不存在: {name}")

    def set_behavior(self, behavior: str):
        if behavior == self._current_behavior:
            return

        char_data = self._characters.get(self._current_character, {})
        if behavior not in char_data:
            # ★ 1.5.0: 无对应行为时回退到待机 → 任意可用资源
            behavior = "待机" if "待机" in char_data else (next(iter(char_data), None))
            if not behavior:
                _log.debug(f"行为 '{behavior}' 无资源,跳过")
                return

        _log.debug(f"切换行为: {self._current_character}/{behavior}")
        files = char_data[behavior]
        self.set_gif_file(files[0], behavior_label=behavior)

    def set_gif_file(self, file_path: str, behavior_label: str = "", once: bool = False):
        """★ 1.5.0: 直接按文件路径加载 GIF（由形象线程 PetManager 指定）

        ★ 1.6.0: once=True 时该 GIF「只播放一遍」，播完发出 gif_once_finished
        （用于「启动默认 GIF 只播放一遍再切入待机」）。
        """
        if not file_path:
            return
        self._last_file = file_path      # ★ 1.9.0: 自愈时用它重载（见 repaint_current）
        if behavior_label:
            self._current_behavior = behavior_label
        else:
            self._current_behavior = Path(file_path).stem
        self._current_frame_idx = 0
        self._behavior_override = behavior_label
        self._loop_gif = not once
        self._gif_loops_done = 0
        self._gif_frames = 0
        self._gif_last_frame = -1
        self._gif_once_notified = False
        self.behavior_changed.emit(self._current_behavior)

        if self._movie:
            self._movie.stop()
            try:
                self._movie.frameChanged.disconnect(self._on_gif_frame)
            except Exception:
                pass
            self._movie = None
        self._frame_timer.stop()

        if file_path.lower().endswith(".gif"):
            self._movie = QMovie(file_path)
            # ★ 1.9.2（内存调研实测结论，用户批准的单行改动）：
            #   原来是 QMovie.CacheAll —— 它会在**载入时把整段 GIF 全部解码**驻留内存。
            #   实测（500×500 / 132 帧 / 6.30 MB 的"盖被子睡着了.gif"，独立进程 RSS）：
            #       CacheAll   +127.9 MB
            #       CacheNone  +  1.9 MB      ← 差 ~126 MB
            #   而播放效果看不出差别（QMovie 本来就会为当前帧做解码，
            #   下一帧在 24fps 下有一整个 41ms 的预算，实测无卡顿）。
            #   形象子进程实测峰值也从 181.9 MB 降下来。
            #   想退回旧行为：把下面这行的 CacheNone 改回 CacheAll 即可。
            #   （相关：连切 6 个 ≥3MB 的 GIF 实测不累积，说明旧 QMovie 释放是好的，
            #     所以这里省下的是**每张图整段解码**的那一份，不是泄漏。）
            self._movie.setCacheMode(QMovie.CacheNone)
            try:
                self._gif_frames = int(self._movie.frameCount() or 0)
            except Exception:
                self._gif_frames = 0
            self._movie.frameChanged.connect(self._on_gif_frame)
            self._movie.start()
        else:
            self._show_single_frame(file_path)

    def _on_gif_frame(self, frame_num: int):
        """GIF 逐帧平滑缩放 —— 避免 setScaledContents 导致的模糊

        ★ 1.6.0: 对「只播放一遍」的 GIF，必须完整播完**一整轮**才暂停并发完成信号。
        注意两个坑：① 第 1 帧的帧号就是 0，不能拿 0 当"回绕"；
        ② 首帧回调时 frame_count 可能还是 0，此时不能靠除法推轮次。
        因此改为「帧号不再增长（回绕/重播）」作为一轮结束的判据。
        """
        if self._movie:
            if not self._loop_gif:
                if self._gif_once_notified:
                    return
                if self._gif_frames <= 0:
                    # 帧数未知（未解析/单帧）：无法可靠判断，交给兜底定时器
                    pass
                elif frame_num > self._gif_last_frame:
                    self._gif_last_frame = frame_num
                else:
                    # 帧号回绕 = 完整播放了一轮
                    self._gif_once_notified = True
                    self._movie.setPaused(True)
                    try:
                        self.gif_once_finished.emit()
                    except Exception:
                        pass
                    return

            pixmap = self._movie.currentPixmap()
            if not pixmap.isNull():
                # ★ 1.9.3 第 33 轮：GIF 帧**同样每帧做一次 SmoothTransformation**
                #   （GIF 常见 24fps，41ms 预算里做高质量重采样很容易掉帧）。
                #   用 `cacheKey()` 当帧标识 —— 同一帧再次出现（循环播放的常态）
                #   就直接复用缩放结果，不再重算。
                try:
                    w, h = self.width(), self.height()
                    key = ("gif", pixmap.cacheKey(), w, h)
                    scaled = self._pix_cache.get(key)
                    if scaled is None:
                        scaled = pixmap.scaled(w, h, Qt.KeepAspectRatio,
                                               Qt.SmoothTransformation)
                        if len(self._pix_cache) >= 80:
                            for _k in list(self._pix_cache.keys())[:20]:
                                self._pix_cache.pop(_k, None)
                        self._pix_cache[key] = scaled
                except Exception:
                    scaled = pixmap.scaled(
                        self.width(), self.height(),
                        Qt.KeepAspectRatio, Qt.SmoothTransformation)
                self._label.setPixmap(scaled)
                # ★★ 1.9.3 第 39 轮：**GIF 这条路也要探针**！
                #   用户报"用着看见明显抽帧"，但日志里**一条 `[抽帧探针]` 都没有** ——
                #   查证：他的形象 `data/桌宠形象/deepseek` 是 **20 个 .gif**，
                #   走的是 `QMovie`（`frameChanged` → 本函数），
                #   而探针当初只挂在 `_next_frame()`（**PNG 序列**那条路）上，
                #   ⇒ **GIF 形象根本没人测**。现在两条路都测。
                self._probe_tick()

    def _show_current_frame(self):
        char_data = self._characters.get(self._current_character, {})
        files = char_data.get(self._current_behavior, [])
        if not files:
            return
        if self._current_frame_idx >= len(files):
            self._current_frame_idx = 0
        self._show_single_frame(files[self._current_frame_idx])

    def _next_frame(self):
        self._current_frame_idx += 1
        self._show_current_frame()
        self._probe_tick()

    def _probe_tick(self):
        """★★ 1.9.3 第 33 轮（队列 Q4 本体就是"探针"）：**给抽帧装个探针**。

        为什么先装探针、而不是继续猜：
          · 我量过：单帧"读盘 + SmoothTransformation"在 900×900/374KB 的图上是
            **0.54 ms**（100ms 预算的 0.5%）—— 虽然已顺手缓存掉（快 121 倍），
            但**这个量级根本不足以造成肉眼可见的抽帧**。
          · 10fps 的 `QTimer` 一旦**迟到**就会掉帧，而 Qt 定时器**不补拍**。
            真正让主线程忙起来的是：聊天流式输出（每个 token 都往
            QTextDocument 里塞）、监控事件、DB 写入、切角色/开设置窗口。
          · 所以正确做法是**记下每次实际间隔**：迟到多少、多久一次，
            日志会直接告诉我们"卡在哪、卡多少"，而不是继续盲猜。
        阈值取 1.8× 间隔（10fps → 180ms），最多 8 秒报一次，避免刷屏。
        """
        import time as _t
        now = _t.monotonic()
        last = getattr(self, "_probe_last_tick", None)
        self._probe_last_tick = now
        if last is None:
            self._probe_dropped = 0
            self._probe_worst = 0.0
            self._probe_last_report = now
            return
        gap_ms = (now - last) * 1000.0
        # ★ 第 39 轮：**预算要按"这个形象本来的帧间隔"算**。
        #   GIF 走 QMovie，帧间隔由 GIF 自己定（常见 40~100ms）；
        #   统一按 100ms 算会把 24fps 的 GIF 漏掉。所以优先问 QMovie。
        budget_ms = getattr(self, "_frame_interval", 100)
        try:
            if self._movie is not None and self._movie.isValid():
                _d = self._movie.nextFrameDelay()
                if _d and _d > 0:
                    budget_ms = max(20, min(500, int(_d)))
        except Exception:
            pass
        if gap_ms > budget_ms * 1.8:
            self._probe_dropped = getattr(self, "_probe_dropped", 0) + 1
            self._probe_worst = max(getattr(self, "_probe_worst", 0.0), gap_ms)
            if now - getattr(self, "_probe_last_report", 0.0) >= 8.0:
                _log.warning(f"[抽帧探针] 本段时间隔超过 {budget_ms * 1.8:.0f}ms 的帧 "
                             f"{self._probe_dropped} 次，最差 {self._probe_worst:.0f}ms"
                             f"（预算 {budget_ms}ms，"
                             f"{'GIF/QMovie' if self._movie else 'PNG序列'}）")
                self._probe_dropped = 0
                self._probe_worst = 0.0
                self._probe_last_report = now

    def _win32_setup(self):
        """★ 1.9.1: 形象窗口的 Win32 加固（见 src/ui/win_window.py 的完整说明）。

        做三件事，全部走 try/except，失败也不影响形象：
          ① 加 `WS_EX_NOACTIVATE` —— 点形象**不抢焦点**（采纳用户思路 1 里真正有用的那半）；
          ② 记下 hwnd，供 `reassert_win32()` 重新声明窗口状态；
          ③ 装前台窗口变化监听 —— 这是 `WM_DWMCOMPOSITIONCHANGED` 在现代 Windows 上的
             替代品（后者 Win8+ 永远不发，详见 win_window.py 的文档核对）。
        在非 Windows 上整个函数是空操作。
        """
        try:
            from src.ui import win_window as _ww
        except Exception as e:
            _log.debug(f"win_window 不可用: {e}")
            return
        self._ww = _ww
        try:
            self._hwnd = _ww.hwnd_of(self)
            if _ww.USE_COMPOSITED:
                _ww.apply_composited(self)
            _ww.apply_noactivate(self)
            _log.info(f"[窗口] 形象窗口 Win32 加固完成: {_ww.snapshot_str(self)}")
        except Exception as e:
            _log.debug(f"Win32 加固失败: {e}")
        try:
            # 前台变化 → 由 pet_process 的队列转到主线程处理（回调在别的线程）
            _ww.install_foreground_hook(self._on_foreground_changed_win32)
        except Exception as e:
            _log.debug(f"装前台监听失败: {e}")

    def _on_foreground_changed_win32(self, payload):
        """WinEvent 回调（**不在主线程**）→ 只置个标志，绝不碰 GUI。

        Qt 的控件只能在主线程碰，而这个回调是 Win32 在别的线程上叫的；
        所以这里只翻转 bool，由主线程的 `_heal_timer` 去真正处理。
        （`QTimer.singleShot` 在非 Qt 线程里不触发 —— 项目里踩过的老坑。）

        ★★ 1.9.1 修「抽」（19:38 真机日志实锤）：以前这里对**每次**前台
          变化都置 pending → 主线程每切一次窗口就强制重画一次（清空 pixmap
          再设回去 + repaint）= 用户看到的"抽"（日志里切 PowerShell / 照片 /
          托盘，每次都跟着一条"触发自愈（前台窗口变化）"）。
          改成：**普通切换什么都不做**，只有"切到全屏窗口"才置（进全屏也
          救一次 —— 形象反正被全屏盖着，闪也看不见）。
          退出全屏 / 图片查看器进出这两个触发器由 700ms 轮询负责（_check_heal）。
        """
        try:
            full = False
            try:
                from src.ui import win_window as ww
                full = ww.foreground_covers_screen()
            except Exception:
                pass
            if full:
                self._heal_pending = True
                self._heal_strong = True
                self._heal_reason = "全屏程序进/出（前台窗口铺满屏幕）"
        except Exception:
            pass

    def _check_heal(self):
        """主线程侧：① 轮询"刚从全屏退出" ② 把 Win32 线程置的标志取出来处理。

        ★★ 1.9.1（用户 13:24 的场景描述驱动的重写）
        用户给的现场对比非常有价值：
          场景 A（文件管理器开图）：退出查看器 → 形象消失 → **等待也不回来**，
                                   必须手动点托盘"显示"才回来；
          场景 B（点聊天窗预览图）  ：退出查看器 → 形象消失 → **"兜底程序触发"自己回来了**。
        查现有兜底（pet_process 里每 3 秒的看门狗）发现：

            if _min or not pet.isVisible():   # ← 只在"最小化/不可见"时才 show()
                pet.show()
            pet.repaint_current()             # ← 而这句对"同一张图"是空操作

        而窗口状态快照证明：出问题时 **isVisible()=True、isMinimized()=False**。
        → 所以看门狗**从不调用 show()**，只做了那个无效的 repaint_current()
          —— 这正是场景 A "等也不回来"的原因。
        → 场景 B 能自己回来，说明**那一次窗口真的被最小化了**，命中了 `_min` 分支。
        （两种场景的差别不在"病"，而在"有没有顺手被最小化"。）

        结论：**不能靠窗口状态判断**（它一切正常），也**不能只靠事件**
        （场景 A 里前台窗口可能压根没变，WinEvent 根本不发）。
        唯一稳的办法：**在定时器里轮询"前台窗口是不是铺满屏幕"，
        一旦从"全屏"变成"不是全屏"（= 刚退出全屏），就无条件做重度自愈。**
        这跟窗口状态无关、跟事件无关，两个场景都覆盖。
        """
        # ── ① 轮询全屏进/出 + 图片查看器进/出（不依赖任何事件）──
        try:
            ww = getattr(self, "_ww", None)
            if ww is None:
                from src.ui import win_window as ww
                self._ww = ww
            _full = ww.foreground_covers_screen()
            # ★ 1.9.1（19:38 日志实锤）：照片查看器是**普通窗口**
            #   （真机 (114,114,1920x1117)），永远过不了全屏判据；
            #   形象恰恰是在它进/出时消失的 → 它需要独立触发器。
            try:
                _viewer = bool(ww.foreground_is_viewer())
            except Exception:
                _viewer = False
            # ★ 顺便把原始数字记下来 —— 上一轮我因为看不到这些数字，
            #   只能猜"为什么全屏检测没认出来"。现在前台一变就有据可查。
            try:
                _dbg = ww.foreground_debug()
            except Exception:
                _dbg = ""
            if _dbg and _dbg != getattr(self, "_last_fg_debug", ""):
                self._last_fg_debug = _dbg
                _log.info(f"[窗口] 前台诊断: {_dbg}")
            _exit_full = self._was_fg_fullscreen and not _full
            _viewer_chg = _viewer != getattr(self, "_was_fg_viewer", False)
            self._was_fg_fullscreen = _full
            self._was_fg_viewer = _viewer
            # ★ 托盘「隐藏桌宠」后不许自动救（19:42 日志：已隐藏还在
            #   空跑自愈、刷 WARNING）。两个触发器都要求窗口当前可见。
            if self.isVisible() and (_exit_full or _viewer_chg):
                self._heal_pending = True
                self._heal_strong = True
                if _exit_full:
                    self._heal_reason = "刚从全屏程序退出（轮询发现）"
                    _log.warning("[窗口] 检测到刚从全屏退出 → 准备强制重显形象")
                else:
                    self._heal_reason = "图片查看器进/出（轮询发现）"
                    _log.warning("[窗口] 检测到图片查看器进/出 → 准备强制重显形象")
        except Exception as e:
            _log.debug(f"全屏轮询失败: {e}")

        if not self._heal_pending:
            return
        now = time.time()
        if now - self._last_heal < 1.6:      # 节流：连续切窗口不必每次重绘
            return
        self._heal_pending = False
        strong = getattr(self, "_heal_strong", False)
        self._heal_strong = False
        self._last_heal = now
        reason = self._heal_reason or "前台窗口变化"
        self._heal_reason = ""
        self.heal_window(reason, strong=strong)

    def showEvent(self, event):
        """★ 1.9.1: 窗口第一次显示出来之后才拿得到有效 hwnd → 在这里做 Win32 加固。"""
        super().showEvent(event)
        if not self._win32_ready:
            self._win32_ready = True
            self._win32_setup()
            try:
                self._heal_timer.start()
            except Exception:
                pass

    def nativeEvent(self, eventType, message):
        """★ 1.9.1: 接住**真的会发**的那几条系统消息，触发窗口自愈。

        · `WM_DISPLAYCHANGE`(0x007E)——分辨率/显示模式变化，全屏游戏/查看器进出常常触发；
        · `WM_DPICHANGED`(0x02E0)——DPI 变化（副屏/缩放切换）；
        · `WM_DWMCOMPOSITIONCHANGED`(0x031E)——**Win8+ 永远不会发**（MSDN 明说），
          这里只是顺手接一下旧系统，别指望它（真正的替代是前台窗口监听）。
        ★ 我们只**观察**不**消费**：一律返回 (False, 0)，让 Qt 继续正常处理。

        ★★★ 1.9.2 修 —— **子进程内存缓慢上升的真凶就在这里**（专项排查+量化复现）：
          原来每次调用都**新建一个 `_MsgHdr` 类**，再对这个新类调
          `ctypes.POINTER(_MsgHdr)`。而 CPython 的 `ctypes.POINTER()` 会把结果
          存进**模块级永久缓存** `ctypes._pointer_type_cache`，**key 就是这个类、
          强引用** → 这个类永远不回收，每次调用都是一次 cache miss + 一条永久条目。
          实测（独立 CPython 3.12.10，20000 次调用）：
              · 对象图 +103.16 MB（约 5408 B/次）
              · RSS 15.62 → 127.82 MB（约 5883 B/次）
              · `_pointer_type_cache` 20 → 20021 条，**gc.collect() 之后仍是 20021**
          而 nativeEvent 会被**频繁**调用（看门狗每 3 秒一次重绘 + 自愈，
          每次重绘都产生原生消息）→ 每小时十几~几十 MB，与实测"42MB 后缓慢上升"吻合。
          修法：把结构体与指针类型**提到模块级只建一次**（见文件下方 _MSG_HDR_PTR）。
        """
        # ★ 提前退出：不是 Windows 原生消息就不用解析（也少一次函数体开销）
        try:
            if eventType not in (b"windows_generic_MSG", b"windows_dispatcher_MSG"):
                return False, 0
        except Exception:
            pass
        try:
            addr = message if isinstance(message, int) else int(message)
            if addr:
                m = _ctypes.cast(addr, _MSG_HDR_PTR).contents
                if m.message in (0x007E, 0x02E0, 0x031E):
                    self._heal_pending = True
                    self._heal_reason = {
                        0x007E: "WM_DISPLAYCHANGE（显示模式变化）",
                        0x02E0: "WM_DPICHANGED（DPI 变化）",
                        0x031E: "WM_DWMCOMPOSITIONCHANGED",
                    }.get(m.message, "系统消息")
        except Exception as e:
            # ★ 1.9.2（R73）：**只报一次**。这条原本是 debug，但 nativeEvent 是
            #   超热路径（每条 Windows 消息都走），一旦真的出错就会**每秒刷几千行**、
            #   把日志刷到几 MB，并且让主进程卡死/崩溃（真机 2026-09-20 就是这么崩的）。
            global _NATIVE_ERR_LOGGED
            if not _NATIVE_ERR_LOGGED:
                _NATIVE_ERR_LOGGED = True
                _log.warning(f"nativeEvent 解析失败（只报这一次）: {e}")
        # ★ PySide6 要求返回 (handled, result) 的元组；False = 这条消息我们没吃
        return False, 0

    def _content_snapshot(self) -> str:
        """★ 1.9.1: 画面侧的诊断（和 win_window 的窗口状态快照互补）。

        真机结论：出问题时**窗口状态全是正常的**（visible/layered/topmost 都在），
        所以问题只可能在"画面有没有被真正呈现"。这一行回答：
          GIF 还在播吗？当前帧是空图吗？标签上有没有实际 pixmap、多大？
        """
        try:
            st = "无电影"
            if self._movie is not None:
                st = {QMovie.NotRunning: "停", QMovie.Paused: "暂停",
                      QMovie.Running: "在播"}.get(self._movie.state(), str(self._movie.state()))
            pm = self._movie.currentPixmap() if self._movie is not None else None
            pm_null = True if pm is None else bool(pm.isNull())
            lab = self._label.pixmap()
            lab_size = "无" if (lab is None or lab.isNull()) else f"{lab.width()}x{lab.height()}"
            return (f"gif={st} 当前帧空={pm_null} 标签pixmap={lab_size} "
                    f"窗口={self.width()}x{self.height()} 上一文件={getattr(self, '_last_file', '')!r}")
        except Exception as e:
            return f"内容快照失败: {e}"

    def heal_window(self, reason: str = "", strong: bool = False):
        """★ 1.9.1: 形象"消失"的统一自愈入口（在主线程调用）。

        【真机诊断结论（2026-09-18 13:12 日志）】
          出问题时窗口状态**全都是对的**：
            `qt_visible=True visible=True iconic=False layered=True topmost=True
             noactivate=True rect=2264,756 195x195`
          —— 可见、没最小化、还是分层窗口、还是置顶、位置尺寸都对。
          也就是说：**窗口没坏，是"分层窗口的画面没被真正呈现"**。
          → 所以"改窗口样式/重设 topmost/RedrawWindow"这类做法**治不了**
            （窗口状态从来没坏过）——这也解释了为什么用户说"两种场景都复现了"。
          → 而用户自己的土办法"用托盘隐藏/显示切一下才回来"恰恰说明：
            **只有 hide→show 重建分层表面才有效**。

        【因此这一版分两级】
          · 轻量（总是做）：Win32 重新声明 + **真正强制重画**
            （关键是：光 `setPixmap(同一张图)` 是**空操作**，Qt 不会重绘 ——
             必须先清空再设回去 + `repaint()`，才会真的把像素重新推给分层表面）；
          · 重度（`strong=True` 时）：**hide → show 一轮**（用户验证过的解法），
            之后重新声明 topmost（show 会让它掉出置顶带）。
          只在"全屏程序进/出"这类场合才走重度，免得每次 Alt+Tab 都闪一下。
        """
        why = reason or "未注明"
        try:
            ww = getattr(self, "_ww", None)
            if ww is None:
                from src.ui import win_window as ww
            _log.warning(f"[窗口] 触发自愈（{why}{'，重度' if strong else ''}）: "
                         f"{ww.snapshot_str(self)}\n"
                         f"        内容快照: {self._content_snapshot()}")
            ww.reassert(self, reason=why)
        except Exception as e:
            _log.debug(f"Win32 自愈失败: {e}")

        # ① 轻量：真正强制重绘（clear → repaint → 设回 → repaint）
        try:
            self._force_repaint()
        except Exception as e:
            _log.debug(f"强制重绘失败: {e}")

        # ② 重度：hide→show 重建分层表面（用户土办法验证过的解法）
        #
        # ★★ 1.9.2 修（用户报"形象平时就在闪烁"，日志里抓到了机制）：
        #     16:02:46 自愈(重度): qt_visible=False visible=False → 该重建 ✅
        #     16:02:49 自愈(重度): qt_visible=True  visible=True  → **还是重建了** ❌
        #   第二行就是闪烁的来源：窗口**已经可见、也没被 cloaked**，说明画面
        #   正常呈现着，此时 hide→show 只会让它肉眼闪一下、一点好处都没有。
        #   所以这里加一道判定：只有"真的没在呈现"才走重度。
        if strong:
            _need_rebuild = True
            try:
                _vis = bool(self.isVisible())
                try:
                    _iconic = bool(self.isMinimized())
                except Exception:
                    _iconic = False
                _cloaked = False
                try:
                    _cloaked = bool(ww.is_cloaked(self))
                except Exception:
                    pass
                _need_rebuild = (not _vis) or _iconic or _cloaked
            except Exception:
                _need_rebuild = True
            if _need_rebuild:
                try:
                    self._recreate_surface(why)
                except Exception as e:
                    _log.warning(f"[窗口] 重建分层表面失败: {e}")
            else:
                # 可见且未被 cloaked → 只做轻量重绘就够了，别闪
                _log.info("[窗口] 可见且未被 cloaked → 跳过 hide→show"
                          "（避免无谓闪烁）")

        try:
            _log.warning(f"[窗口] 自愈完成: {self._content_snapshot()}")
        except Exception:
            pass

    def force_repaint(self):
        """★ 1.9.1: 对外暴露"真·强制重绘"（pet_process 的看门狗会调它）。

        为什么单开一个公开名字：原来 pet_process 调的是 `repaint_current()`，
        而那句在"同一张 pixmap 再设一遍"时是**空操作**（Qt 发现没变就不重绘）
        → 救不了"分层表面没被呈现"。这个才是真的会重画。
        """
        self._force_repaint()

    def reassert_window(self):
        """★ 1.9.1: 周期自检入口（pet_process 的看门狗每次自检都调它）。

        ★★ 关于"抽"（用户 13:39 报的副作用）
        上一版我在**健康时也每 3 秒做一次 `SWP_SHOWWINDOW` 重声明**，
        结果用户明确反馈："对，会抽。"
        → 所以现在改成：**健康时什么都不做**。
          只有 DWM 真的说它没在呈现（cloaked）时才动手重建。

        ★★ 关于"怎么检测"（换掉了"猜时机"）
        真机证据：失效时窗口状态和画面都正常，靠"是不是刚从全屏退出"去猜
        **两次都没认出来**（`DWM 报告 cloaked` 一次都没命中，真正救回来的是
        全屏进/出那条路径）。所以这里的判据是**直接问 DWM**：
        `DWMWA_CLOAKED` 非 0 = DWM 确认没在渲染它 → 才做 hide→show 重建。
        平时（绝大多数时间）这个函数什么都不做，不会有任何视觉影响。
        """
        try:
            ww = getattr(self, "_ww", None)
            if ww is None:
                from src.ui import win_window as ww
                self._ww = ww
            cloaked = ww.is_cloaked(self)
            if cloaked is True:
                _log.warning("[窗口] ★ DWM 报告该窗口被 cloaked（没在呈现）→ 重建分层表面\n"
                             f"        窗口侧: {ww.snapshot_str(self)}\n"
                             f"        画面侧: {self._content_snapshot()}")
                self.heal_window("DWM 报告 cloaked（周期自检发现）", strong=True)
            # ★ 健康（cloaked 为 False/None）→ **什么都不做**。
            #   上一版这里会做 SWP_SHOWWINDOW 重声明，用户实测"会抽"，已去掉。
        except Exception as e:
            _log.debug(f"周期自检失败: {e}")

    def _force_repaint(self):
        """★ 真·强制重绘。

        为什么不能只 `setPixmap(同一张)`：Qt 发现 pixmap 没变就**不会重绘**，
        分层表面还是旧的（这就是"重声明了但没效果"的原因之一）。
        所以：先 clear 制造一次"内容变了"，`repaint()` 立刻同步重画一次，
        再设回真实帧、再 `repaint()`。
        """
        pm = None
        try:
            if self._movie is not None and self._movie.state() != QMovie.NotRunning:
                pm = self._movie.currentPixmap()
        except Exception:
            pm = None
        if pm is None or pm.isNull():
            # 帧是空的 → 走原来那条"重载文件"的路
            self.repaint_current()
            return
        scaled = pm.scaled(self.width(), self.height(),
                           Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self._label.clear()          # ① 制造一次内容变化
        self._label.repaint()        # ② 同步重画（把"空"推上去）
        self._label.setPixmap(scaled)  # ③ 设回真实帧
        self._label.repaint()        # ④ 再同步重画一次
        self.repaint()

    def _recreate_surface(self, why: str):
        """★ hide → show 一轮，强制 Qt/DWM 重建分层窗口表面。

        用户实测的土办法就是这个（"用托盘隐藏/显示切一下才回来"），
        所以把它自动化。做完必须**重新声明 topmost** ——
        hide/show 会让窗口掉出置顶带。
        """
        _log.warning(f"[窗口] 重建分层表面（hide→show，原因：{why}）")
        self.hide()
        QApplication.processEvents()
        self.show()
        self.raise_()
        try:
            from src.ui import win_window as ww
            ww.reassert(self, reason=f"{why}（重建后重新置顶）")
        except Exception as e:
            _log.debug(f"重建后重新置顶失败: {e}")

    def repaint_current(self):
        """★ 1.9.0: 把当前帧重新画一遍 —— 形象"凭空消失"的自愈手段。

        真机现象：形象偶尔会**凭空不见**（子进程还活着、日志里也没有任何
        hide 命令），用托盘"隐藏/显示"切一下才回来。Windows 上
        「无边框 + 半透明(WA_TranslucentBackground) + 置顶」这类分层窗口
        偶发合成失效是老毛病；另一种可能是标签最后一帧变成了空图。
        两种都能靠"重新 setPixmap + 必要时重载 GIF"救回来。
        """
        try:
            if self._movie is not None and self._movie.state() != QMovie.NotRunning:
                pm = self._movie.currentPixmap()
                if not pm.isNull():
                    self._label.setPixmap(pm.scaled(
                        self.width(), self.height(),
                        Qt.KeepAspectRatio, Qt.SmoothTransformation))
                    return
            last = getattr(self, "_last_file", "")
            if last and Path(last).exists():
                self.set_gif_file(last)          # 电影没了/帧空了 → 重新加载
                return
            self._show_current_frame()
        except Exception as e:
            _log.debug(f"重画当前帧失败: {e}")

    def _show_single_frame(self, filepath: str):
        """★ 1.9.3 第 33 轮：**加缓存** —— 修用户报的"会抽帧"。

        用户 2026-10-04："（Q4 那个）会抽帧。"

        查证：老实现**每一帧**都要
          ① `QPixmap(filepath)` —— **读盘 + PNG 解码**
          ② `scaled(..., SmoothTransformation)` —— **每帧做一次高质量重采样**
          而帧定时器是 100ms（10fps）—— 一旦主线程忙（heal 定时器、切窗口、
        监控事件、GC…），这一帧就跑不完 → Qt 定时器**不会补拍**，
        于是"帧被跳过/动画一顿一顿"= 用户看到的**抽帧**。

        现在：按 `(路径, 宽, 高)` 缓存**缩放后**的 QPixmap，
        命中时只做一次 `setPixmap`（微秒级），每帧不再碰磁盘也不再做重采样。
        · 尺寸变了自然 miss（key 带宽高），旧条目靠上限裁剪；
        · 只缓存**缩放后**的小图（源图不进缓存，省内存）。
        """
        try:
            w, h = self.width(), self.height()
            key = (filepath, w, h)
            cache = getattr(self, "_pix_cache", None)
            if cache is None:
                cache = self._pix_cache = {}
            scaled = cache.get(key)
            if scaled is None:
                pixmap = QPixmap(filepath)
                if pixmap.isNull():
                    return
                scaled = pixmap.scaled(w, h, Qt.KeepAspectRatio,
                                       Qt.SmoothTransformation)
                # 上限：按插入顺序丢最旧的（够用且简单，不做 LRU 计数）
                if len(cache) >= 80:
                    for _k in list(cache.keys())[:20]:
                        cache.pop(_k, None)
                cache[key] = scaled
            if not scaled.isNull():
                self._label.setPixmap(scaled)
        except Exception as e:
            _log.error(f"加载图片失败 {filepath}: {e}")

    # ============ 拖拽 ============

    def mousePressEvent(self, event: QMouseEvent):
        if event.button() == Qt.LeftButton:
            self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            self._press_global = event.globalPosition().toPoint()  # 记录按下时的绝对坐标
            self._has_moved = False
            event.accept()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent):
        if event.buttons() & Qt.LeftButton and self._drag_pos is not None:
            new_pos = event.globalPosition().toPoint() - self._drag_pos
            # 移动超过 3px 才算拖拽
            if (new_pos - self.pos()).manhattanLength() > 3:
                self._has_moved = True
            self.move(new_pos)
            self.pet_moved.emit()
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent):
        if event.button() == Qt.LeftButton:
            # 没有移动多少 → 视为点击（用绝对坐标比较，不受窗口移动影响）
            if self._drag_pos is not None and not self._has_moved:
                release_global = event.globalPosition().toPoint()
                if hasattr(self, '_press_global'):
                    delta = release_global - self._press_global
                    if delta.manhattanLength() < 8:
                        self.pet_clicked.emit()
            self._drag_pos = None
            self._has_moved = False
        super().mouseReleaseEvent(event)

    # ============ 拖拽文件到形象 ============

    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            self.setStyleSheet("background: rgba(102,126,234,60); border-radius: 12px;")
        else:
            super().dragEnterEvent(event)

    def dragLeaveEvent(self, event):
        self.setStyleSheet("")
        super().dragLeaveEvent(event)

    def dropEvent(self, event: QDropEvent):
        self.setStyleSheet("")
        urls = event.mimeData().urls()
        for url in urls:
            file_path = url.toLocalFile()
            if file_path:
                _log.info(f"文件拖入形象: {file_path}")
                self.file_dropped.emit(file_path)
        event.acceptProposedAction()

    # ============ 缩放 ============

    def set_scale(self, scale: float):
        s = int(self.PET_SIZE * scale)
        self.resize(s, s)

    def sizeHint(self):
        pixmap = self._label.pixmap()
        if pixmap and not pixmap.isNull():
            return pixmap.size()
        return self._label.size()
