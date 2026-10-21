# -*- coding: utf-8 -*-
"""★ 1.9.4：**临时汇报要持久化 + 加入上下文** 守卫。

## 用户要求（2026-10-06）
    "需要**持久化**并且**加入上下文**。"
  （背景：临时汇报原来只往聊天窗口弹一个**临时气泡** —— 重启就没了；
    模型自己也不知道它刚才"对用户说过什么"。）

## 钉什么
  A. ★★ 会**落库**（`db.add_chat_history`）—— 重启后那条汇报还在聊天记录里；
  B. ★★ 会**攒进上下文**，且**加 `[我给用户的临时汇报]` 前缀**
     （否则模型会把汇报当成"用户说的话"来回应）；
  C. ★★★ **不能在工具执行过程中当场插进 `messages`** ——
     那一刻末尾是**带 `tool_calls` 的 assistant 消息**、`tool` 结果还没追加，
     插一条 assistant 会**打断配对**，API 直接 400。
     ⇒ 必须"**攒到轮次边界再放**"：`note_progress` 只 append 到 `_progress_buf`，
       真正的 `messages.append` 在 `_flush_progress_to_context`，由**每轮开头**调用。
  D. ★ 缓冲字段有初始化（否则第一轮就 AttributeError）；
  E. ★ 接线在 main_window 的 sink 里（显示之外还要走 session）。
"""
import inspect
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PRTS_NO_COMFY_START", "1")
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   {detail}" if detail else ""))


from src.core.session import ChatSession                  # noqa: E402

_src = (ROOT / "src" / "core" / "session.py").read_text(encoding="utf-8")

print("== A. 会落库（重启后还在）==")
_note = inspect.getsource(ChatSession.note_progress)
check("★★`note_progress` 真的调了 `db.add_chat_history`"
      "（原来只是弹个临时气泡，重启就没了）",
      "add_chat_history" in _note)
check("★落库失败不影响对话（有 try）", "except Exception" in _note)

print("\n== B. 会进上下文，且带自述前缀 ==")
_flush = inspect.getsource(ChatSession._flush_progress_to_context)
check("★★追加的是 `assistant` 消息（那是**它自己**说的，不是用户的输入）",
      '"role": "assistant"' in _flush)
check("★★带 `[我给用户的临时汇报]` 前缀 —— 否则模型会把汇报当成用户的话来回应",
      "我给用户的临时汇报" in _flush)

print("\n== C. ★★★ 必须在**轮次边界**才插，不能在工具执行中当场插 ==")
check("★★★`note_progress` 只**攒进缓冲**，不直接 append 到 messages",
      "_progress_buf.append" in _note and "self.messages.append" not in _note)
check("★★★真正的 `messages.append` 在 `_flush_progress_to_context`（轮次边界才调）",
      "self.messages.append" in _flush)
check("★★而 `_flush_progress_to_context()` 是在**每轮开头**被调用的"
      "（那一刻上一轮的 tool_calls→tool 已配对完整，追加才安全）",
      "_flush_progress_to_context()" in _src
      and _src.index("_flush_progress_to_context()")
      > _src.index("while round_count < self.MAX_ROUNDS"))
check("★注释里写明了**为什么**必须攒着（打断 tool_calls→tool 配对会 400）"
      "—— 免得日后有人想「优化」成当场插",
      "tool_calls" in _note and "400" in _note)

print("\n== D. 缓冲字段有初始化 ==")
check("★★`self._progress_buf` 在 `__init__` 里有初始化"
      "（否则第一轮就 AttributeError）",
      "self._progress_buf: list = []" in _src)
check("★而且**没有**误删旁边的 `_input_blocked`"
      "（我编辑时真的误删过一次，已补回 —— 这条就是防它）",
      "self._input_blocked = False" in _src)

print("\n== E. 接线在 main_window 的 sink 里 ==")
_mw = (ROOT / "src" / "ui" / "main_window.py").read_text(encoding="utf-8")
check("★★sink 里除了显示，还调了 `session.note_progress`",
      "note_progress" in _mw)
check("★仍然经信号回 GUI 线程（**绝不能在工具线程里碰 Qt 控件**）",
      "bot_text_in.emit" in _mw)

print("\n== F. ★★★ 真机 2026-10-21：简单任务也汇报 + 答案重复 ==")
# 用户截图：查一次天气 → 答案 → "铃兰在收拾东西，马上帮你查…" → 答案又一遍。
# 日志证据：第3轮 content=98字符 **且** tool_calls=1（meme）→ 汇报放行；
#          第4轮 content=63字符（同一答案）。
# 两处根因：① 汇报只看工具次数，没看"是否已经开口/跑了多久"；
#          ② 调工具那一轮的 assistant 消息 **content 写死 None** → 模型不记得自己答过。
_PB = (ROOT / "src" / "core" / "progress_bus.py").read_text(encoding="utf-8")

check("★★★调工具那轮的 assistant 消息**保存 content**（不再写死 None）",
      '"content": (content_buffer or None)' in _src)
check("★★★正文统一走 `_emit_text()`，并顺手告诉 progress_bus「已经开口了」",
      "def _emit_text" in _src and "_pb.note_visible_text()" in _src)
check("★★除 `_emit_text` 内部外，没有别处直接调 `self.on_text(`"
      "（否则会绕过「已开口」标记）",
      _src.count("self.on_text(") == 1, str(_src.count("self.on_text(")))
check("★★progress_bus 有「本轮已开口 → 拒绝汇报」这条硬限制",
      "_visible_text_this_turn" in _PB and "已开口被拒" in _PB)
check("★★progress_bus 有「跑 < 20 秒 → 拒绝汇报」这条硬限制",
      "_MIN_SECONDS = 20.0" in _PB)
check("★reset_turn 同时清零计时与「已开口」",
      "_turn_started_at = _t.time()" in _PB and "_visible_text_this_turn = False" in _PB)

# ★★ 2026-10-21 真机性能问题：旁路请求要 8 秒，以前是"先花 8 秒、再被拒"。
check("★★★session **先问闸门**再决定要不要发旁路请求（省掉白等的 8 秒）",
      "_can_rep, _why_no = _pb_r.would_accept()" in _src
      and _src.index("_pb_r.would_accept()") < _src.index("_side_progress_line(valid_tool_calls)"))
check("★★闸门预览函数存在（would_accept）", "def would_accept" in _PB)
check("★兜底那句也过闸门（短任务连'还在弄'都不该出）",
      '_pb_r.would_accept()[0]' in _src)

print(f"\n==== 结果: {len(PASS)} PASS / {len(FAIL)} FAIL ====")
for f in FAIL:
    print("  FAIL:", f)
sys.exit(1 if FAIL else 0)
