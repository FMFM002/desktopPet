# -*- coding: utf-8 -*-
"""★★ 1.9.4：**全项目唯一的版本源**。

## 为什么要这个文件
在 1.9.4 之前，版本号是**散落硬编码**的，而且互相不一致（真机取证）：

    README.md:1   # 铃兰桌宠（PRTS Desktop Pet）v1.8.9
    main.py:79    log.info("铃兰桌宠 v1.5.0 启动中...")

两个数字都落后于实际版本，还彼此矛盾 —— 用户看到 README 写 1.8.9、
启动日志写 1.5.0、开发日志写 1.9.3，谁也不知道装的是哪个。

## 规矩（从本版开始）
  · **要显示版本的地方，一律 `from src.version import __version__`**，
    不再写字面量。
  · `_test_version_193.py` 会扫全仓，**发现散落的 `vX.Y.Z` 字面量就报红**
    （白名单见那个套件；注释里的历史标记「★ 1.9.3 第 N 轮」不算 —— 那是变更记录，
     不是"当前版本"）。

## 怎么发版
改 `__version__` 一处即可；README 标题与启动日志都跟着变。
"""
from __future__ import annotations

# ── 版本号：**唯一来源** ──
__version__ = "1.9.4"

# ── 名称与标识（安装包 / 窗口标题 / 数据目录都用它，避免各处写法不一致）──
APP_NAME = "铃兰桌宠"
APP_NAME_EN = "PRTS Desktop Pet"
# 英文标识：给安装包、注册表、目录名用（不要带中文与空格）
APP_ID = "PRTS-DesktopPet"

# ── 展示用字符串 ──
TITLE = f"{APP_NAME}（{APP_NAME_EN}）v{__version__}"
STARTUP_LINE = f"{APP_NAME} v{__version__} 启动中..."


def version_tuple() -> tuple:
    """把 `1.9.4` 拆成 `(1, 9, 4)` —— 打包脚本/安装器比较版本时会用。"""
    parts = []
    for seg in __version__.split("."):
        num = "".join(ch for ch in seg if ch.isdigit())
        parts.append(int(num) if num else 0)
    return tuple(parts)
