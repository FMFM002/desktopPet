"""直接验证 PetProxy 直接渲染模式的队列投递（模拟子进程不可用）"""
import os, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication
app = QApplication(sys.argv)

import src.ui.pet_proxy as pp
pp._DIRECT_RENDER = True

proxy = pp.PetProxy()
app.processEvents()
print("direct_widget:", proxy._direct_widget is not None)

# 用真实的启动 GIF 测试
base = Path(__file__).resolve().parents[2]
startup = base / "data" / "桌宠形象" / "deepseek" / "GIF" / "启动.gif"
print("startup exists:", startup.exists())

proxy.set_character("deepseek")
proxy.set_scale(1.02)
proxy.set_gif_file(str(startup), once=True)
for _ in range(30):
    app.processEvents()
    time.sleep(0.01)

w = proxy._direct_widget
print("behavior:", w._current_behavior)
print("loop_gif(应为False):", w._loop_gif, "| frames:", w._gif_frames)
print("scale ok (size 128*1.02=130):", w.width(), w.height())
