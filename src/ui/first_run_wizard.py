# -*- coding: utf-8 -*-
"""★★ 1.9.4：**首次配置引导**（图形界面）。

## 设计目标：**让新用户尽量少动手**
依据 1.9.3 第 59 轮的探路实测（探针：`临时文件/_scratch_dev/first_run_probe_59.py`）：

    必须手填 1 项（LLM API Key）｜ 能自动发现 7 项 ｜ 可选 8 项

所以本向导**只强制问 1 件事**：
    第 1 步  欢迎 —— 直接把"已自动发现 N 项"摆出来（让他安心：你几乎不用填）
    第 2 步  **LLM Key**（唯一必填）—— 填完**当场测通**，测不通也能先存着
    第 3 步  可选能力 —— **只对勾了的展开**，没勾的不写、不问
    第 4 步  完成 —— 复述"写了什么 / 写到哪 / 在哪能改" + 给个"打开设置"入口

## 三条铁律（写代码时反复回来看）
  ① **能探测的不问**（不把自动发现的东西拿去做确认题）
  ② **不用的不写**（没勾的能力，绝不写进 .env）
  ③ **可以完全跳过**（关掉窗口就当没这回事，程序照常跑 —— 它只是"更好用"，不是"没它不行"）

## 为什么单独一个文件
主窗口 3698 行已经很大了；引导是**只在首启跑一次**的东西，
塞进主窗口只会让那条最热路径更难读。
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QFrame, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QStackedWidget, QVBoxLayout, QWidget,
)

from src.core import first_run as fr
from src.utils.logger import get_logger

_log = get_logger(__name__)

_STYLE = """
QDialog { background: #fafafa; }
QLabel#title { font-size: 17px; font-weight: 600; color: #1f2937; }
QLabel#sub   { color: #6b7280; font-size: 12px; }
QLabel#hint  { color: #6b7280; font-size: 11px; }
QLabel#ok    { color: #15803d; font-size: 12px; }
QLabel#bad   { color: #b91c1c; font-size: 12px; }
QFrame#card  { background: #ffffff; border: 1px solid #e5e7eb; border-radius: 8px; }
"""


class FirstRunWizard(QDialog):
    """首次配置向导。用法：

        w = FirstRunWizard(parent)
        if w.exec() == QDialog.Accepted:
            ...   # 它自己已经写完配置了
    """

    def __init__(self, parent=None, base=None):
        super().__init__(parent)
        self.setWindowTitle("铃兰桌宠 · 首次配置")
        self.setMinimumWidth(520)
        self.setStyleSheet(_STYLE)
        self._base = base
        self._report = fr.detect(base=base)
        self._step = 0
        self._checks: dict[str, QCheckBox] = {}
        self._build()
        self._goto(0)

    # ══════════════════ 界面 ══════════════════
    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 16)
        root.setSpacing(12)

        self._stack = QStackedWidget(self)
        self._stack.addWidget(self._page_welcome())
        self._stack.addWidget(self._page_key())
        self._stack.addWidget(self._page_optional())
        self._stack.addWidget(self._page_done())
        root.addWidget(self._stack, 1)

        # 底部按钮条
        bar = QHBoxLayout()
        self._btn_skip = QPushButton("跳过（以后在设置里配）")
        self._btn_skip.setFlat(True)
        self._btn_skip.clicked.connect(self.reject)
        bar.addWidget(self._btn_skip)
        bar.addStretch(1)
        self._btn_prev = QPushButton("上一步")
        self._btn_prev.clicked.connect(lambda: self._goto(self._step - 1))
        bar.addWidget(self._btn_prev)
        self._btn_next = QPushButton("下一步")
        self._btn_next.setDefault(True)
        self._btn_next.clicked.connect(self._on_next)
        bar.addWidget(self._btn_next)
        root.addLayout(bar)

    def _card(self, widget: QWidget) -> QFrame:
        f = QFrame()
        f.setObjectName("card")
        lay = QVBoxLayout(f)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.addWidget(widget)
        return f

    # ── 第 1 步：欢迎 + 自动检测结果 ──
    def _page_welcome(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setSpacing(8)

        t = QLabel("欢迎使用铃兰桌宠 👋")
        t.setObjectName("title")
        lay.addWidget(t)

        s = QLabel(f"我先自己检查了一遍你的电脑 —— <b>{self._report.summary()}</b>。")
        s.setObjectName("sub")
        s.setWordWrap(True)
        lay.addWidget(s)

        # ★ 把"已自动发现"逐条摆出来（每条带"我是怎么知道的"，可核对）
        lines = []
        for f in self._report.auto_found:
            lines.append(f"✅ <b>{f.label}</b> —— {f.detail}  "
                         f"<span style='color:#9ca3af'>（{f.how}）</span>")
        box = QLabel("<br>".join(lines) if lines else "（没有自动发现的项目）")
        box.setWordWrap(True)
        box.setTextFormat(Qt.RichText)
        lay.addWidget(self._card(box))

        if self._report.must_fill:
            hint = QLabel("接下来只需要你填 <b>1 样东西</b>："
                          + "、".join(f.label for f in self._report.must_fill)
                          + "。<br>其余的我都能自己搞定，或者你想用哪个再勾哪个。")
        else:
            hint = QLabel("你已经配好了，<b>什么都不用填</b>。直接点「下一步」就行。")
        hint.setObjectName("hint")
        hint.setWordWrap(True)
        lay.addWidget(hint)
        lay.addStretch(1)
        return w

    # ── 第 2 步：LLM Key（唯一必填）──
    def _page_key(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setSpacing(8)

        t = QLabel("第 1 件事：LLM API Key")
        t.setObjectName("title")
        lay.addWidget(t)

        f0 = next((f for f in self._report.findings if f.key == "llm_key"), None)
        already = bool(f0 and f0.ok)

        s = QLabel("桌宠的「大脑」要用它。"
                   + ("<b>你已经配好了</b>，想换再改。" if already
                      else "没有它，桌宠不会说话。"))
        s.setObjectName("sub")
        s.setWordWrap(True)
        lay.addWidget(s)

        self._key_edit = QLineEdit()
        self._key_edit.setEchoMode(QLineEdit.Password)
        self._key_edit.setPlaceholderText("sk-...（粘贴进来即可）")
        self._key_edit.setMinimumHeight(32)
        lay.addWidget(self._key_edit)

        self._host_edit = QLineEdit()
        try:
            from src.utils.config import get_config
            self._host_edit.setText(get_config().llm.host or "")
        except Exception:
            self._host_edit.setText("https://api.deepseek.com/v1")
        self._host_edit.setPlaceholderText("接口地址（一般不用改）")
        lay.addWidget(self._host_edit)

        row = QHBoxLayout()
        self._btn_test = QPushButton("测试一下")
        self._btn_test.clicked.connect(self._on_test)
        row.addWidget(self._btn_test)
        self._test_lbl = QLabel("")
        self._test_lbl.setObjectName("hint")
        row.addWidget(self._test_lbl, 1)
        lay.addLayout(row)

        if f0 and f0.hint:
            h = QLabel("💡 " + f0.hint)
            h.setObjectName("hint")
            h.setWordWrap(True)
            lay.addWidget(h)

        lay.addStretch(1)
        return w

    # ── 第 3 步：可选能力（只对勾了的展开）──
    def _page_optional(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setSpacing(6)

        t = QLabel("想用哪些能力？（都可以不选）")
        t.setObjectName("title")
        lay.addWidget(t)

        s = QLabel("没勾的<b>不会写入任何配置</b>，对应工具保持关闭。以后在设置里随时能开。")
        s.setObjectName("sub")
        s.setWordWrap(True)
        lay.addWidget(s)

        self._opt_host = QWidget()
        olay = QVBoxLayout(self._opt_host)
        olay.setContentsMargins(0, 6, 0, 0)
        olay.setSpacing(4)
        for f in self._report.optional:
            cb = QCheckBox(f"{f.label}")
            if f.ok:
                cb.setChecked(True)
                cb.setEnabled(False)
                cb.setText(f"{f.label}（已就绪）")
            else:
                cb.setToolTip(f.hint)
            self._checks[f.key] = cb
            olay.addWidget(cb)
            if f.hint:
                h = QLabel("　　" + f.hint)
                h.setObjectName("hint")
                h.setWordWrap(True)
                olay.addWidget(h)
        lay.addWidget(self._opt_host)
        lay.addStretch(1)
        return w

    # ── 第 4 步：完成 ──
    def _page_done(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setSpacing(8)
        t = QLabel("好了，可以用了 🎉")
        t.setObjectName("title")
        lay.addWidget(t)
        self._done_lbl = QLabel("")
        self._done_lbl.setWordWrap(True)
        self._done_lbl.setTextFormat(Qt.RichText)
        lay.addWidget(self._card(self._done_lbl))
        h = QLabel("想改任何设置：右键桌宠 → 设置。")
        h.setObjectName("hint")
        self._done_hint = h
        lay.addWidget(h)
        lay.addStretch(1)
        return w

    def done_page_text(self) -> str:
        """完成页的**全部可见文字**（含底部那句指引）。

        ★ 给自测用：我第一版只检查 `_done_lbl`，漏掉了另一条 label 里的
          「想改任何设置…」→ 断言假红了一次。文字分散在多个控件里时，
          要提供"整页文本"这种口径，别让测试去猜是哪个控件。
        """
        return f"{self._done_lbl.text()}\n{self._done_hint.text()}"

    # ══════════════════ 交互 ══════════════════
    def _goto(self, i: int):
        i = max(0, min(i, self._stack.count() - 1))
        self._step = i
        self._stack.setCurrentIndex(i)
        self._btn_prev.setEnabled(i > 0)
        self._btn_next.setText("完成" if i == self._stack.count() - 1 else "下一步")
        # 第 2 步：如果没有必填项，就不要在"配 Key"上停留
        if i == 1 and not self._report.must_fill and not self._key_edit.text().strip():
            self._btn_next.setText("下一步（已配好，可跳过）")

    def _on_next(self):
        if self._step == 1:
            self._apply_key()
        if self._step == self._stack.count() - 1:
            self._apply_optional()
            self._render_done()
            self.accept()
            return
        self._goto(self._step + 1)

    def _on_test(self):
        key = self._key_edit.text().strip()
        self._test_lbl.setObjectName("hint")
        self._test_lbl.setText("测试中…")
        self._btn_test.setEnabled(False)
        self.repaint()
        ok, msg = fr.verify_llm_key(key, self._host_edit.text().strip())
        self._btn_test.setEnabled(True)
        self._test_lbl.setObjectName("ok" if ok else "bad")
        self._test_lbl.setStyleSheet(
            "color:#15803d;font-size:12px" if ok else "color:#b91c1c;font-size:12px")
        self._test_lbl.setText(("✅ " if ok else "❌ ") + msg)

    def _apply_key(self):
        key = self._key_edit.text().strip()
        if not key:
            return                       # 没填就不写 —— 铁律②
        ok, msg = fr.save_llm_key(key, self._host_edit.text().strip())
        _log.info(f"[首次引导] 保存 LLM Key: {ok} / {msg}")
        self._key_saved = msg if ok else f"保存失败: {msg}"
        self._key_ok = ok

    def _apply_optional(self):
        """★ 铁律②：**没勾的能力绝不写**。这里目前只记录用户的选择，
        真正的开关由设置窗口／各工具自己的配置负责 —— 引导**不越权**去改
        用户还没表态的东西。"""
        self._optional_picked = [k for k, cb in self._checks.items() if cb.isChecked()]
        _log.info(f"[首次引导] 用户勾选的能力: {self._optional_picked}")

    def _render_done(self):
        parts = []
        parts.append(f"当前版本：<b>{_ver()}</b>")
        if getattr(self, "_key_ok", False):
            parts.append("✅ LLM Key 已保存到 <code>.env</code>（<b>不会进入版本库</b>）")
        elif getattr(self, "_key_saved", ""):
            parts.append(f"⚠️ <span style='color:#b91c1c'>{self._key_saved}</span>")
        else:
            parts.append("· LLM Key 沿用你原有的配置（本次没改）")
        picked = getattr(self, "_optional_picked", [])
        if picked:
            names = "、".join(self._checks[k].text().split("（")[0] for k in picked)
            parts.append(f"· 你勾了：{names}（按提示配好后即可使用）")
        parts.append("· 其余项目都用了自动检测到的值，或程序默认值")
        self._done_lbl.setText("<br>".join(parts))


def _ver() -> str:
    try:
        from src.version import __version__
        return __version__
    except Exception:
        return "?"


def should_show() -> bool:
    """要不要弹引导。★ 老用户绝不打扰（只差 LLM Key 才算首次）。"""
    try:
        return fr.is_first_run()
    except Exception as e:
        _log.warning(f"[首次引导] 判断失败，按'不弹'处理: {e}")
        return False
