# -*- coding: utf-8 -*-
"""★ 1.9.3 第 3 轮批注 3：聊天窗口只显示最近 300MB 正文（**只封顶、不回取**）。

口径（用户确认的简化版）：按正文 UTF-8 字节数从最新往旧累加，超过上限的更早消息
不再显示；**数据不删，也不做"往上翻再取"**。

测试做法：把 `main_window.CHAT_HISTORY_MAX_BYTES` 调小（默认 300MB），
不必真分配几百 MB 字符串。
"""
import os
import sys
import threading
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from PySide6.QtWidgets import QApplication  # noqa: E402

app = QApplication.instance() or QApplication(sys.argv)

from src.ui import main_window as mw  # noqa: E402

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   {detail}" if detail else ""))


class _Sess:
    def __init__(self, msgs):
        self.messages = msgs
        self._lock = threading.RLock()


class _Obj:
    pass


def tail(msgs, rounds=99):
    o = _Obj()
    o.session = _Sess(msgs)
    return mw.MainWindow._current_topic_tail(o, rounds)


# 把上限调成 100 字节（省内存、又足够触发封顶逻辑）
mw.CHAT_HISTORY_MAX_BYTES = 100

print("\n== 1. 内容远小于上限 → 一条都不该少 ==")
msgs = [{"role": "user", "content": f"问题{i}"} for i in range(3)]
msgs += [{"role": "assistant", "content": f"回答{i}"} for i in range(3)]
k, st = tail(msgs)
check("小消息全部显示（start=0）", st == 0 and len(k) == 6, f"start={st} n={len(k)}")

print("\n== 2. ★单条自身超上限 → 至少保留最新那条（上一版就栽在这，界面会变空）==")
big = "x" * 500
m2 = [{"role": "user", "content": "老"},
      {"role": "assistant", "content": "老答"},
      {"role": "user", "content": "新问题"},
      {"role": "assistant", "content": big}]
k2, st2 = tail(m2)
check("★单条超限时仍显示最新那条（不截断、不空白）",
      len(k2[st2:]) == 1 and k2[-1]["content"] == big,
      f"显示 {len(k2[st2:])} 条 / start={st2}")

print("\n== 3. ★多条累计超上限 → 掐掉更早的、保留最新的 ==")
m3 = [{"role": "user", "content": "a" * 60},
      {"role": "assistant", "content": "b" * 60},
      {"role": "user", "content": "最后一句"}]
k3, st3 = tail(m3)
check("★累计超限 → 更早的不显示、最新保留",
      st3 >= 1 and k3[-1]["content"] == "最后一句", f"start={st3} n={len(k3)}")
check("★没有回取机制（只是不显示：keep 里的数据一条没删）",
      len(k3) == 3, f"keep={len(k3)}")

print("\n== 4. 上限很大时（默认 300MB）不受影响 ==")
mw.CHAT_HISTORY_MAX_BYTES = 300 * 1024 * 1024
k4, st4 = tail(msgs)
check("默认上限下小历史完整显示", st4 == 0 and len(k4) == 6, f"start={st4}")

print(f"\n==== 结果: {len(PASS)} PASS / {len(FAIL)} FAIL ====")
for f in FAIL:
    print("  FAIL:", f)
sys.exit(1 if FAIL else 0)
