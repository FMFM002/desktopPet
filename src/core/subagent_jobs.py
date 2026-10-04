#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""★ 1.9.3 第 59 轮：**子代理（subagent）** —— 把一个任务外包给另一个形象。

【用户定稿的设计（1.9.3wsl开发日志.txt 第六十二节·三，逐条对照）】
  1. **工具被没收**：子代理启动时只给**最小工具集**（只读 + 简单查询），危险工具不给；
  2. 它可以**汇报"申请工具 + 次数"并说明意图**，由主 LLM 审核；
  3. 工具新增 action：**放行工具调用**（"工具名"、"次数"）→ `approve_tools`；
  4. **独立 persona 要保留原性格** —— 用它自己角色的人设，不是通用"子代理"人格；
  5. **汇报到聊天窗口的内容要写入 `chat_records`**（带 `speaker` = 那个形象名）；
  6. 子代理输出**以 `tool` 结果的身份**进主 LLM 上下文（结构上不会被误认成主 LLM 自己说的）；
  7. **异步 + 汇报**：`start` 立即返回 handle（不阻塞），跑完把结果作为一条消息推进对话；
  8. **只回传"交付物 + 极简状态"**，不回传整段会话（token 有界）；
  9. ★ "给人看的"与"给模型看的"分开：聊天窗口逐条显示（带自己头像），
     主 LLM 上下文只放那一小段交付物；
 10. **硬上限**：并发 1、超时、低 `max_tokens`、工具调用轮数上限。

【为什么放在 src/core/ 而不是 tools/（与 comfyui_jobs 同一个理由）】
  ToolManager 对 LAZY 工具是"空闲就卸载、下次重新 import"，**模块级变量活不过一次卸载**：
  工具把任务塞进模块 A，主窗口 import 到的是模块 B，永远读不到结果。
  → 任务表、worker 线程、回调都必须活在 `src/core/` 里，进程启动到退出都是同一份。

【绝不做的一件事】
  子代理**用自己的 messages 列表**（`_run_one` 里的局部变量），
  **绝不去碰 `session.messages`** —— 那是所有人共享的工作台，写进去就等于
  把子代理的整段会话灌进主上下文（违反第 8 条），而且会破坏
  `assistant(tool_calls) ↔ tool` 的配对（API 400）。交付物由主窗口走
  `_flush_pending_subagent_results()` 以 **tool 结果的形态**补进主上下文。

【回调（主窗口在启动时注册；本模块在 src/core，工具被卸载不影响回调）】
  · set_message_callback(cb)  cb(agent, text)  → 把子代理这条"给人看"的话显示进聊天窗口
  · set_done_callback(cb)     cb(job_dict)     → 跑完/失败：显示+落库交付物、推进主上下文
  ★ 回调都在 **worker 线程**里被调用，主窗口侧必须走 Signal 回主线程。
"""
from __future__ import annotations

import copy
import json
import logging
import re
import threading
import time
from pathlib import Path

_log = logging.getLogger("SubagentJobs")

BASE_DIR = Path(__file__).resolve().parent.parent.parent
CHARACTERS_DIR = BASE_DIR / "data" / "桌宠形象"

# ══════════════════ 硬上限（用户第 10 条）══════════════════
MAX_CONCURRENT = 1            # 并发上限：同时只允许 1 个子代理
MAX_JOBS_KEPT = 20            # 状态表里最多留几条（跑完的也留着，好让 status 还能查）
DEFAULT_TIMEOUT = 60          # 默认超时（秒）
MIN_TIMEOUT = 10
MAX_TIMEOUT = 300
MAX_TOOL_ROUNDS = 4           # 工具调用轮数上限（超过就要求它交交付物）
SUBAGENT_MAX_TOKENS = 1000    # ★ 低 max_tokens（主对话是 8192）：子代理只交"一小段交付物"
MAX_DELIVERABLE_CHARS = 2000  # 交付物截断上限（回主上下文的那段）
MAX_EMIT_CHARS = 1200         # 单条"给人看的"消息截断上限
WAIT_APPROVAL_SECONDS = 20    # 申请工具后，最多等主模型放行多久（期间 state=waiting_approval）

# ══════════════════ 工具集：**没收**后的最小只读集 ══════════════════
# 键 = 工具名；值 = 允许的 action 白名单（None = 这个工具本身就只有查询类动作）。
# ★ 危险动作（写文件 / 删文件 / 发文件 / 杀进程 / 开应用 / 写长期记忆 …）一律不在里面，
#   想要就走"申请 → 主模型放行"那条路（见 approve_tools）。
SUBAGENT_SAFE_TOOLS: dict = {
    "file_ops": ("view", "read", "search", "list", "list_downloads"),
    "web_search": None,
    "weather_query": None,
    "system_command": ("get_ip_info", "get_local_ip", "system_info", "get_time",
                       "list_processes", "disk_usage", "memory_usage"),
}

# 注入给子代理的**结构性**申请入口（不是真工具，由本模块直接处理）
REQUEST_TOOLS_NAME = "request_tools"
_REQUEST_TOOLS_SPEC = {
    "type": "function",
    "function": {
        "name": REQUEST_TOOLS_NAME,
        "description": (
            "★ 申请使用一个你现在**没有**的工具。你的工具集被主模型收走了，"
            "只有最小只读集；缺工具时**不要硬扛**，用这个函数提出申请："
            "写清工具名、需要几次、为什么。申请会转交给主模型审核，"
            "通过之后你的工具列表里就会出现它。"
            "如果等不到放行，就用现有工具尽力做完，并在交付物里说明缺什么。"),
        "parameters": {
            "type": "object",
            "properties": {
                "tool": {"type": "string",
                         "description": "工具名，例如 shell_exec / download / pptx / office"},
                "times": {"type": "integer",
                          "description": "希望放行的调用次数（默认 1）"},
                "reason": {"type": "string",
                           "description": "为什么需要它（一句话，说清要拿它干什么）"},
            },
            "required": ["tool", "reason"],
            "additionalProperties": False,
        },
    },
}

# ── 跨模块状态（工具卸载不影响）──
_lock = threading.Lock()
_jobs: dict = {}            # job_id -> 任务信息（含已完成的，便于 status 复查）
_seq = 0                    # job_id 自增序号（锁内取，保证唯一）
_message_cb = None          # (agent, text)
_done_cb = None             # (job_dict)
_client_factory = None      # ★ 测试注入用：fn(system_prompt, max_tokens, tools) -> client


# ══════════════════ 回调注册 ══════════════════

def set_message_callback(cb):
    """注册"子代理说了一句话 → 显示进聊天窗口"的回调（agent, text）"""
    global _message_cb
    _message_cb = cb


def set_done_callback(cb):
    """注册"子代理跑完了"的回调（job_dict → 显示+落库交付物、推进主上下文）"""
    global _done_cb
    _done_cb = cb


def set_client_factory(fn):
    """★ 只给回归用：注入一个假的 LLM 客户端工厂，避免测试真打 API。

    fn(system_prompt, max_tokens, tools) -> 具备 `chat_stream(messages)` 的对象。
    生产环境不设（None）→ 走 `_SubagentClient`（真的 ChatClient 子类）。
    """
    global _client_factory
    _client_factory = fn


def _call(fn, *args):
    if not callable(fn):
        return
    try:
        fn(*args)
    except Exception as e:
        _log.warning(f"[子代理] 回调执行失败: {e}")


def _emit(job_id: str, agent: str, text: str):
    """把子代理的这条话送出去"给人看"（聊天窗口），并记进任务表（给 status 看）。

    ★ 只走显示通道：**不写 session.messages、不落库**（那是交付物专属的待遇）。
    """
    text = (text or "").strip()
    if not text:
        return
    text = text[:MAX_EMIT_CHARS]
    with _lock:
        job = _jobs.get(job_id)
        if job is not None:
            job.setdefault("lines", []).append(text)
            del job["lines"][:-12]
    _call(_message_cb, agent, text)


# ══════════════════ 形象 / 人设（第 4 条：保留原性格）══════════════════

def list_characters() -> list:
    """列出 `data/桌宠形象/` 下所有可用形象名（settings.json 里的 name 优先）。"""
    out = []
    try:
        from src.core import character_settings as _cs
        for d in sorted(CHARACTERS_DIR.iterdir()):
            if not d.is_dir():
                continue
            name = d.name
            sfile = d / "settings.json"
            if sfile.exists():
                try:
                    data, _ = _cs.load(sfile)
                    name = str(data.get("name") or "").strip() or d.name
                except Exception:
                    name = d.name
            if name not in out:
                out.append(name)
    except Exception as e:
        _log.debug(f"[子代理] 扫描形象目录失败: {e}")
    return out


def _find_character_dir(name: str):
    """按形象名找角色目录：先按目录名，再按 settings.json 里的 name。"""
    name = (name or "").strip()
    if not name:
        return None
    try:
        cands = [d for d in CHARACTERS_DIR.iterdir() if d.is_dir()]
    except Exception:
        return None
    for d in cands:
        if d.name == name:
            return d
    try:
        from src.core import character_settings as _cs
        for d in cands:
            sfile = d / "settings.json"
            if not sfile.exists():
                continue
            try:
                data, _ = _cs.load(sfile)
            except Exception:
                continue
            if str(data.get("name") or "").strip() == name:
                return d
    except Exception as e:
        _log.debug(f"[子代理] 按 settings.json 匹配形象失败: {e}")
    return None


def load_persona(agent: str):
    """取那个角色**自己的人设**（return (显示名, persona 文本)；找不到返回 ("", "")）。

    ★ 第 4 条的关键：走 `character_settings`（角色自己的 settings.json），
      **不碰全局 config、也不编一个"我是子代理"的通用人格**。
      这样子里代理说话还是它自己的性格（娱乐性靠这里）。
    """
    d = _find_character_dir(agent)
    if d is None:
        return "", ""
    name = d.name
    persona = ""
    try:
        from src.core import character_settings as _cs
        sfile = d / "settings.json"
        if sfile.exists():
            data, _was_old = _cs.load(sfile)
            name = str(data.get("name") or "").strip() or d.name
            persona = _cs.persona_text(data)
    except Exception as e:
        _log.warning(f"[子代理] 读取形象「{agent}」人设失败: {e}")
    return name, persona


def current_main_character() -> str:
    """当前主形象名（"你是谁叫来的"那句话里要用）。

    优先读会话单例（**不复用 get_session()：那会在没有会话时把库和话题建出来**），
    退回全局配置里的 `pet.character`。
    """
    try:
        from src.core import session as _sess_mod
        s = getattr(_sess_mod, "_session", None)
        if s is not None:
            nm = (getattr(s, "character", "") or "").strip()
            if nm:
                return nm
    except Exception as e:
        _log.debug(f"[子代理] 读会话形象名失败: {e}")
    try:
        from src.utils.config import get_config
        return (getattr(get_config().pet, "character", "") or "").strip()
    except Exception:
        return ""


def build_system_prompt(agent_name: str, main_name: str, persona: str) -> str:
    """拼子代理的**独立系统提示词**：它自己的人设 + 一句"你是被谁叫来的"。

    ★★ 为什么那句"被谁叫来的"不能省（第 47/48 轮真机探针实测）：
      不明确说明身份，模型会把"别人的委托"当成"用户在跟我聊天"，
      或者把主形象的历史发言认领成自己说的 —— 必乱。
    ★ 与共享工作台那套的区别：子代理**有自己的 messages**（里面没有别的说话者），
      所以这里**不需要** `_SHARED_CONTEXT_NOTE`，只需要说清"我和主形象的关系"。
    """
    head = (persona or "").strip() or f"你是「{agent_name}」。"
    return (
        head + "\n\n"
        "══════════════════════════════════════\n"
        f"（子代理身份）你是「{agent_name}」，现在被「{main_name}」叫来帮忙做一件事。\n"
        f"· 这不是用户在跟你闲聊 —— 是「{main_name}」把这件事外包给你了，"
        f"做完把结果交回去就行。\n"
        "· **保持你自己的性格和说话方式**（你就是你），但**任务优先**："
        "先把事做完，再谈聊天。\n"
        "· 你手上只有一套**最小只读工具集**。缺工具时用 `request_tools` "
        "申请（写清工具名、次数、理由），主模型会审核。等不到放行就用现有工具尽力做完，"
        "并在交付物里说明缺什么。\n"
        f"· 最多做 {MAX_TOOL_ROUNDS} 轮工具调用，之后必须收尾。\n"
        "· 最后**只输出一小段交付物**（结论 / 结果 / 文件路径 / 数据）："
        "不要复述过程、不要长篇大论。"
    )


def build_task_text(main_name: str, prompt: str, context: str = "") -> str:
    """拼子代理的第一条 user 消息（任务 + 主 LLM 挑给它的少量记录）。"""
    parts = [f"【{main_name} 的委托】", f"任务：{prompt.strip()}"]
    ctx = (context or "").strip()
    if ctx:
        parts.append("\n主对话里的相关记录（供你参考，不要复述）：\n" + ctx[:2000])
    parts.append("\n做完后直接给出**一小段交付物**（一两段以内）。")
    return "\n".join(parts)


# ══════════════════ 工具列表（没收 + 申请入口）══════════════════

def subagent_tools() -> list:
    """子代理能看到的工具声明 = 最小只读集 + `request_tools` 申请入口。"""
    out = []
    try:
        from src.core.tool_manager import get_tool_manager
        tm = get_tool_manager()
    except Exception as e:
        _log.warning(f"[子代理] 取工具管理器失败: {e}")
        tm = None
    if tm is not None:
        for name in SUBAGENT_SAFE_TOOLS:
            info = tm.get_tool(name)
            if info is None:
                continue
            try:
                out.append(_narrow_action_enum(info, name))
            except Exception as e:
                _log.debug(f"[子代理] 收窄工具 {name} 声明失败: {e}")
    out.append(copy.deepcopy(_REQUEST_TOOLS_SPEC))
    return out


def _narrow_action_enum(info, name: str) -> dict:
    """把工具的 openai 声明收窄成"只列允许的 action"。

    ★ 为什么必须收窄（而不是只靠运行期拦截）：模型是照着 **enum** 挑 action 的，
      留着 write/kill_process 这些选项，它就会去调，然后拿到一句"未授权" →
      白烧一轮。收窄之后它一眼就知道自己只有哪些只读动作。
    ★ **必须 deepcopy**：`to_openai_tool()` 返回的是 `ToolInfo.parameters` 的引用，
      就地改会把**主 LLM 的工具表**也一起改坏（主 LLM 就再也不能写文件了）。
    """
    spec = copy.deepcopy(info.to_openai_tool())
    allowed = SUBAGENT_SAFE_TOOLS.get(name)
    fn = spec.get("function") or {}
    params = fn.get("parameters") or {}
    props = params.get("properties") or {}
    act = props.get("action")
    if allowed is not None and isinstance(act, dict) and act.get("enum"):
        act["enum"] = [a for a in act["enum"] if a in allowed]
        act["description"] = (str(act.get("description") or "")
                              + "（子代理只能用这些只读动作）")
    fn["description"] = (str(fn.get("description") or "")
                         + "（子代理只读版：写入/删除/发送等动作已收走）")
    return spec


def tools_for(job_id: str) -> list:
    """★ 第 60 轮：子代理**这一轮实际拿到的工具表**。

    = 最小只读集 + 申请入口 + **主模型已放行的工具**（真的声明出来）。

    ★ 为什么必须在这里重算（第 59 轮的一个真实缺口）：
      第 59 轮只把放行记进 `job["grants"]`，**从来没有再改过 client 的工具表** ——
      模型根本"看不到"那个工具，`approve_tools` 那句"它下一轮就能用了"是空话
      （`_test_subagent_193` 第 7 段能假通过，是因为假客户端的脚本**硬写**了要调它）。
      预放行（start 的 tool/times）同理：不真的进工具表 = 预放行等于没放。
    ★ 放行的工具**不做 action 收窄**：放行就是放行（用户可以放行任意工具）。
    """
    out = subagent_tools()
    have = {(t.get("function") or {}).get("name") for t in out}
    with _lock:
        names = list((_jobs.get(job_id) or {}).get("granted") or [])
    if not names:
        return out
    try:
        from src.core.tool_manager import get_tool_manager
        tm = get_tool_manager()
    except Exception as e:
        _log.warning(f"[子代理] 取放行工具时工具管理器失败: {e}")
        return out
    for name in names:
        if not name or name in have:
            continue
        info = tm.get_tool(name)
        if info is None:
            _log.warning(f"[子代理 {job_id}] 放行的工具「{name}」不存在，跳过")
            continue
        try:
            out.append(info.to_openai_tool())
            have.add(name)
        except Exception as e:
            _log.debug(f"[子代理] 放行工具 {name} 的声明生成失败: {e}")
    return out


# ══════════════════ 预放行（start 的 tool / times）══════════════════

MAX_GRANT_TIMES = 20


def _clamp_times(v, default: int = 1) -> int:
    """放行次数夹到 1~20（与 `approve_tools` 同一口径）。"""
    try:
        return max(1, min(MAX_GRANT_TIMES, int(v)))
    except Exception:
        return default


def build_grants(tool, times=1) -> dict:
    """把 `start` 的 **tool / times** 参数归一成 `{工具名: 次数}`。

    用户要的是"**start 时就预放行**"，所以参数得让模型好写、也好写多个：
      · `tool` 省略/空        → 不预放行（**与第 59 轮行为完全一致**）；
      · `tool="ppt"`          → 放行 1 次；
      · `tool="ppt,shell"`    → 两个都放行（也认中文逗号 / 顿号 / 分号 / 空格）；
      · `tool=["ppt","shell"]`（或它们混着写）→ 同上；
      · `times=3`             → 上面每个都放行 3 次；
      · `times=[1,2]`         → 按位置一一对应，缺的按 1 次。
    """
    if not tool:
        return {}
    out: dict = {}
    if isinstance(tool, dict):
        for k, v in tool.items():
            name = str(k or "").strip()
            if name:
                out[name] = _clamp_times(v)
        return out
    raw = list(tool) if isinstance(tool, (list, tuple, set)) else [tool]
    names: list = []
    for item in raw:
        if isinstance(item, dict):                 # 允许 {"tool": "x", "times": 2}
            name = str(item.get("tool") or item.get("name") or "").strip()
            if name:
                out[name] = _clamp_times(item.get("times"), _clamp_times(times))
            continue
        if isinstance(item, (list, tuple)):        # 允许 ("x", 2)
            name = str(item[0] or "").strip() if item else ""
            if name:
                out[name] = _clamp_times(item[1] if len(item) > 1 else 1)
            continue
        names.extend([s for s in re.split(r"[,，、;；\s]+", str(item or "")) if s])
    tlist = list(times) if isinstance(times, (list, tuple)) else None
    for i, name in enumerate(names):
        if tlist is not None:
            out[name] = _clamp_times(tlist[i] if i < len(tlist) else 1)
        else:
            out[name] = _clamp_times(times)
    return out


# ══════════════════ 提交 ══════════════════

def _active_ids() -> list:
    """活跃任务 id（调用方自己保证持锁或只读）"""
    return [jid for jid, j in _jobs.items()
            if j.get("state") in ("running", "waiting_approval", "cancelling")]


def submit(agent: str, prompt: str, context: str = "",
           timeout: int = DEFAULT_TIMEOUT, grants: dict = None) -> dict:
    """交给某个形象去办一件事 → **立刻返回 handle，不阻塞**。

    ★ 第 60 轮：`grants` = **预放行**（`{工具名: 次数}`，由 `build_grants` 归一）。
      传了就等于"start 的那一刻就把工具批给它" —— 不必再赌主 LLM 在 20 秒窗口里
      主动 `status` + `approve_tools`（真机上主 LLM 常常 `start` 完就收尾，
      于是没人放行，子代理只能降级做完、把"缺 X"写进交付物）。
      **不传（None/空）→ 行为与第 59 轮完全一致**，一个字都不变。

    返回 {"success": True, "job": "sa-1", "agent": ..., "state": "running", "text": ...}
    """
    agent = (agent or "").strip()
    prompt = (prompt or "").strip()
    if not prompt:
        return {"success": False, "error": "prompt 是空的：请把要它做的事写清楚。"}
    if not agent:
        return {"success": False,
                "error": "agent 是空的：要叫谁？（必须是存在的形象名）",
                "characters": list_characters()}

    main_name = current_main_character()
    if main_name and agent == main_name:
        return {"success": False,
                "error": (f"「{agent}」就是你本人。子代理要叫**另一个形象**，"
                          f"否则它回来的交付物会被当成你自己说的（身份就乱了）。"),
                "characters": [c for c in list_characters() if c != main_name]}

    disp_name, persona = load_persona(agent)
    if not disp_name:
        return {"success": False,
                "error": f"找不到形象「{agent}」。",
                "characters": list_characters()}

    # 预放行：只认非空工具名；次数夹到 1~20（与 approve_tools 同口径）
    pre = {}
    try:
        for _k, _v in (grants or {}).items():
            _k = str(_k or "").strip()
            if _k:
                pre[_k] = _clamp_times(_v)
    except Exception as e:
        _log.debug(f"[子代理] 预放行参数解析失败（按不预放行处理）: {e}")
        pre = {}

    with _lock:
        if len(_active_ids()) >= MAX_CONCURRENT:
            cur = _active_ids()[0]
            return {"success": False, "busy": True, "job": cur,
                    "error": (f"并发上限是 {MAX_CONCURRENT}：现在「{_jobs[cur].get('agent')}」"
                              f"（{cur}）还在跑。可以用 subagent status 看进度，"
                              f"或 close 中止它再来。")}
        try:
            timeout = max(MIN_TIMEOUT, min(MAX_TIMEOUT, int(timeout or DEFAULT_TIMEOUT)))
        except Exception:
            timeout = DEFAULT_TIMEOUT
        job_id = _next_job_id_locked()
        _jobs[job_id] = {
            "job_id": job_id, "agent": disp_name, "asked_as": agent,
            "main": main_name, "prompt": prompt, "context": context or "",
            "timeout": timeout, "state": "running",
            "created": time.time(), "finished": None, "elapsed": 0.0,
            "rounds": 0, "grants": dict(pre), "requests": [],
            "granted": list(pre),
            "lines": [], "deliverable": "", "error": "", "cancel": False,
            "persona": persona,
        }
        _prune_jobs_locked()

    threading.Thread(target=_run_one, args=(job_id,), daemon=True,
                     name=f"subagent-{job_id}").start()
    _log.info(f"[子代理] {job_id} 已派出：{disp_name}（上限 {timeout}s，"
              f"人设 {len(persona)} 字"
              f"{'，预放行 ' + '、'.join(f'{k}×{v}' for k, v in pre.items()) if pre else ''}）")
    _pre_note = ("已预放行：" + "、".join(f"{k}×{v}" for k, v in pre.items()) + "。"
                 if pre else "")
    return {"success": True, "action": "subagent", "job": job_id, "agent": disp_name,
            "state": "running", "granted": sorted(pre),
            "text": (f"已经把活交给「{disp_name}」（{job_id}），它自己在后台做，"
                     f"不会占着你。{_pre_note}做完它的交付物会自动回来。"
                     f"期间可以 subagent status 看进度。")}


def _next_job_id_locked() -> str:
    """取号（**调用方必须已持 `_lock`** —— 避免在锁里再抢同一把不可重入锁）。"""
    global _seq
    _seq += 1
    return f"sa-{_seq}"


def _prune_jobs_locked():
    """状态表最多留 MAX_JOBS_KEPT 条（只删**已经跑完**的，绝不删活跃的）。

    ★ **调用方必须已持 `_lock`**：`_lock` 是不可重入的 `threading.Lock`，
      这里再抢一次就是自己等自己（第 51 轮真机事故的同一个形状）。
    """
    if len(_jobs) <= MAX_JOBS_KEPT:
        return
    done = [(j.get("finished") or 0, jid) for jid, j in _jobs.items()
            if j.get("state") not in ("running", "waiting_approval", "cancelling")]
    done.sort()
    for _ts, jid in done[:max(0, len(_jobs) - MAX_JOBS_KEPT)]:
        _jobs.pop(jid, None)


# ══════════════════ 查询 / 中止 / 放行 ══════════════════

def status(job_id: str = "") -> dict:
    """查进度：running/waiting_approval/done/failed/timeout/cancelled
    + 已完成部分 + **它申请了哪些工具**（第 2 条：给主 LLM 审核看的）。"""
    with _lock:
        if not job_id:
            return {"success": True, "jobs": [_brief(_jobs[i]) for i in list(_jobs.keys())],
                    "active": _active_ids()}
        job = _jobs.get(job_id)
        if job is None:
            return {"success": False, "error": f"没有这个子代理任务：{job_id}",
                    "jobs": [_brief(_jobs[i]) for i in list(_jobs.keys())]}
        d = dict(job)
        d.pop("cancel", None)
        d.pop("persona", None)
        d["elapsed"] = round((d.get("finished") or time.time()) - d["created"], 1)
        d["success"] = True
        # 只给最近几条"给人看的"话，避免 status 自己变成一大坨 token
        d["lines"] = (d.get("lines") or [])[-6:]
        d["text"] = _status_text(d)
        return d


def _brief(job: dict) -> dict:
    return {"job": job.get("job_id"), "agent": job.get("agent"),
            "state": job.get("state"),
            "requests": len(job.get("requests") or []),
            "prompt": (job.get("prompt") or "")[:60]}


def _status_text(d: dict) -> str:
    """给模型看的一句话状态（极简；不堆整段会话）。"""
    state = d.get("state")
    if state in ("running", "waiting_approval", "cancelling"):
        tail = ("（它正在等你放行工具）" if state == "waiting_approval" else "")
        return (f"「{d.get('agent')}」（{d.get('job')}）还在跑{tail}，"
                f"已经 {int(d.get('elapsed') or 0)} 秒、{d.get('rounds')} 轮工具调用。")
    if state == "done":
        return f"「{d.get('agent')}」已经做完了（{int(d.get('elapsed') or 0)} 秒）。"
    return (f"「{d.get('agent')}」这次没做成（{state}）："
            f"{(d.get('error') or '')[:120]}")


def approve_tools(job_id: str, tool: str, times: int = 1) -> dict:
    """★ 第 3 条：主 LLM **放行**「工具名 × 次数」。

    放行后子代理下一轮的工具列表里就会出现它；次数用完自动收回。
    """
    tool = (tool or "").strip()
    if not tool:
        return {"success": False, "error": "tool 是空的：要放行哪个工具？"}
    try:
        times = max(1, min(20, int(times or 1)))
    except Exception:
        times = 1
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            return {"success": False, "error": f"没有这个子代理任务：{job_id}"}
        if job.get("state") not in ("running", "waiting_approval"):
            return {"success": False, "state": job.get("state"),
                    "error": (f"「{job_id}」已经结束了（{job.get('state')}），"
                              f"再放行也没人用了。")}
        job["grants"][tool] = int(job["grants"].get(tool, 0)) + times
        if tool not in job["granted"]:
            job["granted"].append(tool)
        _log.info(f"[子代理] 主模型放行 {job_id} 的 {tool} × {times}")
    return {"success": True, "job": job_id, "tool": tool, "times": times,
            "text": (f"已放行「{tool}」× {times} 次给「{job_id}」，"
                     f"它下一轮就能用了。")}


def close(job_id: str) -> dict:
    """中止正在跑的子代理（真中止：置取消位 + 关掉 HTTP 流）。

    ★ 为什么**不**直接复用 `session.abort_chat()`：那个 `_abort_chat` 是
      **主对话**的中止位，动它等于把主 LLM 自己这一轮也一起掐了 ——
      这里要中止的只是子代理那条线程。做法与 `_run_loop` 中止流式请求
      **是同一套**（置位 → break → `stream.close()`），所以中止是真的。
    """
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            return {"success": False, "error": f"没有这个子代理任务：{job_id}"}
        done_already = job.get("state") not in ("running", "waiting_approval")
        job["cancel"] = True
        if not done_already:
            job["state"] = "cancelling"
    if done_already:
        return {"success": True, "job": job_id, "state": job["state"],
                "text": f"「{job_id}」早就结束了（{job['state']}）。"}
    # 有界等待（最多 3 秒）：让 close 的返回值尽量反映"真的停了"
    for _ in range(30):
        with _lock:
            st = (_jobs.get(job_id) or {}).get("state")
        if st not in ("running", "waiting_approval", "cancelling"):
            return {"success": True, "job": job_id, "state": st,
                    "text": f"已经中止「{job_id}」。"}
        time.sleep(0.1)
    return {"success": True, "job": job_id, "state": "cancelling",
            "text": (f"已经给「{job_id}」发了中止信号，它正在收尾"
                     f"（如果正卡在网络读取上，要等这次请求返回）。")}


def list_jobs() -> dict:
    with _lock:
        return {"success": True,
                "jobs": [_brief(_jobs[i]) for i in list(_jobs.keys())],
                "active": _active_ids(),
                "characters": list_characters(),
                "safe_tools": sorted(SUBAGENT_SAFE_TOOLS.keys()),
                # ★★ 1.9.3 第 61 轮：**这份结果里必须自带"还能 start"**。
                #   真机那次模型调完 `action=list`、读完这份 JSON 之后，在思考里
                #   断言"schema 只提到 list，action enum 只有 [list]" —— 而那份
                #   schema 明明是五个值（见 tools/subagent_tool.py 的 TOOL_PARAMETERS）。
                #   它把**这份"名册"当成了工具能力的全部**，于是自己把天气查了。
                #   光靠工具描述不够，这里在它"刚读完的那段文本"里再给一次实话：
                #   能力清单 + 下一步该怎么调（两个字段既给人看、也给模型看）。
                "actions": ["start", "status", "close", "list", "approve_tools"],
                "hint": ("要派活就调 subagent(action=\"start\", agent=\"形象名\", "
                         "prompt=\"要它做的事\")；想先放行工具可再加 tool=\"工具名\"。"
                         "list 只是名册，不是这个工具能力的全部。")}


def wait_all(timeout: float = 60.0) -> bool:
    """等所有子代理跑完（给自测/收尾用，生产代码不用调）。

    ★ 与 comfyui_jobs.wait_all 同一个理由：看**任务状态表**，不看队列长度 ——
      否则会撞上"线程起了但状态还没标上"的极短窗口、提前返回 True。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        with _lock:
            busy = bool([j for j in _jobs.values()
                         if j.get("state") in ("running", "waiting_approval", "cancelling")])
        if not busy:
            return True
        time.sleep(0.05)
    return False


# ══════════════════ 客户端 ══════════════════

class _SubagentClient:
    """子代理专用的 ChatClient 外壳：**系统提示词换成目标形象的人设**。

    ★ 为什么用"包一层"而不是改全局 config：`ChatClient._build_system_prompt()` 读的是
      `config.pet.personality_prompt`（当前形象的人设）。要临时换成另一个形象的人设，
      最脏的做法是改全局配置 —— 那会把**主形象**的人设也一起改掉（真机上就是
      "叫一次子代理，主人格被换了"）。这里覆盖 `_build_system_prompt` 一个 lambda 解决。
    ★ 低思考强度直接白拿：`chat_stream()` 内部调 `self._thinking_low_kwargs()`
      （用户定稿要求复用现成件），服务端不认时的自动降级也是现成的。
    """

    def __init__(self, system_prompt: str, max_tokens: int = SUBAGENT_MAX_TOKENS):
        from src.core.llm_client import ChatClient
        self._inner = ChatClient()
        # ★ 低 max_tokens（第 10 条硬上限之一）：只够它交一小段交付物
        self._inner.max_tokens = max(128, int(max_tokens))
        self._inner._build_system_prompt = lambda: system_prompt

    def set_tools(self, tools: list):
        self._inner.set_tools(tools)

    def chat_stream(self, messages: list):
        return self._inner.chat_stream(messages)


def _make_client(system_prompt: str, max_tokens: int, tools: list):
    """造客户端（回归里被 `set_client_factory` 换掉，绝不打真 API）。"""
    if callable(_client_factory):
        return _client_factory(system_prompt, max_tokens, tools)
    return _SubagentClient(system_prompt, max_tokens)


# ══════════════════ worker ══════════════════

class _SubagentAbort(Exception):
    """被 close 中止。

    ★ `partial`：**正在吐的正文别丢** —— 超时/中止时也要交代半成品，
      否则主模型拿到的只是"（没做完）"，前面查到的线索全白费。
    """

    def __init__(self, msg: str = "", partial: str = ""):
        super().__init__(msg)
        self.partial = partial


class _SubagentTimeout(Exception):
    """超时（同 `_SubagentAbort`，带半成品正文）。"""

    def __init__(self, msg: str = "", partial: str = ""):
        super().__init__(msg)
        self.partial = partial


def _job_snapshot(job_id: str) -> dict:
    with _lock:
        return dict(_jobs.get(job_id) or {})


def _check_stop(job_id: str, deadline: float):
    """每读一个流包都查一次：被中止 / 超时 → 抛出去真收尾。"""
    with _lock:
        if (_jobs.get(job_id) or {}).get("cancel"):
            raise _SubagentAbort("已被中止")
    if time.time() > deadline:
        raise _SubagentTimeout("超时")


def _run_one(job_id: str):
    job = _job_snapshot(job_id)
    agent = job.get("agent") or ""
    main_name = job.get("main") or "主人"
    deadline = time.time() + float(job.get("timeout") or DEFAULT_TIMEOUT)
    partial = ""
    try:
        system_prompt = build_system_prompt(agent, main_name, job.get("persona") or "")
        # ★ 第 60 轮：工具表**按 job 现算**（最小只读集 + 预放行/已放行的工具）。
        #   预放行的工具必须在**第一轮**就出现在声明里，否则"预放行"等于没放。
        tools = tools_for(job_id)
        client = _make_client(system_prompt, SUBAGENT_MAX_TOKENS, tools)
        client.set_tools(list(tools))
        # ★★ 独立的 messages 列表 —— 局部变量，**绝不写进 session.messages**
        messages = [{"role": "user",
                     "content": build_task_text(main_name, job.get("prompt") or "",
                                                job.get("context") or "")}]
        _emit(job_id, agent, f"（{agent} 上线）我是被「{main_name}」叫来帮忙的，"
                             f"这就去做～")
        _log.info(f"[子代理 {job_id}] 开工：{agent}（{len(tools)} 个工具）")

        rounds = 0
        final = ""
        while rounds < MAX_TOOL_ROUNDS:
            _check_stop(job_id, deadline)
            rounds += 1
            # ★ 第 60 轮：**每轮重算工具表** —— 主模型在上一轮放行（approve_tools）
            #   或 start 时预放行的工具，从这一轮起就真的出现在声明里，模型看得到、调得动。
            try:
                client.set_tools(tools_for(job_id))
            except Exception as e:
                _log.debug(f"[子代理 {job_id}] 刷新工具表失败（沿用上一轮）: {e}")
            content, thinking, tool_calls = _stream_once(client, messages, job_id, deadline)
            with _lock:
                if job_id in _jobs:
                    _jobs[job_id]["rounds"] = rounds
            if content.strip():
                final = content.strip()
                partial = final
                _emit(job_id, agent, final)
            if not tool_calls:
                break
            # ── 这一轮的 assistant(tool_calls) 写进**子代理自己的**历史 ──
            messages.append({
                "role": "assistant", "content": None,
                "tool_calls": [{"id": tc["id"], "type": "function",
                                "function": dict(tc["function"])} for tc in tool_calls],
                # 思考模式要求每条 assistant 都带回 reasoning_content（缺了直接 400）
                "reasoning_content": thinking or " ",
            })
            for tc in tool_calls:
                _check_stop(job_id, deadline)
                name = (tc.get("function") or {}).get("name") or ""
                try:
                    args = json.loads((tc.get("function") or {}).get("arguments") or "{}")
                except Exception:
                    args = {}
                if not isinstance(args, dict):
                    args = {}
                if name == REQUEST_TOOLS_NAME:
                    result = _handle_request(job_id, agent, args, deadline)
                else:
                    result = _run_tool(job_id, name, args)
                messages.append({"role": "tool", "tool_call_id": tc["id"],
                                 "content": str(result)[:4000]})
        if not final:
            # ★ 轮数用满还没交付物 → 再要一次"纯交付物"（这一轮**不给工具**，
            #   所以不算"工具调用轮"，不违反轮数上限；否则子代理会一个字都不交）
            try:
                client.set_tools([])
                content, _th, _tc = _stream_once(
                    client,
                    messages + [{"role": "user", "content":
                                 "（轮数用满了）现在只输出最后的交付物："
                                 "一小段结论/结果，不要再调工具、不要再解释过程。"}],
                    job_id, deadline)
                if content.strip():
                    final = content.strip()
                    partial = final
                    _emit(job_id, agent, final)
            except (_SubagentAbort, _SubagentTimeout):
                raise
            except Exception as e:
                _log.debug(f"[子代理 {job_id}] 收尾请求失败: {e}")
        if not final:
            final = partial or "（子代理没给出交付物）"
        _finish(job_id, ok=True, deliverable=final)
    except _SubagentAbort as e:
        _finish(job_id, ok=False, state="cancelled", error=str(e),
                deliverable=getattr(e, "partial", "") or partial)
    except _SubagentTimeout as e:
        _finish(job_id, ok=False, state="timeout",
                error=f"{e}（{job.get('timeout')} 秒还没做完）",
                deliverable=getattr(e, "partial", "") or partial)
    except Exception as e:
        _log.exception(f"[子代理 {job_id}] 异常: {e}")
        _finish(job_id, ok=False, state="failed",
                error=f"{type(e).__name__}: {e}", deliverable=partial)


def _stream_once(client, messages: list, job_id: str, deadline: float):
    """发一次流式请求 → (正文, 思考, tool_calls)。

    ★ 为什么用 `chat_stream` 而不是 `chat`：只有读**流**才能在超时/中止时
      立刻 break + `stream.close()`（真的把 HTTP 请求掐掉）。`chat()` 要等
      整个请求返回，超时就变成"事后才发现"，不满足"超时要真能中止"。
    """
    stream = client.chat_stream(messages)
    content, thinking, buf = "", "", []
    try:
        for chunk in stream:
            _check_stop(job_id, deadline)
            delta = chunk.choices[0].delta if getattr(chunk, "choices", None) else None
            if delta is None:
                continue
            if getattr(delta, "reasoning_content", None):
                thinking += delta.reasoning_content
            if getattr(delta, "content", None):
                content += delta.content
            _merge_tool_calls(buf, getattr(delta, "tool_calls", None))
    except BaseException as _e:
        # 中止/超时/网络错 → 与 session._run_loop 同一套做法：显式 close，真把请求断掉
        try:
            stream.close()
        except Exception:
            pass
        if isinstance(_e, (_SubagentAbort, _SubagentTimeout)):
            _e.partial = content      # 半成品正文带出去（超时也要能交代）
        raise
    return content, thinking, [tc for tc in buf if tc is not None]


def _merge_tool_calls(buf: list, delta_calls):
    """流式 tool_calls 分片合并（与 session._run_loop 里的口径一致）。"""
    for tc in (delta_calls or []):
        if getattr(tc, "index", None) is None or getattr(tc, "function", None) is None:
            continue
        idx = tc.index
        while len(buf) <= idx:
            buf.append(None)
        if buf[idx] is None:
            buf[idx] = {"id": tc.id or "",
                        "function": {"name": tc.function.name or "",
                                     "arguments": tc.function.arguments or ""}}
        elif tc.function and tc.function.arguments:
            buf[idx]["function"]["arguments"] += tc.function.arguments or ""


def _handle_request(job_id: str, agent: str, args: dict, deadline: float) -> str:
    """处理 `request_tools`（第 2 条：向主模型申请工具 + 次数）。

    ★ 申请记进 `job["requests"]` —— `status` 和最后的交付物里都会带上，
      所以主 LLM **一定看得到**"它申请了什么、要几次、为什么"。
    ★ 记完之后**有界等一会**（WAIT_APPROVAL_SECONDS）：主模型在这个窗口里
      `approve_tools` 就立刻生效；等不到就如实告诉子代理"没批"，让它用现有工具做完。
    """
    tool = str(args.get("tool") or "").strip()
    reason = str(args.get("reason") or "").strip()
    try:
        times = max(1, min(20, int(args.get("times") or 1)))
    except Exception:
        times = 1
    if not tool:
        return ("申请无效：没写工具名。请按 "
                "{\"tool\": \"file_ops\", \"times\": 2, \"reason\": \"…\"} 重申请。")
    req = {"tool": tool, "times": times, "reason": reason[:200],
           "at": round(time.time(), 1)}
    with _lock:
        job = _jobs.get(job_id)
        if job is not None:
            job["requests"].append(req)
            job["state"] = "waiting_approval"
    _log.info(f"[子代理 {job_id}] 申请工具 {tool} × {times}：{reason[:80]}")
    _emit(job_id, agent, f"（{agent} 申请用一下「{tool}」×{times} 次：{reason[:80]}）")

    ok = False
    budget = min(WAIT_APPROVAL_SECONDS, max(2.0, deadline - time.time()))
    end = time.time() + budget
    while time.time() < end:
        with _lock:
            job = _jobs.get(job_id) or {}
            if job.get("cancel"):
                break
            ok = int((job.get("grants") or {}).get(tool, 0)) > 0
        if ok:
            break
        time.sleep(0.2)
    with _lock:
        job = _jobs.get(job_id)
        if job is not None and job.get("state") == "waiting_approval":
            job["state"] = "running"
    if ok:
        _emit(job_id, agent, f"（主模型批了「{tool}」，我接着做）")
        return (f"已放行「{tool}」×{times} 次。现在可以调用它了 —— "
                f"次数有限，别浪费。")
    return (f"暂时没等到放行「{tool}」。**不要卡在这里**：用你现有的工具尽力做完，"
            f"并在交付物里说明「缺 {tool}，所以哪一步没做」。")


def _safe_action_ok(name: str, args: dict) -> bool:
    """这个调用**光靠最小只读集**就合法吗（不靠"放行"）？

    ★ 抽出来是为了让"要不要扣放行次数"判断准确：只有**靠放行**才成立的调用才扣次数。
    """
    if name not in SUBAGENT_SAFE_TOOLS:
        return False
    allowed = SUBAGENT_SAFE_TOOLS[name]
    if allowed is None:
        return True
    return str(args.get("action") or "") in allowed


def _tool_allowed(job: dict, name: str, args: dict) -> bool:
    """最小只读集（且 action 在白名单里）**或** 主模型已放行的剩余次数。

    ★ 第 60 轮修正：**放行优先**。
      原来先查最小只读集、命中就只按白名单裁决 —— 于是
      `approve_tools(job, "file_ops")` 对 `write` **完全无效**：放行了照样是"未授权"，
      "放行"这个功能在**最需要它的那个工具**上是空话（预放行同理）。
      现在：只要有剩余放行次数就直接放行（放行就是放行，次数由 `_run_tool` 扣）。
    """
    try:
        if int((job.get("grants") or {}).get(name, 0)) > 0:
            return True
    except Exception:
        pass
    return _safe_action_ok(name, args)


def _run_tool(job_id: str, name: str, args: dict) -> str:
    """执行一个子代理的工具调用（**先过授权**：没授权就回一句让它去申请）。"""
    from src.core.tool_manager import get_tool_manager
    job = _job_snapshot(job_id)
    if not name:
        return "工具名为空，调用无效。"
    if not _tool_allowed(job, name, args):
        return (f"[未授权] 工具「{name}」不在你的最小只读集里（或者这个动作被禁）。"
                f"请用 request_tools 申请：把工具名、次数、理由写清楚。")
    # ★ 放行的次数要真的扣掉（不是无限用）：只有**靠放行**才成立的调用才扣
    #   （只读集里本来就允许的调用，不该消耗主模型批的次数）
    if not _safe_action_ok(name, args):
        with _lock:
            j = _jobs.get(job_id)
            if j is not None:
                j["grants"][name] = max(0, int((j.get("grants") or {}).get(name, 0)) - 1)
    _emit(job_id, job.get("agent") or "",
          f"〔动作〕用了一下 {name}({str(args.get('action') or '')[:20]})")
    _log.info(f"[子代理 {job_id}] 调工具 {name}({list(args.keys())})")
    try:
        return str(get_tool_manager().execute_tool(name, **args))
    except Exception as e:
        return f"[工具执行出错: {name}] {e}"


def _finish(job_id: str, ok: bool, state: str = "", error: str = "",
            deliverable: str = ""):
    """收尾：定状态、裁交付物、（失败时）给一句给人看的说明，然后叫 done 回调。

    ★ 交付物是**唯一**回主上下文的东西（第 8 条：token 有界）；
      子代理自己的整段 messages **就地丢弃**，从不外传。
    """
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            return
        if job.get("state") == "cancelling" and not state:
            state = "cancelled"
        job["state"] = state or ("done" if ok else "failed")
        job["error"] = error or ""
        job["finished"] = time.time()
        job["elapsed"] = round(job["finished"] - job["created"], 1)
        deliverable = (deliverable or "").strip()
        if len(deliverable) > MAX_DELIVERABLE_CHARS:
            deliverable = deliverable[:MAX_DELIVERABLE_CHARS] + "…（已截断）"
        if not deliverable:
            deliverable = ("（没做出东西）" if ok else
                           f"（没做完：{error or job['state']}）")
        job["deliverable"] = deliverable
        snapshot = dict(job)
        snapshot.pop("cancel", None)
        snapshot.pop("persona", None)
    if not ok:
        _emit(job_id, snapshot.get("agent") or "",
              f"（{snapshot.get('agent')} 这边没弄完："
              f"{(error or snapshot['state'])[:120]}）")
    _log.info(f"[子代理 {job_id}] 结束 state={snapshot['state']} "
              f"耗时={snapshot.get('elapsed')}s 交付物={len(deliverable)} 字")
    _call(_done_cb, snapshot)
