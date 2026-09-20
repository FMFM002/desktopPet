"""★ 1.9.2（R69）：LLM 的「临时汇报」工具 —— 长任务中途说一句话，不结束本轮。

【为什么要这个工具（用户的设计）】
  不要"分阶段输出流水线"（那会让日常简短聊天也变慢）。
  只要**提示词里写清楚**"处理复杂任务时，在合适的时候汇报一次情况，别让用户等久了"，
  再给它一条能真的把话说出去的通道，模型自己就会用。

【行为】
  · 把 `text` 立刻投递到聊天窗口（用户马上看到），**不结束本轮**，也不改变对话上下文；
  · 返回给模型的 text 里明确提醒："汇报完请继续完成任务，不要因此收尾"。
  · 日常闲聊不该调用 —— 这条也写在描述里，避免把短对话搞慢。

【约定】工具接口与其它工具一致：TOOL_NAME / TOOL_DESCRIPTION / TOOL_PARAMETERS
        + 同步 `execute(**kwargs) -> str`（返回 JSON 字符串，`text` 字段给模型看）。
"""
import json
import logging

from src.core import progress_bus as _bus

_log = logging.getLogger("Tool.ProgressReport")

TOOL_NAME = "progress_report"
TOOL_DESCRIPTION = (
    "在**长任务进行中**给用户说一句话（临时汇报），**不结束本轮对话**。"
    "★ 使用时机：正在做复杂任务（多步工具调用、批量下载、长文档处理、多轮搜索、"
    "画图/转写等要等的事情）时，在**合适的时候**汇报一次，"
    "让用户知道你在做什么、大概还要多久 —— **别让人干等**。"
    "★ 不要用的情况：日常简短聊天、一句话能答完的事、或者刚汇报过没进展时 —— "
    "滥用会让本来很快的对话变啰嗦变慢。"
    "★ 调用后请**继续完成任务**，不要因为汇报了就收尾。"
    "参数 text：给用户看的一句话（建议 10~40 字，人话，说清正在做什么，"
    "必要时带上大概还要多久）。"
)
TOOL_PARAMETERS = {
    "type": "object",
    "properties": {
        "text": {
            "type": "string",
            "description": "要汇报给用户的一句话（10~40 字，说清正在做什么/还要多久）",
        },
    },
    "required": ["text"],
    "additionalProperties": False,
}


def execute(text: str = "", **kwargs):
    text = (text or "").strip()
    if not text:
        return json.dumps({
            "success": False,
            "action": "progress_report",
            "text": "汇报内容为空，没有发送。请给一句具体的话。",
        }, ensure_ascii=False)
    ok = _bus.report(text, kind="progress")
    _log.info(f"[临时汇报] 工具被调用: {text[:80]}（送出={ok}）")
    return json.dumps({
        "success": bool(ok),
        "action": "progress_report",
        "reported": text[:200],
        "text": ("已把这句话发给用户了。**请继续完成任务**，"
                 "需要的话过一会儿再汇报一次；不要因为汇报而结束本轮。"),
    }, ensure_ascii=False)
