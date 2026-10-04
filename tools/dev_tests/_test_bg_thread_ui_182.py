# -*- coding: utf-8 -*-
"""★ 1.8.2 回归：后台线程回主线程的 UI 回调。

根因证明：Python threading.Thread 里没有 Qt 事件循环，
QTimer.singleShot() 创建的定时器永远不会触发 →
「上传 pptx 后卡片出来了，但内容永远送不到对话」。

修复：用 QObject + Signal（跨线程 queued connection）。

用法: python tools/dev_tests/_test_bg_thread_ui_182.py
"""
import os
import re
import sys
import threading
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))   # ★ 第 40 轮：import _fixtures_193

from PySide6.QtCore import QObject, QTimer, Signal  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

app = QApplication.instance() or QApplication(sys.argv)
MAIN_TID = threading.get_ident()

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   {detail}" if detail else ""))


def pump(seconds: float):
    """跑 Qt 事件循环 seconds 秒（不阻塞 pending 的 queued 回调）"""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.01)


# ── 1. 证明旧写法在后台线程里不触发 ──
print("== 1. 复现根因：后台线程里的 QTimer.singleShot 不触发 ==")
old_fired = []


def _worker_old():
    QTimer.singleShot(0, lambda: old_fired.append(threading.get_ident()))
    QTimer.singleShot(50, lambda: old_fired.append(threading.get_ident()))


t = threading.Thread(target=_worker_old, daemon=True)
t.start()
t.join()
pump(0.6)
check("QTimer.singleShot（后台线程）确实不触发 —— 这就是老 bug",
      not old_fired, f"fired={old_fired}")

# ── 2. Signal 中转器一定回到主线程 ──
print("\n== 2. _TaskSignalRelay：跨线程 Signal 回主线程 ==")
from src.ui.main_window import _TaskSignalRelay  # noqa: E402

relay = _TaskSignalRelay()
got = {"sys": [], "user": []}


class _FakeWindow:
    """刻意做成「非 QObject 的普通类」，与 MainWindow 完全同形，
    验证槽函数挂在普通对象上也一定回到主线程。"""

    def __init__(self):
        self.main_tid = MAIN_TID

    def _on_task_system_msg(self, text):
        got["sys"].append((text, threading.get_ident()))

    def _on_task_user_msg(self, text):
        got["user"].append((text, threading.get_ident()))


win = _FakeWindow()
relay.system_msg.connect(win._on_task_system_msg)
relay.user_msg.connect(win._on_task_user_msg)

worker_tid = []


def _worker_new():
    worker_tid.append(threading.get_ident())
    relay.system_msg.emit("[文档读取完成] 探针.pptx (123 字符)")
    relay.user_msg.emit("[用户上传了文档 探针.pptx]\n内容如下:\n你好世界")


t = threading.Thread(target=_worker_new, daemon=True)
t.start()
t.join()
pump(0.6)

check("system_msg 已送达", len(got["sys"]) == 1, str(got["sys"]))
check("user_msg 已送达", len(got["user"]) == 1, str(got["user"]))
check("回调确实跑在主线程",
      got["sys"] and got["sys"][0][1] == MAIN_TID and got["user"][0][1] == MAIN_TID,
      f"main={MAIN_TID} worker={worker_tid} got={[g[1] for g in got['sys'] + got['user']]}")
check("回调内容与发送内容一致",
      got["user"] and "你好世界" in got["user"][0][0])

# ── 3. 源码静态检查：后台函数里不许再有 QTimer.singleShot ──
print("\n== 3. 源码静态检查 ==")
src = (ROOT / "src" / "ui" / "main_window.py").read_text(encoding="utf-8")

FUNCS = ["_transcribe_audio_file", "_analyze_video_file", "_read_document_file"]
# ★ 1.9.3 第 49 轮：原来这里还有 `_on_stt_transcription` / `_on_stt_speech_start` /
#   `_on_stt_speech_end` 三个 STT 回调。用户把 STT 改成"**音频监控的一部分**"
#   （音量过阈值 → VAD 分段 → 本地 STT → `speech` 监控事件进 raw_events，
#   **不再当用户消息发出去**）之后，这三个回调连同旧的"实时循环"通路一起删除了，
#   它们的线程安全断言随之失去被测对象。

# ★ 1.9.0: 音频/视频/文档这三个已经**统一成委托给 _view_attachment**
#   （文件查看统一走 tools/file_view.py），所以它们的函数体里不再直接出现
#   _task_relay —— 改判"委托给了统一入口"。
DELEGATED = {"_transcribe_audio_file", "_analyze_video_file", "_read_document_file"}


def body_of(name: str) -> str:
    m = re.search(rf"\n    def {re.escape(name)}\(.*?\n(?=    def |\nclass |\Z)", src, re.S)
    return m.group(0) if m else ""


for fn in FUNCS:
    b = body_of(fn)
    check(f"{fn} 已不再用 QTimer.singleShot",
          bool(b) and "QTimer.singleShot" not in b)
    if fn in DELEGATED:
        check(f"{fn} 委托给统一入口 _view_attachment（★1.9.0）",
              "_view_attachment" in b)
    else:
        check(f"{fn} 走 _task_relay", "_task_relay" in b)

check("连接了 system_msg → _on_task_system_msg",
      "self._task_relay.system_msg.connect(self._on_task_system_msg)" in src)
check("连接了 user_msg → _on_task_user_msg",
      "self._task_relay.user_msg.connect(self._on_task_user_msg)" in src)
check("handle_file_upload 有入口日志", "[上传文件]" in src)

# ── 4. 真实端到端：拿真的 pptx 跑一遍 _read_document_file ──
print("\n== 4. 真实 pptx 端到端：抽取 → 回主线程 → 拿到内容 ==")
from src.ui.chat_widget import ChatWidget  # noqa: E402
from src.ui.main_window import MainWindow  # noqa: E402


class _StubPetMgr:
    def set_behavior(self, _b):
        pass


class _StubChat:
    """★ 1.8.4: _read_document_file 现在会用 chat.file_marker 生成卡片标记"""

    @staticmethod
    def file_marker(path, size="", label=""):
        return ChatWidget.file_marker(path, size, label)


class _StubWindow:
    """只提供 _read_document_file 需要的几个成员

    ★ 1.9.0: _read_document_file 现在是**委托**给 _view_attachment，
    桩对象也得把这个统一入口一起带上（真实应用里 self 就是 MainWindow）。
    """

    _view_attachment = MainWindow._view_attachment

    def __init__(self):
        self._task_relay = _TaskSignalRelay()
        self.pet_mgr = _StubPetMgr()
        self.chat = _StubChat()
        self.sys_msgs, self.user_msgs = [], []
        self._task_relay.system_msg.connect(
            lambda t: self.sys_msgs.append((t, threading.get_ident())))
        self._task_relay.user_msg.connect(
            lambda t: self.user_msgs.append((t, threading.get_ident())))


# ★ 1.9.3 第 40 轮：**自造 fixture**（原来依赖用户地盘里的 deepseek_intro.pptx，
#   2026-10-04 被清理后这条断言直接红）。本套件只需要"某个真文档"来走过读取链路。
from _fixtures_193 import fixture_dir, make_pptx          # noqa: E402
pptx = make_pptx(fixture_dir("bg_thread_ui_182") / "sample.pptx",
                 pages=2, title="后台线程读取测试")
check("测试用 pptx 存在（自己造的，不依赖用户文件）", pptx.exists(), str(pptx))

if pptx.exists():
    win = _StubWindow()
    worker_ids = []

    def _run():
        worker_ids.append(threading.get_ident())
        MainWindow._read_document_file(win, str(pptx))

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join()
    pump(0.8)

    check("文档抽取发出了 system_msg", len(win.sys_msgs) == 1, str(win.sys_msgs))
    check("文档抽取发出了 user_msg（内容送进对话）", len(win.user_msgs) == 1,
          f"{len(win.user_msgs)} 条")
    if win.user_msgs:
        text, tid = win.user_msgs[0]
        print(f"  注入对话的正文长度: {len(text)} 字符")
        print(f"  前 120 字: {text[:120]!r}")
        check("回调跑在主线程", tid == MAIN_TID, f"got={tid} main={MAIN_TID}")
        check("消息里带绝对路径", str(pptx.resolve()) in text)
        check("确实是文档内容（非空）", len(text) > 200, f"{len(text)} 字符")
    check("内容来自后台线程", worker_ids and worker_ids[0] != MAIN_TID)

print(f"\n==== 结果: {len(PASS)} PASS / {len(FAIL)} FAIL ====")
for f in FAIL:
    print("  FAIL:", f)
sys.exit(1 if FAIL else 0)
