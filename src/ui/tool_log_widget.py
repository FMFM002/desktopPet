"""
工具执行日志组件
- 展开式内部小窗口
- 固定大小, 内容可滚动
"""
import logging
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont, QTextCursor
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QTextEdit, QPushButton,
    QHBoxLayout, QLabel,
)

_log = logging.getLogger("ToolLog")


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

    def add_log(self, text: str, log_type: str = "info"):
        colors = {
            "info": "#aaa",
            "tool_call": "#5af",
            "tool_result": "#5f5",
            "error": "#f55",
            "system": "#ff5",
        }
        color = colors.get(log_type, "#aaa")
        self._log_text.moveCursor(QTextCursor.End)
        self._log_text.insertHtml(f'<span style="color:{color};">{text}</span><br>')
        self._log_text.moveCursor(QTextCursor.End)

    def clear_logs(self):
        self._log_text.clear()

    def show_at(self, x: int, y: int):
        self.move(x, y)
        self.show()
        self.raise_()
