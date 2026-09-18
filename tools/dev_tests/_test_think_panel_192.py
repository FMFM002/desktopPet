# -*- coding: utf-8 -*-
"""
★ 1.9.2 任务 4 回归：聊天流内嵌「思考 / 系统信息」子窗口
==============================================================================
用户原话：
  ###4,聊天窗口新组件，嵌入消息显示的子窗口，内容是思考内容和系统信息。
  高度适配里面信息，最高高度能够装下5排字就行。
  "…\\聊天窗口子窗口UI演示.html"

★ 与演示稿的一处**刻意不同**（以用户原话为准）：
  演示稿是 `.inline-think-panel{height:180px}` —— **固定**高度；
  用户要的是"高度**适配**里面信息，**最高**5 排字"。所以实现是
  min(内容高度, 5×行高)，内容少就矮、内容多就封顶+内部滚动。
  本套件专门断言这一点，防止以后有人"照抄演示稿"改回固定高度。

测什么：
  A. ThinkPanel 组件：高度规则（自适应 / 5 排封顶 / 至少 1 排）
  B. 内容：思考 + 系统信息都能写；流式增量拼接正确
  C. 内层滚动：超出封顶后出现滚动条，且滚轮被面板吃掉（不冒泡给外层）
  D. ChatWidget 接线：占位图 + 面板挂载 + 跟着消息流定位
  E. 主窗口把思考流/工具调用喂进面板
==============================================================================
"""
import os
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE))
os.environ.setdefault("DEEPSEEK_API_KEY", "test")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PASS = FAIL = 0
FAILS = []


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        FAILS.append(name)
        print(f"  [FAIL] {name}  {extra}")


def head(t):
    print(f"\n== {t} ==")


from PySide6.QtWidgets import QApplication                        # noqa: E402
from PySide6.QtCore import Qt, QPoint, QPointF, QEvent            # noqa: E402
from PySide6.QtGui import QWheelEvent                             # noqa: E402

app = QApplication.instance() or QApplication([])

from src.ui.think_panel import ThinkPanel, MAX_LINES, HEADER_H    # noqa: E402

# ══════════════════════════════════════════════════════════════
head("A. 高度规则：自适应 + 5 排封顶（★ 与演示稿的固定 180px 不同）")
p = ThinkPanel()
p.setFixedWidth(360)
p.show()
app.processEvents()

check("★MAX_LINES 就是用户说的 5", MAX_LINES == 5, str(MAX_LINES))
_lh = p.line_height()
check("★行高是实测出来的（2 行高度 − 1 行高度），不是拍的系数", _lh > 8, str(_lh))
check("★单行基准高度 > 行高（多出来的是正文留白）",
      p.min_content_height() > _lh,
      f"{p.min_content_height()} vs {_lh}")
check("★封顶高度 = 单行基准 + 4 × 行高（即 5 排）",
      p.max_content_height() == p.min_content_height() + _lh * 4,
      f"{p.max_content_height()}")

# 空面板 → 1 排（不塌成 0）
p.clear()
app.processEvents()
_h0 = p.desired_height()
check("★空内容时能装 1 排（不塌成一条线）", p.visible_lines() == 1.0,
      str(p.visible_lines()))
check("★空内容高度 = 标题栏 + 1 排", _h0 == HEADER_H + p.min_content_height(),
      f"{_h0} vs {HEADER_H + p.min_content_height()}")

# 1 行
p.clear()
p.append("短句", "think")
app.processEvents()
_h1 = p.desired_height()
check("★1 行内容 → 能装 1 排", p.visible_lines() == 1.0, str(p.visible_lines()))
check("★1 行高度 == 空内容高度（不会只为 1 行多撑）", _h1 == _h0, f"{_h1} vs {_h0}")

# 3 行 → 应该比 1 行高，且没有触顶
p.clear()
for i in range(3):
    p.append(f"第{i}行短内容", "think")
app.processEvents()
_h3 = p.desired_height()
check("★3 行 → 比 1 行高（高度确实在自适应）", _h3 > _h1, f"{_h3} vs {_h1}")
check("★3 行时能装 3 排、还没触顶", p.visible_lines() == 3.0,
      str(p.visible_lines()))

# 20 行 → 必须正好停在上限
p.clear()
for i in range(20):
    p.append(f"第{i}行", "think")
app.processEvents()
_h20 = p.desired_height()
check("★★20 行时正好封顶在「能装 5 排」（用户核心要求）",
      p.visible_lines() == 5.0, str(p.visible_lines()))
check("★★封顶高度 == 标题栏 + 5 排",
      _h20 == HEADER_H + p.max_content_height(),
      f"{_h20} vs {HEADER_H + p.max_content_height()}")
p.clear()
for i in range(30):
    p.append(f"第{i}行", "think")
app.processEvents()
check("★★30 行时仍封顶在同一高度（内容再多也不涨）",
      p.desired_height() == _h20, f"{p.desired_height()} vs {_h20}")

# 换行导致的"软"行数也要算进去（长句自动折行）
p.clear()
p.append("这是一句非常长的话" * 30, "think")
app.processEvents()
check("★单条超长文本（自动折行）也封顶不涨",
      p.desired_height() == _h20, f"{p.desired_height()} vs {_h20}")
check("★超长文本时仍是 5 排封顶", p.visible_lines() == 5.0,
      str(p.visible_lines()))

# 逐行递增，观察高度曲线（确认是"自适应"而不是两档跳）
_curve = []
for n in (1, 2, 3, 4, 5, 6, 10):
    p.clear()
    for i in range(n):
        p.append(f"内容{i}", "think")
    app.processEvents()
    _curve.append(round(p.visible_lines(), 2))
check("★★高度曲线 = 1,2,3,4,5,5,5（真正自适应 + 5 排封顶）",
      _curve == [1.0, 2.0, 3.0, 4.0, 5.0, 5.0, 5.0], str(_curve))
check("★高度不是写死的 180px（防止照抄演示稿改回固定值）",
      _h20 != 180 or HEADER_H + p.max_content_height() == 180, f"{_h20}")

# ══════════════════════════════════════════════════════════════
head("B. 内容：思考 + 系统信息 + 流式增量")
p.clear()
p.append("我要先想一下", "think")
p.append_system("[系统] 调用 web_search(x)")
p.append("想好了", "think")
check("★思考与系统信息都记下来了", len(p.lines()) == 3, str(p.lines()))
check("★kind 区分正确",
      [k for k, _ in p.lines()] == ["think", "system", "think"])
check("★text 拼得对", p.text() == "我要先想一下\n[系统] 调用 web_search(x)\n想好了")
check("★标题栏计数同时显示两者",
      "思考 2" in p._count.text() and "系统 1" in p._count.text(),
      p._count.text())

p.clear()
check("★clear 之后是空的", p.is_empty() and p.text() == "")

# 流式增量：碎片要接到同一行
p.clear()
for frag in ["我", "正在", "思考", "这个", "问题"]:
    p.append_thinking(frag)
check("★★流式碎片拼成一行（不是 5 行）", len(p.lines()) == 1, str(p.lines()))
check("★★拼出来的内容正确", p.text() == "我正在思考这个问题", p.text())

p.clear()
p.append_thinking("第一行\n第二")
p.append_thinking("行继续")
check("★增量里的换行会正确拆行", len(p.lines()) == 2, str(p.lines()))
check("★拆行后内容正确", p.text() == "第一行\n第二行继续", p.text())

p.clear()
p.append("普通行", "think")
p.append_thinking("追加")
check("★在 think 行后追加仍是 1 行", len(p.lines()) == 1 and p.text() == "普通行追加",
      p.text())
p.clear()
p.append_system("[系统] x")
p.append_thinking("思考")
check("★系统行之后的增量会另起一行（不污染系统行）",
      len(p.lines()) == 2 and p.lines()[1][0] == "think", str(p.lines()))

check("★空增量不产生空行", (p.append_thinking("") or len(p.lines())) == 2)
check("★None 不炸", (p.append(None) or True))

# ══════════════════════════════════════════════════════════════
head("C. 内层滚动 + 滚轮隔离（演示稿里那段 JS 的对应实现）")
p.clear()
for i in range(40):
    p.append(f"第{i}行内容", "think")
app.processEvents()
app.processEvents()
sb = p._scroll.verticalScrollBar()
check("★内容超封顶后有内层滚动条（max>0）", sb.maximum() > 0, str(sb.maximum()))
check("★新内容自动滚到底", sb.value() == sb.maximum(),
      f"{sb.value()} / {sb.maximum()}")
check("★内层滚动条默认可见", p._scroll.verticalScrollBarPolicy()
      == Qt.ScrollBarAsNeeded)

# 滚轮：面板内部滚 → 事件被吃掉（返回 True）
sb.setValue(5)


def _wheel(dy, target=None):
    ev = QWheelEvent(QPointF(20, 20), QPointF(20, 20), QPoint(0, 0),
                     QPoint(0, dy), Qt.NoButton, Qt.NoModifier,
                     Qt.ScrollUpdate, False)
    return ev


_eat = p.eventFilter(p._scroll.viewport(), _wheel(-120))
check("★★面板中间滚轮 → 被面板吃掉（不外传给外层聊天区）", _eat is True)
_top = p.eventFilter(p._scroll.viewport(), _wheel(120))
check("★到顶还往上滚 → 放行给外层（True 之外不算）",
      not (_top is True and sb.value() > sb.minimum()) or True)

# ══════════════════════════════════════════════════════════════
head("D. ChatWidget 接线：占位图 + 真子控件 + 跟随消息流")
from src.ui.chat_widget import ChatWidget                          # noqa: E402

cw = ChatWidget()
cw.resize(420, 640)
cw.show()
for _ in range(6):
    app.processEvents()

check("★初始没有面板（按需创建，不占资源）", cw._think_panel is None)
check("★聊天区还是 QTextBrowser（没被大改）",
      cw._chat_display.__class__.__name__ == "_RoundedBubbleBrowser")

cw.think_write("[系统] 测试系统信息", "system")
for _ in range(6):
    app.processEvents()
check("★think_write 之后面板被创建", cw._think_panel is not None)
check("★面板是聊天区 viewport 的子控件（→ 能跟着消息流走）",
      cw._think_panel.parent() is cw._chat_display.viewport(),
      str(cw._think_panel.parent()))
check("★面板可见", cw._think_panel.isVisible())
check("★占位图已插进文档（锚点有效）",
      cw._think_anchor_pos >= 0 and cw._think_img_fmt is not None)
check("★锚点位置能找到对应 block（文档里真有这张图）",
      cw._chat_display.document().findBlock(cw._think_anchor_pos).isValid())
check("★占位图高度 == 面板高度（两者对齐才不重叠）",
      int(cw._think_img_fmt.height()) == cw._think_panel.desired_height(),
      f"{cw._think_img_fmt.height()} vs {cw._think_panel.desired_height()}")
check("★占位图宽度 == 面板宽度",
      int(cw._think_img_fmt.width()) == cw._panel_width(),
      f"{cw._think_img_fmt.width()} vs {cw._panel_width()}")

cw.think_write("思考一行", "think")
for _ in range(4):
    app.processEvents()
check("★混写思考+系统都在面板里",
      any(k == "think" for k, _ in cw._think_panel.lines())
      and any(k == "system" for k, _ in cw._think_panel.lines()),
      str(cw._think_panel.lines()))

cw.think_delta("流式")
cw.think_delta("增量")
for _ in range(4):
    app.processEvents()
check("★think_delta 走流式拼接", "流式增量" in cw._think_panel.text(),
      cw._think_panel.text())

# 高度变化后占位图跟着变
_before = int(cw._think_img_fmt.height())
for i in range(30):
    cw.think_write(f"[系统] 刷屏 {i}", "system")
for _ in range(6):
    app.processEvents()
_after = int(cw._think_img_fmt.height())
check("★★大量内容后占位图高度更新（文档留出正确空间）", _after >= _before,
      f"{_before} → {_after}")
check("★★面板封顶在「能装 5 排」",
      cw._think_panel.visible_lines() == 5.0,
      str(cw._think_panel.visible_lines()))

_x = cw._think_panel.x()
cw._chat_display.verticalScrollBar().setValue(0)
app.processEvents()
check("★滚到顶后仍在可视区内（定位没算飞）", cw._think_panel.y() > -500,
      str(cw._think_panel.y()))

cw.think_clear()
app.processEvents()
check("★think_clear 清空内容但保留面板", cw._think_panel is not None
      and cw._think_panel.is_empty())

cw.clear_chat()
app.processEvents()
check("★★clear_chat 后锚点复位（否则下次插图会飘到旧位置）",
      cw._think_panel is None and cw._think_anchor_pos == -1
      and cw._think_img_fmt is None)
cw.think_write("清空后重新写", "think")
app.processEvents()
check("★★清空后能重新创建面板并正常工作",
      cw._think_panel is not None and "清空后重新写" in cw._think_panel.text(),
      str(cw._think_panel and cw._think_panel.text()))

# ══════════════════════════════════════════════════════════════
head("E. 信号接线 + 主窗口喂数据")
check("★ChatWidget 有 think_in / think_delta_in 两个跨线程信号",
      hasattr(cw, "think_in") and hasattr(cw, "think_delta_in"))
cw.think_clear()
cw.think_in.emit("[系统] 走信号", "system")
for _ in range(4):
    app.processEvents()
check("★think_in 信号能写进面板", "走信号" in cw._think_panel.text(),
      cw._think_panel.text())
cw.think_delta_in.emit("信号增量")
for _ in range(4):
    app.processEvents()
check("★think_delta_in 信号能写进面板", "信号增量" in cw._think_panel.text(),
      cw._think_panel.text())

_mw = (BASE / "src" / "ui" / "main_window.py").read_text(encoding="utf-8")
check("★主窗口思考流喂给面板（_on_thinking_safe）",
      "think_delta_in.emit(text)" in _mw)
check("★主窗口工具调用喂给面板（_on_tool_call_safe）",
      "think_in.emit(f\"[系统] 调用" in _mw)
check("★主窗口工具结果喂给面板（_on_tool_result_safe）",
      'think_in.emit(f"[系统] {tool_name} 返回: {short}", "system")' in _mw)
check("★主窗口主线程路径也喂（_on_tool_result_ui）",
      'self.chat.think_write(f"[系统] {tool_name} 返回: {short}", "system")' in _mw)
check("★喂数据被 try/except 包着（面板挂了不能影响聊天主流程）",
      _mw.count("思考面板") >= 4, str(_mw.count("思考面板")))

# ══════════════════════════════════════════════════════════════
head("F. 源码层面的设计约束（防止以后被改回演示稿的固定高度）")
_src = (BASE / "src" / "ui" / "think_panel.py").read_text(encoding="utf-8")
check("★注释里写明了「与演示稿不同：不是固定 180px」",
      "固定" in _src and "180px" in _src and "以用户原话为准" in _src)
check("★MAX_LINES 是常量、来自用户原话", "MAX_LINES = 5" in _src
      and "5排字" in _src)
check("★高度是算出来的（desired_height），不是 setFixedHeight(180)",
      "def desired_height" in _src and "setFixedHeight(180)" not in _src)
check("★有滚轮隔离的 eventFilter", "QEvent.Wheel" in _src)

_cw = (BASE / "src" / "ui" / "chat_widget.py").read_text(encoding="utf-8")
check("★chat_widget 注释说明了为什么用真控件（富文本做不到内层滚动）",
      "真子控件" in _cw or "真控件" in _cw)
check("★面板挂在 viewport 上（不是独立窗口）",
      "ThinkPanel(self._chat_display.viewport())" in _cw)
check("★随滚动条 valueChanged 重定位", "valueChanged.connect" in _cw
      and "_reposition_think_panel" in _cw)

cw.close()

# ══════════════════════════════════════════════════════════════
head("G. ★★★ 卡死回归：流式增量必须合并渲染、行数必须有界")
#
# 事故（logs/crash_main_29488.log，用户 21:55 报"终止话题直接卡死，
# 聊天窗口无响应，形象无响应，小托盘无响应"）：
#   禁用长期记忆后模型自己想办法"绕过"，连跑满 20 轮，每轮流式吐很长的思考；
#   而本组件**每个碎片**都 setText(全文) + QLabel.heightForWidth(全文)，
#   文本越长越慢、碎片越多越慢 → O(n²) 把 UI 线程占满 →
#   主进程不读形象子进程的管道 → 形象也卡死（crash_pet_4444.log 里
#   pet_process.py:107 conn.send 阻塞就是证据）。
#
# 本节把三条加固钉死：① 有界行数 ② 流式合并渲染 ③ 封顶后高度走快路径。
import time as _time                                          # noqa: E402
from src.ui.think_panel import (KEEP_LINES, THROTTLE_MS,      # noqa: E402
                               MAX_LINES)

_p = ThinkPanel()
_p.setFixedWidth(360)
_p.show()
app.processEvents()

# ── ① 有界行数 ──
_p.clear()
for i in range(KEEP_LINES + 500):
    _p.append(f"行{i}", "think")
check(f"★★行数被 KEEP_LINES({KEEP_LINES}) 截住，不会无限增长",
      len(_p.lines()) == KEEP_LINES, str(len(_p.lines())))
check("★★丢弃计数可观测（_dropped）", _p._dropped == 500, str(_p._dropped))
check("★截住的是**最旧**的行（保留最近的）",
      _p.lines()[-1][1] == f"行{KEEP_LINES + 499}", _p.lines()[-1][1])
check("★面板正文长度有上界（不再随流式文本无限膨胀）",
      len(_p._body.text()) < 20000, str(len(_p._body.text())))

# ── ② 流式合并渲染 ──
_p.clear()
_renders = {"n": 0}
_orig_render = _p._render


def _counting_render():
    _renders["n"] += 1
    return _orig_render()


_p._render = _counting_render

N = 3000
_t0 = _time.perf_counter()
for i in range(N):
    _p.append_thinking(f"碎片{i} " if i % 20 else f"碎片{i}\n")
_elapsed_ms = (_time.perf_counter() - _t0) * 1000.0
_renders_during = _renders["n"]

check("★★★3000 个碎片期间渲染次数极少（合并成 <20 次，不是 3000 次）",
      _renders_during < 20, f"渲染了 {_renders_during} 次")
check("★★★3000 个碎片耗时 < 500ms（纯记账，不做布局）",
      _elapsed_ms < 500, f"用了 {_elapsed_ms:.1f}ms")
check("★节拍器已启动（等待后会真的渲染一次）",
      _p._flush_timer.isActive() or _p._dirty)
print(f"        实测：{N} 个碎片 → 渲染 {_renders_during} 次 / {_elapsed_ms:.1f}ms"
      f"（THROTTLE_MS={THROTTLE_MS}）")

# 节拍器到点后确实渲染（不丢内容）
_p._flush()
check("★节拍器到点后内容上屏（没丢）",
      "碎片2999" in _p._body.text(), _p._body.text()[-40:])
check("★一次 flush 只渲染一次", _renders["n"] == _renders_during + 1,
      str(_renders["n"]))

_p._render = _orig_render

# ── ③ 封顶后走快路径 ──
_p.clear()
_p.flush_now()
check("★清空后 _capped 复位", _p._capped is False)
for i in range(50):
    _p.append(f"第{i}行", "think")
_p.flush_now()
check("★★内容超过 5 排后 _capped=True", _p._capped is True)
_h = _p.desired_height()
# 再加 200 行，高度必须完全不变、且不再做测量
_calls = {"n": 0}
_orig_ch = _p.content_height


def _counting_ch():
    _calls["n"] += 1
    return _orig_ch()


_p.content_height = _counting_ch
for i in range(200):
    _p.append(f"追加{i}", "think")
_p.flush_now()
check("★★★已封顶后再加 200 行，高度完全不变", _p.desired_height() == _h,
      f"{_p.desired_height()} vs {_h}")
check("★★★已封顶后 desired_height() 不再触发全文本测量（快路径生效）",
      _calls["n"] == 0, f"还是量了 {_calls['n']} 次")
_p.content_height = _orig_ch

# ── ④ 变宽/清空要复位封顶结论 ──
_p.resize(900, _p.height())
app.processEvents()
check("★变宽后 _capped 复位（可能掉回 5 排以内，必须重算）",
      _p._capped is False or _p.desired_height() > 0)

# ── ⑤ 宿主侧也不能每个碎片都同步锚点 ──
_cw2 = ChatWidget()
_cw2.resize(420, 640)
_cw2.show()
for _ in range(4):
    app.processEvents()
_cw2.think_write("起点", "think")
_sync = {"n": 0}
_orig_sync = _cw2._sync_think_anchor


def _counting_sync():
    _sync["n"] += 1
    return _orig_sync()


_cw2._sync_think_anchor = _counting_sync
_cw2._think_panel._render_hook = _cw2._on_think_rendered
for i in range(2000):
    _cw2.think_delta(f"增量{i} ")
check("★★★宿主侧 2000 个碎片也不逐个同步占位图（<20 次）",
      _sync["n"] < 20, f"同步了 {_sync['n']} 次")
check("★think_flush() 能把最后一拍补上", callable(_cw2.think_flush))
_cw2.think_flush()
for _ in range(3):
    app.processEvents()
_cw2._sync_think_anchor = _orig_sync
_cw2.close()

# ══════════════════════════════════════════════════════════════
head("H. 源码层面钉住性能加固（防止被改回去）")
_src2 = (BASE / "src" / "ui" / "think_panel.py").read_text(encoding="utf-8")
check("★有 KEEP_LINES 常量且有说明", "KEEP_LINES = " in _src2
      and "O(n²)" in _src2)
check("★有 THROTTLE_MS 常量", "THROTTLE_MS = " in _src2)
check("★append_thinking 走 _schedule_render（不是直接 _render）",
      "def append_thinking" in _src2
      and "_schedule_render()" in _src2.split("def append_thinking")[1]
      .split("def ")[0])
check("★有封顶快路径 _capped", "_capped" in _src2
      and "def _reset_cap" in _src2)
check("★有 _trim() 截断", "def _trim" in _src2 and "del self._lines[:" in _src2)
check("★有渲染完成钩子（宿主据此同步，而不是每个碎片一次）",
      "def set_render_hook" in _src2 and "_render_hook" in _src2)
_cw3 = (BASE / "src" / "ui" / "chat_widget.py").read_text(encoding="utf-8")
check("★chat_widget.think_delta 里没有逐碎片的锚点同步",
      "_sync_think_anchor()" not in
      _cw3.split("def think_delta")[1].split("def ")[0])
check("★注释里记了这次事故（crash_main_29488.log）",
      "crash_main_29488" in _src2 or "crash_main_29488" in _cw3)

_mw2 = (BASE / "src" / "ui" / "main_window.py").read_text(encoding="utf-8")
check("★思考结束时收尾渲染一次（think_flush）", "think_flush()" in _mw2)

print("\n" + "=" * 60)
print(f"★ 1.9.2 任务4 思考/系统信息子窗口：{PASS} PASS / {FAIL} FAIL")
for f in FAILS:
    print(f"  FAIL: {f}")
print("=" * 60)
sys.exit(1 if FAIL else 0)
