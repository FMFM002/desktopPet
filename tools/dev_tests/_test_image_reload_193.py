# -*- coding: utf-8 -*-
"""★ 1.9.3 第 52 轮：**重画历史时，本地图片必须真的画出来**。

## 现场（用户真机反馈："有一次少了一张图"，那张图是桌宠发的表情包）
`data/DBs/chat_records.db` 里真实存着这样的 assistant 消息：

    ![](file:///D:/Dtopdesk/%E6%99%AE…/data/memes/deepseek-chan/038.webp)\\n🎭 表情包

两条路原来长得不一样：
  · 实时：`self.chat.bot_image_in.emit(p)` → `append_image_preview(p, from_bot=True)`
    → "头像 + 一整行的图片"；
  · 重画：`_reload_chat_view` 只把正文交给 `_append_message` → 与实时那条**对不上**。

本套件钉住修好之后的契约：
  A. 路径解析：`file:///D:/a%20b.png` → `D:\\a b.png`（去 `file:///` + `unquote`）、
     尖括号形式、含空格路径、中文百分号编码、相对路径、远程/内联一律不认；
  B. 一条消息里的**多张图**、**图夹在文字中间**都按出现顺序被认出来；
  C. **文件不存在 / 解析失败 → 绝不抛异常、不画破图**，退回文本渲染并留 `_log`；
  D. **重画那条路（真跑 `_reload_chat_view`）确实会调 `append_image_preview`**
     （归档段 + 当前话题都验；user 侧 `from_bot=False`、assistant 侧 `True`），
     而且文本段顺序不变；
  E. **非图片的普通文本消息行为完全不变**（仍然只走 `_append_message`）；
  F. "往上插历史"那条路遇到含本地图片的一轮 → 回退整段重画（不静默丢图）。
"""
import ast
import logging
import os
import shutil
import sys
import threading
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
BASE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE))

from PySide6.QtWidgets import QApplication  # noqa: E402

app = QApplication.instance() or QApplication([])

from src.ui.chat_widget import ChatWidget        # noqa: E402
from src.ui.main_window import (                 # noqa: E402
    MainWindow, _history_local_image_path, _history_local_images,
    _render_history_message)

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   {detail}" if detail else ""))


# ══════════ 0. 临时图片：名字里故意带**空格 + 中文**（真机路径就长这样） ══════════
# 注意：必须落在**仓库内**（`BASE / "_test_tmp_…"`）—— 别的套件也是这么做的：
#   平台的临时目录在沙箱里不一定可写（真机跑过一次 Permission denied）。
_tmp = BASE / "_test_tmp_imgreload193"
shutil.rmtree(_tmp, ignore_errors=True)
_tmp.mkdir(parents=True, exist_ok=True)
_img = _tmp / "图 片 a.png"
try:
    from PIL import Image as _PILImage
    _PILImage.new("RGB", (60, 40), (20, 120, 200)).save(_img)
except Exception as _e:                                  # pragma: no cover
    print("！！造临时图片失败:", _e)
    sys.exit(1)
_URI = _img.as_uri()
print("临时图片:", _URI)

P = _history_local_image_path

print("\n== A. 路径解析（file:/// + %XX + 尖括号 + 空格） ==")
check("★`file:///D:/a%20b.png` → `D:\\a b.png`（去掉 file:/// 且 unquote 了）",
      P("file:///D:/a%20b.png") == Path(r"D:\a b.png"),
      str(P("file:///D:/a%20b.png")))
check("★中文百分号编码能还原（真机那条就是 %E6%99%AE…）",
      P("file:///D:/%E5%9B%BE%20%E7%89%87/a.png") == Path(r"D:\图 片\a.png"),
      str(P("file:///D:/%E5%9B%BE%20%E7%89%87/a.png")))
check("★尖括号形式 `![](<…>)` 认得出（并且路径里的空格保留）",
      P("<file:///D:/a%20b.png>") == Path(r"D:\a b.png"),
      str(P("<file:///D:/a%20b.png>")))
check("★反斜杠 Windows 路径也认",
      P(r"D:\a b.png") == Path(r"D:\a b.png"), str(P(r"D:\a b.png")))
check("★本身就是编码过的中文路径也能还原",
      P("%E5%9B%BE%E7%89%87.png") == (BASE / "图片.png").resolve(),
      str(P("%E5%9B%BE%E7%89%87.png")))
check("★真实文件 as_uri() 能解析回同一个文件",
      P(_URI) == _img.resolve(), str(P(_URI)))
check("远程 / 内联 data: 一律不当本地图片（保持原样由文本渲染）",
      P("http://x/y.png") is None and P("data:image/png;base64,AA") is None
      and P("https://x/y.png") is None)
check("★解析失败/空值不抛异常（返回 None）",
      P("") is None and P(None) is None and P("   ") is None
      and P("file:") is None)

# ══════════ 1. 假 chat 桩：记录所有"画了什么、按什么顺序" ══════════
class _RecChat(ChatWidget):
    """真 ChatWidget + 记录调用（子类只加记录，不改行为）。"""

    def __init__(self):
        super().__init__()
        self.events = []

    def _append_message(self, role, text, speaker=""):
        self.events.append(("text", role, text, speaker))
        return super()._append_message(role, text, speaker)

    def append_image_preview(self, file_path, from_bot=False):
        self.events.append(("img", str(file_path), bool(from_bot)))
        return super().append_image_preview(file_path, from_bot=from_bot)


class _StubMW(MainWindow):
    """不跑真 `MainWindow.__init__`（那会拉起整个应用）；只借它的方法。"""

    def __init__(self):
        pass


def _fake_mw(content_msgs, archived_msgs=None, rounds=5):
    mw = _StubMW()
    mw.chat = _RecChat()
    mw._chat_load_rounds = rounds
    mw._chat_scroll_hooked = True          # 不接滚动信号，免得干扰
    mw._chat_archived_rendered = False

    class _Sess:
        def __init__(self):
            self.topic_id = 80
            self.character = "铃兰"
            self.messages = content_msgs
            self._lock = threading.Lock()

    class _Db:
        def list_character_topics(self, character, limit=50):
            return ([{"id": 1, "status": "archived"}] if archived_msgs else [])

        def get_topic_messages(self, tid):
            return list(archived_msgs or [])

    mw.session = _Sess()
    mw.db = _Db()
    return mw


print("\n== B. 重画那条路：本地图片真的走 append_image_preview ==")
_cur = [
    {"role": "user", "content": "这是一句很普通的话"},
    {"role": "assistant", "speaker": "铃兰",
     "content": f"图前\n![]({_URI})\n图后"},
    {"role": "user", "content": f"![我发的]({_URI})"},
    {"role": "assistant", "speaker": "铃兰", "content": "好的"},
    # ★ 这一条是"图没了"的现场：文件不在了 → 必须退回文本渲染（不画破图）
    {"role": "assistant", "speaker": "铃兰",
     "content": "![](file:///D:/no/such%20file.png)\n缺图"},
]
_arch = [{"role": "user", "content": "归档段用户的话"},
         {"role": "assistant", "speaker": "铃兰", "content": f"![]({_URI})"}]
_mw = _fake_mw(_cur, _arch)
MainWindow._reload_chat_view(_mw)          # ★ 真跑重画路径
_ev = _mw.chat.events
_imgs = [e for e in _ev if e[0] == "img"]

check("★★重画路径确实调了 append_image_preview（归档段 + 当前话题都有）",
      len(_imgs) == 3, f"{len(_imgs)} 次: {[e[1][-12:] for e in _imgs]}")
check("★图片路径就是解析出来的那个本地文件（没有 %XX、没有 file:///）",
      all(Path(e[1]) == _img.resolve() for e in _imgs),
      str([e[1] for e in _imgs]))
check("★assistant 的图 from_bot=True（左侧桌宠侧），user 的图 from_bot=False",
      [e[2] for e in _imgs] == [True, True, False],
      str([e[2] for e in _imgs]))
_leaked = [e[2] for e in _ev if e[0] == "text" and "![" in e[2]]
check("★画出来的那几张图的标记不会再漏进文本渲染（否则同一张图渲染两遍）",
      len(_leaked) == 1 and "no/such" in _leaked[0],
      str(_leaked))
_texts = [e[2] for e in _ev if e[0] == "text"]
check("★图前/图后的文字都还在（按老样子走 _append_message）",
      "图前" in _texts and "图后" in _texts
      and "这是一句很普通的话" in _texts, str(_texts))
_i_before = _ev.index(("text", "assistant", "图前", "铃兰"))
check("★顺序保持：图前 → 图 → 图后（不是\"先全画图再全写文字\"）",
      _ev[_i_before + 1][0] == "img"
      and _ev[_i_before + 2] == ("text", "assistant", "图后", "铃兰"),
      str(_ev[_i_before:_i_before + 3]))
check("★文件不存在的那条：没画图，整条正文（含标记）按文本渲染",
      any(e[0] == "text" and "no/such%20file.png" in e[2] for e in _ev)
      and not any(e[0] == "img" and "no" in e[1].replace("\\", "/") for e in _ev),
      str([e for e in _ev if e[0] == "text" and "no" in e[2].replace("\\", "/")]))
check("★speaker 仍然传给了文本渲染（头像按说话者取，没弄回退）",
      all(e[3] == "铃兰" for e in _ev
          if e[0] == "text" and e[1] == "assistant"),
      str([e for e in _ev if e[0] == "text" and e[1] == "assistant"]))


def _doc_image_count(widget):
    doc = widget._chat_display.document()
    n, blk = 0, doc.begin()
    while blk.isValid():
        it = blk.begin()
        while not it.atEnd():
            fr = it.fragment()
            if fr.isValid() and fr.charFormat().isImageFormat():
                n += 1
            it += 1
        blk = blk.next()
    return n


check("★★文档里真的出现了图片（不是只调了方法）",
      _doc_image_count(_mw.chat) >= 3, str(_doc_image_count(_mw.chat)))
_plain = _mw.chat._chat_display.toPlainText()
check("★普通文本消息照旧显示", "这是一句很普通的话" in _plain and "好的" in _plain)

print("\n== C. 多图 / 图夹在文字中间（识别层） ==")
_multi = f"开头\n![]({_URI})\n中间\n![alt]({_URI})\n结尾"
_found = _history_local_images(_multi)
check("★一条消息里**多张图**都认出来（2 张，按出现顺序）",
      len(_found) == 2 and _found[0][0] < _found[1][0],
      str([(s, e) for s, e, _ in _found]))
check("★`<img src=...>` 形式也认",
      len(_history_local_images(f'<img src="{_URI}" width="10">')) == 1)
check("★没有图片的普通文本 → 空（一个字都不动）",
      _history_local_images("就是一句普通的话") == []
      and _history_local_images("") == [])

print("\n== D. 文件不存在 / 解析失败：不抛异常、退回文本渲染、留 _log ==")
_records = []


class _Handler(logging.Handler):
    def emit(self, record):
        try:
            _records.append(record.getMessage())
        except Exception:
            pass


_lg = logging.getLogger("MainWindow")
_h = _Handler()
_lg.addHandler(_h)
_old_level = _lg.level
_lg.setLevel(logging.DEBUG)

_fake = _StubMW()
_fake.chat = _RecChat()
_ok_missing = _render_history_message(
    _fake.chat, "assistant", "![](file:///D:/no/such%20file.png)\n后面的话", "铃兰")
check("★文件不存在的图片消息：**不抛异常**，helper 一个字都不画（交给调用方按文本）",
      _ok_missing is False and _fake.chat.events == [],
      str(_fake.chat.events))
check("★★文件不存在时留了一句 _log（用户以后能查为什么图没了）",
      any("不存在" in m and "no" in m.replace("\\", "/") for m in _records),
      str(_records[-3:]))
_ok_weird = _render_history_message(
    _fake.chat, "assistant", "![](不是路径) 后面的话", "铃兰")
check("解析不出来的标记也不抛异常", _ok_weird is False)
_lg.removeHandler(_h)
_lg.setLevel(_old_level)

print("\n== E. 普通文本消息的行为完全不变 ==")
_fake2 = _StubMW()
_fake2.chat = _RecChat()
_ret = _render_history_message(_fake2.chat, "user", "普通用户消息", "")
check("★非图片 → helper 返回 False（调用方按老路渲染）", _ret is False)
check("★helper 自己一个字都没画", _fake2.chat.events == [])
_fake3 = _StubMW()
_fake3.chat = _RecChat()
check("★整条就是一张图 → 只画图（不要多余的空文本气泡）",
      _render_history_message(_fake3.chat, "assistant", f"![]({_URI})",
                              "铃兰") is True
      and [e[0] for e in _fake3.chat.events] == ["img"],
      str(_fake3.chat.events))

print("\n== F. 「往上插历史」遇到含图片的一轮 → 回退整段重画（不静默丢图） ==")
_pre = _StubMW()
_pre.chat = _RecChat()
_pre.chat.prepend_messages = lambda items: 99        # 真被调用就说明没回退
_pre._chat_archived_rendered = False
_pre._chat_load_rounds = 1
_older = [{"role": "user", "content": "更早的用户话"},
          {"role": "assistant", "speaker": "铃兰", "content": f"![]({_URI})"},
          {"role": "user", "content": "现在的话"}]
# 2 轮 → 从头（含图那一轮）；1 轮 → 从下标 2 起 → older = keep[0:2] = 含图那一段
_pre._current_topic_tail = lambda n: (_older, 0 if n >= 2 else 2)
check("★★含本地图片的那一轮 → prepend 直接放弃（返回 False，交给整段重画）",
      MainWindow._try_prepend_history_round(_pre) is False)

_pre2 = _StubMW()
_pre2.chat = _RecChat()
_pre2.chat.prepend_messages = lambda items: 99
_pre2._chat_archived_rendered = False
_pre2._chat_load_rounds = 1
_pre2._save_chat_rounds = lambda n: None             # 桩对象没有 config
_older2 = [{"role": "user", "content": "更早的用户话"},
           {"role": "assistant", "speaker": "铃兰", "content": "纯文字，没有图"},
           {"role": "user", "content": "现在的话"}]
_pre2._current_topic_tail = lambda n: (_older2, 0 if n >= 2 else 2)
check("★纯文字那一轮照旧能 prepend（没有误伤原逻辑）",
      MainWindow._try_prepend_history_round(_pre2) is True)

print("\n== G. 静态：两个调用点还在、而且带 speaker（防以后被改回去） ==")
_mw_src = (BASE / "src" / "ui" / "main_window.py").read_text(encoding="utf-8")
_tree = ast.parse(_mw_src)


def _find(tree, name):
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
            return n
    return None


def _calls(func, name):
    return [n for n in ast.walk(func)
            if isinstance(n, ast.Call) and (
                (isinstance(n.func, ast.Name) and n.func.id == name)
                or (isinstance(n.func, ast.Attribute) and n.func.attr == name))]


_reload = _find(_tree, "_reload_chat_view")
check("★`_reload_chat_view` 里归档段 + 当前话题 + 排队附件都先问过 helper",
      # ★ 1.9.3 第 53 轮：原来 2 处（归档段 / 当前话题）。
      #   新增第 3 处 = **本轮还在排队、尚未补进 session.messages 的附件**也要补画 ——
      #   那正是真机反馈"有一次少了一张图"的真机制：`_persist_attachment` 是
      #   先落 DB、等本轮结束才补内存，而重画读的是内存 → 一轮进行中的重画会漏掉它。
      len(_calls(_reload, "_render_history_message")) == 3,
      f"{len(_calls(_reload, '_render_history_message'))} 处")
check("★prepend 那条路也先查了本地图片（查不到就整段重画）",
      len(_calls(_find(_tree, "_try_prepend_history_round"),
                 "_history_local_images")) == 1)
_assist = [n for n in ast.walk(_reload)
           if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
           and n.func.attr == "_append_message"
           and n.args and isinstance(n.args[0], ast.Constant)
           and n.args[0].value == "assistant"]
check("★★assistant 的 `_append_message` 都带说话者（第 3 个参数不是空字符串常量）",
      # 判据从"字面含 speaker"放宽成"**确实传了非空表达式**"：
      # 归档段/当前话题传的是 `m.get("speaker") or ""`，
      # 第 53 轮新增的排队附件那条传的是队列里存下来的说话者变量。
      # 意图不变 —— 每条 assistant 渲染都必须交代"是谁说的"。
      len(_assist) == 3
      and all(len(n.args) >= 3
              and ast.unparse(n.args[2]).strip() not in ('""', "''")
              for n in _assist),
      f"{len(_assist)} 处")
check("★★helper 每次都用 `role` 变量调 `_append_message`（不走写死的 assistant）",
      all(n.args and not isinstance(n.args[0], ast.Constant)
          for n in _calls(_find(_tree, "_render_history_message"),
                          "_append_message")))

shutil.rmtree(_tmp, ignore_errors=True)

print(f"\n==== 结果: {len(PASS)} PASS / {len(FAIL)} FAIL ====")
for _f in FAIL:
    print("  FAIL:", _f)
sys.exit(1 if FAIL else 0)
