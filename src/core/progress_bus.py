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

# ★★★ 1.9.4 真机第三轮（用户 2026-10-21，附聊天截图）：
#   "简单任务也临时汇报，导致回复重复。"
#   日志证据（查天气那一轮）：system_command → weather_query → **meme** 凑够 3 次工具，
#   于是**在答案已经说出去之后**又投递了一条"铃兰在收拾东西，马上帮你查…"，
#   随后模型再答一遍 → 聊天里三条气泡、语义重复。
#   ⇒ 光数工具次数不够，再加两条**代码级**硬限制：
#     ① **本轮至少跑了 `_MIN_SECONDS` 秒**（7 秒的短任务不算"长任务"）；
#     ② **本轮还没向聊天窗口说过任何可见的话**（已经开口了，就不该再"汇报"）。
_MIN_SECONDS = 20.0

_turn_started_at: float = 0.0
_visible_text_this_turn: bool = False


def note_visible_text():
    """会话层每向聊天窗口送出一次正文就调一次（见 session 的 `_emit_text`）。

    用途：**已经在说话了就别再"临时汇报"** —— 那只会插在答案前后造成重复。
    """
    global _visible_text_this_turn
    _visible_text_this_turn = True


def visible_text_this_turn() -> bool:
    return _visible_text_this_turn


def elapsed_this_turn() -> float:
    import time as _t
    return max(0.0, _t.time() - _turn_started_at) if _turn_started_at else 0.0


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
    # ★★ 1.9.4（真机 2026-10-21）：**这轮已经说过话了** → 拒绝汇报。
    #   查天气那个例子：模型先说"查好啦…"，随后又"汇报"一句"马上帮你查"，
    #   时序颠倒、内容重复。既然已经开口，就不需要"别让用户干等"了。
    if kind == "progress" and _visible_text_this_turn:
        _log.info(f"[临时汇报] 本轮**已经向用户说过话** → 拒绝（避免插在答案里）: "
                  f"{text[:40]}")
        try:
            from src.utils import transcript as _T0
            _T0.note("临时汇报/已开口被拒", text)
        except Exception:
            pass
        return False
    # ★★ 1.9.4（真机 2026-10-21）：**跑得还不够久** → 拒绝。
    #   查天气整轮只有 7 秒，靠"顺手调了个表情包"凑够 3 次工具就放行 —— 那不是长任务。
    if kind == "progress" and elapsed_this_turn() < _MIN_SECONDS:
        _log.info(f"[临时汇报] 本轮只跑了 {elapsed_this_turn():.1f} 秒"
                  f"（<{_MIN_SECONDS:.0f}）→ 按「短任务不汇报」**拒绝**: {text[:40]}")
        try:
            from src.utils import transcript as _T1b
            _T1b.note("临时汇报/短任务被拒",
                      f"本轮 {elapsed_this_turn():.1f} 秒（<{_MIN_SECONDS:.0f}），"
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
            # ★ 1.9.4 修：原来这里也调 `_T0.progress(text, delivered=False)`，
            #   而那个函数把 `False` 一律渲染成「**排队**」—— 于是**被拒的**在
            #   transcript 里也写着"排队"，把排查的人（我）**误导**成"没有 sink、
            #   全攒起来了一次性倒出"。实际 prts 日志证明限流是生效的。
            #   ⇒ 拒绝用**专属标签**，别再和"排队"混。
            _T0.note("临时汇报/被拒(一轮一条)", text)
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
            _log.warning(f"[临时汇报] 投递失败（不再排队，直接丢弃）: {e}")
    # ★★★ 1.9.4 真机修复（用户 2026-10-06）—— **"语言混乱"的真凶**：
    #   真机 transcript 里同一轮出现了 **6 组**这样的配对：
    #       [T6] [临时汇报/排队] 正在翻素材，准备排个好看的PDF。
    #       [T6] [临时汇报/被限流] 本轮第 2 条被拒
    #   `排队` = **当时没有 sink（没人接收）** → 它们**全攒在 `_queue` 里**，
    #   等有 sink 时**一次性倒出来** → 聊天窗口被一串过期废话刷屏。
    #   ⇒ **临时汇报的意义就是「立刻让用户看到」**；攒起来以后一起倒
    #     **完全违背初衷**，而且完全是噪音。
    #   ⇒ 改为：**没有 sink 就丢弃**（并在全量记录里留一笔，便于日后核对）。
    #     原来那句"没有 sink 才排队（不会丢）"是好意，但实测证明**有害**。
    _log.info(f"[临时汇报] 当前没有接收方 → **丢弃**（不排队，避免攒批刷屏）: "
              f"{text[:40]}")
    try:
        from src.utils import transcript as _T2
        _T2.progress(text, delivered=False)
        _T2.note("临时汇报/丢弃", "当时没有 sink（接收方），按新规矩不再排队")
    except Exception:
        pass
    return False



def reset_turn():
    """新一轮开始（主窗口在收到用户消息时调用）。"""
    global _queue, _count_this_turn, _tools_this_turn
    global _turn_started_at, _visible_text_this_turn
    _queue = []
    _count_this_turn = 0
    # ★ 工具计数也要清零 —— 否则上一轮的调用数会**累加到下一轮**，
    #   短任务就会误判成"已调够 3 次"，那条限制等于形同虚设。
    _tools_this_turn = 0
    # ★ 计时与"是否已开口"同样按轮清零（见上面 _MIN_SECONDS 的注释）
    import time as _t
    _turn_started_at = _t.time()
    _visible_text_this_turn = False


def count_this_turn() -> int:
    return _count_this_turn
