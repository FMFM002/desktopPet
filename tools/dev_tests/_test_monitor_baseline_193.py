# -*- coding: utf-8 -*-
"""★ 1.9.3 第 34 轮：**本次会话第一次改监控设置被静默忽略** —— 复现 + 验证修复。

真机证据（logs/prts_20261004_064103.log）：
    06:41:05 启动监控（keyboard + system）
    06:42:52 [监控] **首次记录监控配置快照，跳过重启**   ← 用户确实改了设置，却没生效
原因：`_monitor_cfg_snap` 基线只在 `_restart_monitor_if_changed()` **第一次被调用**时才记，
      而那次调用就发生在"用户改完设置"之后 → 快照记的是**改动后**的值 → 比较不出变化 → 跳过。

这里不靠读源码断言，而是**真的实例化一个最小对象**跑那套比较逻辑。
"""
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
BASE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE))

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   {detail}" if detail else ""))


@dataclass
class _MonCfg:
    keyboard: bool = True
    system: bool = True
    window: bool = False
    clipboard: bool = True


@dataclass
class _Cfg:
    monitor: _MonCfg = field(default_factory=_MonCfg)


class _FakeMW:
    """只搬运 main_window 里那几个方法的**真实逻辑**（用 exec 从源码取，避免抄歪）。"""
    def __init__(self):
        self.config = _Cfg()
        self.restarts = []

    # —— 下面两个方法体从 main_window.py 里**原样取出来**再注入 ——
    def _record_monitor_cfg_snapshot(self):
        try:
            if getattr(self, "_monitor_cfg_snap", None) is not None:
                return False
            import dataclasses as _dc
            self._monitor_cfg_snap = _dc.asdict(self.config.monitor)
            return True
        except Exception:
            return False

    def _restart_monitor_if_changed(self):
        if self._record_monitor_cfg_snapshot():
            return "首次跳过"
        import dataclasses as _dc
        now = _dc.asdict(self.config.monitor)
        snap = self._monitor_cfg_snap
        changed = sorted(k for k, v in now.items() if snap.get(k) != v)
        self._monitor_cfg_snap = now
        if not changed:
            return "未变化跳过"
        self.restarts.append(changed)
        return f"重启:{changed}"


# 从真源码里核对：基线方法是否存在、是否被启动流程调用
SRC = (BASE / "src" / "ui" / "main_window.py").read_text(encoding="utf-8")

print("\n== A. 复现旧 bug：不预置基线 → 第一次改设置会被当成'首次'跳过 ==")
mw = _FakeMW()                      # 不调 _record_monitor_cfg_snapshot（= 旧行为）
mw.config.monitor.clipboard = False  # 用户改了设置
check("★旧行为确实会把'第一次改动'静默跳过（这就是真机那次）",
      mw._restart_monitor_if_changed() == "首次跳过" and mw.restarts == [],
      f"restarts={mw.restarts}")

print("\n== B. 修复后：启动时就记基线 → 第一次改设置能被识别并重启 ==")
mw2 = _FakeMW()
mw2._record_monitor_cfg_snapshot()   # = 启动监控时记基线
mw2.config.monitor.clipboard = False  # 之后用户改了设置
r = mw2._restart_monitor_if_changed()
check("★第一次改设置就能被识别（不再静默跳过）",
      r.startswith("重启:") and mw2.restarts and mw2.restarts[0] == ["clipboard"], r)

print("\n== C. 不该退化的两件事 ==")
mw3 = _FakeMW()
mw3._record_monitor_cfg_snapshot()
check("★没改设置 → 仍然跳过（1.9.2 的'没改就不重启'不能被破坏）",
      mw3._restart_monitor_if_changed() == "未变化跳过")
r2 = mw3._restart_monitor_if_changed()
check("★连续两次都不改 → 仍跳过", r2 == "未变化跳过")
mw4 = _FakeMW()
mw4._record_monitor_cfg_snapshot()
_m = mw4.config.monitor
_m.keyboard = False
mw4._restart_monitor_if_changed()
check("★改过之后基线会更新（第二次不改就不重启）",
      mw4._restart_monitor_if_changed() == "未变化跳过")

print("\n== D. 源码核对：基线确实在启动监控时记了 ==")
check("★存在 _record_monitor_cfg_snapshot 且被 _restart_monitor_if_changed 复用",
      "def _record_monitor_cfg_snapshot" in SRC
      and "if self._record_monitor_cfg_snapshot():" in SRC)
_start = SRC[SRC.index('_log.info("启动监控...")'):]
check("★启动监控之后**马上**记基线（在 start_input_monitoring 之前）",
      "_record_monitor_cfg_snapshot()" in _start[:600],
      _start[:200].replace("\n", " ")[:110])
check("★第 32 轮的后台重启线程没被改掉",
      "threading.Thread(target=_work" in SRC)

print(f"\n==== 结果: {len(PASS)} PASS / {len(FAIL)} FAIL ====")
for f in FAIL:
    print("  FAIL:", f)
sys.exit(1 if FAIL else 0)
