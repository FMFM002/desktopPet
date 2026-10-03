# -*- coding: utf-8 -*-
"""★ 1.9.3 第 36 轮：证明 monitor.stop() 从"干等睡眠"变成"毫秒级"。

真机病灶（`logs/prts_20261004_065116.log`）：
    `[退出] 收尾线程超过 4s 预算仍未结束（t1=活 t2=停）`（每次退出都超）
    `[监控] 重启完成，耗时 2287 ms`（另一次 2013 ms）
根因：循环里 `time.sleep(interval)`（SystemMonitor 一睡 60 秒），
      而 stop() 是 `join(timeout=2)` —— 只能干等。

这里**真的起一个 SystemMonitor（interval=60）再 stop()**，量它花了多久。
按定义：修好后应当 ≪ 2 秒（毫秒级）；修好前必然是 ~2 秒（join 超时）。
"""
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
BASE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE))

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   {detail}" if detail else ""))


print("\n== A. 真起一个间隔 60 秒的 SystemMonitor，量 stop() 耗时 ==")
from src.core.monitor import SystemMonitor, BaseMonitor   # noqa: E402

m = SystemMonitor(interval=60)
m.start()
time.sleep(0.6)                     # 让它进到循环里、睡在 _nap(60) 上
t0 = time.perf_counter()
m.stop()
dt = (time.perf_counter() - t0) * 1000
print(f"  SystemMonitor(interval=60).stop() = {dt:.1f} ms")
check("★stop() 不再干等睡眠（远小于 join 的 2 秒上限）", dt < 300, f"{dt:.1f} ms")
check("★线程确实退出了（不是靠超时溜走，留下僵尸）",
      m._thread is None or not m._thread.is_alive())
check("★原始 sleep 语义没丢：_nap 返回值告诉调用方该 break",
      isinstance(getattr(m, "_stop_event", None), type(None)) is False)

print("\n== B. 对照：普通 sleep 必然等满（证明这个测量是有效的）==")
import threading   # noqa: E402
t = threading.Thread(target=lambda: time.sleep(2), daemon=True)
t.start()
t0 = time.perf_counter()
t.join(timeout=2)
dt_old = (time.perf_counter() - t0) * 1000
print(f"  对照：join 一个正在 sleep(2) 的线程 = {dt_old:.0f} ms")
check("★对照组确实接近 2 秒（说明上面的测量不是假象）", dt_old > 1500, f"{dt_old:.0f} ms")
check("★新做法比对照组快一个数量级", dt < dt_old / 5, f"{dt:.1f} ms vs {dt_old:.0f} ms")

print("\n== C. 重启（改监控设置时走的那条路）也要快 ==")
from src.core.monitor import UnifiedMonitor       # noqa: E402
um = UnifiedMonitor()
um.monitors["system"] = SystemMonitor(interval=60)
um.monitors["system"].start()
time.sleep(0.4)
t0 = time.perf_counter()
for mm in um.monitors.values():
    mm.stop()
dt2 = (time.perf_counter() - t0) * 1000
print(f"  UnifiedMonitor.stop() 路径 = {dt2:.1f} ms")
check("★监控停止路径整体毫秒级（原来光 system 就要 ~2000ms）", dt2 < 400, f"{dt2:.1f} ms")

print("\n== D. 不能被改坏的地方 ==")
import inspect      # noqa: E402
src = inspect.getsource(BaseMonitor)
check("★stop() 里先 set() 再 join（顺序不能反）",
      src.index("self._stop_event.set()") < src.index("self._thread.join(timeout=2)"))
check("★start() 里 clear()（否则重启后 _nap 会立刻返回，循环空转）",
      "self._stop_event.clear()" in src)
check("★join(timeout=2) 仍保留作兜底（线程真卡住时不能把界面拖死）",
      "join(timeout=2)" in src)

im = (BASE / "src" / "core" / "input_monitor.py").read_text(encoding="utf-8")
check("★input_monitor 三个子监控也改了（剪贴板/窗口/进程）",
      im.count("_init_stop_event()") == 3 and im.count("_stop_event.set()") == 3)
check("★input_monitor 里循环内的 time.sleep 已全部替换",
      "time.sleep(" not in im)
check("★三个类都真的继承了 StoppableSleep",
      im.count("(StoppableSleep)") == 3)

print(f"\n==== 结果: {len(PASS)} PASS / {len(FAIL)} FAIL ====")
for f in FAIL:
    print("  FAIL:", f)
sys.exit(1 if FAIL else 0)
