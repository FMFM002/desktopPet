"""
提示词模块 — 所有 LLM 提示词集中管理
========================================
包含四个核心提示词：
  1. PIPELINE_SUMMARIZE  — 数据清洗 pipeline LLM 总结
  2. LTM_DETECT          — 长期记忆判断（YES/NO 二分类）
  3. LTM_EXTRACT         — 长期记忆结构化提取
  4. LTM_CONFLICT_MERGE  — 长期记忆冲突合并
"""
import json

# ============================================================================
# 1. 数据清洗 Pipeline — LLM 总结提示词
# ============================================================================

PIPELINE_SYSTEM = """你是一个智能桌面监控助手，负责分析用户的电脑活动记录并提炼为结构化事件。

## 你的任务
分析多条原始监控数据（键盘输入、窗口切换、进程信息、剪贴板内容、截图描述），
将其归纳为结构化的计算机使用事件列表。

## 输出格式（严格遵守）
返回一个 JSON 数组，每个元素包含：
{
  "time_start": "YYYY-MM-DD HH:MM:SS",   // 事件开始时间（必填）
  "time_end": "YYYY-MM-DD HH:MM:SS",     // 事件结束时间（可为 null 表示单时间点）
  "event": "简短事件标签",                 // 如: 编写代码, 浏览网页, 视频会议, 阅读文档
  "detail": "详细描述（50字以内）",         // 具体做了什么
  "confidence": 85                        // 推测度 0-100，100=确定事实，<70=猜测
}

## 事件分类参考
- 编写代码: IDE 中有键盘输入 + 进程信息显示 code/vscode 等
- 浏览网页: 窗口标题含浏览器 + URL 信息
- 文档处理: 键盘输入 + 窗口标题含 Word/Excel/WPS/Notion
- 聊天通讯: 键盘输入 + 窗口标题含微信/QQ/Discord/Slack
- 观看视频: 窗口标题含 B站/YouTube/Netflix 且无键盘输入
- 游戏娱乐: 进程信息含 steam/game 等
- 系统操作: 终端命令、文件管理、系统设置
- 空闲: 长时间无键盘输入且窗口无变化

## 重要规则
1. 多条原始数据可能属于同一个事件 → 合并为一条
2. 纯快捷键操作（Ctrl+C/V/Alt+Tab）→ 不作为独立事件
3. 窗口切换但不产生实际内容 → 不记录
4. 浏览器子进程、GPU 进程等 → 忽略
5. 空内容或内容过短（<5 个有效字符）→ 忽略
6. 如果一批数据中没有任何有价值内容 → 返回空数组 []
7. 只输出 JSON 数组，不要任何解释文字
"""

PIPELINE_USER_TEMPLATE = """请分析以下 {count} 条原始监控数据，提炼为结构化事件：

{events}

请直接返回 JSON 数组。"""


def build_pipeline_messages(raw_events: list[dict]) -> list[dict]:
    """构建 pipeline 总结的 messages"""
    # 格式化原始事件文本
    lines = []
    for evt in raw_events:
        source = evt.get("source", "?")
        type_ = evt.get("type", "")
        content = evt.get("content", "")
        created = evt.get("created_at", "")
        file_path = evt.get("file_path", "")

        prefix = f"[{source}]"
        if type_:
            prefix += f" [{type_}]"
        line = f"({created}) {prefix} {content}"
        if file_path:
            line += f" 📎{file_path}"
        lines.append(line)

    events_text = "\n".join(lines)
    user_msg = PIPELINE_USER_TEMPLATE.format(
        count=len(raw_events), events=events_text)

    return [
        {"role": "system", "content": PIPELINE_SYSTEM},
        {"role": "user", "content": user_msg},
    ]


# ============================================================================
# 2. 长期记忆判断 — YES/NO 二分类
# ============================================================================

LTM_DETECT_SYSTEM = """你是一个信息价值判断器。你的唯一任务是判断一段对话文本是否包含值得长期记忆的信息。

## 必须记住（YES）的情况：
### 规则层（硬性规则，无需判断）:
- 用户明确说"记住...""记一下...""别忘了..." → YES
- 用户说"我是...""我叫...""我的...是..." → YES
- 用户透露了身份信息（姓名、职业、角色、地点）→ YES
- 用户透露了偏好（"我喜欢...""我不喜欢...""我习惯..."）→ YES
- 用户透露了计划/日程（"我明天要...""下周...""XX 号要..."）→ YES

### LLM 自主判断层：
- 用户讨论技术栈、工具偏好、工作习惯 → 值得记住
- 用户透露系统环境、配置偏好 → 值得记住  
- 用户提及重要的文件路径、项目名称 → 值得记住
- 用户表露情绪模式、对某类事物倾向 → 值得记住
- ★ 1.9.0【经验】一次"踩坑 → 解决"的过程（某个报错/某步操作反复失败后怎么绕过去的、
  哪条命令/哪个参数才有效）→ 值得记住：这是"下次能更快解决"的本钱

## 不应该记住（NO）的情况：
- 摸头、抱抱、"今天好吗"等纯互动/问候 → NO
- 纯粹的闲聊、天气讨论、娱乐八卦 → NO
- 一次性问答（"现在几点""XX 是什么意思"）→ NO
- 工具调用过程中的中间请求 → NO
- 用户只是测试功能（"试试""测试一下"）→ NO
- 系统信息查询结果（天气、搜索结果等）→ NO

## 输出格式
只输出一个单词: YES 或 NO
不要任何解释、标点或换行。
"""


def build_ltm_detect_messages(chat_text: str) -> list[dict]:
    return [
        {"role": "system", "content": LTM_DETECT_SYSTEM},
        # ★ 1.7.1: 强制单行输出，禁用推理模型 thinking 习惯
        {"role": "user", "content": (
            "严格规则：不要思考、不要解释、不要分析。"
            "看完对话后，最后一行只输出一个单词 YES 或 NO。\n\n"
            f"对话如下：\n{chat_text}"
        )},
    ]


# ============================================================================
# 3. 长期记忆结构化提取
# ============================================================================

LTM_EXTRACT_SYSTEM = """你是一个长期记忆提取器。从文本中提取值得永久存储的用户信息。

## 提取字段
每条记忆包含：
{
  "variable_name": "简短英文标识",    // 如 user_name, tech_stack, favorite_editor
  "value": "记忆内容",              // 用中文自然语言描述
  "category": "分类",              // 见下方分类表
  "confidence": 0.85               // 0.0-1.0，确定性
}

## 分类表
| category    | 示例 variable_name       | 说明                       |
|-------------|-------------------------|---------------------------|
| identity    | user_name, occupation   | 身份信息                   |
| preference  | preferred_language, like_food | 个人偏好               |
| tech        | favorite_editor, tech_stack | 技术栈/工具偏好           |
| schedule    | plan_20260101, next_meeting | 计划/日程               |
| project     | current_project, workspace_path | 项目/工作信息         |
| relationship| friend_xxx, coworker_xxx | 人际关系                |
| habit       | work_hours, break_habit | 习惯/作息                |
| experience  | ★ 1.9.0 见下方「经验」规则 | 踩坑与解决办法（怎么绕过去的） |
| system      | os_version, device_info  | 系统环境信息              |
| other       | —                       | 其他无法归类的             |

## 经验（★ 1.9.0，放在"小本子"里的那一类）
用户希望桌宠能积累经验：**同一个困难第二次遇到时，能翻出上次怎么解决的**。
所以遇到"踩坑 → 解决"的过程，请提取成 category="experience" 的一条，value 按
「什么情况 / 为什么不行 / 最后怎么解决」三小段写清楚，variable_name 用
`exp_<简短英文场景>`（如 exp_ocr_tesseract_missing、exp_bili_cover_referer）。
这类记忆的 confidence 给 0.9 以上 —— 它是验证过的结论，不是猜测。

## 规则
1. 一条 variable_name 只对应一条数据，同一类信息合并到同一个变量名
2. 不要提取临时性、一次性信息
3. 不要提取系统查询结果（天气、搜索内容等）
4. 分类不确定时用 "other"
5. confidence: 用户明确说的 → 0.9-1.0，从上下文推断的 → 0.5-0.8
6. variable_name 用英文 snake_case
7. ★ 经验类只记**可复用的做法**，不要记"这次查到了多少度/搜到了什么"

## 输出格式
返回 JSON 数组。如果没有值得提取的内容，返回空数组 []。
只输出 JSON，不要任何解释文字。
"""


def build_ltm_extract_messages(text: str) -> list[dict]:
    return [
        {"role": "system", "content": LTM_EXTRACT_SYSTEM},
        {"role": "user", "content": f"请从以下文本中提取长期记忆：\n\n{text}"},
    ]


# ============================================================================
# 4. 长期记忆冲突合并
# ============================================================================

LTM_MERGE_SYSTEM = """你是一个长期记忆合并决策器。当新信息和已有记忆冲突时，决定如何合并。

## 场景
已有的旧记忆和新提取的信息指向同一个 variable_name（可能是精确匹配或近似匹配）。

## 你要做的
对每一组冲突，选择一种处理方式并给出最终值：

### 处理方式
- "replace": 新信息更准确/更新 → 完全替换旧值
- "merge": 新旧信息互补 → 合并为更完整的描述
- "keep_old": 旧信息更可靠 → 保留旧值不变
- "keep_new": 新信息更可靠 → 使用新值（不改变 variable_name）

## 判断依据
1. 信息来源可靠性：用户明确说的 > 上下文推断的 > 单次观察的
2. 时间新鲜度：新旧信息的时效性（近期偏好 > 历史偏好）
3. 明确程度：具体陈述 > 模糊暗示
4. 一致性：不矛盾的信息可以合并，矛盾的信息保留更可靠的

## 输出格式
返回 JSON 数组：
[
  {
    "variable_name": "xxx",
    "action": "replace|merge|keep_old|keep_new",
    "value": "最终值（action=keep_old 时可省略）",
    "category": "分类",
    "confidence": 0.85,
    "reason": "一句话理由"
  }
]

只输出 JSON 数组，不要任何解释文字。
"""


def build_ltm_merge_messages(conflicts: list[dict]) -> list[dict]:
    """
    conflicts: [{"variable_name": ..., "old_value": ..., "new_value": ...,
                  "old_confidence": ..., "new_confidence": ...,
                  "old_category": ..., "new_category": ...}, ...]
    """
    lines = ["以下变量存在新旧冲突，请决策：\n"]
    for i, c in enumerate(conflicts):
        lines.append(f"--- 冲突 {i+1} ---")
        lines.append(f"变量名: {c['variable_name']}")
        lines.append(f"旧值: {c['old_value']} (置信度: {c['old_confidence']}, 分类: {c.get('old_category', 'other')})")
        lines.append(f"新值: {c['new_value']} (置信度: {c['new_confidence']}, 分类: {c.get('new_category', 'other')})")
        lines.append("")

    return [
        {"role": "system", "content": LTM_MERGE_SYSTEM},
        {"role": "user", "content": "\n".join(lines)},
    ]


# ============================================================================
# JSON 解析 + 验证帮助函数
# ============================================================================

def parse_json_response(content: str) -> list | dict | None:
    """安全解析 LLM 返回的 JSON（支持 markdown code block）"""
    if not content:
        return None
    text = content.strip()

    # 去掉 markdown code block
    if text.startswith("```"):
        lines = text.split("\n")
        # 去掉第一行 ```json 和最后一行 ```
        text = "\n".join(lines[1:]) if len(lines) > 1 else text
        if text.endswith("```"):
            text = text[:-3]

    # 提取 JSON 数组/对象
    text = text.strip()
    if not text:
        return None

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # 尝试提取第一个 [ 或 { 之间的内容
        for start_char, end_char in [('[', ']'), ('{', '}')]:
            start = text.find(start_char)
            if start >= 0:
                end = text.rfind(end_char)
                if end > start:
                    try:
                        return json.loads(text[start:end + 1])
                    except json.JSONDecodeError:
                        continue
        return None


def validate_pipeline_events(data: list) -> list[dict]:
    """验证 pipeline 输出的 JSON 数组，过滤无效条目"""
    if not isinstance(data, list):
        return []
    valid = []
    for item in data:
        if not isinstance(item, dict):
            continue
        if not item.get("event") or len(str(item["event"]).strip()) < 2:
            continue
        # 修正字段
        valid.append({
            "time_start": str(item.get("time_start", "")),
            "time_end": str(item.get("time_end", "")) if item.get("time_end") else None,
            "event": str(item.get("event", ""))[:100],
            "detail": str(item.get("detail", ""))[:500],
            "confidence": int(min(max(item.get("confidence", 50), 0), 100)),
        })
    return valid


def validate_ltm_extract(data: list) -> list[dict]:
    """验证 LTM 提取输出的 JSON 数组"""
    if not isinstance(data, list):
        return []
    valid = []
    for item in data:
        if not isinstance(item, dict):
            continue
        vn = str(item.get("variable_name", "")).strip().lower()
        if not vn or len(vn) < 2:
            continue
        value = str(item.get("value", "")).strip()
        if not value or len(value) < 2:
            continue
        valid.append({
            "variable_name": vn,
            "value": value[:500],
            "category": str(item.get("category", "other"))[:50],
            "confidence": float(min(max(item.get("confidence", 0.5), 0.0), 1.0)),
        })
    return valid


def validate_ltm_merge(data: list) -> list[dict]:
    """验证 LTM 合并输出的 JSON 数组"""
    if not isinstance(data, list):
        return []
    valid = []
    for item in data:
        if not isinstance(item, dict):
            continue
        action = str(item.get("action", ""))
        if action not in ("replace", "merge", "keep_old", "keep_new"):
            continue
        result = {
            "variable_name": str(item.get("variable_name", "")),
            "action": action,
            "reason": str(item.get("reason", ""))[:200],
        }
        if action in ("replace", "merge", "keep_new"):
            result["value"] = str(item.get("value", ""))[:500]
            result["category"] = str(item.get("category", "other"))[:50]
            result["confidence"] = float(min(max(item.get("confidence", 0.5), 0.0), 1.0))
        valid.append(result)
    return valid
