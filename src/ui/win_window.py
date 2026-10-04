#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
★ 1.9.1: 形象窗口的 Win32/分层窗口自愈工具（Windows 专用，非 Windows 全部空操作）

【这个模块解决什么】
  老 bug：**打开系统图片查看器、全屏进出之后，桌宠形象会消失**
  （子进程还活着、日志里没有任何 hide 命令，用托盘"隐藏/显示"切一下才回来）。

【★ 用户给的三点思路 —— 我查了官方文档，逐条核对结果】
  用户在日志里写了三条思路让我"研究研究"，结论如下（都附了依据）：

  思路 1「加 WS_EX_COMPOSITED + WS_EX_NOACTIVATE，让 DWM 始终合成」
    · `WS_EX_COMPOSITED` —— **不适用**。MSDN 的定义是
      "Paints all descendants of a window in bottom-to-top painting order using
       double-buffering"，它解决的是**子窗口**的绘制顺序/半透明（而且要求子窗口
      也带 WS_EX_TRANSPARENT），跟"顶层分层窗口被 DWM 丢掉合成"是两回事；
      文档还写明**窗口类带 CS_OWNDC / CS_CLASSDC / CS_PARENTDC 时不能用**。
      所以它既不对症、还可能无效。
    · `WS_EX_NOACTIVATE` —— **有用、我已采纳**。文档：带此样式的顶层窗口
      "does not become the foreground window when the user clicks it"。
      桌宠本来就不需要焦点，加上它点击形象**不会抢走你在别的程序里的焦点**，
      是个实打实的体验改进（顺带避免"点了形象导致聊天窗口失焦"这类干扰）。

  思路 2「监听 WM_DWMCOMPOSITIONCHANGED，收到就重设样式 + 强制重绘」
    · **在 Windows 8 及以上永远不会触发**。MSDN 原文注意事项：
      "从 Windows 8 起，DWM 组合始终处于启用状态，因此无论视频模式如何更改，
       都不会发送此消息。"（本机是 RTX 5060 + Win10/11，必然收不到）
    · 但**这个思路的意图（检测到环境变化就重新声明窗口状态）是对的**，
      所以我换成了在这些真的会发的信号上做：
        · `WM_DISPLAYCHANGE`（分辨率/显示模式变化 —— 全屏进出常常触发）
        · `EVENT_SYSTEM_FOREGROUND`（`SetWinEventHook`，前台窗口变化 —— 现代
          Windows 上"某个程序全屏了/退出了"最可靠的信号）
        · 以及子进程本来就有的看门狗定时器兜底。

  思路 3「（最彻底）用 WS_EX_NOREDIRECTIONBITMAP 替代 WS_EX_LAYERED，走 DirectComposition」
    · **在 Qt Widgets 上不可行**。文档对它的定义是
      "The window does not render to a redirection surface. This is for windows that
       do not have visible content or that use mechanisms other than surfaces to
       provide their visual." —— 也就是说**这个窗口必须自己用 DirectComposition
      提供画面**。而 Qt Widgets 是用 QPainter/GDI 往重定向表面画的，
      换成 NOREDIRECTIONBITMAP 的结果是**窗口直接什么都不显示**。
      真要走这条路，等于把渲染栈换成 Qt Quick + DComp 后端（或自绘 D3D），
      成本远大于收益。

【所以这一版实际做的是】
  · 采纳 `WS_EX_NOACTIVATE`（思路 1 里真正有用的那半）；
  · 用**真的会触发**的信号（WM_DISPLAYCHANGE + 前台窗口事件）+ 看门狗，
    在环境变化后**重新声明窗口状态并强制重绘**（思路 2 的意图）；
  · ★ 最重要的是：**先把现场测准**。以前失败时日志里一点证据都没有，
    只能靠猜。现在每次触发都会打一份**窗口状态快照**
    （是否可见 / 是否被最小化 / 是不是 topmost / 扩展样式 / 矩形 / 前台窗口），
    这样下一次真的复现时，我们就能知道到底是"丢了 topmost"、"被最小化"，
    还是"表面失效"—— 而不是继续换着法子猜。

【安全约定】
  所有函数：
    · 非 Windows → 直接返回 None/False，不 import ctypes.windll
    · 任何 Win32 调用失败 → 吞掉并 debug 日志，绝不抛给 Qt 事件循环
      （1.9.1 已经因为"一条命令抛异常弄死形象子进程"吃过一次亏）
"""
from __future__ import annotations

import logging
import sys

_log = logging.getLogger("WinWindow")

IS_WIN = sys.platform == "win32"

# ── Win32 常量 ──
GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_TOPMOST = 0x00000008
WS_EX_NOACTIVATE = 0x08000000
WS_EX_COMPOSITED = 0x02000000        # 保留常量 + 开关，默认不开（见文件头说明）

WM_DISPLAYCHANGE = 0x007E
WM_DWMCOMPOSITIONCHANGED = 0x031E    # ★ Win8+ 不会再发，只为兼容旧系统保留
WM_DPICHANGED = 0x02E0

SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004          # ★ 1.9.1 修正：这个漏定义了！
                               #   后果：_set_exstyle 里 SetWindowPos 抛 NameError
                               #   （日志里 `SetWindowLong 失败: name 'SWP_NOZORDER' is not defined`），
                               #   于是"改完扩展样式后 SWP_FRAMECHANGED 让系统重算"这步**永远失败**
                               #   → WS_EX_NOACTIVATE / WS_EX_COMPOSITED 只写进了样式位，
                               #     但没有真正生效。
SWP_NOACTIVATE = 0x0010
SWP_FRAMECHANGED = 0x0020
SWP_SHOWWINDOW = 0x0040
SWP_NOOWNERZORDER = 0x0200
HWND_TOPMOST = -1
HWND_NOTOPMOST = -2

RDW_INVALIDATE = 0x0001
RDW_UPDATENOW = 0x0100
RDW_ALLCHILDREN = 0x0080
RDW_FRAME = 0x0400

EVENT_SYSTEM_FOREGROUND = 0x0003
WINEVENT_OUTOFCONTEXT = 0x0000
WINEVENT_SKIPOWNPROCESS = 0x0002
MONITOR_DEFAULTTONEAREST = 0x00000002

# 是否给形象窗口加 WS_EX_COMPOSITED。
#
# ★★ 1.9.1 实测结论：**不要开！它会把分层窗口彻底弄坏。**
#
#   时间线（用户提供的现场日志）：
#     · 13:29–13:43 的日志里窗口都是 `composited=False` —— 那时没开这个开关；
#     · 我在 13:39:42 把它写进 .env 打开（用户要求"从根本解决"，我也想实测）；
#     · 13:45:40 的日志变成 `composited=True`，`exstyle=0xa080088`，
#       **同时控制台开始刷屏**：
#         `UpdateLayeredWindowIndirect failed for ptDst=(2337,1245),
#          size=(197x197), dirty=(197x197 0,0) (参数错误。)`
#       → 形象**彻底不显示**（这就是用户报的"启动失败"）。
#
#   机制：`WS_EX_COMPOSITED` 让窗口内容走 **DWM 重定向表面**（自下而上双缓冲合成），
#         而 `WS_EX_LAYERED` 要求**应用自己**用 `UpdateLayeredWindow` 提供内容 ——
#         **这两者是互斥的**。加上 COMPOSITED 后，Qt 每次推像素都被 Windows
#         以 `ERROR_INVALID_PARAMETER` 拒绝 → 一个像素都上不了屏。
#
#   → 所以：**默认关**，而且 `apply_composited()` 会**拒绝**在分层窗口上设置它
#     （见那里的判断），避免以后有人又打开。
#   → 环境变量 `PRTS_PET_COMPOSITED` 保留只是为了万一要做 A/B 实验，
#     但既然实测会坏，默认必须是 0。
import os as _os
_comp_env = (_os.getenv("PRTS_PET_COMPOSITED", "0") or "0").strip().lower()
USE_COMPOSITED = _comp_env in ("1", "true", "yes", "on")


def foreground_covers_screen(threshold: float = 0.99) -> bool:
    """★ 1.9.1: 当前前台窗口是不是**真·全屏**（不是"最大化"）。

    【★ 这一版的修正来自真机日志（用户 13:39 的"抽"）】
      上一版用"覆盖 ≥90%"，结果**把"最大化窗口"也判成了全屏**：
        前台='...DeepSeek Harness...' win=(-11,-11,2582x1550) mon=(0,0,2560x1600)
      那是 Windows **最大化窗口**的典型矩形（左/上 -11 是给不可见缩放边框留的，
      高度 1550 只到 1539 —— **没盖住任务栏那 61px**）。
      它覆盖了 96.9%，超过 90% 阈值 → 被判成"全屏进/出" →
      **用户每在最大化窗口之间切换一次，就 hide→show 一次 → 形象"抽"一下**。
      （用户原话："对，会抽。"）

    【现在的判据：两个条件同时满足才算全屏】
      ① 宽高覆盖显示器 ≥ `threshold`（默认 **99%** —— 最大化窗口只有 ~96.9%，被排除）；
      ② **不越界**：窗口矩形不超出显示器（真全屏是严丝合缝；
         最大化窗口会往左上各溢出 ~11px）。
      这样"最大化"与"全屏"就能可靠区分，不再无故触发。
    """
    if not IS_WIN:
        return False
    u = _user32()
    if not u:
        return False
    try:
        import ctypes
        from ctypes import wintypes

        hwnd = u.GetForegroundWindow()
        if not hwnd:
            return False

        class MONITORINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                        ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]

        cands = []
        h = hwnd
        for _ in range(6):                      # 最多往上 6 层，防死循环
            if not h:
                break
            cands.append(h)
            nxt = u.GetAncestor(ctypes.c_void_p(h), 2)   # GA_ROOT = 2
            if not nxt or nxt == h:
                break
            h = nxt

        for h in cands:
            r = wintypes.RECT()
            if not u.GetWindowRect(ctypes.c_void_p(h), ctypes.byref(r)):
                continue
            mon = u.MonitorFromWindow(ctypes.c_void_p(h), MONITOR_DEFAULTTONEAREST)
            if not mon:
                continue
            mi = MONITORINFO()
            mi.cbSize = ctypes.sizeof(MONITORINFO)
            if not u.GetMonitorInfoW(ctypes.c_void_p(mon), ctypes.byref(mi)):
                continue
            m = mi.rcMonitor
            mw, mh = (m.right - m.left), (m.bottom - m.top)
            if mw <= 0 or mh <= 0:
                continue
            cw = min(1.0, max(0.0, min(r.right, m.right) - max(r.left, m.left)) / mw)
            ch = min(1.0, max(0.0, min(r.bottom, m.bottom) - max(r.top, m.top)) / mh)
            if cw < threshold or ch < threshold:
                continue
            # ② 不越界（最大化窗口会溢出 ~11px，真全屏不会）
            if r.left < m.left - 2 or r.top < m.top - 2:
                continue
            if r.right > m.right + 2 or r.bottom > m.bottom + 2:
                continue
            return True
        return False
    except Exception as e:
        _log.debug(f"判断全屏失败: {e}")
        return False


def foreground_debug() -> str:
    """★ 1.9.1: 把"前台窗口 vs 显示器"的原始数字打出来。

    ★ 这是给**下一次复现**用的：上一轮我因为看不到这些数字，
      只能猜"为什么全屏检测没认出来"。现在前台一变就记一行，
      下次直接看数字，不用再猜。
    """
    if not IS_WIN:
        return "非 Windows"
    u = _user32()
    if not u:
        return "无 user32"
    try:
        import ctypes
        from ctypes import wintypes

        hwnd = u.GetForegroundWindow()
        if not hwnd:
            return "无前台窗口"
        buf = ctypes.create_unicode_buffer(128)
        u.GetWindowTextW(ctypes.c_void_p(hwnd), buf, 128)
        r = wintypes.RECT()
        u.GetWindowRect(ctypes.c_void_p(hwnd), ctypes.byref(r))
        mon = u.MonitorFromWindow(ctypes.c_void_p(hwnd), MONITOR_DEFAULTTONEAREST)

        class MONITORINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                        ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]

        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(MONITORINFO)
        u.GetMonitorInfoW(ctypes.c_void_p(mon), ctypes.byref(mi))
        m = mi.rcMonitor
        return (f"前台={buf.value[:40]!r} "
                f"win=({r.left},{r.top},{r.right - r.left}x{r.bottom - r.top}) "
                f"mon=({m.left},{m.top},{m.right - m.left}x{m.bottom - m.top}) "
                f"全屏判定={foreground_covers_screen()} "
                f"查看器={foreground_is_viewer()}")
    except Exception as e:
        return f"前台诊断失败: {e}"


# ★ 1.9.1（真机 2026-09-18 19:38 / 13:36 两段日志实锤）：系统图片查看器
#   （照片/Photos）是个**普通窗口**（真机矩形 (114,114,1920x1117)），永远过不了
#   "真·全屏"判据 —— 但形象恰恰是在它进/出时消失的。历史上（13:36 那次）能
#   自愈，全靠旧判据（≥90%）把最大化窗口误报成全屏、切换时碰巧触发了重度自愈。
#   13:50 修掉误报（修「抽」）后，这条碰巧的救回路也断了 → 14:14 起用户报
#   "没有回来"。→ 查看器必须有自己的判据。
_IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp",
               ".tif", ".tiff", ".heic", ".avif")
# 进程名判据（小写）。photos.exe = Win10/11「照片」。
_VIEWER_PROCS = {"photos.exe", "photoviewerhost.exe", "honeyview.exe", "irfanview.exe"}


def foreground_is_viewer() -> bool:
    """★ 1.9.1: 前台窗口是不是「图片查看器」（照片 app 等）。

    两个判据（任一命中即真）：
      ① 标题：正好是「照片」，或以常见图片扩展名**结尾**。
         （照片 app 看图时标题就是文件名，如 '149803432_p2.jpg'；
          浏览器标签页标题后面还跟着 ' - xxx'，endswith 天然不会误伤。）
      ② 进程名：已知查看器进程（GetWindowThreadProcessId →
         QueryFullProcessImageNameW）。
    """
    if not IS_WIN:
        return False
    u = _user32()
    if not u:
        return False
    try:
        import ctypes

        hwnd = u.GetForegroundWindow()
        if not hwnd:
            return False
        buf = ctypes.create_unicode_buffer(128)
        u.GetWindowTextW(ctypes.c_void_p(hwnd), buf, 128)
        title = (buf.value or "").strip()
        low = title.lower()
        if title == "照片" or low.endswith(_IMAGE_EXTS):
            return True
        # ② 进程名判据
        try:
            pid = ctypes.c_ulong()
            u.GetWindowThreadProcessId(ctypes.c_void_p(hwnd), ctypes.byref(pid))
            if not pid.value:
                return False
            k32 = ctypes.windll.kernel32
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION,
                                False, int(pid.value))
            if not h:
                return False
            try:
                path_buf = ctypes.create_unicode_buffer(512)
                sz = ctypes.c_ulong(512)
                ok = k32.QueryFullProcessImageNameW(ctypes.c_void_p(h), 0,
                                                     path_buf, ctypes.byref(sz))
                if ok:
                    import os as _os
                    proc = _os.path.basename(path_buf.value or "").lower()
                    return proc in _VIEWER_PROCS
                return False
            finally:
                k32.CloseHandle(ctypes.c_void_p(h))
        except Exception:
            return False
    except Exception as e:
        _log.debug(f"判断查看器失败: {e}")
        return False


def is_cloaked(widget):
    """★ 1.9.1: 问 DWM"你到底有没有在渲染这个窗口"（`DWMWA_CLOAKED`）。

    ★★ 这是**真正对症的探针**，也是"从根本解决"的关键：
      前面所有真机证据都指向同一个矛盾 ——
        · 窗口状态全对（visible / layered / topmost / 没最小化）；
        · 内容也在正常播（`gif=在播 当前帧空=False 标签pixmap=130x130`）；
        · 屏幕上却什么都没有。
      这说明"失效"发生在 **DWM 那一层**，而 Windows 正好有一个 API 直接回答这件事：
        `DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED, ...)`
        —— 非 0 表示 **DWM 没有在合成/呈现这个窗口**（被 cloaked）。
      有了它我们就能**检测到故障本身**，而不是去猜"是不是刚从全屏退出"：
        · 健康时 → 只做廉价、不闪的 `SWP_SHOWWINDOW` 重新声明；
        · 真被 cloaked → 才做 hide→show 重建（会闪，但只在真坏的时候）。

    返回 True/False；拿不到时返回 None（**不确定**，调用方别据此做破坏性动作）。
    """
    if not IS_WIN:
        return None
    try:
        import ctypes
        from ctypes import wintypes

        hwnd = hwnd_of(widget)
        if not hwnd:
            return None
        val = wintypes.DWORD(0)
        # DWMWA_CLOAKED = 14
        hr = ctypes.windll.dwmapi.DwmGetWindowAttribute(
            ctypes.c_void_p(hwnd), ctypes.c_uint(14),
            ctypes.byref(val), ctypes.sizeof(val))
        if hr != 0:
            return None
        return bool(val.value)
    except Exception as e:
        _log.debug(f"查询 cloaked 失败: {e}")
        return None


def _user32():
    if not IS_WIN:
        return None
    try:
        import ctypes
        return ctypes.windll.user32
    except Exception as e:
        _log.debug(f"取 user32 失败: {e}")
        return None


def hwnd_of(widget):
    """拿 QWidget 的原生窗口句柄（int）；拿不到返回 0"""
    try:
        return int(widget.winId())
    except Exception as e:
        _log.debug(f"取 hwnd 失败: {e}")
        return 0


def _get_exstyle(hwnd: int) -> int:
    u = _user32()
    if not u or not hwnd:
        return 0
    try:
        import ctypes
        # 64 位要 GetWindowLongPtrW；32 位上它不存在，退回 GetWindowLongW
        fn = getattr(u, "GetWindowLongPtrW", None) or u.GetWindowLongW
        return int(fn(ctypes.c_void_p(hwnd), GWL_EXSTYLE))
    except Exception as e:
        _log.debug(f"GetWindowLong 失败: {e}")
        return 0


def _set_exstyle(hwnd: int, style: int) -> bool:
    u = _user32()
    if not u or not hwnd:
        return False
    try:
        import ctypes
        fn = getattr(u, "SetWindowLongPtrW", None) or u.SetWindowLongW
        fn(ctypes.c_void_p(hwnd), GWL_EXSTYLE, ctypes.c_void_p(style))
        # ★ 改完扩展样式必须 SWP_FRAMECHANGED，否则系统不重新计算非客户区
        u.SetWindowPos(ctypes.c_void_p(hwnd), 0, 0, 0, 0, 0,
                       SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER
                       | SWP_NOACTIVATE | SWP_FRAMECHANGED)
        return True
    except Exception as e:
        _log.debug(f"SetWindowLong 失败: {e}")
        return False


def apply_noactivate(widget) -> bool:
    """★ 采纳自用户思路 1：给形象窗口加 WS_EX_NOACTIVATE。

    效果（MSDN）：点它**不会把自己变成前台窗口** —— 形象不需要焦点，
    这样点形象不会把你正在用的程序切到后台。
    """
    hwnd = hwnd_of(widget)
    if not hwnd:
        return False
    cur = _get_exstyle(hwnd)
    if cur & WS_EX_NOACTIVATE:
        return True
    ok = _set_exstyle(hwnd, cur | WS_EX_NOACTIVATE)
    _log.info(f"[窗口] 已加 WS_EX_NOACTIVATE（点击形象不抢焦点）hwnd={hwnd} ok={ok}")
    return ok


def apply_composited(widget) -> bool:
    """给形象窗口加 WS_EX_COMPOSITED —— ★ **会被拒绝**，见下面。

    ★★ 1.9.1 实测：`WS_EX_COMPOSITED` 与 `WS_EX_LAYERED` **互斥**。
      真机证据（用户提供的控制台输出）：
        `UpdateLayeredWindowIndirect failed for ptDst=(2337,1245),
         size=(197x197), dirty=(197x197 0,0) (参数错误。)`
      加上它之后，Qt 推像素被 Windows 以 ERROR_INVALID_PARAMETER 拒绝，
      **形象一个像素都上不了屏** —— 比原 bug 还严重。
      → 所以这里**主动拒绝**：检测到窗口是分层窗口（WA_TranslucentBackground）
        就直接拒绝并记一条 warning，绝不设置这个样式。
      → 返回 False 表示"没有加，也不该加"。
    """
    hwnd = hwnd_of(widget)
    if not hwnd:
        return False
    cur = _get_exstyle(hwnd)
    if cur & WS_EX_LAYERED:
        _log.warning(
            "[窗口] 拒绝设置 WS_EX_COMPOSITED：该窗口是分层窗口(WS_EX_LAYERED)，"
            "两者互斥。实测加上它会让 UpdateLayeredWindow 报「参数错误」、"
            "形象完全不显示。")
        return False
    if cur & WS_EX_COMPOSITED:
        return True
    ok = _set_exstyle(hwnd, cur | WS_EX_COMPOSITED)
    _log.info(f"[窗口] 已加 WS_EX_COMPOSITED（实验性）hwnd={hwnd} ok={ok}")
    return ok


def snapshot(widget) -> dict:
    """★ 打一份窗口状态快照 —— 用来把"形象消失"这件事**测准**。

    以前失败时日志里什么都没有，只能靠猜。这份快照回答：
      · 窗口自己还认为可见吗？（IsWindowVisible）
      · 被最小化了吗？（IsIconic）
      · 还是 topmost 吗？（扩展样式的 WS_EX_TOPMOST）
      · 还是分层窗口吗？（WS_EX_LAYERED —— WA_TranslucentBackground 靠它）
      · 几何位置/大小跑哪去了？
      · 现在的前台窗口是谁？（是不是图片查看器还没退干净）
    """
    hwnd = hwnd_of(widget)
    info = {"hwnd": hwnd}
    if not hwnd:
        return info
    u = _user32()
    if not u:
        return info
    try:
        import ctypes
        ex = _get_exstyle(hwnd)
        info["visible"] = bool(u.IsWindowVisible(ctypes.c_void_p(hwnd)))
        info["iconic"] = bool(u.IsIconic(ctypes.c_void_p(hwnd)))
        info["exstyle"] = hex(ex)
        info["layered"] = bool(ex & WS_EX_LAYERED)
        info["topmost"] = bool(ex & WS_EX_TOPMOST)
        info["noactivate"] = bool(ex & WS_EX_NOACTIVATE)
        info["composited"] = bool(ex & WS_EX_COMPOSITED)
        try:
            qt_vis = widget.isVisible()
            info["qt_visible"] = bool(qt_vis)
            info["qt_geometry"] = f"{widget.x()},{widget.y()} {widget.width()}x{widget.height()}"
        except Exception:
            pass
        fg = u.GetForegroundWindow()
        info["foreground"] = int(fg) if fg else 0
        if fg:
            buf = ctypes.create_unicode_buffer(256)
            u.GetWindowTextW(ctypes.c_void_p(fg), buf, 256)
            info["foreground_title"] = buf.value[:80]
        # ★ ctypes.wintypes 是**子模块**，只 import ctypes 拿不到它，要显式 import
        from ctypes import wintypes
        r = wintypes.RECT()
        if u.GetWindowRect(ctypes.c_void_p(hwnd), ctypes.byref(r)):
            info["rect"] = f"{r.left},{r.top} {r.right - r.left}x{r.bottom - r.top}"
    except Exception as e:
        info["error"] = str(e)
    return info


def snapshot_str(widget) -> str:
    """快照的紧凑单行版（写日志用）"""
    s = snapshot(widget)
    if not s.get("hwnd"):
        return "hwnd 取不到"
    order = ("qt_visible", "visible", "iconic", "layered", "topmost", "noactivate",
             "composited", "qt_geometry", "rect", "foreground_title")
    parts = [f"{k}={s[k]}" for k in order if k in s]
    for k in ("exstyle", "foreground", "error"):
        if k in s:
            parts.append(f"{k}={s[k]}")
    return " ".join(parts)


def reassert(widget, topmost: bool = True, reason: str = "") -> bool:
    """★ 重新声明窗口状态（**含 SWP_SHOWWINDOW**）。

    【为什么必须带 SWP_SHOWWINDOW（1.9.1 真机证据修正）】
      真机日志显示：形象不可见时，**应用侧一切正常** ——
        `visible=True layered=True topmost=True`，而且
        `内容快照: gif=在播 当前帧空=False 标签pixmap=130x130`
      也就是 GIF **每帧都在重画**（等于每秒调几十次 UpdateLayeredWindow），
      屏幕上却什么都没有。
      → 结论：**对已存在的分层表面反复重画是没用的**，
        DWM 已经不再呈现这个窗口了；只有**窗口级的"重新显示"**
        （ShowWindow / SWP_SHOWWINDOW）才能让它重新评估并恢复合成。
      我第一版的 reassert **只带了 SWP_FRAMECHANGED，没带 SWP_SHOWWINDOW** ——
      这可能就是"重声明了却没效果"的直接原因。
      `SWP_SHOWWINDOW` 对**已经可见**的窗口是视觉无感的（不会闪），
      所以可以低频反复调。

    顺序（每步独立 try）：
      1. 被最小化 → SW_RESTORE
      2. 重新声明 topmost
      3. **SWP_SHOWWINDOW + SWP_FRAMECHANGED** 强制系统重新评估这个窗口
      4. RedrawWindow 强制整体重画
    """
    if not IS_WIN:
        return False
    hwnd = hwnd_of(widget)
    if not hwnd:
        return False
    u = _user32()
    if not u:
        return False
    before = snapshot_str(widget)
    acted = False
    try:
        import ctypes
        if u.IsIconic(ctypes.c_void_p(hwnd)):
            u.ShowWindow(ctypes.c_void_p(hwnd), 9)   # SW_RESTORE
            acted = True
            _log.warning("[窗口] 形象窗口处于最小化状态 → 已 SW_RESTORE")

        if topmost and not (_get_exstyle(hwnd) & WS_EX_TOPMOST):
            ex = _get_exstyle(hwnd) | WS_EX_TOPMOST
            _set_exstyle(hwnd, ex)
            acted = True
            _log.warning("[窗口] 形象窗口丢了 WS_EX_TOPMOST → 已重新声明")

        # ★ 关键：SWP_SHOWWINDOW —— 窗口级的"重新显示"，DWM 会重新评估合成
        u.SetWindowPos(ctypes.c_void_p(hwnd), HWND_TOPMOST, 0, 0, 0, 0,
                       SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE
                       | SWP_FRAMECHANGED | SWP_SHOWWINDOW)
        u.RedrawWindow(ctypes.c_void_p(hwnd), None, None,
                       RDW_INVALIDATE | RDW_UPDATENOW | RDW_ALLCHILDREN | RDW_FRAME)
        acted = True
    except Exception as e:
        _log.debug(f"[窗口] 重新声明失败: {e}")
        return False

    if acted:
        _log.debug(f"[窗口] 已重新声明窗口状态（{reason or '未注明原因'}）\n"
                   f"        重声明前: {before}\n"
                   f"        重声明后: {snapshot_str(widget)}")
    return acted


# ══════════════ 前台窗口变化监听（思路 2 的现代替代） ══════════════

_hook_handle = None
_hook_cb = None
_hook_thread = None


def install_foreground_hook(callback, skip_own: bool = True):
    """监听**前台窗口变化**（`SetWinEventHook(EVENT_SYSTEM_FOREGROUND)`）。

    为什么用它替代 `WM_DWMCOMPOSITIONCHANGED`：后者在 Win8+ 永远不发（见文件头），
    而"某个程序进/出全屏"在现代 Windows 上最可靠的信号就是**前台窗口变化**。

    `callback` 会在**另一个线程**被调用 —— 别在里面直接碰 GUI，
    必须自己 marshal 回主线程（形象子进程是走队列/信号的老套路）。
    """
    global _hook_handle, _hook_cb, _hook_thread
    if not IS_WIN:
        return False
    if _hook_handle:
        return True
    _hook_cb = callback
    try:
        import ctypes
        from ctypes import wintypes

        WinEventProc = ctypes.WINFUNCTYPE(
            None, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p,
            wintypes.LONG, wintypes.LONG, wintypes.DWORD, wintypes.DWORD)

        def _proc(hook, event, hwnd, id_obj, id_child, thread, time_ms):
            try:
                if _hook_cb:
                    _hook_cb(("foreground", int(hwnd or 0)))
            except Exception as e:
                _log.debug(f"前台事件回调失败: {e}")

        _cb_ref = WinEventProc(_proc)          # ★ 必须留引用，否则被 GC 掉就崩
        u = _user32()
        flags = WINEVENT_OUTOFCONTEXT | (WINEVENT_SKIPOWNPROCESS if skip_own else 0)
        h = u.SetWinEventHook(EVENT_SYSTEM_FOREGROUND, EVENT_SYSTEM_FOREGROUND,
                              None, _cb_ref, 0, 0, flags)
        if not h:
            _log.debug("SetWinEventHook 返回 0，安装失败")
            return False
        _hook_handle = h
        _hook_cb_ref = _cb_ref                 # noqa: F841  （保引用）
        globals()["_hook_cb_ref"] = _cb_ref
        _log.info("[窗口] 已安装前台窗口变化监听（用于全屏进出后自愈）")
        return True
    except Exception as e:
        _log.debug(f"安装前台监听失败: {e}")
        return False


def uninstall_foreground_hook():
    global _hook_handle, _hook_cb
    if _hook_handle:
        try:
            _user32().UnhookWinEvent(_hook_handle)
        except Exception:
            pass
        _hook_handle = None
    _hook_cb = None
