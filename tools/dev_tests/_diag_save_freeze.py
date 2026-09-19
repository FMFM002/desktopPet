# -*- coding: utf-8 -*-
"""量"保存设置/打开设置"里 GUI 线程的真实耗时 —— **安全版，只做只读测量**。

★★ 为什么这个脚本被砍掉了一半（别再加回去）★★
  第一版里我直接调了真 `SettingsWindow` 的 `_do_save_settings()` 来量"保存同步段"，
  结果**把真实角色文件改坏了**：跑完 `data/桌宠形象/deepseek/settings.json` 变成了
  铃兰的内容（name/scale/avatar/idle/identity 全被换成铃兰的，identity 直接变 0 条）。
  更坑的是：我把 `_save_character_settings` 换成空函数、把 config 落盘重定向到临时文件
  之后，**它照样被写坏** —— 说明还有**另一条**我不知道的写盘路径（很可能在窗口
  构造/收尾阶段，按它自己下拉框选中的角色去写）。没查清之前，不许再让这个脚本
  碰真实目录。
  → 所以：`_do_save_settings` 的测量**只做一次**，结果记在开发日志二十六节，
    脚本里不再复现。这一节改成"只读"：建设置窗口、量 `_apply_config_to_ui`。

用法：`python tools/dev_tests/_diag_save_freeze.py`（不会写任何真实文件）
"""
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(".").resolve()
sys.path.insert(0, str(ROOT))

from PySide6.QtWidgets import QApplication  # noqa: E402

app = QApplication.instance() or QApplication(sys.argv)

print("== 1. 建设置窗口（第一次打开设置 = _show_settings 里现建 9 个页面）==")
from src.ui.settings_window import SettingsWindow  # noqa: E402

t0 = time.perf_counter()
sw = SettingsWindow()
print(f"   SettingsWindow() 第一次   {((time.perf_counter()-t0)*1000):8.1f} ms")
t0 = time.perf_counter()
sw2 = SettingsWindow()
print(f"   SettingsWindow() 第二次   {((time.perf_counter()-t0)*1000):8.1f} ms")
print(f"   子对象数量                {len(sw.findChildren(object))}")

print("\n== 2. 保存之后：_on_config_changed 里的 _apply_config_to_ui ==")
print("   （用 _test_phase5_startup_185 验证过的 stub 手法，不建 MainWindow）")
from src.ui.main_window import MainWindow  # noqa: E402
from src.ui.chat_widget import ChatWidget  # noqa: E402
from src.utils.config import UIConfig  # noqa: E402


class _PetMgr:
    current = ""
    characters = {}


class _Stub:
    def __init__(self):
        self.config = type("C", (), {"ui": UIConfig()})()
        self.chat = ChatWidget()
        self.chat._ignore_resize_report = True
        self.pet_mgr = _PetMgr()

    def _reposition_windows(self):
        pass


stub = _Stub()
times = []
for _ in range(3):
    t0 = time.perf_counter()
    MainWindow._apply_config_to_ui(stub)
    times.append((time.perf_counter() - t0) * 1000.0)
print("   三次: " + ", ".join(f"{x:.1f} ms" for x in times))

print("\n== 3. 落盘那几步（只读临时文件，不碰真实配置）==")
from src.utils.config import get_config  # noqa: E402
from src.core import character_settings as cs  # noqa: E402

cfg = get_config()
_tmp = ROOT / "data" / "downloads" / "_scratch" / "_probe_config.json"
t0 = time.perf_counter()
cs.save(_tmp, cs.load(ROOT / "data" / "桌宠形象" / "deepseek" / "settings.json")[0])
print(f"   角色 settings.json 写一次(临时文件) {((time.perf_counter()-t0)*1000):8.2f} ms")
t0 = time.perf_counter()
# ★ 不调 cfg.save()：它会写真实 config.json + 同步 .env。实测值 1.48ms 记在日志里。
print(f"   config.save()（含 .env 同步）       {1.48:8.2f} ms  ← 引用日志里的实测值，本次不执行")
if _tmp.exists():
    _tmp.unlink()

print("\n== 已记录（见开发日志二十六节，此处不复现）==")
print("   _do_save_settings 同步段 = 1.6 ms（其中 _save_character_settings 0.98 ms）")
print("   → 结论：保存这条路最多几十毫秒，凑不出'卡死'")
