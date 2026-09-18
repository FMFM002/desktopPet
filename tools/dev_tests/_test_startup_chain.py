"""端到端验证：PetManager + PetProxy(直接渲染) 的「启动 GIF 只播一遍 → 待机」链路"""
import os, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication
app = QApplication(sys.argv)

import src.ui.pet_proxy as pp
pp._DIRECT_RENDER = True

from src.core.pet_manager import PetManager

ok = True
def check(name, cond, extra=""):
    global ok
    print(("  [PASS] " if cond else "  [FAIL] ") + name + (f"  {extra}" if extra else ""))
    if not cond:
        ok = False

proxy = pp.PetProxy()
mgr = PetManager(proxy)
mgr._do_scan(None)

fired = {"n": 0}
proxy.gif_once_finished.connect(lambda: fired.__setitem__("n", fired["n"] + 1))
proxy.gif_once_finished.connect(mgr.on_startup_gif_finished)

mgr._do_switch_character(("deepseek", False))
# 跑事件循环，等启动 GIF 播完（104 帧）
deadline = time.time() + 25
while time.time() < deadline:
    app.processEvents()
    time.sleep(0.01)
    w = proxy._direct_widget
    if fired["n"] > 0:
        break

check("gif_once_finished 事件已回传", fired["n"] == 1, f"n={fired['n']}")
# 给形象线程 + 事件队列一点时间把「切待机 GIF」命令送到底
deadline = time.time() + 4
while time.time() < deadline:
    app.processEvents()
    time.sleep(0.01)
w = proxy._direct_widget
check("启动 GIF 以一次性方式加载(loop_gif=False 曾被设置)",
      w._gif_frames > 0, f"frames={w._gif_frames}")
check("播完后已切到待机 GIF", w._current_behavior in
      ("吃爆米花悠闲", "正在使用记事本", "点头悠闲", "送你一朵小花"),
      f"behavior={w._current_behavior}")
check("待机轮播定时器在跑", mgr._idle_timer is not None)
check("启动等待标记已清除", mgr._startup_pending is False)

# 再跑一会确认不会重复触发
t0 = fired["n"]
deadline = time.time() + 3
while time.time() < deadline:
    app.processEvents()
    time.sleep(0.01)
check("不会重复触发一次性完成事件", fired["n"] == t0, f"n={fired['n']}")

mgr._stop_idle_timer()
mgr._cancel_startup_fallback()
print("\n结果:", "ALL PASS" if ok else "FAILED")
sys.exit(0 if ok else 1)
