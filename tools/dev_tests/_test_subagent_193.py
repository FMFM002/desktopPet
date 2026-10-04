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

# ★★ 1.9.3 第 61 轮：**"模型看不看得见 action"** 单独钉住。
#   真机事故（logs/prts_20261004_200450.log）：模型看了 schema 却断言
#   "action enum 只有 [list]"，于是不派活。上面两条只钉了"enum 五个都在"，
#   钉不住"**描述里有没有把 start 说出来**、五个 action 有没有放在**最前面**"
#   —— 那才是它略读时会漏的地方。描述变了但模型仍看不见，等于没修。
_desc = _mod.TOOL_DESCRIPTION
check("★【61】描述里确实提到 start（模型能读到「派活」这个动作）",
      "start" in _desc, _desc[:60])
check("★【61】描述里说清 action 必填且列全五个取值",
      "必填" in _desc and all(a in _desc[:260] for a in _acts), _desc[:180])
check("★【61】五个 action 在最前面一句话列全（不是埋在散文中间）",
      "action" in _desc[:80]
      and all(a in _desc[:260] for a in _acts), _desc[:160])

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
            self.busy = 0

        def start_busy(self):
            self.busy += 1

        def stop_busy(self):
            self.busy = max(0, self.busy - 1)

    class _ChatStore:
        def __init__(self):
            self.added = []
            self.marked = []

        def add(self, role, content, tool_name=None, tool_call_id=None,
                topic_id=None, speaker=None, ctx_exclude=0):
            self.added.append({"role": role, "content": content, "speaker": speaker,
                               "ctx_exclude": ctx_exclude})
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
    _w._pending_subagent_lines = []
    _w._turn_active = False
    _w._subagent_followup_active = False
    _w.followups = []
    _w._show_subagent_text = lambda a, t: MainWindow._show_subagent_text(_w, a, t)
    _w._on_subagent_msg = lambda a, t: MainWindow._on_subagent_msg(_w, a, t)
    _w._flush_pending_subagent_lines = (
        lambda: MainWindow._flush_pending_subagent_lines(_w))
    _w._flush_pending_subagent_results = (
        lambda: MainWindow._flush_pending_subagent_results(_w))
    # 【4】的主动续轮在这里只记账（真逻辑由第 12 段用真方法单独钉）
    _w._request_subagent_followup = lambda j="": _w.followups.append(j)

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
          len(_msgs[1]["content"]) < 700
          and "（deepseek 上线）" not in _msgs[1]["content"],
          f"{len(_msgs[1]['content'])} 字")
    check("★★★【4】空闲时交付 = **立刻触发一轮**（不许等下一条用户消息）",
          _w.followups == [_st.get("job_id")], str(_w.followups))
    check("★【4】交付物落库那条**不带** ctx_exclude（它必须进主上下文）",
          [a for a in _w.db.chat.added if a["ctx_exclude"] == 0]
          and _w.db.chat.added[-1]["ctx_exclude"] == 0,
          str([(a["speaker"], a["ctx_exclude"]) for a in _w.db.chat.added]))

    # ══════════════════ 5. 窗口显示 + 落库：speaker = 子代理形象名 ══════════════════
    print("\n== 5. 窗口显示 + chat_records 落库（speaker = 那个形象名）==")
    _shown = _w.chat.agent_text_in.calls
    _added = _w.db.chat.added
    # ★★ 第 61 轮口径变更（用户真机："同一段内容显示了两遍" + "（sa-1）不该给用户看"）：
    #   交付物在子代理那边**就是它最后那句发言**（`subagent_jobs._run_one` 把 final
    #   既 `_emit` 出去又当交付物回调回来）→ 已经显示过一次的，不再显示第二遍。
    #   它真正该去的地方只有一个：主 LLM 的上下文（下面第 4 段已经钉住了）。
    check("★★★【61】交付物**不再**当成聊天消息显示（窗口调用里没有它）",
          not any("1+1=2" in str(c[0]) for c in _shown), str(_shown))
    check("★★★【61】窗口调用里也没有内部 handle（sa-… 一律不给用户看）",
          not any("sa-" in str(c[0]) for c in _shown), str(_shown))
    check("★交付物落库了一条（role=assistant）",
          len(_added) == 1 and _added[0]["role"] == "assistant")
    check("★★★落库那条的 speaker = 子代理形象名（不是当前形象 铃兰）",
          _added and _added[0]["speaker"] == "deepseek",
          str(_added[0]["speaker"] if _added else ""))
    check("★★★【61】落库那条（它要进主上下文）里也**没有**内部 handle（sa-1 已删）",
          _added and "sa-" not in str(_added[0]["content"]),
          str(_added[0]["content"] if _added else "")[:80])
    check("★【61】落库正文仍然是那段交付物（只是不再给用户看第二遍）",
          _added and "1+1=2" in str(_added[0]["content"]))

    # ★★ 口径变更（第 60 轮）：交付物**不是**"唯一落库的东西"了 ——
    #   中间发言也落库（用户要求"重启后还能看到中间过程"），但必须带 ctx_exclude=1；
    #   交付物则是唯一**不带**这个标记、因而唯一会进主上下文的那条。
    check("★这次这条链里只有交付物一条落库（呼叫语/中间发言由 _on_subagent_msg 落）",
          len(_added) == 1)
    check("★★交付物那条 ctx_exclude=0（它是唯一会进主 LLM 上下文的）",
          _added and _added[0]["ctx_exclude"] == 0,
          str(_added[0]["ctx_exclude"] if _added else ""))

    # 中间发言：走 `_on_subagent_msg` → **落库 + ctx_exclude=1** + 排队补内存
    _n_before = len(_w.db.chat.added)
    _n_shown_before = len(_w.chat.agent_text_in.calls)
    _w._on_subagent_msg("deepseek", "（deepseek 上线）我先去查一下～")
    _mid = _w.db.chat.added[-1]
    check("★【2】子代理的中间发言**也落库**了（用户要求重启后还看得到）",
          len(_w.db.chat.added) == _n_before + 1
          and "上线" in _mid["content"], str(_mid)[:70])
    check("★★★【2】中间发言那条 speaker = 子代理形象名", _mid["speaker"] == "deepseek")
    check("★★★【2】中间发言带 ctx_exclude=1（只给人看，**不进主 LLM 上下文**）",
          _mid["ctx_exclude"] == 1, str(_mid["ctx_exclude"]))
    check("★【2】空闲时中间发言立刻补进内存（窗口重画看得到）且带 ctx_exclude 标记",
          _fake_sess.messages
          and _fake_sess.messages[-1].get("ctx_exclude") == 1
          and _fake_sess.messages[-1].get("speaker") == "deepseek",
          str(_fake_sess.messages[-1])[:80])
    # ★ 第 61 轮：显示搬进了 `_flush_pending_subagent_lines` —— 空闲时它仍然立刻显示
    check("★★★【61】空闲时中间发言**照旧立刻显示**（显示走 flush，空闲时是同步的）",
          len(_w.chat.agent_text_in.calls) == _n_shown_before + 1
          and _w.chat.agent_text_in.calls[-1][1] == "deepseek"
          and "上线" in _w.chat.agent_text_in.calls[-1][0],
          str(_w.chat.agent_text_in.calls[-1] if _w.chat.agent_text_in.calls else []))

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
    # ★★ 1.9.3 第 61 轮：**那份"名册"自己得说清它只是名册**。
    #   真机那次模型读完 list 的返回，就当成了"这个工具只会 list"，
    #   所以能力清单 + "要派活就 start" 必须写进这份 JSON 本身。
    check("★【61】list 返回里写着全部 action（不是只有 list）",
          (_j.get("actions") or []) == ["start", "status", "close", "list",
                                        "approve_tools"], str(_j.get("actions")))
    check("★【61】list 返回里有一句「要派活就 start」的提示",
          "start" in str(_j.get("hint") or ""), str(_j.get("hint"))[:80])
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

# ══════════════════════════════════════════════════════════════════════════════
# ★★ 1.9.3 第 60 轮新增：【2】中间发言落库 · 【3】start 预放行 · 【4】交付即感知
# ══════════════════════════════════════════════════════════════════════════════

# ══════════════════ 10.【2】中间发言落库，但**重启后也不进主 LLM 上下文** ══════════════════
print("\n== 10.【2】中间发言落库 + ctx_exclude=1：重启后看得到、但灌不进主上下文 ==")

import shutil  # noqa: E402
import src.core.database as _dbmod  # noqa: E402
from src.core.database import ChatRecordsDB, SHARED_CHARACTER  # noqa: E402
from src.core.session import ChatSession, build_shared_context  # noqa: E402

_TMP = BASE / "_test_tmp_dbs_subagent193"
shutil.rmtree(_TMP, ignore_errors=True)
_TMP.mkdir(parents=True, exist_ok=True)
_OLD_DB_DIR = _dbmod.DB_DIR
_dbmod.DB_DIR = _TMP

_MID_TEXT = "（deepseek 上线）我先去翻一下文件，马上回来～"
_DELIV = "「deepseek」交回来的（sa-9）：\n查到了：1+1=2"


class _DbShim:
    """只当 ChatSession.db 用（真 ChatRecordsDB + 两个摘要接口）。"""

    def __init__(self, chat):
        self.chat = chat

    def get_topic_messages(self, tid):
        return self.chat.get_topic_messages(tid)

    def get_topic_summary(self, tid):
        return ""

    def get_last_archived_summary(self, character, before_topic_id=0):
        return ""


def _session_for(topic_id, db_shim, character="铃兰"):
    """绕开 `ChatSession.__init__`（那会建话题/开库），只装配本段要用的字段。"""
    s = ChatSession.__new__(ChatSession)
    s._lock = threading.Lock()
    s.messages = []
    s._character = character
    s._topic_id = topic_id
    s.db = db_shim
    s._streaming = False
    s._summarizing = False
    s._abort_chat = False
    s._abort_tool = False
    s.on_done = None
    return s


try:
    _chatdb = ChatRecordsDB()
    _cols = {r["name"] for r in _chatdb._conn.execute("PRAGMA table_info(chat_records)")}
    check("★【2】chat_records 有了 ctx_exclude 列（幂等迁移出来的）",
          "ctx_exclude" in _cols, str(sorted(_cols)))
    # 幂等：反复启动不许报错
    try:
        _chatdb._init_tables()
        _chatdb._init_tables()
        _chatdb2 = ChatRecordsDB()
        _cols2 = {r["name"] for r in _chatdb2._conn.execute(
            "PRAGMA table_info(chat_records)")}
        _chatdb2.close()
        _idem2 = "ctx_exclude" in _cols2
    except Exception as _e:          # noqa: BLE001
        _idem2 = False
        print("    迁移重复执行抛异常:", _e)
    check("★★【2】ctx_exclude 迁移幂等（每次启动都跑、重复不报错）", _idem2)

    _tid = _chatdb.create_topic("", SHARED_CHARACTER)
    _chatdb.add("user", "主人：帮我看一下", topic_id=_tid)
    # 子代理的中间发言（第 60 轮起落库；标记"不进上下文"）
    _chatdb.add("assistant", _MID_TEXT, topic_id=_tid,
                speaker="deepseek", ctx_exclude=1)
    # 交付物（不带标记 → 会进上下文）
    _chatdb.add("assistant", _DELIV, topic_id=_tid, speaker="deepseek")
    _rows = _chatdb.get_topic_messages(_tid)
    check("★【2】中间发言真的落库了（重启后库里有它）",
          any(r["content"] == _MID_TEXT for r in _rows),
          str([(r["role"], (r["content"] or "")[:8]) for r in _rows]))
    check("★【2】库里那条中间发言 ctx_exclude=1、交付物那条 =0",
          [r["ctx_exclude"] for r in _rows] == [0, 1, 0],
          str([r["ctx_exclude"] for r in _rows]))

    # ── "重启"：重新从库里 load 进内存 ──
    _s2 = _session_for(_tid, _DbShim(_chatdb))
    _s2._load_topic_messages(_tid)
    check("★★【2】重启后 session.messages 里有那些中间发言",
          any(m.get("content") == _MID_TEXT for m in _s2.messages),
          str([(m.get("role"), (m.get("content") or "")[:8]) for m in _s2.messages]))
    check("★★【2】从库带进内存时 ctx_exclude 标记也跟着（不然守卫就白设了）",
          [m.get("ctx_exclude") for m in _s2.messages] == [None, 1, None],
          str([m.get("ctx_exclude") for m in _s2.messages]))

    _ctx = build_shared_context([dict(m) for m in _s2.messages], "铃兰")
    _ctx_text = "\n".join(str(m.get("content") or "") for m in _ctx)
    check("★★★【2】重启后 build_shared_context **跳过**中间发言（主 LLM 看不到）",
          _MID_TEXT not in _ctx_text, _ctx_text[:120])
    check("★★【2】交付物照旧进上下文（重启后也看得到它的结果）",
          "1+1=2" in _ctx_text and "[deepseek]：" in _ctx_text,
          _ctx_text[:160])
    check("★【2】被跳过的只是「中间发言」这一类，用户消息一条不少",
          any(m.get("role") == "user" and "帮我看一下" in (m.get("content") or "")
              for m in _ctx))

    # ── 窗口渲染：真跑 `_reload_chat_view`，中间发言必须照旧画出来 ──
    from PySide6.QtWidgets import QApplication  # noqa: E402
    _app = QApplication.instance() or QApplication([])
    from src.ui.chat_widget import ChatWidget  # noqa: E402
    from src.ui.main_window import MainWindow  # noqa: E402

    class _RecChat(ChatWidget):
        def __init__(self):
            super().__init__()
            self.events = []

        def _append_message(self, role, text, speaker=""):
            self.events.append((role, text, speaker))
            return super()._append_message(role, text, speaker)

    class _ViewDb:
        def list_character_topics(self, character, limit=50):
            return []

        def get_topic_messages(self, tid):
            return []

    class _StubMW(MainWindow):
        def __init__(self):
            pass

    _mw = _StubMW()
    _mw.chat = _RecChat()
    _mw._chat_load_rounds = 999
    _mw._chat_scroll_hooked = True
    _mw._chat_archived_rendered = False
    _mw._pending_attachments = []
    _mw.session = _s2
    _mw.db = _ViewDb()
    MainWindow._reload_chat_view(_mw)
    _texts = [e for e in _mw.chat.events if e[1]]
    check("★★【2】窗口重画**照旧渲染**中间发言（不因 ctx_exclude 被藏起来）",
          any(_MID_TEXT in e[1] for e in _texts),
          str([e[1][:12] for e in _texts]))
    check("★★【2】中间发言在窗口里按**子代理自己的头像**渲染（speaker=deepseek）",
          any(_MID_TEXT in e[1] and e[2] == "deepseek" for e in _texts),
          str([(e[1][:8], e[2]) for e in _texts]))
    check("★【2】交付物在窗口里也在（渲染没被这件事影响）",
          any("1+1=2" in e[1] for e in _texts))

    # ── ★★ 第 61 轮：交付物与"子代理最后那句发言"是**同一段文字** → 窗口只画一次 ──
    #   真机上这两条**内容完全一样**（`_emit(...final)` 发的 final 就是交付物）：
    #     ① ctx_exclude=1 的那条（管窗口显示：重启后还能看到中间过程）；
    #     ② ctx_exclude=0 的那条（管主 LLM 上下文：重启后交付物还在上下文里）。
    #   两条都必须留在数据里 —— 但**给用户看的只能一条**。
    _tid2 = _chatdb.create_topic("", SHARED_CHARACTER)
    _DUP = "铃兰查好啦，明天（10-05）重庆的天气是：小雨转多云，18~21°C，记得带伞～"
    _chatdb.add("user", "叫铃兰帮我查一下明天天气", topic_id=_tid2)
    _chatdb.add("assistant", _DUP, topic_id=_tid2, speaker="铃兰", ctx_exclude=1)
    _chatdb.add("assistant", _DUP, topic_id=_tid2, speaker="铃兰")      # ctx_exclude=0
    _s3 = _session_for(_tid2, _DbShim(_chatdb))
    _s3._load_topic_messages(_tid2)
    check("★★【61】重启后：同内容的两条都在内存里（各有各的用途，一条都不许丢）",
          [m.get("content") for m in _s3.messages].count(_DUP) == 2,
          str([(m.get("content") or "")[:8] for m in _s3.messages]))
    _mw3 = _StubMW()
    _mw3.chat = _RecChat()
    _mw3._chat_load_rounds = 999
    _mw3._chat_scroll_hooked = True
    _mw3._chat_archived_rendered = False
    _mw3._pending_attachments = []
    _mw3.session = _s3
    _mw3.db = _ViewDb()
    MainWindow._reload_chat_view(_mw3)
    _dup_shown = [e for e in _mw3.chat.events if _DUP in (e[1] or "")]
    check("★★★【61】窗口重画时同一段文字**只画一条**（用户只看到一次）",
          len(_dup_shown) == 1, f"{len(_dup_shown)} 条")
    check("★【61】画的那条是子代理自己的头像（speaker=铃兰）",
          bool(_dup_shown) and _dup_shown[0][2] == "铃兰",
          str([(e[1][:8], e[2]) for e in _dup_shown]))
    check("★【61】去重没有误伤普通消息（用户那条照旧渲染）",
          any("叫铃兰帮我查" in (e[1] or "") for e in _mw3.chat.events))
    _chatdb.close()
finally:
    _dbmod.DB_DIR = _OLD_DB_DIR
    shutil.rmtree(_TMP, ignore_errors=True)


# ══════════════════ 11.【3】start 预放行工具 ══════════════════
print("\n== 11.【3】start 支持预放行工具（tool/times），不传则行为完全不变 ==")

check("★【3】build_grants：字符串一个工具",
      sa.build_grants("ppt", 2) == {"ppt": 2}, str(sa.build_grants("ppt", 2)))
check("★【3】build_grants：逗号/顿号/空格分隔多个",
      sa.build_grants("a, b、c d", 1) == {"a": 1, "b": 1, "c": 1, "d": 1},
      str(sa.build_grants("a, b、c d", 1)))
check("★【3】build_grants：数组 + 次数按位置对应（缺的算 1 次）",
      sa.build_grants(["a", "b", "c"], [3, 2]) == {"a": 3, "b": 2, "c": 1},
      str(sa.build_grants(["a", "b", "c"], [3, 2])))
check("★★【3】build_grants：不传/空 → {}（= 不预放行，向后兼容）",
      sa.build_grants(None, 1) == {} and sa.build_grants("", 5) == {}
      and sa.build_grants("  ", 3) == {})
check("★【3】次数被夹到 1~20（与 approve_tools 同口径）",
      sa.build_grants("x", 999) == {"x": sa.MAX_GRANT_TIMES}
      and sa.build_grants("x", 0) == {"x": 1})

_fix = importlib.util.spec_from_file_location(
    "_sa_tool_probe2", BASE / "tools" / "subagent_tool.py")
_mod2 = importlib.util.module_from_spec(_fix)
_fix.loader.exec_module(_mod2)
_tp2 = _mod2.TOOL_PARAMETERS["properties"]
check("★【3】TOOL_PARAMETERS 里 tool/times 的描述**覆盖 start**（模型看得懂）",
      "start" in _tp2["tool"]["description"]
      and "start" in _tp2["times"]["description"],
      f"{_tp2['tool']['description'][:40]} | {_tp2['times']['description'][:40]}")

# ══════════════════ 【61】系统提示词里的"派活引导" ══════════════════
#   真机那次失败时，系统提示词里 `subagent` 出现次数是 **0**（long_term_memory /
#   file_ops / progress_report / office / pptx / web_search 都有专门段落）。
#   这里钉三件事：这段**存在**、**确实点名 subagent+start**、**版本进了 cache_key**
#   （不进缓存键的话，改了这段还会命中旧缓存 → 白改）。
print("\n== 【61】系统提示词里的派活引导（独立一段 + 进 cache_key）==")
from src.core.llm_client import ChatClient  # noqa: E402
_deleg_txt = ChatClient._delegation_prompt_section(None)
check("★【61】引导段存在且点名 subagent 与 start",
      "subagent" in _deleg_txt and "start" in _deleg_txt, _deleg_txt[:80])
_llm_src = (BASE / "src" / "core" / "llm_client.py").read_text(encoding="utf-8")
check("★【61】引导段的版本标记进了 cache_key（否则改完还会命中旧提示词缓存）",
      "_DELEGATION_TAG" in _llm_src and "self._DELEGATION_TAG}" in _llm_src)
check("★【61】两个分支都追加了这段（personality_prompt 分支 + 默认模板分支）",
      _llm_src.count("self._delegation_prompt_section()") >= 2,
      f"出现 {_llm_src.count('self._delegation_prompt_section()')} 次")
check("★【61】没有动 _SHARED_CONTEXT_NOTE（用户明确要求那段别碰）",
      "_SHARED_CONTEXT_NOTE = (" in _llm_src
      and '_SHARED_CONTEXT_TAG = "shared_identity_v1"' in _llm_src)

_old_sess2 = getattr(_sess_mod, "_session", None)
_fake2 = _FakeSession("铃兰")
_sess_mod._session = _fake2
try:
    sa.MIN_TIMEOUT = 1
    # ① 预放行：第一轮的工具声明里就该有那个工具，而且**能真的用**（不是"未授权"）
    _script11 = [
        [_chunk(tool_calls=[_call("fancy_pre", {"q": "x"}, "p1")])],
        [_chunk(content="交付：预放行的工具真的用上了")],
    ]
    _box11 = {"script": _script11, "clients": [], "prompts": [], "max_tokens": []}
    _install_factory(_box11)
    _r11 = sa.submit(agent="deepseek", prompt="用预放行的工具查一下",
                     timeout=30, grants=sa.build_grants("fancy_pre", 2))
    check("★【3】start 接受 grants（仍立即返回 handle）",
          _r11.get("success") is True and _r11.get("granted") == ["fancy_pre"],
          str(_r11.get("granted")))
    sa.wait_all(20)
    _st11 = sa.status(_r11["job"])
    _tr11 = [m["content"] for m in _box11["clients"][0].messages_seen[-1]
             if m.get("role") == "tool"]
    check("★★【3】预放行的工具**立刻可用**（返回的不是「未授权」）",
          bool(_tr11) and "未授权" not in _tr11[-1],
          (_tr11[-1][:60] if _tr11 else "(没有 tool 结果)"))
    check("★【3】预放行的次数被真的扣掉（2 → 1）",
          (_st11.get("grants") or {}).get("fancy_pre") == 1,
          str(_st11.get("grants")))
    check("★【3】start 的返回文案里说清了预放行了什么",
          "预放行" in str(_r11.get("text")), str(_r11.get("text"))[:80])

    # ①b **真实存在**的预放行工具必须进第一轮声明（`fancy_pre` 只是假名，声明不出来）；
    #     顺带钉住"放行能解锁最小只读集里被没收的动作"（第 59 轮这里是空的）
    _tmp_dir = BASE / "data" / "桌宠私有data"
    _tmp_dir.mkdir(parents=True, exist_ok=True)
    _tmp_w = _tmp_dir / "_test_tmp_subagent_write193.txt"
    try:
        _tmp_w.unlink()
    except Exception:
        pass
    _script11b = [
        [_chunk(tool_calls=[_call("file_ops",
                                  {"action": "write", "path": str(_tmp_w),
                                   "content": "预放行真的能写"}, "w1")])],
        [_chunk(content="交付：写完了")],
    ]
    _box11b = {"script": _script11b, "clients": [], "prompts": [], "max_tokens": []}
    _install_factory(_box11b)
    _r11b = sa.submit(agent="deepseek", prompt="写个文件",
                      timeout=30, grants=sa.build_grants("file_ops", 2))
    sa.wait_all(20)
    _names11b = (_box11b["clients"][0].tools_seen[0] if _box11b["clients"] else [])
    check("★★【3】预放行的**真实**工具进了第一轮的工具声明（模型看得到才能调）",
          "file_ops" in _names11b, str(_names11b))
    _tr11b = [m["content"] for m in _box11b["clients"][0].messages_seen[-1]
              if m.get("role") == "tool"]
    check("★★★【3】预放行真的解锁了被没收的动作（file_ops.write 写成功，不是「未授权」）",
          bool(_tr11b) and "未授权" not in _tr11b[-1]
          and _tmp_w.exists() and "预放行真的能写" in _tmp_w.read_text(encoding="utf-8"),
          (_tr11b[-1][:70] if _tr11b else "(没有 tool 结果)"))
    check("★【3】这次也是靠放行才成立的调用 → 次数被扣（2 → 1）",
          (sa.status(_r11b["job"]).get("grants") or {}).get("file_ops") == 1,
          str(sa.status(_r11b["job"]).get("grants")))
    try:
        _tmp_w.unlink()
    except Exception:
        pass

    # ② 不传：与第 59 轮**完全一致**（工具表里没有它，调用被拦）
    _script12 = [
        [_chunk(tool_calls=[_call("fancy_none", {"q": "x"}, "n1")])],
        [_chunk(content="交付：没这个工具，做不了")],
    ]
    _box12 = {"script": _script12, "clients": [], "prompts": [], "max_tokens": []}
    _install_factory(_box12)
    _r12 = sa.submit(agent="deepseek", prompt="随便调一个没有的工具", timeout=30)
    check("★★【3】不传 tool/times → 没有任何预放行（向后兼容）",
          _r12.get("success") is True and not _r12.get("granted")
          and "预放行" not in str(_r12.get("text")),
          f"granted={_r12.get('granted')}")
    sa.wait_all(20)
    _st12 = sa.status(_r12["job"])
    check("★【3】不传时那个工具**不在**第一轮工具声明里",
          "fancy_none" not in (_box12["clients"][0].tools_seen[0]
                               if _box12["clients"] else []))
    _tr12 = [m["content"] for m in _box12["clients"][0].messages_seen[-1]
             if m.get("role") == "tool"]
    check("★★【3】不传时行为与第 59 轮一致：调用被拦下（「未授权」）",
          bool(_tr12) and "未授权" in _tr12[-1],
          (_tr12[-1][:60] if _tr12 else "(没有 tool 结果)"))
    check("★【3】不传时 grants 是空的（记账口径没变）",
          not (_st12.get("grants") or {}), str(_st12.get("grants")))

    # ③ 工具层：`subagent start` 的 tool/times 真的透传到 grants
    _box13 = {"script": [[_chunk(content="交付：走了工具层的 start")]],
              "clients": [], "prompts": [], "max_tokens": []}
    _install_factory(_box13)
    _j13 = json.loads(_mod2.execute(action="start", agent="deepseek", prompt="透传检查",
                                    tool="g1,g2", times=3))
    check("★【3】工具层 start 带 tool/times → 预放行真的落进 job",
          _j13.get("success") is True and _j13.get("granted") == ["g1", "g2"],
          str(_j13.get("granted")))
    _st13 = sa.status(_j13.get("job") or "")
    check("★【3】两个工具的放行次数都是 3",
          (_st13.get("grants") or {}) == {"g1": 3, "g2": 3}, str(_st13.get("grants")))
    sa.wait_all(20)
finally:
    sa.set_client_factory(None)
    sa.MIN_TIMEOUT = 10
    _sess_mod._session = _old_sess2


# ══════════════════ 12.【4】完成 → 主 LLM 下一轮的输入里**必含交付物** ══════════════════
print("\n== 12.【4】交付即感知：下一轮输入必含交付物；忙时排队、合并成一次 ==")

_cap = {"inputs": []}
_orig_run_loop = ChatSession._run_loop


def _fake_run_loop(self):
    """替身：不真的打 API，只把"这一轮**实际会发给模型的输入**"记下来。

    ★ 这是【4】那条硬要求唯一可信的验法：用户要的就是
      "子代理已交付 → 主 LLM 下一轮的输入里必定有它"，那就把那份输入抓出来看。
    """
    with self._lock:
        _snap = [dict(m) for m in self.messages]
    _cap["inputs"].append(
        self._prepare_messages(build_shared_context(_snap, self._character)))
    self._streaming = False
    if self.on_done:
        self.on_done()


ChatSession._run_loop = _fake_run_loop


def _mk_ui_session(messages=None, character="铃兰"):
    s = ChatSession.__new__(ChatSession)
    s._lock = threading.Lock()
    s.messages = list(messages or [])
    s._character = character
    s._topic_id = None
    s.db = None
    s._streaming = False
    s._summarizing = False
    s._abort_chat = False
    s._abort_tool = False
    s.on_done = None
    return s


class _UiChat:
    def __init__(self):
        self.agent_text_in = _Rec()
        self.busy = 0

    def start_busy(self):
        self.busy += 1

    def stop_busy(self):
        self.busy = max(0, self.busy - 1)


class _UiWin:
    pass


def _mk_win(sess):
    """借 MainWindow 的**真**方法（含 `_request_subagent_followup` / `_settle_*`）。"""
    w = _UiWin()
    w.chat = _UiChat()
    w.session = sess
    w.db = _FakeDB()
    w._pending_subagent_results = []
    w._pending_subagent_lines = []
    w._turn_active = False
    w._subagent_followup_active = False
    w._show_subagent_text = lambda a, t: MainWindow._show_subagent_text(w, a, t)
    w._on_subagent_msg = lambda a, t: MainWindow._on_subagent_msg(w, a, t)
    w._flush_pending_subagent_lines = (
        lambda: MainWindow._flush_pending_subagent_lines(w))
    w._flush_pending_subagent_results = (
        lambda: MainWindow._flush_pending_subagent_results(w))
    w._request_subagent_followup = (
        lambda j="": MainWindow._request_subagent_followup(w, j))
    w._settle_subagent_after_turn = (
        lambda: MainWindow._settle_subagent_after_turn(w))
    return w


def _wait_inputs(n, timeout=5.0):
    _end = time.time() + timeout
    while time.time() < _end and len(_cap["inputs"]) < n:
        time.sleep(0.02)
    return len(_cap["inputs"]) >= n


try:
    # ① 空闲时完成 → **立刻**触发一轮，且那一轮的输入里有交付物
    _cap["inputs"] = []
    _sess_a = _mk_ui_session()
    _w_a = _mk_win(_sess_a)
    MainWindow._on_subagent_msg(
        _w_a, "deepseek", "（deepseek 上线）我先去查一下，别急～")
    MainWindow._on_subagent_done(_w_a, {
        "job_id": "sa-77", "agent": "deepseek", "state": "done",
        "deliverable": "查到了：文件已经放在 D:\\out\\a.xlsx", "requests": []})
    _ok_a = _wait_inputs(1)
    check("★★★【4】空闲时子代理一交付 → **立刻**跑起主 LLM 的下一轮",
          _ok_a, f"{len(_cap['inputs'])} 轮")
    _in_a = "\n".join(str(m.get("content") or "") for m in (_cap["inputs"][0]
                                                           if _cap["inputs"] else []))
    check("★★★【4】那一轮的输入里**必定含那条交付物**（不可能再说「还在等」）",
          "已经放在 D:\\out\\a.xlsx" in _in_a, _in_a[-160:])
    check("★★【4】也明确告诉模型「这就是交付物、别再等」",
          "不要再等它" in _in_a)
    check("★★【4】中间发言**不在**那一轮的输入里（token 有界没被破）",
          "我先去查一下" not in _in_a, _in_a[:120])
    check("★★【4】交付物是以 **tool 结果**的身份进去的（结构上不是「我说的」）",
          [m.get("role") for m in _cap["inputs"][0]] == ["assistant", "tool"]
          and _cap["inputs"][0][1].get("tool_call_id")
          == _cap["inputs"][0][0]["tool_calls"][0]["id"])
    # ★★ 第 61 轮：主 LLM 续轮的输入里，交付物那条 tool 结果**就在最末位**
    #   （模型最容易看到的位置 —— 真机日志里"再说等"那轮根本不是这一轮）。
    check("★★★【61】续轮的 messages 里，交付物那条 tool 结果**就是最后一条**",
          len(_cap["inputs"][0]) == 2
          and _cap["inputs"][0][-1].get("role") == "tool",
          str([m.get("role") for m in _cap["inputs"][0]]))
    # ★★ 第 61 轮：子代理**一个字都没吐过**（快照里没有 lines）→ 兜底补显示一条，
    #   否则窗口里"派了活却什么都没有"；而且兜底那条同样不带内部 handle。
    _fb = _w_a.chat.agent_text_in.calls
    check("★★【61】兜底：子代理没吐过正文 → 交付物补显示一条，且**不带** handle",
          bool(_fb) and "a.xlsx" in str(_fb[-1][0]) and "sa-" not in str(_fb[-1][0]),
          str(_fb[-1] if _fb else []))

    # ② 主 LLM 正忙 → 只排队，**不许**叠一轮；本轮结束统一处理
    _cap["inputs"] = []
    _sess_b = _mk_ui_session()
    _w_b = _mk_win(_sess_b)
    _w_b._turn_active = True                     # 模拟"主 LLM 正在跑"
    MainWindow._on_subagent_done(_w_b, {
        "job_id": "sa-78", "agent": "deepseek", "state": "done",
        "deliverable": "结果：B 任务完成", "requests": []})
    check("★★★【4】主 LLM 正忙时**不触发新一轮**（不叠轮、不烧 token）",
          _cap["inputs"] == [] and _w_b._pending_subagent_results,
          str(_w_b._pending_subagent_results))
    check("★【4】忙的时候交付物只是排队（等本轮结束统一补）",
          len(_w_b._pending_subagent_results) == 1)
    # 本轮结束 → 走真的 `_settle_subagent_after_turn`
    _w_b._turn_active = False
    _n_b = MainWindow._settle_subagent_after_turn(_w_b)
    _ok_b = _wait_inputs(1)
    check("★★【4】本轮结束统一处理 → 这时才触发，且只触发**一次**",
          _n_b == 1 and _ok_b, f"flush={_n_b} 轮数={len(_cap['inputs'])}")
    _in_b = "\n".join(str(m.get("content") or "") for m in _cap["inputs"][0])
    check("★★★【4】触发那一轮的输入里含那条交付物",
          "B 任务完成" in _in_b, _in_b[-120:])

    # ③ 多条结果同时到达 → **合并成一次触发**
    _cap["inputs"] = []
    _sess_c = _mk_ui_session()
    _w_c = _mk_win(_sess_c)
    _w_c._turn_active = True                     # 忙：两条都只能排队
    MainWindow._on_subagent_done(_w_c, {
        "job_id": "sa-79", "agent": "deepseek", "state": "done",
        "deliverable": "交付物甲", "requests": []})
    MainWindow._on_subagent_done(_w_c, {
        "job_id": "sa-80", "agent": "铃兰", "state": "done",
        "deliverable": "交付物乙", "requests": []})
    _w_c._turn_active = False
    _n_c = MainWindow._settle_subagent_after_turn(_w_c)
    _wait_inputs(1)
    check("★★★【4】两条一起到 → **只触发一轮**（合并成一次，绝不各来一轮）",
          _n_c == 2 and len(_cap["inputs"]) == 1,
          f"flush={_n_c} 轮数={len(_cap['inputs'])}")
    _in_c = "\n".join(str(m.get("content") or "") for m in _cap["inputs"][0])
    check("★【4】两条交付物都在同一轮的输入里",
          "交付物甲" in _in_c and "交付物乙" in _in_c)

    # ④ "为交付物而续的那一轮"不会自我无限触发
    _cap["inputs"] = []
    _sess_d = _mk_ui_session()
    _w_d = _mk_win(_sess_d)
    MainWindow._on_subagent_done(_w_d, {
        "job_id": "sa-81", "agent": "deepseek", "state": "done",
        "deliverable": "只有一条", "requests": []})
    _wait_inputs(1)
    _w_d._turn_active = False
    MainWindow._settle_subagent_after_turn(_w_d)   # 这一轮本来就是为它续的
    time.sleep(0.2)
    check("★★【4】这一轮结束后**不会**再自我触发（否则无限烧 token）",
          len(_cap["inputs"]) == 1, f"轮数={len(_cap['inputs'])}")
    check("★【4】收尾后 `_subagent_followup_active` 已让出（后续交付还能触发）",
          _w_d._subagent_followup_active is False)

    # ⑤ `resume_after_subagent` 的守卫：忙 / 总结中 → 返回 False（不乱起轮次）
    _sess_e = _mk_ui_session()
    _sess_e._streaming = True
    check("★【4】已有流式请求在跑 → resume_after_subagent 返回 False",
          _sess_e.resume_after_subagent("x") is False)
    _sess_f = _mk_ui_session()
    _sess_f._summarizing = True
    check("★【4】正在总结话题 → 返回 False",
          _sess_f.resume_after_subagent("x") is False)

    # ══════════════ ⑥ 【61】忙时子代理的话延后显示（"交付了还说等"的根治）══════════════
    #   真机现象：子代理的"查好啦"先显示，然后**交付之前就已经在飞的那一轮**正文
    #   （"稍等一下下就好～"）才流出来 → 读起来像"主 LLM 无视了交付"。
    #   修法：主 LLM 正忙时，子代理的话（含最后那句=交付物）**不抢显示**，
    #   等本轮结束统一放 —— 顺序变成"主 LLM 说完 → 子代理的话 → 主 LLM 汇报"。
    print("\n== 12b.【61】忙时子代理的话延后显示：交付物在窗口里只出现一次 ==")
    _cap["inputs"] = []
    _DELIV_TEXT = "铃兰查好啦，明天（10-05）重庆的天气是：小雨转多云，18~21°C。"
    _sess_g = _mk_ui_session()
    _w_g = _mk_win(_sess_g)
    _w_g._turn_active = True                     # 模拟"主 LLM 这一轮还在跑"
    MainWindow._on_subagent_msg(_w_g, "铃兰", "（铃兰 上线）我是被叫来帮忙的，这就去做～")
    MainWindow._on_subagent_msg(_w_g, "铃兰", _DELIV_TEXT)   # 子代理最后那句 = 交付物
    check("★★★【61】主 LLM 正忙 → 子代理的话**先不显示**（否则会排在"
          "「主 LLM 还在说稍等」的前面）",
          _w_g.chat.agent_text_in.calls == [], str(_w_g.chat.agent_text_in.calls))
    check("★★【61】忙的时候中间发言**照旧立刻落库**（重启后还能看到中间过程）",
          len(_w_g.db.chat.added) == 2
          and all(a["ctx_exclude"] == 1 and a["speaker"] == "铃兰"
                  for a in _w_g.db.chat.added),
          str([(a["speaker"], a["ctx_exclude"]) for a in _w_g.db.chat.added]))
    MainWindow._on_subagent_done(_w_g, {
        "job_id": "sa-82", "agent": "铃兰", "state": "done",
        "deliverable": _DELIV_TEXT, "requests": [],
        "lines": ["（铃兰 上线）我是被叫来帮忙的，这就去做～", _DELIV_TEXT]})
    check("★★★【61】交付物**没有**被当成第二条聊天消息（忙的时候也不显示、也不抢跑）",
          _w_g.chat.agent_text_in.calls == [], str(_w_g.chat.agent_text_in.calls))
    _w_g._turn_active = False
    MainWindow._settle_subagent_after_turn(_w_g)
    _wait_inputs(1)
    _shown_g = [c[0] for c in _w_g.chat.agent_text_in.calls]
    check("★★★【61】本轮结束 → 子代理的话统一放出来，交付物那句话**只出现一次**",
          _shown_g.count(_DELIV_TEXT) == 1
          and "（铃兰 上线）我是被叫来帮忙的，这就去做～" in _shown_g,
          str([s[:14] for s in _shown_g]))
    check("★★【61】放出来的每条都带那个形象的 speaker（头像才对）",
          bool(_w_g.chat.agent_text_in.calls)
          and all(c[1] == "铃兰" for c in _w_g.chat.agent_text_in.calls))
    check("★【61】窗口里没有任何内部 handle",
          not any("sa-" in s for s in _shown_g), str(_shown_g))
    _in_g = "\n".join(str(m.get("content") or "") for m in _cap["inputs"][0])
    check("★★【61】紧接着续的那一轮：交付物在输入里、而且**在最后一条**"
          "（模型最先看到的位置）",
          _DELIV_TEXT in _in_g
          and [m.get("role") for m in _cap["inputs"][0]][-1:] == ["tool"],
          str([m.get("role") for m in _cap["inputs"][0]]))
finally:
    ChatSession._run_loop = _orig_run_loop

print(f"\n==== 结果: {len(PASS)} PASS / {len(FAIL)} FAIL ====")
for f in FAIL:
    print("  FAIL:", f)
sys.exit(1 if FAIL else 0)
