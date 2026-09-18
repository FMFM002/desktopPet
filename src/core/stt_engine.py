"""
STT (语音转文字) 引擎 — 基于 Whisper 的实时/离线语音识别

支持模式:
1. whisper_cpp — whisper.cpp HTTP 服务（本地高性能）
2. whisper_local — whisper.cpp CLI 本地 subprocess（编译好的 .exe）
3. openai_whisper — OpenAI Whisper API（云端）
4. faster_whisper — faster-whisper 本地推理

核心功能:
- 麦克风音频流采集 → VAD 分段 → 转写 → 文本输出
- 支持实时流式转写和离线批量转写
- 支持中文/英文/多语言
"""
import os
import io
import time
import threading
import logging
import tempfile
import wave
from pathlib import Path
from typing import Optional, Callable
from collections import deque

import numpy as np

_log = logging.getLogger("STT")

# 项目根（用来找项目内的模型；见 _resolve_model_dir）
_BASE_DIR = Path(__file__).resolve().parent.parent.parent


def _resolve_model_dir(name: str) -> str:
    """★ 1.9.0: 把模型名解析成"项目内的模型目录"（找不到就原样返回，交给库去下）。

    你的要求：**尽量别把东西放到项目外面**，方便以后打包 —— 所以优先认这几个位置：
        · 直接给的绝对/相对目录（存在就用）
        · 项目根/<name>
        · 项目根/data/models/<name>
    例如 `stt.model_name = "small"`，只要 `data/models/small` 存在，就用它，
    不会再跑去 HuggingFace 下（离线也能用）。
    """
    if not name:
        return name
    try:
        p = Path(name)
        if p.is_dir():
            return str(p)
        for cand in (_BASE_DIR / name, _BASE_DIR / "data" / "models" / name):
            if cand.is_dir():
                return str(cand)
    except Exception:
        pass
    return name


# ============================================================================
# 音频采集器（轻量级，复用 pyaudio）
# ============================================================================
class MicrophoneCapture:
    """麦克风音频采集器"""

    def __init__(self, sample_rate: int = 16000, channels: int = 1, chunk_size: int = 1024):
        self.sample_rate = sample_rate
        self.channels = channels
        self.chunk_size = chunk_size
        self._running = False
        self._stream = None
        self._p = None
        self._buffer: deque[np.ndarray] = deque(maxlen=200)
        self._on_audio: Optional[Callable[[np.ndarray], None]] = None

    def set_callback(self, cb: Callable[[np.ndarray], None]):
        self._on_audio = cb

    def start(self) -> bool:
        try:
            import pyaudio
            self._p = pyaudio.PyAudio()
            self._stream = self._p.open(
                format=pyaudio.paInt16, channels=self.channels,
                rate=self.sample_rate, input=True,
                frames_per_buffer=self.chunk_size,
                stream_callback=self._audio_callback if self._on_audio else None,
            )
            self._stream.start_stream()
            self._running = True
            _log.info(f"麦克风采集启动: {self.sample_rate}Hz/{self.channels}ch")
            return True
        except Exception as e:
            _log.error(f"麦克风采集失败: {e}")
            return False

    def _audio_callback(self, in_data, frame_count, time_info, status):
        if not self._running:
            return (None, 1)
        try:
            audio = np.frombuffer(in_data, dtype=np.int16).astype(np.float32) / 32768.0
            self._buffer.append(audio)
            if self._on_audio:
                self._on_audio(audio)
        except Exception:
            pass
        return (in_data, 0)

    def read_chunk(self, duration_sec: float = 0.5) -> Optional[np.ndarray]:
        """读取指定时长的音频数据"""
        target_samples = int(duration_sec * self.sample_rate)
        try:
            import pyaudio
            data = self._stream.read(int(target_samples), exception_on_overflow=False)
            return np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
        except Exception as e:
            _log.debug(f"读取音频块失败: {e}")
            return None

    def get_buffer_duration(self) -> float:
        total = sum(len(c) for c in self._buffer)
        return total / self.sample_rate

    def pop_audio(self, duration_sec: float) -> Optional[np.ndarray]:
        target = int(duration_sec * self.sample_rate)
        chunks = []
        total = 0
        while self._buffer and total < target:
            chunk = self._buffer.popleft()
            chunks.append(chunk)
            total += len(chunk)
        if chunks:
            return np.concatenate(chunks)
        return None

    def stop(self):
        self._running = False
        if self._stream:
            try:
                self._stream.stop_stream()
                self._stream.close()
            except Exception:
                pass
            self._stream = None
        if self._p:
            try:
                self._p.terminate()
            except Exception:
                pass
            self._p = None

    def save_to_wav(self, audio: np.ndarray, filepath: str):
        """保存 float32 音频为 WAV"""
        audio_int16 = (audio * 32767).astype(np.int16)
        with wave.open(filepath, 'wb') as wf:
            wf.setnchannels(self.channels)
            wf.setsampwidth(2)
            wf.setframerate(self.sample_rate)
            wf.writeframes(audio_int16.tobytes())


# ============================================================================
# 简单能量 VAD（无需模型，用于实时检测）
# ============================================================================
class EnergyVAD:
    """基于能量的简单语音活动检测"""

    def __init__(self, threshold: float = 0.02, silence_duration: float = 1.5, sample_rate: int = 16000):
        self.threshold = threshold
        self.silence_samples = int(silence_duration * sample_rate)
        self._silence_count = 0
        self._is_speaking = False

    def process(self, audio: np.ndarray) -> bool:
        energy = np.abs(audio).mean()
        if energy > self.threshold:
            self._silence_count = 0
            if not self._is_speaking:
                self._is_speaking = True
                return True  # 语音开始
        else:
            if self._is_speaking:
                self._silence_count += len(audio)
                if self._silence_count >= self.silence_samples:
                    self._is_speaking = False
                    return True  # 语音结束（触发转写）
        return False

    @property
    def is_speaking(self) -> bool:
        return self._is_speaking


# ============================================================================
# STT 引擎
# ============================================================================
class STTEngine:
    """语音转文字引擎

    ★ 1.9.0（你的要求）：**写死只用项目内的 data/whisper 那一套**
    （`whisper-cli.exe` + `ggml-small.bin`），不再在 whisper.cpp / faster-whisper /
    OpenAI API 之间挑来挑去 —— 设置页里也只剩一个"是否启用"复选框。
    """

    def __init__(self, mode: str = "whisper_local"):
        """
        mode: 参数保留只为兼容老调用方 —— **一律写死 whisper_local**
              （你要的"只用 data/whisper 这一套，不要多选择"）
        """
        self.mode = "whisper_local"
        self._model = None
        self._model_name = "small"  # 只用来拼 data/whisper/ggml-<名>.bin
        self._language = "zh"  # zh/en/auto
        self._initialized = False
        self._init_lock = threading.Lock()

        # whisper_local 后端（延迟加载）
        self._whisper_cli = None

        # 回调
        self.on_transcription: Optional[Callable[[str], None]] = None
        self.on_partial: Optional[Callable[[str], None]] = None
        self.on_speech_start: Optional[Callable[[], None]] = None
        self.on_speech_end: Optional[Callable[[], None]] = None

    def configure(self, mode: str = None, model_name: str = None, language: str = None):
        # ★ 1.9.0: mode 一律忽略（写死 whisper_local）；只允许改语言。
        self.mode = "whisper_local"
        if language:
            self._language = language
        self._initialized = False  # 强制重新初始化
        self._whisper_cli = None
        _log.info(f"STT 配置: mode=whisper_local, lang={self._language}（后端固定 data/whisper）")

    def initialize(self) -> bool:
        if self._initialized:
            return True
        with self._init_lock:
            if self._initialized:
                return True

            # ★ 1.9.0: 写死走项目内 whisper.cpp —— 不再挑后端。
            try:
                ok = self._init_whisper_local()
                if ok:
                    self.mode = "whisper_local"
                    self._initialized = True
                    _log.info("STT 引擎就绪: whisper_local（data/whisper）")
                    return True
                # 缺东西时给一句"能照着做"的话，而不是干巴巴的"未初始化"
                from src.core.whisper_local import (DEFAULT_WHISPER_CLI,
                                                    DEFAULT_WHISPER_MODEL)
                _missing = []
                if not os.path.isfile(DEFAULT_WHISPER_CLI):
                    _missing.append(f"whisper-cli.exe（应位于 {DEFAULT_WHISPER_CLI}）")
                if not os.path.isfile(DEFAULT_WHISPER_MODEL):
                    _missing.append(f"ggml-small.bin（应位于 {DEFAULT_WHISPER_MODEL}）")
                _log.warning("STT 用不了，data/whisper 里还缺："
                             + ("、".join(_missing) if _missing else "（未知原因，见上一条日志）"))
                _log.warning("补齐办法：把 ggml-small.bin（约 465MB）放到 data/whisper/ 即可 —— "
                             "下载：python -c \"from huggingface_hub import snapshot_download; "
                             "snapshot_download('ggerganov/whisper.cpp', "
                             "allow_patterns=['ggml-small.bin'], local_dir='data/whisper')\""
                             "（国内先设 HF_ENDPOINT=https://hf-mirror.com 与代理）")
            except Exception as e:
                _log.error(f"STT 初始化失败: {e}")

        return False

    def _auto_detect(self) -> str:
        """自动检测可用引擎（优先本地）"""
        # 1. 检查预编译 whisper.cpp CLI
        try:
            from src.core.whisper_local import WhisperCLI
            cli = WhisperCLI()
            if cli.available:
                _log.info("检测到 whisper.cpp CLI (本地)")
                return "whisper_local"
        except Exception:
            pass
        # 2. 检查 faster_whisper
        try:
            import faster_whisper
            _log.info("检测到 faster-whisper")
            return "faster_whisper"
        except ImportError:
            pass
        # 3. 检查 whisper.cpp HTTP 服务
        try:
            import httpx
            _log.info("尝试 whisper.cpp HTTP 模式")
            return "whisper_cpp"
        except ImportError:
            pass
        # 4. 回退到 OpenAI API
        _log.info("使用 OpenAI Whisper API")
        return "openai_api"

    def _init_faster_whisper(self) -> bool:
        try:
            from faster_whisper import WhisperModel
            import torch
            device = "cuda" if torch.cuda.is_available() else "cpu"
            compute_type = "float16" if device == "cuda" else "int8"
            _path = _resolve_model_dir(self._model_name)   # ★ 优先项目内 data/models/<名>
            if _path != self._model_name:
                _log.info(f"使用项目内模型目录: {_path}")
            self._model = WhisperModel(_path, device=device, compute_type=compute_type)
            _log.info(f"faster-whisper 模型加载完成: {_path} ({device}/{compute_type})")
            return True
        except ImportError:
            _log.warning("faster-whisper 未安装: pip install faster-whisper")
            return False
        except Exception as e:
            _log.error(f"faster-whisper 初始化失败: {e}")
            return False

    def _init_whisper_cpp(self) -> bool:
        """检查 whisper.cpp HTTP 服务是否可用"""
        try:
            import httpx
            resp = httpx.get("http://127.0.0.1:8080/", timeout=3)
            if resp.status_code == 200:
                _log.info("whisper.cpp HTTP 服务可用")
                return True
        except Exception:
            pass
        _log.warning("whisper.cpp HTTP 服务不可用 (http://127.0.0.1:8080/)")
        return False

    def _init_whisper_local(self) -> bool:
        """初始化 whisper.cpp CLI 本地后端"""
        try:
            from src.core.whisper_local import WhisperCLI
            self._whisper_cli = WhisperCLI()
            if self._whisper_cli.available:
                _log.info("whisper.cpp CLI 本地后端就绪")
                return True
            else:
                _log.warning("whisper.cpp CLI 路径无效，请检查配置")
                return False
        except Exception as e:
            _log.warning(f"whisper.cpp CLI 初始化失败: {e}")
            return False

    # ---- 转写方法 ----

    def transcribe(self, audio: np.ndarray, language: str = None) -> Optional[str]:
        """转写音频（同步）"""
        if not self.initialize():
            _log.error("STT 未初始化")
            return None

        lang = language or self._language
        try:
            if self.mode == "faster_whisper":
                return self._transcribe_faster_whisper(audio, lang)
            elif self.mode == "whisper_cpp":
                return self._transcribe_whisper_cpp(audio, lang)
            elif self.mode == "whisper_local":
                return self._transcribe_whisper_local(audio, lang)
            elif self.mode == "openai_api":
                return self._transcribe_openai(audio, lang)
        except Exception as e:
            _log.error(f"转写失败: {e}")
        return None

    def transcribe_file(self, filepath: str, language: str = None) -> Optional[str]:
        """转写音频文件"""
        try:
            import soundfile as sf
            audio, sr = sf.read(filepath, dtype='float32')
            if sr != 16000:
                # 简单重采样
                from scipy import signal
                audio = signal.resample(audio, int(len(audio) * 16000 / sr))
            return self.transcribe(audio, language)
        except Exception as e:
            _log.error(f"音频文件转写失败: {e}")
            return None

    def _transcribe_faster_whisper(self, audio: np.ndarray, language: str) -> Optional[str]:
        if self._model is None:
            return None
        segments, info = self._model.transcribe(audio, language=language if language != "auto" else None,
                                                 beam_size=5, vad_filter=True)
        text = " ".join(seg.text for seg in segments)
        _log.debug(f"faster-whisper 转写完成: {text[:50]}...")
        return text.strip()

    def _transcribe_whisper_cpp(self, audio: np.ndarray, language: str) -> Optional[str]:
        import httpx
        # 保存临时 WAV
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            tmp_path = f.name
        try:
            import soundfile as sf
            sf.write(tmp_path, audio, 16000)
            with open(tmp_path, "rb") as f:
                files = {"file": ("audio.wav", f, "audio/wav")}
                params = {"response_format": "json"}
                if language != "auto":
                    params["language"] = language
                resp = httpx.post("http://127.0.0.1:8080/inference", files=files,
                                  params=params, timeout=60)
            if resp.status_code == 200:
                data = resp.json()
                return data.get("text", "").strip()
            else:
                _log.error(f"whisper.cpp HTTP 错误: {resp.status_code}")
        finally:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass
        return None

    def _transcribe_whisper_local(self, audio: np.ndarray, language: str) -> Optional[str]:
        """whisper.cpp CLI 本地转写"""
        if self._whisper_cli is None:
            _log.error("whisper_cli 未初始化")
            return None
        return self._whisper_cli.transcribe(audio, language if language != "auto" else "zh")

    def _transcribe_openai(self, audio: np.ndarray, language: str) -> Optional[str]:
        import httpx
        from src.utils.config import get_config
        cfg = get_config().llm
        # 保存临时 WAV
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            tmp_path = f.name
        try:
            import soundfile as sf
            sf.write(tmp_path, audio, 16000)
            with open(tmp_path, "rb") as f:
                resp = httpx.post(
                    f"{cfg.host}/audio/transcriptions",
                    headers={"Authorization": f"Bearer {cfg.api_key}"},
                    files={"file": ("audio.wav", f, "audio/wav")},
                    data={"model": "whisper-1", "language": language if language != "auto" else "zh"},
                    timeout=30,
                )
            if resp.status_code == 200:
                return resp.json().get("text", "").strip()
            else:
                _log.error(f"OpenAI Whisper API 错误: {resp.status_code} {resp.text}")
        finally:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass
        return None

    # ---- 实时语音识别流 ----

    def start_realtime(self, duration_per_chunk: float = 3.0, vad_threshold: float = 0.02):
        """启动实时语音识别（后台线程）"""
        if not self.initialize():
            return

        self._realtime_running = True
        self._mic = MicrophoneCapture()
        self._vad = EnergyVAD(threshold=vad_threshold)
        self._realtime_chunk_duration = duration_per_chunk
        self._speech_buffer: list[np.ndarray] = []

        if not self._mic.start():
            _log.error("实时 STT: 麦克风启动失败")
            return

        self._realtime_thread = threading.Thread(target=self._realtime_loop, daemon=True)
        self._realtime_thread.start()
        _log.info(f"实时 STT 启动: chunk={duration_per_chunk}s, vad_threshold={vad_threshold}")

    def _realtime_loop(self):
        while getattr(self, '_realtime_running', False):
            try:
                chunk = self._mic.read_chunk(0.3)
                if chunk is None:
                    # ★ 1.8.10: 必须 sleep —— read_chunk() 自己吞掉异常并返回 None，
                    #   所以外层那个带 sleep 的 except 永远不会被触发。
                    #   设备被拔掉/驱动报错时会变成 100% CPU 空转。
                    time.sleep(0.1)
                    continue

                # VAD 检测
                vad_event = self._vad.process(chunk)

                if self._vad.is_speaking:
                    self._speech_buffer.append(chunk)

                # 语音结束 → 转写
                if vad_event and not self._vad.is_speaking and self._speech_buffer:
                    if self.on_speech_end:
                        self.on_speech_end()
                    audio = np.concatenate(self._speech_buffer)
                    self._speech_buffer.clear()
                    text = self.transcribe(audio)
                    if text and self.on_transcription:
                        self.on_transcription(text)

                # 语音开始
                if vad_event and self._vad.is_speaking:
                    if self.on_speech_start:
                        self.on_speech_start()

                # 超长语音自动分段
                if self._speech_buffer:
                    total_dur = sum(len(c) for c in self._speech_buffer) / 16000
                    if total_dur > 15:
                        audio = np.concatenate(self._speech_buffer)
                        self._speech_buffer.clear()
                        text = self.transcribe(audio)
                        if text and self.on_partial:
                            self.on_partial(text)

            except Exception as e:
                _log.error(f"实时 STT 循环异常: {e}")
                time.sleep(1)

    def stop_realtime(self):
        self._realtime_running = False
        if hasattr(self, '_mic'):
            self._mic.stop()
        _log.info("实时 STT 已停止")


# ============================================================================
# 全局单例
# ============================================================================
_stt_instance: Optional[STTEngine] = None


def get_stt() -> STTEngine:
    global _stt_instance
    if _stt_instance is None:
        _stt_instance = STTEngine()
    return _stt_instance
