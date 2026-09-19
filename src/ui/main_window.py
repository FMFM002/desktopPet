"""
主窗口 - 整合所有组件 v1.2.0
- 桌宠形象 + 聊天窗口
- 智能定位
- 工具日志窗口
- 监控总结管道
- TTS 语音播报
- 主动待机说话
- 剪贴板/窗口元数据/进程监控
- STT 语音转文字
- 用户画像 / 长期记忆
- 文件上传（视频/音频/文档）
- 终末地 / 哔哩哔哩 / JM 工具
"""
import json
import logging
import os
import re
import threading
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, QTimer, QPoint, QRect, QObject, Signal
from PySide6.QtGui import QScreen, QGuiApplication
from PySide6.QtWidgets import QApplication, QWidget

from src.ui.pet_proxy import PetProxy
from src.ui.chat_widget import ChatWidget
from src.ui.tool_log_widget import ToolLogWidget
from src.ui.settings_window import SettingsWindow
from src.ui.tray_icon import TrayManager
from src.ui.direct_dispatch_window import DirectDispatchWindow

from src.utils.config import get_config
from src.core.session import get_session, reset_session
from src.core.monitor import get_monitor, UnifiedMonitor
from src.core.llm_client import VisionClient, SummarizerClient, switch_provider
from src.core.database import get_db
from src.core.tts_engine import get_tts
from src.core.idle_talker import get_idle_talker
from src.core.input_monitor import get_input_monitor
from src.core.stt_engine import get_stt
from src.core.user_profile import get_profile, get_long_term_memory, get_profile_extractor
from src.core.data_pipeline import get_pipeline
from src.core.pet_manager import get_pet_manager

_log = logging.getLogger("MainWindow")

# ★ 1.9.0: 项目根目录（用于把 config 里的相对路径、工具返回的相对路径解析成绝对路径）
_BASE_DIR = Path(__file__).resolve().parent.parent.parent


class _ImageSignalRelay(QObject):
    """信号中转器：后台线程通过 Signal 安全回调到主线程"""
    image_recognized = Signal(str, str, str)  # filename, result, mode
    image_failed = Signal(str)                 # error message


class _TaskSignalRelay(QObject):
    """★ 1.8.2: 后台任务（音频/视频/文档/STT）回主线程的信号中转器。

    为什么不能用 QTimer.singleShot：它在「调用它的那个线程」里创建 QTimer，
    而 Python 的 threading.Thread 没有 Qt 事件循环，定时器永远不会触发——
    表现为「文件上传后卡片出来了，但内容永远送不到对话里」。
    Signal 跨线程是 queued connection，一定会回到主线程执行。
    """
    system_msg = Signal(str)   # 只显示在聊天区的系统提示
    user_msg = Signal(str)     # 当作一条用户消息发给 LLM
    # ★ 1.9.0（你拍板"做"）：主动说话 → 以**工具调用条目**注入会话
    #   （assistant(tool_calls=[idle_talk]) + tool 结果），不再是用户消息
    idle_trigger = Signal(str)
    heavy_ready = Signal()     # ★ 1.8.5: 重组件加载完毕 → 回主线程跑 _init_finalize
    # ★ 1.8.10: 话题总结跑在 "topic-summarize" 后台线程里，但它会触发
    #   话题切换与输入框禁用 —— 这两个都要碰 QWidget，必须排队回主线程。
    topic_changed = Signal(int)
    input_blocked = Signal(bool)
    # ★ 1.9.0: 工具在**后台线程**里产出文件（典型：jmcomic 的 PDF 是在下载/
    #   合成线程里做完的）→ 要弹卡片 + 写进历史，必须排队回主线程。
    #   以前这里直接 chat.file_card_in.emit(...)：那是纯显示信号、不落库，
    #   所以"PDF 卡片重启后消失"（W6）。统一走这个中转 + 统一落库入口。
    file_ready = Signal(str, str, str)   # (label, path, size)
    # ★ 1.9.1: ComfyUI 后台画图完成 → 往**上下文**插一条系统提示
    #   （用户原话："上下文插入系统提示：刚刚发送了xxxx图片"）
    #   为什么要单开一个信号：file_ready 只管"显示+落库"，
    #   而这条还要进 session.messages 让模型下一轮知道自己已经发过图了。
    context_note = Signal(str)


class MainWindow:
    """主窗口管理器"""

    CHAT_OFFSET_X = 330
    CHAT_OFFSET_Y = 0
    LOG_OFFSET_Y = 490

    def __init__(self):
        _log.info("正在初始化主窗口...")
        self.app = QApplication.instance()
        # ★ 1.5.0: 托盘应用必须防止"最后一个窗口关闭即退出"，否则托盘/聊天隐藏后进程直接退出
        try:
            self.app.setQuitOnLastWindowClosed(False)
        except Exception:
            pass

        # 状态（提前定义，避免后续 AttributeError）
        self._visible = True
        self._processing_image = False
        self._last_assistant_text = ""
        self._stt_active = False
        self._shutting_down = False   # ★ 1.8.5: 退出中 → 监控回调不再落库
        # ★ 1.9.0: 工具推给聊天窗口的附件（图片/文件卡片）待补进会话的正文。
        #   工具回调发生在工具调用链中间，不能当场插 assistant 消息
        #   （会破坏 tool_calls ↔ tool 结果配对 → 400），统一延后到本轮结束。
        self._pending_attachments: list[str] = []
        # ★ 1.9.0: 本轮已经推送过的附件路径（小写）——LLM 常先调 download
        #   再调 file_ops send，同一张图会显示两遍（W6 反馈"封面发了两次"）。
        #   每轮用户消息开始时清空。
        self._attachment_seen: set = set()
        # ★ 1.9.1: "此刻有没有一轮对话在跑"。后台异步产物（ComfyUI 画好的图）
        #   要靠它决定：直接插进 session.messages，还是先排队等本轮结束。
        self._turn_active: bool = False
        # ★ 1.9.1「指令直传」：DirectDispatchWindow 单例（懒创建）+ 本窗口内
        #   已发过的文件路径去重（避免同一次工具输出里多个同名路径被发多次）。
        self._direct_dispatch_win: Optional[DirectDispatchWindow] = None
        self._direct_dispatch_seen: set = set()
        self._batch_monitor_events: list[str] = []
        # ★ 1.7.0: 正在喂给长期记忆的 chat_records id（防异步在途重复投喂）
        self._chat_ltm_inflight: set = set()

        # ─── Phase 1: 形象立即显示 ───
        _log.debug("Phase1: 配置+形象+基础UI...")
        self.config = get_config()
        self.db = get_db()

        # ★ 桌宠独立进程 — 最先启动，GIF 渲染永不卡顿
        self.pet = PetProxy()
        # ★ 1.5.0: 形象线程 — 管理角色/GIF 切换，接收 LLM 思考内容
        self.pet_mgr = get_pet_manager(self.pet)
        self.pet_mgr.scan()
        self.pet_mgr.switch_character(self.config.pet.character)
        self.chat = ChatWidget()
        self.tool_log = ToolLogWidget()
        self._img_relay = _ImageSignalRelay()
        self._img_relay.image_recognized.connect(self._display_recognize_result)
        self._img_relay.image_failed.connect(self._on_image_recognize_failed)
        # ★ 1.8.2: 音频/视频/文档/STT 后台任务回主线程（QTimer.singleShot 在
        #   无事件循环的线程里不触发，是"文件发了但没反应"的根因）
        self._task_relay = _TaskSignalRelay()
        self._task_relay.system_msg.connect(self._on_task_system_msg)
        self._task_relay.user_msg.connect(self._on_task_user_msg)
        self._task_relay.idle_trigger.connect(self._on_idle_trigger_main)
        # ★ 1.8.5: 重组件线程加载完 → 用它回主线程跑 Phase5。
        #   以前是 QTimer.singleShot(0, self._init_finalize)，
        #   但那是在 heavy-load 子线程里调的 —— 子线程没有 Qt 事件循环，
        #   定时器永远不触发，_init_finalize 从没跑过（三份日志里都没有
        #   "Phase5: 更新定时器..." 这一行）。后果就是：
        #   _apply_config_to_ui() 从没在启动时执行 → 颜色/头像/窗口边距
        #   全用 ChatWidget 的默认值，而 config.json 里明明存着用户的设置
        #   （打开设置页看到的是对的，再点一次保存才生效）。
        self._task_relay.heavy_ready.connect(self._init_finalize)
        # ★ 1.8.10: 总结后台线程 → 主线程的 UI 回调（跨线程 Signal 是 queued）
        self._task_relay.topic_changed.connect(self._on_session_topic_changed)
        self._task_relay.input_blocked.connect(self.chat.set_input_blocked)
        # ★ 1.9.0: 后台线程产出的文件 → 主线程统一落库入口
        self._task_relay.file_ready.connect(self._on_tool_file_ready)
        # ★ 1.9.1: ComfyUI 后台画完 → 往上下文插一条系统提示
        self._task_relay.context_note.connect(self._on_context_note)

        self._batch_timer = QTimer()
        self._batch_timer.timeout.connect(self._flush_batch_summarize)

        # ★ 1.9.0（你的批注）：聊天窗口开着时，点它以外的**任何地方**都收起
        #   （不只形象）。WindowDeactivate 那条路只覆盖"抢走激活"的场景，
        #   点桌面/不抢焦点的窗口时根本不触发 → 用全局鼠标看门狗兜底。
        # ★ 接管（2026-09-17 22:32）：轮询从 100ms 提到 **30ms**。
        #   你实测"点外面还是有概率失败"—— 根因就是间隔太长：一次 60~80ms 的
        #   正常点击可能**整个落在两次轮询之间**，`down` 前后两次都是 False，
        #   只剩 `GetAsyncKeyState` 那个"自上次调用以来按过"的低位能兜，
        #   而 MSDN 明说这一位**不可靠** → 于是变成看运气。
        #   30ms 下按下的沿几乎不可能被跳过；代价只是一次 Win32 调用。
        self._gclk_down = False
        self._gclk_timer = QTimer()
        self._gclk_timer.timeout.connect(self._check_global_click)
        self._gclk_timer.start(30)

        self._setup_pet()
        self._reposition_windows()
        # ★ 1.9.1: 原来这里要通知聊天窗口"侧栏可以跟随显示了"（set_sidebar_ready）。
        #   侧栏已整个删除（用户方案 2），这步没了。
        # pet 进程自己 show()，无需主进程调用
        _log.info("形象进程已启动，后台逐步加载...")

        # ★ 托盘紧随形象，优先显示（仅依赖 QApplication，不依赖 LLM/TTS 等）
        try:
            self._setup_tray()
            _log.debug("系统托盘已优先加载")
        except Exception as e:
            _log.debug(f"托盘优先加载跳过: {e}")

        screen = QGuiApplication.primaryScreen()
        if screen:
            screen.geometryChanged.connect(self._reposition_windows)

        # ─── Phase 2: LLM + 会话 + TTS + 设置 (微步队列，每步间渲染 GIF 帧) ───
        self._init_steps = [
            # Phase 2: LLM + 会话
            ("chat-setup",          lambda: self._setup_chat()),
            ("vision-client",       lambda: setattr(self, 'vision', VisionClient())),
            ("summarizer-client",   lambda: setattr(self, 'summarizer', SummarizerClient())),
            ("session-create",      lambda: setattr(self, 'session', get_session())),
            ("session-setup",       lambda: self._setup_session()),
            ("session-history",     lambda: self.session.load_history()),
            # ★ 1.6.0: 载入历史后立刻重画聊天区（带 # 分割线），
            #   否则启动后界面上看不到任何分割线
            ("chat-reload",         lambda: self._reload_chat_view()),
            # ★ 1.9.1: 原来是刷新侧栏书签列表；现在刷新的是左上角那行
            #   「形象名 · tokens」（侧栏/书签已删）
            ("header-info",         lambda: self._update_header_info()),
            # Phase 3: TTS + 设置
            ("tts-create",          lambda: setattr(self, 'tts', get_tts())),
            ("tts-configure",       lambda: self.tts.configure(
                enabled=self.config.tts.enabled, character=self.config.tts.character,
                mode=self.config.tts.mode, provider=self.config.tts.provider)),
            ("tts-callbacks",       lambda: (
                setattr(self.tts, 'on_speak_start', self._on_tts_start),
                setattr(self.tts, 'on_speak_end', self._on_tts_end))),
            # ★ 1.9.0（你的加载优化要求）：设置窗口**不再启动时预建** ——
            #   它一口气要搭 9 个页面，主进程事件循环被占住照样卡；
            #   改成第一次点「设置」时现建（见 _show_settings）。
            # tray-setup 已提升至 Phase 1（形象后立即加载）
        ]
        self._init_step_idx = 0
        QTimer.singleShot(60, self._process_init_steps)

    def _process_init_steps(self):
        """每次执行一个微步，然后 yield 给事件循环渲染 GIF"""
        if self._init_step_idx >= len(self._init_steps):
            # 全部完成，释放闭包引用
            self._init_steps = None
            # 进入 Phase 4
            # ★ 1.6.0: GENIE 本地模型常驻约 4.5GB 内存 —— 默认**不在启动时预加载**，
            #   改为第一次播报时按需加载（可在设置里勾选"启动时预加载"改回旧行为）
            if (self.config.tts.enabled and self.config.tts.provider == "genie"
                    and getattr(self.config.tts, "genie_preload_on_start", False)):
                QTimer.singleShot(2000, self._preload_genie_bg)
            QTimer.singleShot(0, self._init_phase4)
            return
        name, fn = self._init_steps[self._init_step_idx]
        self._init_step_idx += 1
        try:
            fn()
        except Exception as e:
            _log.error(f"初始化步骤 [{name}] 失败: {e}", exc_info=True)
        # ★ 关键：用 0ms 让事件循环处理一帧 GIF 动画
        QTimer.singleShot(0, self._process_init_steps)

    def _init_phase4(self):
        """Phase 4: 监控 + 输入 + 画像 + 待机说话 (重IO组件，后台线程)"""
        _log.debug("Phase4: 监控/画像等重组件(后台线程)...")

        def _load_heavy():
            try:
                _log.debug("加载监控...")
                self.monitor = get_monitor()
                self._setup_monitor()

                _log.debug("加载输入监控...")
                self.input_monitor = get_input_monitor()
                self.input_monitor.set_callback(self._on_input_monitor_data)

                _log.debug("加载STT...")
                self.stt = get_stt()
                self.stt.on_transcription = self._on_stt_transcription
                self.stt.on_speech_start = self._on_stt_speech_start
                self.stt.on_speech_end = self._on_stt_speech_end

                _log.debug("加载用户画像...")
                self.user_profile = get_profile()
                self.long_term_memory = get_long_term_memory()
                self.profile_extractor = get_profile_extractor()
                # ★ 缓存 LTM 工具单例引用，避免后续重复 import
                self._ltm_tool = None
                try:
                    from tools.long_term_memory_tool import get_long_term_memory_tool
                    self._ltm_tool = get_long_term_memory_tool()
                except Exception:
                    pass

                _log.debug("加载待机说话...")
                self.idle_talker = get_idle_talker()
                self.idle_talker.on_idle_message = self.chat.idle_message_in.emit
                self.idle_talker.on_tts = lambda text: self.tts.speak(text)
                # ★ 1.9.0（你的要求）：主动说话走**同一个会话** —— 带完整上下文、
                #   还能自己调工具和用户互动，不再是"另起一个孤立 LLM 调用"。
                self.idle_talker.on_idle_trigger = self._on_idle_trigger_safe
                self.idle_talker.configure(
                    enabled=self.config.idle.enabled,
                    interval_minutes=self.config.idle.interval_minutes)

            except Exception as e:
                _log.error(f"重组件加载失败: {e}", exc_info=True)
            finally:
                # ★ 1.8.10: 必须放 finally —— Phase5（_init_finalize）是**唯一**
                #   会调 _apply_config_to_ui() / _update_summarize_timer() /
                #   启动 Pipeline 的地方。以前 emit 写在 try 末尾，只要上面
                #   任何一步抛异常（get_monitor/get_stt/get_profile/…），
                #   Phase5 就永远不执行 —— 界面全用默认值、总结定时器没起来，
                #   正是 1.8.5 刚修好的那个症状又回来了。
                #   回到主线程必须走 Signal：这里是 heavy-load 子线程，
                #   QTimer 不会触发。
                self._task_relay.heavy_ready.emit()

        threading.Thread(target=_load_heavy, daemon=True, name="heavy-load").start()

    def _init_finalize(self):
        """Phase 5: 最终收尾"""
        _log.debug("Phase5: 更新定时器...")
        # ★ 1.8.5: 这行进日志是"Phase5 到底跑没跑"的唯一凭据 ——
        #   之前它从没出现过，正是"重启后设置被刷回默认"的根因线索。
        _log.info("Phase5: 应用启动 UI 配置（颜色/头像/尺寸）...")
        try:
            self._update_summarize_timer()
        except Exception:
            pass
        # ★ 1.4.0: 启动时从配置文件应用 UI 设置（窗口尺寸/颜色）
        try:
            self._apply_config_to_ui()
        except Exception as e:
            _log.warning(f"应用启动 UI 配置失败: {e}")
        # 窗口尺寸应用后重新定位，防止 chat resize 后位置偏移
        self._reposition_windows()
        # ★ 1.9.1: 原来这里要放行侧栏跟随显示 —— 侧栏已删。
        # 启动数据清洗 Pipeline
        try:
            from src.utils.config import get_config
            if get_config().pipeline.enabled:
                self._pipeline = get_pipeline()
                self._pipeline.start()
                _log.info("数据清洗 Pipeline 已启动")
        except Exception:
            pass
        _log.info("主窗口初始化完成")

    def _preload_genie_bg(self):
        """后台预加载 GENIE worker（启动子进程异步加载模型）"""
        def _load():
            _log.info("GENIE 后台预加载开始...")
            try:
                self.tts.preload_genie()
            except Exception as e:
                _log.warning(f"GENIE 预加载失败: {e}")
        threading.Thread(target=_load, daemon=True, name="genie-preload").start()

    # ============ 设置 ============

    def _setup_pet(self):
        try:
            self.pet.behavior_changed.connect(self._reposition_windows)
            self.pet.pet_moved.connect(self._reposition_windows)
            self.pet.pet_clicked.connect(self._on_pet_clicked)
            self.pet.file_dropped.connect(self.chat.file_dropped_on_pet.emit)
            # ★ 1.6.0: 启动默认 GIF 播完一遍 → 形象线程切入待机轮播
            self.pet.gif_once_finished.connect(self.pet_mgr.on_startup_gif_finished)
            _log.debug(f"桌宠角色: {self.pet_mgr.current or self.config.pet.character}")
        except Exception as e:
            _log.error(f"桌宠初始化失败: {e}", exc_info=True)

    def _setup_chat(self):
        self.chat.send_message.connect(self._on_user_message)
        # ★ 1.9.2（R60，用户要求）：聊天窗口右上角那个按钮 → "加载上一轮历史"
        try:
            self.chat.load_more_cb = self._load_more_history
        except Exception:
            pass
        self.chat.abort_chat.connect(self._on_abort_chat)
        self.chat.abort_tool.connect(self._on_abort_tool)
        self.chat.image_recognize_request.connect(self._on_image_recognize)
        self.chat.file_dropped_on_pet.connect(self._on_file_dropped_on_pet)
        # ★ 1.8.0: 聊天窗口发来的非图片/非纯文本文件 → 转写/分析/抽取/元信息
        self.chat.file_upload_request.connect(self.handle_file_upload)

        # ★ 1.6.0: 话题管理信号 —— 一个角色一个窗口，只有「开启新话题 / 总结旧话题」
        #   ★ 1.9.1: 这两个按钮从侧栏挪到了聊天窗口**右上角按钮组最前面**，
        #     信号名不变，所以这里不用改接线。
        self.chat.new_topic_request.connect(self._on_new_topic)
        self.chat.summarize_topic_request.connect(self._on_summarize_topic)
        # ★ 1.9.1: 总结完成 → 刷新左上角那行（原来还兼着刷新侧栏书签）
        self.chat.summary_done_in.connect(self._update_header_info)
        # ★ 聊天窗口菜单（≡）：保证无托盘时也能访问 设置/退出/显示隐藏
        self.chat.on_settings = self._show_settings
        self.chat.on_quit = self._quit
        self.chat.on_toggle = self._toggle_visibility
        self.chat.on_external_open = self._on_external_open
        # ★ 鼠标拖拽缩放结束 → 持久化尺寸
        self.chat.resized.connect(self._on_chat_resized)
        # ★ 1.9.1: 侧栏/书签相关接线（topic_navigate / topic_delete /
        #   refresh_topic_list_request / sidebar_toggled）已随侧栏一起删除。

        # ★ 1.9.0: 剪贴板里的图片 → 直接发进聊天（不落盘）
        try:
            self.chat.clip_image_pasted.connect(self._on_clip_image_pasted)
        except Exception as e:
            _log.debug(f"剪贴板图片信号连接失败: {e}")

        # 注册 JMComic 文件就绪回调 → 自动弹出文件卡片
        # ★ 同时注册到 ToolManager 的 post-load hooks，
        #    确保 LAZY 卸载后重载时回调不会丢失
        from tools.jmcomic_tool import set_file_ready_callback
        # ★ 1.9.0: jmcomic 的 PDF 是在后台线程里合成完才回调的 ——
        #   改走 file_ready 中转（queued signal → 主线程），
        #   再由 _push_bot_attachment 统一「显示 + 落库」。
        #   （以前这里直接 chat.file_card_in.emit：只显示不落库 → 重启后 PDF 卡片消失）
        _jm_callback = lambda label, path, size: self._task_relay.file_ready.emit(
            label, str(path), size or "")
        set_file_ready_callback(_jm_callback)
        try:
            from src.core.tool_manager import get_tool_manager
            tm = get_tool_manager()
            tm.register_post_load_hook("jmcomic", lambda: set_file_ready_callback(_jm_callback))
        except Exception:
            pass

        # ★ 1.9.0: Pixiv 下载完成 → 同样走 file_ready 中转（后台线程 → 主线程 → 落库）
        try:
            from tools.pixiv_tool import set_file_ready_callback as _pv_set_cb
            _pv_callback = lambda label, path, size: self._task_relay.file_ready.emit(
                label, str(path), size or "")
            _pv_set_cb(_pv_callback)
            from src.core.tool_manager import get_tool_manager as _gtm
            _gtm().register_post_load_hook("pixiv", lambda: _pv_set_cb(_pv_callback))
        except Exception as e:
            _log.debug(f"Pixiv 回调注册失败: {e}")

        # ★ 1.9.1: ComfyUI 后台画图完成 → ① 发图（走 file_ready，显示+落库）
        #   ② 往上下文插一条系统提示（走 context_note，用户明确要求的那句）
        #   两条都可能在 worker 线程被调用 → 必须经 Signal 回主线程。
        #   同样注册 post_load_hook：LAZY 工具卸载重载后回调会丢。
        try:
            from tools import comfyui_tool as _cg
            _cg_file_cb = lambda label, path, size: self._task_relay.file_ready.emit(
                label, str(path), size or "")
            _cg_note_cb = lambda text: self._task_relay.context_note.emit(str(text))
            _cg.set_file_ready_callback(_cg_file_cb)
            _cg.set_context_note_callback(_cg_note_cb)
            from src.core.tool_manager import get_tool_manager as _gtm3
            _gtm3().register_post_load_hook(
                "comfyui",
                lambda: (_cg.set_file_ready_callback(_cg_file_cb),
                         _cg.set_context_note_callback(_cg_note_cb)))
            # ★ 1.9.1（用户："走其他tool一样的卸载规则。comfyUI和模型同加载同卸载，
            #   不用分开。"）→ 注册**卸载后钩子**：comfyui 工具空闲 30 秒被卸载时，
            #   顺带把 ComfyUI 关掉（模型与提交额度一起还回去）。
            #   ★ 不能 import tools.comfyui_tool 里的函数来做这件事 ——
            #     那个模块自己都要被卸载了；所以直接挂 src/core 里的实现。
            from src.core import comfyui_jobs as _cgj
            _gtm3().register_post_unload_hook("comfyui", _cgj.on_tool_unloaded)
            _log.debug("ComfyUI 画图回调已注册（出图 → 发图 + 上下文提示；卸载 → 关服务）")
        except Exception as e:
            _log.debug(f"ComfyUI 回调注册失败: {e}")

        # ★ 1.6.0: 终末地扫码登录 —— 二维码图片 + 登录结果推送到聊天窗口
        try:
            from tools import endfield_tool as _ef
            _ef.set_login_callback(self._on_endfield_login_done)
        except Exception as e:
            _log.debug(f"终末地登录回调注册失败: {e}")

        # ★ 1.9.1「指令直传」新功能：聊天窗口底部新增"指令直传"按钮
        #   → 用户点了 → 屏幕弹出 DirectDispatchWindow（懒单例）→
        #   选工具 + 填参数 + 点"发送" → 回投 dispatch_request(tool, action, params)
        #   → 同步调工具系统 → 结果以「<工具直传>」首行的形象气泡回到聊天。
        #   整个流程**不经过 LLM、不入上下文、不落库、不喂 LTM**（用户原话）。
        self.chat.direct_dispatch_request.connect(self._on_direct_dispatch_open)

    def _on_direct_dispatch_open(self):
        """聊天窗口底部"指令直传"按钮 → 弹出 DirectDispatchWindow（懒单例）。

        已经开着 → 唤起+置顶；没开 → 新建一个，挂上 dispatch_request 与
        window_closed 两个回调后 show()。子窗口里选中工具+点"发送"才会
        真正触发工具调用（详见 _on_direct_dispatch_exec）。
        """
        try:
            win = self._direct_dispatch_win
            if win is None:
                # ★ 修复：DirectDispatchWindow 第一个位置参数是 **tool_manager**，
                #   不是 parent。以前写 DirectDispatchWindow(self) 把 MainWindow
                #   塞了进去 → 内部调 self._tm.get_all_tools() 直接 AttributeError。
                from src.core.tool_manager import get_tool_manager as _gtm_dd
                #   parent 传 None：MainWindow 是普通 Python 类、不是 QWidget，
                #   塞给 QWidget(parent=...) 会直接 TypeError。
                win = DirectDispatchWindow(_gtm_dd())
                win.dispatch_request.connect(self._on_direct_dispatch_exec)
                win.window_closed.connect(self._on_direct_dispatch_closed)
                self._direct_dispatch_win = win
            if win.isVisible():
                win.raise_()
                win.activateWindow()
            else:
                win.show()
        except Exception as e:
            _log.warning(f"[指令直传] 打开窗口失败: {e}", exc_info=True)

    def _on_direct_dispatch_closed(self):
        """DirectDispatchWindow 被用户关闭 → 清掉引用，下次重新建。

        ★ 1.9.2（内存调研结论）：原来只把引用置 None，**没有 deleteLater()** ——
        那个窗口（1056 行、一堆控件）就一直挂在 Qt 对象树上不释放，
        每次开关一次就再新建一个。这里补上销毁。
        """
        try:
            win = self._direct_dispatch_win
            self._direct_dispatch_win = None
            if win is not None:
                try:
                    win.hide()
                except Exception:
                    pass
                win.deleteLater()
                _log.debug("[指令直传] 窗口已 deleteLater（不再每次开关都攒一个）")
        except Exception as e:
            _log.debug(f"[指令直传] 销毁窗口失败: {e}")

    def _on_direct_dispatch_exec(self, tool_name: str, action: str, params: dict):
        """DirectDispatchWindow 发来的"用户已点发送" → 同步跑工具 → 投递结果。

        ★ 用户硬性要求（1.9.1 末尾）：结果**只显示**，**不写 session.messages**、
          **不落库**（不走 db.chat.add）、**不喂 LTM**。
        · 文字部分走 chat.bot_text_in（形象气泡样式 + 首行 `<工具直传>` 标识）
        · 自动识别返回文本里的下载文件路径：图片 → bot_image_in；其他 → file_card_in
        """
        try:
            from src.core.tool_manager import get_tool_manager as _gtm
            tm = _gtm()
            head = f"<工具直传>{tool_name}"
            if action:
                head += f".{action}"
            # 同步跑（直传场景用户独占，自己玩，不并发）
            try:
                result = tm.execute_tool(tool_name, **params) or ""
            except Exception as e:
                self.chat.bot_text_in.emit(f"{head}\n[错误] 工具执行异常: {e}")
                _log.warning(f"[指令直传] {tool_name} 异常: {e}", exc_info=True)
                return
            # 自动发下载的文件
            paths = self._extract_paths_from_result(result)
            for p in paths:
                key = p.lower()
                if key in self._direct_dispatch_seen:
                    continue
                self._direct_dispatch_seen.add(key)
                # ★ 1.9.2（内存调研结论）：这个 set 原先只增不减 ——
                #   长时间闲聊会一直攒。给个上限（超了就整体清掉最省事，
                #   它只是"同一次直传别重复发同一张图"的短期去重）。
                if len(self._direct_dispatch_seen) > 200:
                    self._direct_dispatch_seen.clear()
                ext = Path(p).suffix.lower()
                if ext in self._IMG_EXTS:
                    try:
                        self.chat.bot_image_in.emit(p)
                    except Exception as e:
                        _log.debug(f"[指令直传] 图片预览失败 {p}: {e}")
                else:
                    try:
                        self.chat.file_card_in.emit(
                            f"📎 {Path(p).name}", p, "", True)
                    except Exception as e:
                        _log.debug(f"[指令直传] 文件卡片失败 {p}: {e}")
            text = (result or "").strip() or "(无返回内容)"
            self.chat.bot_text_in.emit(f"{head}\n{text}")
            _log.info(
                f"[指令直传] {tool_name}.{action or '-'} 已完成，"
                f"返回 {len(result)} 字 / 自动发文件 {len(paths)} 个")
        except Exception as e:
            _log.warning(f"[指令直传] 处理失败: {e}", exc_info=True)

    def _extract_paths_from_result(self, text: str) -> list[str]:
        """★ 1.9.1「指令直传」：从工具返回文本里抽**已存在**的绝对路径。

        只抓 win 盘符 (C:\\...、D:/...) 或 POSIX 根 (/...)；必须带
        图片/视频/音频/办公/压缩包扩展。命中后再用 is_file() 二次验证，
        避免误把工具叙述里出现的"C:\\Windows\\..."这种字面量当真路径。
        """
        if not text:
            return []
        exts = self._IMG_EXTS + (
            ".pdf", ".mp4", ".mp3", ".wav", ".flac", ".m4a",
            ".docx", ".xlsx", ".pptx", ".txt", ".zip", ".7z", ".rar",
            ".apk", ".exe",
        )
        pat = re.compile(
            r'([A-Za-z]:[\\/][^\s<>"\'`]+\.[A-Za-z0-9]+|'
            r'/[\w./\-+_]+\.[A-Za-z0-9]+)',
            re.IGNORECASE,
        )
        found: list[str] = []
        seen: set = set()
        for m in pat.finditer(text):
            cand = m.group(1).rstrip(",.;:)").rstrip()
            ext = os.path.splitext(cand)[1].lower()
            if ext not in exts:
                continue
            try:
                p = Path(cand)
                if not p.is_absolute() or not p.is_file():
                    continue
            except Exception:
                continue
            key = str(p).lower()
            if key in seen:
                continue
            seen.add(key)
            found.append(str(p))
        return found

    def _setup_session(self):
        self.session.on_text = self._on_stream_text_safe
        self.session.on_thinking = self._on_thinking_safe
        self.session.on_thinking_done = self._on_thinking_done_safe
        # ★ 1.9.0: 正文里的协议 JSON（{"gif":…,"meme":…}）—— 模型有时不放在思考里，
        #   这条回调保证"GIF 照样切、表情包照样发"，而那段 JSON 不会出现在聊天里。
        self.session.on_protocol = self._on_protocol_safe
        # ★ 1.9.0（你的提议）：工具返回里的 "gif" 提示 → 当场换个动作表情
        self.session.on_tool_call = self._on_tool_call_safe
        self.session.on_tool_result = self._on_tool_result_safe
        self.session.on_system_msg = self._on_system_msg_safe
        self.session.on_done = self._on_session_done_safe
        self.session.on_abort = self._on_session_abort_safe
        # ★ 话题变化回调
        # ★ 1.8.10: 必须走信号 —— Session.summarize_current_topic 会在
        #   "topic-summarize" 后台线程里调 on_topic_changed / on_input_blocked，
        #   而这两个最终都要碰 QWidget（刷新话题列表、setEnabled 输入框）。
        #   直接赋方法会在非 GUI 线程操作控件（Qt 未定义行为）。
        self.session.on_topic_changed = self._task_relay.topic_changed.emit
        # ★ 1.6.0: 角色窗口切换回调
        self.session.on_character_changed = self._on_character_window_changed
        # ★ 1.6.0: 多模态图片直传（按设置里的开关）
        # ★ 1.9.0: 保留调用只为兼容旧配置 —— 聊天里的图片**不再**走 image_url
        #   直传，统一交给 file_view 读成文字（main_window 已不再 attach_image）。
        try:
            self.session.set_multimodal(bool(getattr(self.config.vision, "multimodal", False)))
        except Exception:
            pass
        # ★ 1.6.0: 话题总结完成 → 刷新窗口状态行
        #   （回调来自后台线程，走信号回主线程；★ 1.9.1: 侧栏书签删了，改刷左上角那行）
        self.session.on_summary_done = self.chat.summary_done_in.emit
        self.session.on_input_blocked = self._task_relay.input_blocked.emit

    def _setup_monitor(self):
        self.monitor.set_callbacks(
            data_callback=self._on_monitor_data,
            summarize_callback=self._process_screen_image)

    def _check_global_click(self):
        """★ 第四轮（你的原话简化）：聊天窗口开着时，**只看这次点击是不是落在
        聊天窗口自己身上** —— 是就忽略，不是就收起。

        之前的版本（豁免形象 / 豁免 QMenu / 对话中不收）让逻辑越来越复杂，
        真机上"一闪又回来 / 关不掉"反复翻车。按你的判断：宁可让看门狗只做
        一件事，剩下的交给 _on_pet_clicked 去翻标志。
        """
        try:
            chat = getattr(self, "chat", None)
            if chat is None or not chat.isVisible():
                self._gclk_down = False
                return
            if os.name != "nt":
                return
            import ctypes
            st = ctypes.windll.user32.GetAsyncKeyState(0x01)   # VK_LBUTTON
            down = bool(st & 0x8000)
            pressed = bool(st & 0x1)
            clicked = pressed or (down and not self._gclk_down)
            self._gclk_down = down
            if not clicked:
                return
            try:
                from PySide6.QtGui import QCursor
                pos = QCursor.pos()
            except Exception:
                return
            # ★ 唯一判断：点是不是在聊天窗口矩形里。在 → 忽略；不在 → 收起。
            try:
                if chat.frameGeometry().contains(pos):
                    return
            except Exception:
                return
            # ★ 1.9.1 修复（真机 2026-09-18 17:27/17:31 两段日志实锤）：
            #   开着「指令直传」窗口时点形象打不开聊天窗口。根因是竞速：
            #   直传窗口让**主进程**成了前台 → pet_clicked 的 IPC 回得飞快，
            #   chat.show() 落在鼠标键**还没抬起**的瞬间；看门狗下一个 30ms
            #   tick 看到 down=True 而 _gclk_down 还是聊天窗隐藏期间留下的
            #   旧值 False → 误判"点在聊天窗外" → 刚弹出的窗口又被收掉，
            #   并且 _chat_open_by_pet 被清成 False → 再点形象还是走"收起"
            #   分支 → 永远打不开。
            #   修法①：刚由点形象打开的 0.6 秒内，看门狗看到的点击一律视为
            #   "就是那一下打开点击"，不收（和已有的 _last_watchdog_hide
            #   0.35 秒窗是镜像关系：那边防"关了又弹开"，这边防"开了又被关"）。
            import time as _t
            if _t.monotonic() - getattr(self, "_last_pet_open", 0.0) < 0.6:
                return
            #   修法②：点在「指令直传」窗口里也不收 —— 它是聊天窗口的伴生
            #   窗口（结果全投回聊天），开着它操作它 = 还在"聊天这摊事"里。
            _dd = getattr(self, "_direct_dispatch_win", None)
            try:
                if _dd is not None and _dd.isVisible() and \
                        _dd.frameGeometry().contains(pos):
                    return
            except Exception:
                pass
            self._chat_open_by_pet = False
            # ★ 接管修正（2026-09-17 22:15）：记下"看门狗刚关过"的时刻。
            #   根因：点形象那一次点击会被**两条路各处理一次** ——
            #     ① 看门狗（100ms 轮询）先看到"按下 + 光标不在聊天窗矩形里" → 关窗；
            #     ② 形象子进程的 pet_clicked 走 IPC 晚一点才到 → _on_pet_clicked
            #        又翻一次标志 → 把刚关掉的窗口**重新打开**。
            #   谁先到决定成败，所以现象是"关闭成功的概率增加了"。
            #   修法：紧紧跟着的那次 pet_clicked 直接忽略（它就是同一下点击）。
            self._last_watchdog_hide = _t.monotonic()
            chat.hide()
            if getattr(self, "tool_log", None) is not None:
                self.tool_log.hide()
            _log.debug("[聊天窗口] 点击落在聊天窗外 → 收起")
        except Exception as e:
            _log.debug(f"全局点击看门狗异常: {e}")

    def _setup_tray(self):
        self._tray = TrayManager(self.app)
        self._tray.on_toggle = self._toggle_visibility
        # ★ 接管（2026-09-17 22:30，你的要求）：托盘拆成「显示桌宠」「隐藏桌宠」
        #   两个明确项 —— 显示就强制显示，隐藏就强制隐藏，不再猜当前状态。
        self._tray.on_show = self.show_pet_force
        self._tray.on_hide = self.hide_pet_force
        self._tray.on_settings = self._show_settings
        self._tray.on_quit = self._quit
        self._tray.show()
        # ★ 1.5.0: 启动即用当前形象图标（角色目录 ico.ico / 头像.jpg）
        try:
            self._tray.update_character_icon(self.config.pet.character)
        except Exception:
            pass
        _log.debug("系统托盘就绪")

    # ============ 位置计算 ============

    def _reposition_windows(self):
        try:
            pet_geo = self.pet.geometry()
            screen = QGuiApplication.screenAt(QPoint(pet_geo.x(), pet_geo.y()))
            if screen is None:
                screen = QGuiApplication.primaryScreen()
            if screen is None:
                return

            screen_geo = screen.geometry()
            pet_x, pet_y = pet_geo.x(), pet_geo.y()
            pet_w, pet_h = pet_geo.width(), pet_geo.height()
            chat_w, chat_h = self.chat.width(), self.chat.height()

            # ★ 使用配置的距离
            distance = getattr(self.config.ui, "chat_distance", 10)

            chat_on_left = True
            left_space = pet_x - screen_geo.x()
            right_space = screen_geo.right() - (pet_x + pet_w)

            if left_space < chat_w + distance + 10 and right_space >= chat_w + distance + 10:
                chat_on_left = False

            if chat_on_left:
                chat_x = pet_x - chat_w - distance
            else:
                chat_x = pet_x + pet_w + distance

            chat_y = pet_y + (pet_h - chat_h) // 2
            chat_x = max(screen_geo.x(), min(chat_x, screen_geo.right() - chat_w))
            chat_y = max(screen_geo.y(), min(chat_y, screen_geo.bottom() - chat_h))

            self.chat.move(chat_x, chat_y)
            self.tool_log.move(chat_x, chat_y + chat_h + 10)
        except Exception as e:
            _log.error(f"窗口定位失败: {e}")

    def _on_behavior_changed(self, behavior: str):
        self._reposition_windows()

    def _on_pet_clicked(self):
        """★ 单击桌宠 → 翻一下"用户想让聊天窗口开着"的标志，按这个标志处理。

        第四轮（你的简化要求）：
          · 不再做"chat.isVisible() 判断"——它会被「失焦自动隐藏」干扰，
            让逻辑变成"关不掉 / 一开又没"的死循环。
          · 不再做 0.6 秒防抖 —— 你实测它把"快速连点"全吃了，连关都关不掉。
          · 不再调 `suppress_focus_hide` —— 你说"关闭时不要再延时聚焦了"。
            打开时仍调一次（短窗 0.4s），避免 show() 后被 WindowDeactivate 立刻
            收回（这是形象在子进程、前台权不在主进程那一秒内的真实情形）。
          · 只用一只 80ms 的极短去重（子进程事件可能重复，误触 ≠ 误开关）。
        """
        import time as _t
        _now = _t.monotonic()
        if _now - getattr(self, "_last_pet_toggle", 0.0) < 0.08:
            return                                   # 同一次点击的重复信号
        # ★ 接管修正（2026-09-17 22:15）：如果**刚刚**是看门狗把窗口收起的
        #   （同一下点击，只是 IPC 晚到了），这次就别再翻标志 —— 否则会把刚
        #   关掉的窗口又打开，表现成"点形象关不掉 / 一闪又回来"。
        if _now - getattr(self, "_last_watchdog_hide", 0.0) < 0.35:
            _log.debug("[Pet] 这次点击已由看门狗收起过，忽略 pet_clicked（避免又弹开）")
            self._last_pet_toggle = _now
            return
        self._last_pet_toggle = _now

        # 翻标志
        self._chat_open_by_pet = not getattr(self, "_chat_open_by_pet", False)

        if self._chat_open_by_pet:
            # ── 打开 ──
            # ★ 1.9.1 修复：记下"刚由点形象打开"的时刻，看门狗据此放过
            #   这一下点击本身（详见 _check_global_click 里的说明）。
            self._last_pet_open = _now
            try:
                self.chat.suppress_focus_hide(0.4)  # 0.4s 短窗，挡住 show 后的
            except Exception:                        # WindowDeactivate（已在子进程
                pass                                 # 里注释过，不再赘述）
            self._reposition_windows()
            self.chat.show()
            self.chat.raise_()
            self.chat.activateWindow()
            self.chat.set_hide_on_focus_out(True)
            self._input_field_focus()
            _log.debug("[Pet] 点形象 → 打开聊天窗口")
        else:
            # ── 收起 —— 不调任何延时 / 抑制 —— 你说的"关闭时只看是不是聊天窗口"
            self.chat.hide()
            if getattr(self, "tool_log", None) is not None:
                self.tool_log.hide()
            _log.debug("[Pet] 再点形象 → 收起聊天窗口")

    def _input_field_focus(self):
        """★ 1.9.0: 把键盘焦点交给输入框 —— 点一下形象就能直接打字。

        根因（W1 二次修复，第一版没修好）：不是"setFocus 太早"，而是
        **前台权不在主进程**。形象跑在独立子进程里，你点形象时前台权在
        子进程，主进程 activateWindow() 被 Windows 静默拒绝 → 窗口只有
        Qt 逻辑焦点（光标在闪），键盘输入进不来 —— 正是"光标到位了但
        还是打不了字"。详见 _force_foreground 的说明。

        线程说明：本回调**一定在主线程** —— 子进程模式下 pet_clicked 由
        PetProxy 的 queued signal 投递回主线程，直接渲染模式下同线程。
        所以这里用 QTimer.singleShot 是合法的，与 1.8.2/1.8.5 那个
        「子线程里 QTimer 永不触发」的坑不是一回事（自测里有线程断言守着）。
        """
        try:
            field = getattr(self.chat, "_input_field", None)
            if field is None:
                return
            self.chat.raise_()
            self.chat.activateWindow()
            self._force_foreground(self.chat)
            field.setFocus(Qt.OtherFocusReason)
            for _delay in (0, 80, 220):
                QTimer.singleShot(_delay, lambda f=field: self._focus_input_now(f))
        except Exception as e:
            _log.debug(f"聚焦输入框失败: {e}")

    @staticmethod
    def _force_foreground(win) -> bool:
        """★ 1.9.0: 强制把窗口拉到系统前台，返回是否成功（仅用于诊断日志）。

        为什么需要它：Qt 的 activateWindow() 在「调用进程不是前台进程、
        也没接收最后一次输入」时会被 Windows 静默拒绝。形象跑在**独立子
        进程**里（日志 [PetProxy] 桌宠子进程已启动），你点形象时前台权在
        子进程 —— 正好命中这个情况：聊天窗口只有 Qt 逻辑焦点（光标在闪），
        键盘输入却进不来。
        标准绕法：先把自己 attach 到当前前台线程的输入队列，再
        SetForegroundWindow，然后 detach。
        """
        if win is None:
            return False
        if os.name != "nt":
            try:
                win.raise_()
                win.activateWindow()
                return True
            except Exception:
                return False
        try:
            import ctypes
            user32 = ctypes.windll.user32
            kernel32 = ctypes.windll.kernel32
            hwnd = int(win.winId())
            if user32.GetForegroundWindow() == hwnd:
                return True
            fg = user32.GetForegroundWindow()
            tid_fg = user32.GetWindowThreadProcessId(fg, None) if fg else 0
            tid_me = kernel32.GetCurrentThreadId()
            attached = False
            if tid_fg and tid_fg != tid_me:
                attached = bool(user32.AttachThreadInput(tid_fg, tid_me, True))
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
            user32.SetFocus(hwnd)
            if attached:
                user32.AttachThreadInput(tid_fg, tid_me, False)
            return user32.GetForegroundWindow() == hwnd
        except Exception as e:
            _log.debug(f"强制前台失败: {e}")
            return False

    def _focus_input_now(self, field):
        """★ 1.9.0: 延时抢焦点 —— 先把窗口推到系统前台，再交键盘焦点给输入框。
        只在输入框仍可见且未被禁用时动手（「总结旧话题」期间会被临时禁用）。"""
        try:
            if field is None or not field.isEnabled() or not field.isVisible():
                return
            win = field.window()
            win.raise_()
            win.activateWindow()
            ok = self._force_foreground(win)
            field.setFocus(Qt.OtherFocusReason)
            _log.debug(f"[Focus] 延时聚焦: foreground_ok={ok} "
                       f"isActiveWindow={win.isActiveWindow()} "
                       f"fieldHasFocus={field.hasFocus()}")
        except Exception:
            pass

    # ============ 聊天处理 ============

    def _chat_rounds_path(self):
        """★ 1.9.2（R60）：聊天窗口显示轮数的存盘位置（data/ui_state.json）。"""
        return Path(self.config.config_path).parent / "data" / "ui_state.json"

    def _load_chat_rounds(self) -> int:
        try:
            import json as _json
            p = self._chat_rounds_path()
            if p.is_file():
                return max(2, int(_json.loads(p.read_text(encoding="utf-8"))
                                  .get("chat_rounds", 2)))
        except Exception as e:
            _log.debug(f"[聊天历史] 读显示轮数失败（按 2 轮）: {e}")
        return 2

    def _save_chat_rounds(self, n: int):
        try:
            import json as _json
            p = self._chat_rounds_path()
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(_json.dumps({"chat_rounds": int(n)}, ensure_ascii=False),
                         encoding="utf-8")
        except Exception as e:
            _log.debug(f"[聊天历史] 存显示轮数失败: {e}")

    def _load_more_history(self):
        """★ 1.9.2（R60，用户要求）：聊天窗口右上角按钮 —— **按一次加载上一轮**。"""
        try:
            self._chat_load_rounds = int(getattr(self, "_chat_load_rounds", 2)) + 1
            self._save_chat_rounds(self._chat_load_rounds)
            _log.info(f"[聊天历史] 加载上一轮（现在显示 {self._chat_load_rounds} 轮）")
            self._reload_chat_view(keep_position=True)
        except Exception as e:
            _log.warning(f"[聊天历史] 加载上一轮失败: {e}")

    def _on_user_message(self, text: str):
        _log.info(f"收到用户消息: {text}")
        # ★ 1.9.1: 标记"一轮对话正在进行" —— 后台异步来的东西（比如画好的图）
        #   要靠它判断能不能直接往 session.messages 里插消息（见 _on_context_note）
        self._turn_active = True
        # ★ 1.9.0: 新一轮开始 → 清空附件去重集合（同一轮内同一文件只推一次）
        try:
            self._attachment_seen.clear()
        except Exception:
            pass
        # ★ 1.9.0: 同时重置 meme 的"本轮已选过"标记（一轮只允许发一次）
        try:
            from src.core import meme_bus as _mb2
            _mb2.reset_turn()
        except Exception:
            pass
        # ★ 1.9.2（R55，用户新设计）：**每次用户发言都卸载前面的聊天记录** ——
        #   话题不断、上下文不断（session.messages 一个字不动），只是让每一次对话
        #   都是干净的聊天界面。触发点就是"用户发送消息"这一下（真实发送链路），
        #   所以放在这里，而不是 chat_widget._append_message（所有路径共用，
        #   塞那里会把渲染历史和自测的逐条追加一起清掉，实测打挂 7 条断言）。
        #   ★ 位置注意：**必须放在上面那几行之后** —— a190 有一条按源码位置断言的
        #     "新一轮开始时清空附件去重集合"，插在它前面会把那条打挂（踩过一次）。
        #   ★ 时机注意：ChatWidget 是先渲染这条用户消息、再发 send_message，
        #     所以清屏必须"保留最后一条"（clear_before_last_user_message）。
        try:
            self.chat.clear_before_last_user_message()
        except Exception as _e:
            _log.debug(f"[分段加载] 发言清屏跳过: {_e}")
        self.pet_mgr.on_user_message()
        self.chat.append_system_msg("[正在思考...]")
        self.chat.reset_stream()
        self._last_assistant_text = ""
        # ★ 1.8.10: idle_talker / profile_extractor / session 都在 Phase4 的
        #   后台线程里创建，而 chat 的 send_message 信号在 Phase2 就接上了。
        #   用户在 Phase4 完成前抢先打字时，这里以前会 AttributeError
        #   → 整条消息在 self.session.send_message(text) 之前就抛掉了（消息丢失）。
        #   全部改成软访问。
        _idle = getattr(self, "idle_talker", None)
        if _idle is not None:
            _idle.on_user_message()  # 重置待机冷却
        self.chat.set_hide_on_focus_out(False)  # 对话进行中不自动隐藏
        # ★ 注入用户画像上下文
        self._inject_user_profile_context()
        # ★ 从用户消息中提取画像（LTM 禁用时跳过）
        _extractor = getattr(self, "profile_extractor", None)
        if _extractor is not None and self._is_profile_enabled():
            _extractor.extract_async(text)
        self.session.send_message(text)

    def _on_abort_chat(self):
        self.session.abort_chat()
        self.chat.append_system_msg("[对话已终止]")
        self.chat.set_hide_on_focus_out(True)

    def _on_abort_tool(self):
        self.session.abort_tool()
        self.chat.append_system_msg("[工具调用已终止]")

    # ============ 话题管理（★ 1.6.0: 一个角色一个窗口 / ★ 1.9.1 按你新定义重做） ============

    def _on_new_topic(self):
        """★ 1.9.1: 「开启新话题」—— 按你的定义重做。

        你的原话："清空界面，清空上下文，上一轮上下文入库，创建新的上下文db文件。"

        实现说明（有一条和你的字面说法不同，见最后）：
          · **清空界面** —— `chat.clear_chat()`（1.6.0 那版是"保留旧内容只加分割线"，
            你要的是清空，这里改了）。
          · **清空上下文** —— `session.start_new_topic()` 会把内存里的 messages
            换成新话题的空记录（旧消息不丢，它们在写入时就落库了）。
          · **上一轮上下文入库** —— 本来就是**逐条实时入库**的
            （`session` 每收/发一条就 `add_chat_history`），所以这里无需补写；
            旧话题的行仍在 `chat_records` 里、`status='active'`。
          · **创建新的"上下文 db"** —— 见下面那段说明，本项目是
            **一个 `chat_records.db` + `topics` 表**，新话题 = 新增一行 topic，
            不是新建一个 .db 文件。
        """
        self.session.start_new_topic(summarize=False)
        self.chat.clear_chat()
        self.chat.append_topic_divider("新话题", topic_id="CUR")
        self._update_header_info()

    def _on_summarize_topic(self):
        """★ 1.9.1: 「总结旧话题」—— 按你的定义。

        你的原话："先总结本db文件的所有聊天记录，总结内容也计入该db，然后是
        和开启新话题一样的流程，最后，把总结内容送到上下文。"

        对应到代码（`Session.summarize_current_topic(then_new_topic=True)`）：
          ① 总结当前话题的全部消息（分批喂给摘要模型，产出 summary）；
          ② 总结**写进同一条 topic 记录**（`archive_topic(id, summary)`）→
             满足"总结内容也计入该 db"；
          ③ 转成新话题（= 开启新话题那一套）；
          ④ 新话题注入上一段总结（`get_last_archived_summary`）→ 上下文延续。
        所以这里做的事和 `_on_new_topic` 只差一个"要不要总结"，界面处理一样。
        """
        self.session.summarize_current_topic(then_new_topic=True, reason="旧")
        # 总结是后台线程跑的（topic-summarize），界面清空/刷新由
        # session.on_summary_done → chat.summary_done_in → _update_header_info
        # 以及 on_topic_changed 触发；这里不再同步清屏，免得和后台抢状态。
        self._update_header_info()

    def _update_header_info(self):
        """★ 1.9.1: 刷新聊天窗口左上角那行「形象名 · tokens」。

        原来是 `_refresh_topic_list()`（渲染侧栏书签列表）。书签按你的要求删了，
        这个函数只剩"把当前话题的状态喂给标题栏"这一件事。

        ★ 关于"创建新的上下文 db 文件"（你 00:15 那条的原话）：
          本项目从 1.6.0 起就是 **一个 `data/DBs/chat_records.db` + 一张 topics 表**，
          一个话题 = 表里的一行（`topic_id`），**不是一话题一个 .db 文件**。
          我没有改成"一话题一文件"：那会牵动长期记忆 / 行为监控 / LTM 总结
          （它们都按 topic_id 关联），风险大、收益小，而**用户可见行为是一样的**
          （新话题 = 干净的空上下文）。
          如果你确实想要磁盘上一话题一个文件，跟我说，我再改。
        """
        try:
            char = getattr(self.session, "character", "") or self.config.pet.character
            info = self.session.get_current_topic_info()
            self.chat.set_conversation_info(
                character=char,
                topic_title=info.get("title", ""),
                message_count=info.get("message_count", 0),
                token_count=info.get("token_count", 0),
            )
        except Exception as e:
            _log.warning(f"刷新窗口状态行失败: {e}")

    def _on_character_window_changed(self, name: str):
        """★ 1.6.0: 切换角色 → 切换该角色自己的连续对话窗口"""
        _log.info(f"[窗口] 切换到角色「{name}」的对话窗口")
        if getattr(self, "session", None) is None:
            return
        self._reload_chat_view()
        self._update_header_info()

    def _reload_chat_view(self, keep_position: bool = False):
        """★ 1.6.0: 重画聊天区 = 该角色的旧话题历史 + 当前话题消息

        每段话题前插一条 `####` 分割线。
        ★ 1.9.1: 这里原来还写隐形标记 `⟦T<id>⟧` 供侧栏书签跳转定位；
        书签已按你的要求删除，标记与 `_topic_marks` 一并去掉。
        ★ 1.9.2（用户 R41）：**分段式加载** —— 默认只渲染最后 `_chat_load_rounds` 轮
        （1 轮 = 一用户 + 一助手），上面更早的先不渲染；用户翻到最顶还继续往上翻时，
        `_on_chat_scrolled_top` 会每次再多放 2 轮进来。keep_position 用来在"多放几轮"
        之后把视线留在原来那条消息上（不然会跳到别处）。
        """
        try:
            # 记住当前滚动位置（多放几轮之后要按"离底部多远"还原视线）
            try:
                _sb = self.chat._chat_display.verticalScrollBar()
                _old_gap = max(0, _sb.maximum() - _sb.value())
            except Exception:
                _old_gap = 0
            # 懒连接"翻到顶→加载更多"（只连一次，避免重复触发）
            if not getattr(self, "_chat_scroll_hooked", False):
                self._chat_scroll_hooked = True
                try:
                    self.chat._chat_display.verticalScrollBar().valueChanged.connect(
                        self._on_chat_scrolled_top)
                except Exception as _e:
                    _log.debug(f"[聊天分段加载] 连接滚动信号失败: {_e}")
            self.chat.clear_chat()
            char = getattr(self.session, "character", "") or self.config.pet.character
            topic_id = self.session.topic_id
            # ★ 1.9.2（R52）：用户改了设计 —— 不再靠"往上翻触发加载"，
            #   所以这里**不再连接滚动信号**（用户反馈"往上翻没有触发加载"；
            #   与其去修一个不稳的滚动判定，不如按新设计：每次发言清屏）。
            # ① 旧话题历史：按话题分组，每段前插分割线
            # ★ 1.9.2（R52）：用户要求"**加载历史聊天记录的方式不变**"，
            #   所以这里恢复成原来的无条件渲染（分段改由"每次用户发言清屏"实现）。
            #   同时打开"历史渲染中"开关：这期间 _append_message 绝不清屏，
            #   否则一打开话题就被清空了。
            # ★ 1.9.2（R60，用户要求）：聊天窗口"显示到第几轮"要**跨重启保持**
            #   （真机反馈：重启后不应该直接把全部上下文都摊开）。
            try:
                if getattr(self, "_chat_load_rounds", None) is None:
                    self._chat_load_rounds = self._load_chat_rounds()
            except Exception as _e:
                self._chat_load_rounds = 2
                _log.debug(f"[聊天历史] 轮数初始化失败，按 2 轮: {_e}")
            # ★ 1.9.2（R61）：预算对**整个视图**（归档 + 当前）生效 ——
            #   一次只多放一轮，不再"一按就全灌进来"。
            #   ★ 1.9.2（R61）：**轮 = 以"用户消息"为界**，不是"两条消息" ——
            #     一次发言后面还会跟系统提示/工具记录等，所以"轮数×2 条"根本对不上。
            _rounds0 = max(2, int(getattr(self, "_chat_load_rounds", 2)))
            _cur_keep = []
            try:
                with self.session._lock:
                    _cur_all = [dict(m) for m in self.session.messages]
                _cur_keep = [m for m in _cur_all
                             if (m.get("content") or "").strip()
                             and m.get("role") in ("user", "assistant", "system")]
            except Exception as _e:
                _log.debug(f"[聊天历史] 预读当前话题失败: {_e}")
            _cur_shown = len(_cur_keep)
            _u_idx0 = [i for i, m in enumerate(_cur_keep) if m.get("role") == "user"]
            if len(_u_idx0) > _rounds0:
                _cur_shown = len(_cur_keep) - _u_idx0[-_rounds0]
            _slots = max(0, _rounds0 * 2 - _cur_shown)   # 归档话题只吃剩下的名额
            self.chat._history_rendering = True
            try:
                # ★ 1.9.2（R61，用户："点加载历史把该话题全部上下文都加载进来了"）：
                #   只有当"当前话题还没占满预算"时，才用**剩下的名额**回放归档话题，
                #   而且只回放那么多条。之前写成"N>2 就整段回放归档（最多120条）"，
                #   所以按一次按钮就把整个上下文摊开了 —— 这是那条反馈的根因。
                archived = ([] if _slots <= 0 else
                            [t for t in self.db.list_character_topics(char, limit=50)
                             if t.get("status") == "archived"])
                archived = list(reversed(archived))[-2:]   # 最近 2 段，避免界面过长
                total = 0
                for t in archived:
                    msgs = [m for m in self.db.get_topic_messages(t.get("id"))
                            if m.get("role") in ("user", "assistant")
                            and (m.get("content") or "").strip()]
                    if not msgs:
                        continue
                    first = (msgs[0].get("content") or "").strip().replace("\n", " ")
                    self.chat.append_topic_divider(first, topic_id=t.get("id"))
                    for m in msgs:
                        content = m.get("content") or ""
                        if m.get("role") == "user":
                            self.chat._append_message("user", content)
                        else:
                            self.chat._append_message("assistant", content)
                        total += 1
                        if total >= _slots:
                            break
                    if total >= _slots:
                        break
            except Exception as e:
                _log.debug(f"渲染旧话题历史失败: {e}")
            # ② 当前话题
            self.chat.append_topic_divider("当前话题", topic_id="CUR")
            with self.session._lock:
                msgs = [dict(m) for m in self.session.messages]
            # ★ 1.9.2（R60，用户要求）：只显示最后 N 轮（1 轮 = 一用户 + 一助手），
            #   按一次"↑ 历史"按钮就 +1 轮。N 会存盘、重启后沿用（= 上一次退出的状态）。
            _rounds = max(2, int(getattr(self, "_chat_load_rounds", 2)))
            _keep = [m for m in msgs
                     if (m.get("content") or "").strip()
                     and m.get("role") in ("user", "assistant", "system")]
            #   ★ 只有真的超了才裁；`_keep` 为空时**绝不动** msgs ——
            #     否则一旦消息结构不符合预期（比如自测里的桩对象），
            #     整个视图会被裁成空的（我把 topic_buttons 打挂过就是这个）。
            #   ★ R61：**按用户消息为界**取"最后 N 轮"，一次发言算一轮，
            #     中间夹着的系统提示/工具记录都跟着它那一轮一起显示。
            if _keep:
                _u_idx = [j for j, m in enumerate(_keep) if m.get("role") == "user"]
                if len(_u_idx) > _rounds:
                    _start = _u_idx[-_rounds]
                    _log.debug(f"[聊天历史] 只显示最后 {_rounds} 轮"
                               f"（从倒数第 {_rounds} 条用户消息起，共 {len(_keep)} 条）")
                    msgs = _keep[_start:]
                else:
                    msgs = _keep
            for m in msgs:
                role = m.get("role")
                content = m.get("content") or ""
                if not content:
                    continue
                if role == "user":
                    self.chat._append_message("user", content)
                elif role == "assistant":
                    self.chat._append_message("assistant", content)
                elif role == "system":
                    self.chat.append_system_msg(content)
            self.chat._history_rendering = False
            self.chat.on_stream_done()
        except Exception as e:
            try:
                self.chat._history_rendering = False
            except Exception:
                pass
            _log.warning(f"重载聊天视图失败: {e}")

    def _on_chat_scrolled_top(self, value: int):
        """★ 1.9.2（用户 R41）：用户翻到**最顶**、还继续往上翻 → 每次多放 2 轮进来。

        `_chat_loading_more` 是防重入的：还原滚动位置时会再触发一次 valueChanged，
        没有这个闸就会一路把所有历史都加载完（那就白做分段了）。
        """
        try:
            if getattr(self, "_chat_loading_more", False):
                return
            if value > 0 or not getattr(self, "_chat_hidden_count", 0):
                return
            self._chat_loading_more = True
            try:
                self._chat_load_rounds = int(getattr(self, "_chat_load_rounds", 2)) + 2
                _log.info(f"[聊天分段加载] 再放 2 轮进来（当前上限 "
                          f"{self._chat_load_rounds} 轮）")
                self._reload_chat_view(keep_position=True)
            finally:
                self._chat_loading_more = False
        except Exception as e:
            _log.debug(f"[聊天分段加载] 失败: {e}")

    # ============ 拖拽文件到形象 ============

    def _on_file_dropped_on_pet(self, file_path: str):
        """文件拖拽到形象 → 自动显示聊天窗口并处理"""
        _log.info(f"文件拖拽到形象: {file_path}")
        self._reposition_windows()
        self.chat.show()
        self.chat.raise_()
        self.chat.activateWindow()
        self.chat.set_hide_on_focus_out(False)
        self.chat.handle_external_drop(file_path)

    def _on_stream_text_safe(self, text: str):
        """后台线程回调 -> 通过信号安全投递到主线程"""
        self.pet_mgr.on_reply_start()
        self.chat.stream_text_in.emit(text)
        self._last_assistant_text += text

    def _on_thinking_safe(self, text: str):
        """★ 1.5.0: LLM 思考流增量 → 形象线程（思考 GIF）

        ★ 1.9.2（用户任务 4）：同时喂给聊天窗口里那个「思考 / 系统信息」子窗口。
        走 Signal（跨线程安全），面板高度自适应、5 排字封顶。
        """
        self.pet_mgr.on_thinking_delta(text)
        try:
            self.chat.think_delta_in.emit(text)
        except Exception as e:
            _log.debug(f"[思考面板] 写入增量失败: {e}")

    def _on_thinking_done_safe(self):
        """★ 1.5.0: 思考结束 → 形象线程立刻按思考内容切换 GIF

        ★ 1.9.2：顺便把思考面板**强制渲染一次**收尾 —— 流式期间是节拍器
        合并渲染的（每 ~80ms 一次），最后可能有不到一拍的碎片还没上屏。
        """
        self.pet_mgr.on_thinking_done()
        try:
            self.chat.think_flush()
        except Exception as e:
            _log.debug(f"[思考面板] 收尾渲染失败: {e}")

    def _on_protocol_safe(self, data: dict):
        """★ 1.9.0: 正文里的协议 JSON（`session.on_protocol`）→ 交给形象线程。

        注意：这里**只**负责切 GIF / 记住表情包；表情包的发送仍然统一由
        `_on_session_done_safe` 在文字回复结束后做（保证"分两条、表情包最后"）。
        """
        try:
            self.pet_mgr.apply_protocol(data)
        except Exception as e:
            _log.debug(f"[协议] 应用失败: {e}")

    # ★ 1.9.1: `_on_tool_gif_safe` 删掉了 —— 工具返回不再触发形象切换
    #   （你："决定返回之前的方式，每一次思考的最后给 json"）。
    #   形象切换只剩两个来源：待机计时器 + 思考 JSON 拦截器，都走
    #   `pet_mgr.apply_switch()`。

    def _on_tool_call_safe(self, tool_name: str, args: dict):
        _log.info(f"工具调用: {tool_name}({str(args)[:80]})")
        self.pet_mgr.on_tool_call()
        # ★ 重置流式状态，防止下一轮 LLM 回复时覆盖掉工具日志
        self.chat.reset_stream_in.emit()
        arg_str = str(args)
        arg_short = arg_str[:120] + ("..." if len(arg_str) > 120 else "")
        # ★ 跨线程 Signal 发射 → 主线程执行 append_system_msg （不可用 QTimer，非 Qt 线程无事件循环）
        # ★ 1.9.2：append_system_msg 现在**改投思考面板**（用户要求删掉灰底白字气泡），
        #   所以这里不再额外 emit 一次 think_in —— 那样面板里会出现两条。
        self.chat.system_msg_in.emit(f"[调用: {tool_name}({arg_short})]")

    def _on_tool_display(self, tool_name: str, arg_short: str):
        _log.info(f"工具调用: {tool_name}({arg_short[:80]})")
        self.tool_log.add_log(f"调用: {tool_name}({arg_short[:100]})", "tool_call")
        self.chat.append_system_msg(f"[调用: {tool_name}({arg_short})]")
        self._reposition_windows()

    def _on_tool_result_safe(self, tool_name: str, result: str):
        short = result[:120] + ("..." if len(result) > 120 else "")
        # ★ 跨线程 Signal 发射 → 主线程执行 append_system_msg（同样会进面板）
        self.chat.system_msg_in.emit(f"[{tool_name} 返回]: {short}")

        # ★ file_ops 的 send_file 操作 — 自动显示文件卡片（优先处理，避免双重触发）
        if tool_name == "file_ops":
            self._handle_file_ops_result(result)
        elif tool_name == "endfield":
            # ★ 1.6.0: 终末地扫码登录返回的二维码 → 聊天窗口显示图片
            self._handle_endfield_result(result)
        else:
            # 检测工具返回中是否包含文件路径（PDF/视频等），自动显示为可点击文件卡片
            self._maybe_show_file_card(tool_name, result)

    def _handle_endfield_result(self, result: str):
        """终末地工具结果：带二维码图片时推送到聊天窗口"""
        try:
            data = json.loads(result)
        except (json.JSONDecodeError, TypeError):
            return
        if not isinstance(data, dict):
            return
        qr = data.get("qr_image_path") or ""
        if qr and Path(qr).exists():
            self.chat.bot_image_in.emit(str(Path(qr)))
            if data.get("already_running"):
                self.chat.system_msg_in.emit("[终末地] 上面是之前那张登录码，继续扫或用它重新登录")
            if data.get("mode") == "web":
                # 网页授权：二维码里就是授权链接，同时把链接发出来便于直接点
                self.chat.system_msg_in.emit(
                    f"[终末地] 扫码接口暂时不可用，已改用网页授权，点这里完成授权：{data.get('auth_url','')}")
        elif data.get("auth_url"):
            self.chat.system_msg_in.emit(f"[终末地] 请点开完成授权：{data['auth_url']}")
        elif data.get("need_login"):
            self.chat.system_msg_in.emit(
                "[终末地] 还没登录：请让我「登录终末地」，扫完即可查角色/签到")

    def _on_tool_result_ui(self, tool_name: str, result: str):
        self.tool_log.add_log(f"结果 [{tool_name}]: {result[:200]}", "tool_result")
        short = result[:120] + ("..." if len(result) > 120 else "")
        # append_system_msg 现在改投思考面板（★ 1.9.2），不必再单独写一次
        self.chat.append_system_msg(f"[{tool_name} 返回]: {short}")

    def _on_endfield_login_done(self, ok: bool, message: str, role: dict):
        """★ 1.6.0: 终末地扫码登录结果 → 聊天窗口（回调来自后台线程，走信号）"""
        try:
            if ok:
                name = (role or {}).get("nickname") or "未知角色"
                self.chat.system_msg_in.emit(f"[终末地] {message}（{name}）")
            else:
                self.chat.system_msg_in.emit(f"[终末地] {message}")
        except Exception as e:
            _log.debug(f"推送终末地登录结果失败: {e}")

    def _maybe_show_file_card(self, tool_name: str, result: str):
        """检测工具返回的 JSON 中是否含有文件路径，自动推给聊天窗口"""
        try:
            data = json.loads(result)
            if not isinstance(data, dict) or not data.get("success"):
                return
        except (json.JSONDecodeError, TypeError):
            return

        # ---- JMComic 下载完成，PDF/图片目录 ----
        if tool_name == "jmcomic":
            pdf_path = data.get("pdf", "")
            if pdf_path:
                p = Path(pdf_path)
                if p.exists():
                    size = p.stat().st_size
                    size_str = f"{size / 1024:.1f} KB" if size < 1024 * 1024 else f"{size / 1024 / 1024:.1f} MB"
                    # ★ 1.9.0: 改走 _push_bot_attachment —— 除显示外还写进历史
                    #   （以前只 emit file_card_in，重启后卡片全没了）
                    self._push_bot_attachment(p, "📚 本子下载完成", size_str)
                    return

        # ---- B站视频下载完成 ----
        if tool_name == "bilibili":
            file_path = data.get("file_path", "")
            if file_path:
                p = Path(file_path)
                if p.exists():
                    res = data.get("resolution", "")
                    size = data.get("file_size_mb", "")
                    size_str = f"{size} MB" if size else ""
                    label = f"🎬 视频下载完成" + (f" ({res})" if res else "")
                    self._push_bot_attachment(p, label, size_str)
                    return

        # 通用：检测任何含 file_path / pdf 的结果
        # ★ 1.9.0: 新增的 download 工具（联网搜索配图 / B站封面等）就走这里——
        #   只要返回 {"success": true, "file_path": ...} 就自动弹图/弹卡片。
        #   注意：**不再认 save_dir** —— jmcomic 一开始就返回 save_dir，
        #   以前被当成文件卡片发了出去（W6 反馈"把 download 文件夹路径当卡片发"）。
        file_path = data.get("file_path", data.get("pdf", ""))
        if file_path:
            p = Path(file_path)
            if p.is_file():     # 只推真实文件，目录不算
                label = data.get("message", tool_name)
                size_str = data.get("file_size", "") or ""
                self._push_bot_attachment(p, label, size_str)

    def _handle_file_ops_result(self, result: str):
        """处理 file_ops 工具的特殊操作（如 send_file）"""
        try:
            data = json.loads(result)
            if not isinstance(data, dict):
                return
            # 检测 send_file 操作
            if data.get("action") == "send_file" and data.get("success"):
                file_path = data.get("file_path", "")
                if file_path and Path(file_path).exists():
                    p = Path(file_path)
                    label = data.get("message", f"📎 {p.name}")
                    size_str = data.get("file_size", "")
                    # ★ 1.9.0: 图片/文件都由 _push_bot_attachment 分流
                    #   （图片走左侧内联缩略图，其它走卡片），并写进历史
                    self._push_bot_attachment(p, label, size_str)
        except (json.JSONDecodeError, TypeError):
            pass

    # ── ★ 1.9.0: 工具推送的附件 → 显示 + 落库 ──

    _IMG_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico")

    def _on_tool_file_ready(self, label: str, path: str, size: str):
        """★ 1.9.0: 后台线程产出的文件 → 主线程统一落库入口。
        （jmcomic 的 PDF 就是这么来的；以前它只 emit 显示信号，重启即消失）"""
        self._push_bot_attachment(path, label, size)

    # ── ★ 1.9.1: ComfyUI 后台画完 → 往上下文插一条系统提示 ──

    def _on_context_note(self, text: str):
        """把一条提示插进**上下文**（不只是显示）。

        用户原话："上下文插入系统提示：刚刚发送了xxxx图片。"

        ★ 为什么要区分两路：
          · 若**此刻有一轮对话在跑**（工具调用链中间），直接往 session.messages
            里插消息会破坏 assistant(tool_calls) ↔ tool 结果 的配对 → API 400。
            所以这时先排队，等本轮结束由 `_flush_pending_attachments` 补进去
            （这是 1.9.0 附件消息就定好的做法，见 `_persist_attachment`）。
          · 若**当前空闲**（画图是后台的，很常见：用户问完就去干别的了），
            那就直接插 + 落库，这样**下一条用户消息**就能看到这条提示。
            不这么做的话，"空闲时画完"的提示要等到再下一轮才补上，模型会重复画。
        """
        text = (text or "").strip()
        if not text:
            return
        try:
            if getattr(self, "_turn_active", False):
                self._persist_attachment(text)
                _log.debug("[画图] 本轮进行中 → 上下文提示先排队，本轮结束补进去")
                return
            # 空闲：直接进内存 + 落库
            tid = None
            try:
                tid = self.session.topic_id
            except Exception:
                pass
            try:
                rid = self.db.chat.add("assistant", text, topic_id=tid)
                if rid:
                    # 提示语对长期记忆没有语义，别进 LTM 提取队列
                    self.db.chat.mark_processed([rid], 1)
            except Exception as e:
                _log.warning(f"[画图] 上下文提示落库失败: {e}")
            with self.session._lock:
                self.session.messages.append({"role": "assistant", "content": text})
            self.chat.system_msg_in.emit(f"[系统] {text}")
            _log.info(f"[画图] 已把提示写进上下文: {text}")
        except Exception as e:
            _log.warning(f"[画图] 写上下文提示失败: {e}")

    def _push_bot_attachment(self, path, label: str = "", size: str = "",
                             is_image: bool = None):
        """把工具产出的文件推给聊天窗口，并**写进历史**（重启后仍在）。

        背景：1.8.4 只把「用户上传」侧的卡片/缩略图接进了历史库；工具推送侧
        （jmcomic 卡片、B站视频、终末地二维码）走的是 chat.bot_image_in /
        chat.file_card_in 这两个**只 insertHtml、不落库**的显示信号，
        所以重启后一律消失 —— 与 V7 报的 bug 属同一类。

        做法：① 立刻显示（当场看得到）；② 写一条 assistant 记录进 DB，
        正文带内联标记（图片 ![](file:///…)、文件 [[FILE:…]]），
        `_reload_chat_view` 会按标记把缩略图/卡片重建出来。

        ★ 1.9.0 补两条（你实测反馈）：
          · **目录不算附件**：jmcomic 一开始就返回 save_dir，以前被当成
            "文件卡片"发出来（还因为落了库而永远留着）；
          · **同一轮内同一路径只推一次**：LLM 常会先调 download 再调
            file_ops send，同一张图会显示两遍。
        """
        if not path:
            return
        try:
            p = Path(path)
            if not p.is_absolute():
                p = (_BASE_DIR / p)
            p = p.resolve()
        except Exception as e:
            _log.debug(f"附件路径解析失败 {path}: {e}")
            return
        if not p.exists():
            _log.debug(f"附件不存在，跳过: {p}")
            return
        if p.is_dir():
            # 目录不是"文件"，别给文件夹发卡片（W6 反馈）
            _log.debug(f"附件是目录，跳过: {p}")
            return
        key = str(p).lower()
        if key in self._attachment_seen:
            _log.debug(f"同一轮内已推送过，跳过重复附件: {p.name}")
            return
        self._attachment_seen.add(key)
        if is_image is None:
            is_image = p.suffix.lower() in self._IMG_EXTS
        # ① 立刻显示
        try:
            if is_image:
                self.chat.bot_image_in.emit(str(p))
                marker = f"![]({p.as_uri()})"
            else:
                self.chat.file_card_in.emit(label or f"📎 {p.name}", str(p),
                                            size or "", True)
                marker = self.chat.file_marker(p, size or "", label or "桌宠发送")
        except Exception as e:
            _log.warning(f"附件显示失败 {p.name}: {e}")
            return
        # ② 落库（重启后由标记重建）
        text = f"{marker}\n{label}".strip() if label else marker
        self._persist_attachment(text)

    def _persist_attachment(self, text: str):
        """★ 1.9.0: 附件正文写进 DB，并排队等本轮结束后补进 session.messages。

        为什么延后补 session.messages：工具回调发生在**工具调用链中间**
        （assistant(tool_calls) 已入列、对应的 tool 结果可能还没入列），
        这时插一条 assistant 消息会破坏 tool_calls ↔ tool 结果配对 → 400。
        所以内存补写统一推到本轮结束（_on_session_done_safe）；
        DB 是独立通道，随手就写，不受调用链影响。
        """
        try:
            tid = self.session.topic_id
        except Exception:
            tid = None
        try:
            rid = self.db.chat.add("assistant", text, topic_id=tid)
        except Exception as e:
            _log.warning(f"附件落库失败: {e}")
        else:
            # 附件标记对长期记忆没有语义（只是一条路径），别进 LTM 提取队列
            try:
                if rid:
                    self.db.chat.mark_processed([rid], 1)
            except Exception:
                pass
        try:
            self._pending_attachments.append(text)
        except Exception:
            pass

    def _flush_pending_attachments(self):
        """★ 1.9.0: 本轮结束后把附件消息补进 session.messages ——
        这样话题内重画（_reload_chat_view 读的是 session.messages）也能看到，
        内存与 DB 保持一致。"""
        pend = getattr(self, "_pending_attachments", None)
        if not pend:
            return
        self._pending_attachments = []
        try:
            with self.session._lock:
                for t in pend:
                    self.session.messages.append({"role": "assistant", "content": t})
            _log.debug(f"附件消息已补进会话: {len(pend)} 条")
        except Exception as e:
            _log.debug(f"附件补进会话失败: {e}")

    def _on_system_msg_safe(self, msg: str):
        # chat.system_msg_in 已经是跨线程 Signal，直接 emit
        self.chat.system_msg_in.emit(msg)

    def _on_clip_image_pasted(self, qimage):
        """★ 1.9.0: 剪贴板里的图片**直接**发进聊天（**不写磁盘**）。

        流程：
          1. 立刻在聊天里显示（`append_clip_image` 把 QImage 注册成文档内存资源）
          2. 后台用**同一套视觉后端**（file_view.describe_image，禁思考、如实描述）
             读出图里有什么
          3. 把「标记 + 描述」作为一条用户消息发给 LLM —— 所以即使重启后
             预览变占位提示，"这张图里有什么"仍然留在对话历史里
        """
        try:
            from io import BytesIO
            from PySide6.QtCore import QBuffer, QIODevice
            from PIL import Image
            buf = QBuffer()
            buf.open(QIODevice.WriteOnly)
            qimage.save(buf, "PNG")
            pil = Image.open(BytesIO(bytes(buf.data()))).convert("RGB")
        except Exception as e:
            self.chat.append_system_msg(f"[粘贴的图片读取失败: {e}]")
            return

        import time as _time
        name = f"粘贴的图片_{_time.strftime('%H%M%S')}.png"
        marker = self.chat.append_clip_image(qimage, name)
        self.chat.append_system_msg("[正在看这张图…]")
        self.pet_mgr.set_behavior("思考")

        def _work():
            try:
                from tools.file_view import describe_image, effective_vision_mode
                desc = describe_image(pil)
                _log.info(f"[粘贴图片] 描述完成: {len(desc)} 字符 "
                          f"(backend={effective_vision_mode()})")
            except Exception as e:
                _log.error(f"[粘贴图片] 描述失败: {e}")
                desc = f"[视觉描述失败: {e}]"
            self._task_relay.user_msg.emit(
                f"{marker}\n"
                f"[用户从剪贴板粘贴了一张图片 {name}（未落盘，仅在本次会话可见）]\n"
                f"内容如下:\n{desc}")

        threading.Thread(target=_work, daemon=True).start()

    def _send_chosen_meme(self, name: str):
        """★ 1.9.0: 把 LLM 在思考末尾挑好的表情包**单独发一条**（在文字回复之后）。

        你要的效果是"文字流式输出完 → 再发一条表情包，分两条，表情包最后"，
        所以这里必须由 `_on_session_done_safe` 在 `stream_done_in` 之后调用。

        走的是统一附件通道 `_push_bot_attachment`：
          · 图片在聊天左侧内联缩略图（桌宠侧、带头像）
          · 会写进聊天历史，重启后仍在
        挑不到（模型编了名字 / 文件被删）就**静默跳过**，绝不发不存在的文件。
        """
        try:
            from src.core.meme_library import resolve
            p = resolve(name)
            if p is None:
                _log.warning(f"[表情包] LLM 挑的「{name}」找不到对应文件，跳过发送")
                return
            _log.info(f"[表情包] 单独发送: {p.name}")
            self._push_bot_attachment(p, "🎭 表情包", "", is_image=True)
        except Exception as e:
            _log.warning(f"[表情包] 发送失败({name}): {e}")

    def _on_session_done_safe(self):
        # ★ 1.9.1: 本轮结束 → 清掉"进行中"标记（后台异步提示据此决定是插还是排队）
        self._turn_active = False
        # ★ 1.9.0: 本轮结束 → 把工具推来的附件消息补进会话（错开工具调用链）
        self._flush_pending_attachments()
        # ★ 1.9.0（你的方案）：表情包**优先走 meme 工具** —— LLM 在回复前调用一次，
        #   工具把选中的放进"待发送"；这里（文字回复结束之后）取走并发一条。
        #   老的"思考/正文里写 {"meme": …}"协议保留为兜底（模型偶尔还是会那么写）。
        _meme = ""
        try:
            # ★ 1.9.0: 直接读信箱（src/core/meme_bus）—— 别 import tools.meme_tool：
            #   那个模块会被 ToolManager 卸载/重载，模块级状态会丢（真机踩过，
            #   表情包一张都发不出来就是这个原因）。
            from src.core import meme_bus as _mb
            _pend = _mb.take() or {}
            _meme = str(_pend.get("name") or "")
            if _meme:
                _log.info(f"[表情包] 来自 meme 工具: {_meme}"
                          f"（tag={_pend.get('tag') or '-'}）")
        except Exception as e:
            _log.debug(f"[表情包] 取工具待发送失败: {e}")
        # 兜底：老协议挑的（必须在 on_done() 之前读，它会把 _chosen_meme 清掉）
        if not _meme:
            try:
                _fn = getattr(self.pet_mgr, "chosen_meme", None)
                _meme = (_fn() if callable(_fn) else "") or ""
            except Exception:
                _meme = ""
        self.pet_mgr.on_done()
        self.chat.stream_done_in.emit()
        # ★ 1.9.0: 文字回复已经输出完 → 再单独发一条表情包（分两条、表情包最后）
        if _meme:
            self._send_chosen_meme(_meme)
        # ★ TTS 播报：AI 完整回复
        if self._last_assistant_text.strip():
            self.tts.speak(self._last_assistant_text)
        # ★ 用户画像提取：从完整对话中提取特征（LTM 禁用时跳过）
        if self._last_assistant_text.strip() and self._is_profile_enabled():
            self.profile_extractor.extract_async(self._last_assistant_text)
        # ★ 1.7.0: 聊天记录 → 长期记忆（异步投喂，不阻塞回复）
        if self._is_profile_enabled():
            try:
                self._feed_ltm_from_chat()
            except Exception as e:
                _log.debug(f"聊天记忆入档跳过: {e}")
        self.chat.set_hide_on_focus_out(True)

    def _on_session_abort_safe(self):
        # ★ 1.9.1: 中断也算本轮结束
        self._turn_active = False
        # ★ 1.9.0: 中断同样要收尾附件消息（否则会永远留在待补队列里）
        self._flush_pending_attachments()
        self.pet_mgr.on_abort()
        self.chat.set_hide_on_focus_out(True)

    def _on_session_topic_changed(self, topic_id: int):
        """话题切换/创建/标题更新时刷新窗口状态行（原来是刷新侧栏）"""
        self._update_header_info()

    # ============ TTS ============

    def _on_tts_start(self):
        self.pet_mgr.set_behavior("说话")

    def _on_tts_end(self):
        self.pet_mgr.set_behavior("待机")

    # ============ 监控处理 ============

    def _on_monitor_data(self, source: str, data: dict):
        if source == "screen":
            return
        text = self._build_monitor_text(source, data)
        if text:
            # ★ 上限保护：超出 500 条时丢弃最旧的一半
            if len(self._batch_monitor_events) >= 500:
                self._batch_monitor_events = self._batch_monitor_events[250:]
            self._batch_monitor_events.append(text)

    def _flush_batch_summarize(self):
        if not self._batch_monitor_events:
            return
        # ★ 1.7.0: Pipeline 启用时由 Pipeline 统一清洗总结——
        #   旧 LLM1 路径会把总结再写回 raw_events（cleaned=0），
        #   被 Pipeline 当作原始数据二次清洗，形成"总结→再总结"污染环
        if hasattr(self, '_pipeline'):
            self._batch_monitor_events.clear()
            self._pipeline.trigger_now()
            return
        # Pipeline 未启用 → 保留旧 LLM1 兜底路径
        unique = list(dict.fromkeys(self._batch_monitor_events))
        self._batch_monitor_events.clear()
        _log.info(f"LLM1 定时总结: {len(unique)} 条事件")
        text = "\n".join(unique)
        threading.Thread(
            target=self._summarize_text, args=(text, "activity"), daemon=True).start()

    def _update_summarize_timer(self):
        t_min = max(1, self.config.monitor.summarize_interval_minutes)
        ms = t_min * 60 * 1000
        self._batch_timer.stop()
        self._batch_timer.start(ms)
        _log.info(f"LLM1 总结间隔: {t_min} 分钟")

    def _build_monitor_text(self, source: str, data: dict) -> str:
        if source == "keyboard":
            return f"用户键盘输入: {data.get('text', '')}"
        elif source == "mouse":
            t = data.get("type", "")
            if t == "click":
                return f"鼠标{data.get('button','')}键点击"
            elif t == "scroll":
                return f"鼠标滚轮滚动"
            return ""
        elif source == "system":
            win = data.get("active_window", "")
            cpu = data.get("cpu_percent", 0)
            if win:
                return f"活动窗口: {win} | CPU: {cpu}% | 内存: {data.get('memory_percent',0)}%"
            return ""
        elif source == "audio":
            vol = data.get("volume", 0)
            if vol > 500:
                return f"检测到声音"
            return ""
        # ★ 增强输入监控事件
        elif source == "clipboard":
            return f"剪贴板变化: {data.get('content_preview', '')[:100]}"
        elif source == "window_metadata":
            app = data.get("app_name", "")
            title = data.get("window_title", "")
            url = data.get("browser_url", "")
            if url:
                return f"浏览器: {url} | 窗口: {title}"
            elif app:
                return f"切换应用: {app} | 窗口: {title}"
            return ""
        elif source == "process_metadata":
            cats = data.get("active_categories", [])
            if cats:
                return f"活动应用类别: {', '.join(cats)}"
            return ""
        return ""

    def _process_screen_image(self, image):
        if self._processing_image or not self.config.monitor.screen_enabled:
            return
        self._processing_image = True
        try:
            vision_text = self.vision.analyze(image)
            memories = self.summarizer.summarize(vision_text)
            if memories:
                # ★ 1.7.0: 视觉总结直接写 monitoring_events（结构化事件层），
                #   不再经 add_memories_batch 写回 raw_events——那会被 Pipeline
                #   当作原始监控数据二次清洗（污染环）
                for m in memories:
                    self.db.raw.add_event(
                        event="屏幕活动", detail=str(m)[:2000],
                        confidence=80, source="vision")
                _log.debug(f"屏幕视觉总结入库(monitoring_events): {len(memories)} 条")
        except Exception as e:
            _log.error(f"屏幕处理失败: {e}")
        finally:
            self._processing_image = False

    def _summarize_text(self, text: str, source: str):
        try:
            memories = self.summarizer.summarize(text)
            if memories:
                batch = [{"content": m, "category": source, "source": source, "importance": 1}
                         for m in memories]
                self.db.add_memories_batch(batch)
        except Exception as e:
            _log.error(f"文本总结失败: {e}")

    # ============ 图片识别 ============

    def _on_image_recognize(self, file_path: str):
        """★ 1.9.0: 用户上传图片 → **统一走文件查看工具**（file_view.view_file）。

        以前这里直接调 self.vision.analyze，并把图片 attach 给 session 做
        image_url 多模态直传 —— 于是"看图"存在两条链路（聊天一条、LLM 看文件一条）。
        现在统一：图片一律由 view_file 读成**如实描述**的文本进对话，
        不再往消息里塞 image_url（你要求的"不再是多模态聊天，统一一下"）。
        读图后端由设置页「多模态 / 视觉识别」两块决定，
        见 tools/file_view.effective_vision_mode()。
        """
        path = Path(file_path)
        if not path.exists():
            self.chat.append_system_msg("[图片文件不存在]")
            return

        self.chat.append_system_msg(f"[正在读取图片: {path.name}]...")
        self.pet_mgr.set_behavior("思考")

        # ★ 1.9.0: 记住本次上传的图片 —— 只用于聊天里的缩略图标记（落库/重启后重建），
        #   不再用于 image_url 直传。
        self._last_uploaded_image = str(path)

        def _do_recognize():
            try:
                from tools.file_view import view_file, effective_vision_mode
                _log.debug(f"[图片查看] 后台线程启动: {path.name}")
                res = view_file(str(path), method="multimodal")
                if not res.get("success"):
                    self._img_relay.image_failed.emit(str(res.get("error", "查看失败")))
                    return
                result = res.get("text", "") or ""
                mode = effective_vision_mode()
                _log.info(f"[图片查看] 完成: {len(result)} 字符 "
                          f"(backend={mode}; {res.get('note', '')})")
                self._img_relay.image_recognized.emit(path.name, result, mode)
            except Exception as e:
                _log.error(f"图片查看失败: {e}")
                self._img_relay.image_failed.emit(str(e))

        threading.Thread(target=_do_recognize, daemon=True).start()

    def _on_image_recognize_failed(self, error_msg: str):
        """图片识别失败回调（主线程）"""
        _log.error(f"图片识别失败回调: {error_msg}")
        self.chat.append_system_msg(f"[图片识别失败: {error_msg}]")
        self.pet_mgr.set_behavior("待机")

    def _display_recognize_result(self, filename: str, result: str, mode: str):
        """在主线程显示识别结果（由 Signal 安全回调）"""
        _log.info(f"[图像识别→显示] 开始: {filename} ({mode}, {len(result)} 字符)")
        try:
            _log.debug(f"[图像识别→显示] step1 mode_label...")
            mode_label = {"api": "云端视觉API", "ollama": "Ollama视觉", "ocr": "本地OCR"}.get(mode, mode)
            _log.debug(f"[图像识别→显示] step2 append_system_msg...")
            self.chat.append_system_msg(f"[图片识别完成 ({mode_label})]")
            _log.debug(f"[图像识别→显示] step3 _append_message...")
            self.chat._append_message("assistant", f"[图片: {filename}]\n{result}")
            _log.debug(f"[图像识别→显示] step4 待机...")
            self.pet_mgr.set_behavior("待机")

            _log.debug(f"[图像识别→显示] step5 send_message...")
            self._last_assistant_text = ""
            # ★ 1.6.0: 多模态开启时，图片已作为 image_url 随本条消息直传，
            #   文字部分只作为补充说明（避免重复描述）
            # ★ 1.8.2: 必须把本地视觉识别结果一起带上 —— 当前对话模型若
            #   不支持看图，session 会把 image_url 剥掉重试，那时模型手里
            #   就只剩"请直接看图回答"，于是回一句"我看不到画面"。
            #   带上识别结果后，降级路径也有内容可答。
            # ★ 1.8.4: 正文最前面带 markdown 图片标记 —— 这条消息会落库，
            #   重启后 _render_message_html 会把它重新渲染成缩略图
            #   （以前图片只有界面上的临时预览，没进历史 → "重启后图片没了"）。
            img_marker = ""
            try:
                from pathlib import Path as _P
                _p = _P(self._last_uploaded_image) if getattr(
                    self, "_last_uploaded_image", "") else None
                if _p and _p.exists():
                    img_marker = f"![{_p.name}]({_p.as_uri()})\n"
            except Exception as e:
                _log.debug(f"生成图片标记失败: {e}")
            # ★ 1.9.0: 统一成一条消息 —— 图片内容由 file_view **如实描述**得到，
            #   不再有"图片已随消息直传（image_url）"那条分支（你要求统一）。
            #   正文最前面仍带 markdown 图片标记：落库后重启能重建缩略图。
            send_text = (f"{img_marker}"
                         f"[用户上传了图片 {filename}，以下是读出的内容]\n{result}")
            self.session.send_message(send_text)
            _log.info(f"[图像识别→显示] 完成: send_message 已调用 ({filename})")
        except Exception as e:
            _log.error(f"[图像识别→显示] 异常: {e}", exc_info=True)
            self.chat.append_system_msg(f"[显示结果异常: {e}]")
            self.pet_mgr.set_behavior("待机")

    # ============ 主动待机说话 ============

    # ============ 控制 ============

    def _toggle_visibility(self):
        """托盘双击 / 聊天菜单的「显示 / 隐藏桌面宠物」—— 切换语义"""
        self._apply_pet_visibility(not getattr(self, "_visible", True))

    def _apply_pet_visibility(self, visible: bool, force: bool = False):
        """★ 接管（2026-09-17 22:30）：形象显隐统一走这里。

        · `_visible` 是"用户希望它显示吗"，**主进程自己记着**（原来只看子进程
          状态，所以主进程没法判断"是不是用户主动隐藏的"）。
        · 托盘菜单现在拆成「显示桌宠」「隐藏桌宠」两项（你的要求），
          两项都是**强制**语义：显示就强制显示，隐藏就强制隐藏。
        """
        self._visible = bool(visible)
        try:
            if self._visible:
                self.pet.show()
                _log.debug("[形象] 显示（用户操作）")
            else:
                self.pet.hide()
                self.chat.hide()
                if getattr(self, "tool_log", None) is not None:
                    self.tool_log.hide()
                _log.debug("[形象] 隐藏（用户操作）")
        except Exception as e:
            _log.debug(f"切换形象显隐失败: {e}")

    def show_pet_force(self):
        """托盘「显示桌宠」：强制显示（不切换、不猜状态）"""
        self._apply_pet_visibility(True)

    def hide_pet_force(self):
        """托盘「隐藏桌宠」：强制隐藏（不切换、不猜状态）"""
        self._apply_pet_visibility(False)

    def _on_external_open(self, path: str = ""):
        """★ 接管（2026-09-17 22:30）：打开外部程序后，兜底确认形象还在。

        你实测的现象："左键查看图片，退出图片查看器时，形象会一起消失，可重复。"
        形象只可能被 `hide` 命令藏起来（我把命令日志补上了，下次能查到是谁藏的）；
        在这之前先做一个**幂等兜底**：2 秒 / 6 秒后各确认一次 —— 只要用户没主动
        隐藏，就把形象重新 show() 一次（重复调用无害）。
        """
        _log.debug(f"[形象] 已打开外部程序，稍后兜底确认形象是否还在: {path}")
        for _delay in (2000, 6000):
            QTimer.singleShot(_delay, self._ensure_pet_visible)

    def _ensure_pet_visible(self):
        if not getattr(self, "_visible", True):
            return                      # 用户主动隐藏的，别自作主张弹回来
        try:
            self.pet.show()
            _log.debug("[形象] 外部程序关闭后兜底确认 → 已 show()")
        except Exception as e:
            _log.debug(f"[形象] 兜底 show 失败: {e}")

    def _show_settings(self):
        # ★ 1.9.0（加载优化）：设置窗口懒创建 —— 启动时不再预建 9 个页面
        if getattr(self, "settings_win", None) is None:
            _log.debug("[启动优化] 第一次打开设置，现建窗口")
            self.settings_win = SettingsWindow()
            self.settings_win.config_changed.connect(self._on_config_changed)
            self.settings_win.restart_monitor.connect(self._restart_monitor)
            self.settings_win.provider_changed.connect(self._on_provider_changed)
        self.settings_win.show()
        self.settings_win.raise_()

    def _on_config_changed(self):
        """配置变更回调 —— 异步执行耗时操作，避免阻塞 UI"""
        self.config = get_config()
        # ★ 1.9.0: 视觉/多模态设置改完必须**立刻**让视觉客户端失效重建。
        #   真机现象（你的 Z18 记录）：在设置里关掉「多模态」并选了 OCR，
        #   第一次读图**仍然走了多模态**，第二次才对 —— 就是缓存里的
        #   视觉客户端还是旧配置（mode=api）。这里统一失效，下次读图重新判定。
        try:
            from src.core.llm_client import invalidate_vision_cache, effective_vision_mode
            invalidate_vision_cache()
            _log.info(f"视觉客户端缓存已失效，下次读图按当前设置走: "
                      f"{effective_vision_mode()}")
        except Exception as e:
            _log.debug(f"刷新视觉客户端缓存失败: {e}")
        name = self.config.pet.character
        # ★ 1.5.0: 同步把当前角色 settings.json 的 persona/style/emoji/model 应用到 config。
        #   必须在 switch_character（异步 pet 线程）之前完成，否则 pet 线程 reload 后会把
        #   内存里的 persona 覆盖回 settings.json 旧值，表现为"切换角色性格没变"。
        try:
            from src.core.pet_manager import CharacterInfo, CHARACTERS_DIR
            info = CharacterInfo(CHARACTERS_DIR / name)
            persona = info.persona_prompt()
            if persona:
                self.config.pet.personality_prompt = persona
            for k in ("reply_style", "emoji_level"):
                v = info.settings.get(k)
                if v:
                    setattr(self.config.pet, k, v)
            # ★ 1.9.0: 新版「输出控制 / 行为控制」也灌进运行时配置 ——
            #   以前这两个设置对写了 persona 的角色**根本不生效**
            #   （提示词是原样拼接的，模板里那两行规则压根没走）。
            try:
                _o = info.settings_new["输出控制"]
                _b = info.settings_new["行为控制"]
                for k in ("no_action_description", "no_markdown",
                          "allow_code_block", "allow_json"):
                    if hasattr(self.config.pet, k):
                        setattr(self.config.pet, k, bool(_o[k]))
                for k in ("meme_tendency", "gif_reaction", "proactive_talk"):
                    if hasattr(self.config.pet, k):
                        setattr(self.config.pet, k, _b[k])
                if hasattr(self.config.pet, "meme_tags_prefer"):
                    self.config.pet.meme_tags_prefer = list(_b["meme_tags_prefer"])
            except Exception as _e:
                _log.debug(f"同步输出/行为控制失败: {_e}")
            mp = info.model_params()
            if mp.get("temperature") is not None:
                self.config.llm.temperature = float(mp["temperature"])
            if mp.get("max_tokens"):
                self.config.llm.max_tokens = int(mp["max_tokens"])
            self.config.save()
            _log.info(f"[形象] 已同步应用角色 {name} 的 persona 到 config")
        except Exception as e:
            _log.warning(f"同步应用角色配置失败: {e}")
        # ★ 1.9.0（加载优化）：记录切换前的角色 —— 没换角色时下面这些重活全部跳过
        _prev_char = getattr(self.pet_mgr, "current", "")
        self.pet_mgr.switch_character(name)
        # ★ 1.6.0: 同步切换会话窗口 —— 每个角色一个独立连续上下文，
        #   避免上一角色的历史对话污染新角色的人格（表现为"切换后还自称旧角色"）
        #   ★ 1.9.0: 角色没变就不重载会话窗口（那会重新读库重画聊天历史，白卡一下）
        try:
            if (getattr(self, "session", None) is not None
                    and getattr(self.session, "character", "") != name):
                self.session.set_character(name)
        except Exception as e:
            _log.warning(f"切换角色会话窗口失败: {e}")
        # ★ 1.5.0: 切换形象后同步更新托盘图标（没换角色就不重读图标文件）
        try:
            if getattr(self, "_tray", None) is not None and _prev_char != name:
                self._tray.update_character_icon(name)
        except Exception:
            pass
        # ★ 1.5.0: 切换角色后强制让 LLM 对话客户端重建 system prompt
        #   （否则 ChatClient 缓存的旧角色 persona 不会更新，表现为"性格提示词没变"）
        try:
            from src.core.llm_client import invalidate_chat_cache
            invalidate_chat_cache()
        except Exception:
            pass
        self._update_summarize_timer()

        # ★ 1.4.0: UI 配置统一应用
        self._apply_config_to_ui()

        # TTS 配置
        if hasattr(self, 'tts'):
            self.tts.configure(
                enabled=self.config.tts.enabled,
                character=self.config.tts.character,
                mode=self.config.tts.mode,
                provider=self.config.tts.provider)
            # ★ 1.6.0: 仅在用户勾选"启动时预加载"时才后台加载 GENIE 模型
            if (self.config.tts.enabled and self.config.tts.provider == "genie"
                    and getattr(self.config.tts, "genie_preload_on_start", False)):
                QTimer.singleShot(2000, self._preload_genie_bg)

        # 待机说话配置
        if hasattr(self, 'idle_talker'):
            self.idle_talker.configure(
                enabled=self.config.idle.enabled,
                interval_minutes=self.config.idle.interval_minutes)

        # ★ 视觉识别配置热更新
        if hasattr(self, 'vision'):
            self.vision.reload_config()
        # ★ 1.6.0: 多模态图片直传开关
        if getattr(self, "session", None) is not None:
            self.session.set_multimodal(bool(getattr(self.config.vision, "multimodal", False)))

        # 重定位窗口
        self._reposition_windows()

        # 延迟重启监控，避免同步 join 阻塞 UI 30s
        QTimer.singleShot(200, self.monitor.restart)
        _log.info("配置已更新")

    def _apply_config_to_ui(self):
        """★ 1.4.0: 从配置应用 UI 个性化设置（启动时 + 配置变更时）"""
        ui = self.config.ui
        # 窗口尺寸（可鼠标拖动缩放，故用 resize + 最小尺寸约束）
        new_w = int(getattr(ui, "chat_window_width", 420))
        new_h = int(getattr(ui, "chat_window_height", 640))
        new_w = max(new_w, self.chat.MIN_WIDTH)
        new_h = max(new_h, self.chat.MIN_HEIGHT)

        self.chat._ignore_resize_report = True
        self.chat.resize(new_w, new_h)
        self.chat._ignore_resize_report = False
        # ★ 1.9.1: 原来这里还要 set_sidebar_width / set_sidebar_visible /
        #   _position_sidebar()（侧栏是窗外独立窗口）。侧栏已删，这几步都没了。

        # 透明度
        opacity = getattr(ui, "opacity", 0.92)
        self.chat.setWindowOpacity(opacity)

        # 颜色主题
        try:
            ui_bg = getattr(ui, "ui_bg_color", "#ffffff")
            chat_bg = getattr(ui, "chat_bg_color", "#f7f7fa")
            user_bubble = getattr(ui, "user_bubble_color", "transparent")
            user_text = getattr(ui, "user_text_color", "#222222")
            bot_bubble = getattr(ui, "bot_bubble_color", "transparent")
            bot_text = getattr(ui, "bot_text_color", "#222222")
            self.chat.apply_personalize_colors(ui_bg, chat_bg, user_bubble, user_text, bot_bubble, bot_text)
        except Exception as e:
            _log.debug(f"应用个性化颜色失败: {e}")

        # ★ 聊天头像（空=自带形象）
        try:
            user_avatar = getattr(ui, "user_avatar_path", "") or ""
            pet_avatar = getattr(ui, "pet_avatar_path", "") or ""
            # ★ 1.5.0: 桌宠头像未显式指定时，使用当前角色的 头像.jpg，
            #    保证聊天里的 LLM 头像 = 桌面形象是一套
            if not pet_avatar and getattr(self, "pet_mgr", None) and self.pet_mgr.current:
                info = self.pet_mgr.characters.get(self.pet_mgr.current)
                if info and info.avatar:
                    pet_avatar = str(info.avatar)
            self.chat.set_avatars(user_avatar, pet_avatar)
        except Exception as e:
            _log.debug(f"应用聊天头像失败: {e}")

    def _on_chat_resized(self, width: int, height: int):
        """聊天窗口鼠标拖拽缩放结束后：重定位 + 防抖保存尺寸

        ★ 1.8.3: 不再减侧栏宽度。
        ★ 1.9.1: 侧栏已整个删除（用户方案 2），"减侧栏宽度"这个坑从此不可能再犯。
        历史（留着免得以后有人重新加回来）：侧栏从 1.6.0 起就是聊天窗口**外面**的
        独立窗口，聊天窗口的实际宽度从来不包含它；旧代码在侧栏可见时减掉 158px，
        于是任何一个 resizeEvent 都会把用户存的 420 写成 max(340, 420-158)=340
        —— 表现就是"窗口尺寸每次启动都被刷回最小"。
        """
        self.config.ui.chat_window_width = max(self.chat.MIN_WIDTH, int(width))
        self.config.ui.chat_window_height = height
        self._reposition_windows()
        if not hasattr(self, "_chat_resize_timer"):
            self._chat_resize_timer = QTimer()
            self._chat_resize_timer.setSingleShot(True)
            self._chat_resize_timer.timeout.connect(self._save_chat_size)
        self._chat_resize_timer.start(800)

    def _save_chat_size(self):
        try:
            self.config.save()
            _log.debug(f"聊天窗口尺寸已保存: {self.config.ui.chat_window_width}x{self.config.ui.chat_window_height}")
        except Exception as e:
            _log.warning(f"保存聊天窗口尺寸失败: {e}")

    # ★ 1.9.1: `_on_sidebar_toggled()` 删了 —— 侧栏和它的 ◀/▶ 按钮一起移除，
    #   `config.ui.sidebar_visible` 这个字段也已从 UIConfig 里删掉。

    def _on_provider_changed(self, provider: str):
        """LLM 提供商热切换"""
        switch_provider(provider)

    def _restart_monitor(self):
        if hasattr(self, 'monitor'):
            QTimer.singleShot(100, self.monitor.restart)

    # ============ 增强输入监控 ============

    def _on_input_monitor_data(self, source: str, data: dict):
        """增强输入监控回调（剪贴板/窗口/进程）"""
        # ★ 1.8.5: 退出过程中不再落库（db 可能已关），避免"退出时又报一次错"
        if getattr(self, "_shutting_down", False):
            return
        text = self._build_monitor_text(source, data)
        if text:
            # ★ 1.7.0: 落库 raw_events —— 剪贴板/窗口/进程事件此前只进内存批量列表，
            #   从未进入 Pipeline 清洗链路；现在统一落库（write_counter 联动定量触发）
            try:
                self.db.raw.add_raw_event(
                    source=source,
                    type_=data.get("type", ""),
                    content=text[:2000],
                    metadata=data)
                # 与 UnifiedMonitor 同步的上限保护
                if self.db.raw.write_counter % 50 == 0:
                    self.db.raw.enforce_cap(self.config.monitor.raw_data_cap)
            except Exception as e:
                _log.debug(f"输入监控落库失败: {e}")
            # 上限保护：超出 500 条时丢弃最旧的一半
            if len(self._batch_monitor_events) >= 500:
                self._batch_monitor_events = self._batch_monitor_events[250:]
            self._batch_monitor_events.append(text)
        # 窗口/进程事件也触发用户画像更新（LTM 禁用时跳过）
        if source == "window_metadata" and data.get("browser_url") and self._is_profile_enabled():
            self.profile_extractor.extract_async(
                f"用户在浏览器访问: {data['browser_url']}")

    def start_input_monitoring(self):
        """启动增强输入监控"""
        cfg = self.config.monitor
        self.input_monitor.start(
            clipboard=cfg.clipboard_enabled,
            window=cfg.system_enabled,
            process=cfg.system_enabled,
        )
        _log.info("增强输入监控已启动")

    def stop_input_monitoring(self):
        self.input_monitor.stop()

    # ============ STT 语音转文字 ============

    def _on_task_system_msg(self, text: str):
        """★ 1.8.2: 后台线程 → 主线程，仅显示系统提示"""
        try:
            self.chat.append_system_msg(text)
        except Exception as e:
            _log.warning(f"系统提示显示失败: {e}")

    def _on_task_user_msg(self, text: str):
        """★ 1.8.2: 后台线程 → 主线程，把抽取/转写结果当作一条用户消息发给 LLM"""
        try:
            self._on_user_message(text)
        except Exception as e:
            _log.error(f"后台结果注入对话失败: {e}")

    def _on_idle_trigger_safe(self, prompt: str):
        """★ 1.9.0: 主动说话触发 → 回主线程，以**工具调用条目**注入会话。

        IdleTalker 跑在自己的后台线程里，跨线程 Signal（queued）一定回主线程。
        """
        try:
            if not getattr(self, "_visible", True):
                _log.debug("[主动说话] 形象处于隐藏状态，跳过")
                return
            self._task_relay.idle_trigger.emit(prompt)
        except Exception as e:
            _log.debug(f"[主动说话] 注入失败: {e}")

    def _on_idle_trigger_main(self, prompt: str):
        """★ 1.9.0（你拍板"做"）：主动说话 = 真正的工具调用条目。

        你原话："作为自动工具，在上下文中的角色和其他工具一样，
        「自动工具触发」返回'……'。"
        不再走 user_msg（那会在聊天里显示成用户发的一大段）——
        改走 session.send_auto_tool_trigger：上下文里插
        assistant(tool_calls=[idle_talk]) + tool(结果) 一对，
        聊天里也按「[调用: idle_talk(…)] / [idle_talk 返回]: …」的样式显示。
        """
        try:
            sess = getattr(self, "session", None)
            if sess is None:
                return
            # 和普通用户消息一样：新一轮开始 → 重置附件去重/表情包轮次
            try:
                self._attachment_seen.clear()
            except Exception:
                pass
            try:
                from src.core import meme_bus as _mb2
                _mb2.reset_turn()
            except Exception:
                pass
            self.pet_mgr.on_user_message()
            self.chat.reset_stream()
            self._last_assistant_text = ""
            self.chat.set_hide_on_focus_out(False)   # 主动说话期间不自动隐藏
            if not sess.send_auto_tool_trigger(prompt):
                self.chat.set_hide_on_focus_out(True)
        except Exception as e:
            _log.error(f"[主动说话] 工具条目注入失败: {e}", exc_info=True)

    def _on_stt_transcription(self, text: str):
        """STT 转写完成 → 作为用户消息发送"""
        if not text or not text.strip():
            return
        _log.info(f"STT 识别: {text[:80]}...")
        # 显示在聊天中
        self._task_relay.system_msg.emit(f"[语音识别] {text}")
        self._task_relay.user_msg.emit(text)

    def _on_stt_speech_start(self):
        """用户开始说话"""
        self.pet_mgr.set_behavior("思考")
        self._task_relay.system_msg.emit("[正在聆听...]")

    def _on_stt_speech_end(self):
        """用户停止说话"""
        self._task_relay.system_msg.emit("[语音识别中...]")

    def toggle_stt(self):
        """切换 STT 实时监听"""
        if self._stt_active:
            self.stt.stop_realtime()
            self._stt_active = False
            self.chat.append_system_msg("[语音监听已停止]")
        else:
            # ★ 1.9.0: 以前这里硬编码 configure(mode="auto", model_name="base")，
            #   把设置页里配的 STT 模式/模型**当场覆盖**（真机日志能看到它跑的是
            #   auto 探测而不是用户选的 faster_whisper）。现在按配置来，只在
            #   配置项缺失时才回落到 auto。
            try:
                from src.utils.config import get_config as _gc
                _c = _gc()
                _mode = (getattr(_c.stt, "mode", "") or "auto").strip() or "auto"
                _model = (getattr(_c.stt, "model_name", "") or "base").strip() or "base"
                _lang = (getattr(_c.stt, "language", "") or "zh").strip() or "zh"
            except Exception:
                _mode, _model, _lang = "auto", "base", "zh"
            self.stt.configure(mode=_mode, model_name=_model, language=_lang)
            self.stt.start_realtime(duration_per_chunk=3.0, vad_threshold=0.02)
            self._stt_active = True
            self.chat.append_system_msg("[语音监听已开启，请说话...]")

    # ============ 文件上传（视频/音频/文档） ============

    def handle_file_upload(self, file_path: str):
        """处理上传的文件（★ 1.8.0: 音频转写/视频分析/文档抽取/任意文件元信息）

        图片和纯文本已在 ChatWidget._handle_file 内处理，不会走到这里。
        这里接收：音频 / 视频 / PDF / Office / 以及任何其他类型文件。
        """
        path = Path(file_path)
        if not path.exists():
            self.chat.append_system_msg(f"[文件不存在: {path.name}]")
            return

        suffix = path.suffix.lower()
        file_size = path.stat().st_size
        size_str = (f"{file_size/1024/1024:.1f}MB" if file_size >= 1024 * 1024
                    else f"{file_size/1024:.1f}KB")
        _log.info(f"[上传文件] {path.name} ({suffix or '无扩展名'}, {size_str}) "
                  f"→ 分派处理")

        # 音频 → 后台转写
        if suffix in ('.wav', '.mp3', '.ogg', '.flac', '.m4a', '.aac'):
            self.chat.append_system_msg(f"[正在转写音频: {path.name}]...")
            self.pet_mgr.set_behavior("思考")
            threading.Thread(target=self._transcribe_audio_file, args=(file_path,), daemon=True).start()
            return

        # 视频 → 后台分析
        if suffix in ('.mp4', '.avi', '.mov', '.mkv', '.webm', '.flv'):
            self.chat.append_system_msg(f"[正在分析视频: {path.name}]...")
            self.pet_mgr.set_behavior("思考")
            threading.Thread(target=self._analyze_video_file, args=(file_path,), daemon=True).start()
            return

        # 文档（PDF/Office/纯文本兜底）→ 后台抽取全文
        if suffix in ('.txt', '.md', '.py', '.js', '.json', '.xml', '.html', '.css',
                      '.csv', '.log', '.pdf', '.docx', '.pptx', '.xlsx', '.doc',
                      '.ppt', '.xls'):
            self.chat.append_system_msg(f"[正在读取文档: {path.name}]...")
            self.pet_mgr.set_behavior("思考")
            threading.Thread(target=self._read_document_file, args=(file_path,), daemon=True).start()
            return

        # ★ 1.9.0: 其他任意类型也不再"只给元信息 + 让模型自己去读" ——
        #   统一走文件查看工具（二进制会给文件头 hex + 可打印字符预览）
        self.chat.append_system_msg(f"[正在读取文件: {path.name}]...")
        self.pet_mgr.set_behavior("思考")
        threading.Thread(target=self._view_attachment, args=(file_path,), daemon=True).start()

    def _view_attachment(self, file_path: str, method: str = "auto"):
        """★ 1.9.0: 用户上传文件的**统一入口** —— 全部交给 file_view 读。

        为什么要有它：以前音频走 _transcribe_audio_file、视频走
        _analyze_video_file、文档走 _read_document_file，各写一套抽取逻辑，
        而 LLM 侧看文件又是 file_ops.read —— 三套实现。现在统一成一句
        `view_file(path, method=...)`，聊天与工具看到的内容完全一致。

        结果作为一条用户消息进对话，正文最前面带文件卡片标记（会落库，
        重启后 _reload_chat_view 能按标记重建卡片）。
        """
        path = Path(file_path)
        try:
            from tools.file_view import view_file
            res = view_file(str(path), method=method, max_chars=5000)
            if not res.get("success"):
                _log.warning(f"[上传文件] 查看失败: {path.name} -> {res.get('error')}")
                self._task_relay.system_msg.emit(
                    f"[{path.name} 读取失败] {res.get('error', '未知错误')}")
                return
            _log.info(f"[上传文件] 查看完成: {path.name} kind={res.get('kind')} "
                      f"method={res.get('method')} pages={res.get('pages')}")
            self._task_relay.system_msg.emit(
                f"[{path.name} 读取完成：{res.get('method')}]")
            try:
                _sz = path.stat().st_size
                size_str = (f"{_sz/1024/1024:.1f}MB" if _sz >= 1024 * 1024
                            else f"{_sz/1024:.1f}KB")
            except Exception:
                size_str = ""
            marker = self.chat.file_marker(path.resolve(), size_str, "用户上传")
            _pages = res.get("pages") or []
            _extra = f"  页/表: {_pages}" if _pages else ""
            _note = res.get("note", "")
            self._task_relay.user_msg.emit(
                f"{marker}\n[用户上传了文件 {path.name}]\n"
                f"路径: {path.resolve()}\n"
                f"类型: {res.get('kind')}  方式: {res.get('method')}{_extra}\n"
                f"内容如下:\n{res.get('text', '')}"
                + (f"\n（{_note}）" if _note else ""))
        except Exception as e:
            _log.error(f"文件查看失败: {e}", exc_info=True)
            self._task_relay.system_msg.emit(f"[文件读取出错: {e}]")
        finally:
            self.pet_mgr.set_behavior("待机")

    def _transcribe_audio_file(self, file_path: str):
        """★ 1.9.0: 音频也走统一查看器（method="stt"）"""
        self._view_attachment(file_path, method="stt")

    def _analyze_video_file(self, file_path: str):
        """★ 1.9.0: 视频也走统一查看器（元信息 + 音轨转写）"""
        self._view_attachment(file_path, method="stt")

    def _read_document_file(self, file_path: str):
        """★ 1.9.0: 文档也走统一查看器（method="text" 取原文，超过 5000 字自动抽样）。

        以前这里自己按扩展名逐种抽文本（PDF/docx/pptx/xlsx/纯文本），
        和 LLM 侧的 file_ops.read 是两套实现，容易各自跑偏。
        """
        self._view_attachment(file_path, method="text")

    # ============ 用户画像上下文注入 ============

    def _is_profile_enabled(self) -> bool:
        """检查 LTM/画像功能是否启用（禁用时不报错，只是不处理）"""
        if self._ltm_tool is not None:
            return not self._ltm_tool.is_disabled()
        return True  # 无法判断时默认允许

    def _feed_ltm_from_chat(self):
        """★ 1.7.0: chat_records → 长期记忆

        - 取 memory_processed=0 的记录，user/assistant 文本异步喂给 LTM 工具
          （LLM YES/NO 判断 → 结构化提取 → 去重/冲突合并，全部在工具内完成）
        - tool 角色的记录（工具返回噪声）直接标记已处理，不投喂
        - 每轮最多投喂 40 条（旧→新），剩余留待下一轮继续消化
        - 完成回调标记 memory_processed：1=已处理, -1=失败
        """
        if self._ltm_tool is None:
            return
        rows = self.db.chat.get_unprocessed()
        if not rows:
            return

        # tool 消息：跳过投喂，直接标记已处理
        tool_rows = [r for r in rows if r.get("role") not in ("user", "assistant")]
        if tool_rows:
            self.db.chat.mark_processed([r["id"] for r in tool_rows], 1)

        rows = [r for r in rows
                if r.get("role") in ("user", "assistant")
                and r["id"] not in self._chat_ltm_inflight]
        if not rows:
            return
        rows = rows[:40]

        text = "\n".join(
            f"{r['role']}: {str(r.get('content', ''))[:500]}" for r in rows)
        ids = [r["id"] for r in rows]

        if len(text.strip()) < 10:
            self.db.chat.mark_processed(ids, 1)
            return

        self._chat_ltm_inflight.update(ids)

        def _done(result: str):
            self._chat_ltm_inflight.difference_update(ids)
            try:
                bad = ("不可用" in result) or ("异常" in result) or ("失败" in result)
                self.db.chat.mark_processed(ids, -1 if bad else 1)
            except Exception:
                pass

        _log.info(f"[LTM] 喂给长期记忆 {len(ids)} 条聊天记录")
        try:
            self._ltm_tool.execute_async(text, callback=_done)
        except Exception as e:
            _log.error(f"[LTM] 聊天记忆投喂失败: {e}")
            self._chat_ltm_inflight.difference_update(ids)

    def _inject_user_profile_context(self):
        """将用户画像注入到会话上下文中（LTM 禁用时优雅跳过）"""
        try:
            # ★ 1.8.10: user_profile 在 Phase4 后台线程里才创建
            _prof = getattr(self, "user_profile", None)
            if _prof is None:
                return
            profile_text = _prof.get_context_prompt(500)
            # 检查 LTM 工具是否被禁用（使用缓存引用）
            ltm_disabled = False
            if getattr(self, "_ltm_tool", None) is not None:
                ltm_disabled = self._ltm_tool.is_disabled()
            if ltm_disabled:
                memory_text = ""
            else:
                memory_text = self.long_term_memory.summarize_recent(24)
            context = f"{profile_text}\n{memory_text}"
            self.session.add_context(context)
            _log.debug("用户画像已注入到会话上下文")
        except Exception as e:
            _log.debug(f"画像注入跳过: {e}")

    def _quit(self):
        _log.info("正在退出...")
        # ★ 1.8.5: 先立"正在退出"的旗子，让监控回调别再往（即将关闭的）库里写
        self._shutting_down = True
        # ★ 1.8.4: 退出即落盘 —— 把设置窗口的当前值（全局 + 当前角色）写回本地。
        #   用户反馈"有些内容每次启动都会刷新"，这一脚保证没点过「保存设置」
        #   的改动也不会丢；窗口尺寸/侧栏显隐本来就在改的一瞬间存过了。
        try:
            if getattr(self, "settings_win", None) is not None:
                self.settings_win.flush_all_on_exit()
        except Exception as e:
            _log.warning(f"退出保存设置失败: {e}")
        # 兜底：即使没有设置窗口，也把内存里的 config 落一次盘
        try:
            self.config.save()
        except Exception as e:
            _log.warning(f"退出保存 config 失败: {e}")

        # ★ 立即隐藏形象，让退出看起来瞬间完成
        self.pet.hide()
        self.chat.hide()
        self.tool_log.hide()
        _log.debug("形象+窗口已隐藏，后台清理中...")

        # 先停定时器，防止退出过程中触发回调
        self._batch_timer.stop()

        # 停核心服务（使用线程池并行停止以加速）
        def _stop_services():
            # ★ 1.8.6: Pipeline 必须**第一个**停，而且要在关库之前停稳 ——
            #   它是唯一会在后台调 LLM + 往 raw_events/monitoring_events 写库的
            #   长任务；停在后面会出现"退出中还在跑批次，然后数据库被关掉"。
            try:
                if hasattr(self, '_pipeline'):
                    self._pipeline.stop()
                    _log.debug("Pipeline 已停止")
            except Exception as e:
                _log.warning(f"Pipeline 停止失败: {e}")
            try:
                self.monitor.stop()
                _log.debug("监控已停止")
            except Exception:
                pass
            try:
                self.input_monitor.stop()
                _log.debug("输入监控已停止")
            except Exception:
                pass
            try:
                if self._stt_active:
                    self.stt.stop_realtime()
                    _log.debug("STT 已停止")
            except Exception:
                pass

        def _stop_lightweight():
            try:
                self.idle_talker.stop()
                _log.debug("待机说话已停止")
            except Exception:
                pass
            try:
                self.tts.shutdown()
                _log.debug("TTS 已关闭")
            except Exception:
                pass
            # ★ 1.9.0: 工具管理器的空闲检查线程也要**等它真的退出**。
            #   它是 daemon，以前退出时没人管它 —— 主线程已经在拆解释器了，
            #   它还在 time.sleep/遍历工具表，两边抢 → 访问违例 → Windows 弹
            #   "python.exe - 应用程序错误"（框没人点就永远不退出）。详见
            #   tool_manager._stop_idle_checker 的注释。
            try:
                from src.core.tool_manager import get_tool_manager
                get_tool_manager().shutdown()
                _log.debug("工具管理器已关闭")
            except Exception:
                pass

        # 并行停止轻重服务
        t1 = threading.Thread(target=_stop_services, daemon=True)
        t2 = threading.Thread(target=_stop_lightweight, daemon=True)
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)

        # ★★ 1.9.2（用户反馈"退出也有问题"）：
        #   上一版我给了 25 秒上限，真机日志里正好卡满：
        #       [16:03:06] [退出] 等了 JM 下载 25.0s（剩 2 个未完成）
        #   也就是"为了等两个根本没在下完的任务，白白多等 25 秒" ——
        #   这本身就成了新的"退出慢"问题。改成 6 秒：
        #   够让"就差最后几张"的收尾，又不会让人干等。
        try:
            from tools.jmcomic_tool import wait_for_downloads as _wait_jm
            _w = _wait_jm(timeout=6.0)
            if _w.get("waited", 0) >= 1.0:
                _log.info(f"[退出] 等了 JM 下载 {_w['waited']:.1f}s"
                          f"（剩 {_w.get('remaining', 0)} 个未完成）")
        except Exception as e:
            _log.debug(f"[退出] 等 JM 下载失败（忽略）: {e}")

        # ★ 1.8.5: 关数据库必须排在「监控真的停下来」之后。
        #   以前 db.close() 在 _stop_lightweight 里和停监控并行跑，
        #   db 先关掉、剪贴板/窗口事件还在往 raw_events 写 →
        #   退出时必然冒一句 "输入监控落库失败: 'NoneType' object has no attribute 'execute'"。
        try:
            self.db.close()
            _log.debug("数据库已断开")
        except Exception as e:
            _log.debug(f"数据库关闭失败: {e}")

        # 清理窗口
        try:
            self.pet.close()
        except Exception:
            pass
        try:
            self.chat.close()
        except Exception:
            pass
        try:
            self.tool_log.close()
        except Exception:
            pass

        _log.info("退出完成")
        QApplication.instance().quit()

    def start_monitoring(self):
        if not hasattr(self, 'monitor') or not hasattr(self, 'input_monitor'):
            retry = getattr(self, '_monitor_retry', 0)
            if retry < 15:  # 最多等 3 秒 (15 × 200ms)
                self._monitor_retry = retry + 1
                _log.debug(f"监控组件尚未就绪，延迟重试 ({self._monitor_retry}/15)...")
                QTimer.singleShot(200, self.start_monitoring)
            else:
                _log.error("监控组件加载超时，跳过监控启动")
            return
        _log.info("启动监控...")
        self.monitor.start()
        # 启动增强输入监控
        self.start_input_monitoring()
        # 延迟显示欢迎语
        QTimer.singleShot(500, self._show_welcome)

    def _show_welcome(self):
        name = self.pet_mgr.current or self.config.pet.character
        self.chat.append_system_msg(f"[{name}已上线 ✿]")
        self.chat.append_system_msg(f"单击{name}形象开始聊天~")
        # 刷新窗口状态行（原来的"刷新话题列表"；书签已删）
        self._update_header_info()
        # 不自动显示聊天窗口，等用户点击

    def run(self):
        self.start_monitoring()
