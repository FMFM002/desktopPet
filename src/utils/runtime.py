# -*- coding: utf-8 -*-
"""★★ 1.9.4：**冻结（PyInstaller exe）环境判定** —— 一个地方回答"这是不是 exe 版"。

## 为什么需要单独一个文件
打包成 exe 之后，有**三类事情**和源码运行不一样，而它们散落在各个模块里：

  1. **没有 Python 解释器可用** —— `sys.executable` 变成了 `PRTS-DesktopPet-*.exe`
     本身。任何 `subprocess.Popen([sys.executable, "xxx.py", ...])` 都会变成
     "**再启动一个桌宠**"，而不是"跑那个脚本"。
     （踩点：`src/ui/first_run_wizard.py` 的模型下载、`src/core/account_login.py`
      的 `pip install` 提示。）
  2. **依赖是"打包时定死"的** —— 装不了新包，所以"缺依赖请 pip install"
     这种提示在 exe 里是**错的**，要说"这个功能请用便携 zip 版"。
  3. **根目录就在 exe 旁边** —— `PRTS.spec` 用了 `contents_directory="."`，
     所以 `sys._MEIPASS` **就是** exe 所在目录；本文件把它统一成 `app_dir()`。

★ 这里的判定**只做判断，不做决策**：具体怎么降级由调用方决定（并写清给用户看的话）。
"""
from __future__ import annotations

import sys
from pathlib import Path


def is_frozen() -> bool:
    """是不是 PyInstaller 打出来的 exe（源码运行 = False）。"""
    return bool(getattr(sys, "frozen", False))


def app_dir() -> Path:
    """程序根目录。

    · 源码运行 → 仓库根（`<repo>/src/utils/runtime.py` 往上三层）
    · exe      → exe 所在目录（spec 的 `contents_directory="."` 保证 `_MEIPASS`
                 就是这一层，而不是 `_internal/`）
    """
    if is_frozen():
        base = getattr(sys, "_MEIPASS", None)
        if base:
            return Path(base)
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def has_python() -> bool:
    """能不能"再起一个 python 去跑 .py 脚本"。

    ★ exe 版里 `sys.executable` 是 exe 自己 —— 用它跑脚本等于**再开一个桌宠**。
      所以调用方必须先问这一句，别直接拼 `[sys.executable, script]`。
    """
    return not is_frozen()


def frozen_feature_hint(feature: str) -> str:
    """统一的一句话：这个功能在 exe 里为什么没有、该怎么用。

    口径（用户要求"界面上说清楚，不许崩、也不许假装没装"）：
      · 说清 **exe 里没有**；
      · 给出**能做到的替代路径**（便携 zip 版 / 手动放文件）。
    """
    return (f"⚠ 「{feature}」在 **exe 版**里不可用（exe 不带 Python 环境，"
            f"也没有内置浏览器驱动）。\n"
            f"     需要它请用**便携 zip 版**（解压后双击 启动.bat）：\n"
            f"     在 zip 版里完成一次，然后把结果拷到 exe 目录旁边即可 —— "
            f"两边用的是同一套文件。")
