"""
TTS 语音合成引擎
- GENIE (GPT-SoVITS ONNX): 本地推理，feibi 角色语音克隆 ★默认
  ★ GENIE 运行在独立子进程中，避免 GIL 阻塞和模型加载卡顿
- Edge-TTS: 在线微软TTS，作为后备方案（主进程内运行）
"""
import os
import sys
import json
import re
import time
import logging
import threading
import tempfile
import multiprocessing
import queue as queue_module
from pathlib import Path
from typing import Optional

_log = logging.getLogger("TTS")

# ====== GENIE 资源路径配置（必须在 import genie_tts 之前） ======
_GENIE_ROOT = Path(__file__).resolve().parent.parent.parent  # 项目根目录
# ★★ 1.9.4：TTS 模型**集中到 `data/models/genie/`**（用户要求"模型散落各处，希望集中管理"）。
#   由 `src/core/models.py` 的注册表统一回答"东西在哪"：
#     · 优先 `data/models/genie`；找不到就**回落旧位置**
#       （`src/genie_tts/assets/genie` = 1.9.4 前的家，`data/genie` = 更早的降级位）
#       → 所以**没搬的机器照常能用**，迁移是渐进的，不会因为这次改动开不了 TTS。
#     · 真要搬文件，跑 `python tools/model_manager.py migrate`（本模块只解析路径，不动文件）。
from src.core.models import resolve as _resolve_model
from src.utils.proc import no_window_kwargs as _nw
_GENIE_RES = _resolve_model("genie") or (_GENIE_ROOT / "src" / "genie_tts"
                                         / "assets" / "genie")
_GENIE_DATA_DIR = _GENIE_RES / "GenieData"
_GENIE_CHARACTERS_DIR = _GENIE_RES / "CharacterModels"

# 设置环境变量，阻止 Resources.py 弹出交互式提示
os.environ.setdefault("GENIE_DATA_DIR", str(_GENIE_DATA_DIR))
os.environ.setdefault("HF_HUB_ENABLE_PROGRESS_BAR", "0")

# 角色模型配置
# 目前仅 feibi 角色有完整的 GENIE 本地模型（v2ProPlus 格式）
# 其他角色使用 Edge-TTS 在线语音作为后备
_CHARACTER_CONFIGS = {
    "feibi": {
        "model_dir": str(_GENIE_CHARACTERS_DIR / "v2ProPlus" / "feibi" / "tts_models"),
        "language": "Chinese",
    },
    "linglan": {
        "model_dir": str(_GENIE_CHARACTERS_DIR / "v2ProPlus" / "feibi" / "tts_models"),
        "language": "Chinese",
    },
}

# 标记哪些角色有真正的本地 GENIE 模型（而非共享模型）
_GENIE_NATIVE_CHARACTERS = {"feibi"}

# Edge-TTS 角色映射（后备方案）
VOICE_MAP = {
    "feibi": "zh-CN-XiaoxiaoNeural",
    "mika": "zh-CN-XiaoyiNeural",
    "thirtyseven": "zh-CN-YunxiNeural",
    "linglan": "zh-CN-XiaoxiaoNeural",
}

TEMP_DIR = Path(tempfile.gettempdir()) / "prts_tts_cache"
TEMP_DIR.mkdir(parents=True, exist_ok=True)


def _cleanup_tts_cache(max_files: int = 100):
    """清理 TTS 缓存目录，保留最多 max_files 个文件"""
    try:
        cache_files = sorted(TEMP_DIR.glob("*.mp3"), key=os.path.getmtime)
        while len(cache_files) > max_files:
            try:
                cache_files[0].unlink()
            except Exception:
                pass
            cache_files = cache_files[1:]
    except Exception:
        pass


def _sanitize_for_genie(text: str) -> str:
    """清理文本，移除 GENIE 中文 G2P 无法处理的字符（模块级，供 worker 进程复用）"""
    allowed = re.compile(
        r'[\u4e00-\u9fff\u3000-\u303f\uff00-\uffefa-zA-Z0-9\u0020-\u007e\s]'
    )
    cleaned = ''.join(ch for ch in text if allowed.match(ch))
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    return cleaned


# ============================================================
#  GENIE Worker 子进程
#  所有 ONNX 模型加载 + 推理在独立进程中执行，彻底消除
#  GIL 阻塞和加载卡顿对主进程的影响
# ============================================================

def _genie_worker_process(
    text_queue: multiprocessing.Queue,
    result_queue: multiprocessing.Queue,
    shutdown_event: multiprocessing.Event,
    character_name: str,
    onnx_model_dir: str,
    language: str,
    genie_data_dir: str,
    genie_characters_dir: str,
):
    """GENIE TTS 工作进程入口

    协议:
      text_queue  ←  str (文本) 或 None (退出)
      result_queue → dict:
        {"type": "ready"}                      — 模型加载完成
        {"type": "done"}                       — 合成+播放完成
        {"type": "error", "msg": str}          — 合成失败
        {"type": "fatal", "msg": str}          — 初始化失败，退出
    """
    # 确保 sys.path 包含项目根，使得 from src.genie_tts... 可导入
    _project_root = Path(__file__).resolve().parent.parent.parent
    if str(_project_root) not in sys.path:
        sys.path.insert(0, str(_project_root))

    # 设置环境变量（子进程不会继承 setdefault 结果，需要重新设置）
    os.environ["GENIE_DATA_DIR"] = genie_data_dir
    os.environ["HF_HUB_ENABLE_PROGRESS_BAR"] = "0"

    # ---- 导入 & 加载模型 ----
    try:
        from src.genie_tts.Internal import (
            load_character, set_reference_audio, tts, stop
        )
    except Exception as e:
        result_queue.put({"type": "fatal", "msg": f"GENIE import 失败: {e}"})
        return

    try:
        load_character(
            character_name=character_name,
            onnx_model_dir=onnx_model_dir,
            language=language,
        )
    except Exception as e:
        result_queue.put({"type": "fatal", "msg": f"模型加载失败: {e}"})
        return

    # 设置参考音频
    try:
        prompt_json = Path(genie_characters_dir) / "v2ProPlus" / character_name / "prompt_wav.json"
        if prompt_json.exists():
            with open(prompt_json, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            if "Normal" in cfg:
                wav_path = str(prompt_json.parent / "prompt_wav" / cfg["Normal"]["wav"])
                set_reference_audio(
                    character_name=character_name,
                    audio_path=wav_path,
                    audio_text=cfg["Normal"]["text"],
                    language=language,
                )
    except Exception as e:
        result_queue.put({"type": "fatal", "msg": f"参考音频设置失败: {e}"})
        return

    # 通知主进程：就绪
    result_queue.put({"type": "ready"})

    # ---- 合成循环 ----
    while not shutdown_event.is_set():
        try:
            text = text_queue.get(timeout=0.5)
        except Exception:
            continue

        if text is None:
            break  # 退出信号

        # 文本清理
        cleaned = _sanitize_for_genie(text)
        if not cleaned:
            result_queue.put({"type": "done"})
            continue

        try:
            tts(
                character_name=character_name,
                text=cleaned,
                play=True,
                split_sentence=True,
            )
            result_queue.put({"type": "done"})
        except Exception as e:
            result_queue.put({"type": "error", "msg": str(e)})

    # 清理
    try:
        stop()
    except Exception:
        pass


class TTSEngine:
    """TTS 引擎 — 默认使用 GENIE 本地推理"""

    def __init__(self):
        self._enabled = True
        self._provider = "genie"  # genie | edge
        self._character = "feibi"
        self._mode = "auto"
        self._running = False

        self._pending: list[str] = []
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._last_text_hash = ""

        # ---- GENIE 子进程控制 ----
        self._genie_worker: Optional[multiprocessing.Process] = None
        self._genie_text_queue: Optional[multiprocessing.Queue] = None
        self._genie_result_queue: Optional[multiprocessing.Queue] = None
        self._genie_shutdown: Optional[multiprocessing.Event] = None
        self._genie_ready = False       # Worker 是否已加载模型
        self._genie_loading = False     # Worker 是否正在加载中
        self._genie_load_failed = False
        self._genie_busy = False        # Worker 正在合成中
        # ★ 1.6.0: 空闲释放 —— GENIE worker 常驻约 4.5GB 内存，
        #   长时间不播报就回收，下次播报再按需加载
        self._genie_last_used: float = 0.0
        self._genie_idle_released = False

        # 回调
        self.on_speak_start: Optional[callable] = None
        self.on_speak_end: Optional[callable] = None

    def configure(self, enabled: bool = True, character: str = "feibi",
                  mode: str = "auto", provider: str = "genie"):
        old_character = self._character
        old_provider = self._provider
        old_enabled = self._enabled

        self._enabled = enabled
        self._character = character
        self._mode = mode
        self._provider = provider
        _log.info(f"TTS 配置: enabled={enabled}, provider={provider}, "
                  f"character={character}, mode={mode}")

        # ★ 禁用 TTS 时立即释放 GENIE worker（释放 ONNX 模型 4-5GB 内存）
        if not enabled and self._genie_worker and self._genie_worker.is_alive():
            _log.info("TTS 已禁用，释放 GENIE worker 内存")
            self._stop_genie_worker()
            return

        # 如果角色或 provider 变了，重启 worker
        if provider == "genie" and character in _GENIE_NATIVE_CHARACTERS:
            if old_character != character:
                _log.info("TTS 配置变更，重启 GENIE worker")
                self._stop_genie_worker()
        elif old_character == "feibi" and (provider != "genie" or character not in _GENIE_NATIVE_CHARACTERS):
            _log.info("切换到非 GENIE 角色，停止 worker")
            self._stop_genie_worker()

    # ---- GENIE 子进程管理 ----

    def _start_genie_worker(self):
        """启动 GENIE 后台工作进程（异步加载模型）"""
        if self._genie_worker and self._genie_worker.is_alive():
            return
        # 前置检查：非原生角色不走 GENIE
        if self._character not in _GENIE_NATIVE_CHARACTERS:
            _log.info(f"角色 '{self._character}' 非 GENIE 原生，使用 Edge-TTS")
            self._provider = "edge"
            return

        char_cfg = _CHARACTER_CONFIGS.get(self._character)
        if not char_cfg or not os.path.isdir(char_cfg["model_dir"]):
            _log.warning(f"GENIE 模型目录不存在: {char_cfg.get('model_dir', '?')}")
            self._provider = "edge"
            self._genie_load_failed = True
            return

        # 创建通信对象
        self._genie_text_queue = multiprocessing.Queue(maxsize=10)
        self._genie_result_queue = multiprocessing.Queue()
        self._genie_shutdown = multiprocessing.Event()
        self._genie_ready = False
        self._genie_loading = True
        self._genie_load_failed = False
        self._genie_busy = False

        self._genie_worker = multiprocessing.Process(
            target=_genie_worker_process,
            args=(
                self._genie_text_queue,
                self._genie_result_queue,
                self._genie_shutdown,
                self._character,
                char_cfg["model_dir"],
                char_cfg["language"],
                str(_GENIE_DATA_DIR),
                str(_GENIE_CHARACTERS_DIR),
            ),
            daemon=True,
            name="genie-tts-worker",
        )
        self._genie_worker.start()
        _log.info(f"GENIE worker 已启动 (pid={self._genie_worker.pid})，等待模型加载...")

    def _stop_genie_worker(self):
        """停止 GENIE 工作进程"""
        if not self._genie_worker:
            return
        _log.info("正在停止 GENIE worker...")
        if self._genie_shutdown:
            self._genie_shutdown.set()
        if self._genie_text_queue:
            try:
                self._genie_text_queue.put(None, timeout=1)
            except Exception:
                pass
        if self._genie_worker.is_alive():
            self._genie_worker.join(timeout=5)
            if self._genie_worker.is_alive():
                self._genie_worker.terminate()
                self._genie_worker.join(timeout=2)
        self._genie_worker = None
        self._genie_text_queue = None
        self._genie_result_queue = None
        self._genie_shutdown = None
        self._genie_ready = False
        self._genie_loading = False
        self._genie_busy = False
        _log.info("GENIE worker 已停止")

    def _poll_worker_results(self):
        """消费 worker 发回的结果消息（非阻塞）"""
        if not self._genie_result_queue:
            return

        while True:
            try:
                msg = self._genie_result_queue.get_nowait()
            except queue_module.Empty:
                break

            msg_type = msg.get("type")
            if msg_type == "ready":
                self._genie_ready = True
                self._genie_loading = False
                _log.info("GENIE worker 模型加载完成 ✓")
            elif msg_type == "done":
                self._genie_busy = False
                if self.on_speak_end:
                    self.on_speak_end()
            elif msg_type == "error":
                self._genie_busy = False
                _log.error(f"GENIE worker 合成错误: {msg.get('msg', '')}")
                if self.on_speak_end:
                    self.on_speak_end()
            elif msg_type == "fatal":
                self._genie_loading = False
                self._genie_load_failed = True
                self._genie_ready = False
                _log.error(f"GENIE worker 致命错误: {msg.get('msg', '')}")
                # 回退到 edge
                self._provider = "edge"

    def preload_genie(self) -> bool:
        """预加载 GENIE worker（异步启动子进程加载模型）
        Returns:
            True: GENIE worker 已启动/正在启动中
            False: 不使用 GENIE（已回退到 edge）
        """
        if self._provider != "genie":
            return False
        if self._genie_load_failed:
            return False
        if self._character not in _GENIE_NATIVE_CHARACTERS:
            return False
        self._start_genie_worker()
        # 确保消费者线程在运行以接收 ready 信号
        self._ensure_thread()
        return True

    def speak(self, text: str, force: bool = False):
        """将文本加入播放队列"""
        if not self._enabled:
            return
        if not text or not text.strip():
            return

        # 去重
        text_hash = str(hash(text))
        if text_hash == self._last_text_hash and not force:
            return
        self._last_text_hash = text_hash

        with self._lock:
            self._pending.append(text.strip())

        self._ensure_thread()

    def stop(self):
        """清空待播放队列"""
        with self._lock:
            self._pending.clear()
        # 清空 worker 输入队列
        if self._genie_text_queue:
            while True:
                try:
                    self._genie_text_queue.get_nowait()
                except queue_module.Empty:
                    break

    def shutdown(self):
        """关闭引擎"""
        self._running = False
        self._enabled = False
        self.stop()
        self._stop_genie_worker()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3)
        _log.info("TTS 引擎已关闭")

    # ---- 后台线程 ----

    def _ensure_thread(self):
        if self._thread and self._thread.is_alive():
            return
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="tts-loop")
        self._thread.start()

    def _run_loop(self):
        """后台消费者线程 — 投送文本到 worker / edge-tts，非阻塞"""
        while self._running:
            # 始终轮询 worker 结果（推进回调）
            self._poll_worker_results()
            # ★ 1.6.0: 空闲释放 GENIE worker（默认 15 分钟，可配置）
            self._maybe_release_idle_genie()

            text = None
            with self._lock:
                if self._pending:
                    text = self._pending.pop(0)

            if text:
                try:
                    if self._provider == "genie":
                        self._do_genie_tts(text)
                    else:
                        self._do_edge_tts(text)
                except Exception as e:
                    _log.error(f"TTS 播放异常: {e}")
                time.sleep(0.1)
            else:
                time.sleep(0.2)

    def _maybe_release_idle_genie(self):
        """★ 1.6.0: GENIE worker 空闲太久就释放（它常驻约 4.5GB 内存）

        · 只有在「已加载完成、当前不忙、队列为空、没有待播文本」时才释放；
        · 释放后下次播报会自动重新加载（用户侧表现为第一句稍慢）；
        · 超时时间读 config.tts.genie_idle_release_minutes（0=从不释放）。
        """
        try:
            if self._provider != "genie" or not self._genie_ready:
                return
            if self._genie_busy or self._genie_loading:
                return
            if not (self._genie_worker and self._genie_worker.is_alive()):
                return
            with self._lock:
                if self._pending:
                    return
            try:
                from src.utils.config import get_config
                minutes = int(getattr(get_config().tts, "genie_idle_release_minutes", 15) or 0)
            except Exception:
                minutes = 15
            if minutes <= 0:
                return                      # 用户选择常驻
            if self._genie_last_used <= 0:
                self._genie_last_used = time.time()
                return
            if time.time() - self._genie_last_used < minutes * 60:
                return
            _log.info(f"GENIE worker 空闲超过 {minutes} 分钟，释放内存（下次播报会重新加载）")
            self._stop_genie_worker()
            self._genie_ready = False
            self._genie_loading = False
            self._genie_idle_released = True
        except Exception as e:
            _log.debug(f"空闲释放检查失败: {e}")

    def _do_genie_tts(self, text: str):
        """GENIE: 投送文本到 worker 子进程（非阻塞）"""
        # ★ 1.6.0: 记录最后使用时间，供"空闲释放"判断
        self._genie_last_used = time.time()
        # 还在加载中，等待
        if self._genie_loading:
            if self._genie_load_failed:
                _log.warning("GENIE worker 加载失败，回退 edge-tts")
                self._do_edge_tts(text)
                return
            # 重新入队等待加载完成
            with self._lock:
                self._pending.insert(0, text)
            time.sleep(0.5)
            return

        # 尚未启动
        if not self._genie_worker or not self._genie_worker.is_alive():
            if self._genie_load_failed:
                _log.warning("GENIE 不可用，回退 edge-tts")
                self._do_edge_tts(text)
                return
            self._start_genie_worker()
            # 启动后重新入队等待 ready
            if self._genie_worker:
                with self._lock:
                    self._pending.insert(0, text)
                return
            # ★ 1.8.10: _start_genie_worker() 在「该角色没有原生 GENIE 模型」时
            #   会把 provider 切成 edge 并直接返回（不建 worker）。
            #   原来这里不 return，但下面紧接着就是"Worker 就绪但正忙"分支，
            #   于是这句话既没进 worker 也没重排，被静默丢掉 ——
            #   表现为"切到 edge 后第一句话永远不出声"。
            _log.info("GENIE 未就绪，本句改走 edge-tts")
            self._do_edge_tts(text)
            return

        # Worker 就绪但正忙
        if self._genie_busy:
            with self._lock:
                self._pending.insert(0, text)
            time.sleep(0.1)
            return

        # 文本清理
        clean_text = _sanitize_for_genie(text)
        if not clean_text:
            return

        # 投送到 worker
        try:
            if self.on_speak_start:
                self.on_speak_start()
            self._genie_text_queue.put(clean_text, timeout=1)
            self._genie_busy = True
        except Exception as e:
            _log.error(f"投送 GENIE worker 失败: {e}")
            self._genie_busy = False
            if self.on_speak_end:
                self.on_speak_end()
            self._do_edge_tts(text)

    def _do_edge_tts(self, text: str):
        """Edge-TTS 后备方案"""
        import asyncio
        import hashlib

        voice = VOICE_MAP.get(self._character, "zh-CN-XiaoxiaoNeural")

        text_hash = hashlib.md5(text.encode()).hexdigest()
        cache_file = TEMP_DIR / f"{text_hash}.mp3"

        if not cache_file.exists():
            try:
                import edge_tts
                communicate = edge_tts.Communicate(text, voice, rate="+0%")
                asyncio.run(communicate.save(str(cache_file)))
                _log.debug(f"Edge-TTS 合成: {text[:30]}...")
                # 每次合成后检查缓存上限
                _cleanup_tts_cache(max_files=100)
            except ImportError:
                _log.error("edge-tts 未安装")
                return
            except Exception as e:
                _log.error(f"Edge-TTS 合成失败: {e}")
                return

        if not cache_file.exists():
            return

        try:
            if self.on_speak_start:
                self.on_speak_start()
            self._play_audio_file(str(cache_file))
            if self.on_speak_end:
                self.on_speak_end()
        except Exception as e:
            _log.error(f"播放失败: {e}")

    @staticmethod
    def _play_audio_file(filepath: str):
        """播放音频文件（pygame →系统播放器 回退）"""
        try:
            import pygame
            if not pygame.mixer.get_init():
                pygame.mixer.init()
            pygame.mixer.music.load(filepath)
            pygame.mixer.music.play()
            while pygame.mixer.music.get_busy():
                pygame.time.wait(100)
        except ImportError:
            TTSEngine._play_with_system(filepath)
        except Exception as e:
            _log.warning(f"pygame 播放失败: {e}")
            TTSEngine._play_with_system(filepath)

    @staticmethod
    def _play_with_system(filepath: str):
        import platform
        import subprocess
        system = platform.system()
        try:
            if system == "Windows":
                subprocess.run(
                    ["powershell", "-c",
                     f'(New-Object Media.SoundPlayer "{filepath}").PlaySync()'],
                    capture_output=True, timeout=30, **_nw())
            elif system == "Darwin":
                subprocess.run(["afplay", filepath], capture_output=True, timeout=30, **_nw())
            else:
                subprocess.run(["aplay", filepath], capture_output=True, timeout=30, **_nw())
        except Exception as e:
            _log.error(f"系统播放失败: {e}")


_tts_instance: Optional[TTSEngine] = None


def get_tts() -> TTSEngine:
    global _tts_instance
    if _tts_instance is None:
        _tts_instance = TTSEngine()
    return _tts_instance
