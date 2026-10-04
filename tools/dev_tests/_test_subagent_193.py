# -*- coding: utf-8 -*-
"""★ 1.9.3 第 59 轮：**子代理（subagent）** 回归。

钉住的都是用户定稿的硬要求（1.9.3wsl开发日志.txt 第六十二节·三）：
  1. 工具能注册、`TOOL_PARAMETERS` 合法、**工具被没收**（只有最小只读集）；
  2. `start` **立即返回**（不阻塞）且拿到 handle；
  3. **并发上限 1**：第二个要么排队要么被拒（这里是**拒绝**）；
  4. 子代理**用自己的 messages**，**绝不污染** `session.messages`；
  5. 结果以 **tool 结果的形状**（assistant.tool_calls + tool）回主上下文，
     且**只有交付物**（不含子代理的中间过程）；
  6. 落库那一条的 `speaker` = 子代理的形象名；窗口显示也按那个形象取头像；
  7. 超时 / close **真能中止**（流水线里 `stream.close()` 被真的调到）；
  8. persona 是**那个角色自己的人设**（不是通用"子代理"人格），
     且带一句"被「主形象」叫来帮忙"（第 47/48 轮探针：不说明必乱）。

★ **一个字节的 API 都不打**：`subagent_jobs.set_client_factory()` 注入假客户端，
  它会按脚本吐假的流式 chunk。真实人设是用**真** settings.json 读出来断言的
  （那不需要网络）。

★ 不碰真实 `data/DBs`：落库/会话都用桩对象（沿用 `_test_a190` 的
  `MainWindow.<方法>(桩)` 那套做法）。
"""
import copy
import json
import os
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

BASE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("DEEPSEEK_API_KEY", "test")

from src.core import subagent_jobs as sa  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'[PASS]' if cond else '[FAIL]'} {name}" + (f"  {extra}" if extra else ""))


# ══════════════════ 假 LLM 客户端（按脚本吐流式 chunk）══════════════════

def _chunk(content=None, reasoning=None, tool_calls=None):
    delta = SimpleNamespace(content=content, reasoning_content=reasoning,
                            tool_calls=tool_calls)
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta)])


def _call(name, args: dict, cid: str = "call_1"):
    return SimpleNamespace(index=0, id=cid,
                           function=SimpleNamespace(name=name,
                                                    arguments=json.dumps(args)))


class _FakeStream:
    """假的流式响应：按脚本吐 chunk；可选"每块之间睡一会"来模拟慢请求。"""

    def __init__(self, chunks, gap=0.0, close_flag=None):
        self._chunks = list(chunks)
        self._gap = gap
        self._flag = close_flag

    def __iter__(self):
        for c in self._chunks:
            if self._gap:
                time.sleep(self._gap)
            yield c

    def close(self):
        if self._flag is not None:
            self._flag.append(True)


class _FakeClient:
    """脚本化假客户端。script = [[chunk, ...], [chunk, ...], ...]（一轮一组）。"""

    def __init__(self, script, gap=0.0, record=None):
        self.script = script
        self.gap = gap
        self.record = record if record is not None else {}
        self.round = 0
        self.tools_seen = []
        self.messages_seen = []

    def set_tools(self, tools):
        self.tools_seen.append([t.get("function", {}).get("name")
                                for t in (tools or [])])

    def chat_stream(self, messages):
        self.messages_seen.append(copy.deepcopy(messages))
        idx = min(self.round, len(self.script) - 1)
        self.round += 1
        return _FakeStream(self.script[idx], gap=self.gap,
                           close_flag=self.record.setdefault("closed", []))


def _install_factory(box):
    """box = {"script": [...], "gap": ..., "clients": [], "prompts": [], ...}"""
    def _factory(system_prompt, max_tokens, tools):
        box.setdefault("prompts", []).append(system_prompt)
        box.setdefault("max_tokens", []).append(max_tokens)
        c = _FakeClient(box["script"], gap=box.get("gap", 0.0), record=box)
        box.setdefault("clients", []).append(c)
        return c
    sa.set_client_factory(_factory)


# ══════════════════ 1. 工具注册 / 参数合法 / 工具被没收 ══════════════════
print("== 1. 工具注册 + TOOL_PARAMETERS 合法 + 工具被没收 ==")

import importlib.util  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "_sa_tool_probe", BASE / "tools" / "subagent_tool.py")
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

check("★TOOL_NAME = subagent", _mod.TOOL_NAME == "subagent", _mod.TOOL_NAME)
_tp = _mod.TOOL_PARAMETERS
check("★TOOL_PARAMETERS 是合法 JSON Schema（object + required=action）",
      isinstance(_tp, dict) and _tp.get("type") == "object"
      and _tp.get("required") == ["action"] and _tp.get("additionalProperties") is False)
_acts = (_tp.get("properties") or {}).get("action", {}).get("enum") or []
check("★五个 action 都在（start/status/close/list/approve_tools）",
      set(_acts) == {"start", "status", "close", "list", "approve_tools"}, str(_acts))
check("★参数齐（agent/prompt/context/timeout/job/tool/times）",
      {"agent", "prompt", "context", "timeout", "job", "tool", "times"}
      <= set((_tp.get("properties") or {}).keys()))

# 工具管理器里真的注册上了（ToolManager 是按 TOOL_* 扫的）
from src.core.tool_manager import get_tool_manager  # noqa: E402
_tm = get_tool_manager()
_ti = _tm.get_tool("subagent")
check("★ToolManager 扫到了 subagent 工具", _ti is not None and _ti.enabled)
check("★注册给模型的声明名也对",
      _ti is not None and _ti.to_openai_tool()["function"]["name"] == "subagent")

# 工具被没收：危险动作一个都不在最小只读集里
check("★危险动作被没收（file_ops 不能 write/edit/send）",
      all(a not in sa.SUBAGENT_SAFE_TOOLS["file_ops"]
          for a in ("write", "edit", "send")), str(sa.SUBAGENT_SAFE_TOOLS["file_ops"]))
check("★危险动作被没收（system_command 不能 kill_process/open_app/open_url）",
      all(a not in sa.SUBAGENT_SAFE_TOOLS["system_command"]
          for a in ("kill_process", "open_app", "open_url")))
check("★危险工具整个不给（shell_exec / download / comfyui / office 都不在只读集里）",
      all(n not in sa.SUBAGENT_SAFE_TOOLS
          for n in ("shell_exec", "download", "comfyui", "office", "pptx",
                    "long_term_memory")))
_ts = sa.subagent_tools()
_names = {t["function"]["name"] for t in _ts}
check("★给子代理的工具表 = 最小只读集 + 申请入口 request_tools",
      "request_tools" in _names and set(sa.SUBAGENT_SAFE_TOOLS) & _names,
      str(sorted(_names)))
# ★ 收窄必须是**副本**：就地改会把主 LLM 的工具表也改坏
_fo = _tm.get_tool("file_ops").to_openai_tool()["function"]["parameters"]["properties"]["action"]
check("★声明收窄后没有把主 LLM 的 file_ops 工具表改坏（enum 还在）",
      "write" in (_fo.get("enum") or []), str(_fo.get("enum")))
_sa_fo = next((t for t in _ts if t["function"]["name"] == "file_ops"), None)
if _sa_fo is not None:
    _enum = _sa_fo["function"]["parameters"]["properties"]["action"]["enum"]
    check("★子代理拿到的 file_ops 只剩只读动作", "write" not in _enum, str(_enum))
else:
    check("★子代理拿到的 file_ops 只剩只读动作", False, "工具表里没有 file_ops")

# ══════════════════ 2. persona 是那个角色自己的 ══════════════════
print("\n== 2. persona：是那个角色自己的人设 + 一句「被谁叫来的」==")

_persona_ds, _p_ds = sa.load_persona("deepseek")
_persona_ll, _p_ll = sa.load_persona("铃兰")
check("★两个形象的人设都读得到", bool(_p_ds) and bool(_p_ll),
      f"deepseek={len(_p_ds)}字 铃兰={len(_p_ll)}字")
check("★两个人设**不一样**（不是同一套通用模板）", _p_ds and _p_ll and _p_ds != _p_ll)
_sys_p = sa.build_system_prompt("deepseek", "铃兰", _p_ds)
check("★系统提示词里带着那个角色**自己**的人设原文", _p_ds[:30] in _sys_p,
      _p_ds[:30].replace("\n", " "))
check("★系统提示词里**没有**通用「子代理」人格顶替人设",
      "你是一个通用的子代理" not in _sys_p)
check("★明确说明「是被谁叫来的」（第 47/48 轮探针：不说明必乱）",
      "被「铃兰」叫来" in _sys_p)
check("★也说了任务优先 / 保持自己性格",
      "保持你自己的性格" in _sys_p and "任务优先" in _sys_p)
check("★它不是「当前形象」（两个人设确实互不相同）",
      sa.build_system_prompt("铃兰", "deepseek", _p_ll)[:30] != _sys_p[:30])

# ══════════════════ 3. 会话桩（供"不污染 session.messages"断言）══════════════════
print("\n== 3. start 立即返回 + 并发上限 + 不污染 session.messages ==")


class _FakeSession:
    """只当 ChatSession 用：子代理**永远不该**碰它"""
    def __init__(self, character="铃兰"):
        self._lock = threading.Lock()
        self.messages = []
        self._character = character
        self._topic_id = 7

    @property
    def character(self):
        return self._character


import src.core.session as _sess_mod  # noqa: E402
_old_session = getattr(_sess_mod, "_session", None)
_fake_sess = _FakeSession("铃兰")
_sess_mod._session = _fake_sess          # 让 current_main_character() = 铃兰
try:
    sa.MIN_TIMEOUT = 1                   # 超时用例别等 10 秒
    sa.WAIT_APPROVAL_SECONDS = 2         # 协商窗口别等 20 秒

    # 假客户端要"慢慢地吐" → 用来证明 start 没有阻塞
    _box = {"script": [[_chunk(content="查到了：1+1=2")]], "gap": 0.6,
            "prompts": [], "clients": []}
    _install_factory(_box)
    _t0 = time.time()
    _r1 = sa.submit(agent="deepseek", prompt="帮我算一下 1+1")
    _dt = time.time() - _t0
    check("★★start 立即返回（< 0.5 秒，不阻塞）", _dt < 0.5, f"{_dt:.3f}s")
    check("★start 返回了 handle（job/agent/state=running）",
          bool(_r1.get("job", "").startswith("sa-"))
          and _r1.get("agent") == "deepseek" and _r1.get("state") == "running",
          str({k: _r1.get(k) for k in ("job", "agent", "state")}))
    _jid = _r1["job"]

    _r2 = sa.submit(agent="deepseek", prompt="再算一个")
    check("★并发上限 1：第二个被拒（busy）",
          _r2.get("success") is False and _r2.get("busy") is True, str(_r2.get("error"))[:60])

    check("★子代理跑的时候 session.messages 一个字节没动", _fake_sess.messages == [])

    sa.wait_all(15)
    _st = sa.status(_jid)
    check("★跑完状态 = done", _st.get("state") == "done", str(_st.get("state")))
    check("★交付物就是那个结论", "1+1=2" in (_st.get("deliverable") or ""),
          (_st.get("deliverable") or "")[:40])
    check("★跑完仍然没污染 session.messages", _fake_sess.messages == [],
          f"{len(_fake_sess.messages)} 条")

    # 子代理**自己的** messages：任务是它自己的、里面没有主对话的东西
    _cl = _box["clients"][0]
    _own = _cl.messages_seen[0]
    check("★子代理用的是**自己的** messages（第一条是它自己的任务）",
          len(_own) == 1 and "1+1" in _own[0]["content"]
          and _own[0]["role"] == "user")
    check("★低 max_tokens（硬上限之一）",
          _box["max_tokens"] and _box["max_tokens"][0] == sa.SUBAGENT_MAX_TOKENS,
          str(_box["max_tokens"]))
    check("★工具表里真的没有危险工具（shell_exec/download 不在）",
          all(n not in _cl.tools_seen[0] for n in ("shell_exec", "download", "office")))

    # ══════════════════ 4. 交付物以 tool 结果的形状回主上下文 ══════════════════
    print("\n== 4. 交付物以 tool 结果的身份进主上下文（且只有交付物）==")

    from src.ui.main_window import MainWindow  # noqa: E402

    class _Rec:
        def __init__(self):
            self.calls = []

        def emit(self, *a):
            self.calls.append(a)

    class _FakeChat:
        def __init__(self):
            self.agent_text_in = _Rec()

    class _ChatStore:
        def __init__(self):
            self.added = []
            self.marked = []

        def add(self, role, content, tool_name=None, tool_call_id=None,
                topic_id=None, speaker=None):
            self.added.append({"role": role, "content": content, "speaker": speaker})
            return len(self.added)

        def mark_processed(self, ids, status=1):
            self.marked.extend(ids or [])

    class _FakeDB:
        def __init__(self):
            self.chat = _ChatStore()

    class _Win:
        pass

    _w = _Win()
    _w.chat = _FakeChat()
    _w.db = _FakeDB()
    _w.session = _fake_sess
    _w._pending_subagent_results = []
    _w._turn_active = False
    _w._on_subagent_msg = lambda a, t: MainWindow._on_subagent_msg(_w, a, t)
    _w._flush_pending_subagent_results = (
        lambda: MainWindow._flush_pending_subagent_results(_w))

    MainWindow._on_subagent_done(_w, _st)

    _msgs = _fake_sess.messages
    check("★交付物补进主上下文 = 恰好一对（assistant.tool_calls + tool）",
          len(_msgs) == 2 and _msgs[0]["role"] == "assistant" and _msgs[1]["role"] == "tool",
          str([m["role"] for m in _msgs]))
    check("★身份是工具调用（function.name=subagent），不是「主 LLM 自己说的」",
          _msgs[0].get("tool_calls")
          and _msgs[0]["tool_calls"][0]["function"]["name"] == "subagent")
    check("★tool 结果的 tool_call_id 与 assistant 那条配对（不然会被体检丢掉）",
          _msgs[1].get("tool_call_id") == _msgs[0]["tool_calls"][0]["id"])
    check("★交付物确实在 tool 结果里（带「子代理」标注）",
          "子代理" in _msgs[1]["content"] and "1+1=2" in _msgs[1]["content"])
    check("★主上下文里**只有交付物**，没有子代理的整段会话/中间过程",
          len(_msgs[1]["content"]) < 600
          and "（deepseek 上线）" not in _msgs[1]["content"],
          f"{len(_msgs[1]['content'])} 字")

    # ══════════════════ 5. 窗口显示 + 落库：speaker = 子代理形象名 ══════════════════
    print("\n== 5. 窗口显示 + chat_records 落库（speaker = 那个形象名）==")
    _shown = _w.chat.agent_text_in.calls
    check("★窗口里显示了交付物（走带 speaker 的那条通道）",
          bool(_shown) and "1+1=2" in _shown[-1][0], str(_shown[-1][:1])[:60])
    check("★窗口那条的 speaker = 子代理的形象名（deepseek）",
          _shown and _shown[-1][1] == "deepseek", str(_shown[-1][1] if _shown else ""))
    _added = _w.db.chat.added
    check("★交付物落库了一条（role=assistant）",
          len(_added) == 1 and _added[0]["role"] == "assistant")
    check("★★★落库那条的 speaker = 子代理形象名（不是当前形象 铃兰）",
          _added and _added[0]["speaker"] == "deepseek",
          str(_added[0]["speaker"] if _added else ""))
    check("★落库内容与窗口显示一致", _added and _added[0]["content"] == _shown[-1][0])

    # 落库**只有交付物**（逐条落库会让重启后的主上下文被灌满 → 违反 token 有界）
    check("★落库的只有交付物这一条（没把中间过程逐条写进 chat_records）",
          len(_added) == 1)

    # ══════════════════ 6. 超时真能中止 ══════════════════
    print("\n== 6. 超时 / close 真能中止 ==")
    # 慢速脚本：**一轮里很多块、每块睡 0.4s**，超时 1 秒 → 读到第二三块就该被打断
    #   ★ 必须"一轮多块"：如果一轮只有一块，这一轮 0.4 秒就跑完了，
    #     子代理会正常结束（那就测不出超时了 —— 第一版就是这么假通过的）。
    _slow = [[_chunk(content=f"还在查第 {i} 条…") for i in range(12)]]
    _box2 = {"script": _slow, "gap": 0.4, "clients": [], "prompts": []}
    _install_factory(_box2)
    _r3 = sa.submit(agent="deepseek", prompt="慢慢查", timeout=1)
    check("★timeout 参数被接受（start 仍立即返回）", _r3.get("success") is True)
    sa.wait_all(20)
    _st3 = sa.status(_r3["job"])
    check("★超时状态正确（timeout）", _st3.get("state") == "timeout", str(_st3.get("state")))
    check("★超时后交代了原因（给模型/用户一句实话）",
          "超时" in (_st3.get("error") or "") or "超时" in (_st3.get("deliverable") or ""),
          (_st3.get("error") or _st3.get("deliverable") or "")[:50])
    check("★超时中间产物被保住（不是丢个空交付物）",
          "查" in (_st3.get("deliverable") or ""), (_st3.get("deliverable") or "")[:30])
    check("★超时也真的把 HTTP 流关了", bool(_box2.get("closed")), str(_box2.get("closed")))

    # close：一个跑得很慢的子代理，中途掐掉
    _box3 = {"script": [[_chunk(content=f"一句一句地吐 {i}") for i in range(10)]],
             "gap": 0.4, "clients": [], "prompts": []}
    _install_factory(_box3)
    _n_msgs_before = len(_fake_sess.messages)
    _r4 = sa.submit(agent="deepseek", prompt="说话慢一点", timeout=120)
    time.sleep(0.3)
    _rc = sa.close(_r4["job"])
    check("★close 报告已中止", _rc.get("success") and _rc.get("state") == "cancelled",
          f"{_rc.get('state')} / {str(_rc.get('text'))[:40]}")
    check("★close 真的把 HTTP 流关了（stream.close 被调到）",
          bool(_box3.get("closed")), str(_box3.get("closed")))
    sa.wait_all(10)
    check("★中止后 session.messages 依然没被污染（条数没变）",
          len(_fake_sess.messages) == _n_msgs_before,
          f"{_n_msgs_before} → {len(_fake_sess.messages)}")

    # ══════════════════ 7. 工具协商（阶段 2）：申请 / 放行 / 未授权拦截 ══════════════════
    print("\n== 7. 工具协商：没收 → 申请 → 主 LLM 放行 → 才能用 ==")
    # 第 1 轮：申请一个不在只读集里的工具；第 2 轮：直接用它
    _script = [
        [_chunk(tool_calls=[_call("request_tools",
                                  {"tool": "fancy_tool", "times": 2,
                                   "reason": "要查一个只有它能查的东西"}, "c1")])],
        [_chunk(tool_calls=[_call("fancy_tool", {"q": "x"}, "c2")])],
        [_chunk(content="交付：查完了")],
    ]
    _box4 = {"script": _script, "gap": 0.0, "clients": [], "prompts": []}
    _install_factory(_box4)
    _r5 = sa.submit(agent="deepseek", prompt="查点东西", timeout=60)
    _jid5 = _r5["job"]

    # 申请要**真的能被主 LLM 看到**（status 里）
    _seen_req = None
    for _ in range(50):
        _s = sa.status(_jid5)
        if _s.get("requests"):
            _seen_req = _s["requests"][0]
            break
        time.sleep(0.1)
    check("★子代理的申请出现在 status 里（主 LLM 能看到）", bool(_seen_req),
          str(_seen_req)[:80])
    check("★申请里带了工具名 + 次数 + 理由",
          _seen_req and _seen_req.get("tool") == "fancy_tool"
          and _seen_req.get("times") == 2 and _seen_req.get("reason"))
    check("★等放行期间状态 = waiting_approval",
          sa.status(_jid5).get("state") in ("waiting_approval", "running"),
          str(sa.status(_jid5).get("state")))

    _ap = sa.approve_tools(_jid5, "fancy_tool", 2)
    check("★approve_tools 放行成功", _ap.get("success") is True, str(_ap.get("text"))[:50])
    sa.wait_all(20)
    _st5 = sa.status(_jid5)
    check("★协商这一轮最终做完了", _st5.get("state") == "done", str(_st5.get("state")))
    check("★放行后工具真的被执行（不再是「未授权」，而是工具本身的答复）",
          "未授权" not in (_st5.get("deliverable") or ""))
    # 从假客户端收到的 tool 结果里直接看授权拦截/放行的实情
    _round2 = _box4["clients"][0].messages_seen[-1]
    _tool_results = [m["content"] for m in _round2 if m.get("role") == "tool"]
    check("★第 2 轮里那个工具的返回**不是**「未授权」",
          _tool_results and "未授权" not in _tool_results[-1],
          (_tool_results[-1][:60] if _tool_results else "(没有 tool 结果)"))

    # 未授权：不给放行时，只读集之外的调用必须被拦下来
    _script2 = [
        [_chunk(tool_calls=[_call("shell_exec", {"command": "echo hi"}, "d1")])],
        [_chunk(content="交付：没工具，做不了")],
    ]
    _box5 = {"script": _script2, "gap": 0.0, "clients": [], "prompts": []}
    _install_factory(_box5)
    _r6 = sa.submit(agent="deepseek", prompt="随便跑个命令", timeout=30)
    sa.wait_all(20)
    _seen2 = _box5["clients"][0].messages_seen[-1]
    _tr2 = [m["content"] for m in _seen2 if m.get("role") == "tool"]
    check("★★没放行的 shell_exec 被拦下（返回「未授权」，没真执行）",
          _tr2 and "未授权" in _tr2[-1], (_tr2[-1][:60] if _tr2 else "(没有 tool 结果)"))
    check("★被拦时也告诉子代理该怎么办（去申请）",
          _tr2 and "request_tools" in _tr2[-1])

    # 只读集里的**越权动作**也要拦（file_ops 的 write 不在白名单）
    _script3 = [
        [_chunk(tool_calls=[_call("file_ops",
                                  {"action": "write", "path": "x.txt",
                                   "content": "hi"}, "e1")])],
        [_chunk(content="交付：写不了")],
    ]
    _box6 = {"script": _script3, "gap": 0.0, "clients": [], "prompts": []}
    _install_factory(_box6)
    _r7 = sa.submit(agent="deepseek", prompt="写个文件", timeout=30)
    sa.wait_all(20)
    _seen3 = _box6["clients"][0].messages_seen[-1]
    _tr3 = [m["content"] for m in _seen3 if m.get("role") == "tool"]
    check("★★只读集里的越权动作也被拦（file_ops.write 被没收）",
          _tr3 and "未授权" in _tr3[-1], (_tr3[-1][:60] if _tr3 else "(没有 tool 结果)"))

    # ══════════════════ 8. 工具 execute 的对外形态 ══════════════════
    print("\n== 8. 工具 execute：只走 action 分发，不自己干活 ==")
    _j = json.loads(_mod.execute(action="list"))
    check("★list 返回可用形象 + 默认只读工具",
          _j.get("success") is True and _j.get("characters")
          and _j.get("safe_tools"), str(_j.get("characters")))
    _j2 = json.loads(_mod.execute(action="start"))
    check("★start 少参数时给出可读的错误（不抛异常）",
          _j2.get("success") is False and "prompt" in str(_j2.get("error")),
          str(_j2.get("error"))[:50])
    _j3 = json.loads(_mod.execute(action="status", job="不存在的"))
    check("★status 查不存在的 handle → 明确报错（不抛异常）",
          _j3.get("success") is False and "没有这个" in str(_j3.get("error")))
    _j4 = json.loads(_mod.execute(action="叫错的名字", agent="deepseek", prompt="x"))
    check("★未知 action → 明确报错", _j4.get("success") is False)
    _j5 = json.loads(_mod.execute(action="close", job="不存在的"))
    check("★close 不存在的 handle → 明确报错", _j5.get("success") is False)

    # ══════════════════ 9. 身份保护：不能叫自己当子代理 ══════════════════
    print("\n== 9. 不能把自己当子代理（否则交付物会被当成「我自己说的」）==")
    _r8 = sa.submit(agent="铃兰", prompt="叫我自己")
    check("★叫当前形象本人 → 被拒（并给出可选项）",
          _r8.get("success") is False and _r8.get("characters") == ["deepseek"],
          str(_r8.get("error"))[:50])
    _r9 = sa.submit(agent="不存在的形象", prompt="x")
    check("★叫不存在的形象 → 被拒（并列出可用的）",
          _r9.get("success") is False and _r9.get("characters"),
          str(_r9.get("error"))[:40])

finally:
    sa.set_client_factory(None)
    sa.MIN_TIMEOUT = 10
    sa.WAIT_APPROVAL_SECONDS = 20
    _sess_mod._session = _old_session

print(f"\n==== 结果: {len(PASS)} PASS / {len(FAIL)} FAIL ====")
for f in FAIL:
    print("  FAIL:", f)
sys.exit(1 if FAIL else 0)
