"""验证会话消息体检：思考模式 reasoning_content 补齐 + 残缺工具链清理"""
import os
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE))
os.environ.setdefault("DEEPSEEK_API_KEY", "test")

import src.core.database as _dbmod
_TMP = BASE / "_test_tmp_dbs"
_TMP.mkdir(exist_ok=True)
_dbmod.DB_DIR = _TMP

ok = True
def check(name, cond, extra=""):
    global ok
    print(("  [PASS] " if cond else "  [FAIL] ") + name + (f"  {extra}" if extra else ""))
    if not cond:
        ok = False


from src.core.session import ChatSession
s = ChatSession()

print("== 1. 带 tool_calls 的 assistant 缺 reasoning_content ==")
msgs = [
    {"role": "user", "content": "帮我看看"},
    {"role": "assistant", "content": None,
     "tool_calls": [{"id": "call_1", "type": "function",
                     "function": {"name": "file_ops", "arguments": "{}"}}]},
    {"role": "tool", "tool_call_id": "call_1", "content": "结果"},
    {"role": "assistant", "content": "好了"},
]
out = s._prepare_messages(msgs)
tc_msgs = [m for m in out if m.get("role") == "assistant" and m.get("tool_calls")]
check("保留了工具链", len(tc_msgs) == 1 and len(out) == 4, f"len={len(out)}")
check("自动补齐 reasoning_content",
      all("reasoning_content" in m for m in tc_msgs),
      str(tc_msgs[0].get("reasoning_content")))

print("== 2. 残缺链（有 tool_calls 无 tool 结果）应被丢弃 ==")
msgs2 = [
    {"role": "user", "content": "x"},
    {"role": "assistant", "content": None,
     "tool_calls": [{"id": "call_a", "type": "function",
                     "function": {"name": "shell_exec", "arguments": "{}"}}]},
    {"role": "assistant", "content": "后面直接说话了"},
]
out2 = s._prepare_messages(msgs2)
check("丢弃了残缺的 tool_calls 消息",
      not any(m.get("tool_calls") for m in out2), str(out2))
check("其余消息保留", len(out2) == 2, f"len={len(out2)}")

print("== 3. 孤儿 tool 结果应被丢弃 ==")
msgs3 = [
    {"role": "user", "content": "x"},
    {"role": "tool", "tool_call_id": "ghost", "content": "孤儿"},
    {"role": "assistant", "content": "ok"},
]
out3 = s._prepare_messages(msgs3)
check("孤儿 tool 被丢弃", not any(m.get("role") == "tool" for m in out3), str(out3))

print("== 4. 已经带 reasoning_content 的不被改动 ==")
msgs4 = [
    {"role": "assistant", "content": None, "reasoning_content": "我思考了一下",
     "tool_calls": [{"id": "c9", "type": "function",
                     "function": {"name": "f", "arguments": "{}"}}]},
    {"role": "tool", "tool_call_id": "c9", "content": "r"},
]
out4 = s._prepare_messages(msgs4)
check("原 reasoning_content 未被覆盖",
      out4[0].get("reasoning_content") == "我思考了一下")

print("== 5. 普通对话不受影响 ==")
msgs5 = [{"role": "user", "content": "你好"},
         {"role": "assistant", "content": "你也好"}]
check("普通消息原样返回", s._prepare_messages(msgs5) == msgs5)

print("== 6. 不修改调用方原始列表 ==")
msgs6 = [{"role": "assistant", "content": None,
          "tool_calls": [{"id": "c1", "type": "function",
                          "function": {"name": "f", "arguments": "{}"}}]},
         {"role": "tool", "tool_call_id": "c1", "content": "r"}]
before = [dict(m) for m in msgs6]
s._prepare_messages(msgs6)
check("原始 message 未被写脏",
      "reasoning_content" not in msgs6[0], str(msgs6[0]))

try:
    s.db.delete_topic(s.topic_id)
except Exception:
    pass

print("\n结果:", "ALL PASS" if ok else "FAILED")
sys.exit(0 if ok else 1)
