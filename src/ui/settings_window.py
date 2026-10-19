"""
设置窗口 v5.0
- 左侧竖排侧栏导航（滚轮上下滑动）
- 右侧内容区滚动
- ★ 新增"个性化"栏目：聊天窗口宽/高/距离/底色/气泡色/透明度
- 浅色主题（白底黑字）+ 圆角边
- 异步保存不卡 UI
- 模型名称自动检测下拉菜单
"""
import io
import json
import logging
import threading
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import URLError
from PySide6.QtCore import Qt, Signal, QTimer, QRectF, QEvent, QObject
from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton, QCheckBox, QSpinBox, QComboBox, QGroupBox, QScrollArea, QSlider, QFormLayout, QTextEdit, QFrame, QColorDialog, QFileDialog, QGridLayout, QApplication, QAbstractScrollArea, QAbstractSpinBox
from PySide6.QtGui import QMouseEvent, QColor, QPixmap, QImage, QPainter, QPen

from src.core.pet_manager import scan_character_cards

_log = logging.getLogger("Settings")

# ★ 1.9.0: 项目根目录 —— 头像等路径要存成"相对项目根"的形式
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


def _w_txt(widget) -> str:
    """控件文本（控件可能不存在 → 返回空串，方便兼容旧界面/测试桩）"""
    try:
        return (widget.text() or "").strip() if widget is not None else ""
    except Exception:
        return ""


def _w_csv(widget) -> list:
    """把「逗号分隔」的输入拆成数组（中英文逗号、顿号、换行都算）"""
    import re as _re
    try:
        raw = widget.text() if widget is not None else ""
    except Exception:
        raw = ""
    return [x.strip() for x in _re.split(r"[,，、\n]", raw or "") if x.strip()]


def _portable_avatar_path(p: str) -> str:
    """★ 1.9.0: 头像路径尽量存成「相对项目根」的形式。

    项目每版都会被整包复制到新目录（…/PTRTS1.8.0 → …/PTRTS1.9.0），
    config.json 里若留着绝对路径，头像就会指向旧版本文件夹
    （实际出现过 PTRTS1.8.0 的绝对路径，1.8.0 目录一动头像就丢）。
    项目内的路径存相对；项目外的手工路径保持绝对（用户自己的图片不用搬）。
    """
    v = (p or "").strip()
    if not v:
        return ""
    try:
        pp = Path(v)
        if not pp.is_absolute():
            return pp.as_posix()
        return pp.relative_to(_PROJECT_ROOT).as_posix()
    except ValueError:
        return v       # 项目外路径 → 保持原样
    except Exception:
        return v


def _abs_avatar_path(p: str) -> str:
    """★ 1.9.0: 把 config 里可能存在的相对头像路径还原成绝对路径（预览/加载用）"""
    v = (p or "").strip()
    if not v:
        return ""
    try:
        pp = Path(v)
        if pp.is_absolute():
            return str(pp)
        return str((_PROJECT_ROOT / pp).resolve())
    except Exception:
        return v


# ─── 设置分类 ───
CATEGORIES = [
    ("llm", "LLM API", "🤖"),
    ("vision", "多模态", "🧩"),
    ("personality", "角色性格", "🎭"),
    ("tts", "TTS 语音", "🔊"),
    ("monitor", "监控数据", "📊"),
    ("tools", "工具设置", "🔧"),
    # ★★ 1.9.4（用户 2026-10-18）：B站 / P站 **登录一次就开通**。
    #   用户原话："参考项目 bili_pixiv_toolkit 可以做到开通p站和B站的功能。
    #   如果用户要开通这两个功能，就**引导用户登陆一次**就可以了。"
    #   ⇒ 单独一页，两个按钮；登录完成后 cookie/token 落到项目该在的位置
    #     （B站 → data/cookies/bilibili.json；P站 → .env 的 PIXIV_REFRESH_TOKEN）。
    ("accounts", "账号开通", "🔑"),
    ("personalize", "个性化", "🎨"),
    ("usage", "使用方法", "📖"),
    # ★ 1.9.0（你的要求）：下载路径独立成一项，放最左栏最下面
    ("download_path", "下载路径", "📁"),
]


# ─── 角色选择卡片（头像 + 名称） ────────────────────────────────────

class _CharacterCard(QWidget):
    """单个角色卡片：圆形头像 + 名称，点击选中并高亮边框"""
    clicked = Signal(str)

    def __init__(self, name: str, avatar: QPixmap, selected: bool = False, parent=None):
        super().__init__(parent)
        self._name = name
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedSize(100, 120)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 10, 6, 8)
        lay.setSpacing(6)
        lay.setAlignment(Qt.AlignCenter)

        self._avatar = QLabel()
        self._avatar.setFixedSize(56, 56)
        self._avatar.setAlignment(Qt.AlignCenter)
        if avatar and not avatar.isNull():
            self._avatar.setPixmap(
                avatar.scaled(56, 56, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        else:
            self._avatar.setText("?")
            self._avatar.setStyleSheet("font-size:24px; color:#999; background:transparent;")
        lay.addWidget(self._avatar, alignment=Qt.AlignHCenter)

        self._name_lbl = QLabel(name)
        self._name_lbl.setAlignment(Qt.AlignCenter)
        self._name_lbl.setStyleSheet("font-size:12px; color:#222; background:transparent;")
        self._name_lbl.setWordWrap(True)
        lay.addWidget(self._name_lbl)

        self._set_selected(selected)

    def _set_selected(self, sel: bool):
        self._selected = sel
        if sel:
            self.setStyleSheet("background:#e8f1ff; border:2px solid #007aff; border-radius:10px;")
        else:
            self.setStyleSheet("background:#f5f5f7; border:2px solid transparent; border-radius:10px;")

    def set_selected(self, sel: bool):
        self._set_selected(sel)

    def mousePressEvent(self, event):
        self.clicked.emit(self._name)
        super().mousePressEvent(event)





class _WheelGuard(QObject):
    """★ 1.9.0（你的最终决定）：设置页**彻底禁用滚轮改值**。

    你原话（第三版要求）："直接禁用鼠标滑轮修改。"
    前两版做的是"先点一下拿到焦点才能滚"，真机都没拦住 —— 因为只要
    点过任意一个控件一次（哪怕只是选了个下拉项），焦点就一直在，
    之后悬停滚轮照样改值。所以现在**不管有没有焦点一律拦**：
      · 取值控件（QComboBox / QAbstractSpinBox / QSlider）
        上的滚轮**永远不给它** —— 有滚动区就**转交**给滚动区（页面照滚），
        没有滚动区可转才吃掉；
      · 改值只能点：下拉点开选、数字框点上下箭头或直接输入。
    ⚠ 注意是"转交"不是"吞掉"：直接 return True 把事件吞了的话，
      设置页就彻底滚不动了。
    """

    def __init__(self, win):
        super().__init__(win)
        self._win = win

    @staticmethod
    def _value_ancestor(obj):
        """往上找：obj 本身或它的某个祖先是"取值控件"吗？返回那个控件或 None。

        ★ 真机实测翻车的根因：**QSpinBox 内部盖着一个 QLineEdit 编辑器**，
          鼠标停在数字上时滚轮事件是投给那个 QLineEdit 的，`isinstance(obj, ...)`
          直接判 False → 守卫形同虚设（在测试里我是直接往 QSpinBox 发事件，所以
          测不出来）。所以必须连祖先一起认。
        """
        w = obj
        while w is not None:
            if isinstance(w, (QComboBox, QAbstractSpinBox, QSlider)):
                return w
            try:
                w = w.parentWidget()
            except Exception:
                return None
        return None

    def _is_outer_scroll(self, obj) -> bool:
        """滚轮是不是落在「工具设置页的外层滚动区」上？

        ★ 你实测："主窗口还是不固定，会随着鼠标滑轮上下滑动，两个子窗口各占一半
          就是为了不用滑动主窗口。"
          —— 光把竖条 `AlwaysOff` **拦不住滚轮**（只是看不见条，内容照样滚），
          所以这一页的外层滚动区要把滚轮直接吃掉。
          注意：落在上下两个子窗口里的滚轮要放行（那是它们自己该滚的）。
        """
        try:
            if getattr(self._win, "_active_category", "") != "tools":
                return False
            for sub in (getattr(self._win, "_tools_scroll", None),
                        getattr(self._win, "_tool_detail_scroll", None)):
                if sub is not None and (sub is obj or sub.isAncestorOf(obj)):
                    return False
            ca = getattr(self._win, "_content_area", None)
            return ca is not None and (ca is obj or ca.isAncestorOf(obj))
        except Exception:
            return False

    def eventFilter(self, obj, event):
        try:
            if event.type() != QEvent.Wheel:
                return False
            # ① 工具设置页：外层不许滚（滚轮吃掉，交给上下两个子窗口自己滚）
            if self._is_outer_scroll(obj):
                event.ignore()
                return True
            # ② ★ 直接禁用：取值控件上的滚轮**不管有没有焦点一律不给它**。
            #    有滚动区就转交（页面照滚），没有就吃掉。
            target = self._value_ancestor(obj)
            if target is None or not self._win._owns_widget(obj):
                return False
            parent = target.parentWidget()
            while parent is not None:
                if isinstance(parent, QAbstractScrollArea):
                    QApplication.sendEvent(parent.viewport(), event)
                    return True
                parent = parent.parentWidget()
            return True          # 没有滚动区可转交 → 只能吃掉
        except Exception:
            return False


# ─── 主设置窗口 ──────────────────────────────────────────────────

class SettingsWindow(QWidget):
    """设置窗口 v5.0 — 竖排侧栏 + 个性化"""

    config_changed = Signal()
    restart_monitor = Signal()
    provider_changed = Signal(str)
    _save_result_signal = Signal(str, str)  # (status, message) — 跨线程安全回调
    _models_fetched = Signal(str, list)  # (target: "llm"/"vision", model_list)

    STYLE_COMMON = """
        QLabel { background: transparent; }
        QLineEdit { background: #f0f0f5; color: #222; border: 1px solid #d0d0d5;
                    border-radius: 6px; padding: 7px 10px; font-size: 13px; }
        QLineEdit:focus { border-color: #007aff; background: #fff; }
        QComboBox { background: #f0f0f5; color: #222; border: 1px solid #d0d0d5;
                    border-radius: 6px; padding: 7px 10px; font-size: 13px; min-width: 140px; }
        QComboBox::drop-down { border: none; width: 22px; }
        QComboBox::down-arrow { image: none; border: none; }
        QComboBox QAbstractItemView { background: #fff; color: #222;
                    selection-background-color: #007aff; border: 1px solid #d0d0d5; }
        QSpinBox { background: #f0f0f5; color: #222; border: 1px solid #d0d0d5;
                   border-radius: 6px; padding: 7px; font-size: 13px; }
        QSlider::groove:horizontal { background: #e0e0e5; height: 4px; border-radius: 2px; }
        QSlider::handle:horizontal { background: #007aff; width: 14px; height: 14px;
                   border-radius: 7px; margin: -5px 0; }
        QScrollArea { border: none; background: transparent; }
        QScrollBar:vertical { background: #f5f5f7; width: 8px; border-radius: 4px; }
        QScrollBar::handle:vertical { background: #c0c0c8; border-radius: 4px; min-height: 30px; }
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
        QTextEdit { background: #f0f0f5; color: #222; border: 1px solid #d0d0d5;
                    border-radius: 8px; padding: 8px; font-size: 12px; }
    """

    SIDEBAR_WIDTH = 120

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        # ★ 1.9.0: 开透明底 —— 样式表里 SettingsWindow 那句 border-radius: 12px
        #   才看得见（以前是 False，窗口自己的方底把圆角盖住了）。
        #   同时必须开 WA_StyledBackground：自定义 QWidget 子类默认不画 QSS 背景。
        #   要回退就把这两行去掉。
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setFixedSize(620, 640)

        from src.utils.config import get_config, PROVIDER_PRESETS
        from src.core.tool_manager import get_tool_manager
        self._config = get_config()
        self._tool_manager = get_tool_manager()
        self._provider_presets = PROVIDER_PRESETS

        self._drag_pos = None
        self._active_category = "llm"
        self._category_btns: dict[str, QPushButton] = {}
        self._category_pages: dict[str, QWidget] = {}

        self._save_result_signal.connect(self._on_save_result)
        self._models_fetched.connect(self._on_models_fetched)

        self._build_ui()
        self._apply_style()
        self._load_values()
        self._switch_category("llm")

        # ★ 1.9.0: 滚轮守卫（只在窗口显示期间挂着，隐藏就摘掉，不留全局副作用）
        self._wheel_guard = _WheelGuard(self)
        self._wheel_guard_on = False

        _log.debug("设置窗口 v5.0 就绪")

    # ── 滚轮守卫的挂/摘 ──

    def _owns_widget(self, obj) -> bool:
        """obj 算不算本设置窗口的控件？

        ★ 1.9.0 踩到的坑：切换分类时用 `takeWidget()` 把上一页**摘下来**（防 C++ 对象
          被 setWidget 销毁），那一刻起它就不再是窗口的后代 —— 只用 isAncestorOf
          判断的话，守卫对"非当前页"整个失效。所以这里连页面集合一起认。
        """
        try:
            if self.isAncestorOf(obj):
                return True
            for page in getattr(self, "_category_pages", {}).values():
                if page is obj or page.isAncestorOf(obj):
                    return True
        except Exception:
            pass
        return False

    # ⚠ 别在这里再写一个 showEvent！本类后面（1.8.10 那段同步逻辑）已经有一个，
    #   Python 后者胜 —— 我第一版把守卫安装写在前面那个里，结果**根本没执行**，
    #   表现就是"滚轮还是能改值"。守卫的安装/摘除统一挂在下面那个 showEvent 和
    #   紧随其后的 hideEvent 上。
    def hideEvent(self, event):
        try:
            app = QApplication.instance()
            if app is not None and getattr(self, "_wheel_guard_on", False):
                app.removeEventFilter(self._wheel_guard)
                self._wheel_guard_on = False
        except Exception:
            pass
        super().hideEvent(event)

    # ── 拖拽移动 ──

    def paintEvent(self, event):
        """★ 1.9.0: **自己画**圆角底（设置窗口四角圆角）。

        样式表里那句 `SettingsWindow { background: #fafafa; border-radius: 12px; }`
        实测**从来没生效过**：自定义 Python 类名选择器在 QSS 里不匹配，
        而且顶层自定义窗口的 QSS 背景也不会被绘制（以前看到的底色是窗口系统的
        不透明方底）。所以开透明底 + 自己画圆角矩形。
        """
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setPen(QPen(QColor("#ccc"), 1))
        p.setBrush(QColor("#fafafa"))
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.drawRoundedRect(r, 12, 12)
        p.end()

    def mousePressEvent(self, event: QMouseEvent):
        if event.button() == Qt.LeftButton:
            self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event: QMouseEvent):
        if event.buttons() & Qt.LeftButton and self._drag_pos is not None:
            self.move(event.globalPosition().toPoint() - self._drag_pos)
            event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent):
        self._drag_pos = None
        event.accept()

    def showEvent(self, event):
        """★ 1.8.10: 每次打开设置页，先把「会被别处改动」的控件同步一次。

        聊天窗口尺寸是鼠标拖拽时**直接写进 config** 的（_on_chat_resized →
        _save_chat_size），侧栏显隐是点 ◀/▶ 时写的。而本窗口的 _load_values()
        只在 __init__ 跑过一次，之后没有任何刷新 —— 于是：
            用户拖好窗口 → 打开设置 → 点「保存设置」
        就会把窗口启动时的旧尺寸写回 config，并触发 _apply_config_to_ui()
        把刚拖好的尺寸冲掉。两条保存路径互相覆盖。
        """
        super().showEvent(event)
        try:
            ui = self._config.ui
            self._ui_chat_width.setValue(int(getattr(ui, "chat_window_width", 420)))
            self._ui_chat_height.setValue(int(getattr(ui, "chat_window_height", 640)))
        except Exception as e:
            _log.debug(f"同步聊天窗口尺寸到设置页失败: {e}")
        # ★ 1.9.0: 挂上滚轮守卫（放在最后 —— 前面那段同步逻辑保持原样不动，
        #   `_test_audit_fixes_1810` 会取 showEvent 开头一段来核对它）。
        #   用标志位保证**幂等**：showEvent 可能触发多次，重复 install 会让 Qt 的
        #   引用计数加多次，而 hideEvent 只摘一次 → 残留全局过滤器。
        try:
            app = QApplication.instance()
            if (app is not None and getattr(self, "_wheel_guard", None) is not None
                    and not getattr(self, "_wheel_guard_on", False)):
                app.installEventFilter(self._wheel_guard)
                self._wheel_guard_on = True
        except Exception as e:
            _log.debug(f"装滚轮守卫失败: {e}")

    # ── 样式 ──

    def _apply_style(self):
        self.setStyleSheet(f"""
            SettingsWindow {{ background: #fafafa; border: 1px solid #ccc; border-radius: 12px; }}
            {self.STYLE_COMMON}
        """)

    # ── 工具方法 ──

    def _section(self, title: str) -> QGroupBox:
        box = QGroupBox(title)
        box.setStyleSheet("""
            QGroupBox { color: #444; font-weight: bold; border: 1px solid #e0e0e0;
                        border-radius: 8px; margin-top: 14px; padding-top: 18px; font-size: 13px; }
            QGroupBox::title { subcontrol-origin: margin; left: 12px; padding: 0 8px;
                               color: #555; }
        """)
        return box

    @staticmethod
    def _plain(text: str) -> str:
        """★ 1.9.4：把 markdown 的 `**粗体**` 去掉。

        设置页里的 `QLabel` **不认 HTML/不认 markdown**（没有 setTextFormat(RichText)），
        所以 `**弹出的浏览器**` 会**原样显示成一堆星号** ——
        用户真机截图里就是这样（"这是发布版，不可以这样写"）。
        ⇒ 统一在显示层剥掉 `**`；`_lbl` / `_hint` 都过一遍。
        （工具描述、给模型看的文案里保留 `**` 是没问题的 —— 那不走 QLabel。）
        """
        return (text or "").replace("**", "")

    def _lbl(self, text: str, color: str = "#444") -> QLabel:
        lbl = QLabel(self._plain(text))
        lbl.setStyleSheet(f"color: {color}; font-size: 13px; background: transparent; font-weight: normal;")
        lbl.setWordWrap(True)
        return lbl

    def _hint(self, text: str) -> QLabel:
        lbl = QLabel(self._plain(text))
        lbl.setStyleSheet("color: #999; font-size: 11px; background: transparent;")
        lbl.setWordWrap(True)
        return lbl

    CHECKBOX_STYLE = """
        QCheckBox { color: #444; font-size: 13px; spacing: 8px; }
        QCheckBox::indicator { width: 18px; height: 18px; }
        QCheckBox::indicator:unchecked { background: #f0f0f5; border: 2px solid #c0c0c8; border-radius: 4px; }
        QCheckBox::indicator:checked { background: #007aff; border: 2px solid #007aff; border-radius: 4px; }
        QCheckBox::indicator:hover { border-color: #007aff; }
    """

    def _toggle_row(self, label: str, checked: bool = False) -> tuple:
        """返回 (row_widget, checkbox) 的行布局"""
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 2, 0, 2)
        cb = QCheckBox(label)
        cb.setChecked(checked)
        cb.setStyleSheet(self.CHECKBOX_STYLE)
        h.addWidget(cb)
        h.addStretch()
        return row, cb

    # ── 构建 UI 主框架（竖排侧栏）──

    def _build_ui(self):
        main = QVBoxLayout(self)
        main.setContentsMargins(14, 10, 14, 12)
        main.setSpacing(8)

        # ── 标题栏（可拖拽） ──
        title_bar = QWidget()
        title_bar.setFixedHeight(32)
        tb = QHBoxLayout(title_bar)
        tb.setContentsMargins(4, 0, 0, 0)
        title = QLabel("设置")
        title.setStyleSheet("color: #333; font-size: 15px; font-weight: bold; background: transparent;")
        tb.addWidget(title)
        tb.addStretch()
        close = QPushButton("✕")
        close.setFixedSize(26, 26)
        close.setStyleSheet("""
            QPushButton { background: transparent; color: #888; border: none; font-size: 14px; }
            QPushButton:hover { color: #333; background: #e0e0e0; border-radius: 6px; }
        """)
        close.clicked.connect(self.hide)
        tb.addWidget(close)
        main.addWidget(title_bar)

        # ── 主内容区：侧栏 + 内容 ──
        body = QHBoxLayout()
        body.setSpacing(0)

        # 左侧竖排侧栏
        sidebar = QFrame()
        sidebar.setFixedWidth(self.SIDEBAR_WIDTH)
        sidebar.setStyleSheet("""
            QFrame { background: #f0f0f2; border-right: 1px solid #e0e0e0; border-radius: 8px 0 0 8px; }
        """)
        sidebar_scroll = QScrollArea()
        sidebar_scroll.setWidgetResizable(True)
        sidebar_scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")
        sidebar_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        sidebar_inner = QWidget()
        sidebar_layout = QVBoxLayout(sidebar_inner)
        sidebar_layout.setContentsMargins(6, 8, 6, 8)
        sidebar_layout.setSpacing(2)

        for cat_id, cat_name, cat_icon in CATEGORIES:
            btn = QPushButton(f"{cat_icon}  {cat_name}")
            btn.setCheckable(True)
            btn.setStyleSheet("""
                QPushButton { background: transparent; color: #666; border: none;
                              border-radius: 6px; padding: 8px 10px; font-size: 12px; text-align: left; }
                QPushButton:hover { background: #e4e4e8; color: #333; }
                QPushButton:checked { background: #007aff; color: #fff; font-weight: bold; }
            """)
            btn.clicked.connect(lambda checked, c=cat_id: self._switch_category(c))
            self._category_btns[cat_id] = btn
            sidebar_layout.addWidget(btn)

        sidebar_layout.addStretch()
        sidebar_scroll.setWidget(sidebar_inner)

        sidebar_outer = QVBoxLayout(sidebar)
        sidebar_outer.setContentsMargins(0, 0, 0, 0)
        sidebar_outer.addWidget(sidebar_scroll)
        body.addWidget(sidebar)

        # 右侧内容区（可滚动）
        self._content_area = QScrollArea()
        self._content_area.setWidgetResizable(True)
        # ★ 1.9.0（你的要求）：设置页里一律不要**水平**滚动条（右边内容不该左右拉）
        self._content_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._content_area.setStyleSheet("QScrollArea { border: none; background: #fff; border-radius: 0 8px 8px 0; }")
        body.addWidget(self._content_area, stretch=1)
        main.addLayout(body, stretch=1)

        # ── 底部按钮 ──
        bottom = QHBoxLayout()
        self._save_status = QLabel("")
        self._save_status.setStyleSheet("color: #888; font-size: 11px; background: transparent;")
        bottom.addWidget(self._save_status)
        bottom.addStretch()
        save = QPushButton("保存设置")
        save.setStyleSheet("""
            QPushButton { background: #007aff; color: #fff; border: none;
                          border-radius: 8px; padding: 8px 28px; font-size: 13px; font-weight: bold; }
            QPushButton:hover { background: #0066dd; }
            QPushButton:pressed { background: #0055cc; }
        """)
        save.clicked.connect(self._save_settings)
        bottom.addWidget(save)
        main.addLayout(bottom)

        # 构建各分类页面
        self._category_pages["llm"] = self._create_llm_page()
        self._category_pages["vision"] = self._create_vision_page()
        self._category_pages["personality"] = self._create_personality_page()
        self._category_pages["tts"] = self._create_tts_page()
        self._category_pages["monitor"] = self._create_monitor_page()
        self._category_pages["tools"] = self._create_tools_page()
        self._category_pages["accounts"] = self._create_accounts_page()
        self._category_pages["download_path"] = self._create_download_path_page()
        self._category_pages["personalize"] = self._create_personalize_page()
        self._category_pages["usage"] = self._create_usage_page()

    def _switch_category(self, cat_id: str):
        self._active_category = cat_id
        for bid, btn in self._category_btns.items():
            btn.setChecked(bid == cat_id)
        page = self._category_pages.get(cat_id)
        if page:
            # ★ 1.9.0（你的要求）：主窗口不要有滚轮 —— 「工具设置」页自己上下各有一个
            #   子窗口滚动区，外面这层再挂一条就是**嵌套滚动**，滚起来很难受。
            #   所以这一页把外层竖条关掉，别的页（内容可能很长）维持按需。
            try:
                self._content_area.setVerticalScrollBarPolicy(
                    Qt.ScrollBarAlwaysOff if cat_id == "tools" else Qt.ScrollBarAsNeeded)
            except Exception:
                pass
            # ★ 安全切换：先用 takeWidget() 取出旧页面，防止 setWidget() 销毁 C++ 对象
            if self._content_area.widget() and self._content_area.widget() != page:
                self._content_area.takeWidget()
            if self._content_area.widget() != page:
                self._content_area.setWidget(page)

    # ════════════════════════════════════════════════════════════
    # 使用方法页面（★ 1.5.0）
    # ════════════════════════════════════════════════════════════

    def _create_usage_page(self):
        """★ 1.5.0: 新增「使用方法」页（右侧内容区自带滚动条）"""
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(10)

        title = QLabel("📖 使用方法")
        title.setStyleSheet("font-size:16px; font-weight:bold; color:#222;")
        layout.addWidget(title)

        html = """
        <style>
          p { margin: 2px 0 10px 0; }
          .h { color:#007aff; font-weight:bold; font-size:13px; }
          .k { color:#007aff; font-weight:bold; }
        </style>

        <p><span class="h">💬 发送消息</span><br>
        在聊天窗口底部输入框输入内容，按回车或点「发送」即可与桌宠对话。<br>
        发送文件 / 图片：点输入框左侧的「＋」上传，或直接把文件<b>拖进聊天窗口</b>。</p>

        <p><span class="h">🖼️ 图片预览</span><br>
        把图片拖进聊天窗口，或点附件按钮发送图片 → 聊天里直接显示缩略图。<br>
        · <span class="k">左键</span>点缩略图：用系统图片查看器打开<b>原图</b>（原图始终在你原本的位置，不复制）。<br>
        · <span class="k">右键</span>点缩略图：复制原图路径到剪贴板。</p>

        <p><span class="h">🗂️ 对话窗口：一个角色一个窗口</span><br>
        每个桌宠形象都有<b>自己专属的连续对话窗口</b>：所有话题都在同一个窗口里连下去，切换形象不会串味。<br>
        · 左侧栏顶部显示当前角色与窗口状态（消息数 / token 数），下面是本窗口的对话记录回看。<br>
        · <span class="k">＋ 开启新话题</span>：直接开一段全新对话（不总结上一段）。<br>
        · <span class="k">📝 总结旧话题</span>：先把当前对话总结成一段记忆，再开新话题并<b>继承这段总结</b>，话题可以无缝延续。<br>
        · 上下文写满时，桌宠会<b>自动</b>总结旧话题并开启新话题延续，不会打断你。</p>

        <p><span class="h">🎭 切换桌宠形象</span><br>
        设置 → 角色性格 → 点「头像 + 名称」卡片选中角色 → 调整缩放 / 性格提示词后点保存。<br>
        · 每个角色<b>独立保存</b>设置（缩放、人设、回复风格、emoji），互不干扰。<br>
        · 关闭时用的形象和设置会被记住，下次启动自动恢复。<br>
        · 切换角色时，左侧栏会跟着换成该角色的对话窗口。</p>

        <p><span class="h">📚 侧栏显示</span><br>
        侧栏默认贴在聊天窗口左边，点左上角的 <span class="k">◀</span> 收起，收起后按钮变成 <span class="k">▶</span>，再点一次展开。<br>
        侧栏只是浮层，展开 / 收起<b>不会改变聊天窗口的大小和位置</b>。</p>

        <p><span class="h">🐾 桌宠动作</span><br>
        桌面上的形象会根据状态自动切换 GIF（待机 / 思考 / 说话 / 执行），由「形象线程」根据 LLM 思考内容选择最合适的动作。<br>
        · 「启动默认 GIF」在启动 / 切换形象时只播放<b>一遍</b>，播完自动进入「待机默认」轮播。</p>

        <p><span class="h">🔧 工具与沙箱</span><br>
        桌宠可调用终端、Office、知识图谱等工具帮你做事。<br>
        · 你的电脑默认<b>只读</b>，只有 <span class="k">data/桌宠私有data</span> 目录可写。<br>
        · Office / 知识图谱生成的结果也保存在该目录下。</p>

        <p><span class="h">⚙️ 设置 / 退出</span><br>
        点系统托盘图标，或聊天窗口标题栏的「≡」菜单 → 设置 / 显示隐藏 / 退出。</p>

        <p>&nbsp;</p><p>&nbsp;</p><p>&nbsp;</p><p>&nbsp;</p>
        <p>&nbsp;</p><p>&nbsp;</p><p>&nbsp;</p><p>&nbsp;</p>
        <p>&nbsp;</p><p>&nbsp;</p><p>&nbsp;</p><p>&nbsp;</p>
        <p>&nbsp;</p><p>&nbsp;</p><p>&nbsp;</p><p>&nbsp;</p>
        <p>&nbsp;</p><p>&nbsp;</p><p>&nbsp;</p><p>&nbsp;</p>
        <p style="color:#bbb; font-size:12px;">（本页右侧可滚动 —— 内容超出窗口高度时会出现滚动条）</p>
        """
        body = QLabel(html)
        body.setWordWrap(True)
        body.setTextFormat(Qt.RichText)
        body.setStyleSheet("font-size:13px; line-height:1.7; color:#333;")
        body.setOpenExternalLinks(False)
        layout.addWidget(body)
        layout.addStretch()
        return page

    # ════════════════════════════════════════════════════════════
    # LLM API 页面
    # ════════════════════════════════════════════════════════════

    def _create_llm_page(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(10)

        g0 = self._section("LLM 提供商")
        f0 = QFormLayout(g0)
        f0.setSpacing(8)
        self._llm_provider_combo = QComboBox()
        for key, preset in self._provider_presets.items():
            self._llm_provider_combo.addItem(preset["name"], key)
        self._llm_provider_combo.currentIndexChanged.connect(self._on_llm_provider_changed)
        f0.addRow(self._lbl("提供商:"), self._llm_provider_combo)
        self._llm_provider_hint = self._hint("")
        f0.addRow("", self._llm_provider_hint)
        layout.addWidget(g0)

        g1 = self._section("API 连接参数")
        f1 = QFormLayout(g1)
        f1.setSpacing(8)
        self._llm_host = QLineEdit()
        self._llm_host.setPlaceholderText("https://api.deepseek.com/v1")
        f1.addRow(self._lbl("Host URL:"), self._llm_host)
        self._llm_key = QLineEdit()
        self._llm_key.setEchoMode(QLineEdit.Password)
        self._llm_key.setPlaceholderText("sk-...")
        f1.addRow(self._lbl("API Key:"), self._llm_key)
        model_row = QHBoxLayout()
        self._llm_model_combo = QComboBox()
        self._llm_model_combo.setEditable(True)
        self._llm_model_combo.setPlaceholderText("点击「检测」获取模型列表...")
        self._llm_model_combo.lineEdit().setPlaceholderText("点击「检测」获取模型列表...")
        self._llm_model_combo.setMinimumWidth(260)
        model_row.addWidget(self._llm_model_combo, 1)
        btn = QPushButton("检测")
        btn.setFixedSize(52, 30)
        btn.setStyleSheet("""
            QPushButton { background: #e0e0e5; color: #333; border: 1px solid #d0d0d5;
                          border-radius: 6px; font-size: 12px; }
            QPushButton:hover { background: #007aff; color: #fff; border-color: #007aff; }
        """)
        btn.clicked.connect(lambda: self._detect_models("llm"))
        model_row.addWidget(btn)
        f1.addRow(self._lbl("模型名称:"), model_row)
        f1.addRow("", self._hint("点击「检测」从 API 获取可用模型列表。切换提供商自动检测。"))
        # ★ 1.9.3 第 23 轮（用户："我设置里面改不了 max_tokens"）：这一项以前**界面里没有**，
        #   只能手改 config.json —— 而程序退出时会按内存里的值把 config.json 重写，
        #   所以手改经常"改了没用"（真机已经栽过两次）。现在放进设置里，改名正言顺。
        self._llm_max_tokens = QSpinBox()
        self._llm_max_tokens.setRange(256, 65536)
        self._llm_max_tokens.setSingleStep(512)
        self._llm_max_tokens.setValue(8192)
        self._llm_max_tokens.setMinimumWidth(140)
        f1.addRow(self._lbl("最大输出 tokens:"), self._llm_max_tokens)
        f1.addRow("", self._hint(
            "单次回复的输出上限。★ 用思考型模型（deepseek-flash / reasoner）时别调太小 ——\n"
            "思考内容也会吃这个预算，太小会导致「空回复」或正文被截半句（建议 ≥4096，默认 8192）。"))
        layout.addWidget(g1)

        layout.addStretch()
        return w

    def _on_llm_provider_changed(self, idx: int):
        key = self._llm_provider_combo.currentData()
        preset = self._provider_presets.get(key, {})
        if preset and key != "custom":
            self._llm_host.setText(preset.get("host", ""))
        self._llm_provider_hint.setText(f"当前: {preset.get('name', key)} | 端点: {preset.get('host', '')}")
        self._detect_models("llm")

    # ── 模型检测 ──

    def _detect_models(self, target: str):
        # ★ 1.8.10: key 要按 target 分别取 —— 以前不管检测哪个都用
        #   "LLM key 优先"，配了独立视觉 Key 时点「检测」会拿错 Key 去鉴权
        #   （要么 401，要么列出来的是另一个服务商的模型）。
        if target == "llm":
            host = self._llm_host.text().strip()
            api_key = (self._llm_key.text().strip()
                       or self._vision_api_key.text().strip())
        else:
            host = self._vision_api_host.text().strip()
            api_key = (self._vision_api_key.text().strip()
                       or self._llm_key.text().strip())

        if not host:
            _log.warning(f"[{target}] Host 为空，无法检测模型")
            self._models_fetched.emit(target, [])
            return

        if target == "llm":
            self._llm_model_combo.setPlaceholderText("检测中...")
        else:
            self._vision_api_model_combo.setPlaceholderText("检测中...")

        def _fetch():
            result = []
            try:
                models_url = host.rstrip("/") + "/models"
                req = Request(models_url, method="GET")
                req.add_header("Accept", "application/json")
                if api_key:
                    req.add_header("Authorization", f"Bearer {api_key}")
                with urlopen(req, timeout=8) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    items = data.get("data", [])
                    result = [m["id"] for m in items if "id" in m]
                    result.sort()
                _log.info(f"[{target}] 检测到 {len(result)} 个模型")
            except URLError as e:
                _log.warning(f"[{target}] 模型检测失败 (网络): {e}")
            except json.JSONDecodeError as e:
                _log.warning(f"[{target}] 模型检测失败 (格式): {e}")
            except Exception as e:
                _log.warning(f"[{target}] 模型检测异常: {e}")
            finally:
                self._models_fetched.emit(target, result)

        threading.Thread(target=_fetch, daemon=True).start()

    def _on_models_fetched(self, target: str, models: list):
        combo = self._llm_model_combo if target == "llm" else self._vision_api_model_combo
        current_text = combo.currentText().strip()

        combo.blockSignals(True)
        combo.clear()
        if models:
            combo.addItems(models)
            idx = combo.findText(current_text)
            if idx >= 0:
                combo.setCurrentIndex(idx)
            else:
                combo.setCurrentIndex(-1)
                combo.setEditText(current_text if current_text else "")
            combo.setPlaceholderText(f"已检测 {len(models)} 个模型")
            _log.info(f"[{target}] 模型列表已更新: {len(models)} 个")
        else:
            combo.setCurrentIndex(-1)
            combo.setEditText(current_text)
            combo.setPlaceholderText("检测失败或无模型，可手动输入")
            _log.warning(f"[{target}] 未检测到模型，请检查 Host 和 API Key")
        combo.blockSignals(False)

    # ════════════════════════════════════════════════════════════
    # 视觉识别页面
    # ════════════════════════════════════════════════════════════

    def _create_vision_page(self) -> QWidget:
        """★ 1.9.0: 本页由「视觉识别」改为「多模态」。

        三块**互不干扰**、各自独立开关（你反馈"设置太散、三相互相影响"）：
          ① 多模态直传 —— 图片直接给模型看
             （api = 把图塞给对话模型自己看；ollama = 用本地视觉模型先看图）
          ② 视觉识别 —— 先把图片识别成文字再回灌（api / ollama / ocr）
          ③ STT 语音转文字 —— 之前**只有 config.json 里的 stt 段，没有设置界面**
        """
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(10)

        # ── ① 多模态直传（最上面）──
        g_mm = self._section("① 多模态（图片直传）")
        f_mm = QFormLayout(g_mm)
        f_mm.setSpacing(8)
        row_mm, self._mm_enabled = self._toggle_row("启用多模态直传", checked=False)
        f_mm.addRow(row_mm)
        self._mm_mode = QComboBox()
        self._mm_mode.addItem("云端 API — 把图直接塞给对话模型自己看", "api")
        self._mm_mode.addItem("Ollama 本地 — 用本地视觉模型先看图", "ollama")
        self._mm_mode.currentIndexChanged.connect(self._on_mm_mode_changed)
        f_mm.addRow(self._lbl("多模态选择:"), self._mm_mode)
        self._mm_hint = self._hint("")
        f_mm.addRow("", self._mm_hint)
        layout.addWidget(g_mm)

        # ── ② 视觉识别（与①独立）──
        # ★ 1.5.0: 视觉总开关（蓝色填充=开启，灰=关闭）
        row, self._vision_enabled = self._toggle_row("启用视觉识别", checked=True)
        layout.addWidget(row)
        layout.addWidget(self._hint(
            "② 视觉识别：把图片**先识别成文字**再交给对话模型（对话模型不需要支持看图）。\n"
            "与上面的①互相独立：两个都开时以①的多模态优先，模型不支持看图会自动降级到②。"))

        g0 = self._section("识别模式")
        f0 = QFormLayout(g0)
        f0.setSpacing(8)
        self._vision_mode = QComboBox()
        self._vision_mode.addItem("云端 API 视觉模型", "api")
        self._vision_mode.addItem("Ollama 本地视觉模型", "ollama")
        self._vision_mode.addItem("本地 OCR 引擎", "ocr")
        self._vision_mode.currentIndexChanged.connect(self._on_vision_mode_changed)
        f0.addRow(self._lbl("模式:"), self._vision_mode)
        f0.addRow("", self._hint("API: 调用云端视觉大模型 | Ollama: 本地多模态模型 | OCR: 传统文字识别"))
        layout.addWidget(g0)

        self._vision_api_group = self._section("API 视觉模型配置")
        f_api = QFormLayout(self._vision_api_group)
        f_api.setSpacing(8)
        self._vision_api_host = QLineEdit()
        self._vision_api_host.setPlaceholderText("https://api.deepseek.com/v1")
        f_api.addRow(self._lbl("Host URL:"), self._vision_api_host)
        self._vision_api_key = QLineEdit()
        self._vision_api_key.setEchoMode(QLineEdit.Password)
        self._vision_api_key.setPlaceholderText("API Key")
        f_api.addRow(self._lbl("API Key:"), self._vision_api_key)
        vision_model_row = QHBoxLayout()
        self._vision_api_model_combo = QComboBox()
        self._vision_api_model_combo.setEditable(True)
        self._vision_api_model_combo.setPlaceholderText("点击「检测」获取模型列表...")
        self._vision_api_model_combo.lineEdit().setPlaceholderText("点击「检测」获取模型列表...")
        self._vision_api_model_combo.setMinimumWidth(260)
        vision_model_row.addWidget(self._vision_api_model_combo, 1)
        btn_v = QPushButton("检测")
        btn_v.setFixedSize(52, 30)
        btn_v.setStyleSheet("""
            QPushButton { background: #e0e0e5; color: #333; border: 1px solid #d0d0d5;
                          border-radius: 6px; font-size: 12px; }
            QPushButton:hover { background: #007aff; color: #fff; border-color: #007aff; }
        """)
        btn_v.clicked.connect(lambda: self._detect_models("vision"))
        vision_model_row.addWidget(btn_v)
        f_api.addRow(self._lbl("模型名称:"), vision_model_row)
        f_api.addRow("", self._hint("使用与 LLM 相同的 API 或专用多模态 API。点击「检测」获取可用模型列表。"))
        layout.addWidget(self._vision_api_group)

        self._vision_ollama_group = self._section("Ollama 本地视觉模型")
        f_ol = QFormLayout(self._vision_ollama_group)
        f_ol.setSpacing(8)
        self._vision_ollama_host = QLineEdit()
        self._vision_ollama_host.setPlaceholderText("http://localhost:11434/v1")
        f_ol.addRow(self._lbl("Ollama Host:"), self._vision_ollama_host)
        self._vision_ollama_model_combo = QComboBox()
        self._vision_ollama_model_combo.setEditable(True)
        self._vision_ollama_model_combo.setPlaceholderText("llava:latest")
        f_ol.addRow(self._lbl("视觉模型:"), self._vision_ollama_model_combo)
        f_ol.addRow("", self._hint("需要支持图像的多模态模型 (llava / llama3.2-vision / minicpm-v 等)"))
        layout.addWidget(self._vision_ollama_group)

        self._vision_ocr_group = self._section("本地 OCR 引擎配置")
        f_ocr = QFormLayout(self._vision_ocr_group)
        f_ocr.setSpacing(8)
        self._vision_ocr_engine = QComboBox()
        self._vision_ocr_engine.addItem("Tesseract", "tesseract")
        self._vision_ocr_engine.addItem("EasyOCR", "easyocr")
        f_ocr.addRow(self._lbl("OCR 引擎:"), self._vision_ocr_engine)
        self._vision_ocr_lang = QLineEdit()
        self._vision_ocr_lang.setPlaceholderText("chi_sim+eng")
        f_ocr.addRow(self._lbl("语言 (Tesseract):"), self._vision_ocr_lang)
        f_ocr.addRow("", self._hint("Tesseract 需额外安装系统程序 | EasyOCR 首次自动下载模型"))
        layout.addWidget(self._vision_ocr_group)

        # ── ③ STT 语音转文字（与①②完全独立；此前没有设置界面）──
        # ★ 1.9.0（你的要求）：STT **写死只用项目内 data/whisper 这一套**
        #   （whisper-cli.exe + ggml-small.bin），不再给模式/模型/路径等一堆选择 ——
        #   这里只留一个「是否启用」复选框，其余全部由代码固定。
        g_stt = self._section("③ STT 语音转文字")
        f_stt = QFormLayout(g_stt)
        f_stt.setSpacing(8)
        row_stt, self._stt_enabled = self._toggle_row("启用语音转文字（STT）", checked=False)
        f_stt.addRow(row_stt)
        _cli = _PROJECT_ROOT / "data" / "whisper" / "whisper-cli.exe"
        _mdl = _PROJECT_ROOT / "data" / "whisper" / "ggml-small.bin"
        _stt_state = []
        if not _cli.exists():
            _stt_state.append("缺 whisper-cli.exe")
        if not _mdl.exists():
            _stt_state.append("缺 ggml-small.bin（约 465MB）")
        f_stt.addRow("", self._hint(
            "语音转文字固定使用项目内的 data/whisper/（whisper-cli.exe + ggml-small.bin），\n"
            "不需要选模式或模型；与①②完全独立，关掉不影响图片识别。\n"
            + ("✅ 引擎与模型都在位，可以直接用。"
               if not _stt_state else
               "⚠ 还缺：" + "、".join(_stt_state) + "（补齐后重启即可）")))
        layout.addWidget(g_stt)

        layout.addStretch()
        return w

    def _on_mm_mode_changed(self, idx: int):
        """★ 1.9.0: ① 多模态后端切换 → 更新提示（②的 Ollama 配置由下面那段负责）"""
        mode = self._mm_mode.currentData()
        if mode == "ollama":
            self._mm_hint.setText(
                "本地模式：用下方「② Ollama 本地视觉模型」里的 Host / 模型来看图。\n"
                "那里留空时会自动用检测到的本地多模态模型。")
        else:
            self._mm_hint.setText(
                "云端模式：把图片作为图片内容直接发给**对话模型**（在「LLM API」页配置的模型）。\n"
                "对话模型不支持看图时会自动降级到②的识别方式，不会报错。")

    def _apply_media_settings(self, cfg):
        """★ 1.9.0: 把「① 多模态 / ② 视觉识别 / ③ STT」三块控件写回 config。

        三块**互不干扰**：各自读各自的复选框，绝不互相覆盖
        （你反馈"设置太散、三相互相影响"）。

        抽成独立方法是为了**可测**：它只赋值、不落盘，
        自测可以直接拿一个 stub/真实窗口调它，不必真写 config.json。
        """
        # ① 多模态（图片直传）
        cfg.vision.multimodal = self._mm_enabled.isChecked()
        cfg.vision.multimodal_mode = self._mm_mode.currentData() or "api"
        # ② 视觉识别（enabled 由调用方先赋，这里补齐其余字段以防漏）
        cfg.vision.enabled = self._vision_enabled.isChecked()
        # ③ STT（★ 1.9.0：写死走项目内 data/whisper，页面上只留一个启用开关）
        cfg.stt.enabled = self._stt_enabled.isChecked()
        cfg.stt.mode = "whisper_local"
        cfg.stt.model_name = "small"
        cfg.stt.language = (getattr(cfg.stt, "language", "") or "zh")
        # 路径留空 = 用 whisper_local 里的默认值（data/whisper/whisper-cli.exe + ggml-small.bin）
        cfg.stt.whisper_cli_path = ""
        cfg.stt.whisper_model_path = ""
        try:
            cfg.stt.whisper_threads = int(getattr(cfg.stt, "whisper_threads", 4) or 4)
        except Exception:
            cfg.stt.whisper_threads = 4
        return cfg

    def _on_vision_mode_changed(self, idx: int):
        mode = self._vision_mode.currentData()
        self._vision_api_group.setVisible(mode == "api")
        self._vision_ollama_group.setVisible(mode == "ollama")
        self._vision_ocr_group.setVisible(mode == "ocr")

    # ════════════════════════════════════════════════════════════
    # 角色 & 性格页面
    # ════════════════════════════════════════════════════════════

    def _create_personality_page(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(10)

        g0 = self._section("桌宠形象")
        f0 = QFormLayout(g0)
        f0.setSpacing(8)
        # ★ 1.5.0: 角色选择改为「头像 + 名称」卡片（动态扫描 data/桌宠形象）
        #   注：菲比只是 TTS 语音声线名称，不是桌宠形象，不在此列出（见 TTS 页）
        self._selected_character = "铃兰"
        self._character_cards = {}
        self._char_container = QWidget()
        self._char_layout = QGridLayout(self._char_container)
        self._char_layout.setSpacing(10)
        self._char_layout.setContentsMargins(0, 0, 0, 0)
        self._refresh_character_cards()
        f0.addRow(self._lbl("角色:"), self._char_container)

        self._pet_scale_label = QLabel("1.0x")
        self._pet_scale_label.setStyleSheet("color: #333; font-size: 12px; background: transparent; min-width: 36px;")
        self._pet_scale = QSlider(Qt.Horizontal)
        self._pet_scale.setRange(50, 200)
        self._pet_scale.setValue(100)
        self._pet_scale.valueChanged.connect(lambda v: self._pet_scale_label.setText(f"{v/100:.1f}x"))
        scale_row = QHBoxLayout()
        scale_row.addWidget(self._pet_scale)
        scale_row.addWidget(self._pet_scale_label)
        f0.addRow(self._lbl("缩放:"), scale_row)
        layout.addWidget(g0)

        # ★ 1.9.0（你的第二次更正）：性格这块**就只有一个文本框**。
        #   你原话："那些标记是自然语言性格描述的一部分，不是真正的'标记'。"
        #   —— 所以：
        #     · 16 个标准标记复选框**删掉**（它们是自然语言的人设描述，不该当开关用）
        #     · 保存时**不再跑 normalize_persona**（那会把正文里"像规则"的整句
        #       抽走变成标记 → 你下次打开发现刚写的句子不见了，是之前最难受的一点）
        #     · `identity` 字段仍在文件里（迁移时解析出来的），但界面上不显示、不修改
        g1 = self._section("性格（它是谁、怎么说话）")
        f1 = QFormLayout(g1)
        f1.setSpacing(8)
        f1.addRow("", self._hint("角色设定正文，直接进系统提示词。"
                                 "就照你想的那样写人话：它是谁、什么脾气、怎么说话。"))
        self._pet_prompt_edit = QTextEdit()
        self._pet_prompt_edit.setPlaceholderText(
            "例如：你是 deep seek，蓝色大肥鱼鲸鱼娘。日常可用傲娇、甜、懒的语气……"
        )
        self._pet_prompt_edit.setMinimumHeight(150)
        self._pet_prompt_edit.setMaximumHeight(220)
        f1.addRow(self._pet_prompt_edit)
        layout.addWidget(g1)

        g1b = self._section("输出控制（输出成什么样）")
        f1b = QFormLayout(g1b)
        f1b.setSpacing(8)

        self._pet_reply_style = QComboBox()
        self._pet_reply_style.addItem("简洁 (2-3句)", "concise")
        self._pet_reply_style.addItem("适中 (3-5句)", "balanced")
        self._pet_reply_style.addItem("详细 (不限制)", "detailed")
        f1b.addRow(self._lbl("回复风格:"), self._pet_reply_style)

        self._pet_emoji_level = QComboBox()
        self._pet_emoji_level.addItem("不使用颜文字", "none")
        self._pet_emoji_level.addItem("少量 (偶尔)", "light")
        self._pet_emoji_level.addItem("适中", "medium")
        self._pet_emoji_level.addItem("较多 (活泼)", "heavy")
        f1b.addRow(self._lbl("颜文字使用:"), self._pet_emoji_level)

        row_out, self._pet_no_action = self._toggle_row("禁止动作/神态/旁白描写", checked=True)
        f1b.addRow(row_out)
        row_out2, self._pet_no_markdown = self._toggle_row("严禁 markdown 字符", checked=False)
        f1b.addRow(row_out2)
        row_out3, self._pet_allow_code = self._toggle_row("允许代码块", checked=True)
        f1b.addRow(row_out3)
        row_out4, self._pet_allow_json = self._toggle_row("允许输出 JSON", checked=True)
        f1b.addRow(row_out4)

        self._pet_lang = QLineEdit()
        self._pet_lang.setPlaceholderText("语言，例如 zh_cn（可留空）")
        f1b.addRow(self._lbl("语言:"), self._pet_lang)
        self._pet_output_extra = QLineEdit()
        self._pet_output_extra.setPlaceholderText("额外输出约束（可留空）")
        f1b.addRow(self._lbl("额外约束:"), self._pet_output_extra)
        layout.addWidget(g1b)

        g1c = self._section("行为控制（它会做什么）")
        f1c = QFormLayout(g1c)
        f1c.setSpacing(8)
        self._pet_meme_tendency = QComboBox()
        self._pet_meme_tendency.addItem("不发", "off")
        self._pet_meme_tendency.addItem("很少", "rare")
        self._pet_meme_tendency.addItem("正常", "normal")
        self._pet_meme_tendency.addItem("较多", "often")
        f1c.addRow(self._lbl("表情包倾向:"), self._pet_meme_tendency)
        self._pet_meme_tags = QLineEdit()
        self._pet_meme_tags.setPlaceholderText("优先挑哪些情绪（逗号分隔），例如：happy, daily")
        f1c.addRow(self._lbl("优先情绪:"), self._pet_meme_tags)
        row_b1, self._pet_gif_reaction = self._toggle_row("按工具/情绪切换形象动作", checked=True)
        f1c.addRow(row_b1)
        row_b2, self._pet_proactive = self._toggle_row("允许主动搭话（可观察你在用什么程序）",
                                                       checked=True)
        f1c.addRow(row_b2)
        layout.addWidget(g1c)

        # ★ 1.5.0: 默认行为 = 自动扫描当前角色 GIF 文件夹，供用户选择
        g2 = self._section("默认行为")
        f2 = QFormLayout(g2)
        f2.setSpacing(8)
        self._pet_startup_gif = QComboBox()
        self._pet_startup_gif.setMinimumWidth(200)
        f2.addRow(self._lbl("启动默认 GIF:"), self._pet_startup_gif)
        f2.addRow("", self._hint("启动或切换形象时显示的第一个 GIF（自动扫描当前角色 GIF 文件夹）。"))
        layout.addWidget(g2)

        # ★ 1.9.0: 这里原本加过「行为动作 GIF」四槽（思考/执行/说话/待机），
        #   已按你的反馈**整块删除** —— 动作不靠人去配映射表，
        #   而是由 LLM 在思考末尾直接按 GIF **文件名**挑（文件名就说清了内容），
        #   见 pet_manager._parse_gif_from_thinking。
        #   设置页保留「启动默认」与「待机默认轮播」这两项真正需要人配的。

        # 待机默认 GIF（多选 + 可滚动）
        g_idle = self._section("待机默认 GIF（多选）")
        idle_lay = QVBoxLayout(g_idle)
        idle_lay.setSpacing(4)
        self._pet_idle_gifs_area = QScrollArea()
        self._pet_idle_gifs_area.setWidgetResizable(True)
        self._pet_idle_gifs_area.setMinimumHeight(130)
        self._pet_idle_gifs_area.setStyleSheet(
            "QScrollArea { background:#fafafa; border:1px solid #e0e0e0; border-radius:6px; }")
        self._pet_idle_gifs_box = QWidget()
        self._pet_idle_gifs_layout = QVBoxLayout(self._pet_idle_gifs_box)
        self._pet_idle_gifs_layout.setContentsMargins(8, 6, 8, 6)
        self._pet_idle_gifs_layout.setSpacing(3)
        self._pet_idle_gifs_area.setWidget(self._pet_idle_gifs_box)
        self._pet_idle_checks: dict[str, QCheckBox] = {}
        idle_lay.addWidget(self._pet_idle_gifs_area)
        idle_lay.addWidget(self._hint("勾选的 GIF 会在待机时按「切换规则」轮播。"))

        # 切换规则（模式下拉 + 滚轮调时间，不出现列表）
        rule_lay = QHBoxLayout()
        self._pet_idle_rule_mode = QComboBox()
        self._pet_idle_rule_mode.addItem("随机时间切换", "random")
        self._pet_idle_rule_mode.addItem("固定时间切换", "fixed")
        self._pet_idle_rule_mode.currentIndexChanged.connect(self._on_idle_rule_mode_changed)
        rule_lay.addWidget(self._pet_idle_rule_mode)
        # ★ 接管（2026-09-17 23:05，你的批注"#直接删掉-/+按钮，本来就不怎么用"）：
        #   原来这里是个自绘的「− 值 ＋」控件 —— 那两个按钮是滚轮被禁后临时补的，
        #   而且「＋」一直点不中。现在换成**标准 QSpinBox**（和其他数值项一致：
        #   直接改数字 / 点系统自带的小箭头都行），滚轮仍由 _WheelGuard 一律拦掉。
        #   `_WheelValue` 这个自绘控件整个删掉了。
        self._pet_idle_rule_value = QSpinBox()
        self._pet_idle_rule_value.setRange(1, 10)
        self._pet_idle_rule_value.setValue(5)
        self._pet_idle_rule_value.setSuffix(" 随机度")
        self._pet_idle_rule_value.setFixedWidth(112)
        rule_lay.addWidget(self._pet_idle_rule_value)
        rule_lay.addStretch()
        f2.addRow(self._lbl("切换规则:"), rule_lay)
        layout.addWidget(g_idle)

        layout.addStretch()
        return w

    # ─── 角色卡片：动态扫描 + 选中 ───

    def _refresh_character_cards(self):
        """动态扫描 data/桌宠形象，构建头像+名称卡片（菲比等纯语音声线不列出）"""
        while self._char_layout.count():
            item = self._char_layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
        self._character_cards.clear()

        cards = scan_character_cards()
        if not cards:
            self._char_layout.addWidget(self._hint("未找到任何角色（data/桌宠形象/ 下无可用角色）"))
            return

        cols = 3
        for i, c in enumerate(cards):
            avatar = self._avatar_qpixmap(c.get("avatar") or "", 56)
            card = _CharacterCard(
                c["name"], avatar,
                selected=(c["name"] == self._selected_character))
            card.clicked.connect(self._on_character_card_clicked)
            self._character_cards[c["name"]] = card
            self._char_layout.addWidget(card, i // cols, i % cols)

    def _on_character_card_clicked(self, name: str):
        # ★ 1.8.4: 切角色前先把**当前角色**的改动写回磁盘。
        #   否则"改了角色 A 的人设 → 点一下角色 B"就把 A 的改动丢了
        #   （_load_character_settings_to_ui 会把 UI 覆盖成 B 的值）。
        if name != getattr(self, "_selected_character", None):
            self.flush_current_character_settings()
        self._selected_character = name
        for n, card in self._character_cards.items():
            card.set_selected(n == name)
        # ★ 1.5.0: 选中卡片即加载该角色自己的 settings 到 UI（persona/scale/style/emoji）
        self._load_character_settings_to_ui(name)

    # ─── ★ 1.8.4: 退出/切角色时把设置强制落盘 ───

    def flush_current_character_settings(self) -> bool:
        """把性格页当前值写回**当前选中角色**的 settings.json。

        - 退出程序时调用 → "没点保存设置"的改动也不丢；
        - 切换角色卡片前调用 → "改 A → 点 B"不会丢 A 的改动。
        """
        try:
            name = getattr(self, "_selected_character", "") or ""
            if not name:
                return False
            persona = self._pet_prompt_edit.toPlainText().strip()
            scale = self._pet_scale.value() / 100.0
            style = self._pet_reply_style.currentData() or "concise"
            emoji = self._pet_emoji_level.currentData() or "light"
            startup = self._pet_startup_gif.currentData() or ""
            if not startup:
                startup = self._normalize_startup_gif(self._pet_startup_gif.currentText())
            idle = [g for g, cb in self._pet_idle_checks.items() if cb.isChecked()]
            rule = {"mode": self._pet_idle_rule_mode.currentData() or "random",
                    "value": self._pet_idle_rule_value.value()}
            self._save_character_settings(name, persona, scale, style, emoji,
                                          startup, idle, rule)
            # 全局字段同步（切换/退出后主窗口接着用）
            self._config.pet.character = name
            self._config.pet.personality_prompt = persona
            self._config.pet.scale = scale
            self._config.pet.reply_style = style
            self._config.pet.emoji_level = emoji
            _log.info(f"[落盘] 角色「{name}」设置已写回 settings.json")
            return True
        except Exception as e:
            _log.warning(f"[落盘] 角色设置写回失败: {e}")
            return False

    def flush_all_on_exit(self):
        """★ 1.8.4: 退出时把设置全部落盘。

        全局 config + 当前角色的 settings.json。
        其他角色（"两套角色"里的另一套）的 settings.json 早就在磁盘上了
        （只在被选中编辑时才写），这里不需要也无从覆盖。

        ★★ 1.9.3 第 26 轮（真机 bug，用户报"我在设置里改了 max_tokens 却不生效"）：
          **旧代码这里只调 `self._config.save()`，写的是内存里的旧值** ——
          用户在界面上改了、但没点「保存设置」的改动**全部丢失**，
          而日志还会打一句"config.json 已保存（退出）"，看起来一切正常。
          （README 一直承诺"退出时会把没点过保存的改动也写回本地"，
           但那只对**角色 settings.json** 成立，全局 config 其实没收集界面值。）
          现在：**先按 `apply_only=True` 把界面值收进 cfg，再同步落盘**。
          `apply_only` 只做"界面 → cfg"的赋值，不另起线程、不发信号 ——
          退出阶段发信号给正在关闭的窗口是危险的，所以这里刻意不触发。
        """
        self.flush_current_character_settings()
        try:
            self._do_save_settings(self._config, self._config.llm.provider,
                                   apply_only=True)
        except Exception as e:
            _log.warning(f"[落盘] 退出时收集界面值失败（改用内存里的值保存）: {e}")
        try:
            self._config.save()
            _log.info("[落盘] config.json 已保存（退出）")
        except Exception as e:
            _log.warning(f"[落盘] config.json 保存失败: {e}")

    def _load_character_settings_to_ui(self, name: str):
        """★ 1.5.0: 把当前角色的 settings.json 载入性格页 UI（每角色独立设置）"""
        try:
            import json
            from src.core import character_settings as _cs
            cdir = self._character_dir(name)
            if cdir is None:
                return
            sfile = cdir / "settings.json"
            if not sfile.exists():
                return
            # ★ 1.9.0: 新版三块结构；旧文件自动在内存里转换（不写盘）
            data, _was_old = _cs.load(sfile)
        except Exception as e:
            _log.debug(f"加载角色设置失败 {name}: {e}")
            return
        _p, _o, _b = data["性格"], data["输出控制"], data["行为控制"]
        self._pet_prompt_edit.setPlainText(_p.get("free_text", "") or "")
        # ★ 1.9.0: 性格页只剩一个文本框（自称/语气/特点/标记仍在文件里，不再显示）
        for attr, val in (("_pet_lang", _o.get("lang", "")),
                          ("_pet_output_extra", _o.get("extra", "")),
                          ("_pet_meme_tags", "、".join(_b.get("meme_tags_prefer", [])))):
            w = getattr(self, attr, None)
            if w is not None:
                w.setText(val or "")
        for attr, val in (("_pet_no_action", _o.get("no_action_description", True)),
                          ("_pet_no_markdown", _o.get("no_markdown", False)),
                          ("_pet_allow_code", _o.get("allow_code_block", True)),
                          ("_pet_allow_json", _o.get("allow_json", True)),
                          ("_pet_gif_reaction", _b.get("gif_reaction", True)),
                          ("_pet_proactive", _b.get("proactive_talk", True))):
            w = getattr(self, attr, None)
            if w is not None:
                w.setChecked(bool(val))
        if hasattr(self, "_pet_meme_tendency"):
            _mi = self._pet_meme_tendency.findData(_b.get("meme_tendency", "normal"))
            self._pet_meme_tendency.setCurrentIndex(_mi if _mi >= 0 else 2)

        sc = float(data.get("scale", 1.0) or 1.0)
        self._pet_scale.setValue(max(50, min(200, int(sc * 100))))
        idx = self._pet_reply_style.findData(_o.get("reply_style", "concise") or "concise")
        if idx >= 0:
            self._pet_reply_style.setCurrentIndex(idx)
        idx = self._pet_emoji_level.findData(_o.get("emoji_level", "light") or "light")
        if idx >= 0:
            self._pet_emoji_level.setCurrentIndex(idx)

        # ★ 1.5.0: 启动默认 GIF / 待机默认 GIF（多选）/ 切换规则
        self._refresh_gif_controls(name)
        sg = self._normalize_startup_gif(data["gif"].get("startup", "") or "")
        idx = self._pet_startup_gif.findData(sg)
        if idx < 0:
            idx = self._pet_startup_gif.findText(sg)
        self._pet_startup_gif.setCurrentIndex(idx if idx >= 0 else 0)
        idle = data["gif"].get("idle", []) or []
        for g, cb in self._pet_idle_checks.items():
            cb.setChecked(g in idle)
        rule = data["gif"].get("idle_switch_rule", {}) or {}
        mi = self._pet_idle_rule_mode.findData(rule.get("mode", "random"))
        self._pet_idle_rule_mode.setCurrentIndex(mi if mi >= 0 else 0)
        self._on_idle_rule_mode_changed(self._pet_idle_rule_mode.currentIndex())
        self._pet_idle_rule_value.setValue(int(rule.get("value", 5) or 5))

    def _character_dir(self, name: str):
        """★ 1.5.0: 按文件夹名或 settings.name 解析角色目录（容错显示名≠文件夹名）"""
        try:
            from src.core.pet_manager import CHARACTERS_DIR
            d = CHARACTERS_DIR / name
            if d.is_dir():
                return d
            if CHARACTERS_DIR.exists():
                for d in sorted(CHARACTERS_DIR.iterdir()):
                    if d.is_dir() and not d.name.startswith("."):
                        sf = d / "settings.json"
                        if sf.exists():
                            try:
                                nm = json.loads(sf.read_text(encoding="utf-8")).get("name", "")
                                if nm == name:
                                    return d
                            except Exception:
                                pass
        except Exception:
            pass
        return None

    def _refresh_gif_controls(self, name: str):
        """★ 1.5.0: 扫描当前角色 GIF 文件夹，填充「启动默认 GIF」下拉与「待机默认」多选"""
        try:
            from src.core.pet_manager import CharacterInfo
            cdir = self._character_dir(name)
            info = CharacterInfo(cdir) if cdir else None
            gifs = sorted(info.gifs.keys()) if info else []
        except Exception as e:
            _log.debug(f"扫描角色 GIF 失败 {name}: {e}")
            gifs = []
        # 启动默认 GIF 下拉
        self._pet_startup_gif.blockSignals(True)
        self._pet_startup_gif.clear()
        self._pet_startup_gif.addItem("(自动)", "")
        for g in gifs:
            self._pet_startup_gif.addItem(g, g)
        self._pet_startup_gif.blockSignals(False)
        # 待机默认 GIF 多选（清空后重建）
        self._pet_idle_checks.clear()
        while self._pet_idle_gifs_layout.count():
            it = self._pet_idle_gifs_layout.takeAt(0)
            w = it.widget()
            if w:
                w.deleteLater()
        if not gifs:
            self._pet_idle_gifs_layout.addWidget(self._hint("该角色暂无 GIF"))
        else:
            for g in gifs:
                cb = QCheckBox(g)
                cb.setStyleSheet(self.CHECKBOX_STYLE)
                self._pet_idle_checks[g] = cb
                self._pet_idle_gifs_layout.addWidget(cb)
            self._pet_idle_gifs_layout.addStretch()

    def _normalize_startup_gif(self, value: str) -> str:
        """★ 1.6.0: 规范化「启动默认 GIF」——占位文本 / 已不存在的名字一律视为「(自动)」"""
        v = (value or "").strip()
        if not v or v in ("(自动)", "自动", "auto"):
            return ""
        try:
            if self._pet_startup_gif.findData(v) < 0:
                return ""   # 该 GIF 已不存在（被删除/改名）→ 回落到自动
        except Exception:
            return ""
        return v

    def _on_idle_rule_mode_changed(self, idx: int):
        mode = self._pet_idle_rule_mode.currentData()
        self._pet_idle_rule_value.setSuffix(" 分钟" if mode == "fixed" else " 随机度")

    def _save_character_settings(self, name: str, persona: str, scale: float,
                                 reply_style: str, emoji_level: str,
                                 startup_gif: str = "", idle_gifs: list = None,
                                 idle_rule: dict = None):
        """★ 1.9.0: 把角色设置写回 data/桌宠形象/<name>/settings.json（**schema 2**）。

        写的是新版三块结构（性格 / 输出控制 / 行为控制），用 character_settings.save()
        统一落盘 —— 它负责补默认值、类型纠正、旧键清理。
        """
        try:
            from src.core import character_settings as _cs
            from src.core.pet_manager import CHARACTERS_DIR
            cdir = self._character_dir(name) or (CHARACTERS_DIR / name)
            sfile = cdir / "settings.json"
            data, _ = _cs.load(sfile) if sfile.exists() else (_cs.normalize({}), False)

            data["name"] = data.get("name") or name
            data["scale"] = float(scale)
            # 性格：★ 1.9.0 —— **只有一个文本框**，正文原样写进 free_text。
            #   不再把 identity 拿出来当复选框，也不再跑 normalize_persona
            #   （那会把正文里的整句抽走变成标记，导致你下次打开发现句子没了）。
            data["性格"]["free_text"] = persona or ""
            # 输出控制
            data["输出控制"].update({
                "reply_style": reply_style,
                "emoji_level": emoji_level,
                "no_action_description": bool(getattr(self, "_pet_no_action", None)
                                              and self._pet_no_action.isChecked()),
                "no_markdown": bool(getattr(self, "_pet_no_markdown", None)
                                    and self._pet_no_markdown.isChecked()),
                "allow_code_block": bool(getattr(self, "_pet_allow_code", None)
                                         and self._pet_allow_code.isChecked()),
                "allow_json": bool(getattr(self, "_pet_allow_json", None)
                                   and self._pet_allow_json.isChecked()),
                "lang": _w_txt(getattr(self, "_pet_lang", None)),
                "extra": _w_txt(getattr(self, "_pet_output_extra", None)),
            })
            # 行为控制
            data["行为控制"].update({
                "meme_tendency": (self._pet_meme_tendency.currentData()
                                  if hasattr(self, "_pet_meme_tendency") else "normal"),
                "meme_tags_prefer": _w_csv(getattr(self, "_pet_meme_tags", None)),
                "gif_reaction": bool(getattr(self, "_pet_gif_reaction", None)
                                     and self._pet_gif_reaction.isChecked()),
                "proactive_talk": bool(getattr(self, "_pet_proactive", None)
                                       and self._pet_proactive.isChecked()),
            })
            # gif 段
            data["gif"].update({
                "startup": startup_gif or "",
                "idle": list(idle_gifs or []),
                "idle_switch_rule": idle_rule or {"mode": "random", "value": 5},
            })
            data.pop("behavior_gifs", None)
            _cs.save(sfile, data)
            _log.debug(f"角色设置已写回（schema {_cs.SCHEMA}）: {name}/settings.json")
        except Exception as e:
            _log.warning(f"写回角色设置失败: {e}")

    # ── 小程序：控件取值（新控件可能还不存在，所以都带兜底）──
    # ★ 放在模块级而不是静态方法：测试里常拿桩对象调 _save_character_settings，
    #   桩没有 SettingsWindow 的类属性，写成 self._txt 会 AttributeError
    #   （真机不会，但测试会 —— 而且静默被 except 吞掉，看起来像"没写进去"）。

    # ════════════════════════════════════════════════════════════
    # TTS 语音页面
    # ════════════════════════════════════════════════════════════

    def _create_tts_page(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(10)

        g0 = self._section("语音合成 (TTS)")
        f0 = QFormLayout(g0)
        f0.setSpacing(10)

        row1, self._tts_enabled_toggle = self._toggle_row("启用 TTS 播报", checked=True)
        f0.addRow(row1)

        self._tts_mode = QComboBox()
        self._tts_mode.addItem("自动播报 (每条回复)", "auto")
        self._tts_mode.addItem("手动点击播报", "click")
        f0.addRow(self._lbl("播报模式:"), self._tts_mode)

        self._tts_provider = QComboBox()
        self._tts_provider.addItem("GENIE 本地 (GPT-SoVITS)", "genie")
        self._tts_provider.addItem("Edge-TTS 在线 (微软免费)", "edge")
        f0.addRow(self._lbl("引擎:"), self._tts_provider)

        self._tts_character = QComboBox()
        self._tts_character.addItems(["feibi (菲比·中文) ★本地模型", "晓晓 (温柔女声) Edge", "晓依 (活泼女声) Edge", "云希 (男声) Edge"])
        f0.addRow(self._lbl("语音角色:"), self._tts_character)

        f0.addRow("", self._hint("★ GENIE 本地 ONNX 推理（仅 feibi 有本地模型克隆）| 其他角色使用 Edge-TTS 在线免费语音"))
        layout.addWidget(g0)

        g1 = self._section("GENIE 高级设置")
        f1 = QFormLayout(g1)
        f1.setSpacing(8)
        self._tts_genie_data = QLineEdit()
        self._tts_genie_data.setPlaceholderText("留空自动检测 素材目录/GenieData")
        f1.addRow(self._lbl("GenieData 目录:"), self._tts_genie_data)
        self._tts_genie_char = QLineEdit()
        self._tts_genie_char.setPlaceholderText("留空自动检测")
        f1.addRow(self._lbl("角色模型目录:"), self._tts_genie_char)

        # ★ 1.6.0: 内存控制 —— GENIE 模型常驻约 4.5GB
        row_pre, self._tts_genie_preload = self._toggle_row("启动时预加载模型", checked=False)
        f1.addRow("", row_pre)
        self._tts_genie_idle = QSpinBox()
        self._tts_genie_idle.setRange(0, 240)
        self._tts_genie_idle.setSuffix(" 分钟")
        self._tts_genie_idle.setSpecialValueText("从不释放")
        f1.addRow(self._lbl("空闲释放:"), self._tts_genie_idle)
        f1.addRow("", self._hint(
            "GENIE 本地模型常驻内存约 4.5GB（它就是「内存爆满」的主要来源）。\n"
            "· 关闭「启动时预加载」→ 启动不再占这 4.5GB，第一次播报时再加载（稍慢）。\n"
            "· 「空闲释放」到点后自动回收内存，下次播报再加载；填 0 = 一直常驻。"))
        layout.addWidget(g1)

        layout.addStretch()
        return w

    # ════════════════════════════════════════════════════════════
    # 监控数据页面
    # ════════════════════════════════════════════════════════════

    def _create_monitor_page(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(10)

        g0 = self._section("屏幕截图监控")
        f0 = QFormLayout(g0)
        f0.setSpacing(8)
        row, self._scr_enabled = self._toggle_row("启用截图监控")
        f0.addRow(row)
        self._scr_interval = QSpinBox()
        self._scr_interval.setRange(1, 60)
        self._scr_interval.setValue(5)
        self._scr_interval.setSuffix(" 秒")
        f0.addRow(self._lbl("截图间隔:"), self._scr_interval)
        self._scr_quality = QSpinBox()
        self._scr_quality.setRange(10, 95)
        self._scr_quality.setValue(30)
        self._scr_quality.setSuffix(" % 质量")
        f0.addRow(self._lbl("JPEG 质量:"), self._scr_quality)
        f0.addRow("", self._hint("质量越低文件越小，识别效果差异不大"))
        layout.addWidget(g0)

        g1 = self._section("输入设备监控")
        f1 = QFormLayout(g1)
        f1.setSpacing(8)
        row1, self._kb_enabled = self._toggle_row("键盘输入监控")
        f1.addRow(row1)
        row2, self._ms_enabled = self._toggle_row("鼠标活动监控")
        f1.addRow(row2)
        self._ms_min_move = QSpinBox()
        self._ms_min_move.setRange(1, 100)
        self._ms_min_move.setValue(10)
        self._ms_min_move.setSuffix(" 像素 (最小移动)")
        f1.addRow(self._lbl("鼠标过滤:"), self._ms_min_move)
        row3, self._clipboard_enabled = self._toggle_row("剪贴板内容监控")
        f1.addRow(row3)
        f1.addRow("", self._hint("监控复制粘贴的文本内容"))
        layout.addWidget(g1)

        g2 = self._section("音频监控")
        f2 = QFormLayout(g2)
        f2.setSpacing(8)
        row, self._audio_enabled = self._toggle_row("麦克风音量监控")
        f2.addRow(row)
        self._audio_threshold = QSpinBox()
        self._audio_threshold.setRange(100, 2000)
        self._audio_threshold.setValue(500)
        self._audio_threshold.setSingleStep(50)
        self._audio_threshold.setSuffix(" 阈值")
        f2.addRow(self._lbl("音量阈值:"), self._audio_threshold)
        f2.addRow("", self._hint("高于阈值时记录，用于判断用户是否在说话"))
        layout.addWidget(g2)

        g3 = self._section("系统状态监控")
        f3 = QFormLayout(g3)
        f3.setSpacing(8)
        row, self._sys_enabled = self._toggle_row("系统资源监控 (CPU/内存/磁盘/窗口)")
        f3.addRow(row)
        self._sys_interval = QSpinBox()
        self._sys_interval.setRange(5, 300)
        self._sys_interval.setValue(10)
        self._sys_interval.setSuffix(" 秒")
        f3.addRow(self._lbl("采集间隔:"), self._sys_interval)
        layout.addWidget(g3)

        g4 = self._section("数据总结")
        f4 = QFormLayout(g4)
        f4.setSpacing(8)
        self._summarize_interval = QSpinBox()
        self._summarize_interval.setRange(5, 240)
        self._summarize_interval.setValue(60)
        self._summarize_interval.setSuffix(" 分钟")
        f4.addRow(self._lbl("LLM 总结间隔:"), self._summarize_interval)
        f4.addRow("", self._hint("每过此间隔，LLM 会自动总结监控数据生成记忆条目"))
        layout.addWidget(g4)
        layout.addStretch()
        return w

    # ════════════════════════════════════════════════════════════
    # 待机说话页面
    # ════════════════════════════════════════════════════════════

    # ══════════════════════════════════════════════════════════════
    # ★★ 1.9.4（用户 2026-10-18）：**账号开通**页 —— B站 / P站 登录一次即可
    #
    #   用户原话："参考项目 bili_pixiv_toolkit 可以做到开通p站和B站的功能。
    #             如果用户要开通这两个功能，就**引导用户登陆一次**就可以了。"
    #
    #   实现要点：
    #     · 真正的登录**会弹浏览器并阻塞**（Playwright + gppt），
    #       所以必须跑在**后台线程** —— 否则设置窗口会假死；
    #     · 线程里**绝不能碰 Qt 控件**（1.9.2 那次硬崩的教训）→ 用 Signal 回主线程；
    #     · 两个平台各自一个「一键开通」按钮 + 状态标签 + 结果说明。
    # ══════════════════════════════════════════════════════════════
    def _create_accounts_page(self) -> QWidget:
        from PySide6.QtCore import QObject, Signal

        # 跨线程桥：Worker 线程 emit → 主线程槽（Qt 会自动排队，安全）
        class _Bridge(QObject):
            line = Signal(str)          # 一行进度
            done = Signal(bool, str)    # 结束：成功?, 结果说明

        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(10)

        # ── 依赖状态（缺了就先把话说清楚）──
        g_dep = self._section("需要的依赖")
        f_dep = QFormLayout(g_dep)
        f_dep.setSpacing(8)
        # ★★ 1.9.4：判据是"**依赖在不在**"，不是"是不是 exe"。
        #   第一版 exe 把 playwright 排除了 → 这里置灰并说"exe 版不支持"；
        #   **真机反馈后重打的包**已自带 playwright + Edge 驱动 → exe 里照样能点。
        #   只有精简版 exe / 没装依赖时才降级，并**说清怎么补救**。
        _avail, _why, _miss = True, "", []
        try:
            from src.core import account_login as _al
            _miss = _al.missing_deps()
            _hint_cmd = _al.install_hint()
            _avail, _why = _al.feature_available()
        except Exception as _e:
            _miss, _hint_cmd = ["（读取失败）"], f"（{_e}）"
            _avail, _why = False, f"读取依赖状态失败: {_e}"
        if not _avail:
            _state_txt = "⚠ " + (_why.splitlines()[0] if _why else "不可用")
        else:
            _state_txt = ("✅ 都装好了" if not _miss else f"❌ 缺：{'、'.join(_miss)}")
        self._acc_dep_label = self._lbl(_state_txt)
        f_dep.addRow(self._lbl("状态:"), self._acc_dep_label)
        self._acc_dep_cmd = QLineEdit(_hint_cmd)
        self._acc_dep_cmd.setReadOnly(True)
        self._acc_dep_cmd.setPlaceholderText("exe 版没有可执行的安装命令")
        f_dep.addRow(self._lbl("安装命令:"), self._acc_dep_cmd)
        f_dep.addRow("", self._hint(
            (_why if not _avail else
             "这两个功能需要 playwright / requests / pixivpy3 / gppt。\n"
             "★ 一律装进**桌宠自己的 Python**（上面那条命令就是），"
             "别用系统里另一个 Python 装 —— 那样桌宠 import 不到。\n"
             "★ playwright 用的是**系统自带的 Edge**，所以**不需要**再跑 "
             "`playwright install`。\n"
             "★ **exe 版已经自带这些（含 Edge 驱动）**，可以直接点「一键开通」。")))
        layout.addWidget(g_dep)

        # ── 两个平台 ──
        for _key, _title, _desc in (
                ("bilibili", "B站",
                 "点一下会**弹出浏览器**，在里面登录一次 B站（登录成功后先看到自己的头像，"
                 "再关掉那个窗口）。cookie 会自动存好，以后不用再登。"),
                ("pixiv", "P站",
                 "点一下会**弹出浏览器**，在里面登录一次 Pixiv。"
                 "拿到的令牌会自动写进 .env，以后不用再登。\n"
                 "★ P站国内需要代理；如果你开了代理客户端，它能自己走。"),
        ):
            g = self._section(f"{_title}")
            f = QFormLayout(g)
            f.setSpacing(8)
            _st = QLabel("…")
            f.addRow(self._lbl("状态:"), _st)
            f.addRow("", self._hint(_desc))
            _h = QHBoxLayout()
            _b = QPushButton(f"一键开通（登录一次）")
            _btn_ref = QPushButton("刷新状态")
            # ★ 只有"依赖真的缺"才置灰（完整版 exe 自带 playwright → 可点）
            _b.setEnabled(_avail)
            if not _avail:
                _b.setText("一键开通不可用（看上面的说明）")
                _b.setToolTip("缺少浏览器驱动；见上方「需要的依赖」里的说明")
            _h.addWidget(_b)
            _h.addWidget(_btn_ref)
            _h.addStretch(1)
            f.addRow("", _h)
            _res = QLabel("")
            _res.setWordWrap(True)
            f.addRow("", _res)
            layout.addWidget(g)

            _bridge = _Bridge(self)
            _b.clicked.connect(
                lambda _c=False, k=_key, b=_b, r=_res, br=_bridge:
                self._start_account_login(k, b, r, br))
            _btn_ref.clicked.connect(
                lambda _c=False, s=_st, r=_res: self._refresh_account_status(s, r))
            # 首屏就把状态刷一次
            self._refresh_account_status(_st, _res)
            # 存起来，供 Worker 结果回填（避免闭包被回收）
            if not hasattr(self, "_acc_refs"):
                self._acc_refs = {}
            self._acc_refs[_key] = {"status": _st, "res": _res, "btn": _b,
                                    "bridge": _bridge}

        layout.addStretch()
        return w

    def _refresh_account_status(self, status_label=None, res_label=None):
        """刷新两个平台的开通状态（只读文件/环境变量，不起浏览器）。"""
        try:
            from src.core import account_login as _al
            s = _al.status()
        except Exception as e:
            s = {"bilibili": False, "pixiv": False}
            if res_label is not None:
                res_label.setText(f"读取失败: {e}")
        for k, ref in getattr(self, "_acc_refs", {}).items():
            ok = bool(s.get(k))
            ref["status"].setText("✅ 已开通" if ok else "❌ 未开通")
            ref["status"].setStyleSheet(
                "color: #15803d;" if ok else "color: #b91c1c;")

    def _start_account_login(self, key: str, btn, res_label, bridge):
        """★ 在**后台线程**里跑登录（会弹浏览器并阻塞），结果经 Signal 回主线程。"""
        import threading
        from PySide6.QtCore import Qt

        btn.setEnabled(False)
        btn.setText("正在登录…")
        res_label.setStyleSheet("color: #6b7280;")
        res_label.setText("正在打开浏览器，请在弹出的窗口里登录…")

        def _on_line(t):
            bridge.line.emit(str(t))

        def _on_done(ok, msg):
            bridge.done.emit(bool(ok), str(msg))

        bridge.line.connect(lambda t: res_label.setText(self._plain(t)))
        bridge.done.connect(
            lambda ok, msg: self._finish_account_login(key, ok, msg,
                                                       btn, res_label))

        def _work():
            try:
                from src.core import account_login as _al
                fn = (_al.login_bilibili if key == "bilibili"
                      else _al.login_pixiv)
                ok, msg = fn(on_line=_on_line)
            except Exception as e:
                ok, msg = False, f"出错: {type(e).__name__}: {e}"
            _on_done(ok, msg)

        threading.Thread(target=_work, daemon=True).start()

    def _finish_account_login(self, key, ok, msg, btn, res_label):
        btn.setEnabled(True)
        btn.setText("一键开通（登录一次）")
        res_label.setStyleSheet(
            "color: #15803d;" if ok else "color: #b91c1c;")
        res_label.setText(self._plain(msg))
        # 状态标签同步刷新
        self._refresh_account_status()

    def _create_download_path_page(self) -> QWidget:
        """★ 1.9.0（你的要求）：下载路径独立成一项，放最左栏最下面。

        原来它挤在「工具管理」页里；现在单独一页，和 LLM API 同级。
        （「待机说话」那页取消了 —— 它并进「工具设置 → idle_talk」。）
        """
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(10)

        g = self._section("下载目录")
        f = QFormLayout(g)
        f.setSpacing(8)
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(6)
        self._download_path = QLineEdit()
        self._download_path.setPlaceholderText("data/downloads（留空使用默认）")
        h.addWidget(self._download_path, 1)
        b_browse = QPushButton("浏览…")
        b_browse.clicked.connect(self._pick_download_dir)
        h.addWidget(b_browse)
        b_open = QPushButton("打开")
        b_open.clicked.connect(self._open_download_dir)
        h.addWidget(b_open)
        f.addRow(self._lbl("目录:"), row)
        f.addRow("", self._hint(
            "B站视频、JMComic、P站、联网搜图等下载文件的保存位置。\n"
            "可填绝对路径，也可填相对项目目录的路径；留空 = data/downloads。"))
        layout.addWidget(g)
        layout.addStretch()
        return w

    def _pick_download_dir(self):
        from PySide6.QtWidgets import QFileDialog
        d = QFileDialog.getExistingDirectory(self, "选择下载目录",
                                             str(_PROJECT_ROOT))
        if d:
            self._download_path.setText(d)

    def _open_download_dir(self):
        import os as _os
        p = (self._download_path.text() or "").strip() or str(_PROJECT_ROOT / "data" / "downloads")
        path = Path(p) if Path(p).is_absolute() else (_PROJECT_ROOT / p)
        try:
            path.mkdir(parents=True, exist_ok=True)
            _os.startfile(str(path))
        except Exception as e:
            _log.debug(f"打开下载目录失败: {e}")
    def _create_tools_page(self) -> QWidget:
        """★ 第四轮（HTML v4 单层窗口）：上下两个子窗口都改成
        「纯文字标题 + 一层滚动区」，去掉 QGroupBox 标题条再嵌滚动框的双层观感。

        你原话："工具列表 + 工具详情'两层窗口'看着冗余，要减一层。参考 LLM API，
        标题窗口里面就是内容。如果做不到，就不要标题窗口，只有标题，然后是
        内容滑动窗口。"

        布局：
          上半：标题（纯文字） + 工具列表滚动区 —— 一工具一排：
                加载策略下拉（关闭/按需/常驻）→ 工具名 → 设置 → 介绍
          下半：标题（纯文字） + 详情滚动区 —— 点「设置」= 该工具的设置页；
                点「介绍」= 说明；刚进来默认显示 data/assets/t&PRTS.jpg
        下载目录**不在这一页**了 —— 按你说的挪到最左栏最下面「下载路径」。
        """
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(8)         # 没 QGroupBox 外框了，标题和滚动区之间 8px 呼吸

        # ── 上半：纯文字标题 + 单层滚动区 ──
        ttl1 = QLabel("工具列表 <span style='color:#999; font-weight:400'>"
                      "（加载策略：关闭 = LLM 看不到也调不到 / 按需 / 常驻）</span>")
        ttl1.setTextFormat(Qt.RichText)
        ttl1.setStyleSheet("font-size:12.5px; color:#666; font-weight:600; padding:0 2px;"
                           " background:transparent;")
        layout.addWidget(ttl1)

        self._tools_scroll = QScrollArea()
        self._tools_scroll.setWidgetResizable(True)
        self._tools_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._tools_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        # ★ 对照 HTML .flatbox：border-radius 10px，没第二层
        self._tools_scroll.setStyleSheet(
            "QScrollArea { background:#fff; border:1px solid #e2e2e6; border-radius:10px; }")
        self._tools_box = QWidget()
        self._tools_layout = QVBoxLayout(self._tools_box)
        self._tools_layout.setContentsMargins(6, 6, 6, 6)
        self._tools_layout.setSpacing(4)
        self._tools_scroll.setWidget(self._tools_box)
        # 上下两个子窗口 3:2 占满内容区，免得主窗口又冒出一条
        self._tools_scroll.setMinimumHeight(120)
        layout.addWidget(self._tools_scroll, 3)

        # ── 下半：同样只有标题 + 一层内容滚动区 ──
        ttl2 = QLabel("工具详情 <span style='color:#999; font-weight:400'>"
                      "（点「设置」或「介绍」切换；刚进来显示默认图）</span>")
        ttl2.setTextFormat(Qt.RichText)
        ttl2.setStyleSheet("font-size:12.5px; color:#666; font-weight:600; padding:0 2px;"
                           " background:transparent;")
        layout.addWidget(ttl2)

        self._tool_detail_scroll = QScrollArea()
        self._tool_detail_scroll.setWidgetResizable(True)
        self._tool_detail_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._tool_detail_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._tool_detail_scroll.setStyleSheet(
            "QScrollArea { background:#fff; border:1px solid #e2e2e6; border-radius:10px; }")
        self._tool_detail_box = QWidget()
        self._tool_detail_layout = QVBoxLayout(self._tool_detail_box)
        self._tool_detail_layout.setContentsMargins(12, 12, 12, 12)
        self._tool_detail_layout.setSpacing(8)
        self._tool_detail_scroll.setWidget(self._tool_detail_box)
        self._tool_detail_scroll.setMinimumHeight(110)
        layout.addWidget(self._tool_detail_scroll, 2)

        # 控件表（策略下拉；旧的"开启复选框"被"关闭"档取代了）
        self._tools_policy_combos: dict[str, QComboBox] = {}
        self._tools_enable_checks: dict[str, QCheckBox] = {}   # 兼容老代码：由策略派生
        self._action_checks: dict[str, dict] = {}              # 工具 → {action: QCheckBox}
        # 主动说话的控件（原来在「待机说话」页；现在挂在 idle_talk 的设置里，
        # 但仍要**先建出来**，因为保存/加载要读它们）
        self._build_idle_widgets()
        self._show_tool_default()
        self._refresh_tools_list()
        return w

    def _build_idle_widgets(self):
        """主动说话的控件（由「工具设置 → idle_talk → 设置」显示）"""
        holder = QWidget()
        f = QFormLayout(holder)
        f.setSpacing(8)
        row, self._idle_enabled = self._toggle_row("启用主动说话", checked=True)
        f.addRow(row)
        self._idle_interval = QSpinBox()
        self._idle_interval.setRange(2, 120)
        self._idle_interval.setValue(10)
        self._idle_interval.setSuffix(" 分钟")
        f.addRow(self._lbl("触发时间:"), self._idle_interval)
        self._idle_prompt = QTextEdit()
        self._idle_prompt.setMaximumHeight(70)
        self._idle_prompt.setPlaceholderText("用户长时间没有理你了，你可以主动一次，"
                                            "可以调用工具看看用户再干嘛")
        f.addRow(self._lbl("提示词:"), self._idle_prompt)
        f.addRow("", self._hint(
            "它是**自动工具**：到点由系统触发，LLM 不会主动调用它。\n"
            "在上下文里以「自动工具触发」的形式出现，和普通工具调用同样式。"))
        self._idle_widgets = holder
        holder.setParent(None)      # 先不挂在任何页面上，等点「设置」再塞进详情区

    # ── 详情子窗口：默认图 / 介绍 / 设置 ──────────────────────────

    def _clear_tool_detail(self):
        while self._tool_detail_layout.count():
            it = self._tool_detail_layout.takeAt(0)
            w = it.widget()
            if w:
                w.setParent(None)

    def _show_tool_default(self):
        """刚进「工具设置」时，详情区显示那张图"""
        self._clear_tool_detail()
        img = _PROJECT_ROOT / "data" / "assets" / "t&PRTS.jpg"
        lbl = QLabel()
        lbl.setAlignment(Qt.AlignCenter)
        lbl.setStyleSheet("background:transparent;")
        if img.exists():
            pm = QPixmap(str(img))
            if not pm.isNull():
                lbl.setPixmap(pm.scaled(560, 240, Qt.KeepAspectRatio,
                                        Qt.SmoothTransformation))
        else:
            lbl.setText("（默认图缺失：data/assets/t&PRTS.jpg）")
        self._tool_detail_layout.addWidget(lbl)
        tip = QLabel("选一个工具，点「设置」看它的可配项，点「介绍」看它是干什么的。")
        tip.setAlignment(Qt.AlignCenter)
        tip.setStyleSheet("color:#888; font-size:12px; background:transparent;")
        self._tool_detail_layout.addWidget(tip)
        self._tool_detail_layout.addStretch()

    @staticmethod
    def _format_intro(text: str, width: int = 32) -> str:
        """★ 1.9.0（你的要求）：把一坨说明排成**短行分点**，别糊成一大行。

        你原话："介绍内容，尽量提行，分点介绍，单行太长不好看，纯文字排版问题。"
        做法：先按句号/分号/换行切句，过长的句子再在逗号/顿号处软切，
        最后每行前面加「· 」。返回可直接给 QLabel 的富文本。
        """
        import re
        t = (text or "").strip()
        if not t:
            return "（没有说明）"
        # 工具描述里常见的 "支持: a、b、c" 也按顿号切开，免得一行拖很长
        t = re.sub(r"支持[:：]\s*", "支持：\n", t)
        out = []
        for para in [p.strip() for p in t.split("\n") if p.strip()]:
            for sent in [s.strip() for s in re.split(r"(?<=[。；;！!？?])", para) if s.strip()]:
                s = sent
                while len(s) > width:
                    cut = max(s.rfind("，", 0, width), s.rfind("、", 0, width),
                              s.rfind(",", 0, width), s.rfind(" ", 0, width))
                    if cut < 8:
                        cut = width - 1
                    out.append(s[:cut + 1].strip())
                    s = s[cut + 1:].strip()
                if s:
                    out.append(s)
        if not out:
            return t
        # 以「：」结尾的行跟下一行并起来（"支持：" 单独一行太傻）
        merged = []
        for ln in out:
            if merged and merged[-1].endswith(("：", ":")):
                merged[-1] = merged[-1] + ln
            else:
                merged.append(ln)
        return "<br>".join("· " + ln for ln in merged)

    def _show_tool_intro(self, name: str):
        """点「介绍」→ 详情区显示说明（内容与 tools功能.txt 同源）"""
        self._clear_tool_detail()
        tool = self._tool_manager.get_tool(name) if self._tool_manager else None
        title = QLabel(f"<b>{name}</b>")
        title.setStyleSheet("font-size:14px; background:transparent;")
        self._tool_detail_layout.addWidget(title)
        # ★ 1.9.0: 说明分点提行（详情区无水平滚动条，文字一律折行）
        body = QLabel(self._format_intro((tool.description if tool else "") or ""))
        body.setWordWrap(True)
        body.setTextFormat(Qt.RichText)
        body.setTextInteractionFlags(Qt.TextSelectableByMouse)
        body.setStyleSheet("color:#333; font-size:12.5px; background:transparent;")
        self._tool_detail_layout.addWidget(body)
        _acts = self._tool_actions(name)
        pol = QLabel(f"当前加载策略：{getattr(tool, 'load_policy', None).value if tool else '-'}"
                     f"　|　可禁用的 action 数：{len(_acts)}")
        pol.setStyleSheet("color:#888; font-size:11.5px; background:transparent;")
        self._tool_detail_layout.addWidget(pol)
        self._tool_detail_layout.addStretch()

    def _show_tool_settings(self, name: str):
        """点「设置」→ 详情区变成该工具的设置页。

        · idle_talk：触发时间 + 提示词（你指定的两项）
        · 其它工具：把它的 action 列成「是否禁用 / action 名字 / 简介」一排排
        """
        self._clear_tool_detail()
        title = QLabel(f"<b>{name}</b> — 设置")
        title.setStyleSheet("font-size:14px; background:transparent;")
        self._tool_detail_layout.addWidget(title)

        if name == "idle_talk":
            w = getattr(self, "_idle_widgets", None)
            if w is not None:
                w.setParent(self._tool_detail_box)
                self._tool_detail_layout.addWidget(w)
            self._tool_detail_layout.addStretch()
            return

        acts = self._tool_actions(name)
        if not acts:
            # ★ 1.9.0: 有些工具本来就**没有 action 参数**（shell_exec 用 command、
            #   weather_query 用 city…），把它们真实的参数名写出来，
            #   免得看起来像"又出 bug 了、动作全是 0 个"。
            try:
                _tool = self._tool_manager.get_tool(name) if self._tool_manager else None
                _props = ((getattr(_tool, "parameters", None) or {}).get("properties") or {})
                _names = "、".join(list(_props.keys())[:8]) or "（无参数）"
            except Exception:
                _names = "（读不到）"
            self._tool_detail_layout.addWidget(self._hint(
                "这个工具没有 action 参数，所以没有可禁用的动作。\n"
                f"它的参数是：{_names}"))
            self._tool_detail_layout.addStretch()
            return
        head = QLabel("是否禁用　|　action 名字　|　简介"
                      "　　　（勾上**立刻生效**，不用再点保存）")
        head.setStyleSheet("color:#666; font-size:11.5px; font-weight:bold; background:transparent;")
        self._tool_detail_layout.addWidget(head)
        self._action_checks.setdefault(name, {})
        disabled = set(self._disabled_actions().get(name, []))
        for a in acts:
            row = QWidget()
            h = QHBoxLayout(row)
            h.setContentsMargins(0, 2, 0, 2)
            # ★ 1.9.0（你的批注）：action 和简介的间距太宽、内容被窗口挡住 → 全线收紧
            h.setSpacing(4)
            cb = QCheckBox("禁用")
            cb.setStyleSheet(self.CHECKBOX_STYLE)
            cb.setChecked(a["name"] in disabled)
            cb.setFixedWidth(48)
            # ★ 1.9.0（你的实测反馈）：勾了**立刻写盘生效**。
            #   你上次"并没有被挡"的根因就是：勾完还得跑到页面底部点「保存设置」，
            #   不点的话 config 里 `disabled_actions` 还是空的（我查过确实是 {}），
            #   ToolManager 自然拦不住。现在勾一下就直接落到配置里。
            cb.toggled.connect(
                lambda _c=False, n=name: self._apply_disabled_actions_now(n))
            self._action_checks[name][a["name"]] = cb
            h.addWidget(cb)
            nm = QLabel(a["name"])
            nm.setFixedWidth(108)
            nm.setStyleSheet("font-family:Consolas,monospace; color:#1a3d6d;"
                             "background:transparent;")
            h.addWidget(nm)
            ds = QLabel(a.get("desc", "") or "")
            ds.setWordWrap(True)
            ds.setStyleSheet("color:#777; font-size:11.5px; background:transparent;")
            h.addWidget(ds, 1)
            self._tool_detail_layout.addWidget(row)
        self._tool_detail_layout.addStretch()

    def _disabled_actions(self) -> dict:
        try:
            return dict(getattr(self._config.tools, "disabled_actions", {}) or {})
        except Exception:
            return {}

    def _tool_actions(self, name: str) -> list:
        """从工具的参数 schema 里抽出 action 枚举（没有就返回空）。

        ★ 1.9.0 修 bug（你发现的「目前所有动作都为 0 个」）：
          以前这里硬按 `tools.{name}_tool` 猜模块名 —— 但工具文件名并不统一
          （bilibili_tool / file_ops / knowledge_graph / shell_exec /
           system_tool / weather_tool …），14 个里有 6 个猜不中，
          猜不中就静默返回空 → 那些工具的 action 列表全是 0 个，
          连带"禁用某个 action"也没法试。
          现在直接用 ToolManager 扫出来的 `parameters`，不再猜文件名。
        """
        params = None
        try:
            tool = self._tool_manager.get_tool(name) if self._tool_manager else None
            if tool is not None:
                params = getattr(tool, "parameters", None)
        except Exception as e:
            _log.debug(f"取工具 {name} 的 schema 失败: {e}")
        if not isinstance(params, dict):
            # 兜底：ToolManager 不可用时，退回按文件名猜（老逻辑）
            try:
                import importlib
                mod = importlib.import_module(f"tools.{name}_tool")
                for attr in ("get_parameters", "PARAMETERS", "TOOL_PARAMETERS"):
                    v = getattr(mod, attr, None)
                    if callable(v):
                        params = v()
                        break
                    if isinstance(v, dict):
                        params = v
                        break
            except Exception:
                params = None
        try:
            props = (params or {}).get("properties", {}) or {}
            act = props.get("action") or {}
            enums = act.get("enum") or []
            text = act.get("description") or ""
            out = []
            for e in enums:
                desc = ""
                for line in str(text).splitlines():
                    if str(e) in line:
                        desc = line.strip()
                        break
                if not desc:
                    desc = str(text)[:60]
                out.append({"name": str(e), "desc": desc})
            return out
        except Exception:
            return []

    def _refresh_tools_list(self):
        """★ 1.9.0: 一工具一排 —— 加载策略（关闭/按需/常驻）→ 工具名 → 设置 → 介绍。

        按你的意见：**加载策略下拉保留**（"关闭"就等于以前的"不启用"）；
        介绍不再堆在表格里，改成点「介绍」在下面那个子窗口看。
        主动说话（idle_talk）也作为**自动工具**排在这里。
        """
        self._tools_enable_checks.clear()
        self._tools_policy_combos.clear()
        while self._tools_layout.count():
            it = self._tools_layout.takeAt(0)
            w = it.widget()
            if w:
                w.deleteLater()
        old_grid = getattr(self, "_tools_grid", None)
        if old_grid is not None:
            while old_grid.count():
                it = old_grid.takeAt(0)
                w = it.widget()
                if w:
                    w.deleteLater()
            self._tools_layout.removeItem(old_grid)

        grid = QGridLayout()
        grid.setContentsMargins(4, 4, 4, 4)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(6)
        grid.setColumnStretch(1, 1)

        for c, head in enumerate(("加载策略", "工具名字", "设置", "介绍")):
            h = QLabel(head)
            h.setStyleSheet("color:#666; font-size:11px; font-weight:bold; background:transparent;")
            grid.addWidget(h, 0, c)

        rows = [(t.name, t.load_policy.value, t.description)
                for t in self._tool_manager.get_all_tools()]
        # 主动说话：自动工具（LLM 不调用），也排在列表里
        rows.append(("idle_talk", "auto", "主动说话（自动工具，LLM 不会主动调用）"))

        _POL = ["关闭", "按需", "常驻"]
        _REV = {"disabled": 0, "lazy": 1, "resident": 2, "auto": 1}
        for r, (name, policy, desc) in enumerate(rows, start=1):
            combo = QComboBox()
            combo.addItem("关闭", "disabled")
            combo.addItem("按需", "lazy")
            combo.addItem("常驻", "resident")
            combo.setCurrentIndex(_REV.get(policy, 1))
            combo.setFixedWidth(54)          # ★ 1.9.0（你的批注）：就两个字，54 装得下还紧凑
            combo.setToolTip("关闭=LLM 看不到也调不到 | 按需=首次调用才加载，空闲自动卸载 | 常驻=一直留着")
            combo.setStyleSheet(
                "QComboBox { font-size: 12px; padding: 2px 3px; background: #fff;"
                " border: 1px solid #d0d0d5; border-radius: 4px; }")
            self._tools_policy_combos[name] = combo
            grid.addWidget(combo, r, 0, Qt.AlignLeft | Qt.AlignVCenter)

            nm = QLabel(f"<b>{name}</b>" + ("　<small style='color:#999'>自动工具</small>"
                                            if name == "idle_talk" else ""))
            nm.setStyleSheet("background:transparent;")
            # ★ 第四轮（你实测"悬停工具名显示黑框"）：删 setToolTip。
            #   desc 看名字就能猜到大概，不需要黑框弹窗；要看介绍点「介绍」按钮。
            grid.addWidget(nm, r, 1, Qt.AlignLeft | Qt.AlignVCenter)

            b_set = QPushButton("设置")
            b_set.setFixedWidth(56)
            b_set.clicked.connect(lambda _c=False, n=name: self._show_tool_settings(n))
            grid.addWidget(b_set, r, 2, Qt.AlignLeft | Qt.AlignVCenter)

            b_intro = QPushButton("介绍")
            b_intro.setFixedWidth(56)
            b_intro.clicked.connect(lambda _c=False, n=name: self._show_tool_intro(n))
            grid.addWidget(b_intro, r, 3, Qt.AlignLeft | Qt.AlignVCenter)

        self._tools_grid = grid
        self._tools_layout.addLayout(grid)
        self._tools_layout.addStretch()
    def _get_tools_policy_changes(self) -> dict[str, str]:
        """★ 1.9.0: 直接读「加载策略」下拉（关闭/按需/常驻）。

        idle_talk 是自动工具、不是真工具，跳过（它由 cfg.idle.enabled 控制）。
        """
        changes = {}
        for name, combo in self._tools_policy_combos.items():
            if name == "idle_talk":
                continue
            changes[name] = combo.currentData() or "lazy"
        return changes

    def _collect_disabled_actions(self) -> dict:
        """★ 1.9.0: 收集"哪些 action 被禁用了"（详情子窗口里的复选框）"""
        out = {}
        for tool, checks in getattr(self, "_action_checks", {}).items():
            off = [a for a, cb in checks.items() if cb.isChecked()]
            if off:
                out[tool] = off
        return out

    def _apply_disabled_actions_now(self, name: str):
        """★ 1.9.0（你的实测反馈）：勾选/取消「禁用」→ **立刻**写进配置并落盘。

        为什么不是等「保存设置」：这个勾选框在下面的详情子窗口里，离页面底部的
        保存按钮很远，很容易以为已经生效了。而 ToolManager 是每次调用都现读
        `get_config().tools.disabled_actions`，所以只要配置更新了，**不用重启**
        下一次调用就会被拦。
        """
        try:
            cfg = self._config
            cfg.tools.disabled_actions = self._collect_disabled_actions()
            cfg.save()          # 同进程内立刻生效；落盘是为了重启后还在
            off = cfg.tools.disabled_actions.get(name) or []
            _log.info(f"[工具设置] {name} 禁用的 action: {off or '（无）'}")
        except Exception as e:
            _log.warning(f"写 disabled_actions 失败: {e}")

    # ════════════════════════════════════════════════════════════
    # ★ 个性化页面
    # ════════════════════════════════════════════════════════════

    def _create_personalize_page(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(10)

        g0 = self._section("聊天窗口尺寸")
        f0 = QFormLayout(g0)
        f0.setSpacing(8)

        self._ui_chat_width = QSpinBox()
        self._ui_chat_width.setRange(280, 800)
        self._ui_chat_width.setValue(420)
        self._ui_chat_width.setSuffix(" px")
        f0.addRow(self._lbl("窗口宽度:"), self._ui_chat_width)

        self._ui_chat_height = QSpinBox()
        self._ui_chat_height.setRange(320, 900)
        self._ui_chat_height.setValue(640)
        self._ui_chat_height.setSuffix(" px")
        f0.addRow(self._lbl("窗口高度:"), self._ui_chat_height)

        self._ui_chat_distance = QSpinBox()
        self._ui_chat_distance.setRange(0, 500)
        self._ui_chat_distance.setValue(10)
        self._ui_chat_distance.setSuffix(" px")
        f0.addRow(self._lbl("距形象距离:"), self._ui_chat_distance)

        # ★ 1.9.1: 「侧栏宽度」这一项删掉了 —— 聊天窗口的左侧栏整个移除
        #   （用户方案 2），宽度设置已经没有作用对象，留着只会误导。

        f0.addRow("", self._hint("聊天窗口相对于桌宠形象的间距"))
        layout.addWidget(g0)

        g1 = self._section("颜色主题")
        f1 = QFormLayout(g1)
        f1.setSpacing(8)

        self._ui_bg_color_btn = QPushButton()
        self._ui_bg_color_val = "#ffffff"
        self._ui_bg_color_btn.setFixedSize(32, 24)
        self._ui_bg_color_btn.setStyleSheet(f"background: {self._ui_bg_color_val}; border: 1px solid #ccc; border-radius: 4px;")
        self._ui_bg_color_btn.clicked.connect(lambda: self._pick_color("ui_bg"))
        bg_row = QHBoxLayout()
        bg_row.addWidget(self._lbl("大底色:"))
        bg_row.addWidget(self._ui_bg_color_btn)
        bg_row.addStretch()
        f1.addRow(bg_row)

        self._ui_chat_bg_color_btn = QPushButton()
        self._ui_chat_bg_color_val = "#f7f7fa"
        self._ui_chat_bg_color_btn.setFixedSize(32, 24)
        self._ui_chat_bg_color_btn.setStyleSheet(f"background: {self._ui_chat_bg_color_val}; border: 1px solid #ccc; border-radius: 4px;")
        self._ui_chat_bg_color_btn.clicked.connect(lambda: self._pick_color("chat_bg"))
        chat_row = QHBoxLayout()
        chat_row.addWidget(self._lbl("聊天区底色:"))
        chat_row.addWidget(self._ui_chat_bg_color_btn)
        chat_row.addStretch()
        f1.addRow(chat_row)

        self._ui_user_bubble_btn = QPushButton()
        self._ui_user_bubble_val = "transparent"
        self._ui_user_bubble_btn.setFixedSize(32, 24)
        self._ui_user_bubble_btn.setStyleSheet(f"background: {self._ui_user_bubble_val}; border: 1px solid #ccc; border-radius: 4px;")
        self._ui_user_bubble_btn.clicked.connect(lambda: self._pick_color("user_bubble"))
        ub_row = QHBoxLayout()
        ub_row.addWidget(self._lbl("用户气泡底色:"))
        ub_row.addWidget(self._ui_user_bubble_btn)
        ub_row.addStretch()
        f1.addRow(ub_row)

        self._ui_user_text_btn = QPushButton()
        self._ui_user_text_val = "#222222"
        self._ui_user_text_btn.setFixedSize(32, 24)
        self._ui_user_text_btn.setStyleSheet(f"background: {self._ui_user_text_val}; border: 1px solid #ccc; border-radius: 4px;")
        self._ui_user_text_btn.clicked.connect(lambda: self._pick_color("user_text"))
        ut_row = QHBoxLayout()
        ut_row.addWidget(self._lbl("用户字体颜色:"))
        ut_row.addWidget(self._ui_user_text_btn)
        ut_row.addStretch()
        f1.addRow(ut_row)

        self._ui_bot_bubble_btn = QPushButton()
        self._ui_bot_bubble_val = "transparent"
        self._ui_bot_bubble_btn.setFixedSize(32, 24)
        self._ui_bot_bubble_btn.setStyleSheet(f"background: {self._ui_bot_bubble_val}; border: 1px solid #ccc; border-radius: 4px;")
        self._ui_bot_bubble_btn.clicked.connect(lambda: self._pick_color("bot_bubble"))
        bb_row = QHBoxLayout()
        bb_row.addWidget(self._lbl("Bot 气泡底色:"))
        bb_row.addWidget(self._ui_bot_bubble_btn)
        bb_row.addStretch()
        f1.addRow(bb_row)

        self._ui_bot_text_btn = QPushButton()
        self._ui_bot_text_val = "#222222"
        self._ui_bot_text_btn.setFixedSize(32, 24)
        self._ui_bot_text_btn.setStyleSheet(f"background: {self._ui_bot_text_val}; border: 1px solid #ccc; border-radius: 4px;")
        self._ui_bot_text_btn.clicked.connect(lambda: self._pick_color("bot_text"))
        bt_row = QHBoxLayout()
        bt_row.addWidget(self._lbl("Bot 字体颜色:"))
        bt_row.addWidget(self._ui_bot_text_btn)
        bt_row.addStretch()
        f1.addRow(bt_row)

        f1.addRow("", self._hint("点击色块弹出取色器选择颜色。聊天文本默认无底色（透明）"))
        layout.addWidget(g1)

        layout.addWidget(self._create_avatar_section())

        g2 = self._section("透明度")
        f2 = QFormLayout(g2)
        f2.setSpacing(8)

        self._ui_opacity_label = QLabel("0.92")
        self._ui_opacity_label.setStyleSheet("color: #333; font-size: 12px; background: transparent; min-width: 36px;")
        self._ui_opacity = QSlider(Qt.Horizontal)
        self._ui_opacity.setRange(30, 100)
        self._ui_opacity.setValue(92)
        self._ui_opacity.valueChanged.connect(lambda v: self._ui_opacity_label.setText(f"{v/100:.2f}"))
        op_row = QHBoxLayout()
        op_row.addWidget(self._ui_opacity)
        op_row.addWidget(self._ui_opacity_label)
        f2.addRow(self._lbl("整体透明度:"), op_row)

        f2.addRow("", self._hint("数值越小越透明。0.30=最透明, 1.00=不透明"))
        layout.addWidget(g2)

        layout.addStretch()
        return w

    # ── 聊天头像 ──

    def _create_avatar_section(self) -> QGroupBox:
        g = self._section("聊天头像")
        f = QFormLayout(g)
        f.setSpacing(8)

        self._pet_avatar_path = ""
        self._user_avatar_path = ""

        self._pet_avatar_preview = QLabel()
        self._user_avatar_preview = QLabel()
        for lb in (self._pet_avatar_preview, self._user_avatar_preview):
            lb.setFixedSize(56, 56)
            lb.setAlignment(Qt.AlignCenter)
            lb.setStyleSheet("background: #f5f5f7; border: 1px solid #e0e0e0; border-radius: 28px;")

        pet_up, pet_reset = self._avatar_buttons(True)
        user_up, user_reset = self._avatar_buttons(False)

        pet_row = QHBoxLayout()
        pet_row.addWidget(self._pet_avatar_preview)
        pet_row.addWidget(pet_up)
        pet_row.addWidget(pet_reset)
        pet_row.addStretch()
        f.addRow(self._lbl("桌宠头像:"), pet_row)

        user_row = QHBoxLayout()
        user_row.addWidget(self._user_avatar_preview)
        user_row.addWidget(user_up)
        user_row.addWidget(user_reset)
        user_row.addStretch()
        f.addRow(self._lbl("用户头像:"), user_row)

        f.addRow("", self._hint(
            "支持 png/jpg/bmp/webp，自动居中裁剪为正方形并压缩到 0.5MB 以内。\n"
            "「重置」后使用自带形象。上传后需点击「保存设置」生效。"))
        return g

    def _avatar_buttons(self, is_pet: bool) -> tuple:
        """返回 (上传按钮, 重置按钮)"""
        style = """
            QPushButton { background: #e0e0e5; color: #333; border: 1px solid #d0d0d5;
                          border-radius: 6px; padding: 4px 14px; font-size: 12px; }
            QPushButton:hover { background: #007aff; color: #fff; border-color: #007aff; }
        """
        up = QPushButton("上传")
        up.setStyleSheet(style)
        up.clicked.connect(lambda: self._upload_avatar(is_pet))
        reset = QPushButton("重置")
        reset.setStyleSheet(style)
        reset.clicked.connect(lambda: self._reset_avatar(is_pet))
        return up, reset

    @staticmethod
    def _avatar_qpixmap(path: str, size: int) -> QPixmap:
        """加载头像为圆形 QPixmap；path 为空或失败时回退到自带形象"""
        from PIL import Image, ImageDraw, ImageOps
        base_dir = Path(__file__).resolve().parent.parent.parent
        default = base_dir / "data" / "桌宠形象" / "铃兰" / "铃兰头像.jpg"

        candidates = []
        if path:
            candidates.append(Path(path))
        candidates.append(default)

        for c in candidates:
            try:
                if not (c and Path(c).exists()):
                    continue
                img = Image.open(c).convert("RGBA")
                img = ImageOps.fit(img, (size, size), Image.LANCZOS, centering=(0.5, 0.5))
                mask = Image.new("L", (size, size), 0)
                ImageDraw.Draw(mask).ellipse((0, 0, size, size), fill=255)
                img.putalpha(mask)
                buf = io.BytesIO()
                img.save(buf, format="PNG")
                return QPixmap.fromImage(QImage.fromData(buf.getvalue(), "PNG"))
            except Exception as e:
                _log.debug(f"头像预览加载失败 {c}: {e}")
        return QPixmap(size, size)

    def _set_avatar_preview(self, label: QLabel, path: str, size: int = 56):
        label.setPixmap(self._avatar_qpixmap(path, size))

    def _upload_avatar(self, is_pet: bool):
        file_path, _ = QFileDialog.getOpenFileName(
            self, "选择头像图片", "",
            "图片 (*.png *.jpg *.jpeg *.bmp *.webp *.gif)")
        if not file_path:
            return
        out = self._process_and_store_avatar(file_path, is_pet)
        if not out:
            return
        if is_pet:
            self._pet_avatar_path = out
            self._set_avatar_preview(self._pet_avatar_preview, out)
            _log.info(f"桌宠头像已更新: {out}")
        else:
            self._user_avatar_path = out
            self._set_avatar_preview(self._user_avatar_preview, out)
            _log.info(f"用户头像已更新: {out}")
        self._save_status.setText("头像已更新，点击「保存设置」生效")
        self._save_status.setStyleSheet("color: #007aff; font-size: 11px; background: transparent;")

    def _reset_avatar(self, is_pet: bool):
        if is_pet:
            self._pet_avatar_path = ""
            self._set_avatar_preview(self._pet_avatar_preview, "")
        else:
            self._user_avatar_path = ""
            self._set_avatar_preview(self._user_avatar_preview, "")
        self._save_status.setText("已重置为自带形象，点击「保存设置」生效")
        self._save_status.setStyleSheet("color: #007aff; font-size: 11px; background: transparent;")

    @staticmethod
    def _process_and_store_avatar(src_path: str, is_pet: bool) -> str:
        """居中裁剪为正方形 → 缩放 256×256 → 压缩到 0.5MB 以内 → 保存"""
        try:
            from PIL import Image, ImageOps
            out_dir = Path(__file__).resolve().parent.parent.parent / "data" / "avatars"
            out_dir.mkdir(parents=True, exist_ok=True)
            out_path = out_dir / ("pet_avatar.jpg" if is_pet else "user_avatar.jpg")

            img = Image.open(src_path).convert("RGBA")
            img = ImageOps.fit(img, (256, 256), Image.LANCZOS, centering=(0.5, 0.5))
            bg = Image.new("RGB", img.size, (255, 255, 255))
            bg.paste(img, mask=img.split()[-1])

            limit = int(0.5 * 1024 * 1024)  # 0.5MB
            quality = 90
            while True:
                bg.save(out_path, format="JPEG", quality=quality, optimize=True)
                if out_path.stat().st_size <= limit or quality <= 35:
                    break
                quality -= 10
            _log.info(f"头像已保存: {out_path} ({out_path.stat().st_size / 1024:.1f}KB)")
            return str(out_path)
        except Exception as e:
            _log.error(f"处理头像失败: {e}", exc_info=True)
            return ""

    # ── 颜色选择器 ──

    def _pick_color(self, target: str):
        attr_map = {
            "ui_bg": "_ui_bg_color_val",
            "chat_bg": "_ui_chat_bg_color_val",
            "user_bubble": "_ui_user_bubble_val",
            "user_text": "_ui_user_text_val",
            "bot_bubble": "_ui_bot_bubble_val",
            "bot_text": "_ui_bot_text_val",
        }
        btn_map = {
            "ui_bg": "_ui_bg_color_btn",
            "chat_bg": "_ui_chat_bg_color_btn",
            "user_bubble": "_ui_user_bubble_btn",
            "user_text": "_ui_user_text_btn",
            "bot_bubble": "_ui_bot_bubble_btn",
            "bot_text": "_ui_bot_text_btn",
        }

        current = getattr(self, attr_map[target])
        color = QColorDialog.getColor(QColor(current), self, "选择颜色")
        if color.isValid():
            hex_color = color.name()
            setattr(self, attr_map[target], hex_color)
            btn = getattr(self, btn_map[target])
            btn.setStyleSheet(f"background: {hex_color}; border: 1px solid #ccc; border-radius: 4px;")

    # ════════════════════════════════════════════════════════════
    # 加载 & 保存
    # ════════════════════════════════════════════════════════════

    def _load_values(self):
        cfg = self._config

        # LLM
        idx = self._llm_provider_combo.findData(cfg.llm.provider)
        if idx >= 0:
            self._llm_provider_combo.setCurrentIndex(idx)
        self._llm_host.setText(cfg.llm.host)
        self._llm_key.setText(cfg.llm.api_key)
        idx = self._llm_model_combo.findText(cfg.llm.model)
        if idx >= 0:
            self._llm_model_combo.setCurrentIndex(idx)
        elif cfg.llm.model:
            self._llm_model_combo.setEditText(cfg.llm.model)
        else:
            self._llm_model_combo.setCurrentIndex(-1)
        # ★ 1.9.3 第 23 轮：max_tokens 现在有界面控件了（以前只能改 config.json）
        try:
            self._llm_max_tokens.setValue(int(cfg.llm.max_tokens or 8192))
        except Exception:
            self._llm_max_tokens.setValue(8192)

        # Pet / 性格
        _card_names = [c["name"] for c in scan_character_cards()]
        if cfg.pet.character and cfg.pet.character in _card_names:
            self._selected_character = cfg.pet.character
        else:
            self._selected_character = _card_names[0] if _card_names else "铃兰"
        self._refresh_character_cards()
        # ★ 1.5.0: 每角色独立设置 —— 从当前角色 settings.json 加载（persona/scale/style/emoji）
        self._load_character_settings_to_ui(self._selected_character)

        # Vision
        idx = self._vision_mode.findData(cfg.vision.mode)
        if idx >= 0:
            self._vision_mode.setCurrentIndex(idx)
        self._vision_api_host.setText(cfg.vision.host)
        self._vision_api_key.setText(cfg.vision.api_key)
        idx = self._vision_api_model_combo.findText(cfg.vision.model)
        if idx >= 0:
            self._vision_api_model_combo.setCurrentIndex(idx)
        elif cfg.vision.model:
            self._vision_api_model_combo.setEditText(cfg.vision.model)
        else:
            self._vision_api_model_combo.setCurrentIndex(-1)
        self._vision_ollama_host.setText(cfg.vision.ollama_host)
        idx = self._vision_ollama_model_combo.findText(cfg.vision.ollama_model)
        if idx >= 0:
            self._vision_ollama_model_combo.setCurrentIndex(idx)
        else:
            self._vision_ollama_model_combo.setEditText(cfg.vision.ollama_model)
        idx = self._vision_ocr_engine.findData(cfg.vision.ocr_engine)
        if idx >= 0:
            self._vision_ocr_engine.setCurrentIndex(idx)
        self._vision_ocr_lang.setText(cfg.vision.ocr_lang)
        self._vision_enabled.setChecked(bool(cfg.vision.enabled))
        # ★ 1.9.0: ① 多模态（独立开关 + 后端选择）
        self._mm_enabled.setChecked(bool(getattr(cfg.vision, "multimodal", False)))
        _mi = self._mm_mode.findData(getattr(cfg.vision, "multimodal_mode", "api") or "api")
        self._mm_mode.setCurrentIndex(_mi if _mi >= 0 else 0)
        self._on_mm_mode_changed(self._mm_mode.currentIndex())
        self._on_vision_mode_changed(0)

        # ★ 1.9.0: ③ STT —— 页面只有一个启用开关（模式/模型/路径已写死为
        #   项目内 data/whisper，不再从设置页读写）
        self._stt_enabled.setChecked(bool(cfg.stt.enabled))

        # TTS
        self._tts_enabled_toggle.setChecked(cfg.tts.enabled)
        idx = self._tts_mode.findData(cfg.tts.mode)
        if idx >= 0:
            self._tts_mode.setCurrentIndex(idx)
        idx = self._tts_provider.findData(cfg.tts.provider)
        if idx >= 0:
            self._tts_provider.setCurrentIndex(idx)
        tts_char_inv = {
            "feibi": "feibi (菲比·中文) ★本地模型",
            "mika": "晓依 (活泼女声) Edge",
            "thirtyseven": "云希 (男声) Edge",
            "linglan": "晓晓 (温柔女声) Edge",
        }
        display_name = tts_char_inv.get(cfg.tts.character, "feibi (菲比·中文) ★本地模型")
        idx = self._tts_character.findText(display_name)
        if idx >= 0:
            self._tts_character.setCurrentIndex(idx)
        self._tts_genie_data.setText(cfg.tts.genie_data_dir or "")
        self._tts_genie_char.setText(cfg.tts.genie_character_dir or "")
        self._tts_genie_preload.setChecked(
            bool(getattr(cfg.tts, "genie_preload_on_start", False)))
        self._tts_genie_idle.setValue(
            int(getattr(cfg.tts, "genie_idle_release_minutes", 15) or 0))

        # Idle
        self._idle_enabled.setChecked(cfg.idle.enabled)
        self._idle_interval.setValue(cfg.idle.interval_minutes)
        try:
            self._idle_prompt.setPlainText(getattr(cfg.idle, "prompt", "") or "")
        except Exception:
            pass

        # Monitor
        self._scr_enabled.setChecked(cfg.monitor.screen_enabled)
        self._scr_interval.setValue(cfg.monitor.screen_interval)
        self._scr_quality.setValue(cfg.monitor.screen_quality)
        self._kb_enabled.setChecked(cfg.monitor.keyboard_enabled)
        self._ms_enabled.setChecked(cfg.monitor.mouse_enabled)
        self._ms_min_move.setValue(cfg.monitor.mouse_min_move)
        self._clipboard_enabled.setChecked(cfg.monitor.clipboard_enabled)
        self._audio_enabled.setChecked(cfg.monitor.audio_enabled)
        self._audio_threshold.setValue(cfg.monitor.audio_threshold)
        self._sys_enabled.setChecked(cfg.monitor.system_enabled)
        self._sys_interval.setValue(cfg.monitor.sys_interval)
        self._summarize_interval.setValue(cfg.monitor.summarize_interval_minutes)

        # Tools / 下载路径
        self._download_path.setText(cfg.tools.download_path or "")

        # ★ 个性化 (UI)
        ui = cfg.ui
        self._ui_chat_width.setValue(getattr(ui, "chat_window_width", 420))
        self._ui_chat_height.setValue(getattr(ui, "chat_window_height", 640))
        self._ui_chat_distance.setValue(getattr(ui, "chat_distance", 10))
        # ★ 1.9.1: 侧栏宽度控件已删（左侧栏整个移除）
        self._ui_opacity.setValue(int(getattr(ui, "opacity", 0.92) * 100))

        # 个性化颜色（从 UIConfig 扩展字段读取，兼容旧版）
        # ★ 1.8.2: 这里必须用「显式映射」——之前用
        #   f"_ui_{attr_key.replace('_color','')}_val" 拼属性名，
        #   对 ui_bg_color / chat_bg_color 会拼成 _ui_ui_bg_val / _ui_chat_bg_val，
        #   与 __init__ 里真正的 _ui_bg_color_val / _ui_chat_bg_color_val 对不上：
        #   加载时读到的颜色没写进控件，保存时又把初始默认色写回 config，
        #   于是"大底色/聊天区底色每次启动都被刷回默认"。
        for attr_key, val_attr, btn_attr, default in [
            ("ui_bg_color", "_ui_bg_color_val", "_ui_bg_color_btn", "#ffffff"),
            ("chat_bg_color", "_ui_chat_bg_color_val", "_ui_chat_bg_color_btn", "#f7f7fa"),
            ("user_bubble_color", "_ui_user_bubble_val", "_ui_user_bubble_btn", "transparent"),
            ("user_text_color", "_ui_user_text_val", "_ui_user_text_btn", "#222222"),
            ("bot_bubble_color", "_ui_bot_bubble_val", "_ui_bot_bubble_btn", "transparent"),
            ("bot_text_color", "_ui_bot_text_val", "_ui_bot_text_btn", "#222222"),
        ]:
            val = getattr(ui, attr_key, default) or default
            setattr(self, val_attr, val)
            btn = getattr(self, btn_attr, None)
            if btn is not None:
                btn.setStyleSheet(
                    f"background: {val}; border: 1px solid #ccc; border-radius: 4px;")

        # ★ 聊天头像（★ 1.9.0: config 里存的是相对项目根的路径，预览前还原成绝对）
        self._user_avatar_path = _abs_avatar_path(getattr(ui, "user_avatar_path", "") or "")
        self._pet_avatar_path = _abs_avatar_path(getattr(ui, "pet_avatar_path", "") or "")
        self._set_avatar_preview(self._user_avatar_preview, self._user_avatar_path)
        self._set_avatar_preview(self._pet_avatar_preview, self._pet_avatar_path)

        _log.debug("设置值已加载")

    def _save_settings(self):
        """异步保存设置，不阻塞 UI"""
        if getattr(self, "_saving", False):
            _log.warning("保存正在进行中，忽略重复点击")
            return
        self._saving = True

        self._save_status.setText("保存中...")
        self._save_status.setStyleSheet("color: #007aff; font-size: 11px; background: transparent;")

        # ★ 1.9.2（内存调研结论）：原来**每次保存都新建一个无 parent 的 QTimer**，
        #   旧引用被覆盖 → 那些 QTimer 只能等 GC，反复保存会攒一堆。
        #   改成建一次、之后只 start()（结果一样：单次触发、每次保存重新计时）。
        if getattr(self, "_save_watchdog", None) is None:
            self._save_watchdog = QTimer(self)
            self._save_watchdog.setSingleShot(True)
            self._save_watchdog.timeout.connect(self._on_save_timeout)
        self._save_watchdog.start(5000)

        cfg = self._config
        old_provider = cfg.llm.provider

        try:
            self._do_save_settings(cfg, old_provider)
        except RuntimeError as e:
            _log.error(f"保存设置时控件已销毁: {e}")
            self._save_status.setText("保存失败，请重试")
            self._save_status.setStyleSheet("color: #e04040; font-size: 11px; background: transparent;")
            self._saving = False
            if hasattr(self, "_save_watchdog") and self._save_watchdog:
                self._save_watchdog.stop()

    def _do_save_settings(self, cfg, old_provider, apply_only: bool = False):
        """实际的设置保存逻辑（提取为独立方法，便于异常处理）。

        `apply_only=True`（★ 1.9.3 第 26 轮新增）：**只做"界面 → cfg"的赋值就返回**，
        不起保存线程、不发信号。给退出阶段的 `flush_all_on_exit()` 用 ——
        它要的是"把没点保存的改动收进 cfg"，然后自己同步落盘；
        在窗口正在关闭的时候发 config_changed 信号是危险的。
        """

        # LLM
        cfg.llm.provider = self._llm_provider_combo.currentData() or "deepseek"
        cfg.llm.host = self._llm_host.text().strip()
        cfg.llm.api_key = self._llm_key.text().strip()
        cfg.llm.model = self._llm_model_combo.currentText().strip()
        # ★ 1.9.3 第 23 轮：把界面上的 max_tokens 写回配置（否则设置页改了也不生效）
        try:
            cfg.llm.max_tokens = int(self._llm_max_tokens.value())
        except Exception:
            pass

        # Pet / 性格 —— 当前角色独立保存
        cfg.pet.character = self._selected_character
        # ★ 1.5.0: persona/scale/style/emoji 写回当前角色 settings.json（每角色独立）
        _new_scale = self._pet_scale.value() / 100.0
        _new_style = self._pet_reply_style.currentData() or "concise"
        _new_emoji = self._pet_emoji_level.currentData() or "light"
        _new_persona = self._pet_prompt_edit.toPlainText().strip()
        _new_startup = self._pet_startup_gif.currentData() or ""
        if not _new_startup:
            # 兜底：currentData 为空时用文本，但仍要过滤占位文本
            _new_startup = self._normalize_startup_gif(self._pet_startup_gif.currentText())
        _new_idle = [g for g, cb in self._pet_idle_checks.items() if cb.isChecked()]
        _new_rule = {
            "mode": self._pet_idle_rule_mode.currentData() or "random",
            "value": self._pet_idle_rule_value.value(),
        }
        self._save_character_settings(
            self._selected_character, _new_persona, _new_scale, _new_style, _new_emoji,
            _new_startup, _new_idle, _new_rule)
        # 全局字段同步（派生/兜底）：切换角色时 _apply_to_config 会用角色 settings 覆盖 personality_prompt
        cfg.pet.scale = _new_scale
        cfg.pet.reply_style = _new_style
        cfg.pet.emoji_level = _new_emoji
        cfg.pet.personality_prompt = _new_persona

        # Vision
        cfg.vision.mode = self._vision_mode.currentData() or "api"
        cfg.vision.host = self._vision_api_host.text().strip()
        cfg.vision.api_key = self._vision_api_key.text().strip()
        cfg.vision.model = self._vision_api_model_combo.currentText().strip()
        cfg.vision.ollama_host = self._vision_ollama_host.text().strip()
        cfg.vision.ollama_model = self._vision_ollama_model_combo.currentText().strip()
        cfg.vision.ocr_engine = self._vision_ocr_engine.currentData() or "tesseract"
        cfg.vision.ocr_lang = self._vision_ocr_lang.text().strip() or "chi_sim+eng"
        cfg.vision.enabled = self._vision_enabled.isChecked()
        # ★ 1.9.0: 多模态 / 视觉识别 / STT 三块 → 抽成纯赋值方法（见下），
        #   这样自测可以直接调它，不用真去写 config.json。
        self._apply_media_settings(cfg)

        # TTS
        cfg.tts.enabled = self._tts_enabled_toggle.isChecked()
        cfg.tts.mode = self._tts_mode.currentData() or "auto"
        cfg.tts.provider = self._tts_provider.currentData() or "genie"
        tts_char_map = {
            "feibi (菲比·中文) ★本地模型": "feibi",
            "晓晓 (温柔女声) Edge": "linglan",
            "晓依 (活泼女声) Edge": "mika",
            "云希 (男声) Edge": "thirtyseven",
        }
        cfg.tts.character = tts_char_map.get(self._tts_character.currentText(), "feibi")
        cfg.tts.genie_data_dir = self._tts_genie_data.text().strip()
        cfg.tts.genie_character_dir = self._tts_genie_char.text().strip()
        cfg.tts.genie_preload_on_start = self._tts_genie_preload.isChecked()
        cfg.tts.genie_idle_release_minutes = int(self._tts_genie_idle.value())

        # Idle
        cfg.idle.enabled = self._idle_enabled.isChecked()
        cfg.idle.interval_minutes = self._idle_interval.value()

        # Monitor
        cfg.monitor.screen_enabled = self._scr_enabled.isChecked()
        cfg.monitor.screen_interval = self._scr_interval.value()
        cfg.monitor.screen_quality = self._scr_quality.value()
        cfg.monitor.keyboard_enabled = self._kb_enabled.isChecked()
        cfg.monitor.mouse_enabled = self._ms_enabled.isChecked()
        cfg.monitor.mouse_min_move = self._ms_min_move.value()
        cfg.monitor.clipboard_enabled = self._clipboard_enabled.isChecked()
        cfg.monitor.audio_enabled = self._audio_enabled.isChecked()
        cfg.monitor.audio_threshold = self._audio_threshold.value()
        cfg.monitor.system_enabled = self._sys_enabled.isChecked()
        cfg.monitor.sys_interval = self._sys_interval.value()
        cfg.monitor.summarize_interval_minutes = self._summarize_interval.value()

        # Tools / 下载路径
        cfg.tools.download_path = self._download_path.text().strip()

        # ★ 个性化 (UI)
        cfg.ui.chat_window_width = self._ui_chat_width.value()
        cfg.ui.chat_window_height = self._ui_chat_height.value()
        cfg.ui.chat_distance = self._ui_chat_distance.value()
        # ★ 1.9.1: 不再保存 sidebar_width（字段已从 UIConfig 删除）
        cfg.ui.opacity = self._ui_opacity.value() / 100.0

        # 个性化颜色存入 UIConfig（作为扩展属性）
        for attr_key, field_name in [
            ("_ui_bg_color_val", "ui_bg_color"), ("_ui_chat_bg_color_val", "chat_bg_color"),
            ("_ui_user_bubble_val", "user_bubble_color"), ("_ui_user_text_val", "user_text_color"),
            ("_ui_bot_bubble_val", "bot_bubble_color"), ("_ui_bot_text_val", "bot_text_color"),
        ]:
            setattr(cfg.ui, field_name, getattr(self, attr_key))

        # ★ 聊天头像路径（★ 1.9.0: 存成相对项目根，避免跨版本指向旧目录）
        cfg.ui.user_avatar_path = _portable_avatar_path(self._user_avatar_path)
        cfg.ui.pet_avatar_path = _portable_avatar_path(self._pet_avatar_path)

        # 工具策略
        new_policies = self._get_tools_policy_changes()
        current_policies = self._tool_manager.get_policy_map()
        changed_count = 0
        for name, policy_str in new_policies.items():
            if current_policies.get(name) == policy_str:
                continue
            try:
                from src.core.tool_manager import ToolLoadPolicy
                self._tool_manager.set_policy(name, ToolLoadPolicy(policy_str))
                changed_count += 1
            except Exception:
                pass
        cfg.tool_policies = new_policies
        # ★ 1.9.0: action 级禁用（详情子窗口里的复选框）+ 主动说话的提示词
        try:
            cfg.tools.disabled_actions = self._collect_disabled_actions()
        except Exception as e:
            _log.debug(f"收集 action 禁用失败: {e}")
        try:
            cfg.idle.prompt = (self._idle_prompt.toPlainText() or "").strip()
        except Exception:
            pass
        if changed_count > 0:
            _log.debug(f"工具策略: {changed_count} 项变更")
        enabled_names = [name for name, p in new_policies.items() if p != "disabled"]
        cfg.enabled_tools = enabled_names

        if apply_only:
            # ★ 1.9.3 第 26 轮：只要"界面值收进 cfg"这一步；落盘由调用方同步做
            return

        def _do_save():
            try:
                cfg.save()
                self._save_result_signal.emit("success", "")
            except Exception as e:
                _log.error(f"保存设置失败: {e}", exc_info=True)
                self._save_result_signal.emit("error", str(e)[:80])

            self.config_changed.emit()
            if cfg.llm.provider != old_provider:
                self.provider_changed.emit(cfg.llm.provider)

        threading.Thread(target=_do_save, daemon=True).start()

    def _on_save_result(self, status: str, message: str):
        if hasattr(self, "_save_watchdog") and self._save_watchdog:
            self._save_watchdog.stop()
        self._saving = False

        if status == "success":
            self._save_status.setText("已保存 ✓")
            self._save_status.setStyleSheet("color: #34c759; font-size: 11px; background: transparent;")
            QTimer.singleShot(3000, lambda: self._save_status.setText(""))
        else:
            self._save_status.setText(f"保存失败: {message}")
            self._save_status.setStyleSheet("color: #e04040; font-size: 11px; background: transparent;")
            QTimer.singleShot(5000, lambda: self._save_status.setText(""))

    def _on_save_timeout(self):
        self._save_status.setText("保存超时，请重试")
        self._save_status.setStyleSheet("color: #e04040; font-size: 11px; background: transparent;")
        self._saving = False
        QTimer.singleShot(5000, lambda: self._save_status.setText(""))
        _log.error("保存操作超时（5秒）—— 可能是文件系统阻塞")
