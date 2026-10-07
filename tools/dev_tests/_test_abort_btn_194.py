# -*- coding: utf-8 -*-
"""★ 1.9.4：**终止对话 / 终止工具按钮不该"灰色卡死"** 守卫。

## 用户报告（离线前）
    "做了测试，聊天时，**终止对话/终止工具都失效，都是灰色，无法交互，
     概率性触发**，暂时不清楚怎么复现。"

## 查出来的真相：**不是概率，是"按发起方式必然发生"**
  · 按钮在 `chat_widget` 里建好时是 **disabled**；
  · **4 处"放开"全在"用户输入"路径里**：
      `_on_send`（打字发送）/ 上传图片 / 上传文本 / 上传其它文件；
  · **"关掉"在 `on_stream_done()`**。
  ⇒ 凡是**没走那 4 条路径**发起的对话 —— **待机主动说话、指令直传、
    共享工作台、图片识别请求** —— 按钮**从初始化起就一直灰着**。
    用户以为是"概率性"，其实是**看那一轮是怎么发起的**。

## 修法
  把"放开"挪到**"只要开始出字"**这一个单点（`append_streaming_text`）——
  它与"谁发起的"无关：**任何一轮只要桌宠开始说话，用户就该能中止它**。
  "关掉"仍留在 `on_stream_done()`（那一轮真的结束了）。

## 钉什么
  A. 初始是灰的；
  B. ★★ **只要流式出字就放开** —— **这条正是修复点**（用"直接喂流式文本"
     来模拟"不是通过 _on_send 发起的一轮"，覆盖待机/共享工作台等路径）；
  C. 该轮结束后重新关上；
  D. ★ **反向守卫**：确认"放开"不是恒真（初始状态必须是灰的）。
"""
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


from PySide6.QtWidgets import QApplication            # noqa: E402
_app = QApplication.instance() or QApplication([])
from src.ui.chat_widget import ChatWidget             # noqa: E402

_w = ChatWidget()


def _state():
    return (_w._abort_chat_btn.isEnabled(), _w._abort_tool_btn.isEnabled())


print("== A/D. 初始必须是灰的（反向守卫：证明'放开'不是恒真）==")
check("★初始两个按钮都是灰的", _state() == (False, False), str(_state()))

print("\n== B. ★★ 只要流式出字就放开（不论这一轮怎么发起的）==")
_w.append_streaming_text("我在呢～")
check("★★流式出字后**两个按钮都可用**"
      "（用'直接喂流式文本'模拟待机说话/共享工作台这类**不走 _on_send** 的一轮）",
      _state() == (True, True), str(_state()))

print("\n== C. 该轮结束后重新关上 ==")
_w.on_stream_done()
check("★`on_stream_done()` 之后重新变灰（该轮真的结束了）",
      _state() == (False, False), str(_state()))

print("\n== E. 类里不该再有重复定义的方法（顺手清掉的死代码）==")
_src = (ROOT / "src" / "ui" / "chat_widget.py").read_text(encoding="utf-8")
check("★★`set_input_blocked` 只定义一次（原来定义了两次，后一个覆盖前一个）",
      _src.count("def set_input_blocked(") == 1,
      f"实际 {_src.count('def set_input_blocked(')} 次")

print(f"\n==== 结果: {len(PASS)} PASS / {len(FAIL)} FAIL ====")
for f in FAIL:
    print("  FAIL:", f)
sys.exit(1 if FAIL else 0)
