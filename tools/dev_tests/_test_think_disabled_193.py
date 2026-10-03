# -*- coding: utf-8 -*-
"""★ 1.9.3（第 3 轮批注 1）：聊天窗口**不再显示思考 / 工具调用 / 系统信息** —— 钉住这条。

用户原话：「聊天窗口不再显示思考内容和工具调用，系统信息。删除相关代码。」

本轮落地的是"**停用显示**"（三条入口变空操作）；面板子系统的**物理删除**留下一轮
（它被一整套测试钉着，要连测试一起重写）。所以本套件钉的是"停用"这件事本身：

  1. `think_write` / `think_delta` / `append_system_msg` 调用**不崩、且什么都不显示**；
  2. 不再产生任何思考面板（面板计数恒为 0）；
  3. 聊天窗口本身没被搞坏（仍能正常渲染对话）；
  4. 源码守卫：这三条入口的函数体里**不应再出现**创建/写入面板的调用
     （否则"停用"会随着以后改动悄悄失效）。

原 `_test_think_panel_192.py`（170 条断言）测的正是被停用掉的显示路径，
已按批注 1 退役留档：`临时文件/_test_think_panel_192.py.retired`。
"""
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from PySide6.QtWidgets import QApplication  # noqa: E402

app = QApplication.instance() or QApplication(sys.argv)

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   {detail}" if detail else ""))


print("\n== 1. 三条显示入口：可调用、不崩、不显示 ==")
from src.ui.chat_widget import ChatWidget  # noqa: E402

w = ChatWidget()
for _fn, _args in (("think_write", ("一段思考内容", "think")),
                   ("think_delta", ("思考增量",)),
                   ("append_system_msg", ("[调用: shell_exec]",))):
    try:
        getattr(w, _fn)(*_args)
        _ok = True
    except Exception as e:
        _ok = False
        print(f"      {_fn} 抛错: {type(e).__name__}: {e}")
    check(f"★{_fn}() 可调用且不崩", _ok)

_txt = w._chat_display.toPlainText()
check("★思考内容没有出现在聊天里", "一段思考内容" not in _txt and "思考增量" not in _txt,
      _txt.replace("\n", "|")[:50])
check("★工具调用/系统信息没有出现在聊天里", "[调用: shell_exec]" not in _txt)

print("\n== 2. 不再产生任何思考面板 ==")
check("★面板计数恒为 0", int(w.think_panel_count()) == 0, str(w.think_panel_count()))
check("★_think_panels 恒为空", list(getattr(w, "_think_panels", [])) == [])

print("\n== 3. 聊天本身没被搞坏 ==")
w._append_message("user", "你好")
w._append_message("assistant", "在的～")
_t2 = w._chat_display.toPlainText()
check("★仍能渲染对话（用户 + 桌宠）", "你好" in _t2 and "在的" in _t2,
      _t2.replace("\n", "|")[:60])

print("\n== 4. 源码守卫：三条入口里不能再有「建面板/写面板」的调用 ==")
_src = (ROOT / "src" / "ui" / "chat_widget.py").read_text(encoding="utf-8")
_bad = []
for _fn in ("append_system_msg", "think_write", "think_delta"):
    _m = _src.split(f"def {_fn}(self", 1)
    if len(_m) < 2:
        _bad.append(f"{_fn} 不存在")
        continue
    _body = _m[1].split("\n    def ", 1)[0]
    for _needle in ("_ensure_think_panel(", "_new_think_panel(", "insertImage("):
        if _needle in _body:
            _bad.append(f"{_fn} 里仍有 {_needle}")
check("★三条入口里没有建面板/写面板的调用（停用不会悄悄失效）", not _bad,
      "; ".join(_bad[:4]))

print(f"\n==== 结果: {len(PASS)} PASS / {len(FAIL)} FAIL ====")
for f in FAIL:
    print("  FAIL:", f)
sys.exit(1 if FAIL else 0)
