# -*- coding: utf-8 -*-
"""量一下"保存设置/打开设置"里 GUI 线程那几件事的真实耗时。

只做安全的 offscreen 测量：_test_a190 里已经证明 SettingsWindow() 能这样建起来。
（MainWindow 会起桌宠子进程 + 监控线程，不适合在诊断脚本里造，所以
  _apply_config_to_ui / session.set_character / 托盘图标仍保持"推断"。）
"""
import json
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(".").resolve()
sys.path.insert(0, str(ROOT))

from PySide6.QtWidgets import QApplication  # noqa: E402

app = QApplication.instance() or QApplication(sys.argv)


def ms(fn):
    t0 = time.perf_counter()
    r = fn()
    return (time.perf_counter() - t0) * 1000.0, r


print("== 1. 首次打开设置窗口（_show_settings 里现建 9 个页面）==")
from src.ui.settings_window import SettingsWindow  # noqa: E402

_t, sw1 = ms(SettingsWindow)
print(f"   第一次 SettingsWindow()  : {_t:8.1f} ms")
_t2, sw2 = ms(SettingsWindow)
print(f"   再来一个（对照）          : {_t2:8.1f} ms")

print("\n== 2. 保存时那几件会落盘的事 ==")
from src.utils.config import get_config  # noqa: E402
from src.core import character_settings as cs  # noqa: E402

cfg = get_config()
_t, _ = ms(cfg.save)
print(f"   config.save()（含 .env 同步）: {_t:8.1f} ms   ← 注意：这一步在**子线程**里")

cfg_json = ROOT / "config.json"
_t, _ = ms(lambda: json.loads(cfg_json.read_text(encoding="utf-8")))
print(f"   读回 config.json             : {_t:8.1f} ms   ({cfg_json.stat().st_size/1024:.1f} KB)")

print("\n== 3. 一个角色 settings.json 的读写（保存设置时每个角色一次）==")
for d in sorted((ROOT / "data" / "桌宠形象").iterdir()):
    sf = d / "settings.json"
    if not d.is_dir() or not sf.exists():
        continue
    _tl, _ = ms(lambda p=sf: cs.load(p))
    _d, _ = cs.load(sf)
    tmp = ROOT / "data" / "downloads" / "_scratch" / f"_profile_{d.name}.json"
    _ts, _ = ms(lambda p=tmp, x=_d: cs.save(p, x))
    print(f"   {d.name:10s} load {_tl:6.2f} ms | save {_ts:6.2f} ms | {sf.stat().st_size/1024:.1f} KB")
    if tmp.exists():
        tmp.unlink()

print("\n== 4. 设置窗口的子对象数量（页面越多越贵？）==")
n_widgets = len(sw1.findChildren(object))
print(f"   SettingsWindow 子对象总数     : {n_widgets}")
print("\n注：_apply_config_to_ui / session.set_character / update_character_icon")
print("    需要 MainWindow，本脚本不造 → 仍属**未实测**。")
