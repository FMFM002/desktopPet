"""
网页搜索工具

★ 1.9.0: 支持返回图片链接。
  1.9.0 开发日志第 1 条：「联网搜索工具，无法返回图片。方法：做成一个工具接口，
  获取url，下载到 data/downloads/websearch，然后发送到聊天窗口。」
  改动：
    · 请求带上 include_images=true（Tavily 支持），把结果里的图片链接取出来；
    · 结果文本里附上图片链接，并明确告诉模型「要发图就用 download 工具」；
    · 真正的下载与送进聊天窗口由 tools/download_tool.py 完成（通用能力，
      B站封面等其它来源也复用它）。
"""
import os
import requests
from dotenv import load_dotenv
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

TOOL_NAME = "web_search"
TOOL_DESCRIPTION = (
    "联网搜索信息。传入关键词进行网络搜索，返回搜索结果摘要与相关图片链接。"
    "★ 需要把图片发给用户时：拿到结果里的图片链接后，调 download 工具"
    "(url=图片链接) 把图下载下来，它会自动显示在聊天窗口里。"
)
TOOL_PARAMETERS = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": "搜索关键词",
        }
    },
    "required": ["query"],
}

# 最多带几张图回来（多了会挤占上下文）
MAX_IMAGES = 3


def _collect_images(data: dict) -> list[str]:
    """★ 1.9.0: 从 Tavily 响应里收集图片链接。

    Tavily 在 include_images=true 时返回 images 字段；不同版本可能是
    字符串列表，也可能是 [{url: …}, …]，两种都兼容。
    """
    raw = data.get("images") or []
    urls: list[str] = []
    if isinstance(raw, dict):
        raw = raw.get("results") or raw.get("images") or []
    if not isinstance(raw, (list, tuple)):
        return urls
    for item in raw:
        u = ""
        if isinstance(item, str):
            u = item
        elif isinstance(item, dict):
            u = str(item.get("url") or item.get("image_url") or "")
        u = u.strip()
        if u.lower().startswith(("http://", "https://")) and u not in urls:
            urls.append(u)
        if len(urls) >= MAX_IMAGES:
            break
    return urls


def execute(query: str) -> str:
    """联网搜索"""
    api_key = os.getenv("TAVILY_API_KEY", "")
    api_url = os.getenv("TAVILY_API_URL", "https://api.tavily.com/search")

    if not api_key:
        return f"模拟搜索: 关于'{query}'的结果 - 搜索API未配置"

    try:
        resp = requests.post(api_url, json={
            "api_key": api_key,
            "query": query,
            "search_depth": "basic",
            "max_results": 5,
            # ★ 1.9.0: 让搜索结果一并带回图片链接
            "include_images": True,
        }, timeout=10)
        # ★ 1.8.10: 先看 HTTP 状态。以前直接 resp.json()，
        #   于是 401/403/429 这类"返回 JSON 但没有 results 字段"的错误响
        #   被当成"搜索成功但没结果"，用户/模型都以为真的查无此事，
        #   实际是 Key 失效或被限流。
        if resp.status_code != 200:
            detail = ""
            try:
                _j = resp.json()
                detail = str(_j.get("detail") or _j.get("error") or "")[:120]
            except Exception:
                detail = (resp.text or "")[:120]
            return (f"[搜索失败] HTTP {resp.status_code}"
                    f"{' - ' + detail if detail else ''}"
                    f"（请检查 TAVILY_API_KEY 是否有效或是否被限流）")

        data = resp.json()
        if isinstance(data, dict) and (data.get("detail") or data.get("error")):
            return (f"[搜索失败] {str(data.get('detail') or data.get('error'))[:120]}"
                    f"（请检查 TAVILY_API_KEY 是否有效或是否被限流）")

        results = data.get("results", [])
        images = _collect_images(data)
        if not results and not images:
            return f"未找到关于'{query}'的结果"

        lines = [f"搜索'{query}':"]
        for i, r in enumerate(results[:5], 1):
            title = r.get("title", "无标题")
            content = r.get("content", "")[:150]
            lines.append(f"{i}. {title}")
            lines.append(f"   {content}")
        # ★ 1.9.0: 图片链接单独列出来，并告诉模型怎么把它们变成聊天里的图
        if images:
            lines.append("")
            lines.append(f"相关图片（共 {len(images)} 张）：")
            for i, u in enumerate(images, 1):
                lines.append(f"  图{i}: {u}")
            lines.append("需要把图片发给用户时，调用 download 工具"
                         "(url=上面的图片链接)，图片会自动出现在聊天窗口里。")
        return "\n".join(lines)
    except Exception as e:
        return f"搜索出错: {e}"
