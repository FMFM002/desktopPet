"""
★ 1.9.0: 表情包的"待发送"信箱（工具 ↔ 主窗口之间的中转）。

【为什么单独开一个模块，而不是写在 meme_tool 里】
  真机踩的坑（日志证据）：
      [01:55:33] 工具 [meme] 清掉同名 sys.modules 占用   ← 每次调用都重新 import
      [01:56:04] 工具 [meme] 卸载（空闲超时）           ← 模块被卸载
      [01:56:07] 工具 [meme] 清掉同名 sys.modules 占用   ← 又是新的模块对象
  ToolManager 对 LAZY 工具是"用完/空闲就卸载、下次再加载"的，所以
  **写在 tools/meme_tool.py 里的全局变量活不过一次卸载**：
  工具把待发送塞进模块 A，主窗口 `import tools.meme_tool` 拿到的是模块 B，
  `take_pending()` 永远是空的 → 表情包一张都发不出来。

  放在 src/core/ 下就没事：ToolManager 只清"同名"的工具模块，
  这个信箱模块不属于任何工具，从进程启动到退出都是同一个对象。

【职责】
  · 工具侧：pick() 塞一张（并记"本轮已选过"）；每轮由主窗口 reset()
  · 主窗口侧：take() 取走并在回复结束后发送；peek()/was_picked() 查询
"""
from __future__ import annotations

import logging

_log = logging.getLogger("MemeBus")

_pending: dict = {}
_picked_this_turn: bool = False


def reset_turn():
    """新一轮开始（主窗口在收到用户消息时调用）"""
    global _pending, _picked_this_turn
    _pending = {}
    _picked_this_turn = False


def was_picked_this_turn() -> bool:
    return _picked_this_turn


def pick(path, name: str = "", tag: str = "", reason: str = "",
         desc: str = "") -> dict:
    """工具侧：把挑好的表情包放进待发送

    ★ 1.9.2：多带一个 desc（表情包描述）—— 主窗口日志里能看出"这次发的图
    画的是什么"，排查"发了张莫名其妙的图"时有据可查。
    """
    global _pending, _picked_this_turn
    if _picked_this_turn:
        return {}
    _pending = {"path": str(path), "name": name or str(path).rsplit("\\", 1)[-1].rsplit("/", 1)[-1],
                "tag": tag or "", "reason": reason or "", "desc": desc or ""}
    _picked_this_turn = True
    _log.info(f"[表情包信箱] 待发送: {_pending['name']} (tag={tag or '-'}"
              f"{', 描述=' + desc if desc else ''})")
    return dict(_pending)


def peek() -> dict:
    return dict(_pending)


def take() -> dict:
    """主窗口侧：取走待发送（回复结束后调用）"""
    global _pending
    p, _pending = _pending, {}
    return p


def clear():
    """只清待发送，不动"本轮已选过"标记"""
    global _pending
    _pending = {}
