# -*- coding: utf-8 -*-
"""量"保存设置时界面卡死"到底卡在哪一步（用户 09-19 批注：无法关闭设置界面）。

不需要造 MainWindow：
  · 保存那条路 → 真的建一个 SettingsWindow（offscreen），直接调它的
    `_do_save_settings`（这就是**同步跑在 GUI 线程**的那一段）。
  · 保存之后那条路 → 用 _test_phase5_startup_185 里验证过的 stub 手法，
    调 `MainWindow._apply_config_to_ui(stub)`（另一个 GUI 线程嫌疑犯）。
脚本会把 config.json / .env / 角色 settings.json 先备份再还原（这些流程会真落盘）。
"""
import json
import os
import shutil
import sys
import threading
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(".").resolve()
sys.path.insert(0, str(ROOT))
SCRATCH = ROOT / "data" / "downloads" / "_scratch"
SCRATCH.mkdir(parents=True, exist_ok=True)

# ── 0. 先备份会被真写的东西 ────────────────────────────────────────────
BAK = SCRATCH / "_freeze_bak"
if BAK.exists():
    shutil.rmtree(BAK)
BAK.mkdir(parents=True)
TARGETS = [ROOT / "config.json", ROOT / ".env"]
for d in (ROOT / "data" / "桌宠形象").iterdir():
    if d.is_dir() and (d / "settings.json").exists():
        TARGETS.append(d / "settings.json")
for f in TARGETS:
    if f.exists():
        shutil.copy2(f, BAK / (f.name if f.parent.name != "桌宠形象" else f"{f.parent.name}__{f.name}"))
print("已备份:", ", ".join(t.name for t in TARGETS))

from PySide6.QtWidgets import QApplication  # noqa: E402

app = QApplication.instance() or QApplication(sys.argv)

TIMES: dict[str, float] = {}


def wrap(obj, name, label=None):
    """把 obj.name 包一层计时（不动源码，只在本次进程内生效）"""
    fn = getattr(obj, name)
    key = label or name

    def timed(*a, **k):
        t0 = time.perf_counter()
        r = fn(*a, **k)
        TIMES[key] = TIMES.get(key, 0.0) + (time.perf_counter() - t0) * 1000.0
        return r

    setattr(obj, name, timed)


print("\n== 1. 建设置窗口 ==")
from src.ui.settings_window import SettingsWindow  # noqa: E402

t0 = time.perf_counter()
sw = SettingsWindow()
print(f"   SettingsWindow()  {((time.perf_counter()-t0)*1000):.1f} ms")

print("\n== 2. 保存：同步跑在 GUI 线程的那一段（用户说卡死就在这附近）==")
cfg = sw._config
for m in ("_save_character_settings", "_get_tools_policy_changes",
          "_collect_disabled_actions", "_apply_media_settings"):
    if hasattr(sw, m):
        wrap(sw, m)
if getattr(sw, "_tool_manager", None) is not None:
    wrap(sw._tool_manager, "set_policy")
    wrap(sw._tool_manager, "get_policy_map")
wrap(cfg, "save", "cfg.save()（子线程里）")

t0 = time.perf_counter()
try:
    sw._do_save_settings(cfg, cfg.llm.provider)
    _sync_ms = (time.perf_counter() - t0) * 1000.0
    print(f"   ★ _do_save_settings 同步耗时: {_sync_ms:8.1f} ms   ← 这一整段在 GUI 线程上")
except Exception as e:
    _sync_ms = -1
    print("   _do_save_settings 抛错:", type(e).__name__, e)

time.sleep(1.2)   # 等它起的保存线程写完，再看明细
print("   ── 明细 ──")
for k, v in sorted(TIMES.items(), key=lambda kv: -kv[1]):
    if k == "cfg.save()（子线程里）":
        continue
    print(f"      {k:34s} {v:8.2f} ms")
print(f"      {'cfg.save()（在子线程，不计入上面的同步段）':34s} {TIMES.get('cfg.save()（子线程里）', 0):8.2f} ms")

print("\n== 3. 保存之后：_on_config_changed 里最可疑的 _apply_config_to_ui ==")
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
_t, _ = None, None
times = []
for i in range(3):
    t0 = time.perf_counter()
    MainWindow._apply_config_to_ui(stub)
    times.append((time.perf_counter() - t0) * 1000.0)
print(f"   _apply_config_to_ui(stub) 三次: " +
      ", ".join(f"{x:.1f} ms" for x in times))

print("\n== 4. 还原被写过的文件 ==")
for f in TARGETS:
    b = BAK / (f.name if f.parent.name != "桌宠形象" else f"{f.parent.name}__{f.name}")
    if b.exists():
        shutil.copy2(b, f)
print("   已还原:", ", ".join(t.name for t in TARGETS))
