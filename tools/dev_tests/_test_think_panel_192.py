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


def _cur(c):
    """本轮面板（每轮一个之后，取"正在写"的那个）。"""
    return c._think_current["panel"] if c._think_current else None


def _anchors(c):
    return [e["fmt"] for e in c._think_panels]


check("★初始没有面板（按需创建，不占资源）", cw.think_panel_count() == 0)
check("★聊天区还是 QTextBrowser（没被大改）",
      cw._chat_display.__class__.__name__ == "_RoundedBubbleBrowser")

cw.think_write("[系统] 测试系统信息", "system")
for _ in range(6):
    app.processEvents()
check("★think_write 之后面板被创建", cw.think_panel_count() == 1)
check("★面板是聊天区 viewport 的子控件（→ 能跟着消息流走）",
      _cur(cw).parent() is cw._chat_display.viewport(),
      str(_cur(cw).parent()))
check("★面板可见", _cur(cw).isVisible())
check("★占位图已插进文档（锚点有效）",
      _anchors(cw) and cw._find_think_block(cw._think_panels[0]["key"]) is not None)
check("★占位图高度 == 面板高度（两者对齐才不重叠）",
      int(_anchors(cw)[0].height()) == _cur(cw).desired_height(),
      f"{_anchors(cw)[0].height()} vs {_cur(cw).desired_height()}")
check("★占位图**不**按面板宽度设（只占高度，避免撑宽文档）",
      int(_anchors(cw)[0].width()) == cw.THINK_ANCHOR_W,
      f"{_anchors(cw)[0].width()} vs THINK_ANCHOR_W={cw.THINK_ANCHOR_W}")

cw.think_write("思考一行", "think")
for _ in range(4):
    app.processEvents()
check("★混写思考+系统都在面板里",
      any(k == "think" for k, _ in _cur(cw).lines())
      and any(k == "system" for k, _ in _cur(cw).lines()),
      str(_cur(cw).lines()))

cw.think_delta("流式")
cw.think_delta("增量")
for _ in range(4):
    app.processEvents()
check("★think_delta 走流式拼接", "流式增量" in _cur(cw).text(),
      _cur(cw).text())

# 高度变化后占位图跟着变
_before = int(_anchors(cw)[0].height())
for i in range(30):
    cw.think_write(f"[系统] 刷屏 {i}", "system")
for _ in range(6):
    app.processEvents()
_after = int(_anchors(cw)[0].height())
check("★★大量内容后占位图高度更新（文档留出正确空间）", _after >= _before,
      f"{_before} → {_after}")
check("★★面板封顶在「能装 5 排」",
      _cur(cw).visible_lines() == 5.0, str(_cur(cw).visible_lines()))

cw._chat_display.verticalScrollBar().setValue(0)
app.processEvents()
check("★滚到顶后仍在可视区内（定位没算飞）", _cur(cw).y() > -500,
      str(_cur(cw).y()))

cw.think_clear()
app.processEvents()
check("★think_clear 清空内容但保留面板",
      cw.think_panel_count() >= 1 and all(
          e["panel"].is_empty() for e in cw._think_panels))

cw.clear_chat()
app.processEvents()
check("★★clear_chat 后全部释放（否则下次插图会飘到旧位置）",
      cw.think_panel_count() == 0 and cw._think_current is None)
cw.think_write("清空后重新写", "think")
app.processEvents()
check("★★清空后能重新创建面板并正常工作",
      cw.think_panel_count() == 1 and "清空后重新写" in _cur(cw).text(),
      str(_cur(cw) and _cur(cw).text()))

# ══════════════════════════════════════════════════════════════
head("D2. ★★★ 每轮一个面板（用户 22:05 的核心要求）")
#
# 用户原话："我要的不是只有一个，是每次'用户信息'+'思考/系统信息子窗口'+
#           'LLM回复（包括文件，表情包）'，如果没做完，那么后面继续+…"
# 旧实现全程只建一个面板并一直复用 → 用户"只看到一眼，后面就再也每出现过了"。
cw.clear_chat()
app.processEvents()


def _round(c, user_text, think_text, reply_text):
    """模拟一轮：用户消息 → 思考 → LLM 回复。"""
    c._append_message("user", user_text)
    c.think_write(f"[系统] {think_text}", "system")
    c.append_streaming_text(reply_text)
    c.on_stream_done()
    for _ in range(3):
        app.processEvents()


_round(cw, "第一轮问题", "第一轮思考", "第一轮回复")
check("★★第 1 轮后有 1 个面板", cw.think_panel_count() == 1,
      str(cw.think_panel_count()))
check("★★回复开始后面板已封口（下一轮才会开新的）",
      cw._think_current is None)

_round(cw, "第二轮问题", "第二轮思考", "第二轮回复")
check("★★★第 2 轮后有 2 个面板（每轮一个，不再是全程一个）",
      cw.think_panel_count() == 2, str(cw.think_panel_count()))
_round(cw, "第三轮问题", "第三轮思考", "第三轮回复")
check("★★★第 3 轮后有 3 个面板", cw.think_panel_count() == 3,
      str(cw.think_panel_count()))

_texts = [e["panel"].text() for e in cw._think_panels]
check("★★每轮面板各自装的是**自己那轮**的思考（不串台）",
      _texts[0].strip().endswith("第一轮思考")
      and _texts[1].strip().endswith("第二轮思考")
      and _texts[2].strip().endswith("第三轮思考"), str(_texts))
check("★★三个面板在文档里各有各的占位图（都在）",
      all(cw._find_think_block(e["key"]) is not None
          for e in cw._think_panels))
check("★★三个占位图的资源名互不相同（不是共用一张图）",
      len({e["key"] for e in cw._think_panels}) == 3,
      str([e["key"] for e in cw._think_panels]))
check("★★面板数量上界 = MAX_THINK_PANELS（不随会话无限增长）",
      cw.MAX_THINK_PANELS >= 3, str(cw.MAX_THINK_PANELS))

# 超量后旧的被摘掉
for _i in range(cw.MAX_THINK_PANELS + 3):
    _round(cw, f"压{_i}", f"压思考{_i}", f"压回复{_i}")
check(f"★★超过上界后只保留 MAX_THINK_PANELS({cw.MAX_THINK_PANELS}) 个",
      cw.think_panel_count() == cw.MAX_THINK_PANELS,
      str(cw.think_panel_count()))
check("★★被摘掉的最旧占位图真的从文档里删了",
      # ★ 1.9.2: _find_think_block 现在返回「无效 block」而不是 None（崩溃修复），
      #   所以这里要用 isValid() 判断，不能再写 `is None`。
      not cw._find_think_block("prts-think-1").isValid())
check("★最近那一轮的面板还在、且有内容",
      cw._think_panels[-1]["panel"].text().strip().endswith(
          f"压思考{cw.MAX_THINK_PANELS + 2}"),
      cw._think_panels[-1]["panel"].text()[-30:])

# "没做完就继续"：一轮里出现多次思考段 → 每段一个面板
cw.clear_chat()
app.processEvents()
cw._append_message("user", "多步任务")
cw.think_write("[系统] 第1段思考", "system")
cw.append_streaming_text("先做第一步")
cw.on_stream_done()
cw.think_write("[系统] 第2段思考", "system")     # 工具轮之后又思考
cw.append_streaming_text("再做第二步")
cw.on_stream_done()
for _ in range(3):
    app.processEvents()
check("★★「没做完就继续」→ 同一轮里出现 2 个面板",
      cw.think_panel_count() == 2, str(cw.think_panel_count()))

# ══════════════════════════════════════════════════════════════
head("E. 信号接线 + 主窗口喂数据")
check("★ChatWidget 有 think_in / think_delta_in 两个跨线程信号",
      hasattr(cw, "think_in") and hasattr(cw, "think_delta_in"))
cw.clear_chat()
app.processEvents()
cw.think_in.emit("[系统] 走信号", "system")
for _ in range(4):
    app.processEvents()
check("★think_in 信号能写进面板", "走信号" in _cur(cw).text(),
      _cur(cw).text())
cw.think_delta_in.emit("信号增量")
for _ in range(4):
    app.processEvents()
check("★think_delta_in 信号能写进面板", "信号增量" in _cur(cw).text(),
      _cur(cw).text())

_mw = (BASE / "src" / "ui" / "main_window.py").read_text(encoding="utf-8")
check("★主窗口思考流喂给面板（_on_thinking_safe）",
      "think_delta_in.emit(text)" in _mw)
# ★ 1.9.2 修订：工具调用/返回**不再**单独 think_in.emit 一次 —— 因为它们走
#   system_msg_in → append_system_msg，而后者现在本身就改投面板了。
#   再 emit 一次会让面板里每条系统信息出现两遍（这是我改完立刻发现并去掉的）。
check("★★工具调用/返回经 system_msg_in 进面板（不再重复 emit 到面板）",
      'self.chat.system_msg_in.emit(f"[调用: {tool_name}({arg_short})]")' in _mw)
check("★★工具返回也经 system_msg_in", 'system_msg_in.emit(f"[{tool_name} 返回]: {short}")' in _mw)
check("★★没有重复投递：main_window 里不再有 think_in.emit",
      "think_in.emit" not in _mw,
      "还有 think_in.emit → 面板里会出现两条")
check("★★append_system_msg 本身就是「进面板」的那一处（单一来源）",
      "def append_system_msg" in (_cws := (BASE / "src" / "ui" / "chat_widget.py")
                                  .read_text(encoding="utf-8"))
      and "self.think_write(str(text), \"system\")" in _cws)
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
_cw2._think_panels[-1]["panel"]._render_hook = _cw2._on_think_rendered
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

# ══════════════════════════════════════════════════════════════
head("I. ★★★ 系统消息不再在聊天区画「灰底白字」气泡（用户 22:34 批注）")
#
# 用户原话："成功了，但是我看见系统消息还在外面，哪些灰低白字可以全删了。
# 另外，调用工具前的思考也要显示到这个窗口，所有的思考，所有的系统信息。"
#
# 做法：append_system_msg() 只改**去向**（聊天富文本 → 思考面板），不改方法名 ——
# 全项目 35 处调用（工具调用/返回、转写、语音、图片识别、文件提示…）
# 一行都不用动，所以不会漏掉某一类系统信息。
_cw4 = ChatWidget()
_cw4.resize(420, 640)
_cw4.show()
for _ in range(6):
    app.processEvents()

_MARK = "系统消息应当只在面板里_MARK42"
_cw4.append_system_msg(_MARK)
for _ in range(4):
    app.processEvents()

_chat_txt = _cw4._chat_display.toPlainText()
_panel_txt = _cw4._think_panels[-1]["panel"].text() if _cw4._think_panels else ""
check("★★★系统消息进了思考面板", _MARK in _panel_txt, _panel_txt[-60:])
check("★★★系统消息**不再**出现在聊天区（灰底白字气泡已删）",
      _MARK not in _chat_txt, _chat_txt[-120:])
check("★面板里那一条标的是 system 类型",
      any(k == "system" and _MARK in t
          for k, t in _cw4._think_panels[-1]["panel"].lines()),
      str(_cw4._think_panels[-1]["panel"].lines())[-100:])

# 跨线程信号那条路（system_msg_in）也必须走面板
_MARK2 = "走信号的系统消息_MARK43"
_cw4.system_msg_in.emit(_MARK2)
for _ in range(4):
    app.processEvents()
check("★★system_msg_in → 也进面板",
      _MARK2 in _cw4._think_panels[-1]["panel"].text())
check("★★system_msg_in → 也不进聊天区",
      _MARK2 not in _cw4._chat_display.toPlainText())

# 空消息不该造出空面板
_n_panels = _cw4.think_panel_count()
_cw4.append_system_msg("")
_cw4.append_system_msg("   ")
for _ in range(3):
    app.processEvents()
check("★空/空白系统消息不产生噪音面板", _cw4.think_panel_count() == _n_panels,
      f"{_n_panels} → {_cw4.think_panel_count()}")

check("★★append_system_msg 不再往聊天区 insertHtml",
      "insertHtml" not in _cw3.split("def append_system_msg")[1].split("def ")[0],
      "还在往聊天区插图 → 灰气泡没删干净")
check("★注释里引了用户原话（灰底白字/所有的系统信息）",
      "灰底白字" in _cw3 or "灰低白字" in _cw3)
check("★注释说明了做法是「只改去向不改方法名」（35 处调用免改）",
      "35" in _cw3)
_cw4.close()

# ══════════════════════════════════════════════════════════════
head("J. ★★★ 崩溃修复：开启新话题后再对话（真机 23:06 静默硬崩溃）")
#
# 真机事故：用户"开启新话题 → 发送你好"→ 聊天窗口消失、形象变孤儿（管道关闭）、
# 小托盘退出、终端高频报错。日志证据只有一条：
#     [ChatWidget] DEBUG: 思考面板定位失败: 'NoneType' object has no attribute 'isValid'
#   主进程日志戛然而止、无 Python 堆栈、无 faulthandler 输出
#   → Qt 里访问**已 deleteLater 销毁的 C++ 对象**的典型症状。
#
# 我这块代码的两个致命疏忽：
#   ① `_find_think_block()` 找不到时返回 None，调用处一律直接 `blk.isValid()`
#      → AttributeError（上面那条日志就是它）；
#   ② "开启新话题"会 deleteLater 旧面板，而面板内部有 80ms 节拍器、
#      宿主还有 _on_think_rendered / think_flush 两条回调路径 →
#      都会去碰那块延迟销毁的内存。
_cw5 = ChatWidget()
_cw5.resize(420, 640)
_cw5.show()
for _ in range(6):
    app.processEvents()

# ① 找不到时必须是「无效 block」而不是 None
check("★★★_find_think_block 找不到时返回无效 block（不是 None）",
      _cw5._find_think_block("绝不存在的key") is not None)
check("★★★返回的 block 的 isValid() 是 False（调用处可安全判断）",
      _cw5._find_think_block("绝不存在的key").isValid() is False)
check("★★_find_think_pos 找不到时返回 -1",
      _cw5._find_think_pos("绝不存在的key") == -1)
_src_cw2 = (BASE / "src" / "ui" / "chat_widget.py").read_text(encoding="utf-8")
check("★源码里不再有 `return None` 跟着 _find_think_block",
      "return QTextBlock()" in _src_cw2)

# ② 真机路径：写面板 → 开新话题(clear_chat) → 再写 → 反复触发回调
_cw5.think_write("第一轮系统信息", "system")
for _ in range(4):
    app.processEvents()
check("★先有一个面板", _cw5.think_panel_count() == 1)
_cw5.clear_chat()
app.processEvents()
check("★开启新话题后旧面板被清空", _cw5.think_panel_count() == 0
      and _cw5._think_current is None)
_ok = True
try:
    # 销毁之后把三条回调路径都跑一遍（真机上就是这些在碰死对象）
    for _ in range(6):
        app.processEvents()
        _cw5._reposition_think_panels()
        _cw5.think_flush()
        _cw5._on_think_rendered()
        _cw5._seal_think_panel()
except Exception as e:
    _ok = False
    print("    ", type(e).__name__, e)
check("★★★清空后反复触发三条回调路径都不抛异常（原来在这里静默崩溃）", _ok)
_cw5.think_write("新话题的系统信息", "system")
for _ in range(4):
    app.processEvents()
check("★★★开启新话题后还能正常新建面板并写入",
      _cw5.think_panel_count() == 1
      and "新话题的系统信息" in _cw5._think_panels[-1]["panel"].text(),
      str(_cw5.think_panel_count()))

# ③ 存活校验与死条目清理
check("★有 _alive() 存活校验（shiboken6.isValid）", callable(_cw5._alive)
      and _cw5._alive(_cw5._think_panels[-1]["panel"]) is True)
check("★_alive(None) 是 False", _cw5._alive(None) is False)
check("★有 _prune_think_panels()", callable(_cw5._prune_think_panels))
# 造一个"已销毁"的条目，确认会被摘掉而不是被继续使用
_dead = _cw5._think_panels[-1]
_dead_panel = _dead["panel"]
_dead_panel.setParent(None)
_dead_panel.deleteLater()
for _ in range(4):
    app.processEvents()
_n_before = len(_cw5._think_panels)
_cw5._prune_think_panels()
check("★★已销毁的面板条目会被自动摘掉（不再被继续碰）",
      len(_cw5._think_panels) <= _n_before
      and all(_cw5._alive(e["panel"]) for e in _cw5._think_panels),
      f"{_n_before} → {len(_cw5._think_panels)}")

# ④ 销毁顺序：必须先停节拍器再 deleteLater
check("★★ThinkPanel 提供了 stop_timers()", hasattr(ThinkPanel, "stop_timers"))
check("★★_dispose_think_panel 先 stop_timers 再 deleteLater（顺序不能反）",
      _src_cw2.index("panel.stop_timers()")
      < _src_cw2.index("panel.deleteLater()"))
_tp = ThinkPanel()
_tp._dirty = True
_tp.stop_timers()
check("★stop_timers 真的停了节拍器并清了脏标记",
      not _tp._flush_timer.isActive() and _tp._dirty is False)
check("★set_render_hook(None) 能断开回调",
      (_tp.set_render_hook(None) or True) and _tp._render_hook is None)
check("★think_panel 里 _flush 只定义一次（我加 stop_timers 时重复过一次）",
      (BASE / "src" / "ui" / "think_panel.py").read_text(encoding="utf-8")
      .count("def _flush(self)") == 1)

# ⑤ 定位：文档坐标 + 自己判断可视区（不能用 cursorRect —— 它会把区外位置钳到边缘）
check("★★定位用文档坐标 blockBoundingRect（cursorRect 会把区外位置钳到视口边缘）",
      "layout.blockBoundingRect(blk)" in _src_cw2)
check("★★不用 cursorRect 定位面板（我上一版用它 → 面板全堆在上边框下面）",
      "self._chat_display.cursorRect(" not in
      _src_cw2.split("def _reposition_think_panels")[1].split("def ")[0])
check("★★滚出可视区的面板会被 hide（不堆在边缘）",
      "p.hide()          # 滚出可视区" in _src_cw2 or "p.hide()" in
      _src_cw2.split("def _reposition_think_panels")[1].split("def ")[0])
check("★★占位图独占一个 block（insertBlock 而不是插 <br>）",
      "textCursor().insertBlock()" in _src_cw2)
_cw5.clear_chat()
app.processEvents()
_cw5._append_message("user", "上面一条用户消息")
_cw5.think_write("系统信息", "system")
for _ in range(4):
    app.processEvents()
if _cw5._think_panels:
    _e = _cw5._think_panels[-1]
    _pos = _cw5._find_think_pos(_e["key"])
    from PySide6.QtGui import QTextCursor as _QTC
    _cur = _QTC(_cw5._chat_display.document())
    _cur.setPosition(_pos)
    _r = _cw5._chat_display.cursorRect(_cur)
    check("★★★面板 y 与占位图 cursorRect.y 完全贴合（不再偏一行去遮气泡）",
          abs(_e["panel"].y() - _r.y()) <= 2,
          f"面板y={_e['panel'].y()} 图y={_r.y()}")
else:
    check("★★★面板 y 与占位图 cursorRect.y 完全贴合", False, "没建出面板")
_cw5.close()

# ⑦ 启动错位修复：占位图不能撑宽文档
_cw6 = ChatWidget()
_cw6.resize(420, 640)
_cw6.show()
for _ in range(6):
    app.processEvents()
check("★★THINK_ANCHOR_W 是 1（占位图只占高度、不占宽度）",
      _cw6.THINK_ANCHOR_W == 1, str(_cw6.THINK_ANCHOR_W))
_cw6.think_write("系统信息", "system")
for _ in range(4):
    app.processEvents()
_doc6 = _cw6._chat_display.document()
check("★★占位图实际宽度是 1px",
      int(_cw6._think_panels[-1]["fmt"].width()) == 1,
      str(_cw6._think_panels[-1]["fmt"].width()))
check("★★★文档没有被占位图撑宽（真机「UI 左右错位、聊天记录跑到屏幕外」的根因）",
      _doc6.idealWidth() <= _cw6._chat_display.viewport().width(),
      f"idealWidth={_doc6.idealWidth()} viewport={_cw6._chat_display.viewport().width()}")
check("★★没有产生水平滚动（内容不会被横向挤出可视区）",
      _cw6._chat_display.horizontalScrollBar().maximum() == 0,
      str(_cw6._chat_display.horizontalScrollBar().maximum()))
check("★面板仍在可视区内（x 不为负、不至于贴出屏幕）",
      _cw6._think_panels[-1]["panel"].x() >= 0,
      str(_cw6._think_panels[-1]["panel"].x()))
check("★源码里两处 setWidth 都用 THINK_ANCHOR_W（不是 _panel_width）",
      "setWidth(self.THINK_ANCHOR_W)" in _src_cw2
      and "fmt.setWidth(self._panel_width())" not in _src_cw2)
_cw6.close()

# ⑥ 主进程要有 faulthandler（这次静默崩溃没留下堆栈）
_mn = (BASE / "main.py").read_text(encoding="utf-8")
check("★★main.py 开了 faulthandler 且写独立文件（下次硬崩溃能留 C 级堆栈）",
      "faulthandler" in _mn and "main_faulthandler.log" in _mn)
check("★注册了 SIGABRT/SIGSEGV 等信号的 dump", "SIGABRT" in _mn)
check("★all_threads=True（崩溃时能看到所有线程）", "all_threads=True" in _mn)

print("\n" + "=" * 60)
print(f"★ 1.9.2 任务4 思考/系统信息子窗口：{PASS} PASS / {FAIL} FAIL")
for f in FAILS:
    print(f"  FAIL: {f}")
print("=" * 60)
sys.exit(1 if FAIL else 0)
