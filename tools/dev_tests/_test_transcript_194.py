# -*- coding: utf-8 -*-
"""★ 1.9.4：**全量对话记录（transcript）** 守卫。

## 由来（用户 2026-10-06）
  用户报"聊天回复有 bug，汇报应该是调用工具后的，不是每次都强制，现在这样显得混乱"，
  但接着说："**又聊了几句，发现恢复正常了，暂时无法复现。强化log，最好全量记录。**"

  ⇒ **复现不了的问题只能靠事发现场的证据。** 所以要保证：
     模型收到了什么、说了什么、调了什么工具、参数什么、结果什么、
     临时汇报发了几条 —— **都落盘，且找得到**。

## 钉什么
  A. 全量记录写进**独立文件**（不淹没在主日志 800+ 行噪音里），且**一行一条**；
  B. ★★ **密钥必须打码** —— 参数里 `key/token/cookie/secret…` 这类名字，
     值**绝不能原样落盘**（这个文件是要发给别人看的）；
  C. 超长字段**截断并注明截掉多少**（不做"悄悄丢弃"）；
  D. ★ 记录失败**绝不影响桌宠运行**（任何异常都吞掉）；
  E. 三个关键点真的接上了：**工具调用（含参数值）/ 工具结果 / 临时汇报**；
  F. ★ **反向守卫**：确认"打码"不是把什么值都变成 `…（已打码）`
     （普通值必须原样保留，否则这日志就没用了）。
"""
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PRTS_NO_COMFY_START", "1")
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   {detail}" if detail else ""))


from src.utils import transcript as T                   # noqa: E402

print("== A. 独立文件、一行一条 ==")
_p = T.transcript_path()
check("★路径是 logs/transcript_*.log（**独立**于主日志）",
      _p.name.startswith("transcript_") and _p.suffix == ".log"
      and _p.parent.name == "logs", str(_p))
check("★文件名带日期（便于按天取证据）", len(_p.stem.split("_")) >= 2)

print("\n== B/C. 密钥打码 + 超长截断 ==")
_T0 = T.next_turn()
T.tool_call("demo", {"city": "上海", "api_key": "sk-abcdef1234567890",
                     "refresh_token": "Zr6LiGverylongtoken", "cookie": "abcxyz"})
T.tool_result("demo", "x" * 5000)
_txt = _p.read_text(encoding="utf-8")
check("★★API key **没有原样落盘**（只留前缀 + 打码标记）",
      "sk-abcdef1234567890" not in _txt and "已打码" in _txt)
check("★★token / cookie 也打码了",
      "Zr6LiGverylongtoken" not in _txt and "abcxyz" not in _txt)
check("★超长字段**截断并注明截掉多少**（不是悄悄丢）",
      "此处截掉" in _txt)
check("★而且截断后不会把整个文件撑爆",
      len(_txt.splitlines()[-1]) < 6000)

print("\n== F. ★ 反向守卫：普通值必须**原样保留**（否则日志没用）==")
check("★★普通参数（city=上海）**原样在**（打码不能打过头）",
      "city=上海" in _txt, "（若这条红，说明打码把普通值也吞了）")

print("\n== D. 记录失败绝不影响桌宠 ==")
_orig = T._write


def _boom(*a, **k):
    raise RuntimeError("模拟落盘失败")


T._write = _boom
try:
    T.chat_bubble("user", "试试")
    T.tool_call("x", {"a": 1})
    T.progress("试试", True)
    check("★★落盘抛异常时，调用方**不被带崩**（内部吞掉）", True)
except Exception as e:
    check("★★落盘抛异常时，调用方**不被带崩**（内部吞掉）", False,
          f"漏出来了: {type(e).__name__}: {e}")
finally:
    T._write = _orig

print("\n== E. 三个关键点真的接上了 ==")
for _rel, _need, _what in (
        ("src/core/tool_manager.py", "transcript", "工具调用/结果"),
        ("src/core/progress_bus.py", "transcript", "临时汇报投递"),
        ("src/core/session.py", "transcript", "用户消息/模型回复")):
    _s = (ROOT / _rel).read_text(encoding="utf-8")
    check(f"★★{_what} 接上了全量记录（{_rel}）", _need in _s)
_s_tm = (ROOT / "src/core/tool_manager.py").read_text(encoding="utf-8")
check("★★工具调用记的是**参数值**（不是只有参数名）",
      "tool_call(tool_name, kwargs)" in _s_tm)
check("★★工具结果也记（主日志只留前 100 字，翻不出问题）",
      "tool_result(tool_name, result" in _s_tm)

print("\n== G. ★★★ 一轮最多一条「临时汇报」（用户实测的 bug）==")
# 用户 2026-10-06 附**实际对话**为证：查一次天气冒出两条汇报，第一条还是
# "我正在陪你聊天呢，稍等一下～"（跟查天气无关）。
# 用户原话："**汇报应该是调用工具后的，不是每次都强制，现在这样显得混乱。**"
from src.core import progress_bus as P                   # noqa: E402
P.reset_turn()
_sent = []
_orig_sink = P._sink
try:
    P.set_sink(lambda t: _sent.append(t))
    # ★★ 1.9.4（用户要求）：**代码级限制 —— 工具连续调用 < 3 次不许汇报**。
    #   所以这里必须先"调够 3 次工具"，否则第一条也会被拒（守卫本身要跟上新规矩）。
    check("★★★工具调用 <3 次时，汇报**一律拒绝**（用户要的代码级限制）",
          P.report("短任务不该汇报") is False and not _sent,
          f"sent={_sent}")
    for _ in range(3):
        P.note_tool_called()
    # ★★ 1.9.4（2026-10-21）：现在还要求"本轮至少跑了 20 秒" —— 测试里把起点往前挪
    P._turn_started_at -= 60
    _r1 = P.report("第一条")
    _r2 = P.report("第二条（应被拒）")
    check("★★★调够 3 次后**一轮只放行一条**（第二条被拒且不投递）",
          _r1 is True and _r2 is False and _sent == ["第一条"], f"sent={_sent}")
    P.reset_turn()
    check("★★reset_turn 把**工具计数也清零**"
          "（否则上一轮的调用数会累加，短任务被误判成'调够了'）",
          P.tools_this_turn() == 0, str(P.tools_this_turn()))
    for _ in range(3):
        P.note_tool_called()
    P._turn_started_at -= 60
    _r3 = P.report("新一轮第一条")
    check("★★**新一轮能重新发**（限流是按轮算的，不是全局只许一条）",
          _r3 is True and _sent[-1] == "新一轮第一条")

    # ★★★ 2026-10-21 真机：查天气那一轮只花了 7 秒，却因为顺手调了个表情包
    #   凑够 3 次工具就放行 → 在答案之后又冒出一条"马上帮你查"，随后答案重复。
    #   ⇒ 两条新硬限制：**跑够 20 秒** + **本轮还没开口**。
    P.reset_turn()
    for _ in range(3):
        P.note_tool_called()
    check("★★★只跑了 0 秒（<20s）→ 拒绝汇报（7 秒的短任务不算长任务）",
          P.report("太快的短任务") is False)
    P._turn_started_at -= 60
    P.note_visible_text()
    check("★★★本轮**已经说过话** → 拒绝汇报（避免插在答案里造成重复）",
          P.report("都说完了还汇报") is False)
    P.reset_turn()
    for _ in range(3):
        P.note_tool_called()
    P._turn_started_at -= 60
    check("★★两条都满足（跑够 20 秒 + 还没开口）→ 正常放行",
          P.report("长任务汇报") is True)
    check("★reset_turn 把「是否已开口」也清零", P.visible_text_this_turn() is False)
    check("★★第二轮起会记一笔「被拒(一轮一条)」到全量记录（方便日后核对）"
          "—— ★ 标签必须**专属**：原来复用 `progress(delivered=False)`，"
          "而它渲染成「排队」，跟「丢弃」混在一起，**把排查的我误导过一次**",
          "临时汇报/被拒(一轮一条)" in _p.read_text(encoding="utf-8"))
finally:
    P._sink = _orig_sink
    P.reset_turn()

_s_lc = (ROOT / "src/core/llm_client.py").read_text(encoding="utf-8")
check("★★提示词**明确排除了「一次工具调用就能完成的事」**"
      "（查天气/查视频/读文件/问游戏数据）—— 原文把「查资料」也算进多步任务，"
      "于是一次调用也会触发",
      "一次工具调用就能出结果" in _s_lc and "查天气" in _s_lc)
check("★提示词里写了「一轮最多说一句」", "一轮最多说一句" in _s_lc)

print(f"\n==== 结果: {len(PASS)} PASS / {len(FAIL)} FAIL ====")
for f in FAIL:
    print("  FAIL:", f)
sys.exit(1 if FAIL else 0)
