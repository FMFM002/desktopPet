"""
聊天窗口组件 v3.1 - 极简浅色 + 可拖拽缩放 + 自定义头像
- 左侧栏：当前角色窗口状态 + 「开启新话题 / 总结旧话题」（★ 1.6.0 一个角色一个窗口）
- 消息无底色：用户头像靠右、桌宠头像靠左，仅展示文本
- 系统消息居中显示
- 文件卡片（★ 1.8.2: 固定 3cm × 1cm 紧凑芯片，尺寸见 CARD_*_CM）
- 支持拖拽上传文件到形象
- 无边框窗口，鼠标拖动边缘可缩放大小
- 用户 / 桌宠头像支持外部自定义（设置页上传）
"""
import base64
import itertools
import logging
import os
import re
import subprocess
import io
from pathlib import Path
from PySide6.QtCore import (Qt, Signal, QTimer, QEvent, QUrl, QRect, QPoint, QRectF,
                            QPropertyAnimation, QEasingCurve, QParallelAnimationGroup)
from PySide6.QtGui import (QFont, QFontMetrics, QTextCursor, QDragEnterEvent, QDropEvent,
                           QDesktopServices, QPixmap, QPainter, QIcon, QColor, QPen,
                           QPainterPath, QTextTable, QTextLength, QTextDocument,
                           QTextBlock)
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QTextBrowser,
    QPushButton, QLineEdit, QLabel, QFileDialog, QApplication,
    QFrame, QSizePolicy, QScrollArea, QListWidget, QListWidgetItem, QMenu,
)
_log = logging.getLogger("ChatWidget")

_BASE_DIR = Path(__file__).resolve().parent.parent.parent

AVATAR_DISPLAY_SIZE = 34          # 聊天头像显示尺寸

# ★ 1.9.0: 气泡几何（底色由 _RoundedBubbleBrowser 自绘，数字见《UI几何模型.txt》）
BUBBLE_PAD_X = 12                 # 气泡左右内边距
BUBBLE_PAD_Y = 8                  # 气泡上下内边距
BUBBLE_RADIUS = 12                # 圆角半径（Qt 富文本不支持，改由绘制层画）

# ★ 1.9.2（内存调研结论）：聊天窗里"内存粘贴图"的两个上限
#   实测背景：每张 1920×1080 的 QImage 约 6.5 MB，粘 10 张 = +64.7 MB；
#   原来这个 dict 无上限、只在 clear_chat 清，key 还是毫秒时间戳（会撞车覆盖）。
MAX_CLIP_IMAGES = 8               # 最多留最近 8 张（聊天里也只看得见最近几张）
MAX_CLIP_SIDE = 1280              # 存前把长边缩到 ≤1280（单张 ~1.5 MB）


# 自带形象头像（桌宠默认头像）★ 1.5.0: 迁移到 data/桌宠形象/<角色>/
def _default_pet_avatar() -> Path | None:
    chars_dir = _BASE_DIR / "data" / "桌宠形象"
    if chars_dir.exists():
        for d in sorted(chars_dir.iterdir()):
            if not d.is_dir() or d.name.startswith("."):
                continue
            # 优先 settings.json 的 avatar 字段 → 目录里第一张图
            s_file = d / "settings.json"
            if s_file.exists():
                try:
                    import json as _json
                    name = (_json.loads(s_file.read_text(encoding="utf-8"))
                            .get("avatar") or "").strip()
                    if name and (d / name).exists():
                        return d / name
                except Exception:
                    pass
            for cand in list(d.glob("*.jpg")) + list(d.glob("*.png")):
                return cand
    return None


# 默认头像（自带形象头像；不存在时头像位置留空）
DEFAULT_AVATAR_PATH = _default_pet_avatar()


# ★ 1.9.1（你的方案 2「直接爆改」）：**左侧栏整个删掉了**。
#
# 原来这里是一个 `SidebarWindow(QWidget)` —— 一个**独立的顶层窗口**，
# 贴在聊天窗口左边外面（1.6.0 的设计）。副作用就是你报的
# "点击聊天窗口的左侧栏也会退出"：它是个真窗口，点它必然抢焦点 →
# 聊天窗口收到 WindowDeactivate → 触发"失焦即隐藏"→ 看起来像被关掉了。
#
# 两个候选方案里你选了方案 2，所以这里不留兼容壳：
#   · 侧栏窗口 / 跟随定位 / 显示隐藏状态 / 宽度设置 —— 全删
#   · 侧栏里的书签列表 —— 全删（你说"书签功能直接删除"）
#   · 「开启新话题 / 总结旧话题」两个按钮 —— 挪到聊天窗口**右上角按钮组最前面**
#   · 左上角那个 ◀ 切换按钮的位置 —— 换成「形象名字 + tokens 计数」
# 相关备份：chat_widget.py.bak-20260918-005525


class _RoundedBubbleBrowser(QTextBrowser):
    """★ 1.9.0: 自己画圆角气泡。

    为什么要自己画：**Qt 富文本不支持 border-radius**。实测三种写法
    （背景+圆角写在 <td> / 写在 <table> / bgcolor+style）四角全是背景色，
    也就是全是方角；同一台机器上 QLabel + QSS 圆角却正常。

    为什么不用"把方角遮掉"的取巧办法：Qt 给**嵌套表格**画底色的位置和
    `frameBoundingRect()` 差着 (6, 2) px（实测），遮角要么遮不干净、
    要么切过头，而且依赖这个偏移量会随布局变化而失效。

    所以改成：气泡底色**不由 HTML 画**（HTML 只保留内边距），由这里在
    `paintEvent` 里、**文字之前**用 QPainter 画圆角矩形。
    已实测：super().paintEvent() 不会擦掉预画内容，文字会正常叠在上面。
    几何因此是自洽的 —— 画出来的气泡就是我算出来的矩形。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.bubble_pad_x = BUBBLE_PAD_X
        self.bubble_pad_y = BUBBLE_PAD_Y
        self.bubble_radius = BUBBLE_RADIUS
        # ChatWidget 会在每次插入消息时登记 {"start","end","color"}
        self.bubble_spans: list = []

    @staticmethod
    def _iter_frames(frame):
        """递归遍历帧树（气泡那个 1×1 表格嵌套在外层表格的单元格里）"""
        stack = [frame]
        while stack:
            f = stack.pop()
            for child in f.childFrames():
                yield child
                stack.append(child)

    def _painted_box(self, frame, cell):
        """算出 Qt **实际绘制该单元格底色**的矩形（文档坐标）。

        ★ 1.9.0 实测出来的经验规则：把 frameBoundingRect 平移 −(padx, pady)，
        其中 pad = (frame 尺寸 − 文字块并集尺寸) / 2。

        为什么不用「文字块并集直接外扩」：并集的**位置**在嵌套表格里不可靠
        （frame.x 与 union.x 会差一整个内边距），但 frame 的**尺寸**是准的。
        两处实测都精确吻合：
          气泡 frame(191,21,154,31) union(191,21,130,15) pad(12,8) → (179,13) ✓
          卡片 frame( 16,17,326,43) union( 64,17,308,35) pad( 9,4) → (  7,13) ✓
        """
        try:
            doc = self.document()
            layout = doc.documentLayout()
            union = None
            blk = doc.findBlock(cell.firstCursorPosition().position())
            last = cell.lastCursorPosition().position()
            while blk.isValid() and blk.position() <= last:
                if blk.text().strip():
                    r = layout.blockBoundingRect(blk)
                    if r.isValid() and r.width() > 0:
                        union = r if union is None else union.united(r)
                blk = blk.next()
            if union is None:
                return None
            fr = layout.frameBoundingRect(frame)
            padx = max(0.0, (fr.width() - union.width()) / 2.0)
            pady = max(0.0, (fr.height() - union.height()) / 2.0)
            return QRectF(fr).translated(-padx, -pady)
        except Exception as e:
            _log.debug(f"单元格绘制矩形计算失败: {e}")
            return None

    def _bubble_geometry(self) -> list:
        """气泡区间 → [(QRectF 视口坐标, QColor), …]

        坑（实测）：不能对区间内所有 block 求并集 —— 表格会带出"结构性空块"
        （boundingRect 报的是整格可用宽度，实测 326/380），还有头像块。
        所以只认**这条消息里那个 1×1 气泡表格**（外壳表格是 1×2，天然排除）。
        """
        out = []
        spans = self.bubble_spans or []
        if not spans:
            return out
        try:
            doc = self.document()
            dx = self.horizontalScrollBar().value()
            dy = self.verticalScrollBar().value()
            tables = [f for f in self._iter_frames(doc.rootFrame())
                      if isinstance(f, QTextTable)]
            for sp in spans:
                start, end = int(sp.get("start", 0)), int(sp.get("end", 0))
                color = QColor(str(sp.get("color", "")))
                if not color.isValid() or color.alpha() == 0:
                    continue
                for t in tables:
                    if t.rows() != 1 or t.columns() != 1:
                        continue
                    try:
                        if not (start <= t.firstPosition() <= end):
                            continue
                        if t.format().height().type() != QTextLength.VariableLength:
                            continue        # 设了高度的是文件卡片，不是气泡
                        cell = t.cellAt(0, 0)
                    except Exception:
                        continue
                    box = self._painted_box(t, cell)
                    if box is not None:
                        box.translate(-dx, -dy)
                        out.append((box, color))
                    break
        except Exception as e:
            _log.debug(f"气泡几何计算失败: {e}")
        return out

    def _card_rects(self) -> list:
        """★ 1.9.0: 文件卡片矩形 —— 「显式设了高度的 1×1 表格」就是卡片
        （file_card_html 生成；气泡没有显式高度）。卡片底色恒为白，
        所以不用像气泡那样额外记账。"""
        out = []
        try:
            doc = self.document()
            dx = self.horizontalScrollBar().value()
            dy = self.verticalScrollBar().value()
            for frame in self._iter_frames(doc.rootFrame()):
                if not isinstance(frame, QTextTable):
                    continue
                try:
                    if frame.rows() != 1 or frame.columns() != 1:
                        continue
                    if frame.format().height().type() == QTextLength.VariableLength:
                        continue     # 没设高度的表格 = 气泡，不是卡片
                    cell = frame.cellAt(0, 0)
                except Exception:
                    continue
                box = self._painted_box(frame, cell)
                if box is not None:
                    box.translate(-dx, -dy)
                    out.append((box, QColor("#ffffff")))
        except Exception as e:
            _log.debug(f"卡片几何计算失败: {e}")
        return out

    def paintEvent(self, event):
        try:
            if self.bubble_radius > 0:
                pairs = self._bubble_geometry() + self._card_rects()
                if pairs:
                    vp = QRectF(self.viewport().rect())
                    p = QPainter(self.viewport())
                    p.setRenderHint(QPainter.Antialiasing, True)
                    p.setPen(Qt.NoPen)
                    for rect, color in pairs:
                        if not rect.intersects(vp):
                            continue
                        p.setBrush(color)
                        p.drawRoundedRect(rect, self.bubble_radius, self.bubble_radius)
                    p.end()
        except Exception as e:
            _log.debug(f"圆角绘制失败: {e}")
        # 文字画在气泡/卡片**上面**
        super().paintEvent(event)


class ChatWidget(QWidget):
    """聊天窗口（无边框、可拖拽缩放、QQ 风格气泡）

    ★ 1.9.1（用户方案 2「直接爆改」）：**左侧栏没了**。
      原来它是独立的 SidebarWindow（一个真窗口），点它会抢焦点 →
      聊天窗口判定失焦 → 自动隐藏，用户看到的就是"点左侧栏也会退出聊天窗口"。
      现在：
        · 左上角 = 「形象名字 · N tokens」（原来的 ◀ 显隐按钮位置）
        · 右上角按钮组 = 开启新话题 / 总结旧话题 / ＋上传 / ≡菜单（话题两个排最前）
        · 书签列表整块删除
    """

    # ── 对外信号 ──
    send_message = Signal(str)
    abort_chat = Signal()
    abort_tool = Signal()
    file_uploaded = Signal(str, str)
    image_recognize_request = Signal(str)
    file_dropped_on_pet = Signal(str)  # ★ 拖拽到形象后的文件路径
    # ★ 1.8.0: 非图片/非纯文本文件（音频/视频/PDF/Office/任意二进制）交给主窗口处理
    #   （音频转写 / 视频分析 / 文档抽取 / 元信息+路径发给 LLM）
    file_upload_request = Signal(str)
    # ★ 1.9.0: 剪贴板里的**图片数据**（截图/复制图）→ 直接发进聊天（不落盘）
    clip_image_pasted = Signal(object)   # QImage

    # 跨线程安全的信号
    stream_text_in = Signal(str)
    system_msg_in = Signal(str)
    stream_done_in = Signal()
    reset_stream_in = Signal()
    idle_message_in = Signal(str)
    # ★ 1.8.4: 第 4 个参数 from_bot —— 桌宠发来的文件靠左带桌宠头像
    file_card_in = Signal(str, str, str, bool)
    image_preview_in = Signal(str)
    # ★ 1.6.0: 桌宠发来的图片（左侧 + 桌宠头像）
    bot_image_in = Signal(str)
    # ★ 1.9.1: 总结完成 → 请主窗口刷新**标题栏那行「形象名 + tokens」**
    #   （原来是刷新侧栏书签；书签删了，这个信号留着但换了用途）
    summary_done_in = Signal()
    # ★ 1.9.1: 跳转到聊天区指定文本位置（跨线程安全）—— 仍被"滚到底/定位"用
    scroll_to_in = Signal(int)
    # ★ 1.9.1「指令直传」新功能：
    #   · direct_dispatch_request —— 用户点底部"指令直传"按钮 → 通知主窗口
    #     弹出 DirectDispatchWindow；
    #   · bot_text_in —— 主窗口把工具执行结果（纯文本）投到聊天气泡
    #     （**只显示、不落库、不进 session.messages、不喂 LTM**，用户原话）。
    direct_dispatch_request = Signal()
    bot_text_in = Signal(str)
    # ★ 1.9.2（用户任务 4）：思考 / 系统信息 子窗口的跨线程入口
    think_in = Signal(str, str)        # (一行文本, kind: think|system)
    think_delta_in = Signal(str)       # 流式思考增量

    # 话题相关（★ 1.6.0: 一个角色一个窗口，只有「开启新话题 / 总结旧话题」两个动作）
    new_topic_request = Signal()
    summarize_topic_request = Signal()
    # ★ 鼠标缩放结束（新宽度, 新高度）
    resized = Signal(int, int)

    # ★ 1.9.2: 保留最近多少个思考面板（用户只看得到最近几轮 → 有界，不随会话增长）
    MAX_THINK_PANELS = 12

    DEFAULT_WIDTH = 420
    DEFAULT_HEIGHT = 640
    MIN_WIDTH = 340
    MIN_HEIGHT = 420
    RESIZE_MARGIN = 6     # 边缘可拖拽缩放的宽度

    # ★ 1.8.2: 文件卡片尺寸 —— 用「厘米」定，按屏幕 DPI 换算成像素。
    #   想改大小只动这两行即可。
    # ★ 1.8.4: 用户反馈「确实有点小了」→ 按 1.5 倍整体放大（3×1 → 4.5×1.5）。
    CARD_WIDTH_CM = 4.5
    CARD_HEIGHT_CM = 1.5
    CARD_MIN_W_PX = 96    # 兜底：屏幕 DPI 异常时也别小于这个
    CARD_MIN_H_PX = 30

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Tool | Qt.WindowStaysOnTopHint)
        # ★ 1.9.0: 开透明底，样式表里那句 border-radius: 12px 才看得见 ——
        #   以前这里是 False，窗口自己那块不透明的方底把圆角盖住了
        #   （QSS 早就写了圆角，只是一直不生效）。要回退就把这两行去掉。
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        # ★ 1.9.0: 必须同时开 WA_StyledBackground —— 自定义 QWidget 子类默认
        #   是 False，样式表里的 background/border-radius 根本不会被绘制。
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setAcceptDrops(True)
        self.setMinimumSize(self.MIN_WIDTH, self.MIN_HEIGHT)
        self.resize(self.DEFAULT_WIDTH, self.DEFAULT_HEIGHT)

        self._stream_buffer = ""
        self._last_role = ""
        self._stream_start = 0
        self._stream_span = None      # ★ 1.9.0: 流式气泡的区间登记（自绘圆角用）
        # ★ 1.9.0: 剪贴板粘贴的图片（key → QImage）。**只在内存里**：
        #   正文里放 [[CLIP:key|名字|尺寸]] 标记指向它，重启后像素没了，
        #   重画会显示占位提示（文字描述仍保留在历史里）。
        self._clip_images: dict = {}
        # ★ 1.9.2: 粘贴图 key 的自增序号（配合毫秒时间戳，避免同毫秒撞车覆盖）
        self._clip_seq = itertools.count(1)
        # ★ 1.9.1: 标题栏左上角那行信息（原来是侧栏按钮的位置）。
        #   你要求"换成「形象名字」+「tokens 计数」"。
        self._char_name = ""
        self._msg_count = 0
        self._token_count = 0

        # ★ 缩放状态（原生 startSystemResize 失败时的手动回退）
        self._resizing = False
        self._resize_edge = ""
        self._resize_start_geo = None
        self._resize_start_pos = None
        # ★ 程序调用 resize() 时不应触发尺寸持久化
        self._ignore_resize_report = False
        self._resize_debounce = QTimer(self)
        self._resize_debounce.setSingleShot(True)
        self._resize_debounce.timeout.connect(self._emit_resized_debounced)

        # ★ 头像（data URI 缓存）；空路径时使用自带形象
        self._user_avatar_path = ""
        self._bot_avatar_path = ""
        self._avatar_uri = {
            "user": self._make_avatar_uri(None),
            "bot": self._make_avatar_uri(None),
        }

        # ★ 浅色主题（与设置界面同款配色）；文本默认无底色
        self._personalize = {
            "ui_bg": "#ffffff", "chat_bg": "#f7f7fa",
            "user_bubble": "transparent", "user_text": "#222222",
            "bot_bubble": "transparent", "bot_text": "#222222",
        }

        self._build_ui()
        self._apply_style()
        self._bind_signals()

        self._hide_on_focus_out = True
        self.installEventFilter(self)
        self._chat_display.setOpenLinks(False)
        self._chat_display.anchorClicked.connect(self._on_anchor_clicked)
        # ★ 1.5.0: 右键图片/文件卡片 → 复制原图路径
        self._chat_display.setContextMenuPolicy(Qt.CustomContextMenu)
        self._chat_display.customContextMenuRequested.connect(self._on_chat_context_menu)

        # ★ 1.5.0: 聊天窗口菜单回调（无托盘时也能 设置/退出/显示隐藏）
        self.on_settings = None
        self.on_quit = None
        self.on_toggle = None
        self.on_external_open = None      # ★ 接管：打开外部程序后通知主窗口

        _log.debug("聊天窗口 v3.2 就绪")

    # ══════════════════════════════════════════
    # UI 构建
    # ══════════════════════════════════════════

    def _build_ui(self):
        # 主布局直接放在窗口上；边框/圆角交给 ChatWidget 样式表
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # ★ 1.9.1: 侧栏窗口（SidebarWindow）连同它的布局**整块删掉了** —— 见文件头注释。
        #   原来这里挂的是：状态行 _conv_info + ＋新话题 + 📝总结旧话题 + 书签列表。

        # ── 主聊天区 ──
        chat_container = QFrame()
        chat_layout = QVBoxLayout(chat_container)
        chat_layout.setContentsMargins(10, 10, 10, 8)
        chat_layout.setSpacing(4)

        # 标题栏
        header = QHBoxLayout()

        # ★ 1.9.1: 左上角（原来是「◀」侧栏显隐按钮）→ 换成「形象名字 · tokens 计数」
        #   你原话："聊天窗口左上角的左侧栏按钮也删掉，换成'形象名字'+'tokens计数'"
        self._id_label = QLabel("")
        self._id_label.setStyleSheet(
            "color: #666; font-size: 11px; background: transparent;")
        self._id_label.setToolTip("当前形象与上下文用量")
        header.addWidget(self._id_label)

        header.addStretch()

        # ★ 1.9.1: 「开启新话题 / 总结旧话题」挪到右上角按钮组，且**排在最前面**。
        #   你原话："开启新话题和总结旧话题两个按钮加入右上角的按钮内容，排在最前面。"
        topic_btn_style = """
            QPushButton { background: #ececf0; color: #555; border: none;
                          border-radius: 13px; font-size: 11px; padding: 0 10px; }
            QPushButton:hover { background: #007aff; color: #fff; }
            QPushButton:pressed { background: #0055cc; color: #fff; }
        """
        self._new_topic_btn = QPushButton("开启新话题")
        self._new_topic_btn.setFixedHeight(26)
        self._new_topic_btn.setToolTip(
            "清空界面和上下文，把上一段归档，然后开一段全新的对话")
        self._new_topic_btn.setStyleSheet(topic_btn_style)
        self._new_topic_btn.clicked.connect(lambda: self.new_topic_request.emit())
        header.addWidget(self._new_topic_btn)

        self._summarize_topic_btn = QPushButton("总结旧话题")
        self._summarize_topic_btn.setFixedHeight(26)
        self._summarize_topic_btn.setToolTip(
            "先把当前对话总结并存进记录，再开新话题，并把总结带进新上下文")
        self._summarize_topic_btn.setStyleSheet(topic_btn_style)
        self._summarize_topic_btn.clicked.connect(
            lambda: self.summarize_topic_request.emit())
        header.addWidget(self._summarize_topic_btn)

        upload_btn = QPushButton("＋")
        upload_btn.setFixedSize(26, 26)
        upload_btn.setToolTip("上传文件")
        upload_btn.clicked.connect(self._on_upload_file)
        upload_btn.setStyleSheet("""
            QPushButton { background: #ececf0; color: #888; border: none;
                          border-radius: 13px; font-size: 14px; }
            QPushButton:hover { background: #007aff; color: #fff; }
        """)
        header.addWidget(upload_btn)

        # ★ 1.5.0: 菜单按钮（≡）— 设置 / 退出 / 显示隐藏
        self._menu_btn = QPushButton("≡")
        self._menu_btn.setFixedSize(26, 26)
        self._menu_btn.setToolTip("菜单（设置 / 退出）")
        self._menu_btn.clicked.connect(self._on_chat_menu)
        self._menu_btn.setStyleSheet("""
            QPushButton { background: #ececf0; color: #888; border: none;
                          border-radius: 13px; font-size: 16px; font-weight: bold; }
            QPushButton:hover { background: #007aff; color: #fff; }
        """)
        header.addWidget(self._menu_btn)
        chat_layout.addLayout(header)

        # 分隔线
        sep = QFrame()
        sep.setFixedHeight(1)
        sep.setStyleSheet("background: #e8e8ee;")
        chat_layout.addWidget(sep)

        # 对话区（★ 1.9.0: 换成带圆角自绘的子类，见 _RoundedBubbleBrowser）
        self._chat_display = _RoundedBubbleBrowser()
        self._chat_display.setReadOnly(True)
        self._chat_display.setFont(QFont("Microsoft YaHei", 10))
        self._chat_display.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._chat_display.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        chat_layout.addWidget(self._chat_display, stretch=1)

        # ★ 1.9.2（用户任务 4）：嵌在消息流里的"思考 / 系统信息"子窗口。
        #   它是 _chat_display.viewport() 的**真子控件**（不是画在富文本里），
        #   位置由文档里的一张占位图决定 → 跟着消息流一起滚；
        #   内部有自己的 QScrollArea → 内层能独立滚（富文本做不到这个）。
        #   ★ 用户 22:05 修订：**每轮一个**（不是全程共用一个）。
        self._think_panels: list = []      # [{"key","panel","fmt"}]
        self._think_current = None         # 本轮正在写的那一个
        self._chat_display.verticalScrollBar().valueChanged.connect(
            self._reposition_think_panels)
        self._chat_display.installEventFilter(self)

        # 输入区
        input_row = QHBoxLayout()
        input_row.setSpacing(6)
        self._input_field = QLineEdit()
        self._input_field.setPlaceholderText("输入消息...")
        self._input_field.setStyleSheet("""
            QLineEdit { background: #f2f2f5; border: 1px solid #e4e4e8;
                        border-radius: 14px; padding: 7px 12px; font-size: 13px; color: #333; }
            QLineEdit:focus { border-color: #007aff; background: #ffffff; }
        """)
        self._input_field.returnPressed.connect(self._on_send)
        # ★ 1.9.0: 让输入框的 Ctrl+V 走我们自己的处理（剪贴板图片/文件直接发）
        self._input_field.installEventFilter(self)
        input_row.addWidget(self._input_field, stretch=1)

        self._send_btn = QPushButton("发送")
        self._send_btn.setStyleSheet("""
            QPushButton { background: #007aff; color: #fff; border: none;
                          border-radius: 14px; padding: 7px 16px; font-size: 13px; font-weight: bold; }
            QPushButton:hover { background: #0066dd; }
            QPushButton:pressed { background: #0055cc; }
        """)
        self._send_btn.clicked.connect(self._on_send)
        input_row.addWidget(self._send_btn)
        chat_layout.addLayout(input_row)

        # 底部按钮
        ctrl_row = QHBoxLayout()
        ctrl_row.setSpacing(4)

        self._abort_chat_btn = QPushButton("终止对话")
        self._abort_chat_btn.setEnabled(False)
        self._abort_chat_btn.clicked.connect(lambda: self.abort_chat.emit())
        self._abort_chat_btn.setStyleSheet("""
            QPushButton { background: #ff3b30; color: #fff; border: none;
                          border-radius: 6px; padding: 5px 10px; font-size: 11px; }
            QPushButton:hover { background: #e02d22; }
            QPushButton:disabled { background: #f0f0f2; color: #bbb; }
        """)
        ctrl_row.addWidget(self._abort_chat_btn)

        self._abort_tool_btn = QPushButton("终止工具")
        self._abort_tool_btn.setEnabled(False)
        self._abort_tool_btn.clicked.connect(lambda: self.abort_tool.emit())
        self._abort_tool_btn.setStyleSheet("""
            QPushButton { background: #ff9500; color: #fff; border: none;
                          border-radius: 6px; padding: 5px 10px; font-size: 11px; }
            QPushButton:hover { background: #e08400; }
            QPushButton:disabled { background: #f0f0f2; color: #bbb; }
        """)
        ctrl_row.addWidget(self._abort_tool_btn)
        # ★ 1.9.1「指令直传」新功能：在"终止工具"按钮**右边**再加一个
        #   "指令直传"按钮。点击 → 屏幕弹出 DirectDispatchWindow →
        #   选工具 + 填参数 + 点"发送" → 直接调工具系统执行，结果以
        #   「<工具直传>」首行的形象气泡回到聊天。整个流程不经过 LLM。
        self._direct_dispatch_btn = QPushButton("指令直传")
        self._direct_dispatch_btn.setToolTip("直接调用工具，跳过 LLM（结果仅显示，不入库）")
        self._direct_dispatch_btn.clicked.connect(lambda: self.direct_dispatch_request.emit())
        self._direct_dispatch_btn.setStyleSheet("""
            QPushButton { background: #5856d6; color: #fff; border: none;
                          border-radius: 6px; padding: 5px 10px; font-size: 11px; }
            QPushButton:hover { background: #4947b8; }
            QPushButton:disabled { background: #f0f0f2; color: #bbb; }
        """)
        ctrl_row.addWidget(self._direct_dispatch_btn)
        ctrl_row.addStretch()
        chat_layout.addLayout(ctrl_row)

        main_layout.addWidget(chat_container, stretch=1)

    def _bind_signals(self):
        self.stream_text_in.connect(self.append_streaming_text)
        self.system_msg_in.connect(self.append_system_msg)
        self.stream_done_in.connect(self.on_stream_done)
        self.reset_stream_in.connect(self.reset_stream)
        self.idle_message_in.connect(self._handle_idle_message)
        self.file_card_in.connect(self.append_file_card)
        self.image_preview_in.connect(self.append_image_preview)
        # ★ 1.6.0: 桌宠发来的图片 → 左侧 + 桌宠头像
        self.bot_image_in.connect(lambda p: self.append_image_preview(p, from_bot=True))
        # ★ 1.6.0: 后台线程请求滚动到指定位置
        self.scroll_to_in.connect(self.scroll_to_position)
        # ★ 1.9.1「指令直传」：主窗口把工具执行结果（纯文本）投到这里
        #   → 形象气泡显示。**只显示、不写 DB、不进 session.messages、
        #   不喂 LTM**（用户原话："不经过 LLM，不计入上下文，不入库，
        #   不计入长期记忆"）。
        self.bot_text_in.connect(lambda t: self._append_message("assistant", t))
        # ★ 1.9.1: 原来这里把 topic_list_refresh_in 转发成"刷新侧栏书签"的请求。
        #   书签删了 → 删掉这条转发；总结完成后由主窗口直接接 summary_done_in。
        # ★ 1.9.2（用户任务 4）：思考 / 系统信息 子窗口
        self.think_in.connect(self._handle_think_in)
        self.think_delta_in.connect(self._handle_think_delta_in)

    def _style_sheet(self, ui_bg: str, chat_bg: str) -> str:
        """生成主样式表（无边框圆角窗口）"""
        return f"""
            ChatWidget {{
                background: {ui_bg};
                border: 1px solid #cccccc;
                border-radius: 12px;
            }}
            QTextEdit, QTextBrowser {{
                background: {chat_bg};
                color: #222;
                border: none;
                border-radius: 8px;
                padding: 6px;
            }}
            QLineEdit {{
                background: #f2f2f5;
                border: 1px solid #e4e4e8;
                border-radius: 14px;
                padding: 7px 12px;
                font-size: 13px;
                color: #333;
            }}
            QLineEdit:focus {{
                border-color: #007aff;
                background: #ffffff;
            }}
            QScrollBar:vertical {{
                background: transparent;
                width: 8px;
                margin: 2px;
            }}
            QScrollBar::handle:vertical {{
                background: #c0c0c8;
                border-radius: 4px;
                min-height: 30px;
            }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
                height: 0px;
            }}
        """

    def _apply_style(self):
        self.setStyleSheet(self._style_sheet(
            self._personalize["ui_bg"], self._personalize["chat_bg"]))
        # ★ 1.9.1: 原来这里还给 SidebarWindow 设样式表 —— 侧栏已删（见文件头注释）。

    # ══════════════════════════════════════════
    # ★ 1.9.1: 左上角「形象名 + tokens」+ 话题按钮
    # ══════════════════════════════════════════

    @staticmethod
    def _truncate(text: str, width: int) -> str:
        """按显示宽度截断（中文算 2 列）。

        ★ 1.9.1: 原来是"保证书签不超出侧栏"用的；书签删了，但
        `append_topic_divider` 还在用它截断话题分割线的标题，所以留着。
        """
        text = (text or "").replace("\n", " ").strip()
        out, used = "", 0
        for ch in text:
            w = 2 if ord(ch) > 0x2E80 else 1
            if used + w > width:
                return out + "…"
            out += ch
            used += w
        return out

    @staticmethod
    def format_tokens(n: int) -> str:
        """★ 1.9.1（你的要求）：tokens 到一定量自动换成 k / M 单位。

        你原话："tokens单位为k和M，到一定量自动切换单位。"
        规则：<1000 原样；<100 万用 k（一位小数）；再往上用 M。
        例：0 → "0 tokens"；999 → "999 tokens"；1234 → "1.2k tokens"；
            45678 → "45.7k tokens"；1234567 → "1.2M tokens"
        """
        try:
            n = int(n or 0)
        except (TypeError, ValueError):
            n = 0
        if n < 1000:
            return f"{n} tokens"
        k = n / 1000
        # ★ 边界：999999 会四舍五入成 "1000.0k"，很难看 → 让它进位成 M
        if round(k, 1) < 1000:
            return f"{k:.1f}k tokens"
        return f"{n / 1_000_000:.1f}M tokens"

    def set_conversation_info(self, character: str = "", topic_title: str = "",
                              message_count: int = 0, token_count: int = 0):
        """★ 1.9.1: 左上角那行状态。

        你要求："聊天窗口左上角的左侧栏按钮也删掉，换成'形象名字'+'tokens计数'"。
        ★ 后续修正（你 01:4x 的批注）："刚开始时会显示'0条消息'而不是tokens。修复。"
          原来 token 为 0 时会**退化成显示消息条数**，所以刚启动看到的是
          "0 条消息"。现在**永远显示 tokens**（0 就是 "0 tokens"），
          消息条数只留在 tooltip 里，不占标题栏。
          并且 tokens 到 1000/100 万会自动换成 k / M（见 format_tokens）。
        """
        self._char_name = (character or "").strip()
        self._msg_count = int(message_count or 0)
        self._token_count = int(token_count or 0)

        parts = []
        if self._char_name:
            parts.append(self._char_name)
        # ★ 永远显示 tokens —— 不再回退成"N 条消息"
        parts.append(self.format_tokens(self._token_count))
        text = " · ".join(parts)
        if hasattr(self, "_id_label"):
            self._id_label.setText(text)
            tip = []
            if topic_title:
                tip.append("新话题" if str(topic_title).upper() == "NEW" else str(topic_title))
            if self._msg_count:
                tip.append(f"{self._msg_count} 条消息")
            tip.append(f"{self._token_count} tokens")
            self._id_label.setToolTip("当前形象与上下文用量"
                                      + ("：" + " · ".join(tip) if tip else ""))

    def paintEvent(self, event):
        """★ 1.9.0: **自己画**圆角底（聊天窗口四角圆角）。

        为什么不用样式表：`_style_sheet()` 里那句
        `ChatWidget { background: …; border-radius: 12px; }` 其实**从来没生效过** ——
        实测①自定义 Python 类名选择器在 QSS 里不匹配；②就算改成 `#id`/无选择器、
        并开 `WA_StyledBackground`，顶层自定义窗口的 QSS 背景也不会被绘制
        （以前看着"有底色"，那是窗口系统给的不透明方底）。
        所以改成自己画：透明底 + 圆角矩形，四角自然透明。
        """
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        _bg = (getattr(self, "_personalize", {}) or {}).get("ui_bg", "#ffffff")
        p.setPen(QPen(QColor("#cccccc"), 1))
        p.setBrush(QColor(_bg))
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.drawRoundedRect(r, 12, 12)
        p.end()

    # ★ 1.9.1: moveEvent / showEvent / hideEvent / closeEvent 原来都要顺手
    #   跟着定位或隐藏侧栏窗口；侧栏删了，这些钩子一并去掉。
    #   showEvent 仍然保留（下面还有别的逻辑要用），只是不再碰侧栏。

    def showEvent(self, event):
        super().showEvent(event)

    def hideEvent(self, event):
        super().hideEvent(event)

    def closeEvent(self, event):
        super().closeEvent(event)

    def set_input_blocked(self, blocked: bool):
        """阻止/允许输入（上下文窗口满时）"""
        self._input_field.setEnabled(not blocked)
        if blocked:
            self._input_field.setPlaceholderText("上下文窗口已满，正在总结旧话题...")
        else:
            self._input_field.setPlaceholderText("输入消息...")

    def set_input_blocked(self, blocked: bool):
        """阻止/允许输入（上下文窗口满时）"""
        self._input_field.setEnabled(not blocked)
        if blocked:
            self._input_field.setPlaceholderText("上下文窗口已满，正在总结旧话题...")
        else:
            self._input_field.setPlaceholderText("输入消息...")

    # ══════════════════════════════════════════
    # 焦点管理
    # ══════════════════════════════════════════

    def eventFilter(self, obj, event):
        if obj is self and event.type() == QEvent.WindowDeactivate:
            if self._hide_on_focus_out:
                QTimer.singleShot(100, self._check_focus_and_hide)
        # ★ 1.9.0: 输入框里 Ctrl+V —— 剪贴板上的图片/文件**直接发进聊天**。
        #   QLineEdit 粘贴图片什么都不会发生（它只认文本），
        #   以前必须先把图存成文件再拖进来，这一步就是你要省掉的。
        elif obj is getattr(self, "_input_field", None) and event.type() == QEvent.KeyPress:
            try:
                if event.key() == Qt.Key_V and (event.modifiers() & Qt.ControlModifier):
                    if self._try_paste_attachment():
                        return True     # 我们处理了 → 别再让 QLineEdit 粘一次
            except Exception as e:
                _log.debug(f"粘贴处理失败: {e}")
        # ★ 1.9.2（用户任务 4）：聊天区尺寸变了 → 思考面板的宽度/位置也跟着算一遍
        elif obj is getattr(self, "_chat_display", None) and event.type() == QEvent.Resize:
            try:
                if self._think_panels:
                    w = self._panel_width()
                    for _e in list(self._think_panels):
                        _e["panel"].setFixedWidth(w)
                        _e["panel"]._relayout_height()
                    self._on_think_rendered()
            except Exception as e:
                _log.debug(f"思考面板随聊天区重排失败: {e}")
        return super().eventFilter(obj, event)

    def _try_paste_attachment(self) -> bool:
        """★ 1.9.0: 尝试把剪贴板内容当附件发出去。返回 True 表示已处理。

        · 剪贴板里是**文件**（资源管理器里复制文件 → 带本地路径）
              → 走已有的上传链路。文件本来就在盘上，**我们没有额外落盘**。
        · 剪贴板里是**图片数据**（截图/网页复制图）
              → 直接发进聊天：内存里渲染，**不写磁盘**。
        · 其它（纯文本）→ 返回 False，交给 QLineEdit 正常粘贴。
        """
        cb = QApplication.clipboard()
        md = cb.mimeData()
        if md is None:
            return False
        # 1) 文件
        if md.hasUrls():
            paths = []
            for u in md.urls():
                try:
                    if u.isLocalFile():
                        p = u.toLocalFile()
                        if p and Path(p).exists():
                            paths.append(p)
                except Exception:
                    continue
            if paths:
                for p in paths:
                    _log.info(f"[粘贴] 从剪贴板发送文件: {Path(p).name}")
                    self._handle_file(p)
                return True
        # 2) 图片数据（不落盘）
        if md.hasImage():
            img = cb.image()
            if not img.isNull():
                _log.info(f"[粘贴] 从剪贴板发送图片 {img.width()}x{img.height()}（不落盘）")
                self.clip_image_pasted.emit(img)
                return True
        return False

    def _check_focus_and_hide(self):
        # ★ 1.5.0: 打开外部文件/图片查看器时抑制一次失焦隐藏，
        #    否则聊天窗口会"凭空消失"（看起来像 UI 崩溃）
        if getattr(self, "_suppress_next_hide", False):
            self._suppress_next_hide = False
            return
        # ★ 1.9.0: 再加一道**时间窗**抑制。
        #   真机现象：点形象开/关窗口时"偶尔"还会闪一下（关不掉或刚开又没）。
        #   根因：一次点击可能引发**不止一次** WindowDeactivate，而上面那个
        #   `_suppress_next_hide` 是**一次性**的 —— 第一个事件就把它用掉了，
        #   后面的事件照样把窗口藏了。时间窗能挡住窗口期内所有失焦事件。
        import time as _t
        if _t.time() < getattr(self, "_suppress_hide_until", 0.0):
            return
        if not self.isActiveWindow() and self._hide_on_focus_out:
            self.hide()

    def suppress_focus_hide(self, seconds: float = 0.6):
        """★ 1.9.0: 在接下来 seconds 秒内**忽略失焦隐藏**（点形象切换窗口时用）。

        比 `_suppress_next_hide` 稳：那个只挡一次，而一次点击可能产生多次失焦。
        """
        import time as _t
        self._suppress_next_hide = True
        self._suppress_hide_until = _t.time() + float(seconds)

    def set_hide_on_focus_out(self, enabled: bool):
        self._hide_on_focus_out = enabled

    def _open_file_external(self, file_path: str):
        """★ 1.5.0: 用系统默认程序打开文件（图片=系统图片查看器，原图仍在原位置）。
        所有打开都抑制一次失焦隐藏，避免聊天窗口"凭空消失"。

        ★ 接管（2026-09-17 22:30）：补两件事 ——
          ① **打日志**（以前这条路径一行日志都没有，出问题根本查不到）；
          ② 通知主窗口（on_external_open）—— 你实测"退出图片查看器时形象会
             一起消失"，主窗口那边会在几秒后兜底确认形象还在不在。
        """
        path = Path(file_path)
        self._suppress_next_hide = True
        _log.info(f"用系统默认程序打开: {path}")
        try:
            if os.name == "nt":
                os.startfile(str(path))
            else:
                subprocess.run(["xdg-open", str(path)])
        except Exception as e:
            _log.warning(f"打开文件失败: {e}")
            self._suppress_next_hide = False
            return
        cb = getattr(self, "on_external_open", None)
        if callable(cb):
            try:
                cb(str(path))
            except Exception as e:
                _log.debug(f"外部打开回调失败: {e}")

    def _on_chat_menu(self):
        """★ 1.5.0: ≡ 菜单 — 显示/隐藏、设置、退出（托盘不可用时也能访问）"""
        from PySide6.QtWidgets import QMenu
        menu = QMenu(self)
        a_toggle = menu.addAction("显示 / 隐藏桌面宠物")
        a_set = menu.addAction("设置")
        a_quit = menu.addAction("退出")
        action = menu.exec_(self._menu_btn.mapToGlobal(
            self._menu_btn.rect().bottomLeft()))
        if action == a_toggle and callable(self.on_toggle):
            self.on_toggle()
        elif action == a_set and callable(self.on_settings):
            self.on_settings()
        elif action == a_quit and callable(self.on_quit):
            self.on_quit()

    # ══════════════════════════════════════════
    # 文件处理
    # ══════════════════════════════════════════

    def _on_upload_file(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self, "选择文件", "",
            "所有文件 (*.*);;图片 (*.png *.jpg *.jpeg *.gif *.webp);;文档 (*.txt *.pdf *.md);;代码 (*.py *.js *.ts *.cpp)")
        if file_path:
            self._handle_file(file_path)

    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dropEvent(self, event: QDropEvent):
        urls = event.mimeData().urls()
        for url in urls:
            file_path = url.toLocalFile()
            if file_path:
                self._handle_file(file_path)
        event.acceptProposedAction()

    def handle_external_drop(self, file_path: str):
        """外部拖拽（从形象拖入）"""
        self._handle_file(file_path)

    def _handle_file(self, file_path: str):
        path = Path(file_path)
        if not path.exists():
            return
        file_size = path.stat().st_size
        file_name = path.name
        ext = path.suffix.lower()

        # ★ 1.8.0: 任何文件都可发送（与图片同级体验）。
        #   >50MB 拒收；>10MB 只发卡片+元信息，不读内容。
        if file_size > 50 * 1024 * 1024:
            self.append_system_msg(f"[文件过大: {file_name} ({file_size/1024/1024:.1f}MB > 50MB)]")
            return
        size_str = (f"{file_size/1024/1024:.1f}MB" if file_size >= 1024 * 1024
                    else f"{file_size/1024:.1f}KB")

        # 图片 → 视觉识别（原链路：预览 + 多模态直传）
        if ext in {'.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp'}:
            self.append_image_preview(file_path, from_bot=False)
            preview = f"[图片文件: {file_name}, 大小: {file_size/1024:.1f}KB, 尺寸: {self._get_image_size(file_path)}]"
            self.file_uploaded.emit(file_name, preview)
            _log.info(f"图片上传: {file_name}")
            self.image_recognize_request.emit(file_path)
            self._abort_chat_btn.setEnabled(True)
            self._abort_tool_btn.setEnabled(True)
            return

        # 纯文本：卡片 + 内容预览随消息直发（带绝对路径，LLM 需要时可再读全文）
        text_extensions = {'.txt', '.md', '.py', '.js', '.ts', '.cpp', '.c', '.h', '.java',
                           '.json', '.xml', '.yaml', '.yml', '.toml', '.cfg', '.ini', '.log',
                           '.css', '.html', '.csv'}
        if ext in text_extensions and file_size <= 10 * 1024 * 1024:
            try:
                content = path.read_text(encoding='utf-8', errors='ignore')
                preview = content[:2000]
                if len(content) > 2000:
                    preview += f"\n\n... (文件共 {len(content)} 字符，已截断)"
            except Exception as e:
                preview = f"[读取失败: {e}]"
            self.file_uploaded.emit(file_name, preview)
            # ★ 1.8.4: 卡片做成标记写进消息正文 —— 气泡里"卡片 + 文字"同属一条
            #   消息、共用同一个头像（QQ 式），而且会随消息落库，重启后还在。
            marker = self.file_marker(path.resolve(), size_str, "用户上传")
            shown = f"{marker}\n[上传文件: {file_name}]\n{preview}"
            self._append_message("user", shown)
            send_text = (f"{marker}\n[用户上传了文件: {file_name}]\n路径: {path.resolve()}\n"
                         f"大小: {size_str}\n文件内容预览:\n{preview}")
            self.send_message.emit(send_text)
            self._abort_chat_btn.setEnabled(True)
            self._abort_tool_btn.setEnabled(True)
            return

        # 其余任何文件（音频/视频/PDF/Word/PPT/Excel/压缩包/任意二进制）：
        # 先显示文件卡片，再交给主窗口后台处理
        # （音频转写 / 视频分析 / 文档抽取全文 / 元信息+路径发给 LLM，
        #   LLM 需要时可调用 file_ops 的 read 自行查看）
        self.append_file_card("用户上传", str(path.resolve()), size_str)
        self.file_uploaded.emit(file_name, f"[文件: {file_name}, 大小: {size_str}, 类型: {ext}]")
        _log.info(f"文件上传(交主窗口处理): {file_name} ({ext})")
        self.file_upload_request.emit(str(path))
        self._abort_chat_btn.setEnabled(True)
        self._abort_tool_btn.setEnabled(True)

    # ══════════════════════════════════════════
    # 聊天消息（QQ 风格气泡 + 头像）
    # ══════════════════════════════════════════

    def _on_send(self):
        text = self._input_field.text().strip()
        if not text:
            return
        self._input_field.clear()
        self._append_message("user", text)
        self._abort_chat_btn.setEnabled(True)
        self._abort_tool_btn.setEnabled(True)
        self.send_message.emit(text)

    @staticmethod
    def _is_transparent(color: str) -> bool:
        """判断颜色是否为“无底色”（transparent / 空 / none）"""
        return (not color) or color.strip().lower() in ("transparent", "none", "null")

    def _bubble_style(self, color: str, text_color: str) -> str:
        """文本容器样式（只负责文字）。
        ★ 1.9.0: 底色/内边距/圆角已挪到 _bubble_td_style（内层 td）——
        写在 <span> 上时 Qt 只按每行的文字范围刷底色，换行后是锯齿状背景，
        不像 QQ 那种规整气泡。color 形参保留，兼容旧调用。"""
        return f"color:{text_color}; font-size:13px; line-height:1.5;"

    def _bubble_td_style(self, color: str) -> str:
        """★ 1.9.0: 气泡内边距（**底色不再由 HTML 画**）。

        原因：Qt 富文本不支持 border-radius，而且它给嵌套表格画底色的位置
        与 frameBoundingRect() 差着 (6,2) px（实测），"遮角"既不可靠也不精确。
        所以底色改由 _RoundedBubbleBrowser 在绘制层画圆角矩形（文字之前），
        这里只留内边距，给文字留出空间。
        无底色（transparent）时返回空串：正文直接左对齐、不加内边距。
        """
        if self._is_transparent(color):
            return ""
        return f"padding:{BUBBLE_PAD_Y}px {BUBBLE_PAD_X}px;"

    def _avatar_html(self, is_bot: bool = False) -> str:
        """当前头像的 <img> 标签（圆形 data URI）"""
        uri = self._avatar_uri["bot" if is_bot else "user"]
        s = AVATAR_DISPLAY_SIZE
        return f'<img src="{uri}" width="{s}" height="{s}">'

    # ─── Markdown 图片内联解析（★ 1.5.0: LLM 发来的图片与用户上传同款预览） ───
    # URL 允许含空格（Windows 路径常见），仅以 ")" 结束；alt 不允许 "]"
    _MD_IMG_RE = __import__("re").compile(r'!\[([^\]]*)\]\(([^)]+)\)')

    # ★ 1.8.4: 文件卡片标记 —— 随消息一起落库，重启后还能重建成卡片。
    #   格式 [[FILE:路径|大小|标签]]；Windows 路径不允许含 "|"，所以分隔安全。
    #   ★ 1.9.0 修正：路径里**可以**含 "]"（例如 JM 的本子名 "…[欶澜汉化组](C108)…"），
    #   旧正则 `([^|\]]+)` 会在第一个 "]" 处把路径截断 → 重启后
    #   `p.exists()` 为假 → 卡片变成"[文件已不在]"（你反馈的"JM 的 PDF 重启后消失"，
    #   而 B站那种不含方括号的文件名就没事，正好对上）。
    #   现在：路径取到第一个 "|"（Windows 文件名不可能含 "|"），标签取到 "]]"。
    _FILE_MARKER_RE = re.compile(r'\[\[FILE:(.+?)\|([^|]*)\|([^\n]*?)\]\]')

    @staticmethod
    def file_marker(path, size: str = "", label: str = "") -> str:
        """生成「文件卡片」标记文本（放进消息正文里一起持久化）"""
        return f"[[FILE:{str(path)}|{size or ''}|{label or ''}]]"

    @classmethod
    def _split_file_markers(cls, text: str):
        """把文本里的 [[FILE:...]] 拆出来，返回 (剩余文本, [(path,size,label),...])"""
        files = []

        def _collect(m):
            files.append((m.group(1), m.group(2), m.group(3)))
            return "\u0000FILE\u0000"

        clean = cls._FILE_MARKER_RE.sub(_collect, text)
        return clean, files

    # ★ 1.9.0: 剪贴板图片标记 —— 像素**不落盘**，只在内存里（见 append_clip_image）
    _CLIP_MARKER_RE = re.compile(r'\[\[CLIP:([^|\]]+)\|([^|\]]*)\|([^\]]*)\]\]')

    @classmethod
    def _split_clip_markers(cls, text: str):
        """把 [[CLIP:key|名字|尺寸]] 拆出来 → (剩余文本, [(key,名字,尺寸),...])"""
        clips = []

        def _collect(m):
            clips.append((m.group(1), m.group(2), m.group(3)))
            return "\u0000CLIP\u0000"

        clean = cls._CLIP_MARKER_RE.sub(_collect, text)
        return clean, clips

    @classmethod
    def _split_inline_images(cls, text: str):
        """把文本里的 markdown 图片 ![alt](url) 拆出来，返回 (剩余文本, [(alt,url),...])。
        保证流式重渲染时解析结果稳定（按出现顺序）。"""
        images = []
        def _collect(m):
            images.append((m.group(1), m.group(2)))
            return "\u0000IMG\u0000"
        clean = cls._MD_IMG_RE.sub(_collect, text)
        return clean, images

    def _render_inline_image_html(self, url: str, alt: str = "") -> str:
        """把一张图片（本地文件优先）渲染为 QQ 风格缩略图（左键系统查看器 / 右键复制路径）。
        远程 URL 退化为可点击文字链接，避免 QTextBrowser 无法加载。"""
        import urllib.parse as _up
        raw = url.strip()
        path = None
        if raw.startswith("file://"):
            parsed = _up.urlparse(raw)
            p = _up.unquote(parsed.path)
            if len(p) >= 3 and p[0] == '/' and p[2] == ':':
                p = p[1:]
            elif len(p) >= 4 and p[0:2] == '//' and p[3] == ':':
                p = p[2:]
            path = Path(p)
        elif raw.startswith("http://") or raw.startswith("https://"):
            return (f'<a href="{_up.quote(raw, safe=":/?=&#")}" style="color:#007aff; '
                    f'font-size:12px; text-decoration:none;">🔗 {alt or "图片"}</a>')
        else:
            # 本地绝对/相对路径
            cand = Path(raw)
            if not cand.is_absolute():
                cand = (_BASE_DIR / raw).resolve()
            path = cand

        if path is None or not path.exists():
            return (f'<span style="color:#999; font-size:11px;">[图片不可用: '
                    f'{_up.quote(alt or raw, safe="")}]</span>')

        # 与用户上传一致的缩略图（file:/// 原图路径）
        file_uri = path.as_uri()
        try:
            from PIL import Image as _PILImage
            with _PILImage.open(path) as _img:
                w, h = _img.size
            max_w = 260
            if w > max_w:
                tw, th = max_w, int(h * max_w / w)
            else:
                tw, th = w, h
        except Exception:
            tw, th = 200, 120
        disp = (alt or path.name).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        esc_uri = file_uri.replace("&", "&amp;")
        return (
            f'<a href="{esc_uri}" style="text-decoration:none; cursor:pointer;" '
            f'title="{disp} · 左键查看原图，右键复制路径">'
            f'<img src="{esc_uri}" width="{tw}" height="{th}" '
            f'style="border-radius:8px; cursor:pointer; margin:2px 0;"></a>')

    def _render_message_html(self, role: str, text: str) -> str:
        """生成一条消息的 HTML（表格布局保证左右分列与换行可靠）。
        ★ 1.5.0: 支持 LLM 返回的 markdown 图片 ![alt](url) → 内联缩略图。"""
        p = self._personalize
        clean_text, images = self._split_inline_images(text)
        clean_text, files = self._split_file_markers(clean_text)
        clean_text, clips = self._split_clip_markers(clean_text)
        # 干掉占位符，别让它落到屏幕上
        clean_text = (clean_text.replace("\u0000IMG\u0000", "")
                      .replace("\u0000FILE\u0000", "")
                      .replace("\u0000CLIP\u0000", ""))
        escaped = clean_text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")\
            .replace("\r\n", "<br>").replace("\r", "<br>").replace("\n", "<br>")
        av = AVATAR_DISPLAY_SIZE
        col_w = av + 14

        # 图片缩略图块（无论 user/assistant，统一内联在气泡上方）
        img_html = ""
        if images:
            parts = [self._render_inline_image_html(u, a) for a, u in images]
            img_html = ('<div style="margin:2px 0;">' + "".join(parts) + '</div>')

        # ★ 1.9.0: 剪贴板图片（[[CLIP:key|名字|尺寸]]）—— 像素只在内存里：
        #   本次会话内能显示（把资源重新注册一遍），重启后显示占位提示。
        if clips:
            _cparts = []
            for _key, _name, _dims in clips:
                _qimg = self._clip_images.get(_key)
                if _qimg is not None:
                    try:
                        self._chat_display.document().addResource(
                            QTextDocument.ImageResource, QUrl(f"clip://{_key}"), _qimg)
                    except Exception:
                        pass
                    _w, _h = _qimg.width(), _qimg.height()
                    if _w > 260:
                        _h = int(_h * 260 / _w)
                        _w = 260
                    _cparts.append(f'<img src="clip://{_key}" width="{_w}" height="{_h}" '
                                   f'style="border-radius:8px; margin:2px 0;">')
                else:
                    _cparts.append(
                        f'<span style="color:#999; font-size:11px;">'
                        f'[粘贴的图片「{_name or _dims}」：未落盘，重启后不可预览]</span>')
            img_html += '<div style="margin:2px 0;">' + "".join(_cparts) + '</div>'

        # ★ 1.8.4: 文件卡片块 —— 和图片一样贴在发送方气泡里，
        #   这样"一条消息一个头像"，重启后也能从标记重建
        card_html = ""
        if files:
            cards = []
            for fpath, fsize, flabel in files:
                try:
                    p = Path(fpath)
                    # ★ 1.9.0: 目录不渲染成卡片。历史库里可能还留着旧版本
                    #   （jmcomic 刚开始下载时把 save_dir 当附件）写下的目录行，
                    #   这里直接跳过，用户就不会再看到那张"download 文件夹"卡片。
                    if p.is_dir():
                        continue
                    if p.exists():
                        cards.append(self.file_card_html(flabel, p, fsize,
                                                         *self.card_size_px()))
                    else:
                        cards.append(
                            f'<span style="color:#999;font-size:11px;">'
                            f'[文件已不在: {Path(fpath).name}]</span>')
                except Exception as e:
                    _log.debug(f"重建文件卡片失败: {e}")
            if cards:
                card_html = ('<div style="margin:2px 0;">' + "".join(cards) + '</div>')

        # ★ 1.9.0: 气泡几何重做（数字见《UI几何模型.txt》）
        #   外层 td 只负责「把气泡块推到头像这一侧」；真正装内容的是
        #   内层无 width 的 table：
        #     · Qt 富文本里 div 的 width/max-width 全被忽略，只有 table
        #       会按内容自适应（1.8.2 实测结论）→ 短消息得到短气泡；
        #     · 内层 td 用 align="left" → 气泡内**所有行**都齐左，修掉以前
        #       <td align="right"> 连文字对齐一起管、续行右对齐（左边参差）；
        #     · 底色/内边距写在内层 td 上 → 规整矩形，不再是每行一段的锯齿背景。
        #   气泡最大可用宽 = 聊天内容 400 − 头像列 48 ≈ 352px（外层自动约束）。
        text_span = f'<span style="{self._bubble_style_for(role)}">{escaped}</span>'
        payload = img_html + card_html + text_span
        side = "right" if role == "user" else "left"
        td_style = self._bubble_td_style(self._bubble_color_for(role))
        bubble = (f'<table cellspacing="0" cellpadding="0" align="{side}">'
                  f'<tr><td align="left" valign="top" style="{td_style}">{payload}</td></tr>'
                  f'</table>')

        if role == "user":
            return (
                f'<table width="100%" cellspacing="0" cellpadding="3" style="margin:6px 0;">'
                f'<tr>'
                f'<td align="right" valign="top">{bubble}</td>'
                f'<td width="{col_w}" align="right" valign="top">{self._avatar_html(False)}</td>'
                f'</tr></table>')
        if role == "assistant":
            return (
                f'<table width="100%" cellspacing="0" cellpadding="3" style="margin:6px 0;">'
                f'<tr>'
                f'<td width="{col_w}" align="left" valign="top">{self._avatar_html(True)}</td>'
                f'<td align="left" valign="top">{bubble}</td>'
                f'</tr></table>')
        # system
        return (
            f'<div style="margin:3px 0; text-align:center;">'
            f'<span style="color:#999; font-size:10px; '
            f'background:#ececf0; padding:3px 12px; border-radius:10px;">{escaped}</span>'
            f'</div><br>')

    def _bubble_color_for(self, role: str) -> str:
        """★ 1.9.0: 按角色取气泡底色（供 _bubble_td_style 用）"""
        p = getattr(self, "_personalize", {}) or {}
        if role == "user":
            return p.get("user_bubble", "transparent")
        if role == "assistant":
            return p.get("bot_bubble", "transparent")
        return "transparent"

    def _bubble_style_for(self, role: str) -> str:
        """按角色取气泡文字样式（user/assistant），system 走默认"""
        p = getattr(self, "_personalize", {}) or {}
        if role == "user":
            return self._bubble_style(p.get("user_bubble", "transparent"),
                                      p.get("user_text", "#222222"))
        if role == "assistant":
            return self._bubble_style(p.get("bot_bubble", "transparent"),
                                      p.get("bot_text", "#222222"))
        return "color:#333; font-size:13px;"

    def append_clip_image(self, qimage, name: str = "") -> str:
        """★ 1.9.0: 把剪贴板里的图片**直接**发进聊天（**不落盘**）。

        做法：`QTextDocument.addResource(ImageResource, "clip://<key>", QImage)`
        把图片注册成**内存资源**，正文里放 `[[CLIP:key|名字|尺寸]]` 标记指向它。
        所以从"粘贴"到"聊天里看到图"全程没碰磁盘。

        ⚠ 代价（已在开发日志里写明）：像素只在内存里，**重启后预览会变成
        占位提示**"[粘贴的图片：未落盘]"；但图片的**文字描述**会随消息落库，
        所以"这张图里有什么"这件事在历史里仍然查得到。

        返回标记文本，调用方拼进要发给 LLM 的消息正文里。
        """
        import itertools as _it
        import time as _time
        # ★ 1.9.2（内存调研结论）：三个问题一起修 ——
        #   ① 原来 key 是 `int(time.time()*1000)`，实测 10 次粘贴只产生 8 个 key
        #      （同一毫秒内撞车 → 后面的把前面的**覆盖**掉，图就丢了）。
        #      改成"毫秒 + 自增序号"，保证唯一。
        #   ② `_clip_images` 无上限：实测每张 1920×1080 的 QImage 约 6.5 MB，
        #      粘 10 张就 +64.7 MB，且只在 clear_chat 才清 → 加 LRU 上限。
        #   ③ 粘贴图按原分辨率存着太奢侈：聊天里最多显示 260px 宽，
        #      存前先缩到 ≤MAX_CLIP_SIDE，单张从 6.5 MB 降到 ~1.5 MB。
        key = f"{int(_time.time() * 1000)}-{next(self._clip_seq)}"
        try:
            _w, _h = qimage.width(), qimage.height()
            if max(_w, _h) > MAX_CLIP_SIDE:
                qimage = qimage.scaled(
                    MAX_CLIP_SIDE if _w >= _h else int(_w * MAX_CLIP_SIDE / _h),
                    int(_h * MAX_CLIP_SIDE / _w) if _w >= _h else MAX_CLIP_SIDE,
                    Qt.KeepAspectRatio, Qt.SmoothTransformation)
        except Exception as e:
            _log.debug(f"粘贴图缩放失败（按原图存）: {e}")
        self._clip_images[key] = qimage          # 持引用，别让 QImage 被回收
        # 超上限就丢最旧的（dict 保序 → 第一个就是最旧）
        while len(self._clip_images) > MAX_CLIP_IMAGES:
            _old = next(iter(self._clip_images))
            self._clip_images.pop(_old, None)
        try:
            self._chat_display.document().addResource(
                QTextDocument.ImageResource, QUrl(f"clip://{key}"), qimage)
        except Exception as e:
            _log.warning(f"注册内存图片失败: {e}")
        # 按"用户侧"插一行（右侧 + 用户头像），与图片预览同款布局
        try:
            av = AVATAR_DISPLAY_SIZE
            w, h = qimage.width(), qimage.height()
            max_w = 260
            if w > max_w:
                h = int(h * max_w / w)
                w = max_w
            img_tag = (f'<img src="clip://{key}" width="{w}" height="{h}" '
                       f'style="border-radius:8px;">')
            row = (f'<td align="right" valign="top">{img_tag}</td>'
                   f'<td width="{av + 14}" align="right" valign="top">'
                   f'{self._avatar_html(False)}</td>')
            self._chat_display.moveCursor(QTextCursor.End)
            self._chat_display.insertHtml(
                f'<table width="100%" cellspacing="0" cellpadding="3" style="margin:6px 0;">'
                f'<tr>{row}</tr></table>')
            self._chat_display.moveCursor(QTextCursor.End)
            sb = self._chat_display.verticalScrollBar()
            sb.setValue(sb.maximum())
        except Exception as e:
            _log.warning(f"显示粘贴图片失败: {e}")
        return f"[[CLIP:{key}|{name or '粘贴的图片'}|{qimage.width()}x{qimage.height()}]]"

    def _record_bubble(self, start: int, end: int, role: str):
        """★ 1.9.0: 登记这条消息的气泡区间与角色（供自绘圆角用）。

        底色不由 HTML 画，所以绘制层必须自己知道"哪一段是气泡、什么颜色"。
        无底色（transparent）的消息不登记 —— 没有气泡可画。
        """
        try:
            color = self._bubble_color_for(role)
            if self._is_transparent(color) or end <= start:
                return
            self._chat_display.bubble_spans.append(
                {"start": int(start), "end": int(end), "role": role, "color": color})
        except Exception as e:
            _log.debug(f"登记气泡区间失败: {e}")

    def _append_message(self, role: str, text: str):
        """插入一条消息（★ 1.9.0: 同时登记气泡区间）"""
        # ★ 1.9.2: 桌宠正文出现 = 本轮结束 → 把本轮的思考面板封口，
        #   下一轮第一次思考写入时会自动开一个新面板（"每轮都出现"的关键）
        if role == "assistant":
            self._seal_think_panel()
        self._chat_display.moveCursor(QTextCursor.End)
        _start = self._chat_display.textCursor().position()
        self._chat_display.insertHtml(self._render_message_html(role, text))
        self._chat_display.moveCursor(QTextCursor.End)
        self._record_bubble(_start, self._chat_display.textCursor().position(), role)

    def append_streaming_text(self, text: str):
        """流式文本（桌宠风格，无底色）"""
        self._stream_buffer += text
        if self._last_role != "assistant":
            self._last_role = "assistant"
            # ★ 1.9.2: 正文开始流式输出 = 本轮思考结束 → 封口，下一轮另开一个面板
            self._seal_think_panel()
            self._chat_display.moveCursor(QTextCursor.End)
            self._stream_start = self._chat_display.textCursor().position()
            # ★ 1.9.0: 流式这条只登记一次区间，之后每帧更新 end
            #   （流式内容永远在文档末尾，前面的位置不会被顶动）
            self._stream_span = None
            try:
                _c = self._bubble_color_for("assistant")
                if not self._is_transparent(_c):
                    self._stream_span = {"start": self._stream_start,
                                         "end": self._stream_start,
                                         "role": "assistant", "color": _c}
                    self._chat_display.bubble_spans.append(self._stream_span)
            except Exception:
                self._stream_span = None

        cursor = self._chat_display.textCursor()
        cursor.movePosition(QTextCursor.End)
        end_pos = cursor.position()
        cursor.setPosition(self._stream_start)
        cursor.setPosition(end_pos, QTextCursor.KeepAnchor)
        cursor.removeSelectedText()

        cursor.insertHtml(self._render_message_html("assistant", self._stream_buffer))
        if getattr(self, "_stream_span", None) is not None:
            self._stream_span["end"] = cursor.position()

        sb = self._chat_display.verticalScrollBar()
        sb.setValue(sb.maximum())

    def append_system_msg(self, text: str):
        """★★ 1.9.2（用户 22:34 批注）：系统消息**不再在聊天区画「灰底白字」气泡**，
        一律进「思考 / 系统信息」子窗口。

        你原话："成功了，但是我看见系统消息还在外面，哪些灰低白字可以全删了。
        另外，调用工具前的思考也要显示到这个窗口，所有的思考，所有的系统信息。"

        做法：**只改去向，不改方法名** —— 全项目有 35 处调用
        （工具调用/工具返回/转写/语音/图片识别/文件提示…），
        在这里统一改投面板，那 35 处一行都不用动，也就不会漏掉某一类系统信息。
        于是"所有的系统信息"自然都在面板里了。
        """
        if text is None or not str(text).strip():
            return
        try:
            self.think_write(str(text), "system")
        except Exception as e:
            _log.debug(f"系统消息改投思考面板失败: {e}")

    def on_stream_done(self):
        self._last_role = ""
        self._stream_buffer = ""
        self._stream_start = 0
        self._abort_chat_btn.setEnabled(False)
        self._abort_tool_btn.setEnabled(False)

    def _handle_idle_message(self, text: str):
        self._append_message("assistant", text)

    # ══════════════════════════════════════════
    # 工具追踪
    # ══════════════════════════════════════════

    def append_tool_trace(self, icon: str, label: str, detail: str = ""):
        detail_html = ""
        if detail:
            detail_html = f'<br><span style="color:#888;font-size:10px;">{detail}</span>'
        self._chat_display.moveCursor(QTextCursor.End)
        escaped_icon = icon.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        escaped_label = label.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        self._chat_display.insertHtml(
            f'<div style="margin:4px 0; padding:6px 10px; background:#f0f0f5; '
            f'border-left:3px solid #007aff; border-radius:4px;">'
            f'<span style="color:#007aff;font-size:11px;font-weight:bold;">{escaped_icon} {escaped_label}</span>'
            f'{detail_html}</div><br>')
        self._chat_display.moveCursor(QTextCursor.End)
        sb = self._chat_display.verticalScrollBar()
        sb.setValue(sb.maximum())

    # ══════════════════════════════════════════
    # ★ 1.9.2（用户任务 4）：消息流里的「思考 / 系统信息」子窗口
    # ══════════════════════════════════════════
    #
    # 做法：在文档末尾插一张**占位图**（1×1 透明），真面板是
    # `_chat_display.viewport()` 的子控件，按占位图所在的 blockRect 定位。
    # 于是它天然"嵌在消息流里、跟着一起滚"，同时因为是真控件，
    # 内部能挂自己的 QScrollArea（内层独立滚动）。
    #
    # 高度变化时改占位图的 height（QTextImageFormat），文档会自动重排，
    # 不需要重画整段 HTML。
    #
    # ★★ 1.9.2 修订（用户 22:05 的批注）：「我要的不是只有一个，是每次
    #    "用户信息" + "思考/系统信息子窗口" + "LLM回复（包括文件，表情包）"，
    #    如果没做完，那么后面继续 + "思考/系统信息子窗口" + "LLM回复"」
    #    —— 原实现在整个会话里**只建了一个面板**并一直复用（永远停在文档末尾），
    #    所以用户"只看到一眼，后面就再也每出现过了"。
    #    现在改成**每轮一个**：
    #        用户消息 → [本轮面板 A] → LLM 回复
    #                  → [下一轮面板 B] → LLM 回复 …
    #    · 新面板在"本轮第一次思考/系统写入"时按需创建，插在当前文档末尾；
    #    · LLM 的**正文**开始流式输出 = 本轮结束 → 封口（seal）；下一次思考
    #      写入会自动开一个新面板；
    #    · 于是"没做完就继续"天然成立（工具轮之间会再开一个面板）；
    #    · 只保留最近 MAX_THINK_PANELS 个（旧的连占位图一起摘掉），
    #      不让它随会话无限增长。
    #
    # ★ 命名而不是存"位置"：每个占位图用**唯一资源名** prts-think-<seq>，
    #   找 anchor 时按名字扫文档。因为一旦摘掉旧占位图，后面所有字符位置都会
    #   平移，存下标必然错位（这是踩过的坑）。

    _THINK_SEQ = 0
    # ★ 1.9.2: 思考面板占位图的宽度固定 1px —— 它只需要占高度。
    #   设宽了会把 QTextDocument 的理想宽度撑过可视区，启动时聊天记录
    #   会被横向挤出屏幕（真机报的"UI 左右错位，开启对话后恢复"）。
    THINK_ANCHOR_W = 1

    # ── ★★ 1.9.2 崩溃修复：面板生命周期安全 ─────────────────────
    #
    # 真机事故（用户 23:06）："开启新话题 → 发送你好 → 聊天窗口消失、形象还在
    # 但点不开、小托盘退出、终端高频报错"。日志证据：
    #     [ChatWidget] DEBUG: 思考面板定位失败: 'NoneType' object has no attribute 'isValid'
    # 主进程日志在那一刻**戛然而止**，既没有"退出完成"也没有 Python 崩溃记录
    # → 静默硬崩溃（Qt 里访问**已被 deleteLater 销毁的 C++ 对象**就是这个症状）。
    #
    # 我这块代码原来有两个致命疏忽：
    #   ① `_find_think_block()` 找不到时会**返回 None**，而调用处一律直接
    #      `blk.isValid()` → AttributeError（上面那条日志就是它）；
    #   ② "开启新话题"会 `deleteLater()` 掉旧面板，但 deleteLater 是**延迟销毁**，
    #      而面板内部还有一个 80ms 的 `_flush_timer` 可能稍后触发、
    #      `_on_think_rendered` / `think_flush` 也可能去碰那块已释放的内存。
    #      —— 碰死对象在 PySide6 里往往是**直接访问违例**，不给你 Python 堆栈。
    #
    # 现在三条硬规矩：
    #   A. 找 block 失败返回**无效 block**（不是 None），调用处只需查 isValid()；
    #   B. 每次用面板之前先 `_panel_alive()` 校验（shiboken6.isValid），
    #      发现已销毁就把条目摘掉，绝不再碰；
    #   C. 拆面板时**先停它的节拍器、断开钩子、隐藏、脱离父控件**，再 deleteLater，
    #      顺序错了就可能留下"定时器还在、对象已死"的窗口。

    @staticmethod
    def _alive(obj) -> bool:
        """Qt 对象还活着吗（deleteLater 之后 C++ 侧可能已经没了）。"""
        if obj is None:
            return False
        try:
            import shiboken6
            return bool(shiboken6.isValid(obj))
        except Exception:
            # 拿不到 shiboken 就退回"属性可访问即算活"（比不判强）
            try:
                obj.objectName()
                return True
            except Exception:
                return False

    def _prune_think_panels(self):
        """把所有已销毁的面板条目摘掉（每次操作面板前先跑一遍）。"""
        dead = [e for e in self._think_panels if not self._alive(e.get("panel"))]
        for e in dead:
            try:
                self._think_panels.remove(e)
            except ValueError:
                pass
            if self._think_current is e:
                self._think_current = None
        if dead:
            _log.debug(f"[思考面板] 摘掉 {len(dead)} 个已销毁的条目")
        return dead

    def _find_think_block(self, key: str):
        """按占位图的资源名找它所在的 block（不依赖字符位置，抗平移）。

        ★ 找不到时返回**无效 block**（`QTextBlock()`），绝不返回 None ——
          调用处习惯写 `blk.isValid()`，返回 None 会直接 AttributeError
          （真机上就是这么炸出一条 debug 日志的）。
        """
        try:
            doc = self._chat_display.document()
            blk = doc.begin()
            while blk.isValid():
                it = blk.begin()
                while not it.atEnd():
                    f = it.fragment()
                    if f.isValid() and f.charFormat().isImageFormat():
                        if f.charFormat().toImageFormat().name() == key:
                            return blk
                    it += 1
                blk = blk.next()
        except Exception as e:
            _log.debug(f"找思考面板占位图失败: {e}")
        return QTextBlock()                     # ★ 无效 block，不是 None

    def _find_think_pos(self, key: str) -> int:
        """找占位图那个**图片字符**的文档位置；找不到返回 -1。

        ★ 为什么要这个：定位面板不能用 `blockBoundingRect(block)` —— 那个矩形
        把 block 的前导行高也算进去了，于是面板会被向上偏大约一行，
        真机上就是用户报的"**子窗口的上边沿会部分遮挡消息气泡**"。
        改成拿图片字符自己的 `cursorRect()`：它是**视口坐标**，
        既精确到图片本身，又天然带滚动偏移（不用再手动减滚动条）。
        """
        blk = self._find_think_block(key)
        if not blk.isValid():
            return -1
        try:
            it = blk.begin()
            while not it.atEnd():
                f = it.fragment()
                if f.isValid() and f.charFormat().isImageFormat() \
                        and f.charFormat().toImageFormat().name() == key:
                    return f.position()
                it += 1
        except Exception as e:
            _log.debug(f"找思考面板占位图位置失败: {e}")
        return -1

    def _dispose_think_panel(self, panel):
        """**安全地**销毁一个 ThinkPanel（顺序很重要，见上面 A/B/C 三条）。

        必须先停它内部的节拍器：`deleteLater()` 是延迟销毁，若那个 80ms 的
        `_flush_timer` 在被销毁之后触发，就是"定时器还在、对象已死" →
        访问违例（真机那次静默硬崩溃最像这个）。
        """
        if not self._alive(panel):
            return
        try:
            panel.stop_timers()                 # 停节拍器（think_panel 提供）
        except Exception as e:
            _log.debug(f"停思考面板节拍器失败: {e}")
        try:
            panel.set_render_hook(None)          # 断开宿主回调，别让它回来找我们
        except Exception:
            pass
        try:
            panel.hide()
        except Exception:
            pass
        try:
            panel.setParent(None)               # 脱离 Qt 父子树，避免父控件销毁时连带操作
        except Exception:
            pass
        try:
            panel.deleteLater()
        except Exception:
            pass

    def _new_think_panel(self):
        """开一轮新的思考面板（占位图插在文档末尾）。返回 panel。"""
        from src.ui.think_panel import ThinkPanel
        from PySide6.QtGui import QImage, QTextImageFormat

        ChatWidget._THINK_SEQ += 1
        key = f"prts-think-{ChatWidget._THINK_SEQ}"
        panel = ThinkPanel(self._chat_display.viewport())
        panel.hide()
        panel.set_render_hook(self._on_think_rendered)

        doc = self._chat_display.document()
        pm = QImage(1, 1, QImage.Format_ARGB32)
        pm.fill(0)
        doc.addResource(QTextDocument.ImageResource, QUrl(key), pm)

        self._chat_display.moveCursor(QTextCursor.End)
        # ★★ 1.9.2：占位图**独占一个 block**（用 insertBlock 而不是插一个 <br>）。
        #   这样 `blockBoundingRect(blk)` 就精确等于图片本身的矩形 ——
        #   既没有"含前导行高、把面板向上偏一行遮住气泡"的问题，
        #   又能用文档坐标定位（不会被 cursorRect 的视口钳制坑到）。
        self._chat_display.textCursor().insertBlock()
        self._chat_display.moveCursor(QTextCursor.End)
        fmt = QTextImageFormat()
        fmt.setName(key)
        # ★ 1.9.2：占位图只负责"占高度"，宽度固定 1px。
        #   以前设成 viewport-8，会把 QTextDocument 的理想宽度撑过可视区 →
        #   启动时聊天记录被横向挤出屏幕（真机报的"UI 左右错位"）。
        fmt.setWidth(self.THINK_ANCHOR_W)
        fmt.setHeight(panel.desired_height())
        self._chat_display.textCursor().insertImage(fmt)
        self._chat_display.moveCursor(QTextCursor.End)

        entry = {"key": key, "panel": panel, "fmt": fmt}
        self._think_panels.append(entry)
        self._think_current = entry
        # 超量 → 把最旧的连占位图一起摘掉（用户只看得到最近几轮）
        while len(self._think_panels) > self.MAX_THINK_PANELS:
            self._drop_think_panel(self._think_panels[0])
        return panel

    def _drop_think_panel(self, entry):
        """摘掉一个旧面板：删占位图字符 + 销毁控件。"""
        try:
            blk = self._find_think_block(entry["key"])
            if blk.isValid():
                cur = QTextCursor(blk)
                it = blk.begin()
                while not it.atEnd():
                    f = it.fragment()
                    if f.isValid() and f.charFormat().isImageFormat() \
                            and f.charFormat().toImageFormat().name() == entry["key"]:
                        cur.setPosition(f.position())
                        cur.setPosition(f.position() + f.length(),
                                        QTextCursor.KeepAnchor)
                        cur.removeSelectedText()
                        break
                    it += 1
        except Exception as e:
            _log.debug(f"摘除旧思考面板占位图失败: {e}")
        # ★ 1.9.2：统一走安全销毁（停节拍器 → 断钩子 → 隐藏 → 脱离父级 → deleteLater）
        self._dispose_think_panel(entry.get("panel"))
        try:
            self._think_panels.remove(entry)
        except ValueError:
            pass
        if self._think_current is entry:
            self._think_current = None

    def _ensure_think_panel(self):
        """取**本轮**的面板；没有（或已销毁）就开一个新的。"""
        self._prune_think_panels()
        if self._think_current is None:
            return self._new_think_panel()
        return self._think_current["panel"]

    def _seal_think_panel(self):
        """本轮结束（LLM 开始输出正文 / 用户发新消息）→ 封口。

        封口后下一次思考写入会自动开新面板 —— 这就是"每轮都出现"的关键。
        """
        self._prune_think_panels()
        entry = self._think_current
        if entry is None:
            return
        panel = entry.get("panel")
        if self._alive(panel):
            try:
                panel.flush_now()
            except Exception:
                pass
            try:
                if not panel.is_empty():
                    panel.show()
            except Exception:
                pass
        self._think_current = None

    def _panel_width(self) -> int:
        vp = self._chat_display.viewport().width()
        return max(120, vp - 8)

    def _on_think_rendered(self):
        """★ 1.9.2：面板渲染完成 → 同步占位图尺寸 + 重定位。

        由 ThinkPanel 的节拍器每 ~80ms 调一次（以前是每个流式碎片一次，
        而这里的 QTextCursor.setCharFormat 会触发整个 QTextDocument 重排）。
        ★ 这个回调可能在"面板已被销毁"之后才到达（deleteLater 是延迟销毁），
          所以第一件事就是校验存活 + 摘掉死条目，绝不去碰死对象。
        """
        self._prune_think_panels()
        if not self._think_panels:
            return
        try:
            for e in list(self._think_panels):
                self._sync_think_anchor(e)
            self._reposition_think_panels()
        except Exception as e:
            _log.debug(f"思考面板渲染后同步失败: {e}")

    def _sync_think_anchor(self, entry=None):
        """把某个面板的占位图尺寸改成它当前所需（文档重排 → 面板位置也就对了）。

        ★★ 1.9.2 启动错位修复（用户报："刚启动，点开聊天界面，UI 左右错位，
        聊天记录在左边屏幕外面，子窗口位置正确。开启对话后恢复正常。"）：
        占位图原来被设成**和聊天区一样宽**（viewport-8），于是文档的"理想宽度"
        被撑到超过可视区 → 水平方向被挤出屏幕（而面板是独立子控件，所以它
        "位置正确"，正好对上用户看到的现象）。聊过一轮后文档重排才恢复。
        修法：占位图**只需要占高度**，宽度固定 1px —— 这样它永远不会影响
        文档宽度，面板的位置由 cursorRect 给，不依赖图片宽度。
        """
        self._prune_think_panels()
        targets = [entry] if entry is not None else list(self._think_panels)
        for e in targets:
            if e is None or not self._alive(e.get("panel")):
                continue
            blk = self._find_think_block(e["key"])
            if not blk.isValid():
                continue
            try:
                e["fmt"].setWidth(self.THINK_ANCHOR_W)     # ★ 1px：不撑宽文档
                e["fmt"].setHeight(max(1, e["panel"].desired_height()))
                it = blk.begin()
                while not it.atEnd():
                    f = it.fragment()
                    if f.isValid() and f.charFormat().isImageFormat() \
                            and f.charFormat().toImageFormat().name() == e["key"]:
                        c = QTextCursor(self._chat_display.document())
                        c.setPosition(f.position())
                        c.setPosition(f.position() + f.length(),
                                      QTextCursor.KeepAnchor)
                        c.setCharFormat(e["fmt"])
                        break
                    it += 1
            except Exception as ex:
                _log.debug(f"同步思考面板占位图失败: {ex}")

    def _reposition_think_panels(self, *_):
        """把所有面板移到各自占位图的位置（随滚动条/消息流走）。

        ★ 1.9.2 崩溃修复：
          · 先摘掉已销毁的条目（deleteLater 之后 C++ 对象可能已经没了，
            再去 `isVisible()` / `move()` 就是访问违例）；
          · `_find_think_block` 返回的是**无效 block**（不是 None），
            所以这里统一用 `isValid()` 判断，不会再出 AttributeError。
        """
        self._prune_think_panels()
        if not self._think_panels:
            return
        try:
            doc = self._chat_display.document()
            layout = doc.documentLayout()
            dy = self._chat_display.verticalScrollBar().value()
            dx = self._chat_display.horizontalScrollBar().value()
            vh = self._chat_display.viewport().height()
            w = self._panel_width()
            for e in list(self._think_panels):
                p = e.get("panel")
                if not self._alive(p):
                    continue
                blk = self._find_think_block(e["key"])
                if not blk.isValid():
                    try:
                        p.hide()
                    except Exception:
                        pass
                    continue
                try:
                    # ★★ 1.9.2 修（用户："子窗口堆叠在主窗口上边框下面，不跟着消息走"）：
                    #   上一版改用 `cursorRect()` 是**错的** —— Qt 对**可视区之外**的
                    #   游标位置会把矩形**钳制到视口边缘**（顶上或底下），于是所有
                    #   滚出视野的面板全被摆到"上边框下面"堆成一摞。
                    #   正确做法：用**文档坐标**（blockBoundingRect）再减滚动偏移 ——
                    #   绝对的、不会被钳制；然后自己判断在不在可视区来决定显隐。
                    #   （占位图现在是独立 block，blockBoundingRect 就是图片本身的
                    #     矩形，所以也不会再有"含前导行高、遮住气泡"那个问题。）
                    r = layout.blockBoundingRect(blk)
                    y = int(r.top()) - dy
                    x = max(0, int(r.left()) - dx + 4)
                    if y + p.height() < 0 or y > vh:
                        p.hide()          # 滚出可视区 → 藏起来，别堆在边缘
                        continue
                    if w != p.width():
                        p.setFixedWidth(w)
                        p._relayout_height()
                    p.show()
                    p.move(x, y)
                except Exception as ex:
                    _log.debug(f"单个思考面板定位失败（跳过）: {ex}")
        except Exception as e:
            _log.debug(f"思考面板定位失败: {e}")

    # 兼容旧名（外部/测试仍在调）
    def _reposition_think_panel(self, *a):
        self._reposition_think_panels(*a)

    def think_write(self, text: str, kind: str = "think"):
        """★ 对外主入口：往**本轮**的「思考 / 系统信息」面板写一行。"""
        p = self._ensure_think_panel()
        if not self._alive(p):
            return
        p.show()
        if kind == "system":
            p.append_system(text)
        else:
            p.append(text, "think")
        p.flush_now()                 # 非流式 → 立即渲染（_render 钩子会做同步）
        sb = self._chat_display.verticalScrollBar()
        sb.setValue(sb.maximum())

    def think_delta(self, delta: str):
        """★ 流式思考增量（LLM 的 reasoning_content 碎片）。

        ★ 1.9.2 卡死修复：这里**只记账**。渲染、占位图尺寸同步、重定位全部交给
        ThinkPanel 的节拍器（~12 次/秒）。以前每个碎片都做一遍全文 setText +
        heightForWidth + 文档重排，碎片一多就把 UI 线程占死（真机 21:55 那次
        "终止话题卡死、形象/托盘全无响应"）。
        """
        if not delta:
            return
        p = self._ensure_think_panel()
        if not self._alive(p):
            return
        p.show()
        p.append_thinking(delta)

    def think_flush(self):
        """这轮思考结束/需要收尾时强制渲染一次（保证界面跟上、锚点归位）。

        ★ 1.9.2 崩溃修复：先摘死条目再逐个校验存活 —— 这个函数是
        `_on_thinking_done_safe()` 调的，而"开启新话题"可能刚好在它之前
        把旧面板销毁掉（真机那次静默崩溃就在这个时间窗口里）。
        """
        self._prune_think_panels()
        if self._think_current is not None:
            p = self._think_current.get("panel")
            if self._alive(p):
                try:
                    p.flush_now()
                except Exception as e:
                    _log.debug(f"思考面板收尾渲染失败: {e}")
            return
        for e in list(self._think_panels):
            p = e.get("panel")
            if not self._alive(p):
                continue
            try:
                p.flush_now()
            except Exception:
                pass

    def think_clear(self):
        """清空**所有**思考面板（新话题/新对话时用）。"""
        self._prune_think_panels()
        for e in list(self._think_panels):
            p = e.get("panel")
            if not self._alive(p):
                continue
            try:
                p.clear()
            except Exception:
                pass
        self._think_current = None
        self._reposition_think_panels()

    def think_panel_count(self) -> int:
        """当前保留的面板数（测试/排查用）—— 应当是"每轮一个"。"""
        self._prune_think_panels()
        return len(self._think_panels)

    def _handle_think_in(self, text: str, kind: str):
        self.think_write(text, kind)

    def _handle_think_delta_in(self, delta: str):
        self.think_delta(delta)

    # ══════════════════════════════════════════
    # 图片预览
    # ══════════════════════════════════════════

    @staticmethod
    def _get_image_size(file_path: str) -> str:
        try:
            from PIL import Image
            with Image.open(file_path) as img:
                return f"{img.width}×{img.height}"
        except Exception:
            return "?"

    def append_image_preview(self, file_path: str, from_bot: bool = False):
        """★ QQ 风格内联预览：聊天里显示缩略图，但 <img> 指向原图 file:/// 路径，
        点击缩略图 → 系统图片查看器打开原图（原图始终留在你原本的位置）。

        ★ 1.6.0: from_bot 决定气泡方向 ——
          默认 False=用户上传（右侧 + 用户头像）；
          桌宠（LLM）发来的图片必须显式传 from_bot=True（左侧 + 桌宠头像），
          否则会渲染得像用户自己发的。

        注意：Qt 的 QTextEdit 不支持 data:image/...;base64 内联图片，只认本地
        文件路径，所以这里一律用 path.as_uri()（file:///...）作为 img src。
        ★ 1.6.0: 必须先 resolve() 成绝对路径 —— 工具返回的是相对路径（如
        data/桌宠形象/deepseek/头像.jpg），相对路径调 as_uri() 会直接抛
        "relative path can't be expressed as a file URI"，图片就永远显示不出来。"""
        path = Path(file_path)
        if not path.is_absolute():
            path = (_BASE_DIR / path).resolve()
        else:
            path = path.resolve()
        if not path.exists():
            self.append_system_msg(f"[图片不存在: {path.name}]")
            return
        try:
            from PIL import Image
            with Image.open(path) as img:
                w, h = img.size
            max_w = 260
            if w > max_w:
                thumb_w = max_w
                thumb_h = int(h * max_w / w)
            else:
                thumb_w, thumb_h = w, h

            escaped = path.name.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            file_uri = path.as_uri()   # 原图路径，点击/系统查看器都开它
            av = AVATAR_DISPLAY_SIZE
            img_cell = (
                f'<a href="{file_uri}" style="text-decoration:none; cursor:pointer;" '
                f'title="{escaped} · 左键查看原图，右键复制路径">'
                f'<img src="{file_uri}" width="{thumb_w}" height="{thumb_h}" '
                f'style="border-radius:8px; cursor:pointer;"></a>')
            if from_bot:
                # 桌宠（助手）侧：头像在左，图片靠左
                row = (f'<td width="{av + 14}" align="left" valign="top">'
                       f'{self._avatar_html(True)}</td>'
                       f'<td align="left" valign="top">{img_cell}</td>')
            else:
                # 用户侧：头像在右，图片靠右
                row = (f'<td align="right" valign="top">{img_cell}</td>'
                       f'<td width="{av + 14}" align="right" valign="top">'
                       f'{self._avatar_html(False)}</td>')
            self._chat_display.moveCursor(QTextCursor.End)
            self._chat_display.insertHtml(
                f'<table width="100%" cellspacing="0" cellpadding="3" style="margin:6px 0;">'
                f'<tr>{row}</tr></table>')
            self._chat_display.moveCursor(QTextCursor.End)
            sb = self._chat_display.verticalScrollBar()
            sb.setValue(sb.maximum())
        except Exception as e:
            _log.warning(f"图片预览失败: {e}")
            self.append_system_msg(f"[图片预览失败: {path.name}]")

    def append_topic_divider(self, label: str = "", topic_id=0):
        """话题分割线（一排 #），返回插入位置。

        ★ 1.9.1: 原来还会在文本里塞一个隐形标记 `⟦T{topic_id}⟧` 供"书签跳转"
        定位。书签删了 → 标记也一起去掉（留着只会白占文档长度）。
        `topic_id` 参数保留，因为主窗口还在按话题传（将来想恢复跳转不用改调用方）。
        """
        marks = "#" * 28
        shown = self._truncate(label, 24) if label else ""
        body = f"{marks}  {shown}  {marks}" if shown else marks
        self._chat_display.moveCursor(QTextCursor.End)
        pos = self._chat_display.textCursor().position()
        self._chat_display.insertHtml(
            f'<div style="margin:10px 0; text-align:center; color:#c0c0cc; '
            f'font-size:10px; letter-spacing:1px;">{body}</div>')
        self._chat_display.moveCursor(QTextCursor.End)
        sb = self._chat_display.verticalScrollBar()
        sb.setValue(sb.maximum())
        return pos

    # ★ 1.9.1: `scroll_to_topic_mark()` 删了 —— 它只服务"书签左键跳转"，
    #   书签功能按你的要求整个移除了（话题分割线 `#` 仍然保留，只是不可点）。

    def scroll_to_position(self, pos: int):
        """★ 1.6.0: 跳转到聊天区指定文本位置（侧栏书签左键定位用）"""
        try:
            cur = self._chat_display.textCursor()
            cur.setPosition(max(0, int(pos)))
            self._chat_display.setTextCursor(cur)
            self._chat_display.ensureCursorVisible()
            sb = self._chat_display.verticalScrollBar()
            sb.setValue(max(sb.minimum(), sb.value() - 40))
        except Exception as e:
            _log.debug(f"跳转位置失败: {e}")

    # ══════════════════════════════════════════
    # 文件卡片（★ 1.8.2: 固定 3cm × 1cm 紧凑芯片）
    # ══════════════════════════════════════════

    def card_size_px(self) -> tuple[int, int]:
        """把 CARD_*_CM 换算成当前屏幕的像素尺寸。

        Qt 富文本里只有 <table width height> 是**真正按像素生效**的
        （div 的 width/height、max-width 都会被忽略，反而铺满整行）。
        """
        dpi = 96.0
        try:
            scr = self.screen() or QApplication.primaryScreen()
            if scr is not None:
                dpi = float(scr.logicalDotsPerInch() or 96.0)
        except Exception:
            dpi = 96.0
        if dpi <= 0:
            dpi = 96.0
        w = max(self.CARD_MIN_W_PX, int(round(self.CARD_WIDTH_CM / 2.54 * dpi)))
        h = max(self.CARD_MIN_H_PX, int(round(self.CARD_HEIGHT_CM / 2.54 * dpi)))
        return w, h

    def _elide_to_px(self, text: str, max_px: int, px_size: int = 11) -> str:
        """按像素宽度截断文本（中间省略）"""
        try:
            f = QFont("Microsoft YaHei")
            f.setPixelSize(px_size)
            fm = QFontMetrics(f)
            return fm.elidedText(text, Qt.ElideMiddle, max(24, int(max_px)))
        except Exception:
            return text

    def _elide_filename(self, name: str, max_px: int, px_size: int = 11) -> str:
        """★ 1.8.2: 截断文件名但**保住完整扩展名**。

        直接用 elidedText(middle) 会把 ".pptx" 削成 "ptx"，
        所以这里把扩展名单独摘出来，只截断主干。
        """
        try:
            f = QFont("Microsoft YaHei")
            f.setPixelSize(px_size)
            fm = QFontMetrics(f)
            max_px = max(24, int(max_px))
            if fm.horizontalAdvance(name) <= max_px:
                return name
            stem, suffix = os.path.splitext(name)
            # 扩展名宽度必须给主干至少留 ~14px（一两个字符），否则整体中间省略
            if suffix and fm.horizontalAdvance(suffix) <= max_px - 14:
                room = max_px - fm.horizontalAdvance(suffix)
                return fm.elidedText(stem, Qt.ElideRight, max(12, room)) + suffix
            return fm.elidedText(name, Qt.ElideMiddle, max_px)
        except Exception:
            return name

    def append_file_card(self, label: str, file_path: str, file_size: str = "",
                         from_bot: bool = False):
        """在聊天区显示可点击文件卡片

        ★ 1.8.2: 固定尺寸的小卡片（见 CARD_*_CM）。
        ★ 1.8.4: 改成和图片预览完全同款的行布局 —— 靠发送方，带发送方头像，
        一条消息一个头像（QQ 式）。用户上传靠右+用户头像；桌宠发来的靠左+桌宠头像。
        """
        path = Path(file_path).resolve()
        if not path.exists():
            self.append_system_msg(f"[文件不存在: {path.name}]")
            return

        w, h = self.card_size_px()
        card = self.file_card_html(label, path, file_size, w, h)
        av = AVATAR_DISPLAY_SIZE
        if from_bot:
            row = (f'<td width="{av + 14}" align="left" valign="top">'
                   f'{self._avatar_html(True)}</td>'
                   f'<td align="left" valign="top">{card}</td>')
        else:
            row = (f'<td align="right" valign="top">{card}</td>'
                   f'<td width="{av + 14}" align="right" valign="top">'
                   f'{self._avatar_html(False)}</td>')
        self._chat_display.moveCursor(QTextCursor.End)
        self._chat_display.insertHtml(
            f'<table width="100%" cellspacing="0" cellpadding="3" style="margin:6px 0;">'
            f'<tr>{row}</tr></table>')
        self._chat_display.moveCursor(QTextCursor.End)
        sb = self._chat_display.verticalScrollBar()
        sb.setValue(sb.maximum())

    # 文件类型 → (图标, 简称)
    _FILE_ICONS = {
        ".pdf": ("📄", "PDF"), ".doc": ("📝", "Word"), ".docx": ("📝", "Word"),
        ".ppt": ("📊", "PPT"), ".pptx": ("📊", "PPT"),
        ".xls": ("📈", "Excel"), ".xlsx": ("📈", "Excel"), ".csv": ("📈", "CSV"),
        ".mp3": ("🎵", "音频"), ".wav": ("🎵", "音频"),
        ".flac": ("🎵", "音频"), ".m4a": ("🎵", "音频"),
        ".mp4": ("🎬", "视频"), ".mkv": ("🎬", "视频"), ".avi": ("🎬", "视频"),
        ".mov": ("🎬", "视频"), ".webm": ("🎬", "视频"),
        ".zip": ("📦", "压缩包"), ".rar": ("📦", "压缩包"), ".7z": ("📦", "压缩包"),
        ".py": ("🐍", "Python"), ".js": ("📜", "JS"), ".ts": ("📜", "TS"),
        ".txt": ("📃", "文本"), ".md": ("📃", "Markdown"),
        ".png": ("🖼️", "图片"), ".jpg": ("🖼️", "图片"), ".jpeg": ("🖼️", "图片"),
        ".gif": ("🖼️", "图片"), ".webp": ("🖼️", "图片"),
    }

    def file_card_html(self, label: str, path: Path, file_size: str,
                       w: int, h: int) -> str:
        """生成文件卡片的 HTML（固定 w×h 像素，返回给测试/渲染共用）"""
        name = path.name
        ext = path.suffix.lower()
        icon, type_name = self._FILE_ICONS.get(ext, ("📎", ext.lstrip(".").upper() or "文件"))

        # HTML 转义（文件名可能含 & < >）
        esc = lambda s: (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        file_url = path.as_uri()

        pad_x, pad_y = 6, 3
        # ★ 1.8.4: 字号与内边距跟着卡片尺寸等比缩放
        #   （38px = 1cm @96dpi，所以 h=38 时 scale=1，h=57 时 scale=1.5）
        scale = max(0.8, h / 38.0)
        f_name = int(round(11 * scale))
        f_meta = int(round(9 * scale))
        pad_x = int(round(6 * scale))
        pad_y = int(round(3 * scale))
        # 第一行：图标 + 文件名；第二行：来源/类型 · 大小
        name_px = w - pad_x * 2 - int(round(18 * scale))   # 减去图标 + 间距
        short_name = self._elide_filename(name, name_px, f_name)
        # 「用户上传」这类通用标签没有信息量，换成操作提示；有意义的标签
        # （如「📚 本子下载完成」）优先显示在第二行。
        generic_labels = ("", "用户上传", "上传")
        if (label or "").strip() not in generic_labels:
            meta = f"{label.strip()} · {type_name}" + (f" · {file_size}" if file_size else "")
        else:
            meta = type_name + (f" · {file_size}" if file_size else "") + " · 点击打开"
        meta_short = self._elide_to_px(meta, w - pad_x * 2, f_meta)

        # 说明：Qt 富文本里只有 <table width height> 真正按像素生效，
        # div 的 width/height/max-width 会被忽略（卡片会铺满整行）。
        # ★ 1.9.0: 底色不再由 Qt 画（改用 bgcolor 会得到方角）——白底与圆角
        #   由 _RoundedBubbleBrowser 自绘（见 _card_rects）。
        return (
            f'<table width="{w}" height="{h}" cellpadding="0" '
            f'cellspacing="0" border="0">'
            f'<tr><td style="padding:{pad_y}px {pad_x}px;">'
            f'<a href="{file_url}" style="text-decoration:none;">'
            f'<span style="color:#1a1a1a; font-size:{f_name}px; font-weight:600;">'
            f'{icon} {esc(short_name)}</span><br>'
            f'<span style="color:#999; font-size:{f_meta}px;">{esc(meta_short)}</span>'
            f'</a></td></tr></table>'
        )

    def _on_anchor_clicked(self, url):
        try:
            url_str = url.toString() if hasattr(url, "toString") else str(url)
            if url_str.startswith("file:///"):
                from urllib.parse import urlparse, unquote
                parsed = urlparse(url_str)
                raw_path = unquote(parsed.path)
                if len(raw_path) >= 3 and raw_path[0] == '/' and raw_path[2] == ':':
                    raw_path = raw_path[1:]
                elif len(raw_path) >= 4 and raw_path[0:2] == '//' and raw_path[3] == ':':
                    raw_path = raw_path[2:]
                file_path = Path(raw_path)
                if file_path.exists():
                    self._open_file_external(str(file_path))
                else:
                    _log.warning(f"链接文件不存在: {url_str} → {file_path}")
            else:
                QDesktopServices.openUrl(url)
        except Exception as e:
            _log.warning(f"打开链接失败: {e}")

    def _on_chat_context_menu(self, pos):
        """★ 1.5.0: 聊天里右键图片/文件卡片 → 复制其原图/原文件路径到剪贴板"""
        try:
            url = self._chat_display.anchorAt(pos)
            url_str = str(url)
            if url_str and url_str.startswith("file:///"):
                from urllib.parse import urlparse, unquote
                parsed = urlparse(url_str)
                raw = unquote(parsed.path)
                if len(raw) >= 3 and raw[0] == '/' and raw[2] == ':':
                    raw = raw[1:]
                elif len(raw) >= 4 and raw[0:2] == '//' and raw[3] == ':':
                    raw = raw[2:]
                menu = QMenu(self)
                act = menu.addAction("复制路径")
                if menu.exec_(self._chat_display.mapToGlobal(pos)) == act:
                    QApplication.clipboard().setText(raw)
                    self.append_system_msg("[已复制图片路径]")
                return
        except Exception as e:
            _log.warning(f"右键菜单失败: {e}")

    # ══════════════════════════════════════════
    # 工具方法
    # ══════════════════════════════════════════

    def clear_chat(self):
        self._chat_display.clear()
        # ★ 1.9.0: 文档清空了，气泡区间也必须跟着清（否则会拿旧区间去画）
        try:
            self._chat_display.bubble_spans.clear()
        except Exception:
            pass
        # ★ 1.9.0: 剪贴板内存图片也清掉（清空聊天后没有消息引用它们了）
        try:
            self._clip_images.clear()
        except Exception:
            pass
        self._stream_buffer = ""
        self._last_role = ""
        self._stream_start = 0
        self._stream_span = None
        # ★ 1.9.2（用户任务 4）：清空聊天时，思考面板的占位图也跟着没了
        #   （_chat_display.clear() 会把文档清掉）→ 全部安全销毁并复位，
        #   下次 think_write 会重新插占位图（每轮一个）。
        #   ★ 崩溃修复：**先**把列表清空再销毁 —— 否则销毁过程中若有
        #     `_on_think_rendered` 回调进来，会拿到"正在被销毁"的条目。
        _old = list(self._think_panels)
        self._think_panels = []
        self._think_current = None
        for _e in _old:
            self._dispose_think_panel(_e.get("panel"))

    def reset_stream(self):
        self._last_role = ""
        self._stream_buffer = ""
        self._stream_start = 0
        self._stream_span = None

    def show_at(self, x: int, y: int):
        self.move(x, y)
        self.show()
        self.raise_()
        self.activateWindow()

    def set_topic_title(self, title: str):
        """★ 1.9.1: 话题标题不再单独占标题栏 —— 并进左上角那行/tooltip。

        1.6.0 时它写进 `_chat_title`（一个 setVisible(False) 的隐藏 QLabel，
        等于白写）。现在侧栏和 `_chat_title` 都删了，话题名只做 tooltip 补充。
        """
        self._topic_title = (title or "").strip()
        if getattr(self, "_topic_title", ""):
            self.set_conversation_info(self._char_name, self._topic_title,
                                       self._msg_count, self._token_count)

    # ══════════════════════════════════════════
    # 头像（可自定义，默认自带形象）
    # ══════════════════════════════════════════

    @staticmethod
    def _make_avatar_uri(path) -> str:
        """加载图片并转成圆形 PNG 的 data URI；失败时回退到自带形象"""
        from PIL import Image, ImageDraw, ImageOps

        def _load(p):
            img = Image.open(p).convert("RGBA")
            img = ImageOps.fit(img, (128, 128), Image.LANCZOS, centering=(0.5, 0.5))
            mask = Image.new("L", (128, 128), 0)
            ImageDraw.Draw(mask).ellipse((0, 0, 128, 128), fill=255)
            img.putalpha(mask)
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            return buf.getvalue()

        candidates = []
        if path:
            # ★ 1.9.0: 支持相对路径。项目每版都会被复制到新目录
            #   （…/PTRTS1.8.0 → …/PTRTS1.9.0），config 里若存绝对路径，
            #   头像就会指向旧版本文件夹（config.json 里真出现过
            #   PTRTS1.8.0 的绝对路径）。相对路径一律以项目根解析。
            _p = Path(path)
            candidates.append(_p if _p.is_absolute() else (_BASE_DIR / _p))
        if DEFAULT_AVATAR_PATH:
            candidates.append(DEFAULT_AVATAR_PATH)

        for c in candidates:
            try:
                p = Path(c)
                if p and p.exists() and p.stat().st_size > 0:
                    data = _load(p)
                    return "data:image/png;base64," + base64.b64encode(data).decode()
            except Exception as e:
                _log.debug(f"头像加载失败 {c}: {e}")
        # 最终兜底：空 URI（显示为 broken 图标，几乎不会发生）
        return ""

    def set_avatars(self, user_path: str = "", bot_path: str = ""):
        """设置用户 / 桌宠头像路径（空串=使用自带形象），并刷新已显示的消息"""
        self._user_avatar_path = user_path or ""
        self._bot_avatar_path = bot_path or ""
        self._refresh_avatar_uris()
        # 强制重绘聊天区，使已存在的头像即刻生效
        try:
            doc = self._chat_display.document()
            doc.markContentsDirty(0, doc.characterCount())
        except Exception:
            pass

    def _refresh_avatar_uris(self):
        # 空路径 = 自带形象（铃兰）；用户上传后 settings 会写入 data/avatars/user_avatar.jpg
        self._avatar_uri = {
            "user": self._make_avatar_uri(self._user_avatar_path),
            "bot": self._make_avatar_uri(self._bot_avatar_path),
        }

    # ══════════════════════════════════════════
    # 鼠标拖拽缩放（无边框窗口）
    # ══════════════════════════════════════════

    def _edge_at(self, pos: QPoint) -> str:
        """返回鼠标所处的边缘标识，如 'l'/'r'/'tl'/'br'/''"""
        m = self.RESIZE_MARGIN + 2
        x, y = pos.x(), pos.y()
        w, h = self.width(), self.height()
        edge = ""
        if y <= m:
            edge += "t"
        elif y >= h - m:
            edge += "b"
        if x <= m:
            edge += "l"
        elif x >= w - m:
            edge += "r"
        return edge

    @staticmethod
    def _qt_edges(edge: str) -> Qt.Edges:
        """边缘字符串 → Qt.Edges（供 startSystemResize 使用）"""
        return {
            "l": Qt.LeftEdge, "r": Qt.RightEdge,
            "t": Qt.TopEdge, "b": Qt.BottomEdge,
            "tl": Qt.TopEdge | Qt.LeftEdge,
            "tr": Qt.TopEdge | Qt.RightEdge,
            "bl": Qt.BottomEdge | Qt.LeftEdge,
            "br": Qt.BottomEdge | Qt.RightEdge,
        }.get(edge, Qt.Edges())

    @staticmethod
    def _cursor_for(edge: str):
        if edge in ("tl", "br"):
            return Qt.SizeFDiagCursor
        if edge in ("tr", "bl"):
            return Qt.SizeBDiagCursor
        if edge in ("t", "b"):
            return Qt.SizeVerCursor
        if edge in ("l", "r"):
            return Qt.SizeHorCursor
        return Qt.ArrowCursor

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            edge = self._edge_at(event.position().toPoint())
            if edge:
                # ★ 优先使用原生系统缩放（Windows/macOS/X11 均支持），稳定不卡
                win = self.windowHandle()
                if win and win.startSystemResize(self._qt_edges(edge)):
                    event.accept()
                    return
                # 平台不支持 → 回退到手动几何计算
                self._resizing = True
                self._resize_edge = edge
                self.setCursor(self._cursor_for(edge))
                self._resize_start_geo = QRect(self.geometry())
                self._resize_start_pos = event.globalPosition().toPoint()
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._resizing and self._resize_start_geo is not None:
            delta = event.globalPosition().toPoint() - self._resize_start_pos
            geo = QRect(self._resize_start_geo)
            min_w, min_h = self.MIN_WIDTH, self.MIN_HEIGHT
            if "l" in self._resize_edge:
                geo.setLeft(min(geo.left() + delta.x(), geo.right() - min_w + 1))
            if "r" in self._resize_edge:
                geo.setRight(max(geo.right() + delta.x(), geo.left() + min_w - 1))
            if "t" in self._resize_edge:
                geo.setTop(min(geo.top() + delta.y(), geo.bottom() - min_h + 1))
            if "b" in self._resize_edge:
                geo.setBottom(max(geo.bottom() + delta.y(), geo.top() + min_h - 1))
            self.setGeometry(geo)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._resizing:
            self._resizing = False
            self._resize_edge = ""
            self._resize_start_geo = None
            self._resize_start_pos = None
            self.setCursor(Qt.ArrowCursor)
            self.resized.emit(self.width(), self.height())
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # ★ 1.9.1: 原来这里要跟着把侧栏窗口重新贴合 —— 侧栏已删，无需再做。
        # 程序主动 resize 时通过 _ignore_resize_report 屏蔽，避免循环保存
        if not self._ignore_resize_report and not self._resizing:
            self._emit_resized_debounced()

    def _emit_resized_debounced(self):
        """缩放结束后防抖 300ms 再上报，避免原生缩放过程中频繁 emit"""
        if not hasattr(self, '_resize_report_timer'):
            self._resize_report_timer = QTimer(self)
            self._resize_report_timer.setSingleShot(True)
            self._resize_report_timer.timeout.connect(
                lambda: self.resized.emit(self.width(), self.height()))
        self._resize_report_timer.start(300)

    # ══════════════════════════════════════════
    # 个性化颜色
    # ══════════════════════════════════════════

    def apply_personalize_colors(self, ui_bg: str, chat_bg: str,
                                  user_bubble: str, user_text: str,
                                  bot_bubble: str, bot_text: str):
        """动态应用个性化颜色主题（浅色为默认，文本默认无底色）"""
        self._personalize = {
            "ui_bg": ui_bg or "#ffffff", "chat_bg": chat_bg or "#f7f7fa",
            "user_bubble": user_bubble, "user_text": user_text or "#222222",
            "bot_bubble": bot_bubble, "bot_text": bot_text or "#222222",
        }
        self.setStyleSheet(self._style_sheet(
            self._personalize["ui_bg"], self._personalize["chat_bg"]))
        # ★ 1.9.0: 气泡颜色可能变了 —— 已登记的气泡就地换色（不必重画消息）
        try:
            for _sp in self._chat_display.bubble_spans:
                _sp["color"] = self._bubble_color_for(_sp.get("role", "assistant"))
            self._chat_display.viewport().update()
        except Exception:
            pass

    @staticmethod
    def _hex_to_rgba(hex_color: str, alpha: int) -> str:
        """#rrggbb → r, g, b, a 格式"""
        hex_color = hex_color.lstrip("#")
        if len(hex_color) == 6:
            r, g, b = int(hex_color[0:2], 16), int(hex_color[2:4], 16), int(hex_color[4:6], 16)
            return f"{r}, {g}, {b}, {alpha}"
        return "20, 20, 30, 245"
