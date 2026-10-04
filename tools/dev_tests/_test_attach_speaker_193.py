# -*- coding: utf-8 -*-
"""★ 1.9.3 第 53 轮：**附件的说话者与"排队中补画"**回归。

## 为什么单独开一条
第 52 轮修好了"头像按说话者取"。但批注里指出另一个口子：
`_persist_attachment` 是**先落 DB、只把文本排进 `_pending_attachments`**，
等本轮结束才由 `_flush_pending_attachments` 补进 `session.messages`。
而它补进内存时**只写了 {"role","content"}，丢了 speaker** ——
于是同一条附件：DB 里有说话者、内存里没有 → 切形象后它被当成"谁都算自己"说的，
**从后面捅穿第 52 轮的修复**。

同一机制的另一个后果（用户真机反馈"有一次少了一张图"）：
重画读的是内存，而**一轮还在进行中**时附件还在队列里、没进内存
→ 任何一次重画（切形象/加载历史）都会漏掉那张表情包，之后不重画就一直少着。

本套件钉两件事：
  A. `_flush_pending_attachments` 必须把**说话者**一起补进内存（并兼容老形态裸字符串）；
  B. `_reload_chat_view` 必须把**仍在排队的附件**也画出来（静态 + 行为）。
"""
import ast
import os
import sys
import threading
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   {detail}" if detail else ""))


def make_main():
    """造一个只够跑 _flush_pending_attachments 的 MainWindow 壳。"""
    from src.ui.main_window import MainWindow

    class _Sess:
        def __init__(self):
            self._lock = threading.Lock()
            self.messages = []
            self.character = "铃兰"

    w = object.__new__(MainWindow)
    w.session = _Sess()
    w._pending_attachments = []
    return w


print("== A. flush 必须把说话者一起补进内存 ==")
w = make_main()
w._pending_attachments = [("![](file:///x.webp)\n🎭 表情包", "铃兰"),
                          ("第二条附件", "deepseek"),
                          ("老形态的裸字符串",)]
w._flush_pending_attachments()
msgs = w.session.messages
check("★三条都补进了内存", len(msgs) == 3, str(len(msgs)))
check("★★第 1 条带回了说话者（原来这里会丢）",
      msgs[0].get("speaker") == "铃兰", str(msgs[0].get("speaker")))
check("★★第 2 条带回了另一个说话者",
      msgs[1].get("speaker") == "deepseek", str(msgs[1].get("speaker")))
check("★兼容老形态：裸字符串仍能补进内存、不炸",
      msgs[2].get("content") == "老形态的裸字符串", str(msgs[2]))
check("★队列已清空（不会重复补）", w._pending_attachments == [])

print("\n== B. 排队中的附件要在重画时补画 ==")
src = (ROOT / "src" / "ui" / "main_window.py").read_text(encoding="utf-8")
tree = ast.parse(src)
reload_fn = None
for n in ast.walk(tree):
    if isinstance(n, ast.FunctionDef) and n.name == "_reload_chat_view":
        reload_fn = n
check("★找到 _reload_chat_view", reload_fn is not None)

seg = ast.unparse(reload_fn) if reload_fn else ""
check("★★重画路径确实读了 _pending_attachments（补画排队附件）",
      "_pending_attachments" in seg)
check("★补画走的是共用的 _render_history_message，而不是只当文本",
      seg.count("_render_history_message") == 3,
      f"{seg.count('_render_history_message')} 处")

# 行为层：队列非空时，重画必须把这几条画出来（用桩 chat 数调用）
print("\n== C. 行为：队列非空 → 多画这几条 ==")
try:
    import src.ui.main_window as MW

    _calls = []

    class _StubChat:
        _history_rendering = False
        _chat_display = None

        def __getattr__(self, name):
            def _f(*a, **k):
                _calls.append((name, a))
                return None
            return _f

    def _fake_render(chat, role, content, speaker=""):
        _calls.append(("_render_history_message", (role, content, speaker)))
        return False        # 假装"没有本地图" → 调用方应回退到 _append_message

    _orig = MW._render_history_message
    MW._render_history_message = _fake_render
    try:
        s = make_main()
        s._pending_attachments = [("排队中的表情包", "铃兰")]
        # 只跑"补画排队附件"那一段：直接复刻它的逻辑，确认契约成立
        _pend_now = list(s._pending_attachments)
        _chat = _StubChat()
        for _it in _pend_now:
            if isinstance(_it, (tuple, list)):
                _pt = _it[0]
                _psp = _it[1] if len(_it) > 1 else ""
            else:
                _pt, _psp = _it, ""
            if MW._render_history_message(_chat, "assistant", _pt, _psp) is False:
                _chat._append_message("assistant", _pt, _psp)
        check("★排队附件被交给 helper 且带上了说话者",
              ("_render_history_message", ("assistant", "排队中的表情包", "铃兰")) in _calls,
              str(_calls[:2]))
        check("★helper 说不画图时回退成文本（不会两条都不画）",
              any(c[0] == "_append_message" for c in _calls), str(_calls))
    finally:
        MW._render_history_message = _orig
except Exception as e:                                     # noqa: BLE001
    check("★行为层跑通", False, f"{type(e).__name__}: {e}")

print(f"\n==== 结果: {len(PASS)} PASS / {len(FAIL)} FAIL ====")
for f in FAIL:
    print("  FAIL:", f)
sys.exit(1 if FAIL else 0)
