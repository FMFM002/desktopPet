"""复现：启动 GIF 是否真的在动？一次性 GIF 播完后待机 GIF 是否在动？"""
import os, sys, time
from pathlib import Path

BASE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication
app = QApplication(sys.argv)

from src.ui.pet_animation import PetAnimationWidget

startup = BASE / "data" / "桌宠形象" / "deepseek" / "GIF" / "启动.gif"
idle = BASE / "data" / "桌宠形象" / "deepseek" / "GIF" / "吃爆米花悠闲.gif"
print("startup:", startup.exists(), "idle:", idle.exists())

w = PetAnimationWidget()
w.show()
app.processEvents()

done = []
w.gif_once_finished.connect(lambda: done.append(time.time()))


def pump(sec, label):
    """跑事件循环 sec 秒，统计 movie 帧号变化次数"""
    t0 = time.time()
    frames = set()
    last_pix = None
    changes = 0
    while time.time() - t0 < sec:
        app.processEvents()
        time.sleep(0.005)
        if w._movie is not None:
            try:
                fn = w._movie.currentFrameNumber()
            except Exception:
                fn = -1
            frames.add(fn)
            px = w._label.pixmap()
            key = px.cacheKey() if px and not px.isNull() else None
            if key != last_pix:
                changes += 1
                last_pix = key
    print(f"  [{label}] 不同帧号={len(frames)} label重绘次数={changes} "
          f"state={w._movie.state() if w._movie else None} "
          f"loop_gif={w._loop_gif} behavior={w._current_behavior}")
    return changes


print("== 播放启动 GIF（once=True）==")
w.set_gif_file(str(startup), once=True)
app.processEvents()
print("  frames=", w._gif_frames, "loop_gif=", w._loop_gif)
c1 = pump(3.0, "启动GIF 3s")
print("  gif_once_finished 次数:", len(done))

print("== 切换到待机 GIF（once=False）==")
w.set_gif_file(str(idle), once=False)
app.processEvents()
print("  frames=", w._gif_frames, "loop_gif=", w._loop_gif)
c2 = pump(3.0, "待机GIF 3s")
print("  gif_once_finished 次数:", len(done))

print()
print("结论: 启动帧变化", c1, "| 待机帧变化", c2)
