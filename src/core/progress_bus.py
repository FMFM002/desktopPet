"""★ 1.9.2（R69，用户设计）：LLM 的「临时汇报」通道（工具 ↔ 主窗口）。

【用户的原始设计意图】
  不要做死板的"分阶段输出流水线"（那会让日常简短聊天也变慢）。
  只在提示词里写清"处理复杂任务时，在合适的时候汇报一次情况，别让用户等久了"，
  然后**开一条通道**让 LLM 能把"临时汇报"发出来 —— 这就是本模块。

【为什么单独放 src/core/，而不是写在工具里】
  照抄 `meme_bus` 踩过的坑：ToolManager 对工具是"空闲就卸载、下次再加载"的，
  **写在 `tools/xxx_tool.py` 里的全局变量活不过一次卸载** ——
  工具把汇报塞进模块 A，主窗口 `import` 拿到的是模块 B，通道永远取不到东西。
  放 `src/core/` 下则从进程启动到退出都是同一个对象。

【职责】
  · 工具侧（tools/progress_report_tool.py）：`report(text)` 立刻投递一句话
  · 主窗口侧：`set_sink(fn)` 注册投递函数（一般是"把这句话显示到聊天窗口"）
  · 兜底：没注册 sink 时先排队，主窗口空下来可以 `take_all()` 取走
"""
from __future__ import annotations

import logging

_log = logging.getLogger("ProgressBus")

_queue: list = []
_sink = None
_count_this_turn: int = 0


def set_sink(fn):
    """主窗口启动时注册：`fn(text) -> None`，把这条汇报显示给用户。"""
    global _sink
    _sink = fn
    _log.debug(f"[临时汇报] 投递通道已注册: {bool(fn)}")


def report(text: str, kind: str = "progress") -> bool:
    """工具侧调用：投递一句临时汇报。返回是否成功送出。

    ★ 有 sink 就**立刻**送出去（用户马上看到，这才是"别让人等久了"的意义）；
      没有 sink 才排队（不会丢）。
    """
    text = (text or "").strip()
    if not text:
        return False
    global _count_this_turn
    _count_this_turn += 1
    if _sink is not None:
        try:
            _sink(text)
            _log.info(f"[临时汇报] 已投递（{kind}，本轮第 {_count_this_turn} 条）: "
                      f"{text[:60]}")
            return True
        except Exception as e:
            _log.warning(f"[临时汇报] 投递失败，改为排队: {e}")
    _queue.append(text)
    _log.info(f"[临时汇报] 已排队（{kind}）: {text[:60]}")
    return True


def take_all() -> list:
    """主窗口侧：取走所有排队中的汇报（取完清空）。"""
    global _queue
    out, _queue = _queue, []
    return out


def reset_turn():
    """新一轮开始（主窗口在收到用户消息时调用）。"""
    global _queue, _count_this_turn
    _queue = []
    _count_this_turn = 0


def count_this_turn() -> int:
    return _count_this_turn
