# -*- coding: utf-8 -*-
"""★ 1.9.3 第 51 轮：**锁死锁守卫**（真机上栽过，这条本该拦住它）

## 事故
2026-10-04 15:57:46，用户发了一条消息「？」，主窗口随即变"(未响应)"，
35 秒后被强杀（`logs/prts_20261004_155636.log` 里"最差 35250ms"的帧间隔
就是这段冻结），期间 LLM 一个字都没吐。

根因：`_build_llm_messages()` 内部无条件 `with self._lock:`，
而它的两个调用点（`_run_loop`）**已经持有同一把锁**；
`self._lock` 是**不可重入**的 `threading.Lock`（不是 RLock）
→ **自己等自己，永久卡死**。

## 为什么当时所有套件都是绿的
**没有任何套件真的走完"发消息 → _run_loop → 组上下文"这条路径** ——
大家都停在装配函数或 mock 掉流式循环。**绿灯掩盖了死锁。**

## 本套件两道闸
  A. **运行时**：确认"持锁 + 传快照"（生产形态）与"不持锁 + 不传快照"
     （对外契约）都能返回；并用一把**独立**的锁反证
     "不可重入锁里二次加锁确实会卡死"，让 B 段的静态检查有的放矢。
  B. **静态防回归（真正拦住原 bug 的那道）**：扫 `session.py`，
     任何 `with self._lock:` 块里若**裸调** `_build_llm_messages()`（不传快照），
     直接 FAIL —— 那正是当初的死锁形状。
     ★ 为什么必须靠静态检查：这个形状**要跑起来才会卡死**，而"跑起来会卡死"
       没法用"跑一下看看"来验证（那就是把测试进程挂住）。
"""
import ast
import os
import re
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


def make_session():
    """不碰 DB/配置，造一个够用的 ChatSession（只用到 _lock/messages/_character/_topic_id）。"""
    from src.core.session import ChatSession
    s = object.__new__(ChatSession)
    s._lock = threading.Lock()
    s.messages = [
        {"role": "user", "content": "你喜欢吃什么？"},
        {"role": "assistant", "content": "米饭。", "speaker": "甲"},
        {"role": "user", "content": "你呢？"},
    ]
    s._character = "乙"
    s._topic_id = None          # 不走 DB 摘要分支
    s.db = None
    return s


def run_with_watchdog(fn, timeout=5.0):
    """子线程里跑 fn；返回 (是否按时完成, 结果或异常描述)。卡死也不拖住进程。"""
    done, box = threading.Event(), {}

    def _w():
        try:
            box["r"] = fn()
        except Exception as e:                 # noqa: BLE001
            box["exc"] = f"{type(e).__name__}: {e}"
        finally:
            done.set()

    threading.Thread(target=_w, daemon=True).start()
    if not done.wait(timeout):
        return False, "超时未返回（死锁）"
    return True, box.get("exc", box.get("r"))


print("== A. 运行时：两种调用形态都要能用 ==")

# 前提：这台锁必须真的不可重入，否则本套件的立意就不成立
_lk = threading.Lock()
_lk.acquire()
_reentrant = _lk.acquire(blocking=False)
if _reentrant:
    _lk.release()
_lk.release()
check("★前提：threading.Lock 是不可重入的（旧代码死锁的根因）", _reentrant is False)


def _call_locked(sess):
    """生产形态：调用方已持锁，把快照传进去。"""
    with sess._lock:
        return sess._build_llm_messages([dict(m) for m in sess.messages])


_ok, _r = run_with_watchdog(lambda: _call_locked(make_session()))
check("★★已持锁 + 传快照（生产在用的形态）能返回",
      _ok and isinstance(_r, list), _r if not _ok else f"{len(_r)} 条")

_ok2, _r2 = run_with_watchdog(lambda: make_session()._build_llm_messages())
check("★不持锁 + 不传快照能自己加锁完成（对外契约不变）",
      _ok2 and isinstance(_r2, list), _r2 if not _ok2 else f"{len(_r2)} 条")

# 反证：不可重入锁里二次加锁确实卡死。★ 用**独立**的锁演示，
#      绝不去碰 session 的真锁（否则会真把后续检查卡住）。
_demo = threading.Lock()


def _demo_reentrant(lk):
    with lk:
        with lk:
            return "不该到这儿"


_dok, _dr = run_with_watchdog(lambda: _demo_reentrant(_demo), timeout=1.0)
check("★反证：不可重入锁里二次加锁确实会卡死（所以 B 段的静态检查是必须的）",
      not _dok, _dr)

print("\n== B. 静态防回归：with self._lock 里不许裸调它（= 原死锁形状）==")
src = (ROOT / "src" / "core" / "session.py").read_text(encoding="utf-8")
tree = ast.parse(src)
lines = src.splitlines()

bad = []
for node in ast.walk(tree):
    if not isinstance(node, ast.With):
        continue
    if not any("self._lock" in ast.unparse(it.context_expr) for it in node.items):
        continue
    seg = "\n".join(lines[node.lineno - 1:node.end_lineno])
    for mm in re.finditer(r"_build_llm_messages\(([^)]*)\)", seg):
        if not mm.group(1).strip():            # 空参 = 它自己会加锁 = 死锁
            bad.append(f"session.py 第 {node.lineno} 行的 with self._lock 里裸调了它")
check("★★没有「持锁 + 无参调用 _build_llm_messages」的死锁形状",
      not bad, "; ".join(bad))

check("★_build_llm_messages 支持 snapshot 形参（修法在位）",
      "def _build_llm_messages(self, snapshot" in src)
_n = src.count("_build_llm_messages([dict(m) for m in self.messages])")
check("★两个调用点都传了快照", _n == 2, f"{_n} 处")

print(f"\n==== 结果: {len(PASS)} PASS / {len(FAIL)} FAIL ====")
for f in FAIL:
    print("  FAIL:", f)
sys.exit(1 if FAIL else 0)
