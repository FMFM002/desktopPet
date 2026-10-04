"""
工具执行日志组件
- 展开式内部小窗口
- 固定大小, 内容可滚动
"""
import logging
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QTextEdit, QPushButton,
    QHBoxLayout, QLabel,
)

_log = logging.getLogger("ToolLog")

# ★ 1.9.2: 工具日志最多保留多少行（超出自动丢最旧）。
#   内存调研实测：原来 maximumBlockCount=0（无上限），每条 811 B，
#   而 clear_logs() 零调用点 → 是全项目唯一随进程寿命单调增长的 UI 结构。
#   这个窗口只是给人瞄最近几十条，500 行足够。
MAX_LOG_BLOCKS = 500


class ToolLogWidget(QWidget):
    """工具日志窗口"""

    log_closed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setFixedSize(320, 200)

        self._build_ui()
        self._apply_style()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(4)

        header = QHBoxLayout()
        title = QLabel("工具日志")
        title.setStyleSheet("color: #aaa; font-weight: bold;")
        header.addWidget(title)
        header.addStretch()

        close_btn = QPushButton("X")
        close_btn.setFixedSize(24, 24)
        close_btn.clicked.connect(self.hide)
        close_btn.clicked.connect(lambda: self.log_closed.emit())
        close_btn.setStyleSheet("""
            QPushButton {
                background: transparent; color: #999; border: none;
                font-size: 14px; font-weight: bold;
            }
            QPushButton:hover { color: #fff; background: #555; border-radius: 3px; }
        """)
        header.addWidget(close_btn)
        layout.addLayout(header)

        self._log_text = QTextEdit()
        self._log_text.setReadOnly(True)
        # ★ 1.9.2（内存调研结论）：这里是**唯一随进程寿命单调增长**的 UI 结构 ——
        #   实测 `maximumBlockCount=0`（无上限）、每条 811 B，
        #   而 `clear_logs()` 全项目零调用点，所以只增不减。
        #   日志窗口本来只用来瞄最近几十条，给个上限即可（超出自动丢最旧）。
        #   ⚠ `setMaximumBlockCount` 是 QPlainTextEdit 的 API；QTextEdit 要设在
        #     `document()` 上（第一版写成 self._log_text.setMaximumBlockCount
        #     直接 AttributeError）。
        self._log_text.document().setMaximumBlockCount(MAX_LOG_BLOCKS)
        self._log_text.setFont(QFont("Consolas", 10))
        self._log_text.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOn)
        layout.addWidget(self._log_text)

    def _apply_style(self):
        self.setStyleSheet("""
            ToolLogWidget {
                background: rgba(20, 20, 20, 230);
                border: 1px solid #333;
                border-radius: 8px;
            }
            QTextEdit {
                background: rgba(0, 0, 0, 180);
                color: #ccc; border: none;
                border-radius: 4px; padding: 4px;
            }
            QScrollBar:vertical {
                background: #1a1a1a; width: 8px; border-radius: 4px;
            }
            QScrollBar::handle:vertical {
                background: #444; border-radius: 4px; min-height: 20px;
            }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
                height: 0px;
            }
        """)

