"""
统一监控模块（重构 v2.0）
- 屏幕截图、键盘（升级为输入监控）、音频、系统状态
- 智能过滤无意义数据
- 已删除：MouseMonitor（不再监控鼠标）
- 已删除：CPU/磁盘监控
- 新增：1000 条上限 + 写入计数器
"""
import os
import time
import threading
import logging
from collections import deque
from datetime import datetime
from typing import Optional, Callable

_log = logging.getLogger("Monitor")


class StoppableSleep:
    """★★ 1.9.3 第 36 轮：**可被 stop() 立刻打断的睡眠**。

    真机证据（`logs/prts_20261004_065116.log`）：
      · 退出：`06:51:51 正在退出 → 06:51:55`
        并稳定打 `[退出] 收尾线程超过 4s 预算仍未结束（t1=活 t2=停）` —— **每次都超**；
      · 改监控设置：`[监控] 重启完成，耗时 2287 ms`（另一次 2013 ms）。

    根因：监控循环里用 `time.sleep(...)`（`SystemMonitor` 一睡就是 **60 秒**！），
    而 `stop()` 是 `thread.join(timeout=2)` —— **只能干等那次睡眠走完**。
    于是"退出慢"和"改设置要等 2 秒"是**同一个原因**。

    改成 `self._stop_event.wait(n)`：`stop()` 里 `set()` 一下，线程**立刻醒来**，
    stop 从"最多 2 秒"变成毫秒级。收益：
      ① 退出真正快，不必再靠 4 秒硬超时兜底；
      ② 不会出现"关库时监控线程还活着"（旧注释警告过的那个场景）；
      ③ 改监控设置几乎瞬时。
    ★ 保留 `join(timeout=...)` 作为**兜底**：万一线程真的卡在别处（比如 pynput
      监听器里），超时后照样返回，不会把界面拖死。
    """

    def _init_stop_event(self):
        self._stop_event = threading.Event()

    def _nap(self, seconds: float) -> bool:
        """可打断的睡眠。返回 True 表示 `stop()` 已经叫停，调用方应当 `break`。"""
        return self._stop_event.wait(seconds)


class BaseMonitor(StoppableSleep):
    """监控基类"""

    def __init__(self, name: str):
        self.name = name
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._callback: Optional[Callable[[str, dict], None]] = None
        self._error_count = 0
        self._max_errors = 10
        self._init_stop_event()          # ★ 第 36 轮：可打断睡眠

    def set_callback(self, cb: Callable[[str, dict], None]):
        self._callback = cb

    def start(self):
        if self._running:
            return
        self._stop_event.clear()         # ★ 第 36 轮：重启前复位，否则 wait 会立刻返回
        self._running = True
        self._thread = threading.Thread(target=self._safe_run, daemon=True, name=f"mon-{self.name}")
        self._thread.start()

    def stop(self):
        self._running = False
        # ★★ 第 36 轮：**先叫醒它**再去 join —— 否则就是干等 time.sleep 走完
        #   （SystemMonitor 一睡 60 秒，join 只能等满 timeout=2）。
        try:
            self._stop_event.set()
        except Exception:
            pass
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)

    def _safe_run(self):
        """带错误保护的运行"""
        try:
            self._run()
        except Exception as e:
            _log.error(f"[{self.name}] 致命错误: {e}", exc_info=True)

    def _run(self):
        raise NotImplementedError

    def _emit(self, data: dict):
        if self._callback:
            try:
                self._callback(self.name, data)
            except Exception as e:
                _log.error(f"[{self.name}] 回调错误: {e}")


class ScreenMonitor(BaseMonitor):
    """屏幕监控 - 定时截图, 仅变化时发送, 最多保留 N 张"""

    def __init__(self, interval: int = 60, max_photos: int = 10):
        super().__init__("screen")
        self.interval = interval
        self.max_photos = max_photos
        self._last_hash = None

    def _run(self):
        try:
            import mss
            from PIL import Image
            import hashlib
            import io
        except ImportError as e:
            _log.error(f"[screen] 缺少依赖(mss/Pillow): {e}")
            return

        from pathlib import Path
        BASE_DIR = Path(__file__).resolve().parent.parent.parent
        photo_dir = BASE_DIR / "data" / "DBs" / "screenphotos"
        photo_dir.mkdir(parents=True, exist_ok=True)

        _log.info(f"[screen] 截图间隔: {self.interval}s, 最多保留: {self.max_photos} 张")
        with mss.mss() as sct:
            while self._running:
                try:
                    monitor = sct.monitors[1]
                    screenshot = sct.grab(monitor)
                    img = Image.frombytes("RGB", screenshot.size, screenshot.bgra, "raw", "BGRX")
                    img.thumbnail((1920, 1080), Image.LANCZOS)

                    buffer = io.BytesIO()
                    img.save(buffer, format="JPEG", quality=30)
                    img_hash = hashlib.md5(buffer.getvalue()).hexdigest()

                    if img_hash != self._last_hash:
                        self._last_hash = img_hash
                        # 保存截图到文件
                        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                        photo_path = photo_dir / f"screen_{ts}.jpg"
                        img.save(str(photo_path), "JPEG", quality=30)

                        # 限制截图数量
                        self._enforce_photo_limit(photo_dir)

                        self._emit({
                            "image": img, "timestamp": datetime.now().isoformat(),
                            "hash": img_hash, "file_path": str(photo_path)})

                    # ★ 第 36 轮：可打断睡眠（原来是 time.sleep(self.interval)，
                    #   会让 stop() 干等到睡眠结束）
                    if self._nap(self.interval):
                        break
                except Exception as e:
                    _log.error(f"[screen] 截图错误: {e}")
                    if self._nap(5):
                        break

    def _enforce_photo_limit(self, photo_dir):
        """限制截图文件夹中的文件数量"""
        try:
            files = sorted(photo_dir.glob("screen_*.jpg"), key=os.path.getmtime)
            while len(files) > self.max_photos:
                try:
                    files[0].unlink()
                    _log.debug(f"[screen] 删除旧截图: {files[0].name}")
                except Exception:
                    pass
                files = files[1:]
        except Exception:
            pass


class KeyboardMonitor(BaseMonitor):
    """键盘监控 — 升级版（Enter 或 30 秒无输入触发）"""

    def __init__(self):
        super().__init__("keyboard")
        self._buffer = []
        self._last_emit = time.time()
        self._key_count = 0
        self._idle_timeout = 30.0  # 30 秒无输入触发

    def _run(self):
        try:
            from pynput import keyboard
        except ImportError as e:
            _log.error(f"[keyboard] pynput未安装: {e}")
            return

        _log.info("[keyboard] 键盘监听启动 (Enter / 30s 触发)")

        def on_press(key):
            if not self._running:
                return False
            try:
                # Enter 键 → 立即触发
                if key == keyboard.Key.enter:
                    self._flush()
                    return
                # 快捷键不触发监控（Ctrl/Alt/Shift/Win 等修饰键不作为内容积累）
                char = key.char
                if char:
                    self._buffer.append(char)
                    self._key_count += 1
            except AttributeError:
                # 特殊键
                name = str(key).replace("Key.", "")
                self._buffer.append(f"[{name}]")
                self._key_count += 1
                # 如果按的是 Ctrl/Alt/Shift 等纯修饰键 → 不计入文字，撤回
                # 但保留在 buffer 中以便显示上下文
                if name.lower() in ("ctrl", "ctrl_l", "ctrl_r",
                                     "alt", "alt_l", "alt_r", "alt_gr",
                                     "shift", "shift_l", "shift_r",
                                     "cmd", "cmd_l", "cmd_r",
                                     "win", "win_l", "win_r"):
                    pass  # 保留但仅作上下文，不影响触发逻辑

            # 检查是否 30 秒无新输入
            now = time.time()
            if now - self._last_emit > self._idle_timeout and self._buffer:
                self._flush()

        try:
            with keyboard.Listener(on_press=on_press) as listener:
                while self._running:
                    # 每 2 秒检查一次是否触发 30s 超时
                    # ★ 第 36 轮：可打断（原来是 time.sleep(2)）
                    if self._nap(2):
                        break
                    if self._buffer and time.time() - self._last_emit > self._idle_timeout:
                        self._flush()
                # listener 退出
        except Exception as e:
            _log.error(f"[keyboard] 监听器错误: {e}")

    def _flush(self):
        """清空缓冲区并发送"""
        if not self._buffer:
            return
        text = "".join(self._buffer)
        self._buffer.clear()
        if self._is_meaningful(text):
            self._emit({
                "text": text,
                "key_count": self._key_count,
                "timestamp": datetime.now().isoformat()})
        self._last_emit = time.time()

    def _is_meaningful(self, text: str) -> bool:
        import re
        clean = re.sub(r'\[.*?\]', '', text)
        if len(clean.strip()) < 3:
            return False
        if len(set(clean)) <= 2 and len(clean) > 5:
            return False
        return True


class AudioMonitor(BaseMonitor):
    """音频监控 —— **音量过阈值就用本地 STT 转写，结果作为监控事件入库**。

    ★★ 1.9.3 第 49 轮（用户设计，2026-10-04）：
      *"STT 属于监控的一部分，开启后，麦克风声音超过一定阈值才会启动 STT"*，
      并明确选了：**转写结果写进监控事件（raw_events）→ 进 Pipeline 汇总，
      不直接当用户消息发给桌宠**。

    于是它和"屏幕截图 → vision"完全对称：
        屏幕：定时截图 → `_summarize_callback(image)`
        麦克风：**一直在采**，音量过阈值才开始、静音 1.5s 判为说完 → 本地 STT → `text`
      转写文本走 `_emit({"type": "speech", "text": ...})` →
      `UnifiedMonitor._on_monitor_data` → `raw_events`。

    · 复用现成的 `MicrophoneCapture`（阻塞式 `read_chunk`）+ `EnergyVAD`（能量端点检测），
      不再自己开一套 pyaudio 流 —— 这两个类此前无人调用，现在**活了**。
    · **STT 没就绪时自动退回旧行为**（只报音量），监控不会变成哑巴。
    · 单段最长 30s 封顶，防止一直有噪声时无限攒内存。
    """

    MAX_UTTERANCE_SEC = 30.0

    def __init__(self, threshold: int = 500):
        super().__init__("audio")
        self._threshold = threshold
        self._stt = None            # 就绪的 STT 引擎；None = 退回"只报音量"

    def _get_stt(self):
        """拿到**真的能用**的 STT 引擎；拿不到返回 None（于是退回只报音量）。

        ★ 踩坑：`STTEngine` **没有 `available` 属性** —— 我第一版用
          `getattr(stt, "available", True)` 判定，等于永远"可用"，
          会把没装模型的机器引到必然失败的转写上。
          正确的就绪信号是 **`initialize()`**（幂等，返回 bool；
          `transcribe()` 内部也是先调它）。
        """
        try:
            from src.core.stt_engine import get_stt
            stt = get_stt()
            if stt is not None and stt.initialize():
                return stt
            _log.info("[audio] STT 未就绪（缺 exe/模型）→ 音频监控退回只报音量")
        except Exception as e:
            _log.debug(f"[audio] 取 STT 失败，退回只报音量: {e}")
        return None

    def _emit_speech(self, samples, volume: float):
        """把一段语音交给 STT，成功就发一条 `speech` 监控事件。"""
        try:
            import numpy as np
            text = self._stt.transcribe(np.concatenate(samples))
        except Exception as e:
            _log.warning(f"[audio] 转写失败: {e}")
            return
        text = (text or "").strip()
        if not text:
            _log.debug("[audio] 这段没转出文字（静音/噪声），不入库")
            return
        _log.info(f"[audio] 语音转写 {len(text)} 字 → 监控事件")
        self._emit({"type": "speech", "text": text,
                    "volume": float(volume),
                    "seconds": round(len(samples) * 1024 / 16000.0, 1),
                    "timestamp": datetime.now().isoformat()})

    def _run(self):
        try:
            import numpy as np
        except ImportError as e:
            _log.error(f"[audio] numpy未安装: {e}")
            return

        from src.core.stt_engine import EnergyVAD, MicrophoneCapture

        self._stt = self._get_stt()
        _log.info(f"[audio] 音频监听启动（阈值={self._threshold}，"
                  f"STT={'已就绪 → 转写入库' if self._stt else '不可用 → 只报音量'}）")

        cap = MicrophoneCapture(sample_rate=16000)
        if not cap.start():
            _log.error("[audio] 麦克风打不开，音频监控未启动")
            return
        vad = EnergyVAD(threshold=0.02, silence_duration=1.5)
        utt, pre, peak = [], [], 0.0
        try:
            while self._running:
                chunk = cap.read_chunk(1024 / 16000.0)      # ≈64ms
                if chunk is None:
                    if self._nap(0.5):
                        break
                    continue
                vol = float(np.abs(chunk).mean())
                if self._stt is None:
                    # ── 退回旧行为：只报音量（沿用原阈值语义）──
                    if vol * 32768 > self._threshold:
                        self._emit({"volume": vol * 32768,
                                    "timestamp": datetime.now().isoformat()})
                    continue
                was = vad.is_speaking
                trig = vad.process(chunk)
                if trig and not was:                        # 语音开始
                    utt = list(pre)                         # 带点"预滚"，别切掉首字
                    peak = vol
                if vad.is_speaking:
                    utt.append(chunk)
                    peak = max(peak, vol)
                    if len(utt) * 1024 / 16000.0 > self.MAX_UTTERANCE_SEC:
                        _log.debug("[audio] 单段超 30s，强制切段转写")
                        self._emit_speech(utt, peak)
                        utt, peak = [], 0.0
                        vad = EnergyVAD(threshold=0.02, silence_duration=1.5)
                if trig and was:                            # 语音结束
                    if utt:
                        self._emit_speech(utt, peak)
                    utt, peak = [], 0.0
                pre.append(chunk)                           # 预滚只留最近 3 块（≈200ms）
                if len(pre) > 3:
                    pre.pop(0)
        finally:
            cap.stop()


class SystemMonitor(BaseMonitor):
    """系统状态监控（精简版 — 仅进程内存 + 窗口变化，删除 CPU/磁盘）"""

    def __init__(self, interval: int = 60):
        super().__init__("system")
        self._interval = interval
        self._last_window = ""
        self._last_window_time = time.time()

    def _run(self):
        try:
            import psutil
        except ImportError as e:
            _log.error(f"[system] psutil未安装: {e}")
            return

        _log.info("[system] 系统监控启动 (进程内存 + 窗口变化)")

        # 尝试 win32gui（更可靠），回退到 pygetwindow
        try:
            import win32gui
            import win32process
            HAS_WIN32 = True
        except ImportError:
            HAS_WIN32 = False
            import pygetwindow as gw
            _log.info("[system] 使用 pygetwindow（无 win32gui）")

        while self._running:
            try:
                # 活跃窗口（变化时记录）
                active_window = ""
                process_name = ""
                if HAS_WIN32:
                    try:
                        hwnd = win32gui.GetForegroundWindow()
                        active_window = win32gui.GetWindowText(hwnd)
                        _, pid = win32process.GetWindowThreadProcessId(hwnd)
                        try:
                            process_name = psutil.Process(pid).name()
                        except Exception:
                            process_name = ""
                    except Exception:
                        pass
                else:
                    try:
                        win = gw.getActiveWindow()
                        if win:
                            active_window = win.title
                    except Exception:
                        pass

                now = time.time()
                if active_window != self._last_window and active_window:
                    # 记录上一个窗口的持续时长
                    duration = now - self._last_window_time
                    if self._last_window and duration > 5:
                        self._emit({
                            "type": "window_change",
                            "previous_window": self._last_window,
                            "duration_seconds": round(duration, 1),
                            "active_window": active_window,
                            "process_name": process_name,
                            "timestamp": datetime.now().isoformat()})

                    self._last_window = active_window
                    self._last_window_time = now

                # 进程内存（>100MB 的重大进程）
                try:
                    top_processes = []
                    for proc in psutil.process_iter(['name', 'memory_info']):
                        try:
                            mi = proc.info['memory_info']
                            if mi and mi.rss > 100 * 1024 * 1024:  # >100MB
                                top_processes.append({
                                    "name": proc.info["name"] or "",
                                    "memory_mb": round(mi.rss / (1024 * 1024), 1),
                                })
                        except (psutil.NoSuchProcess, psutil.AccessDenied):
                            continue
                    # 按内存排序，取 top 10
                    top_processes.sort(key=lambda x: x["memory_mb"], reverse=True)
                    top_processes = top_processes[:10]

                    # 合并同名进程
                    merged = {}
                    for p in top_processes:
                        name = p["name"]
                        if name in merged:
                            merged[name]["count"] += 1
                            merged[name]["memory_mb"] += p["memory_mb"]
                        else:
                            merged[name] = {**p, "count": 1}
                    top_merged = sorted(merged.values(), key=lambda x: x["memory_mb"], reverse=True)[:10]

                    if top_merged:
                        self._emit({
                            "type": "top_processes",
                            "processes": top_merged,
                            "timestamp": datetime.now().isoformat()})
                except Exception:
                    pass

                # ★★ 第 36 轮：**这一行是"退出慢"的头号元凶** ——
                #   系统监控间隔默认 60 秒，原来 time.sleep(60) 会让 stop() 干等满 2 秒。
                #   换成可打断睡眠后，stop() 毫秒级返回。
                if self._nap(self._interval):
                    break
            except Exception as e:
                _log.error(f"[system] 错误: {e}")
                if self._nap(30):
                    break


class UnifiedMonitor:
    """统一监控管理器（重构 v2.0）"""

    def __init__(self):
        from src.utils.config import get_config
        self.config = get_config().monitor
        self.monitors: dict[str, BaseMonitor] = {}
        self._data_callback: Optional[Callable] = None
        self._raw_data_queue = deque(maxlen=200)
        self._summarize_callback: Optional[Callable] = None
        self._data_count = 0  # 写入计数器

    def set_callbacks(self, data_callback: Callable = None, summarize_callback: Callable = None):
        self._data_callback = data_callback
        self._summarize_callback = summarize_callback

    def start(self):
        self.stop()
        cfg = self.config

        if cfg.screen_enabled:
            self.monitors["screen"] = ScreenMonitor(cfg.screen_interval, cfg.screen_max_photos)
        if cfg.keyboard_enabled:
            self.monitors["keyboard"] = KeyboardMonitor()
        # MouseMonitor 已删除 — mouse_enabled 仅保留配置兼容性，不再实例化
        # ★★ 1.9.3 第 49 轮（用户设计）：**STT 属于监控的一部分**。
        #   开了 STT（`cfg.stt.enabled`）就该把麦克风监控跑起来 ——
        #   否则"启用语音转文字"这个开关等于没接线（真机现状就是这样：
        #   audio_enabled=False、stt.enabled=True，于是语音监控根本不存在）。
        #   `audio_enabled` 仍然单独有效（只报音量的老行为）。
        #   ★ 注意开关分两处：麦克风在 `config.monitor.audio_enabled`，
        #     STT 在 `config.stt.enabled`（`self.config` 只是 monitor 那一段）。
        try:
            from src.utils.config import get_config as _gc
            _stt_on = bool(_gc().stt.enabled)
        except Exception:
            _stt_on = False
        if cfg.audio_enabled or _stt_on:
            self.monitors["audio"] = AudioMonitor(threshold=cfg.audio_threshold)
            if not cfg.audio_enabled:
                _log.info("[audio] 因「启用语音转文字（STT）」而启动麦克风监控"
                          " → 音量过阈值自动转写进 raw_events")
        if cfg.system_enabled:
            self.monitors["system"] = SystemMonitor(interval=cfg.sys_interval)

        if not self.monitors:
            _log.info("所有监控已禁用")
            return

        for m in self.monitors.values():
            m.set_callback(self._on_monitor_data)
            m.start()

        _log.info(f"监控启动: {list(self.monitors.keys())} ({len(self.monitors)}个)")

    def stop(self):
        for m in self.monitors.values():
            m.stop()
        self.monitors.clear()
        _log.debug("监控已停止")

    def _on_monitor_data(self, source: str, data: dict):
        # 写入原始事件到 SQLite
        try:
            from src.core.database import get_db
            db = get_db()

            type_ = data.get("type", "")
            content = ""
            file_path = data.get("file_path", None)

            if source == "keyboard":
                content = data.get("text", "")[:2000]
                type_ = "text"
            elif source == "screen":
                content = f"[截图] {data.get('hash', '')}"
                type_ = "screenshot"
            elif source == "audio":
                # ★ 1.9.3 第 49 轮：音频监控现在有两种事件 ——
                #   `speech`（STT 转写出来的文字，用户要的那种）和
                #   `volume`（STT 不可用时的老行为兜底）。
                if type_ == "speech":
                    content = f"[语音] {data.get('text', '')[:2000]}"
                else:
                    content = f"[音量] {data.get('volume', 0):.0f}"
                    type_ = "volume"
            elif source == "system":
                if type_ == "window_change":
                    prev = data.get("previous_window", "")
                    dur = data.get("duration_seconds", 0)
                    content = f"[窗口切换] {prev} → {data.get('active_window', '')} (持续 {dur:.0f}s)"
                elif type_ == "top_processes":
                    procs = data.get("processes", [])
                    parts = [f"{p['name']}({p['count']}进程, {p['memory_mb']:.0f}MB)" for p in procs[:5]]
                    content = f"[进程] {', '.join(parts)}"
                else:
                    content = str(data)
            else:
                content = str(data)[:2000]

            db.raw.add_raw_event(
                source=source, type_=type_, content=content,
                file_path=file_path, metadata=data)

            # 上限检查
            self._data_count += 1
            if self._data_count % 50 == 0:
                db.raw.enforce_cap(self.config.raw_data_cap)

        except Exception as e:
            _log.error(f"[UnifiedMonitor] 写入 raw_events 失败: {e}")

        # 入内存队列
        self._raw_data_queue.append({
            "source": source, "data": data, "timestamp": datetime.now().isoformat()})

        if self._data_count % 50 == 0:
            _log.debug(f"监控数据累计: {self._data_count} 条")

        if self._data_callback:
            self._data_callback(source, data)

        # 截图 → LLM 视觉（通过回调，保持兼容）
        if source == "screen" and "image" in data and self._summarize_callback:
            self._summarize_callback(data["image"])

    def restart(self):
        from src.utils.config import get_config
        self.config = get_config().monitor
        self.stop()
        self.start()


_monitor_instance: Optional[UnifiedMonitor] = None


def get_monitor() -> UnifiedMonitor:
    global _monitor_instance
    if _monitor_instance is None:
        _monitor_instance = UnifiedMonitor()
    return _monitor_instance
