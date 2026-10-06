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
_tools_this_turn: int = 0

# ★★★ 1.9.4（用户 2026-10-06 要求 —— **代码级硬限制，不靠提示词**）：
#   "临时汇报这个功能是在**长任务**中，为了不让用户干等，让用户知道 LLM 在做什么的。
#    希望加入**代码级别的限制**，比如**工具连续调用小于三个，就不要临时汇报**。"
#   ⇒ 本轮工具调用数 **< `_MIN_TOOL_CALLS`** 时，`report()` **一律拒绝**。
#   ★ 为什么必须放在代码里：提示词只是"建议"，模型可以不遵守
#     （真机就发生过：一次查天气发了两条汇报）。**代码说不行就是不行。**
_MIN_TOOL_CALLS = 3


def note_tool_called():
    """★ 工具侧（`tool_manager`）每执行一次工具就调一次。

    支撑上面那条**代码级限制**："工具连续调用 < 3 次不许临时汇报"。
    """
    global _tools_this_turn
    _tools_this_turn += 1


def tools_this_turn() -> int:
    """本轮到目前为止调了几次工具（给界面/诊断看）。"""
    return _tools_this_turn


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
    global _count_this_turn          # ★ 必须在使用之前声明（Python 语法要求）
    text = (text or "").strip()
    if not text:
        return False
    # ★★★ 1.9.4（用户要求，**代码级限制**）：**工具连续调用 < 3 次 → 不许汇报**。
    #   他说这功能是"给**长任务**用的，别让人干等"，而查一次天气（1 次工具调用）
    #   根本不需要汇报 —— 那只会变成噪音。
    #   ⇒ 放在**代码**里，不放在提示词里：提示词是建议，模型可以不遵守。
    if kind == "progress" and _tools_this_turn < _MIN_TOOL_CALLS:
        _log.info(f"[临时汇报] 本轮才调了 {_tools_this_turn} 次工具"
                  f"（<{_MIN_TOOL_CALLS}）→ 按「短任务不汇报」的规矩**拒绝**: "
                  f"{text[:40]}")
        try:
            from src.utils import transcript as _T1
            _T1.note("临时汇报/短任务被拒",
                     f"本轮工具调用 {_tools_this_turn} 次（<{_MIN_TOOL_CALLS}），"
                     f"内容={text[:60]}")
        except Exception:
            pass
        return False
    # ★★★ 1.9.4 真机修复（用户 2026-10-06，附**实际对话**为证）：
    #   他查一次天气，聊天里出现了**两条**临时汇报：
    #       重庆
    #       → 我正在陪你聊天呢，稍等一下～            ← 汇报 1（跟查天气毫无关系）
    #       → 正在看重庆相关的资料，马上给你整理～    ← 汇报 2
    #       → 重庆今天是多云呢，气温 24°C…            ← 真正的答案
    #   用户原话："**汇报应该是调用工具后的，不是每次都强制，现在这样显得混乱。**"
    #   ⇒ **一轮只放行一条临时汇报**（原来没有上限，模型想发几条发几条）。
    #     第二条起直接**拒掉并且不投递**，让模型把话留到最终回答里。
    if kind == "progress" and _count_this_turn >= 1:
        _log.info(f"[临时汇报] 本轮已有 {_count_this_turn} 条，"
                  f"按「一轮只放一条」的规矩**拒绝**这条: {text[:40]}")
        try:
            from src.utils import transcript as _T0
            _T0.progress(text, delivered=False)
            _T0.note("临时汇报/被限流", f"本轮第 {_count_this_turn + 1} 条被拒")
        except Exception:
            pass
        return False
    _count_this_turn += 1
    if _sink is not None:
        try:
            _sink(text)
            _log.info(f"[临时汇报] 已投递（{kind}，本轮第 {_count_this_turn} 条）: "
                      f"{text[:60]}")
            # ★★ 1.9.4（用户："强化log，最好全量记录"）：
            #   他报的正是"汇报每次都出现，显得混乱"——所以**每一次投递**都要有据可查。
            try:
                from src.utils import transcript as _T
                _T.progress(text, delivered=True)
            except Exception:
                pass
            return True
        except Exception as e:
            _log.warning(f"[临时汇报] 投递失败，改为排队: {e}")
    _queue.append(text)
    _log.info(f"[临时汇报] 已排队（{kind}）: {text[:60]}")
    try:
        from src.utils import transcript as _T2
        _T2.progress(text, delivered=False)
    except Exception:
        pass
    return True



def reset_turn():
    """新一轮开始（主窗口在收到用户消息时调用）。"""
    global _queue, _count_this_turn, _tools_this_turn
    _queue = []
    _count_this_turn = 0
    # ★ 工具计数也要清零 —— 否则上一轮的调用数会**累加到下一轮**，
    #   短任务就会误判成"已调够 3 次"，那条限制等于形同虚设。
    _tools_this_turn = 0


def count_this_turn() -> int:
    return _count_this_turn
