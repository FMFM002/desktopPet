"""
配置管理模块
- 从 .env 加载敏感信息
- 管理所有应用配置
- 自动持久化到 config.json 和 .env
"""
import os
import json
import logging
import threading
from pathlib import Path
from dataclasses import dataclass, field, asdict
from typing import Optional

_log = logging.getLogger("Config")

# ★ 1.8.2: config.json 可能被「设置窗口后台线程」和「主线程定时器」同时保存，
#   两者会用同一个 config.json.tmp，replace 会互相踩 → 偶发 FileNotFoundError，
#   表现就是"点了保存但没保存上"。这里用一把进程内锁把保存串行化。
_SAVE_LOCK = threading.RLock()

# ★ 1.8.2: 视觉 API Key 单独存 .env（config.json 里不落 key）
VISION_KEY_ENV = "DEEPSEEK_VISION_API_KEY"

# 加载 .env
try:
    from dotenv import load_dotenv, set_key
    BASE_DIR = Path(__file__).resolve().parent.parent.parent
    ENV_PATH = BASE_DIR / ".env"
    if ENV_PATH.exists():
        load_dotenv(ENV_PATH)
        _log.info(f".env 已加载: {ENV_PATH}")
    else:
        _log.warning(f".env 未找到: {ENV_PATH}")
except ImportError:
    _log.error("python-dotenv 未安装")
    set_key = None
    BASE_DIR = Path(__file__).resolve().parent.parent.parent
    ENV_PATH = BASE_DIR / ".env"

VERIFY_DEPS = True  # 启动时是否验证依赖

# ====== 提供商预设 ======
PROVIDER_PRESETS = {
    "deepseek": {
        "name": "DeepSeek",
        "host": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
    },
    "openai": {
        "name": "OpenAI",
        "host": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
    },
    "ollama": {
        "name": "Ollama (本地)",
        "host": "http://localhost:11434/v1",
        "model": "qwen2.5:7b",
    },
    "custom": {
        "name": "自定义",
        "host": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
    },
}


@dataclass
class LLMConfig:
    """LLM API 配置"""
    provider: str = "deepseek"  # deepseek | openai | ollama | custom
    host: str = "https://api.deepseek.com/v1"
    api_key: str = ""
    model: str = "deepseek-chat"
    temperature: float = 0.7
    max_tokens: int = 2048


@dataclass
class VisionConfig:
    """视觉模型配置"""
    enabled: bool = True
    # 识别模式: "api"(云端视觉API) | "ollama"(本地Ollama视觉) | "ocr"(本地OCR引擎)
    mode: str = "api"
    host: str = "https://api.deepseek.com/v1"
    api_key: str = ""
    model: str = "deepseek-chat"
    # Ollama 视觉模型名
    ollama_model: str = "llava:7b"
    ollama_host: str = "http://localhost:11434/v1"
    # 本地 OCR 引擎: "tesseract" | "easyocr"
    ocr_engine: str = "tesseract"
    ocr_lang: str = "chi_sim+eng"
    # ★ 1.6.0: 多模态直传 —— 把图片作为 image_url 直接塞进对话 messages，
    #   让对话模型自己看图（而不是先识别成文字再回灌）。
    #   要求所用模型支持视觉；若模型不支持会自动降级回文字识别。
    multimodal: bool = False
    # ★ 1.9.0: 多模态的后端选择（设置页「多模态」页最上面那个下拉）：
    #   "api"    = 把图直接塞给**对话模型**自己看（用 LLM API 页的模型）
    #   "ollama" = 用本地 Ollama 视觉模型先看图再回答
    #   与 vision.mode（②视觉识别的 api/ollama/ocr）互相独立。
    multimodal_mode: str = "api"


@dataclass
class PetConfig:
    """桌宠形象配置"""
    character: str = "铃兰"
    behavior: str = "待机"
    scale: float = 1.0
    idle_animation: str = "breathe"
    think_animation: str = "thinking"
    speak_animation: str = "talk"
    exec_animation: str = "exec"
    # 性格/人设
    personality_prompt: str = ""  # 自定义系统提示词（为空则用默认）
    reply_style: str = "concise"  # concise | balanced | detailed
    emoji_level: str = "light"    # none | light | medium | heavy
    # ★ 1.9.0: 「输出控制」—— 只管"输出成什么样"（由角色 settings.json 的
    #   输出控制 块同步过来；以前这些混在 persona 文本里，等于改不动）
    no_action_description: bool = True   # 严禁动作/神态/旁白描写
    no_markdown: bool = False            # 严禁 markdown 字符
    allow_code_block: bool = True
    allow_json: bool = True
    # ★ 1.9.0: 「行为控制」—— 只管"它会做什么"
    meme_tendency: str = "normal"        # off | rare | normal | often
    meme_tags_prefer: list = field(default_factory=lambda: ["happy"])
    gif_reaction: bool = True            # 是否响应工具返回的 gif 提示
    proactive_talk: bool = True          # 主动说话是否带"在用什么程序"的观察


@dataclass
class MonitorConfig:
    """监控配置（重构 v2.0）"""
    screen_enabled: bool = False
    screen_interval: int = 60           # 截图间隔（秒）
    screen_quality: int = 49            # JPEG 质量 1-100
    screen_max_photos: int = 10         # 最多保留截图数
    keyboard_enabled: bool = True
    mouse_enabled: bool = False         # 已废弃，保留兼容
    audio_enabled: bool = False         # 麦克风录音开关
    # === 下一轮讨论 ===
    system_audio_enabled: bool = False  # 系统音频捕获（实验性，默认关闭）
    # === ===
    audio_threshold: int = 500          # 音量阈值
    clipboard_enabled: bool = True      # 剪贴板监控
    # === 下一轮讨论 ===
    window_monitor_mode: str = "polling"  # polling | event (SetWinEventHook 实验性)
    process_monitor_mode: str = "snapshot_diff"  # snapshot_diff | wmi_event
    # === ===
    window_metadata_enabled: bool = False
    process_metadata_enabled: bool = False
    system_enabled: bool = True         # 系统监控（进程内存 + 窗口）
    sys_interval: int = 10             # 系统监控采集间隔（秒）
    mouse_min_move: int = 1
    mouse_sample_interval: float = 0.3
    summarize_interval_minutes: int = 10  # pipeline 清洗间隔（分钟）
    summarize_trigger_count: int = 100    # pipeline 清洗触发条数
    raw_data_cap: int = 1000             # raw_events 上限


@dataclass
class TTSConfig:
    """TTS 配置"""
    enabled: bool = True
    mode: str = "auto"  # auto | click
    character: str = "feibi"  # feibi | mika | thirtyseven | linglan
    provider: str = "genie"  # genie (默认本地) | edge (在线后备)
    genie_data_dir: str = ""  # 自定义 GenieData 目录（空=自动检测素材目录）
    genie_character_dir: str = ""  # 自定义角色模型目录（空=自动检测）
    # ★ 1.6.0: GENIE 本地模型常驻约 4.5GB 内存，空闲这么久就自动释放（0=从不释放）
    genie_idle_release_minutes: int = 15
    # ★ 1.6.0: 启动时是否后台预加载 GENIE（关掉可省 4.5GB，代价是第一次播报要等模型加载）
    genie_preload_on_start: bool = False


@dataclass
class IdleConfig:
    """主动待机说话配置（★ 1.9.0：它现在是"自动工具"，在设置 → 工具设置 → idle_talk 里配）"""
    enabled: bool = True
    interval_minutes: int = 10  # 主动说话间隔（分钟）
    # ★ 1.9.0: 自定义触发提示词（留空用内置默认）
    prompt: str = ""


@dataclass
class STTConfig:
    """STT 语音转文字配置"""
    enabled: bool = False
    mode: str = "auto"  # auto | faster_whisper | whisper_cpp | whisper_local | openai_api
    model_name: str = "base"  # tiny/base/small/medium/large
    language: str = "zh"  # zh/en/auto
    # whisper_local 专属
    whisper_cli_path: str = ""  # whisper-cli.exe 路径（空=自动检测）
    whisper_model_path: str = ""  # ggml 模型路径（空=自动检测）
    whisper_threads: int = 4


@dataclass
class UserProfileConfig:
    """用户画像配置"""
    enabled: bool = True
    auto_extract: bool = True  # 自动从对话中提取画像
    inject_context: bool = True  # 注入到 LLM 上下文


@dataclass
class UIConfig:
    """UI 配置"""
    pet_position: tuple = (100, 100)
    chat_window_width: int = 420
    chat_window_height: int = 640
    # ★ 1.9.1: `sidebar_width` / `sidebar_visible` 已删除 ——
    #   聊天窗口的左侧栏整个移除了（用户方案 2：删掉左侧栏，
    #   左上角换成「形象名 + tokens」，书签功能一并删除）。
    #   旧 config.json 里这两个键留着不读也不写，不影响兼容。
    chat_distance: int = 10          # ★ 距形象距离
    opacity: float = 0.92
    always_on_top: bool = True
    style: str = "light"
    # ★ 个性化颜色（浅色主题，与设置界面一致）
    ui_bg_color: str = "#ffffff"
    chat_bg_color: str = "#f7f7fa"
    # ★ 聊天文本默认无底色（transparent），保留字段以便需要时自定义
    user_bubble_color: str = "transparent"
    user_text_color: str = "#222222"
    bot_bubble_color: str = "transparent"
    bot_text_color: str = "#222222"
    # ★ 聊天头像（空=使用自带形象）
    user_avatar_path: str = ""
    pet_avatar_path: str = ""


@dataclass
class ToolDownloadConfig:
    """工具下载路径配置"""
    download_path: str = ""  # 下载目录（空=使用默认 data/downloads）
    # ★ 1.9.0: action 级禁用 —— 设置 → 工具设置 → 某工具 → 设置 里的"禁用"复选框。
    #   形如 {"weather_query": ["query"], "pixiv": ["download"]}
    #   由 ToolManager.execute_tool 在执行前拦截。
    disabled_actions: dict = field(default_factory=dict)


@dataclass
class PipelineConfig:
    """数据清洗 Pipeline 配置"""
    enabled: bool = True
    vision_queue_enabled: bool = False   # 视觉识别异步队列（实验性）
    stt_queue_enabled: bool = False      # STT 异步队列（实验性）
    # ★ 1.8.9: 大力清洗 —— 积压超过 bulk_threshold 时一次多吃点，
    #   否则按 trigger_count（默认 100）一批批啃，1000 条要跑十几分钟。
    bulk_threshold: int = 500            # 积压超过这个数 → 进入大力清洗
    bulk_batch_size: int = 500           # 大力清洗时单批取多少条原始数据
    bulk_max_tokens: int = 4000          # 大力清洗时 LLM 输出的 token 上限


@dataclass
class LTMSummarizerConfig:
    """★ 1.7.3: 长期记忆定期总结配置（聚类合并 + 衰减清理）"""
    enabled: bool = True
    file_size_threshold_kb: int = 1024   # DB 文件 ≥ 1MB 触发
    count_threshold: int = 200           # 条目 ≥ 200 触发
    cooldown_minutes: int = 5            # 两次 summarize 最小间隔
    decay_idle_days: int = 7             # 多久未被访问开始衰减
    decay_rate_per_period: float = 0.1   # 每 decay_idle_days 扣这么多 confidence
    delete_threshold: float = 0.15       # confidence ≤ 此值删除
    cluster_similarity: float = 0.75     # 聚类相似度阈值（difflib）
    run_on_start: bool = False           # 启动时是否立刻跑一次


@dataclass
class DatabaseConfig:
    """数据库配置（重构 v2.0 — 纯 SQLite，弃用 MySQL）"""
    enabled: bool = True


@dataclass
class AppConfig:
    """应用总配置"""
    llm: LLMConfig = field(default_factory=LLMConfig)
    vision: VisionConfig = field(default_factory=VisionConfig)
    pet: PetConfig = field(default_factory=PetConfig)
    monitor: MonitorConfig = field(default_factory=MonitorConfig)
    tts: TTSConfig = field(default_factory=TTSConfig)
    idle: IdleConfig = field(default_factory=IdleConfig)
    stt: STTConfig = field(default_factory=STTConfig)
    profile: UserProfileConfig = field(default_factory=UserProfileConfig)
    ui: UIConfig = field(default_factory=UIConfig)
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    pipeline: PipelineConfig = field(default_factory=PipelineConfig)
    ltm: LTMSummarizerConfig = field(default_factory=LTMSummarizerConfig)
    tools: ToolDownloadConfig = field(default_factory=ToolDownloadConfig)
    enabled_tools: list = field(default_factory=list)
    tool_policies: dict = field(default_factory=dict)  # tool_name → "resident"/"lazy"/"disabled"
    config_path: Path = BASE_DIR / "config.json"
    _raw_data: dict = field(default_factory=dict, repr=False)  # 原始 JSON 数据备份

    def __post_init__(self):
        self._load_from_env()
        self._load_from_file()
        _log.info(f"配置初始化完成: LLM={self.llm.provider}/{self.llm.model}, "
                  f"DB={'启用' if self.database.enabled else '禁用'}, "
                  f"TTS={'启用' if self.tts.enabled else '禁用'}, "
                  f"待机说话={'启用' if self.idle.enabled else '禁用'}/间隔{self.idle.interval_minutes}分钟")

    def _load_from_env(self):
        """从环境变量加载敏感配置（不再包含 MySQL）"""
        self.llm.api_key = os.getenv("DEEPSEEK_API_KEY", self.llm.api_key)
        self.llm.host = os.getenv("DEEPSEEK_BASE_URL", self.llm.host)
        self.llm.model = os.getenv("DEEPSEEK_MODEL", self.llm.model)
        # ★ 1.8.2: 视觉 Key 先读专用变量，没有再回落到 LLM Key
        #   （之前视觉 Key 既不进 config.json 也不进 .env，每次启动都被 LLM Key 覆盖）
        self.vision.api_key = (os.getenv(VISION_KEY_ENV)
                               or os.getenv("DEEPSEEK_API_KEY", self.vision.api_key))
        self.vision.host = os.getenv("DEEPSEEK_BASE_URL", self.vision.host)
        _log.debug(f"LLM API: {self.llm.host} / {self.llm.model}")

    def _load_from_file(self):
        """从 config.json 加载"""
        if self.config_path.exists():
            try:
                with open(self.config_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self._raw_data = data  # 保留原始数据供 ToolManager 读取
                self._update_from_dict(data)
                _log.debug(f"config.json 已加载")
            except Exception as e:
                _log.warning(f"config.json 加载失败: {e}")

    def save(self):
        """保存配置到文件（config.json）并同步 .env"""
        with _SAVE_LOCK:
            self._save_locked()

    def _save_locked(self):
        vision_dict = asdict(self.vision)
        vision_dict.pop("api_key", None)  # api_key 不写入 config.json
        data = {
            "pet": asdict(self.pet),
            "monitor": asdict(self.monitor),
            "tts": asdict(self.tts),
            "idle": asdict(self.idle),
            "stt": asdict(self.stt),
            "profile": asdict(self.profile),
            "ui": asdict(self.ui),
            "vision": vision_dict,
            "enabled_tools": self.enabled_tools,
            "tool_policies": self.tool_policies,
            "llm": {
                "provider": self.llm.provider,
                "host": self.llm.host,
                "model": self.llm.model,
                "temperature": self.llm.temperature,
                "max_tokens": self.llm.max_tokens,
            },
            "database": {"enabled": self.database.enabled},
            "pipeline": asdict(self.pipeline),
            "ltm": asdict(self.ltm),
            "tools": asdict(self.tools),
        }
        # 保存 config.json
        try:
            tmp_path = self.config_path.parent / (self.config_path.name + ".tmp")
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            tmp_path.replace(self.config_path)
        except Exception as e:
            _log.error(f"config.json 保存失败: {e}")
            raise

        # 同步 .env（不再包含 MySQL 配置）
        self._sync_env_keys({
            "DEEPSEEK_API_KEY": self.llm.api_key,
            "DEEPSEEK_BASE_URL": self.llm.host,
            "DEEPSEEK_MODEL": self.llm.model,
            # ★ 1.8.2: 视觉专用 Key。与 LLM Key 相同 → 传空并标记 remove_empty，
            #   把 .env 里的旧专用 Key 删掉（否则它会一直覆盖 LLM Key）。
            VISION_KEY_ENV: (self.vision.api_key
                             if self.vision.api_key != self.llm.api_key else ""),
        }, remove_empty={VISION_KEY_ENV})

        _log.info(f"配置已自动保存: {self.config_path}")

    @staticmethod
    def _sync_env_keys(kv: dict, remove_empty: frozenset = frozenset()):
        """批量写入 .env（纯 Python，不调 dotenv，避免任何阻塞风险）。

        remove_empty: 值为空时要【删除】该键的键名集合；
                      其余空值沿用旧行为（跳过，不动 .env）。
        """
        if not kv or not ENV_PATH:
            return
        try:
            if ENV_PATH.exists():
                text = ENV_PATH.read_text(encoding="utf-8")
            else:
                text = ""
            lines = text.splitlines(keepends=True)

            for key, value in kv.items():
                if not value:
                    if key in remove_empty:
                        lines = [ln for ln in lines
                                 if not (ln.startswith(key + "=") or ln.startswith(key + " "))]
                    continue
                found = False
                for i, line in enumerate(lines):
                    if line.startswith(key + "=") or line.startswith(key + " "):
                        lines[i] = f"{key}={value}\n"
                        found = True
                        break
                if not found:
                    if lines and not lines[-1].endswith("\n"):
                        lines[-1] += "\n"
                    lines.append(f"{key}={value}\n")

            tmp_env = ENV_PATH.parent / (ENV_PATH.name + ".tmp")
            tmp_env.write_text("".join(lines), encoding="utf-8")
            tmp_env.replace(ENV_PATH)
            _log.debug(f".env 已同步 {len(kv)} 个键")
        except Exception as e:
            _log.warning(f".env 同步失败: {e}")

    def _update_from_dict(self, data: dict):
        for key, value in data.items():
            if hasattr(self, key):
                attr = getattr(self, key)
                if isinstance(attr, (LLMConfig, VisionConfig, PetConfig,
                                     MonitorConfig, TTSConfig, IdleConfig,
                                     STTConfig, UserProfileConfig,
                                     UIConfig, DatabaseConfig, PipelineConfig,
                                     LTMSummarizerConfig,
                                     ToolDownloadConfig)):
                    if isinstance(value, dict):
                        for k, v in value.items():
                            if hasattr(attr, k):
                                setattr(attr, k, v)
                elif isinstance(attr, (list, dict)):
                    setattr(self, key, value)
                elif not isinstance(attr, (LLMConfig, VisionConfig, PetConfig,
                                           MonitorConfig, TTSConfig, IdleConfig,
                                           STTConfig, UserProfileConfig,
                                           UIConfig, DatabaseConfig, PipelineConfig,
                                           LTMSummarizerConfig,
                                           ToolDownloadConfig)):
                    try:
                        setattr(self, key, value)
                    except Exception:
                        pass


_config_instance: Optional[AppConfig] = None


def get_config() -> AppConfig:
    global _config_instance
    if _config_instance is None:
        _config_instance = AppConfig()
    return _config_instance
