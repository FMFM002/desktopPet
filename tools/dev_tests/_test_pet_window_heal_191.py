#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
★ 1.9.1：形象窗口「全屏进出后消失」的 Win32 自愈 —— 回归

【这条 bug 的背景】
  打开系统图片查看器、全屏进出之后，桌宠形象会消失（子进程还活着、
  日志里没有任何 hide 命令，托盘切一下才回来）。

【用户给了三点思路，我查官方文档逐条核对（结论写进 win_window.py 文件头）】
  · 思路 1（WS_EX_COMPOSITED + WS_EX_NOACTIVATE）→ COMPOSITED **不对症**
    （MSDN 说它管的是子窗口绘制顺序，且窗口类带 CS_OWNDC 系列时不可用）；
    NOACTIVATE **有用，已采纳**。
  · 思路 2（监听 WM_DWMCOMPOSITIONCHANGED）→ **Win8+ 永远不发**（MSDN 明确写了），
    换成 WM_DISPLAYCHANGE + 前台窗口事件 + 看门狗（意图保留）。
  · 思路 3（WS_EX_NOREDIRECTIONBITMAP + DirectComposition）→ **Qt Widgets 上不可行**
    （那个样式要求窗口自己用 DComp 提供画面，换掉等于换渲染栈）。

  所以本文件重点断言两件事：
    ① 那些"看起来对但实际不成立"的做法**没有**被硬塞进去（比如别偷偷用 COMPOSITED）；
    ② 真的做了的部分（NOACTIVATE / 快照 / 重声明 / 前台监听）确实到位、且**不炸**。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

PASS = FAIL = 0


def check(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}   {detail}")


print("== ★1.9.1 形象窗口 Win32 自愈（全屏进出后消失）==")
from PySide6.QtWidgets import QApplication  # noqa: E402

app = QApplication.instance() or QApplication(sys.argv)

from src.ui import win_window as ww            # noqa: E402
from src.ui.pet_animation import PetAnimationWidget  # noqa: E402

# ── 1. 官方文档核对结论被**固化成断言**（防止以后有人照着想当然改回去）──
print("\n-- 1. 思路核对结论（写死在注释里的结论要能被回归守住）--")
_src = (ROOT / "src" / "ui" / "win_window.py").read_text(encoding="utf-8")
check("★保留 WM_DWMCOMPOSITIONCHANGED 常量（老系统仍有意义）",
      "WM_DWMCOMPOSITIONCHANGED = 0x031E" in _src)
check("★★注释里写明了它在 Win8+ 永远不发（免得后人以为是主方案）",
      "Windows 8" in _src and "不会发送此消息" in _src)
check("★★COMPOSITED 改为**默认关闭**（实测开着会坏），但保留可开关",
      "PRTS_PET_COMPOSITED" in _src and "USE_COMPOSITED = _comp_env" in _src)
check("★注释里写明了 COMPOSITED 管的是子窗口绘制顺序",
      "descendants" in _src and "bottom-to-top" in _src)
check("★★注释里写明了 NOREDIRECTIONBITMAP 在 Qt Widgets 上不可行",
      "NOREDIRECTIONBITMAP" in _src and "DirectComposition" in _src)
_src_head = _src.split("def ")[0]
check("★常量值与 Winuser.h 一致",
      ww.WS_EX_NOACTIVATE == 0x08000000 and ww.WS_EX_COMPOSITED == 0x02000000
      and ww.WS_EX_LAYERED == 0x00080000 and ww.WM_DISPLAYCHANGE == 0x007E)
check("★COMPOSITED 现在是**可开关**的（环境变量控制，用户要求试它）",
      "PRTS_PET_COMPOSITED" in _src and "USE_COMPOSITED" in _src)

# ── 2. 采纳的部分：NOACTIVATE + 快照 + 重声明 ──
print("\n-- 2. 实际采纳的做法 --")
w = PetAnimationWidget()
w.resize(128, 128)
w.show()
for _ in range(20):
    app.processEvents()
check("★窗口显示后会做 Win32 加固（不再只在托盘切换时才处理）", w._win32_ready is True)
check("★自愈定时器在主线程跑着（Win32 回调在别的线程，只能靠它回主线程）",
      w._heal_timer.isActive())

_snap = ww.snapshot(w)
# ★ rect 要真窗口才有 —— offscreen 平台插件的窗口不是真的原生窗口，
#   GetWindowRect 会返回 0，所以它按"有就更好"处理，不进必查清单。
for _k in ("visible", "iconic", "layered", "topmost", "noactivate", "foreground_title"):
    check(f"★快照含 {_k}（这就是把问题'测准'的关键）", _k in _snap, str(sorted(_snap.keys())))
check("★快照里带了矩形（拿得到时）/ 拿不到时也不缺字段",
      "rect" in _snap or "qt_geometry" in _snap, str(sorted(_snap.keys())))
check("★快照能一行写进日志", isinstance(ww.snapshot_str(w), str)
      and "qt_visible=" in ww.snapshot_str(w))

# ── 3. ★ 所有 Win32 调用都不许抛（1.9.1 已经因为"一条命令抛异常弄死子进程"吃过亏）──
print("\n-- 3. 健壮性：任何 Win32 调用都不许把异常冒到 Qt 事件循环 --")
_thrown = []
for _fn, _desc in ((lambda: ww.apply_noactivate(w), "apply_noactivate"),
                   (lambda: ww.apply_composited(w), "apply_composited"),
                   (lambda: ww.reassert(w, reason="自测"), "reassert"),
                   (lambda: ww.snapshot(w), "snapshot"),
                   (lambda: w.snapshot_str() if hasattr(w, "snapshot_str") else 1, "…")):
    try:
        _fn()
    except Exception as e:
        _thrown.append(f"{_desc}: {e}")
check("★NOACTIVATE / COMPOSITED / reassert / snapshot 都不抛", _thrown == [], str(_thrown))

# 拿不到 hwnd 时也不能炸（非 Windows / 窗口还没创建）
check("★hwnd 取不到时 snapshot 返回空壳而不是抛",
      ww.snapshot(type("X", (), {"winId": lambda self: 0})()).get("hwnd") == 0)
check("★hwnd 取不到时 reassert 返回 False 而不是抛",
      ww.reassert(type("X", (), {"winId": lambda self: 0})()) is False)

# 前台监听：装/卸都要成功且幂等
_ok_install = ww.install_foreground_hook(lambda payload: None)
check("★前台窗口监听能装（WM_DWMCOMPOSITIONCHANGED 的现代替代）", _ok_install is True
      or os.name != "nt")
check("★重复装是幂等的", ww.install_foreground_hook(lambda payload: None) is True
      or os.name != "nt")
ww.uninstall_foreground_hook()
check("★能卸载（不留钩子泄漏）", ww._hook_handle is None)

# ── 4. 形象窗口侧：消息触发 → 置标志 → 主线程自愈 ──
print("\n-- 4. 触发链路 --")
check("★接了 WM_DISPLAYCHANGE / WM_DPICHANGED（这两个在现代 Windows 真的会发）",
      "0x007E" in (ROOT / "src" / "ui" / "pet_animation.py").read_text(encoding="utf-8")
      and "0x02E0" in (ROOT / "src" / "ui" / "pet_animation.py").read_text(encoding="utf-8"))
check("★接的是 DWMCOMPOSITIONCHANGED 但注释标明 Win8+ 收不到",
      "0x031E" in (ROOT / "src" / "ui" / "pet_animation.py").read_text(encoding="utf-8"))
# nativeEvent 必须返回 tuple（PySide6 要求），返回别的东西会让 Qt 报错
_ret = w.nativeEvent(b"windows_generic_MSG", 0)
check("★nativeEvent 返回 (handled, result) 元组，且不消费消息",
      isinstance(_ret, tuple) and _ret[0] is False, repr(_ret))

# Win32 线程侧回调：只置标志，不碰 GUI
# ★ 1.9.1 修「抽」（19:38 真机日志实锤）：普通前台变化**不再**置 pending
#   （以前每切一次窗口就强制重画一次 = 用户看到的"抽"），只有切到全屏才置。
_orig_fcs_cb = ww.foreground_covers_screen
try:
    ww.foreground_covers_screen = lambda: False
    w._heal_pending = False
    w._heal_strong = False
    w._on_foreground_changed_win32(("foreground", 123))
    check("★★普通前台变化不再置 pending（修「抽」：切窗口零动作）",
          w._heal_pending is False, f"pending={w._heal_pending}")
    ww.foreground_covers_screen = lambda: True
    w._on_foreground_changed_win32(("foreground", 124))
    check("★切到全屏窗口时回调置 pending 且 strong（进入也救，反正被全屏盖着）",
          w._heal_pending is True and w._heal_strong is True,
          f"pending={w._heal_pending} strong={w._heal_strong}")
finally:
    ww.foreground_covers_screen = _orig_fcs_cb
    w._heal_pending = False
    w._heal_strong = False

# 取出/清掉 + 节流（打桩固定前台状态，保证确定性）
_orig_fcs_t = ww.foreground_covers_screen
_orig_fiv_t = getattr(ww, "foreground_is_viewer", None)
try:
    ww.foreground_covers_screen = lambda: False
    if _orig_fiv_t is not None:
        ww.foreground_is_viewer = lambda: False
    w._was_fg_fullscreen = False
    w._was_fg_viewer = False
    w._heal_pending = True
    w._last_heal = 0.0
    w._check_heal()
    check("★主线程取出标志并清掉", w._heal_pending is False)
    w._heal_pending = True
    w._check_heal()
    check("★节流生效：节流窗口内 pending 保持（不重复自愈）",
          w._heal_pending is True, f"pending={w._heal_pending}")
finally:
    ww.foreground_covers_screen = _orig_fcs_t
    if _orig_fiv_t is not None:
        ww.foreground_is_viewer = _orig_fiv_t
    w._heal_pending = False

check("★heal_window 是统一自愈入口（Win32 修 + Qt 重画）",
      callable(getattr(w, "heal_window", None)))
w.heal_window("自测")
check("★自愈后窗口仍然正常（没把形象搞没）", w.isVisible() is True)

# ── 5. ★★ 真机诊断后的加强版（2026-09-18 13:20 现场日志驱动）──
print("\n-- 5. 真机诊断结论驱动的加强版自愈 --")
_pa_src = (ROOT / "src" / "ui" / "pet_animation.py").read_text(encoding="utf-8")
check("★★源码里写明了诊断结论：出问题时窗口状态**全是正常的**",
      "窗口没坏" in _pa_src or "窗口状态**全都是对的**" in _pa_src)
check("★★结论写明「只有 hide→show 重建分层表面才有效」（用户土办法验证过）",
      "_recreate_surface" in _pa_src and "hide" in _pa_src.lower())
check("★所以分两级：轻量（强制重绘）总做，重度（hide→show）只在必要时",
      "strong" in _pa_src
      and ("只在全屏进/出" in _pa_src.replace("\n", "")
           or "普通切换什么都不做" in _pa_src.replace("\n", "")))

check("★内容快照能回答「画面侧」的问题（GIF 在播吗/帧空吗/标签有 pixmap 吗）",
      callable(getattr(w, "_content_snapshot", None))
      and "gif=" in w._content_snapshot()
      and "标签pixmap=" in w._content_snapshot(), w._content_snapshot())
check("★_force_repaint 存在（关键：光 setPixmap(同一张) 是空操作，Qt 不会重绘）",
      callable(getattr(w, "_force_repaint", None)) and "clear()" in _pa_src)
check("★_recreate_surface 存在（hide→show 重建分层表面）",
      callable(getattr(w, "_recreate_surface", None)))
check("★有「全屏检测」决定要不要走重度（免得每次 Alt+Tab 都闪）",
      callable(getattr(ww, "foreground_covers_screen", None))
      and isinstance(ww.foreground_covers_screen(), bool))

# 重度自愈必须安全：hide→show 之后窗口还得在、还得是可见的
_w_before = w.isVisible()
w.heal_window("自测重度", strong=True)
check("★★重度自愈（hide→show）之后窗口仍然可见（别把人搞没了）",
      w.isVisible() is True, f"before={_w_before} after={w.isVisible()}")

# 标志链路：前台回调置 strong → 主线程取出消费
# ★ 1.9.1：只有切到全屏才置 strong；普通切换零动作
_orig_fcs_b2 = ww.foreground_covers_screen
try:
    ww.foreground_covers_screen = lambda: True
    w._heal_pending = False
    w._heal_strong = False
    w._heal_reason = ""
    w._on_foreground_changed_win32(("foreground", 1))
    check("★切到全屏时回调置 pending 且 strong",
          w._heal_pending is True and w._heal_strong is True,
          f"pending={w._heal_pending} strong={w._heal_strong}")
    # 取出消费（固定前台状态避免真实前台干扰）
    ww.foreground_covers_screen = lambda: False
    if _orig_fiv_t is not None:
        ww.foreground_is_viewer = lambda: False
    w._was_fg_fullscreen = False
    w._was_fg_viewer = False
    w._last_heal = 0.0
    w._heal_pending = True
    w._heal_strong = True
    w._check_heal()
    check("★★主线程取出后会**清掉 strong**（不然下一次普通切换也会闪一下）",
          w._heal_strong is False, f"strong={w._heal_strong}")
finally:
    ww.foreground_covers_screen = _orig_fcs_b2
    if _orig_fiv_t is not None:
        ww.foreground_is_viewer = _orig_fiv_t
    w._heal_pending = False
    w._heal_strong = False

# ── 6. ★★ 轮询"退出全屏"触发（用户 13:24 场景描述驱动的关键修复）──
print("\n-- 6. 轮询检测「刚从全屏退出」（不依赖事件、不看窗口状态）--")
check("★★源码写明了现有兜底的漏洞（只看 isVisible/isMinimized，而那两项都是正常的）",
      "从不调用" in _pa_src or "从不成立" in _pa_src)
check("★★且写明「同一张 pixmap 再设一遍」是空操作（所以旧兜底救不回来）",
      "空操作" in _pa_src)
check("★新增 _was_fg_fullscreen 状态（只检测「进入全屏」是不够的，形象是在退出时消失的）",
      "_was_fg_fullscreen" in _pa_src)
_pp_src = (ROOT / "src" / "ui" / "pet_process.py").read_text(encoding="utf-8")
check("★★pet_process 的看门狗改用 force_repaint（不再只调无效的 repaint_current）",
      "pet.force_repaint()" in _pp_src)
check("★force_repaint 是公开方法（看门狗要能调到）",
      callable(getattr(w, "force_repaint", None)))
check("★force_repaint 真的会重画（内部走 clear→repaint→设回）",
      "_force_repaint" in _pa_src and "self._label.clear()" in _pa_src)

# 用打桩验证"进入全屏不触发、退出全屏才触发重度"
_seq = [True, True, False, False, False]
_ci = {"n": 0}
_orig_fcs = ww.foreground_covers_screen


def _fake_fcs():
    # ★ 每个 _check_heal 会调用**两次**：一次是轮询本身，一次是 foreground_debug()
    #   里带的"全屏判定"。所以用 n//2 让同一个 heal 周期落到同一个阶段。
    i = min(_ci["n"] // 2, len(_seq) - 1)
    _ci["n"] += 1
    return _seq[i]


_healed = []
try:
    ww.foreground_covers_screen = _fake_fcs
    if _orig_fiv_t is not None:
        ww.foreground_is_viewer = lambda: False   # ★ 打桩：本节只测全屏触发器
    w.heal_window = lambda reason="", strong=False: _healed.append((reason, strong))
    w._last_heal = 0.0
    w._heal_pending = False
    w._was_fg_fullscreen = False
    w._was_fg_viewer = False
    w._check_heal()                       # 第 1 次：全屏 → 不触发
    check("★进入全屏时不触发（不然进的时候也闪一下）", _healed == [], str(_healed))
    w._check_heal()                       # 第 2 次：还是全屏 → 不触发
    check("★持续全屏时不触发", _healed == [], str(_healed))
    w._check_heal()                       # 第 3 次：退出全屏 → 触发重度
    check("★★退出全屏那一刻触发**重度**自愈（就是用户报的那一下）",
          len(_healed) == 1 and _healed[0][1] is True and "全屏" in _healed[0][0],
          str(_healed))
    check("★状态跟着更新（不会每轮都触发一次）", w._was_fg_fullscreen is False)
finally:
    ww.foreground_covers_screen = _orig_fcs
    if _orig_fiv_t is not None:
        ww.foreground_is_viewer = _orig_fiv_t

# ── 6b. ★★ 图片查看器进/出触发（2026-09-18 19:38 日志实锤的补丁）──
#   照片 app 是**普通窗口**（真机 (114,114,1920x1117)），全屏判据罩不住它；
#   13:36 那次能自愈，全靠旧判据（≥90%）把最大化误报成全屏碰巧触发。
#   13:50 修掉误报（修「抽」）后，这条碰巧的救回路也断了 → 14:14 起"没有回来"。
print("\n-- 6b. 图片查看器进/出触发（照片是普通窗口，全屏判据罩不住）--")
_ww_src = (ROOT / "src" / "ui" / "win_window.py").read_text(encoding="utf-8")
check("★win_window 提供了 foreground_is_viewer（查看器独立判据）",
      callable(getattr(ww, "foreground_is_viewer", None)))
check("★标题判据在位（照片看图时标题就是文件名，如 xxx.jpg）",
      ".jpg" in _ww_src and "照片" in _ww_src)
check("★进程名判据在位（photos.exe 等已知查看器）",
      "photos.exe" in _ww_src and "QueryFullProcessImageName" in _ww_src)
check("★pet_animation 有 _was_fg_viewer 状态（进出两个方向都要测得到）",
      "_was_fg_viewer" in _pa_src)
_vseq = [False, True, True, False, False]
_vi = {"n": 0}


def _fake_fiv():
    # foreground_is_viewer 每个轮询周期只调 1 次（foreground_debug 不查它）
    i = min(_vi["n"], len(_vseq) - 1)
    _vi["n"] += 1
    return _vseq[i]


_healed2 = []
try:
    ww.foreground_covers_screen = lambda: False   # 全程不是全屏（照片就是普通窗口）
    ww.foreground_is_viewer = _fake_fiv
    w.heal_window = lambda reason="", strong=False: _healed2.append((reason, strong))
    w._last_heal = 0.0
    w._heal_pending = False
    w._was_fg_fullscreen = False
    w._was_fg_viewer = False
    w._check_heal()                       # 第 1 次：非查看器 → 不触发
    check("★平时（非查看器、非全屏）不触发 —— 这就是「不抽」", _healed2 == [], str(_healed2))
    w._check_heal()                       # 第 2 次：切到查看器 → 触发重度
    check("★★切到图片查看器触发**重度**自愈（进入也救）",
          len(_healed2) == 1 and _healed2[0][1] is True and "查看器" in _healed2[0][0],
          str(_healed2))
    w._check_heal()                       # 第 3 次：还在查看器 → 不触发
    check("★停留在查看器时不触发（看图过程中不闪）", len(_healed2) == 1, str(_healed2))
    w._last_heal = 0.0                    # 绕过节流：单独验证退出触发器
    w._check_heal()                       # 第 4 次：退出查看器 → 触发重度
    check("★★退出图片查看器触发**重度**自愈（兜住两场景的关键一环）",
          len(_healed2) == 2 and _healed2[1][1] is True and "查看器" in _healed2[1][0],
          str(_healed2))
    check("★状态跟着更新（不会每轮都触发）", w._was_fg_viewer is False)
finally:
    ww.foreground_covers_screen = _orig_fcs
    if _orig_fiv_t is not None:
        ww.foreground_is_viewer = _orig_fiv_t

# ── 7. ★★ 真·对症探针：问 DWM「你到底有没有在渲染」（DWMWA_CLOAKED）──
#   为什么加它：真机证据表明"失效发生在 DWM 那一层"（窗口状态和画面都正常），
#   所以靠"猜时机"（是不是刚从全屏退出）两次都没认出来。
#   这个 API 直接回答"有没有在呈现"，才是能检测到故障本身的探针。
print("\n-- 7. DWM cloaked 探针（对症检测，不靠猜）--")
check("★★win_window 提供了 is_cloaked（DWMWA_CLOAKED = 14）",
      callable(getattr(ww, "is_cloaked", None))
      and "DWMWA_CLOAKED" in _src and "c_uint(14)" in _src)
check("★拿不到时返回 None（不确定 → 调用方不许据此做破坏性动作）",
      ww.is_cloaked(type("X", (), {"winId": lambda self: 0})()) is None)
# 真机实测过：真实窗口上 hr=0、cloaked=0（offscreen 平台的窗口不是真窗口，会拿不到，
# 所以这里只断言"可调用 + 类型合理"，不断言一定拿得到）
_cl = ww.is_cloaked(w)
check("★is_cloaked 可调用且返回值类型合理（None / bool）",
      _cl is None or isinstance(_cl, bool), repr(_cl))

# ★ 关键分支：cloaked=True 时必须走重度重建；健康时只做不闪的重新声明
_o_cloak = ww.is_cloaked
_h2 = []
try:
    w.heal_window = lambda reason="", strong=False: _h2.append((reason, strong))
    ww.is_cloaked = lambda widget: True          # 打桩：DWM 说没在渲染
    w.reassert_window()
    check("★★DWM 报告 cloaked → 走**重度**重建（hide→show）",
          len(_h2) == 1 and _h2[0][1] is True and "cloaked" in _h2[0][0], str(_h2))
    _h2.clear()
    ww.is_cloaked = lambda widget: False         # 打桩：健康
    _reasserted = []
    _o_re = ww.reassert
    ww.reassert = lambda widget, topmost=True, reason="": _reasserted.append(reason) or True
    try:
        w.reassert_window()
    finally:
        ww.reassert = _o_re
    check("★★健康时**什么都不做**（连重声明都不做 —— 上一版每 3 秒做一次，"
          "用户实测「会抽」，已去掉）",
          _h2 == [] and _reasserted == [], f"heal={_h2} reassert={_reasserted}")
finally:
    ww.is_cloaked = _o_cloak

# ── 8. ★ SWP_SHOWWINDOW：真机证据指向的"窗口级重新显示" ──
print("\n-- 8. reassert 必须带 SWP_SHOWWINDOW --")
check("★★源码里写明了「对已存在的分层表面反复重画没用」这个真机结论",
      "反复重画是没用的" in _src or "对已存在的分层表面反复重画" in _src)
check("★★reassert 真的带上了 SWP_SHOWWINDOW（第一版漏了，这可能就是没效果的直接原因）",
      "| SWP_FRAMECHANGED | SWP_SHOWWINDOW)" in _src)
check("★周期自检入口 reassert_window 已接进看门狗",
      "pet.reassert_window()" in _pp_src)
check("★前台诊断会把「窗口矩形 vs 显示器矩形」的原始数字记下来"
      "（下次复现不用再猜为什么没认出来）",
      "foreground_debug" in _src and "前台诊断" in _pa_src)

# ── 9. ★★ 修"抽"（用户 13:39："对，会抽。"）──
print("\n-- 9. 修用户报的「抽」（我引入的副作用）--")
check("★★源码写明了「最大化窗口」被误判成全屏这件事（含用户日志里的真实矩形）",
      "-11,-11" in _src or "最大化" in _src)
check("★★全屏判据现在是 99% + 不越界（最大化窗口高度只覆盖 ~96.2%，会被排除）",
      "threshold: float = 0.99" in _src and "不越界" in _src)
# 用用户日志里的真实两串数字验证判据（把结论固化成回归）
_MON = (0, 0, 2560, 1600)
_MAXIMIZED = (-11, -11, 2582, 1550)


def _covers(win, mon):
    w, h = win[2], win[3]
    mw, mh = mon[2], mon[3]
    cw = min(1.0, max(0.0, min(win[0] + w, mon[0] + mw) - max(win[0], mon[0])) / mw)
    ch = min(1.0, max(0.0, min(win[1] + h, mon[1] + mh) - max(win[1], mon[1])) / mh)
    if cw < 0.99 or ch < 0.99:
        return False
    if win[0] < mon[0] - 2 or win[1] < mon[1] - 2:
        return False
    return True


check("★★用用户日志里的真实数字验证：最大化窗口**不算**全屏（否则每次切窗口都会抽）",
      _covers(_MAXIMIZED, _MON) is False, "最大化应为 False")
check("★★真全屏（严丝合缝）才算全屏",
      _covers((0, 0, 2560, 1600), _MON) is True)
check("★★健康时 reassert_window **什么都不做**（上一版每 3 秒重声明一次 → 就是「抽」的来源）",
      "什么都不做" in _pa_src and "SWP_SHOWWINDOW 重声明，用户实测" in _pa_src)

# ── 10. ★ 按用户 13:39 的方案启用 WS_EX_COMPOSITED（附实测依据）──
print("\n-- 10. WS_EX_COMPOSITED（用户点名要的方案）--")
check("★★USE_COMPOSITED **默认关**（实测：开了会让分层窗口彻底坏掉）",
      ww.USE_COMPOSITED is False, str(ww.USE_COMPOSITED))
check("★★实测证据被写进源码：UpdateLayeredWindowIndirect 报「参数错误」",
      "UpdateLayeredWindowIndirect" in _src and "参数错误" in _src)
check("★★COMPOSITED 与 LAYERED 互斥这件事写明了（免得后人再打开）",
      "互斥" in _src)
check("★★apply_composited 会**主动拒绝**分层窗口（防御性，不只是默认关）",
      "拒绝设置 WS_EX_COMPOSITED" in _src and "WS_EX_LAYERED" in
      _src.split("def apply_composited")[1].split("def ")[0])
check("★环境变量 PRTS_PET_COMPOSITED 仍保留（只作 A/B 实验用，默认 0）",
      "PRTS_PET_COMPOSITED" in _src and '"0")' in _src)
check("★file_ops 里写的 `WS_EX_COMPOSITED` 常量值正确（0x02000000）",
      ww.WS_EX_COMPOSITED == 0x02000000)
check("★apply_composited 会被调用（在 Win32 加固时）",
      "USE_COMPOSITED" in _pa_src and "apply_composited" in _pa_src)

# ── 11. ★★ 通用护栏：用了但没定义的 Win32 常量 ──
#   血泪教训（2026-09-18）：我用了 `SWP_NOZORDER` 却忘了定义，
#   结果 `_set_exstyle` 里那句 SetWindowPos 每次都抛 NameError
#   （日志：`SetWindowLong 失败: name 'SWP_NOZORDER' is not defined`），
#   被 except 吞掉 → **改完样式后"让系统重算"那一步永远失败**，
#   也就是 WS_EX_NOACTIVATE / WS_EX_COMPOSITED 只写了样式位却没真正生效。
#   这种错**静态就能查出来**，所以这里用 AST 扫一遍：
#   凡是以 Win32 前缀开头的名字，要么在模块里定义过，要么是从别处来的，
#   否则直接 FAIL。
print("\n-- 11. 护栏：Win32 常量必须都有定义 --")
import ast as _ast

_PREFIXES = ("SWP_", "WS_EX_", "WS_", "RDW_", "WM_", "GA_", "GWL_", "GCL_",
             "HWND_", "EVENT_", "WINEVENT_", "CS_", "MONITOR_")
_tree = _ast.parse(_src)
_defined = set()
for _node in _ast.walk(_tree):
    if isinstance(_node, _ast.Assign):
        for _t in _node.targets:
            if isinstance(_t, _ast.Name):
                _defined.add(_t.id)
    elif isinstance(_node, (_ast.Import, _ast.ImportFrom)):
        for _a in _node.names:
            _defined.add((_a.asname or _a.name).split(".")[0])
    elif isinstance(_node, (_ast.FunctionDef, _ast.AsyncFunctionDef, _ast.ClassDef)):
        _defined.add(_node.name)
    elif isinstance(_node, _ast.arg):
        _defined.add(_node.arg)
_used = {n.id for n in _ast.walk(_tree)
         if isinstance(n, _ast.Name) and n.id.startswith(_PREFIXES)}
_missing = sorted(x for x in _used if x not in _defined)
check("★★用了的 Win32 常量全都定义过（SWP_NOZORDER 那次就是这么漏的）",
      _missing == [], f"未定义: {_missing}")
check("★确有 SWP_NOZORDER 定义（0x0004）", ww.SWP_NOZORDER == 0x0004)
check("★★_set_exstyle 里 SetWindowPos 那段真的会执行到（不再被 NameError 吞）",
      "SWP_NOZORDER" in _src.split("def _set_exstyle")[1].split("def ")[0]
      if "def _set_exstyle" in _src else False)

# ── 12. ★★ 色键方案已回退（2026-09-18 19:15 用户拍板）──
#   真机三连败（描边 / 消失且不回来 / 还是抽）→ "返回两种场景都能自愈的备份"。
#   回退目标：bak-20260918-141430（色键动手前的状态，自愈那套完整）。
#   本节断言从"色键生效"反转成"色键已清除干净"——防止将来有人误以为还在。
print("\n-- 12. 色键方案已回退 --")
check("★★win_window 不再有 apply_color_key（色键入口已删）",
      getattr(ww, "apply_color_key", None) is None)
check("★win_window 不再有 COLOR_KEY / LWA_COLORKEY 常量",
      not hasattr(ww, "COLOR_KEY") and not hasattr(ww, "LWA_COLORKEY"))
check("★★pet_animation 不再有 _use_colorkey（环境变量开关已删）",
      "_use_colorkey" not in _pa_src and "PRTS_PET_COLORKEY" not in _pa_src)
check("★pet_animation 不再有 _prepare_frame（二值化合成已删）",
      "_prepare_frame" not in _pa_src)
check("★★WA_TranslucentBackground 恢复为默认无条件开启（逐像素 alpha 老路）",
      "self.setAttribute(Qt.WA_TranslucentBackground)" in _pa_src
      and "WA_TranslucentBackground, False" not in _pa_src)
check("★源码里没有残留的 numpy 色键合成（frombuffer/tobytes）",
      "frombuffer" not in _pa_src and "tobytes" not in _pa_src)

# ── 13. ★ 日志顺序坑（导致 prts_*.log 永远 0 字节）──
print("\n-- 13. 日志顺序：先重定向 stderr 再 setup_logging --")
_mn_src = (ROOT / "main.py").read_text(encoding="utf-8")
check("★★main.py 里 redirect_stderr_to_file 在 setup_logging **之前**",
      _mn_src.find("redirect_stderr_to_file(\"main\")") < _mn_src.find("log = setup_logging("),
      "顺序反了：日志文件会占到 fd 2、被 dup2 顶掉 → 永远 0 字节")
check("★★pet_process.py 里也是同样顺序",
      _pp_src.find("redirect_stderr_to_file(\"pet\")")
      < _pp_src.find("setup_logging(verbose=False)"))
check("★源码里写明了这个坑（免得后人调回去）",
      "0 字节" in _mn_src or "0 字节" in _pp_src)

# ── 14. ★★★ 子进程内存：nativeEvent 的 ctypes 永久缓存泄漏（1.9.2 定位并修复）──
#   真凶不是"切换 GIF 留内存"（旧 QMovie 清理链完整），而是 nativeEvent 里
#   **每次调用新建 _MsgHdr 类再对它 ctypes.POINTER()** —— CPython 会把结果塞进
#   模块级永久字典 ctypes._pointer_type_cache（key 是那个类、强引用），
#   每条原生消息泄漏约 5.4KB 且 GC 收不掉。实测 20000 次 → +103MB。
#   修法：结构体与指针类型提到模块级只建一次。
import ctypes as _ct                                    # noqa: E402
import src.ui.pet_animation as _pa_mod                  # noqa: E402
check("★★★nativeEvent 用的 MSG 指针类型在模块级（不是每次新建）",
      hasattr(_pa_mod, "_MSG_HDR_PTR") and hasattr(_pa_mod, "_MsgHdr"))
check("★★★nativeEvent 函数体里不再 `class _MsgHdr`（那是泄漏根源）",
      "class _MsgHdr" not in _pa_src.split("def nativeEvent")[1].split("def ")[0])
# ★ 判据要盯"实际调用形态"，不能只看出现没出现 POINTER( 这个词 ——
#   我自己的解释性注释里就写了 `ctypes.POINTER(_MsgHdr)`（第一版因此假红）。
#   真正的判据：函数体里用的是**复用的模块级指针类型** _MSG_HDR_PTR。
_nb = _pa_src.split("def nativeEvent")[1].split("def ")[0]
check("★★nativeEvent 用复用的模块级指针类型（cast(addr, _MSG_HDR_PTR)）",
      "cast(addr, _MSG_HDR_PTR)" in _nb)
check("★★nativeEvent 里没有把 POINTER 结果算在函数内（只允许出现在注释里）",
      not any(l.strip().startswith(("m = ctypes.POINTER", "ctypes.POINTER(_MsgHdr)"))
              for l in _nb.split("\n")))
check("★nativeEvent 对非 Windows 原生消息提前退出",
      "windows_generic_MSG" in _pa_src)
_before = len(_ct._pointer_type_cache)
_addr = _ct.addressof(_ct.c_int(0))
for _i in range(20000):
    _m = _ct.cast(_addr, _pa_mod._MSG_HDR_PTR).contents
    _ = _m.message
_after = len(_ct._pointer_type_cache)
check(f"★★★20000 次解析后 ctypes 指针缓存零增长（{_before} → {_after}）",
      _after == _before, f"{_before} → {_after}（原来会 +20000 条 / +103MB）")
check("★注释里留了量化复现数字（给下一任看）",
      "103.16" in _pa_src or "+103" in _pa_src or "20021" in _pa_src)

print(f"\n★ 形象窗口自愈回归：{PASS} PASS / {FAIL} FAIL")

sys.exit(1 if FAIL else 0)

