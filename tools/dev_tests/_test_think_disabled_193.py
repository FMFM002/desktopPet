# -*- coding: utf-8 -*-
"""★ 1.9.3（第 3 轮批注 1 / 第 41 轮补齐）：聊天窗口**不再显示思考 / 工具调用 / 系统信息**。

用户原话：「聊天窗口不再显示思考内容和工具调用，系统信息。删除相关代码。」

★ 第 3 轮只做到"入口变空操作"；**第 41 轮把面板子系统整体物理删除**
（信号 / 连接点 / `_think_panels` 状态 / 十几个空壳方法 / 调用点全部清掉）。
所以本套件现在钉两层：

  1. **契约层**：`think_write` / `think_delta` / `append_system_msg` 调用**不崩、
     且什么都不显示**；面板计数恒为 0；聊天本身仍能正常渲染对话。
  2. **源码层（比原来更强）**：那套面板机制**必须已经不存在** ——
     信号、状态、空壳方法一个都不许回来（否则"删除相关代码"会悄悄反弹）。

原 `_test_think_panel_192.py`（170 条断言）测的正是被删掉的显示路径，
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
check("★_think_panels 状态已彻底不存在",
      not hasattr(w, "_think_panels"), str(getattr(w, "_think_panels", "不存在")))

print("\n== 3. 聊天本身没被搞坏 ==")
w._append_message("user", "你好")
w._append_message("assistant", "在的～")
_t2 = w._chat_display.toPlainText()
check("★仍能渲染对话（用户 + 桌宠）", "你好" in _t2 and "在的" in _t2,
      _t2.replace("\n", "|")[:60])

print("\n== 4. 源码守卫：面板机制必须**已经物理删除**（比「停用」更强）==")
_src = (ROOT / "src" / "ui" / "chat_widget.py").read_text(encoding="utf-8")


def _has_def(src, name):
    return f"def {name}(" in src


# 4a. 那三条惰性入口体里不能有"建面板/写面板"
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
check("★三条入口里没有建面板/写面板的调用", not _bad, "; ".join(_bad[:4]))

# 4b. 被删掉的东西不许回来。
#     ★ 用 **AST** 而不是字符串搜索 —— 注释和 docstring 里写"原来这里有个 xxx"
#       是留痕、不是残留；字符串搜索会被自己的说明文字骗到（本轮就踩了这个坑）。
import ast  # noqa: E402
import pathlib  # noqa: E402


def _used_names(path):
    """AST 里**真正被用到**的名字：定义的函数/类名、属性名、变量名。

    注释、docstring、字符串字面量都不算 —— 这正是我们要的口径。
    """
    tree = ast.parse(pathlib.Path(path).read_text(encoding="utf-8"))
    names = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(n.name)
        elif isinstance(n, ast.Attribute):
            names.add(n.attr)
        elif isinstance(n, ast.Name):
            names.add(n.id)
    return names


_gone = [
    "think_in", "think_delta_in", "think_flush_in",
    "_think_panels", "_think_current",
    "_ensure_think_panel", "_new_think_panel", "_drop_think_panel",
    "_dispose_think_panel", "_seal_think_panel", "_on_think_rendered",
    "_sync_think_anchor", "_reposition_think_panels", "_reposition_think_panel",
    "_find_think_block", "_find_think_pos", "_prune_think_panels",
    "_panel_width", "append_tool_trace", "MAX_THINK_PANELS",
]
_names_cw = _used_names(ROOT / "src" / "ui" / "chat_widget.py")
_left = sorted(n for n in _gone if n in _names_cw)
check("★上面这些残留一个都没回来（AST 口径：注释/docstring 不算）",
      not _left, "; ".join(_left[:6]))

# 4c. 三条惰性入口必须还在（契约不能因为清理而丢掉）
check("★think_write / think_delta / think_panel_count 仍在",
      all(_has_def(_src, f) for f in ("think_write", "think_delta", "think_panel_count")))

# 4d. 主窗口不该再往聊天窗口发思考增量（信号已不存在，发就会 AttributeError）
_names_mw = _used_names(ROOT / "src" / "ui" / "main_window.py")
_bad_mw = sorted(n for n in ("think_delta_in", "think_flush_in", "think_in",
                             "think_flush") if n in _names_mw)
check("★主窗口不再碰思考面板信号 / 不再调 think_flush", not _bad_mw,
      "; ".join(_bad_mw))
check("★思考仍然喂给形象（GIF 表情没被误删）",
      "on_thinking_delta" in _names_mw)

print(f"\n==== 结果: {len(PASS)} PASS / {len(FAIL)} FAIL ====")
for f in FAIL:
    print("  FAIL:", f)
sys.exit(1 if FAIL else 0)
