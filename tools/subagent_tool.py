"""★ 1.9.3 第 59 轮：**子代理工具** —— 把一个任务外包给另一个形象（用户定稿）。

【它做什么】
  `start` 把活交给某个形象，**立即返回**一个 handle（不阻塞主对话）；
  子代理在自己的线程里、用**自己的 messages** 干活（只带最小只读工具集），
  跑完由主窗口把**交付物**以 tool 结果的身份补进主上下文，并带着那个形象的头像
  显示 + 落库到聊天记录。`status` 查进度、`close` 中止、`list` 看有哪些形象，
  `approve_tools` 放行子代理申请的工具。
  ★ 第 60 轮：`start` 也接受 `tool`/`times` —— **预放行**（不必赌主 LLM 回头放行）；
  子代理的**中间发言**也会落库（`ctx_exclude=1`：重启后窗口里看得到，
  但**不进**主 LLM 上下文）。

【实现放在 src/core/subagent_jobs.py —— 不在这里】
  理由同 comfyui：LAZY 工具会被 ToolManager 空闲卸载并重新 import，
  模块级状态活不过一次卸载（工具塞进模块 A、主窗口拿到模块 B）。
  任务表 + worker 线程 + 回调必须活在 `src/core/`。本文件只做 action 分发。

【约定】与其它工具一致：TOOL_NAME / TOOL_DESCRIPTION / TOOL_PARAMETERS
        + 同步 `execute(**kwargs)`（返回 JSON 字符串，`text` 字段给模型看）。
"""
import json
import logging

from src.core import subagent_jobs as _jobs

_log = logging.getLogger("Tool.Subagent")

TOOL_NAME = "subagent"

TOOL_DESCRIPTION = (
    "★ 把一个任务**外包给另一个形象**去办（异步，不占你的时间）。"
    "适合：可以独立完成、比较琐碎/耗时、或者正好是那个形象拿手的活"
    "（查资料、看文件、跑一段查询…）。"
    "\n· `action=start`：叫某个形象来帮忙。**立即返回**一个 job handle，"
    "你继续聊你的，它做完会把**交付物**送回来（会以工具结果的身份出现在你面前）。"
    "参数 agent=形象名（必须是存在的角色，不能是你自己）、"
    "prompt=要它做的事（写清「只做这件事」）、context=你挑给它的少量关键记录。"
    "★ 它一上来只有最小只读集；**你现在就知道它缺哪个工具**的话，"
    "用 `tool` + `times` 在 start 时就**预放行**（例如 tool=\"download\"、times=2；"
    "要放行多个就写 tool=\"download,office\" 或 tool=[\"download\",\"office\"]）——"
    "这样它第一轮就能用，不必等你在 20 秒窗口里回头放行。"
    "\n· `action=status`：查它做得怎么样了（job=handle，留空=看全部）。"
    "**它如果申请了工具，会显示在这里**。"
    "\n· `action=approve_tools`：★ 放行它申请的工具（参数 job=handle、tool=工具名、times=次数）。"
    "它的工具集一开始被收走、只有最小只读集，缺工具时会向你申请 —— 你审核后放行。"
    "\n· `action=close`：中止它（job=handle）。"
    "\n· `action=list`：看看有哪些形象可用、以及它默认能用的只读工具。"
    "\n· 同一时间只允许 1 个子代理在跑；一次别派太长太重的活。"
)


TOOL_PARAMETERS = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["start", "status", "close", "list", "approve_tools"],
            "description": "start=叫个子代理来帮忙 / status=查进度 / close=中止 / "
                           "list=看可用形象 / approve_tools=放行它申请的工具",
        },
        "agent": {
            "type": "string",
            "description": "[start] 要叫谁（形象名，必须存在且不能是当前这个形象）",
        },
        "prompt": {
            "type": "string",
            "description": "[start] 任务内容 + 身份确认。"
                           "建议写成「你是被叫来帮忙的，只做这件事：…」",
        },
        "context": {
            "type": "string",
            "description": "[start] 你挑给它的**少量**关键聊天记录（可选，别贴一大段）",
        },
        "timeout": {
            "type": "integer",
            "description": "[start] 最长跑多少秒（默认 60，范围 10~300）",
        },
        "job": {
            "type": "string",
            "description": "[status/close/approve_tools] start 返回的 handle，例如 sa-1",
        },
        "tool": {
            "type": "string",
            "description": "[approve_tools/start] 工具名。approve_tools=要放行它申请的那个；"
                           "start=**预放行**（可省；要放行多个就写成 \"a,b\"）",
        },
        "times": {
            "type": "integer",
            "description": "[approve_tools/start] 放行几次（默认 1，上限 20）",
        },
    },
    "required": ["action"],
    "additionalProperties": False,
}


def _ok(payload: dict) -> str:
    payload.setdefault("action", "subagent")
    return json.dumps(payload, ensure_ascii=False)


def execute(action: str = "", **kwargs) -> str:
    """子代理工具的入口（只做参数校验 + 分发；真正的活在 src/core/subagent_jobs）"""
    act = str(action or "").strip()
    if act in ("", "help"):
        return _ok({
            "success": False,
            "text": ("要指定 action：start(叫个子代理) / status(查进度) / "
                     "close(中止) / list(看可用形象) / approve_tools(放行工具)。"),
        })

    if act == "start":
        res = _jobs.submit(
            agent=kwargs.get("agent") or "",
            prompt=kwargs.get("prompt") or "",
            context=kwargs.get("context") or "",
            timeout=kwargs.get("timeout") or _jobs.DEFAULT_TIMEOUT,
            # ★ 第 60 轮：start 时**预放行**（tool / times，可写多个）。
            #   不传 → `build_grants` 返回 {} → 行为与第 59 轮完全一致。
            grants=_jobs.build_grants(kwargs.get("tool"), kwargs.get("times") or 1),
        )
        if not res.get("success"):
            _log.warning(f"[子代理工具] start 被拒: {res.get('error')}")
        return _ok(res)

    if act == "status":
        return _ok(_jobs.status(str(kwargs.get("job") or "").strip()))

    if act == "close":
        job = str(kwargs.get("job") or "").strip()
        if not job:
            return _ok({"success": False, "error": "close 要带 job（那个 handle）。"})
        return _ok(_jobs.close(job))

    if act == "list":
        return _ok(_jobs.list_jobs())

    if act == "approve_tools":
        job = str(kwargs.get("job") or "").strip()
        if not job:
            return _ok({"success": False,
                        "error": "approve_tools 要带 job（那个 handle）和 tool（工具名）。"})
        return _ok(_jobs.approve_tools(job, str(kwargs.get("tool") or ""),
                                       kwargs.get("times") or 1))

    return _ok({"success": False,
                "error": f"未知 action「{act}」。可用：start/status/close/list/approve_tools。"})
