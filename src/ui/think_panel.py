"""
★ 1.9.2 任务 4：聊天窗口里"嵌入消息流的子窗口"（思考 / 系统信息）

用户原话：
  ###4,聊天窗口新组件，嵌入消息显示的子窗口，内容是思考内容和系统信息。
  高度适配里面信息，最高高度能够装下5排字就行。
  "…\\聊天窗口子窗口UI演示.html"

## 和演示稿的关系（★ 有一处**不一样**，以用户原话为准）
演示稿 `聊天窗口子窗口UI演示.html` 里面板写的是 `.inline-think-panel{height:180px}`
（**固定**高度）。用户原话要的是"**高度适配**里面信息，**最高**高度能够装下
5 排字"——也就是：
    · 内容少 → 面板矮（跟着内容缩）
    · 内容多 → 长到 5 排字的高度就**封顶**，再多的内容靠面板**内部滚动**
所以本实现是 **min(内容高度, 5 排字封顶)**，不是固定 180px。
（180px / 演示稿的 .74rem×1.55 行高 ≈ 18px/行 ≈ 装 8~9 行，也超过用户说的 5 排。）

## 为什么是"真 QWidget"而不是画在富文本里
聊天区是一条 `QTextBrowser`（消息都是 HTML 塞进去的，见 chat_widget._chat_display）。
富文本**做不到**"内层独立滚动"——那需要一个真的滚动区。
所以这里是真 `QFrame` + `QScrollArea`，由 ChatWidget 把它挂在
`_chat_display.viewport()` 上、按占位图的位置**跟着消息流一起滚**。

## 滚动事件隔离（演示稿里特意演示的那条）
面板是真子控件 → 滚轮事件天然先到它手上。
但滚到顶/底之后 Qt 会想把事件往父级传（外层聊天区就会跟着滚）。
所以这里装了 eventFilter：**在面板范围内一律吃掉滚轮**，
只有明确"到顶还在往上/到底还在往下"时才放行给外层 —— 与演示稿里那段
`atTop/atBottom` 的 JS 逻辑一一对应。
"""
from __future__ import annotations

import logging
import os

from PySide6.QtCore import Qt, QEvent, QSize, QTimer
from PySide6.QtGui import QFont, QFontMetrics, QPainter, QColor, QPen
from PySide6.QtWidgets import (QFrame, QWidget, QVBoxLayout, QHBoxLayout, QLabel,
                               QScrollArea, QSizePolicy, QApplication)

_log = logging.getLogger("ThinkPanel")

# ── 高度规则 ────────────────────────────────────────────────
MAX_LINES = 5              # ★ 用户原话："最高高度能够装下5排字就行"
MIN_LINES = 1              # 至少留 1 排（不然空面板会塌成一条线）
PANEL_PAD_X = 12           # 正文左右内边距（对齐演示稿 .panel-body padding）
PANEL_PAD_Y = 8            # 正文上下内边距
HEADER_H = 24              # 标题栏高度
# ★ 行高**不自己乘系数**：演示稿 CSS 的 line-height:1.55 是"行框 = 1.55×字号"，
#   而 Qt 的 QFontMetrics.lineSpacing() 返回的已经是字体自带的自然行高
#   （已含 leading）。第一版在这里又乘了一次 1.55，导致"1 行"被算成 1.2 排、
#   高度曲线变成 1.2/1.85/2.5…（实测）。想要更透气就把字号调大一点，
#   不要去改行高系数。
FONT_PT = 9.0              # 正文字号（比演示稿的 .74rem 稍大，中文更易读）

# ── ★★ 1.9.2 性能相关（真机卡死事故后的加固）────────────────
#
# 事故经过（logs/crash_main_29488.log，用户 21:55 报的"终止话题直接卡死"）：
#   禁用长期记忆后模型自己去"绕过"（连跑满 20 轮 MAX_ROUNDS），
#   每轮都在流式吐很长的思考文本；而本组件**每个碎片**都要：
#       setText(全文) → QLabel.heightForWidth(全文) → 重排
#   文本越长越慢，碎片越多越慢 → **O(n²)**，UI 线程彻底占满 →
#   聊天窗/形象/托盘全部无响应（形象子进程还会阻塞在 conn.send 上，
#   因为主进程没空读管道）。用户只能 Ctrl+C。
#
# 三条加固，缺一不可：
KEEP_LINES = 300           # ① 只保留最近 N 行（面板只看得见 5 行，留 300 够滚了）
                           #    → 让 setText 的代价**有上界**，不再随流式文本无限增长
                           #    （顺带解决内存调研里"无上限增长"那条）
THROTTLE_MS = 80           # ② 流式增量**合并渲染**，最多 ~12 次/秒（原来每个碎片一次）

# ── 配色 ────────────────────────────────────────────────────
# 演示稿是**深色**（#202225 底 + #9b59b6 紫），但聊天窗口现在是浅色主题
# （ui_bg #ffffff / chat_bg #f7f7fa），直接上深紫会在白底上很跳。
# 所以默认用"浅底 + 同款紫强调"保住演示稿的**结构**和辨识色；
# 想把演示稿的深色原样搬上来，把环境变量 PRTS_THINK_PANEL_DARK 设成 1 即可。
_PALETTE_LIGHT = {
    "bg": "#f4f1f8", "border": "#ddd3ea", "accent": "#9b59b6",
    "header_bg": "#ece4f4", "header_fg": "#6b4d86",
    "body_fg": "#4a4a52", "dot": "#9b59b6",
}
_PALETTE_DARK = {
    "bg": "#202225", "border": "#3a3d42", "accent": "#9b59b6",
    "header_bg": "#1a1c1f", "header_fg": "#b8a9d4",
    "body_fg": "#bcc0c4", "dot": "#9b59b6",
}


def palette() -> dict:
    if os.environ.get("PRTS_THINK_PANEL_DARK", "").strip() in ("1", "true", "True"):
        return _PALETTE_DARK
    return _PALETTE_LIGHT


TITLE = "思考 / 系统信息"


class ThinkPanel(QFrame):
    """嵌在聊天消息流里的"思考 + 系统信息"子窗口。高度自适应，5 排字封顶。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._lines: list = []          # [(kind, text)] kind: think | system
        self._line_h = 16
        self._capped = False            # ★ 1.9.2：是否已确认超过 5 排（决定能否走快路径）
        self._dirty = False             # ★ 1.9.2：有未渲染的改动（流式合并用）
        self._dropped = 0               # ★ 1.9.2：被 KEEP_LINES 丢弃的旧行数（可观测）
        self._build()
        # ★ 1.9.2：流式增量合并渲染的节拍器（见文件头 THROTTLE_MS 那段事故说明）
        self._flush_timer = QTimer(self)
        self._flush_timer.setSingleShot(True)
        self._flush_timer.timeout.connect(self._flush)
        # ★ 滚轮隔离：装在自己的 viewport 与正文 Label 上
        for w in (self, self._scroll.viewport(), self._body):
            w.installEventFilter(self)

    # ── 构建 ──────────────────────────────────────────────
    def _build(self):
        pal = palette()
        self.setObjectName("ThinkPanel")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setStyleSheet(
            f"#ThinkPanel {{ background: {pal['bg']};"
            f" border: 1px solid {pal['border']};"
            f" border-left: 3px solid {pal['accent']};"
            f" border-radius: 8px; }}")
        self.setFrameShape(QFrame.NoFrame)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        # 标题栏（对齐演示稿：小圆点 + 大写小字）
        self._header = QWidget(self)
        self._header.setFixedHeight(HEADER_H)
        self._header.setStyleSheet(
            f"background: {pal['header_bg']};"
            f" border-top-left-radius: 7px; border-top-right-radius: 7px;")
        hl = QHBoxLayout(self._header)
        hl.setContentsMargins(10, 0, 10, 0)
        hl.setSpacing(6)
        self._dot = QLabel("\u25cf", self._header)      # ●
        self._dot.setStyleSheet(
            f"color: {pal['dot']}; font-size: 9px; background: transparent;")
        hl.addWidget(self._dot)
        self._title = QLabel(TITLE, self._header)
        self._title.setStyleSheet(
            f"color: {pal['header_fg']}; font-size: 10px;"
            f" letter-spacing: 0.6px; background: transparent;")
        hl.addWidget(self._title)
        hl.addStretch()
        self._count = QLabel("", self._header)
        self._count.setStyleSheet(
            f"color: {pal['header_fg']}; font-size: 9px; background: transparent;")
        hl.addWidget(self._count)
        lay.addWidget(self._header)

        # 正文（内层独立滚动）
        self._scroll = QScrollArea(self)
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._scroll.setStyleSheet(
            "QScrollArea { background: transparent; border: none; }"
            "QScrollBar:vertical { background: transparent; width: 7px; margin: 0; }"
            "QScrollBar::handle:vertical { background: #c9c2d6;"
            " border-radius: 3px; min-height: 18px; }"
            "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical"
            " { height: 0; }"
            "QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical"
            " { background: transparent; }")

        self._body = QLabel("", self._scroll)
        self._body.setWordWrap(True)
        self._body.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self._body.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        f = QFont("Consolas")
        f.setStyleHint(QFont.Monospace)
        f.setPointSizeF(FONT_PT)
        self._body.setFont(f)
        self._body.setStyleSheet(
            f"color: {pal['body_fg']}; background: transparent;"
            f" padding: {PANEL_PAD_Y}px {PANEL_PAD_X}px;")
        self._body.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)
        self._scroll.setWidget(self._body)
        lay.addWidget(self._scroll, 1)

        fm = QFontMetrics(self._body.font())
        # ★ 直取自然行高作为初值，真正用于高度计算的是 _probe() 实测出来的值
        self._line_h = max(10, int(fm.lineSpacing()))
        self._probe()
        self._relayout_height()

    # ── 行高/内边距的**实测标定** ────────────────────────────
    #
    # ★ 为什么不"算"而"量"（两版都栽在这）：
    #   第一版把演示稿的 line-height:1.55 当成系数又乘了一次 → 1 行被算成 1.2 排；
    #   第二版改成"行高 + 2×styleshset padding"，结果 QLabel 报出来的单行高度
    #   比 `行高 + padding` 还多 12px（样式表 padding 之外还有别的内嵌留白），
    #   1 行又变成 1.86 排。
    #   所以这里不去猜任何一层留白：直接**量**——
    #       h1 = 量"1 行"的高度（含全部留白）
    #       h2 = 量"2 行"的高度
    #     于是：单行基准 = h1，每多一行的增量 = h2 - h1（这才是真行高）
    #   这样无论字体、DPI、样式表怎么变，"能装 N 排"的高度永远精确。

    def _probe(self):
        """实测单行高度与每行增量。只在构建时做一次。"""
        try:
            self._body.setText("量")
            h1 = self._body.heightForWidth(160)
            self._body.setText("量\n量")
            h2 = self._body.heightForWidth(160)
            self._body.setText("")
            h1, h2 = int(h1 or 0), int(h2 or 0)
            if h1 > 0 and h2 > h1:
                self._one_h = h1
                self._lh_real = h2 - h1
            else:
                self._one_h = self._line_h + self.padding_h()
                self._lh_real = self._line_h
        except Exception as e:
            _log.debug(f"[ThinkPanel] 标定失败，退回字体度量: {e}")
            self._one_h = self._line_h + self.padding_h()
            self._lh_real = self._line_h

    # ── 高度：min(内容, 5 排字) ─────────────────────────────
    #
    # ★ 踩过的坑：正文 QLabel 的样式表里带 `padding: 8px 12px`，
    #   `heightForWidth()` 返回的高度**已经包含**这 16px 上下内边距。
    #   所以"能装下 N 排字"的高度是 `N × 行高 + 2 × PANEL_PAD_Y`，
    #   不能直接拿 `N × 行高` 去比（第一版就是这么错的：1 行内容
    #   算出来比预期多 16px）。

    def padding_h(self) -> int:
        """正文上下留白（= 单行高度 − 真行高）。仅用于展示/排查。"""
        return 2 * PANEL_PAD_Y

    def line_height(self) -> int:
        """★ 实测的"每多一行要加多少像素"。"""
        return getattr(self, "_lh_real", self._line_h)

    def min_content_height(self) -> int:
        """装下 1 排字所需的高度（含全部留白，实测值）。"""
        return getattr(self, "_one_h", self._line_h + self.padding_h())

    def max_content_height(self) -> int:
        """正好装下 MAX_LINES 排字所需的高度。"""
        return self.min_content_height() + self.line_height() * (MAX_LINES - 1)

    def content_height(self) -> int:
        """正文按当前宽度换行后**真实**需要的高度（含留白）。"""
        w = max(40, self._scroll.viewport().width())
        try:
            h = self._body.heightForWidth(w)
        except Exception:
            h = self._line_h * max(1, len(self.text().split("\n")))
        if h <= 0:
            h = self.min_content_height()
        return int(h)

    def desired_height(self) -> int:
        """面板总高 = 标题栏 + 夹在 [1 排, 5 排] 之间的正文高度。

        ★ 快路径（1.9.2 卡死加固的第 ③ 条）：一旦确认内容已超过封顶高度，
        高度就是常量，**不必再对全文做一次 heightForWidth**。
        文本只会增长（流式）或整体清空，所以 `_capped` 一旦置位就一直有效；
        只有在"变宽可能让行数掉回 5 排以内"时才复位（见 _reset_cap）。
        """
        if self._capped:
            return HEADER_H + self.max_content_height()
        body = self.content_height()
        if body >= self.max_content_height():
            self._capped = True
            return HEADER_H + self.max_content_height()
        return HEADER_H + max(self.min_content_height(), body)

    def _reset_cap(self):
        """内容被清空/替换或面板变宽 → 之前"已封顶"的结论作废，重算一次。"""
        self._capped = False

    def visible_lines(self) -> float:
        """当前面板实际能显示几排字（测试与排查用）。"""
        return round((self.desired_height() - HEADER_H - self.min_content_height())
                     / float(self.line_height()) + 1.0, 3)

    def _relayout_height(self):
        # ★ 防重入：setFixedHeight 会触发 Resize，若 Resize 又回调这里就是死循环
        #   （第一版把 Resize 也接进 eventFilter，结果 offscreen 下栈溢出）。
        if getattr(self, "_in_relayout", False):
            return self.height() or self.desired_height()
        self._in_relayout = True
        try:
            h = self.desired_height()
            if h != self.height():
                self.setFixedHeight(h)
            self.updateGeometry()
            return h
        finally:
            self._in_relayout = False

    # ── 内容 ──────────────────────────────────────────────
    def text(self) -> str:
        return "\n".join(t for _k, t in self._lines)

    def lines(self) -> list:
        return list(self._lines)

    def is_empty(self) -> bool:
        return not any(t.strip() for _k, t in self._lines)

    def clear(self):
        self._lines.clear()
        self._body.setText("")
        self._count.setText("")
        self._capped = False
        self._dirty = False
        self._relayout_height()
        self._scroll_to_bottom()

    # ── ★ 1.9.2：有界行数 + 合并渲染（卡死加固 ①②）──────────
    def _trim(self):
        """只保留最近 KEEP_LINES 行。

        面板最高只看得见 5 行，把上万行的思考全文一直留在 QLabel 里毫无意义：
        每次 setText 都是 O(全文)，流式碎片一来就是 O(n²)。
        """
        n = len(self._lines)
        if n > KEEP_LINES:
            del self._lines[:n - KEEP_LINES]
            self._dropped += n - KEEP_LINES

    def _schedule_render(self):
        """流式路径：标记脏 + 启动节拍器（已启动就不重复启动 = 天然合并）。"""
        self._dirty = True
        if not self._flush_timer.isActive():
            self._flush_timer.start(THROTTLE_MS)

    def _flush(self):
        """节拍器到点：把这一小段时间攒下的增量一次性渲染。"""
        if not self._dirty:
            return
        self._dirty = False
        self._render()

    def flush_now(self):
        """立刻渲染（测试 / "这轮思考结束"收尾时用，保证同步语义）。"""
        self._flush_timer.stop()
        self._dirty = False
        self._render()

    def append(self, text: str, kind: str = "think"):
        """加一行。kind: think（思考）| system（系统信息）。

        思考正文里的换行会被拆开，便于按"排"计数与滚动定位。
        """
        if text is None:
            return
        for ln in str(text).split("\n"):
            self._lines.append((kind, ln))
        self._trim()
        self._render()          # 非流式路径：立即渲染（代价小，且保持同步语义）

    def append_thinking(self, delta: str):
        """★ 流式思考增量：接到上一行后面（LLM 的 reasoning_content 就是碎片）。

        ★ 1.9.2：这里**不再**直接 `_render()`。碎片可能每秒几十上百个，
        每个都 setText(全文) + heightForWidth(全文) 会退化成 O(n²) 把 UI 线程占死
        （真机 21:55 那次"终止话题卡死"就是这么来的）。改成记账 + 节拍器合并渲染。
        """
        if not delta:
            return
        if self._lines and self._lines[-1][0] == "think":
            k, t = self._lines[-1]
            parts = (t + str(delta)).split("\n")
            self._lines[-1] = (k, parts[0])
            for p in parts[1:]:
                self._lines.append(("think", p))
        else:
            self._lines.append(("think", str(delta)))
        self._trim()
        self._schedule_render()

    def append_system(self, text: str):
        self.append(text, "system")

    def set_lines(self, lines):
        self._lines = [(str(k), str(t)) for k, t in lines]
        self._trim()
        self._reset_cap()
        self._render()

    def _sync_body_size(self) -> int:
        """把正文 Label 按"当前宽度换行后需要的高度"撑开。

        ★ 为什么必须显式做：滚动区里的 label 若不等布局周期结束就申请滚动，
        `verticalScrollBar().maximum()` 还是旧值（实测只滚到 4/433）。
        先把 label 尺寸定死，滚动范围立刻就正确了。
        """
        w = max(40, self._scroll.viewport().width())
        try:
            h = self._body.heightForWidth(w)
        except Exception:
            h = self._line_h
        h = max(self.min_content_height(), int(h or 0))
        if self._body.width() != w or self._body.height() != h:
            self._body.resize(w, h)
        return h

    def _render(self):
        self._body.setText(self.text())
        n_think = sum(1 for k, _ in self._lines if k == "think")
        n_sys = sum(1 for k, _ in self._lines if k == "system")
        bits = []
        if n_think:
            bits.append(f"思考 {n_think}")
        if n_sys:
            bits.append(f"系统 {n_sys}")
        self._count.setText(" · ".join(bits))
        self._sync_body_size()
        self._relayout_height()
        self._scroll_to_bottom()
        # ★ 1.9.2：渲染完成后回调一次 —— 让宿主（ChatWidget）在这里做占位图尺寸
        #   同步与重定位。以前宿主是**每个流式碎片**都做一次，而那次同步会触发
        #   QTextDocument 重排，同样是把 UI 线程拖死的元凶之一。
        cb = getattr(self, "_render_hook", None)
        if callable(cb):
            try:
                cb()
            except Exception:
                pass

    def set_render_hook(self, cb):
        """渲染完成后回调（宿主用来同步"占位图尺寸 + 面板位置"）。"""
        self._render_hook = cb

    def _scroll_to_bottom(self):
        sb = self._scroll.verticalScrollBar()
        sb.setValue(sb.maximum())

    # ── 滚轮隔离（对齐演示稿那段 JS）────────────────────────
    def eventFilter(self, obj, ev):
        # ★ 只处理 Wheel。**不要**在这里处理 Resize —— setFixedHeight 会触发
        #   Resize，再回调 _relayout_height 就成了无限递归（第一版实测栈溢出）。
        #   尺寸变化统一由 resizeEvent() 负责。
        if ev.type() == QEvent.Wheel:
            sb = self._scroll.verticalScrollBar()
            dy = ev.angleDelta().y()
            at_top = sb.value() <= sb.minimum() and dy > 0
            at_bottom = sb.value() >= sb.maximum() and dy < 0
            # 演示稿：只有"到顶还往上 / 到底还往下"才把事件让给外层
            ev.accept()
            if at_top or at_bottom:
                return False
            return True
        return super().eventFilter(obj, ev)

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        # ★ 变宽可能让行数掉回 5 排以内 → 之前"已封顶"的结论作废，重算一次
        self._reset_cap()
        self._relayout_height()

    def sizeHint(self) -> QSize:
        return QSize(self.width() or 300, self.desired_height())

    def minimumSizeHint(self) -> QSize:
        return QSize(120, HEADER_H + self._line_h * MIN_LINES)
