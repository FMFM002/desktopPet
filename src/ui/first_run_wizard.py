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

import sys
import threading

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QFrame, QHBoxLayout, QLabel, QLineEdit,
    QPlainTextEdit, QPushButton, QScrollArea, QStackedWidget, QVBoxLayout, QWidget,
)

from src.core import first_run as fr
from src.utils.logger import get_logger
from src.utils.proc import no_window_kwargs as _nw

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

    # ★★ 1.9.4：下载跑在**后台线程**里，所以要靠信号把日志送回 GUI 线程
    #   （Qt 规矩：控件只能在 GUI 线程碰；跨线程直接 appendPlainText 会崩或乱序）。
    _sig_line = Signal(str)      # 下载日志的单行
    _sig_done = Signal(str)      # 某个模型下载结束（key）

    def __init__(self, parent=None, base=None):
        super().__init__(parent)
        self.setWindowTitle("铃兰桌宠 · 首次配置")
        self.setMinimumWidth(520)
        # ★★ 1.9.4 真机修复（用户 2026-10-06）：
        #   "需要**固定窗口长度**，做滚轮。" + "报错的**红色文字显示不完全**，
        #    给的窗口太小了。"
        #   ⇒ 把窗口**高度固定住在合理值**（内容由上面的滚动区消化），
        #     并放宽一点宽度 —— 长报错才换得开行、不截断。
        self.resize(620, 640)
        self.setMinimumHeight(520)
        self.setMaximumHeight(900)
        self.setStyleSheet(_STYLE)
        self._base = base
        # ★★ 1.9.4 真机修复（用户 2026-10-06）：
        #   "我的建议是**首次配置，不提及 comfyUI 和 Ollama**。"
        #   ⇒ 引导只要"不含服务"的那份报告（设置窗口仍用默认的、含服务的）。
        self._report = fr.detect(base=base, include_services=False)
        self._step = 0
        self._checks: dict[str, QCheckBox] = {}
        self._model_rows: dict[str, dict] = {}
        self._build()
        # 下载线程 → GUI：日志逐行进框、结束后恢复按钮
        self._sig_line.connect(self._dl_log.appendPlainText)
        self._sig_done.connect(self._on_download_done)
        self._goto(0)

    def _on_download_done(self, key: str):
        row = self._model_rows.get(key)
        if row:
            row["btn_dl"].setEnabled(True)

    # ══════════════════ 界面 ══════════════════
    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 16)
        root.setSpacing(12)

        self._stack = QStackedWidget(self)
        self._stack.addWidget(self._page_welcome())      # 0
        self._stack.addWidget(self._page_key())          # 1
        self._stack.addWidget(self._page_optional())     # 2
        # ★★ 1.9.4（用户 2026-10-06 要求）：**新增一页专门放"检测 / 下载"**。
        #   用户原话："勾选功能页面就**纯勾选和一些提醒说明**，检测和下载放在下一步。
        #             检测项和下载项要**根据上一步勾选情况来排版**，
        #             不要出现**没勾线但还是要求填写路径**的情况，
        #             傻逼用户可能会卡在这里。"
        #   ⇒ 这一页是**动态生成的**（进这一页时才按勾选结果铺内容），见 _populate_setup()。
        self._setup_page = self._page_setup()            # 3
        self._stack.addWidget(self._setup_page)
        self._stack.addWidget(self._page_done())         # 4

        # ★★ 1.9.4 真机修复（用户 2026-10-06）—— **这条让他走不下去**：
        #   原话："检测模型和具体数据配置页面，**选项太多，页面太长，没有滚轮，
        #         看不见下一步按钮和下面的配置**。需要**固定窗口长度，做滚轮**。"
        #   根因：页面直接堆在窗口里，内容一长就把底部按钮条**顶出可视区**。
        #   ⇒ 把整叠页面塞进 **QScrollArea**，按钮条留在滚动区**外面**
        #     —— 内容再长，按钮条也永远可见。
        _scroll = QScrollArea()
        _scroll.setWidgetResizable(True)
        _scroll.setFrameShape(QFrame.NoFrame)
        _scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        _scroll.setWidget(self._stack)
        root.addWidget(_scroll, 1)

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

        # ══════════════════════════════════════════════════════════
        # ★★ 1.9.4 真机追加（用户 2026-10-06 要求）：
        #   "下载之前希望先询问用户有没有所需文件，要求填写路径，
        #    **扫描路径（深度为1）**，确认是正确的模型就把路径写到配置里面。"
        #   "下载 log 希望显示在引导里面，让用户看见正在下载模型。"
        #   ⇒ 每个"缺失的模型"给三样：① 路径输入（我本地已有 → 扫描并写入配置）
        #        ② 下载按钮（真下）③ 日志框（看得见在动，而不是干等）
        # ══════════════════════════════════════════════════════════
        # ★★ 1.9.4（用户 2026-10-06）：这一页**只放纯勾选 + 提醒说明**。
        #   检测 / 下载 / 填路径全部挪到**下一页**（`_page_setup`）——
        #   用户原话："勾选功能页面就纯勾选和一些提醒说明，检测和下载放在下一步。"
        #   原来这里就摆着"模型文件 + 路径框 + 下载"，而用户还没勾任何东西，
        #   于是出现"**没勾线但还是要求填写路径**"的情况（他说"傻逼用户会卡在这里"）。
        _hint = QLabel("勾好之后，<b>下一步</b>我会按你勾的内容逐项检查"
                       "（缺什么、要不要下、本地已有的可以指路径给我扫描）。")
        _hint.setObjectName("hint")
        _hint.setWordWrap(True)
        lay.addWidget(_hint)

        lay.addStretch(1)
        return w

    # ── 模型：扫路径 / 下载（★ 1.9.4 追加）──
    def _on_scan(self, key: str):
        """用户说"我本地已经有了" → 扫他给的路径（**深度 1**）→ 校验 → 写配置。"""
        from src.core import models as _M
        row = self._model_rows.get(key)
        if not row:
            return
        p = row["edit"].text().strip()
        if not p:
            row["res"].setStyleSheet("color:#b91c1c")
            row["res"].setText("请先填写路径")
            return
        ok, found, why = _M.probe_dir(key, p, depth=1)
        if not ok:
            row["res"].setStyleSheet("color:#b91c1c")
            row["res"].setText(why)
            return
        ok2, msg = _M.bind_path(key, found)
        row["res"].setStyleSheet("color:#15803d" if ok2 else "#b91c1c")
        row["res"].setText(why + ("\n" + msg if ok2 else f"\n{msg}"))

    def _on_download(self, key: str):
        """★ 在**向导里**下载，输出**实时**写进日志框（用户要求"看得见在下载"）。"""
        row = self._model_rows.get(key)
        if not row:
            return
        row["btn_dl"].setEnabled(False)
        self._dl_log.appendPlainText(f"── 开始获取 {key} ──")

        def _work():
            import subprocess
            from pathlib import Path as _P
            _root = _P(__file__).resolve().parents[2]
            try:
                proc = subprocess.Popen(
                    [sys.executable, str(_root / "tools" / "model_manager.py"),
                     "install", "--key", key, "--yes"],
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                    encoding="utf-8", errors="replace", cwd=str(_root), **_nw())
                for line in proc.stdout:            # 逐行 → 日志框
                    self._sig_line.emit(line.rstrip())
                proc.wait()
                self._sig_line.emit(f"── {key} 结束（返回码 {proc.returncode}）──")
            except Exception as e:
                self._sig_line.emit(f"[错误] {type(e).__name__}: {e}")
            finally:
                self._sig_done.emit(key)

        # ★ 真的把线程启动起来（我第一版写了 _work 却忘了 start —— 那样按钮点了没反应）
        threading.Thread(target=_work, daemon=True).start()

    # ── 第 4 步：完成 ──
    # ══════════════════════════════════════════════════════════
    # ★★ 1.9.4（用户 2026-10-06）：**按勾选排版的"检测 / 下载"页**
    #
    #   用户原话："检测项和下载项要**根据上一步勾选情况来排版**，
    #             不要出现**没勾线但还是要求填写路径**的情况。"
    #   所以这一页**进来时才生成**（`_populate_setup`），
    #   而且**只为勾了的东西**出内容 —— 没勾的连字都不出现。
    # ══════════════════════════════════════════════════════════
    def _page_setup(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setSpacing(6)
        self._setup_lay = lay

        self._setup_title = QLabel("按你勾选的来检查")
        self._setup_title.setObjectName("title")
        lay.addWidget(self._setup_title)

        self._setup_sub = QLabel("")
        self._setup_sub.setObjectName("sub")
        self._setup_sub.setWordWrap(True)
        lay.addWidget(self._setup_sub)

        # 动态区：每次进这一页都清空重建
        self._setup_host = QWidget()
        self._setup_host_lay = QVBoxLayout(self._setup_host)
        self._setup_host_lay.setContentsMargins(0, 4, 0, 0)
        lay.addWidget(self._setup_host)

        # 下载日志（用户要求"显示在引导里面，让用户看见正在下载"）
        self._dl_log = QPlainTextEdit()
        self._dl_log.setReadOnly(True)
        self._dl_log.setMaximumHeight(110)
        self._dl_log.setPlaceholderText("（下载时的输出会显示在这里）")
        lay.addWidget(self._dl_log)

        lay.addStretch(1)
        return w

    def _populate_setup(self):
        """★ 按 `self._optional_picked`（勾选页的结果）**重新生成**这一页的内容。

        规则（用户："不要出现没勾线但还是要求填写路径的情况"）：
          · 只为**勾了的**项目出内容；
          · 没勾的**一个字都不显示**；
          · 勾了但**已就位**的 → 只显示"✅ 已就位"，**不给**填路径的框
            （没有可填的东西，给了只会让人卡住）。
        """
        # 清空上一次的内容
        while self._setup_host_lay.count():
            _it = self._setup_host_lay.takeAt(0)
            _w = _it.widget()
            if _w is not None:
                _w.deleteLater()
        self._model_rows = {}

        _picked = list(getattr(self, "_optional_picked", []) or [])
        if not _picked:
            self._setup_title.setText("没有勾选任何可选项")
            self._setup_sub.setText("你什么都没勾 —— 那就直接点「下一步」。"
                                    "以后想开，在「右键桌宠 → 设置」里随时能开。")
            return

        self._setup_title.setText("按你勾选的来检查")
        self._setup_sub.setText("下面**只有你勾了的**项目。缺的东西可以"
                                "① 指个路径让我扫描（本地已有就别重复下载），或 ② 我来下。")

        for _f in self._report.findings:
            if _f.key not in _picked:
                continue
            _row = QWidget()
            _rl = QVBoxLayout(_row)
            _rl.setContentsMargins(0, 2, 0, 6)
            _st = "✅ 已就位" if _f.ok else "❌ 缺少模型/未配置"
            _lab = QLabel(f"{_st} &nbsp;<b>{_f.label}</b> &nbsp;"
                          f"<span style='color:#6b7280'>{_f.detail}</span>")
            _lab.setTextFormat(Qt.RichText)
            _rl.addWidget(_lab)
            if _f.hint:
                _h2 = QLabel("　　" + _f.hint)
                _h2.setObjectName("hint")
                _h2.setWordWrap(True)
                _rl.addWidget(_h2)

            # ★★ 1.9.4 真机修复（用户 2026-10-06）：
            #   "我的建议是**首次配置，不提及 comfyUI 和 Ollama**。
            #    **ollama 在设置里面**检测，下载，扫描，选择。
            #    **comfyUI 就完全从项目剥离**作为第三方功能…"
            #   ⇒ 引导这一页**不再出**本地服务的检测按钮（那两项已经不在报告里了）。
            #     Ollama 的一键获取改到**设置窗口**（见 settings 那边）。
            #     所以这里原先那块 `_svc_rows` / `_on_probe` 整块删掉了。

            # ★ 已就位的**不给**填路径的框（没有可填的东西，给了只会让人卡住）
            # ★★ 而且**只有"模型"才给路径/下载** —— 本地服务（ollama/comfyui）
            #   靠上面那个「检测」按钮，它们**不是模型**，没有"路径"可填、也没有
            #   "下载模型"这回事。（我第一版漏了这个 group 判断，于是 ollama
            #   同时出现在"模型行"和"服务行"里 —— 两套 UI 打架。）
            if not _f.ok and _f.group == "models":
                _h = QHBoxLayout()
                _edit = QLineEdit()
                _edit.setPlaceholderText("本地已有的话，把路径粘这里（指它的上一级也行）")
                _h.addWidget(_edit, 1)
                _btn_scan = QPushButton("扫描并写入")
                _btn_dl = QPushButton("下载")
                _h.addWidget(_btn_scan)
                _h.addWidget(_btn_dl)
                _rl.addLayout(_h)
                _res = QLabel("")
                _res.setObjectName("hint")
                _res.setWordWrap(True)
                _rl.addWidget(_res)
                self._model_rows[_f.key] = {"edit": _edit, "res": _res,
                                            "btn_dl": _btn_dl, "key": _f.key}
                _btn_scan.clicked.connect(
                    lambda _c=False, k=_f.key: self._on_scan(k))
                _btn_dl.clicked.connect(
                    lambda _c=False, k=_f.key: self._on_download(k))
            self._setup_host_lay.addWidget(self._card(_row))

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
        # ★★ 1.9.4：离开"勾选页"（index 2）后进入"检测/下载页"（index 3）时，
        #   先把勾选结果收下来，再**按勾选**重建那一页 —— 这就是"根据上一步勾选排版"。
        if i == 3:
            self._apply_optional()          # 收下勾选结果（原来只在点完成时收）
            self._populate_setup()
        # ★★ 1.9.4 真机修复：进入"完成页"时**立刻把内容填上**。
        #   原来只在本页点「完成」时才 _render_done()，于是用户切到这一页时
        #   看到的是**一个空白的框**（真机反馈原话："下面有一个空白的框，不知道是干嘛的"）。
        if i == self._stack.count() - 1:
            self._render_done()
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
            parts.append(f"· 你勾了：{names}")
        # ★★ 1.9.4 真机修复：勾了**需要模型**的能力却什么也没发生。
        #   用户原话："点击完成。形象出现。**没有看见下载模型和其他东西的迹象**。"
        #   原因是本包**故意不含模型**（否则 77MB 变 2GB 级），而向导只是"记录勾选"，
        #   既没下也没说怎么下 —— 用户自然会以为坏了。
        #   ⇒ 这里把"下一步到底做什么"**明说出来**，并给出确切命令。
        _MODEL_KEYS = {"whisper": "STT 语音识别", "easyocr": "OCR 文字识别",
                       "genie": "TTS 语音合成"}
        _need = [k for k in picked if k in _MODEL_KEYS]
        if _need:
            _who = "、".join(_MODEL_KEYS[k] for k in _need)
            parts.append(
                f"<br>⚠ <b>你勾的能力需要额外的模型文件（本包不含，共约 2.4 GB）</b>："
                f"{_who}<br>"
                f"　　在项目目录下跑这一条即可获取：<br>"
                f"　　<code>python tools/model_manager.py install --all</code><br>"
                f"　　（不装也能用其他功能 —— 只是这几项不可用）")
        parts.append("<br>· 其余项目都用了自动检测到的值，或程序默认值")
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
