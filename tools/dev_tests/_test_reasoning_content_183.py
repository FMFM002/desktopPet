# -*- coding: utf-8 -*-
"""★ 1.8.3 回归：deepseek-flash 思考模式的 reasoning_content 400。

真实日志证据（logs/prts_20260915_203645.log 第 190-204 行）：
  第2轮 thinking=282字符, tool_calls=1  → file_ops send 执行
  第3轮 thinking=0字符,  content=97, tool_calls=0   ← 空思考！
  边界轮 #1: 追加处理...
  边界轮 #1 失败: 400 The `reasoning_content` in the thinking mode
                  must be passed back to the API.

根因：session.py 第 1095 行 `if thinking_buffer:` 为假 → 第 3 轮落库的
assistant 消息压根没有 reasoning_content 字段；边界轮把整个历史重发 → 400。
（第 966 行注释写的是"要求**每一条** assistant 消息都带回"，但代码只覆盖了
 带 tool_calls 的那一类。）

用法: python tools/dev_tests/_test_reasoning_content_183.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from src.core.session import ChatSession  # noqa: E402

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   {detail}" if detail else ""))


# _prepare_messages 不碰 self，可以脱开 __init__ 直接调（签名里 self 用不到）
prep = lambda msgs: ChatSession._prepare_messages(None, msgs)  # noqa: E731


def tool_call_msg(rid="c1", with_reasoning=True, reasoning="我先查一下文件"):
    m = {"role": "assistant", "content": None,
         "tool_calls": [{"id": rid, "type": "function",
                         "function": {"name": "file_ops", "arguments": "{}"}}]}
    if with_reasoning:
        m["reasoning_content"] = reasoning
    return m


print("== 1. 复现日志场景：空 thinking 的那一轮 assistant 消息 ==")
history = [
    {"role": "system", "content": "你是桌宠"},
    {"role": "user", "content": "把视频发过来"},
    tool_call_msg("c1"),                                  # 第2轮: thinking=282
    {"role": "tool", "tool_call_id": "c1", "content": '{"success": true}'},
    {"role": "assistant", "content": "哼，就这一个视频，早给你翻出来了——"},   # 第3轮: thinking=0
]
out = prep(history)
last = out[-1]
check("最后一条 assistant 已被补上 reasoning_content",
      last.get("role") == "assistant" and last.get("reasoning_content") is not None,
      f"reasoning_content={last.get('reasoning_content')!r}")
check("补的是非 None 的占位值（API 只要求字段存在）",
      isinstance(last.get("reasoning_content"), str))
check("原文 content 没被动过",
      last.get("content") == "哼，就这一个视频，早给你翻出来了——")
check("带 tool_calls 的那条仍有自己的思考内容",
      out[2].get("reasoning_content") == "我先查一下文件",
      repr(out[2].get("reasoning_content")))

print("\n== 2. 普通（非思考）模型不该被塞进多余字段 ==")
plain = [
    {"role": "user", "content": "你好"},
    tool_call_msg("c1", with_reasoning=False),
    {"role": "tool", "tool_call_id": "c1", "content": "ok"},
    {"role": "assistant", "content": "你好呀"},
]
out_plain = prep(plain)
check("工具的 assistant 仍按老行为补占位（兼容旧逻辑）",
      out_plain[1].get("reasoning_content") == " ", repr(out_plain[1].get("reasoning_content")))
check("普通文本 assistant 不会被塞 reasoning_content",
      "reasoning_content" not in out_plain[3], repr(out_plain[3]))

print("\n== 3. 不写脏调用方的 self.messages（深拷贝一层）==")
orig = [
    {"role": "user", "content": "hi"},
    tool_call_msg("c1"),
    {"role": "tool", "tool_call_id": "c1", "content": "ok"},
    {"role": "assistant", "content": "答"},
]
snapshot = [dict(m) for m in orig]
prep(orig)
check("传入的字典列表没被就地修改",
      all(a.get("reasoning_content") == b.get("reasoning_content")
          for a, b in zip(orig, snapshot)),
      str([m.get("reasoning_content") for m in orig]))

print("\n== 4. 残缺 tool_calls 链仍会被丢弃（老行为不能丢）==")
broken = [
    {"role": "user", "content": "hi"},
    tool_call_msg("c9"),                      # 没有对应的 tool 结果
    {"role": "assistant", "content": "答"},
]
out_broken = prep(broken)
check("无 tool 结果的 tool_calls 链被丢弃",
      not any(m.get("tool_calls") for m in out_broken), str(out_broken))
check("正常消息保留", len(out_broken) == 2, f"{len(out_broken)} 条")

print("\n== 5. 边界轮不该在「已给文字回复」后再跑 ==")
src = (ROOT / "src" / "core" / "session.py").read_text(encoding="utf-8")
tail = src.split("# ★ 1.8.3: 已经给出文字回复", 1)
check("标记 reset 已加在文字回复的 break 之前", len(tail) == 2)
if len(tail) == 2:
    after = tail[1][:400]
    check("reset 紧跟 break",
          "tool_just_executed = False" in after and "break" in after,
          after.split("\n")[0:6])
check("except 结构完好（err_name 还在）",
      "err_name = type(e).__name__" in src)
check("_prepare_messages 的旧兜底已扩展为「全部 assistant」",
      "history_uses_reasoning" in src)

print("\n== 6. ★1.9.0 真机 400：tool 结果变成孤儿 / 链中间被塞消息 ==")
# 现场（logs/prts_20260916_211405.log 21:17:35）：
#   Error code: 400 - Messages with role 'tool' must be a response to a
#   preceding message with 'tool_calls'   → 整轮对话失败，用户什么都收不到。
# 库里那条话题的真实样子（chat_records）：只有一串 tool 结果，
# **assistant 的 tool_calls 根本没落库**；而旧实现的"残缺链清理"只丢 assistant、
# 把它的 tool 结果留在列表里 → 孤儿 tool → 400。


def violates_tool_rules(msgs) -> str:
    """校验 OpenAI 兼容接口的两条硬规则，返回 "" 表示合法，否则返回原因。

    规则①：tool 消息必须**紧跟**在带对应 tool_call_id 的 assistant.tool_calls 之后
           （中间不能再夹别的消息）
    规则②：assistant.tool_calls 里的每个 id 都必须有对应的 tool 结果
    """
    open_ids = None
    for m in msgs:
        role = m.get("role")
        if open_ids is not None and role != "tool":
            if open_ids:
                return f"tool_calls 缺结果 {open_ids} 就被 {role} 顶开了"
            open_ids = None
        if role == "assistant" and m.get("tool_calls"):
            open_ids = [tc.get("id") for tc in m["tool_calls"]]
        elif role == "tool":
            if open_ids is None:
                return f"孤儿 tool 消息（tool_call_id={m.get('tool_call_id')}）"
            if m.get("tool_call_id") in open_ids:
                open_ids.remove(m.get("tool_call_id"))
    if open_ids:
        return f"末尾 tool_calls 缺结果 {open_ids}"
    return ""


# ① 真机复现：从库里读回来的历史（tool 结果全成孤儿）
from_db = [
    {"role": "user", "content": "[[FILE:seg1.wav]]"},
    {"role": "tool", "tool_call_id": "A", "content": "音频识别结果"},
    {"role": "tool", "tool_call_id": "B", "content": "dir 命令输出"},
    {"role": "tool", "tool_call_id": "C", "content": "音频识别结果2"},
    {"role": "user", "content": "发个表情包看看"},
]
out_db = prep(from_db)
check("①孤儿 tool 全部被丢掉（不再触发 400）",
      violates_tool_rules(out_db) == "" and
      not any(m.get("role") == "tool" for m in out_db),
      violates_tool_rules(out_db) or f"{len(out_db)} 条")
check("①用户的正文都还在（只丢工具链，不丢对话）",
      [m["role"] for m in out_db] == ["user", "user"], str([m["role"] for m in out_db]))

# ② 残缺链：assistant 声明了 A/B 两个调用，只回来了 A
_partial_assistant = {
    "role": "assistant", "content": None, "reasoning_content": "先查一下",
    "tool_calls": [
        {"id": "A", "type": "function",
         "function": {"name": "file_ops", "arguments": "{}"}},
        {"id": "B", "type": "function",
         "function": {"name": "shell_exec", "arguments": "{}"}},
    ],
}
partial = [
    {"role": "user", "content": "看下文件"},
    dict(_partial_assistant),
    {"role": "tool", "tool_call_id": "A", "content": "结果A"},   # B 的结果丢了
    {"role": "user", "content": "继续"},
]
out_partial = prep(partial)
check("②残缺链连它的 tool 结果一起丢（旧实现只丢 assistant → 留孤儿）",
      violates_tool_rules(out_partial) == ""
      and not any(m.get("role") == "tool" for m in out_partial)
      and not any(m.get("tool_calls") for m in out_partial),
      violates_tool_rules(out_partial) or str([m.get("role") for m in out_partial]))
check("②后面的用户消息不受影响",
      [m for m in out_partial if m.get("role") == "user"][-1]["content"] == "继续")

# ③ 反向对照：旧算法（只丢 assistant、留 tool 结果）在这种输入上必然违规
def _old_prepare(messages):        # noqa: D401  （仅用于反向对照，复刻旧实现）
    out, pending = [], []
    for m in messages:
        m = dict(m)
        role = m.get("role")
        if role == "assistant" and m.get("tool_calls"):
            pending = [tc.get("id") for tc in m["tool_calls"] if isinstance(tc, dict)]
        elif role == "tool":
            tid = m.get("tool_call_id")
            if pending and tid in pending:
                pending.remove(tid)
            else:
                continue
        out.append(m)
    if pending:
        out = [m for m in out
               if not (m.get("role") == "assistant" and m.get("tool_calls"))]
    return out


check("③反向对照：旧实现在同样的输入上会留下孤儿 tool（证明本条测的是真行为）",
      violates_tool_rules(_old_prepare(partial)) != "",
      violates_tool_rules(_old_prepare(partial)))

# ④ 相邻性：链中间被塞了别的消息 → 必须挪到 tool 结果后面
squeezed = [
    {"role": "user", "content": "看下文件"},
    tool_call_msg("A"),
    {"role": "assistant", "content": "（附件卡片：seg1.wav）"},
    {"role": "tool", "tool_call_id": "A", "content": "结果A"},
]
out_squeezed = prep(squeezed)
check("④夹在链中间的消息被挪到 tool 结果之后（保证紧跟）",
      violates_tool_rules(out_squeezed) == "",
      violates_tool_rules(out_squeezed) or str([m.get("role") for m in out_squeezed]))
check("④中间那条内容没丢",
      any(m.get("content") == "（附件卡片：seg1.wav）" for m in out_squeezed))

# ⑤ 正常链的顺序不能被动过
normal = [
    {"role": "user", "content": "hi"},
    tool_call_msg("A"),
    {"role": "tool", "tool_call_id": "A", "content": "ok"},
    {"role": "assistant", "content": "答"},
]
out_normal = prep(normal)
check("⑤正常工具链顺序原样保留",
      [m.get("role") for m in out_normal] == ["user", "assistant", "tool", "assistant"],
      str([m.get("role") for m in out_normal]))
check("⑤所有输出都满足接口的两条硬规则（不变式）",
      all(violates_tool_rules(prep(c)) == ""
          for c in (from_db, partial, squeezed, normal)))

print(f"\n==== 结果: {len(PASS)} PASS / {len(FAIL)} FAIL ====")
for f in FAIL:
    print("  FAIL:", f)
sys.exit(1 if FAIL else 0)
