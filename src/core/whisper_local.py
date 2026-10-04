"""
whisper.cpp 本地 STT 后端
================================================================================
通过 subprocess 调用 whisper-cli.exe，实现本地离线语音转文字。

依赖：
  - whisper-blas-bin-x64/Release/whisper-cli.exe  （预编译 BLAS 加速版）
  - whisper.cpp-master/models/ggml-small.bin     （模型文件 465MB）
  - 或 ggml-tiny.en.bin（74MB，英文优化）、Silero VAD

使用方式：
  from src.core.whisper_local import WhisperCLI
  cli = WhisperCLI()
  result = cli.transcribe("audio.wav")  # 返回文本

配置路径：
  WHISPER_CLI = "D:/Dtopdesk/.../whisper-blas-bin-x64/Release/whisper-cli.exe"
  WHISPER_MODEL = "D:/Dtopdesk/.../models/ggml-small.bin"
================================================================================
"""
import os
import subprocess
import tempfile
import logging
from pathlib import Path
from typing import Optional

import numpy as np

_log = logging.getLogger("WhisperCLI")

# ====== 默认路径 ======
# whisper-cli.exe 和模型文件放在编译目录中：
#   whisper-cli.exe     （可执行文件）
#   ggml-small.bin      （推荐，多语言 465MB）
#   ggml-tiny.en.bin    （轻量，英文 74MB）
#
# ★★ 1.9.4：目录改为**走集中管理的解析器**（用户要求"模型散落各处，希望集中管理"）。
#   规范位置是 `data/models/whisper/`；**找不到就回落旧位置 `data/whisper/`** ——
#   所以没搬的机器照常能用（迁移靠 `python tools/model_manager.py migrate`，是显式的）。
from src.core.models import resolve as _resolve_model          # noqa: E402

_BASE = (_resolve_model("whisper")
         or (Path(__file__).parent.parent.parent / "data" / "whisper"))
DEFAULT_WHISPER_CLI = str(_BASE / "whisper-cli.exe")
DEFAULT_WHISPER_MODEL = str(_BASE / "ggml-small.bin")
DEFAULT_WHISPER_MODEL_TINY = str(_BASE / "ggml-tiny.en.bin")


class WhisperCLI:
    """whisper.cpp 本地 CLI 封装"""

    def __init__(self, executable: str = None, model: str = None,
                 language: str = "zh", threads: int = 4):
        """
        Args:
            executable: whisper-cli.exe 路径（None=读取config或自动检测）
            model: ggml 模型文件路径（None=读取config或自动检测）
            language: 目标语言 (zh/en/auto)
            threads: CPU 线程数
        """
        # 优先从 config.json 读取路径
        try:
            from src.utils.config import get_config
            cfg = get_config().stt
            if not executable and cfg.whisper_cli_path:
                executable = cfg.whisper_cli_path
            if not model and cfg.whisper_model_path:
                model = cfg.whisper_model_path
            if cfg.whisper_threads:
                threads = cfg.whisper_threads
            if cfg.language:
                language = cfg.language
        except Exception:
            pass

        self.executable = executable or DEFAULT_WHISPER_CLI
        self.model = model or DEFAULT_WHISPER_MODEL
        self.language = language
        self.threads = threads

        # 验证可用性
        self.available = os.path.isfile(self.executable) and os.path.isfile(self.model)
        if not self.available:
            _log.warning(f"[WhisperCLI] 不可用: exe={os.path.exists(self.executable)}, "
                         f"model={os.path.exists(self.model)}")
        else:
            _log.info(f"[WhisperCLI] 就绪: {self.executable}, model={Path(self.model).name}")

    def transcribe(self, audio: np.ndarray, language: str = None) -> Optional[str]:
        """
        转写音频数据（numpy float32 array, 16kHz）
        Returns: 转写文本 或 None
        """
        if not self.available:
            _log.error("[WhisperCLI] 不可用")
            return None

        # 保存临时 WAV
        tmp_path = None
        try:
            import soundfile as sf
            fd, tmp_path = tempfile.mkstemp(suffix=".wav")
            os.close(fd)
            sf.write(tmp_path, audio, 16000)
            return self.transcribe_file(tmp_path, language)
        except Exception as e:
            _log.error(f"[WhisperCLI] 临时文件错误: {e}")
            return None
        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass

    def _build_cmd(self, filepath: str, lang: str, json_out: bool = True):
        """★ 1.9.0: 组装 whisper-cli 命令 —— **把踩过的坑都固化在这里**。

        真机实测（2026-09-16，用户机器 + 我这边都复现），同一段音频：

            -m <绝对路径>  -f <任意>                → 进程返回 3221226505 (0xC0000409) 崩
            -m <相对路径>  -f <绝对路径>            → 正常
            -m <相对路径>  -f <相对路径>            → 正常
            （cwd 都设成项目根；纯 ASCII 路径也一样崩 —— **不是中文路径的问题**）

        结论：**这个 whisper.cpp 构建吃不下绝对路径的 `-m`**（0xC0000409 = 栈溢出，
        像是模型路径那块缓冲区的问题）。`-f`（音频）用绝对路径没问题。

        所以统一改成：**cwd 切到模型所在目录，`-m` 只给文件名**；音频仍传绝对路径
        （音频可能在 %TEMP%、也可能在任意下载目录，保持绝对最稳）。

        另外 `-ng`（禁 GPU）是防御性参数：不加时它会探测 GPU/BLAS 后端
        （输出 `use gpu = 1 / devices = 2`），这套是 CPU/BLAS 构建，禁掉没有代价。

        Returns: (cmd, cwd, 出错时附加的提示文字)
        """
        model_path = Path(self.model)
        cmd = [
            self.executable,
            "-m", model_path.name,          # ★ 只给文件名（见上面说明）
            "-f", str(Path(filepath).resolve()),   # 音频给绝对路径（这是安全的）
            "-l", lang,
            "-t", str(self.threads),
            "-ng",
            "--no-timestamps",
            "--print-progress", "false",
        ]
        if json_out:
            cmd.append("-oj")
        hint = ""
        if Path(self.model).is_absolute():
            hint = ("（提示：whisper-cli 的 -m 不能给绝对路径，已自动改用"
                    "「cwd=模型目录 + 文件名」的方式）")
        return cmd, str(model_path.parent), hint

    @staticmethod
    def _text_from_raw_output(raw: str) -> str:
        """★ 1.9.0: 从 whisper-cli 的原始 stdout 里抠出转写文本。

        用途：`-oj`（JSON）解析不出来时的兜底 —— 实测这个构建就是这种情况，
        而 stdout 里本来就有干净的中文转写。规则：
          · 丢掉时间戳行（形如 `[00:00:09.000 --> 00:00:11.160] 内容`，内容要留）
          · 丢掉以 { 或 [ 开头的 JSON/元信息行
          · 其余按出现顺序拼起来
        """
        if not raw:
            return ""
        import re as _re
        parts = []
        for line in raw.splitlines():
            s = line.strip()
            if not s:
                continue
            if s.startswith(("{", "}")):
                continue
            m = _re.match(r"^\[[\d:.\s\->]+\]\s*(.*)$", s)   # [时间] 文本
            if m:
                s = m.group(1).strip()
                if not s:
                    continue
            elif s.startswith("["):                          # 其它方括号元信息
                continue
            parts.append(s)
        return " ".join(parts).strip()

    def transcribe_file(self, filepath: str, language: str = None) -> Optional[str]:
        """
        转写音频文件
        Returns: 转写文本 或 None
        """
        if not self.available:
            _log.error("[WhisperCLI] 不可用")
            return None

        lang = language or self.language

        cmd, run_cwd, hint = self._build_cmd(filepath, lang, json_out=True)

        try:
            _log.debug(f"[WhisperCLI] 执行: whisper-cli -m {Path(self.model).name} -f {Path(filepath).name}")
            result = subprocess.run(
                cmd,
                cwd=run_cwd,
                capture_output=True,
                text=True,
                timeout=120,  # 2 分钟超时
                encoding="utf-8",
                errors="replace",
            )

            if result.returncode != 0:
                _log.error(f"[WhisperCLI] 进程返回非零: {result.returncode}{hint}\n"
                           f"{result.stderr[:200]}")
                return None

            # 解析 JSON 输出
            import json
            # whisper-cli -oj 输出 JSON 到 stdout，每行一个 JSON 对象
            output = result.stdout.strip()
            if output:
                # 取最后一行（通常是完整结果的 JSON）
                lines = output.split("\n")
                for line in reversed(lines):
                    line = line.strip()
                    if line and line.startswith("{"):
                        try:
                            data = json.loads(line)
                            text = data.get("text", "")
                            if text.strip():
                                _log.debug(f"[WhisperCLI] 转写完成: {text[:80]}...")
                                return text.strip()
                        except json.JSONDecodeError:
                            continue

            # ★ 1.9.0: 兜底必须**真的把文本取出来**。
            #   真机实测：这个构建的 `-oj` 并没有给出我们预期的逐行 JSON，但
            #   stdout 里**就是干净的转写文本**。老代码这里只打了一行 warning
            #   就 `return None` —— 于是"识别明明成功了却返回空"，用户看到的是
            #   "音频识别失败"。现在真正解析一次原始输出。
            text = self._text_from_raw_output(result.stdout)
            if text:
                _log.debug(f"[WhisperCLI] 从原始输出取到文本: {text[:60]}...")
                return text
            _log.warning(f"[WhisperCLI] 没能从输出里取出文本: {(result.stdout or '')[:120]}")

            return None

        except subprocess.TimeoutExpired:
            _log.error("[WhisperCLI] 转写超时 (120s)")
            return None
        except FileNotFoundError:
            _log.error(f"[WhisperCLI] 可执行文件未找到: {self.executable}")
            return None
        except Exception as e:
            _log.error(f"[WhisperCLI] 转写异常: {e}")
            return None


# ============================================================================
# 全局单例
# ============================================================================
_whisper_instance: Optional[WhisperCLI] = None


