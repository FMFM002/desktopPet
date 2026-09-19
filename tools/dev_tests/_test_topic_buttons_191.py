# -*- coding: utf-8 -*-
"""★ 1.9.1：话题按钮 + 标题栏信息 + 聊天区重画。

本文件**取代**了 1.6.0 的 `_test_topic_bookmarks.py`。
替换原因（用户 00:15 的批注）："书签功能直接删除"——
聊天窗口的左侧栏连同里面的书签列表整个移除了，所以原来那一套
（`update_topic_list` / `topic_navigate_request` / `scroll_to_topic_mark` /
`_topic_marks`）已经没有测试对象。旧文件已丢进回收站。

现在测的是新定义：
  · 「开启新话题」= 清空界面 + 清空上下文 + 开一段全新话题
    （用户原话："清空界面，清空上下文，上一轮上下文入库，创建新的上下文db文件"；
     其中"db 文件"本项目实为 chat_records.db 里的一行 topic，见主窗口注释）
  · 「总结旧话题」= 先总结入库，再走开启新话题那套流程，并把总结带进新上下文
  · 左上角显示「形象名 · N tokens」
  · 书签相关的东西确实都不在了
"""
import os
import sys
import threading
from pathlib import Path

BASE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE))
os.environ.setdefault("DEEPSEEK_API_KEY", "test")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import src.core.database as _dbmod  # noqa: E402

_TMP = BASE / "_test_tmp_dbs"
_TMP.mkdir(exist_ok=True)
_dbmod.DB_DIR = _TMP

ok = True


def check(name, cond, extra=""):
    global ok
    print(("  [PASS] " if cond else "  [FAIL] ") + name + (f"  {extra}" if extra else ""))
    if not cond:
        ok = False


from PySide6.QtWidgets import QApplication  # noqa: E402

app = QApplication(sys.argv)

CHAR = "测试角色_话题191"

# ── 1. DB：历史消息（排除当前话题）──
print("== 1. 角色历史消息 ==")
from src.core.database import get_db  # noqa: E402

db = get_db()

t1 = db.create_topic("", CHAR)
# ★ 先清掉本角色在本轮之前留下的残留话题 —— `_test_tmp_dbs` 是 7 个套件**共用**的
#   （不能整个删目录），而本文件跑挂过一次就会把消息留在库里，
#   下一次跑 `get_character_history_messages` 就会多出几条（实测：期望 2 拿到 5）。
for _old in db.list_character_topics(CHAR, limit=500):
    try:
        db.delete_topic(_old["id"])
    except Exception:
        pass
db.delete_topic(t1)
t1 = db.create_topic("", CHAR)
db.add_chat_history("user", "第一段：你好", topic_id=t1)
db.add_chat_history("assistant", "第一段：你好呀", topic_id=t1)
db.archive_topic(t1, "用户打了招呼，聊了天气；未完成：无")
t2 = db.create_topic("", CHAR)
db.add_chat_history("user", "第二段：再见", topic_id=t2)

hist = db.get_character_history_messages(CHAR, exclude_topic=t2, limit=50)
check("历史含旧话题消息", len(hist) == 2, f"{len(hist)}")
check("历史不含当前话题消息", all("再见" not in (m["content"] or "") for m in hist))
check("历史按时间正序", hist[0]["content"] == "第一段：你好")

# ── 2. 左上角：形象名 + tokens ──
print("\n== 2. 左上角「形象名 · tokens」==")
from src.ui.chat_widget import ChatWidget  # noqa: E402
from src.ui import main_window as mw_mod  # noqa: E402

w = ChatWidget()


class _FakeSession:
    character = CHAR
    topic_id = t2

    @staticmethod
    def get_current_topic_info():
        return {"topic_id": t2, "title": "NEW", "message_count": 1,
                "token_count": 1234, "character": CHAR}


stub = type("Stub", (), {})()
stub.session = _FakeSession()
stub.db = db
stub.config = type("C", (), {"pet": type("P", (), {"character": CHAR})()})()
stub.chat = w
mw_mod.MainWindow._update_header_info(stub)

check("标题栏显示形象名", CHAR in w._id_label.text(), w._id_label.text())
check("标题栏显示 tokens 计数（★ 1.9.1 起带 k/M 单位，1234 → 1.2k）",
      "1.2k tokens" in w._id_label.text(), w._id_label.text())
check("★标题栏不再显示话题名（话题名只进 tooltip，且 NEW 会被规范化成「新话题」）",
      "新话题" not in w._id_label.text() and "新话题" in w._id_label.toolTip(),
      f"label={w._id_label.text()!r} tip={w._id_label.toolTip()!r}")
check("★_refresh_topic_list 已被 _update_header_info 取代（书签函数不存在了）",
      not hasattr(mw_mod.MainWindow, "_refresh_topic_list")
      and hasattr(mw_mod.MainWindow, "_update_header_info"))

class _StubBase:
    """所有桩的公共部分：把 MainWindow 的三个真实方法**绑定**到桩上。

    ★ 为什么要绑：`_on_new_topic` 内部会调 `self._update_header_info()`，
    光给桩塞 session/chat/config 会 AttributeError（第一版就挂了）。
    """

    config = stub.config

    def _update_header_info(self):
        return mw_mod.MainWindow._update_header_info(self)

    def _on_new_topic(self):
        return mw_mod.MainWindow._on_new_topic(self)

    def _on_summarize_topic(self):
        return mw_mod.MainWindow._on_summarize_topic(self)


# ── 3. 开启新话题：清空界面 + 建新话题 ──
print("\n== 3. 开启新话题：清空界面 + 清空上下文 ==")


class _NewTopicSession:
    """记录 start_new_topic 被怎么调，不真的建库"""

    def __init__(self):
        self.character = CHAR
        self.topic_id = t2
        self.calls = []
        self.messages = [{"role": "user", "content": "第二段：再见"}]

    def start_new_topic(self, summarize=False):
        self.calls.append(("start_new_topic", summarize))
        self.messages = []          # 清空上下文
        self.topic_id = 9999        # "新话题"

    def summarize_current_topic(self, then_new_topic=True, reason="manual"):
        self.calls.append(("summarize_current_topic", then_new_topic, reason))
        self.messages = []
        self.topic_id = 9998

    def get_current_topic_info(self):
        return {"topic_id": self.topic_id, "title": "NEW",
                "message_count": len(self.messages), "token_count": 0}


w2 = ChatWidget()
w2.clear_chat()
w2._append_message("user", "这是上一段话题的内容，应该被清掉")
check("（前置）界面里确实有旧内容", "应该被清掉" in w2._chat_display.toPlainText())

s3 = _NewTopicSession()
stub3 = _StubBase()
stub3.session = s3
stub3.chat = w2
mw_mod.MainWindow._on_new_topic(stub3)

check("★调了 start_new_topic(summarize=False)", s3.calls == [("start_new_topic", False)],
      str(s3.calls))
check("★上下文被清空", s3.messages == [])
check("★★界面也被清空了（1.6.0 那版是「保留旧内容」，你要求的是清空）",
      "应该被清掉" not in w2._chat_display.toPlainText(),
      w2._chat_display.toPlainText()[:60])
check("★新话题插了一条 # 分割线",
      "#" in w2._chat_display.toPlainText()
      and "新话题" in w2._chat_display.toPlainText())

# ── 4. 总结旧话题：走"总结 + 新话题"那一套 ──
print("\n== 4. 总结旧话题 ==")
s4 = _NewTopicSession()
stub4 = _StubBase()
stub4.session = s4
stub4.chat = ChatWidget()
mw_mod.MainWindow._on_summarize_topic(stub4)
check("★调了 summarize_current_topic(then_new_topic=True)",
      s4.calls == [("summarize_current_topic", True, "旧")], str(s4.calls))

# session 侧的实现是否满足"总结入库 + 继承到新上下文"（查源码，避免真跑 LLM）
_sess_src = (BASE / "src" / "core" / "session.py").read_text(encoding="utf-8")
check("★总结会写回该话题的记录里（archive_topic）",
      "archive_topic(" in _sess_src)
check("★新话题会继承上一段总结（get_last_archived_summary）",
      "get_last_archived_summary" in _sess_src)

# ── 5. 书签/侧栏确实都不在了 ──
print("\n== 5. 书签已按你的要求删除 ==")
for _attr in ("_topic_list", "update_topic_list", "topic_navigate_request",
              "topic_delete_request", "scroll_to_topic_mark", "_topic_marks",
              "refresh_topic_list_request", "topic_list_refresh_in",
              "_sidebar_win", "_sidebar_visible", "_toggle_sidebar_btn",
              "sidebar_toggled"):
    check(f"ChatWidget 没有 {_attr}", not hasattr(w, _attr))
check("★主窗口也没有侧栏/书签的处理函数了",
      not hasattr(mw_mod.MainWindow, "_on_topic_navigate")
      and not hasattr(mw_mod.MainWindow, "_on_topic_delete")
      and not hasattr(mw_mod.MainWindow, "_on_sidebar_toggled"))
check("★总结完成的信号还在，只是改成刷新标题栏",
      hasattr(w, "summary_done_in"))

# ── 6. 聊天区重画：旧话题 + 当前话题，不再写隐形标记 ──
print("\n== 6. 聊天区重画 ==")


class _Sess:
    """只测渲染，不真的调 LLM"""

    def __init__(self):
        self.character = CHAR
        self.topic_id = t2
        self.messages = [{"role": "user", "content": "第二段：再见"},
                         {"role": "assistant", "content": "第二段：拜拜"}]
        self._lock = threading.Lock()

    def get_current_topic_info(self):
        return {"topic_id": self.topic_id, "title": "NEW", "message_count": 2,
                "token_count": 20, "character": self.character}


w3 = ChatWidget()
stub6 = type("Stub6", (), {})()
stub6.session = _Sess()
stub6.db = db
stub6.config = stub.config
stub6.chat = w3
mw_mod.MainWindow._reload_chat_view(stub6)
text = w3._chat_display.toPlainText()
# ★ 1.9.2（R60）：默认只显示最后 2 轮、**不回放归档话题**
#   （用户原话："不应该直接全上下文可见，应该保持上一次退出的状态"）；
#   按过"↑ 历史"（_chat_load_rounds > 2）之后归档内容才回来。
_mw_src = (Path(__file__).resolve().parents[2] / "src" / "ui" / "main_window.py").read_text(encoding="utf-8")
check("★历史加载是'全视图预算'（按一次只多放一轮，不再整段归档灌进来）",
      "_slots = max(0, _limit - len(_cur_keep[-_limit:]))" in _mw_src
      and "if total >= _slots:" in _mw_src)
check("界面含当前话题内容", "第二段：再见" in text)
check("界面用 # 分割线且标注当前话题", "#" in text and "当前话题" in text)
check("★不再写隐形跳转标记（书签跳转才需要它）", "⟦T" not in text)
check("★也不再记 _topic_marks", getattr(stub6, "_topic_marks", None) is None)

# 按过"↑ 历史"之后 → 更早的归档内容应该回来（证明是"分段"而不是"删掉"）
stub6._chat_load_rounds = 6
w3b = ChatWidget()
stub6.chat = w3b
mw_mod.MainWindow._reload_chat_view(stub6)
check("★按过'加载历史'后归档话题会回来",
      "第一段：你好" in w3b._chat_display.toPlainText())

# 清理
db.delete_topic(t1)
db.delete_topic(t2)

print("\n结果:", "ALL PASS" if ok else "FAILED")
sys.exit(0 if ok else 1)
