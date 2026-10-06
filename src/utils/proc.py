# -*- coding: utf-8 -*-
"""★ 1.9.4：起子进程时**别弹控制台黑窗口**（Windows 专有问题的统一出口）。

用户真机两次报告：
  · "你的这一步动作导致我的屏幕不停的闪现cmd黑色窗口"
  · "**弹出了更频繁的cmd窗口，要把我闪瞎了。**"

根因：Windows 上 `subprocess` 起控制台程序（python / powershell / nvidia-smi /
tesseract / aria2c …）**默认会新开一个控制台窗口**，即使你用了
`capture_output=True` 也一样 —— 必须在 `creationflags` 里给 `CREATE_NO_WINDOW`。

用法：
    from src.utils.proc import no_window_kwargs as _nw
    subprocess.run(cmd, ..., **_nw())

非 Windows 返回空 dict（`creationflags` 是 Windows 专有参数，别的平台传了会报错）。
"""
from __future__ import annotations


def no_window_kwargs() -> dict:
    """Windows 上返回 `{"creationflags": CREATE_NO_WINDOW}`，其它平台返回 `{}`。"""
    import sys
    if sys.platform != "win32":
        return {}
    # CREATE_NO_WINDOW = 0x08000000：不给子进程建控制台窗口
    return {"creationflags": 0x08000000}
