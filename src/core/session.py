"""
会话循环模块 v2.0
- LLM2 多轮对话循环
- 工具调用与结果返回
- 流式输出支持
- ★ 话题系统：多话题管理、自动标题、Token 限制
"""
import json
import re
import threading
import time
import logging
from pathlib import Path
from typing import Optional, Callable

from src.core.reply_filter import (ProtocolTailFilter, ToolMarkerTailFilter,
                                   split_protocol)
# ★ 1.9.3: 共享工作台标识（所有形象共用同一个话题/上下文）
from src.core.database import SHARED_CHARACTER

_log = logging.getLogger("Session")

# ★ 1.9.3（用户 2026-10-04 拍板）：喂给 LLM 的上下文 = 最近 N 条原始消息 + 更早的摘要。
RECENT_CONTEXT_LIMIT = 20


def build_shared_context(messages: list, current_character: str = "",
                         summary: str = "",
                         limit: int = RECENT_CONTEXT_LIMIT) -> list:
    """★ 1.9.3: 把共享工作台的原始消息装配成**喂给 LLM 的上下文**。

    规则（用户定稿，真机 6 轮验证过）：
      · `role='user'`            → 原样 user；
      · `role='assistant'` 且 speaker == 当前形象（或 speaker 为空/NULL）→ 原样 assistant（无前缀）；
      · `role='assistant'` 且 speaker 是**别的**形象 → 变成 `user` 角色，内容 `[<speaker>]：<content>`；
      · `role='tool'` 且**当前列表里有配对的 assistant.tool_calls** → 原样保留
        （本轮对话循环里工具结果必须回传给模型，否则工具调用整条断掉）；
        配不上对的就是历史遗留/孤儿工具结果 → 跳过（放进请求会 400）。

    ★ 1.9.3 第 60 轮：带 `ctx_exclude` 标记的消息（子代理的**中间发言**）**一律跳过** ——
      它们落库只是为了"重启后窗口里还看得到"，对主 LLM 而言**不存在**
      （定稿第 8 条：只回传交付物，token 有界）。窗口渲染走 `_reload_chat_view`，
      读的是 `session.messages`，**不受这里影响**。

    最后只把**最近 `limit` 条**非 system 消息放进去（system 消息始终保留），
    更早的那段折成一条 system 摘要；`summary` 为空时只注明"更早的消息已省略" ——
    不为此新造一个会调 LLM 的同步轮子。

    `messages` 里的每条消息用可选的 `speaker` 键标明说话者。
    """
    # 先收集"这份列表里真实存在的工具调用 id"——只有它们对应的 tool 结果才跟着走。
    # （库里加载出来的 tool 消息旧行为就是跳过的，这里保持一致；本轮循环里的必须留。）
    _paired_tc_ids = set()
    for m in messages:
        if m.get("role") == "assistant":
            for tc in (m.get("tool_calls") or []):
                if isinstance(tc, dict) and tc.get("id"):
                    _paired_tc_ids.add(tc["id"])

    kept: list = []
    for m in messages:
        if m.get("ctx_exclude"):
            continue                      # ★ 只给人看的（子代理中间发言）→ 不进上下文
        role = m.get("role")
        if role == "tool":
            if m.get("tool_call_id") in _paired_tc_ids:
                kept.append(m)            # 本轮工具调用链的一部分，必须回传
            continue                      # 孤儿工具结果不进上下文（否则 400）
        if role == "assistant" and m.get("tool_calls"):
            kept.append(m)                # 工具调用链必须保持 assistant
            continue
        if role != "assistant":
            kept.append(m)
            continue
        speaker = (m.get("speaker") or "").strip()
        name = (current_character or "").strip()
        if speaker and name and speaker != name:
            kept.append({"role": "user",
                         "content": f"[{speaker}]：{m.get('content') or ''}"})
        else:
            # speaker 为空（历史遗留）或就是当前形象 → 当作"我自己说的"
            kept.append(m)

    system_msgs = [m for m in kept if m.get("role") == "system"]
    convo = [m for m in kept if m.get("role") != "system"]
    if limit and len(convo) > limit:
        dropped = len(convo) - limit
        convo = convo[-limit:]
        note = (summary or "").strip() or f"（更早的 {dropped} 条消息已省略）"
        system_msgs = system_msgs + [{
            "role": "system",
            "content": f"[更早的对话摘要，供你了解来龙去脉，不用复述]\n{note}",
        }]
        _log.debug(f"共享上下文装配：折叠更早的 {dropped} 条，保留最近 {limit} 条")
    return system_msgs + convo

# 项目根目录（多模态直传时把相对图片路径转成绝对路径）
_BASE_DIR = Path(__file__).resolve().parent.parent.parent

# ★ 1.4.0+: 爬虫命令拦截正则（编译一次，全局复用）
_CRAWLER_SHELL_PATTERNS: list[tuple[re.Pattern, str]] = [
    # curl/wget 爬取网页（不拦截 where curl / curl --version 等无 URL 的）
    (re.compile(r'\bcurl\b.*https?://', re.IGNORECASE), "curl 爬取网页"),
    (re.compile(r'\bwget\b.*https?://', re.IGNORECASE), "wget 爬取网页"),
    # Python 内联爬虫脚本
    (re.compile(r'python[23]?\s+-c\s+".*?(?:urllib\.request|urllib2|requests\.|scrapy|BeautifulSoup|bs4)', re.IGNORECASE | re.DOTALL), "Python 内联爬虫"),
    (re.compile(r"python[23]?\s+-c\s+'.*?(?:urllib\.request|urllib2|requests\.|scrapy|BeautifulSoup|bs4)", re.IGNORECASE | re.DOTALL), "Python 内联爬虫"),
    # Python 执行 data/ 目录下的脚本（LLM 习惯 data/fetch_xxx.py 这种模式）
    (re.compile(r'python[23]?\s+data[/\\][\w.]+\.py\b', re.IGNORECASE), "执行 data/ 目录脚本"),
    # PowerShell 爬虫
    (re.compile(r'powershell\b.*(?:Invoke-WebRequest|Invoke-RestMethod)\b.*https?://', re.IGNORECASE | re.DOTALL), "PowerShell 爬虫"),
    # Node.js 爬虫
    (re.compile(r'\bnode\b.*?(?:\bfetch\s*\(|axios|cheerio|puppeteer)', re.IGNORECASE | re.DOTALL), "Node.js 爬虫"),
]

_CRAWLER_FILE_PATTERNS: list[tuple[re.Pattern, str]] = [
    # Python 脚本中包含爬虫库
    (re.compile(r'(?:urllib\.request|requests\.get|scrapy|BeautifulSoup|bs4\.)', re.IGNORECASE), "写入 Python 爬虫脚本"),
    # PowerShell 脚本中包含爬虫
    (re.compile(r'Invoke-WebRequest.*https?://', re.IGNORECASE), "写入 PS 爬虫脚本"),
]


def _repair_json_args(raw: str):
    """★ 1.9.3 第 18 轮：尽力把「坏掉的工具参数 JSON」修好（修不好返回 None）。

    针对 deepseek-flash 的已知毛病（issue #1541、vLLM #47449 / #48343）：
      · 裸标识符（枚举没加引号）：{"action": status} → {"action": "status"}
      · 流式被截断：{"a": 1, "b": "x → 补右引号 / 右括号
      · 尾逗号：{"a": 1,} → 去掉
    """
    if not raw or not raw.strip():
        return None
    import re as _re
    t = raw.strip()
    try:
        return json.loads(t)
    except Exception:
        pass
    t = _re.sub(r",\s*([}\]])", r"\1", t)
    t = _re.sub(r'([:,\[]\s*)([A-Za-z_][A-Za-z0-9_]*)(\s*[,}\]])', r'\1"\2"\3', t)
    for _ in range(6):
        try:
            return json.loads(t)
        except Exception:
            if t.count('"') % 2 == 1:
                t += '"'
            else:
                t = t.rstrip()
                if not t.endswith(("}", "]")):
                    t += "}"
                else:
                    break
    try:
        return json.loads(t)
    except Exception:
        return None


# ══════════════════════════════════════════════════════════════
# ★★ 1.9.3 第 21 轮：DSML 工具调用的**解析与执行**
# ══════════════════════════════════════════════════════════════
# 真机证据（logs/prts_20261004_013741.log 第 11 轮）：
#   `content=155字符, thinking=720字符, tool_calls=0` —— 模型想把 PPT 发出去
#   （以及调 meme），却把工具调用**以 DSML 文本吐在正文里**（不是标准 tool_calls 字段）。
#   第 17 轮只做到"藏起来不显示" → **这次调用被整个丢掉**：
#   `file_ops send` 从未执行 → **文件根本没发出去** → 用户看到的就是"失败了"。
#   所以必须把它**解析回标准 tool_calls 并真正执行**（交接块里的 c 项）。
#
# 形状（竖线是**全角** U+FF5C，且跨 chunk 拼出来，只能用累积正文解析）：
#   开标记 = "<" + 全角竖线×2 + "DSML" + 全角竖线×2 + " "   （后跟 invoke/parameter）
#   闭标记 = "</" + 全角竖线×2 + "DSML" + 全角竖线×2 + " "
# 源码里不直接写那两个全角字符，统一由 _DSML_BAR 拼出来，免得日后被编辑器/正则弄坏。
_DSML_BAR = "\uff5c\uff5c"
_DSML_OPEN = "<" + _DSML_BAR + "DSML" + _DSML_BAR + " "
_DSML_CLOSE = "</" + _DSML_BAR + "DSML" + _DSML_BAR + " "


def looks_like_dsml(text: str) -> bool:
    """正文里是否出现了 DSML 工具调用标记（用来判定"这是调用，不是正文"）。"""
    return bool(text) and ("DSML" in text or "\uff5c\uff5c" in text)


def parse_dsml_tool_calls(text: str) -> list:
    """把 DSML 文本解析成标准 tool_calls；解析不到返回 []。

    先做**归一化**（把全角标记换成 ASCII 的 `<DSML ` / `</DSML `），
    再用普通正则解析 —— 比直接对着全角字符写正则可读得多，也不容易被转义坑到。
    """
    if not looks_like_dsml(text):
        return []
    t = text.replace(_DSML_OPEN, "<DSML ").replace(_DSML_CLOSE, "</DSML ")

    def _params(body: str) -> dict:
        import html as _html
        args = {}

        def _put(pname: str, attrs: str, raw: str):
            raw = _html.unescape(raw).strip()
            # `string="false"` 表示这个参数是 JSON（数字/布尔/对象），其余按字符串
            if 'string="false"' in attrs:
                try:
                    args[pname] = json.loads(raw)
                except Exception:
                    args[pname] = raw
            else:
                args[pname] = raw

        for pm in re.finditer(
                r'<DSML\s+parameter\s+name="([^"]+)"([^>]*)>(.*?)</DSML\s+parameter\s*>',
                body, re.S):
            _put(pm.group(1).strip(), pm.group(2), pm.group(3))
        # ★ 兜底：流被截断时参数的闭标记根本没到 → 取到"下一个开标记/结尾"为止。
        #   （真机第 11 轮那段就是断在半路的，不兜这一层就一个参数都拿不到。）
        for pm in re.finditer(
                r'<DSML\s+parameter\s+name="([^"]+)"([^>]*)>(.*?)(?=<DSML\s|\Z)', body, re.S):
            pname = pm.group(1).strip()
            if pname not in args:
                _put(pname, pm.group(2), pm.group(3))
        return args

    out = []
    for m in re.finditer(r'<DSML\s+invoke\s+name="([^"]+)"\s*>(.*?)</DSML\s+invoke\s*>',
                         t, re.S):
        name = m.group(1).strip()
        if name:
            out.append({"id": f"dsml_{len(out)}",
                        "function": {"name": name,
                                     "arguments": json.dumps(_params(m.group(2)),
                                                             ensure_ascii=False)}})
    if out:
        return out

    # 兜底：开标记在、闭标记被截断（流式被切断）时，仍尽力把名字与已到的参数捞出来
    for m in re.finditer(r'<DSML\s+invoke\s+name="([^"]+)"\s*>(.*)$', t, re.S):
        name = m.group(1).strip()
        if name:
            out.append({"id": f"dsml_{len(out)}",
                        "function": {"name": name,
                                     "arguments": json.dumps(_params(m.group(2)),
                                                             ensure_ascii=False)}})
    return out


def recover_leaked_tool_calls(text: str) -> list:
    """★ 1.9.3 第 21 轮：把**漏在正文里的工具调用**救回来（DSML 优先，其次 JSON 形状）。

    返回标准 tool_calls 列表；救不回来返回 []（调用方按普通正文处理）。
    """
    if not text or not text.strip():
        return []
    # ① DSML 形状（deepseek-flash 在长提示词下常这样）
    got = parse_dsml_tool_calls(text)
    if got:
        _log.info(f"从正文里恢复 DSML 工具调用: {[g['function']['name'] for g in got]}")
        return got
    # ② JSON 形状：{"name": ..., "arguments": ...}
    try:
        s = text.strip()
        if '{' in s and '}' in s:
            data = json.loads(s[s.index('{'):s.rindex('}') + 1])
            if isinstance(data, dict) and "name" in data and "arguments" in data:
                fa = data["arguments"]
                if isinstance(fa, str):
                    fa = json.loads(fa) if fa.strip() else {}
                _log.info(f"从正文里恢复 JSON 工具调用: {data['name']}")
                return [{"id": "leak_001",
                         "function": {"name": data["name"],
                                      "arguments": json.dumps(fa, ensure_ascii=False)}}]
    except Exception:
        _log.debug("JSON 泄漏恢复失败，按普通文本处理")
    return []


def _is_tool_failure(result: str) -> bool:
    """★ 1.4.0: 检测工具返回是否表示失败"""
    if not result:
        return True
    result_lower = result.lower()

    # ★ shell_exec 特殊处理: 用退出码判断，避免命令文本中的关键词（如 echo "FAILED"）误判
    if "退出码:" in result_lower:
        import re
        m = re.search(r'退出码:\s*(-?\d+)', result_lower)
        if m:
            exit_code = int(m.group(1))
            return exit_code != 0  # 只有非零退出码才是失败

    # ★ web_search 特殊处理: 以 "搜索'" 开头 = 成功返回了搜索结果
    if result_lower.startswith("搜索'") or result_lower.startswith("模拟搜索"):
        return False

    # ★ file_ops 特殊处理: 以 ✅ 开头 = 成功
    if result.startswith("✅"):
        return False

    # ★ 1.4.0+: office 工具 — 结果内含 "success": true 则不算失败
    #   (OfficeCLI 有时 exit code ≠ 0 但 JSON 明确 success:true)
    if '"success": true' in result_lower or '"success":true' in result_lower:
        return False

    failure_keywords = [
        "失败", "错误", "error", "fail", "不存在", "not found",
        "未找到", "超时", "timeout", "未安装", "不可用", "unavailable",
        "请先", "拒绝", "denied", "permission", "找不到",
    ]
    return any(kw in result_lower for kw in failure_keywords)


def _repair_json(raw: str) -> str:
    """★ 1.4.0+: 修复 LLM 输出的常见 JSON 格式问题"""
    s = raw.strip()
    # 1. 去除尾部多余逗号: ,]  ,}
    s = re.sub(r',\s*([}\]])', r'\1', s)
    # 2. 去除尾部连续逗号
    s = re.sub(r',\s*,', ',', s)
    # 3. 如果以 { 开头但不以 } 结尾，补全
    if s.startswith('{') and not s.rstrip().endswith('}'):
        # 找到最后一个未闭合的字符串/对象层级，补全 }
        depth = 0
        in_str = False
        escape = False
        last_good = len(s)
        for i, ch in enumerate(s[1:], 1):
            if escape:
                escape = False
                continue
            if ch == '\\':
                escape = True
                continue
            if ch == '"':
                in_str = not in_str
                continue
            if in_str:
                continue
            if ch == '{' or ch == '[':
                depth += 1
            elif ch == '}' or ch == ']':
                depth -= 1
                if depth == 0:
                    last_good = i + 1
        if last_good < len(s):
            s = s[:last_good]
        # 补全未闭合的括号
        depth = 0
        in_str = False
        escape = False
        for ch in s:
            if escape:
                escape = False
                continue
            if ch == '\\':
                escape = True
                continue
            if ch == '"':
                in_str = not in_str
                continue
            if in_str:
                continue
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
        s = s + '}' * depth
    return s



class ChatSession:
    """聊天会话 - 多轮循环 + 话题管理"""

    # ★ 1.9.3 第 25 轮：20 → 30 轮。
    #   真机证据（logs/prts_20261004_025634.log）：用户拍板"PPT 以自己写脚本为主"之后，
    #   那条路线要花更多轮 —— 那次前 13 轮全在做素材搜集，第 18~20 轮才开始写生成脚本，
    #   结果**在第 20 轮撞墙**，脚本只写了两段、PPT 根本没做出来。
    #   （75% 处仍会提醒收尾；撞墙时也仍会汇报"刚在做什么"并问要不要继续。）
    MAX_ROUNDS = 30
    MAX_MESSAGES = 80  # ★ 1.4.0: 提升消息上限（多轮工具调用会产生更多消息）
    DEFAULT_MAX_TOKENS = 168000  # DeepSeek 上下文窗口

    def __init__(self):
        from src.core.llm_client import ChatClient
        from src.core.tool_manager import get_tool_manager
        from src.core.database import get_db

        self.client = ChatClient()
        self.tool_manager = get_tool_manager()
        self.db = get_db()
        self.messages: list[dict] = []
        self._topic_id: Optional[int] = None  # ★ 当前话题 ID
        self._character: str = ""             # ★ 1.6.0: 当前角色（一个角色一个连续窗口）
        self._first_user_message: Optional[str] = None  # 用于自动生成标题
        self._lock = threading.Lock()
        self._streaming = False
        self._abort_chat = False
        self._abort_tool = False
        self._input_blocked = False  # ★ Token 超限时阻止输入
        self._crawler_attempts = 0   # ★ 1.4.0+: 爬虫拦截计数
        self._consecutive_searches = 0  # ★ 1.4.0+: 连续搜索计数
        self._summarizing = False    # ★ 1.6.0: 避免总结重入
        self._pending_user_text = ""  # ★ 1.6.0: 总结期间暂存的用户消息
        self._pending_images: list[str] = []  # ★ 1.6.0: 待随下一条用户消息直传的多模态图片
        self._multimodal = False      # ★ 1.6.0: 是否启用图片直传（image_url）

        # 回调
        self.on_text: Optional[Callable[[str], None]] = None
        self.on_thinking: Optional[Callable[[str], None]] = None       # ★ 1.5.0: 思考流增量（reasoning_content）
        self.on_thinking_done: Optional[Callable[[], None]] = None    # ★ 1.5.0: 思考结束（形象线程立即切换 GIF）
        self.on_tool_call: Optional[Callable[[str, dict], None]] = None
        self.on_tool_result: Optional[Callable[[str, str], None]] = None
        self.on_system_msg: Optional[Callable[[str], None]] = None
        self.on_done: Optional[Callable[[], None]] = None
        self.on_abort: Optional[Callable[[], None]] = None
        self.on_topic_changed: Optional[Callable[[int], None]] = None  # ★ 话题切换回调
        self.on_input_blocked: Optional[Callable[[bool], None]] = None  # ★ 输入阻止回调
        self.on_character_changed: Optional[Callable[[str], None]] = None  # ★ 1.6.0: 角色窗口切换
        self.on_summary_done: Optional[Callable[[], None]] = None  # ★ 1.6.0: 话题总结完成（刷新侧栏书签）
        # ★ 1.9.0: LLM 把动作/表情包协议 JSON（{"gif":…,"meme":…}）吐在**正文**里时，
        #   由 reply_filter 在流里挡掉、在这里把解析结果交给形象线程。
        self.on_protocol: Optional[Callable[[dict], None]] = None
        # ★ 1.9.1: 工具返回的 "gif" 钩子删了（你："决定返回之前的方式"）——
        #   形象切换只剩"待机计时器"和"思考 JSON 拦截器"两个来源。

        _log.info("会话初始化完成")

    # ══════════════════════════════════════════════════
    # ★ 1.6.0: 角色窗口（一个角色一个窗口，所有话题连续）
    # ══════════════════════════════════════════════════

    @property
    def character(self) -> str:
        return self._character

    def _config_character(self) -> str:
        try:
            from src.utils.config import get_config
            return get_config().pet.character or ""
        except Exception:
            return ""

    def set_character(self, name: str, load: bool = True):
        """切换到某个形象（★ 1.9.3: 不再切换上下文窗口，只换 persona 与"谁在回复"）

        所有形象共用**同一个**共享话题，所以这里只更新 `self._character`；
        上下文/聊天窗口保持不变（原话："切换形象时聊天窗口显示全部历史"）。
        """
        name = (name or "").strip()
        if not name:
            return
        if name == self._character:
            if load:
                self.load_character_window(name)
            return
        # ★ 若正在流式输出，先中止，避免回复写进新形象的身份
        if self._streaming:
            _log.info("切换角色前中止进行中的对话")
            self._abort_chat = True
            for _ in range(20):
                if not self._streaming:
                    break
                time.sleep(0.1)
        self._character = name
        _log.info(f"切换角色: {name}（共享工作台，不换话题）")
        if load:
            self.load_character_window(name)
        if self.on_character_changed:
            try:
                self.on_character_changed(name)
            except Exception:
                pass

    def load_character_window(self, name: str = ""):
        """载入（或首次创建）**共享工作台**话题及其消息。

        ★ 1.9.3（用户定稿）：所有形象共用同一个话题 ——
        永远用 `SHARED_CHARACTER` 去取/建活跃话题，不再按形象名建话题。
        `name` 只决定"谁在回复/用哪套 persona"。
        如果话题**已经是当前这个**，就直接返回（不重载），
        免得把正在进行的对话从内存里冲掉。
        """
        name = (name or self._character or self._config_character()).strip()
        if name:
            self._character = name
        topic = self.db.get_active_topic(SHARED_CHARACTER)
        if topic is None:
            # 首次进入共享工作台：若无任何历史则直接建新话题；
            # 若有历史（旧版本遗留的已归档话题）则继承最近话题的总结作为上下文续接
            self._topic_id = self.db.create_topic("", SHARED_CHARACTER)
            self._first_user_message = None
            summary = self.db.get_last_archived_summary(SHARED_CHARACTER)
            with self._lock:
                self.messages.clear()
                if summary:
                    self.messages.append({
                        "role": "system",
                        "content": f"[上一段对话的总结，用于延续上下文]\n{summary}"
                    })
            _log.info(f"为共享工作台创建新话题 #{self._topic_id}（当前形象「{name}」）")
        elif self._topic_id == topic["id"]:
            # ★ 已经是同一个共享话题 → 不清空、不重载
            _log.debug(f"共享话题 #{self._topic_id} 已在内存中，跳过重载"
                       f"（当前形象「{name}」）")
        else:
            self._topic_id = topic["id"]
            self._first_user_message = None
            self._load_topic_messages(self._topic_id)
            _log.info(f"恢复共享话题 #{self._topic_id} "
                      f"({len(self.messages)} 条，当前形象「{name}」)")
        self._input_blocked = False
        if self.on_topic_changed and self._topic_id:
            try:
                self.on_topic_changed(self._topic_id)
            except Exception:
                pass
        if self.on_input_blocked:
            try:
                self.on_input_blocked(False)
            except Exception:
                pass
        self._check_token_limit()

    def summarize_current_topic(self, then_new_topic: bool = True, reason: str = "manual"):
        """总结当前话题（后台线程），并把总结归入新话题的上下文。

        日志#443: 开启新话题 / 总结旧话题 / 上下文满自动总结 三者共用此入口。
        ★ 1.9.3: 共享工作台 —— 归档的是**共享话题**，换出来的新话题仍是共享话题。
        """
        topic_id = self._topic_id
        snapshot = []
        with self._lock:
            snapshot = [dict(m) for m in self.messages]
        text = self._messages_to_transcript(snapshot)
        if not text.strip():
            if self.on_system_msg:
                self.on_system_msg("[当前对话内容太少，无需总结]")
            return
        if self._summarizing:
            _log.info("总结进行中，跳过重复请求")
            return
        self._summarizing = True
        if self.on_system_msg:
            self.on_system_msg(f"[正在总结{reason}对话，稍后开启新话题...]")

        def _work():
            summary = ""
            try:
                from src.core.llm_client import get_summarizer_client
                client = get_summarizer_client()
                summary = client.summarize_conversation(text)
            except Exception as e:
                _log.warning(f"话题总结失败: {e}")
            try:
                if topic_id:
                    self.db.archive_topic(topic_id, summary)
            except Exception as e:
                _log.warning(f"写入话题总结失败: {e}")
            self._summarizing = False
            # 回到主流程：创建新的**共享**话题并把总结作为起始上下文
            self._start_new_topic_internal(summary, reason)

        threading.Thread(target=_work, daemon=True, name="topic-summarize").start()

    @staticmethod
    def _messages_to_transcript(messages: list[dict], limit_chars: int = 20000) -> str:
        """把消息列表压成可总结的文本（丢弃 tool 细节，避免超长）"""
        lines = []
        for m in messages:
            role = m.get("role")
            content = m.get("content")
            if not isinstance(content, str) or not content.strip():
                continue
            if role == "user":
                lines.append(f"用户: {content.strip()}")
            elif role == "assistant":
                lines.append(f"桌宠: {content.strip()}")
            elif role == "system":
                c = content.strip()
                if c.startswith("[系统上下文]") or c.startswith("[上一段对话的总结"):
                    lines.append(f"（背景）: {c}")
        text = "\n".join(lines)
        if len(text) > limit_chars:
            text = text[-limit_chars:]
        return text

    def _start_new_topic_internal(self, summary: str, reason: str):
        """内部：归档后创建新的**共享**话题，把总结写入新话题上下文。

        ★ 1.9.3: 话题是共享的，所以不再有"总结期间角色切了就丢弃注入"那一步 ——
        总结属于共享上下文，换形象也照样续接。
        """
        topic_id = self.db.create_topic("", SHARED_CHARACTER)
        self._topic_id = topic_id
        self._first_user_message = None
        self._input_blocked = False
        with self._lock:
            self.messages.clear()
            if summary:
                self.messages.append({
                    "role": "system",
                    "content": f"[上一段对话的总结，用于延续上下文]\n{summary}"
                })
        _log.info(f"新共享话题 #{topic_id} 已创建（当前形象={self._character}, 原因={reason}）")
        # ★ 用总结内容作为新话题标题，侧栏一眼可见上下文来源
        if summary:
            try:
                title = summary.strip().replace("\n", " ")[:30]
                self.db.set_topic_title(topic_id, title)
            except Exception:
                pass
        if self.on_system_msg:
            if summary:
                self.on_system_msg("[新话题已开启，已继承上一段对话的总结]")
            else:
                self.on_system_msg("[新话题已开启（总结为空，已按原对话标题归档为书签）]")
        if self.on_input_blocked:
            try:
                self.on_input_blocked(False)
            except Exception:
                pass
        if self.on_topic_changed:
            try:
                self.on_topic_changed(topic_id)
            except Exception:
                pass
        if self.on_summary_done:
            try:
                self.on_summary_done()
            except Exception:
                pass
        # ★ 1.6.0: 总结期间被暂存的消息，现在自动发出
        pending = self._pending_user_text
        self._pending_user_text = ""
        if pending:
            _log.info("发送总结期间暂存的消息")
            try:
                self.send_message(pending)
            except Exception as e:
                _log.warning(f"补发暂存消息失败: {e}")

    def start_new_topic(self, summarize: bool = False):
        """用户点击「开启新话题 / 总结旧话题」入口"""
        if summarize:
            self.summarize_current_topic(then_new_topic=True, reason="旧")
        else:
            topic_id = self.db.create_topic("", SHARED_CHARACTER)
            self._topic_id = topic_id
            self._first_user_message = None
            self._input_blocked = False
            with self._lock:
                self.messages.clear()
            _log.info(f"用户开启新共享话题 #{topic_id}（当前形象={self._character}, 不总结）")
            if self.on_system_msg:
                self.on_system_msg("[已开启新话题]")
            if self.on_input_blocked:
                try:
                    self.on_input_blocked(False)
                except Exception:
                    pass
            if self.on_topic_changed:
                try:
                    self.on_topic_changed(topic_id)
                except Exception:
                    pass

    # ══════════════════════════════════════════════════
    # 话题管理
    # ══════════════════════════════════════════════════

    @property
    def topic_id(self) -> Optional[int]:
        return self._topic_id

    def new_topic(self) -> int:
        """创建新的**共享**话题，返回 topic_id（★ 1.9.3: 所有形象共用）"""
        self._save_current_state()
        topic_id = self.db.create_topic("", SHARED_CHARACTER)
        self._topic_id = topic_id
        self._first_user_message = None
        self._input_blocked = False
        with self._lock:
            self.messages.clear()
        _log.info(f"新共享话题创建: id={topic_id} (当前形象={self._character})")
        if self.on_topic_changed:
            self.on_topic_changed(topic_id)
        if self.on_input_blocked:
            self.on_input_blocked(False)
        return topic_id

    def get_current_topic_info(self) -> dict:
        """获取当前角色窗口的信息（★ 1.6.0: 供侧栏状态显示）"""
        if self._topic_id is None:
            return {"topic_id": None, "title": "", "message_count": 0, "token_count": 0,
                    "character": self._character}
        title = self.db.get_topic_title(self._topic_id)
        token_count = self.db.get_topic_token_count(self._topic_id)
        with self._lock:
            msg_count = len(self.messages)
        return {
            "topic_id": self._topic_id,
            "title": title or "NEW",
            "message_count": msg_count,
            "token_count": token_count,
            "character": self._character,
        }

    def _save_current_state(self):
        """保存当前状态到数据库（话题切换/退出时调用）"""
        # messages 已在每次追加时实时写库，这里只需确保一致性
        pass

    def _load_topic_messages(self, topic_id: int):
        """从数据库加载话题消息到内存（★ 1.9.3: 带上 speaker，供拼上下文时标注身份）

        ★ 1.9.3 第 60 轮：`ctx_exclude` 也一起带进内存 ——
          子代理的**中间发言**落库了（重启后窗口里还能看到），
          但它们在内存里也带着这个标记，`build_shared_context` 会把它们跳过，
          **不会**被灌进主 LLM 上下文。窗口渲染不看这个标记，照旧显示。
        """
        try:
            db_messages = self.db.get_topic_messages(topic_id)
            with self._lock:
                self.messages.clear()
                for m in db_messages:
                    role = m["role"]
                    # 跳过 tool 消息（缺少 assistant tool_calls 会导致 API 400）
                    if role == "tool":
                        continue
                    msg = {"role": role, "content": m["content"]}
                    if m.get("tool_name"):
                        msg["tool_name"] = m["tool_name"]
                    if m.get("speaker"):
                        msg["speaker"] = m["speaker"]
                    if m.get("ctx_exclude"):
                        msg["ctx_exclude"] = 1
                    self.messages.append(msg)
            _log.debug(f"加载话题 #{topic_id} 消息: {len(self.messages)} 条")
        except Exception as e:
            _log.warning(f"加载话题消息失败: {e}")

    def _build_llm_messages(self, snapshot: list = None) -> list[dict]:
        """★ 1.9.3: 装配喂给 LLM 的**共享工作台上下文**。

        self.messages 保留的是"聊天窗口要显示的完整历史"；
        真正发给模型的是这里的结果：最近 RECENT_CONTEXT_LIMIT 条原文
        + 更早那段折成的一条 system 摘要（规则见 build_shared_context）。

        ★★ 1.9.3 第 51 轮 —— **修一个真机死锁**：
          本方法原来无条件 `with self._lock:`，而它的两个调用点
          （`_run_loop` 里的 1324 / 1809 行）**已经持有同一把锁**，
          而 `self._lock` 是**不可重入**的 `threading.Lock`（不是 RLock）
          → 自己等自己 → **永久卡死**。
          真机表现：发一条消息后窗口变"(未响应)"，35 秒后被用户强杀
          （`logs/prts_20261004_155636.log` 里"最差 35250ms"的帧间隔就是这段冻结）。
          回归当时**测不到** —— 没有任何套件真的走完
          "发消息 → _run_loop → 组上下文"这条路径。
          → 改法：**已经持锁的调用方把快照传进来**（`snapshot=`），
            没传才自己加锁。顺带把 DB 读摘要挪到锁外 —— 锁里做 I/O 本来就不该。
        """
        if snapshot is None:
            with self._lock:
                snapshot = [dict(m) for m in self.messages]
        summary = ""
        if self._topic_id is not None:
            try:
                summary = self.db.get_topic_summary(self._topic_id) or ""
                if not summary:
                    summary = self.db.get_last_archived_summary(
                        SHARED_CHARACTER, self._topic_id) or ""
            except Exception as e:
                _log.debug(f"取共享话题摘要失败（按无摘要处理）: {e}")
        return build_shared_context(snapshot, self._character, summary)

    def _check_token_limit(self):
        """★ 1.6.0: 上下文接近上限时自动总结旧话题并开启新话题（不再阻断输入）"""
        if self._topic_id is None:
            return
        try:
            is_full = self.db.is_topic_full(self._topic_id)
        except Exception:
            return
        if is_full and not self._summarizing and not self._streaming:
            _log.warning(f"话题 #{self._topic_id} 上下文已满 → 自动总结并开启新话题")
            if self.on_system_msg:
                self.on_system_msg("[上下文已满，正在自动总结旧话题并延续...]")
            self.summarize_current_topic(then_new_topic=True, reason="满上下文")

    def _auto_generate_title(self, user_text: str):
        """根据第一轮用户消息自动生成标题"""
        if self._topic_id is None:
            return
        if self._first_user_message is not None:
            return  # 标题已生成或已记录第一条消息
        self._first_user_message = user_text

        # 检查标题是否已设置
        existing_title = self.db.get_topic_title(self._topic_id)
        if existing_title:
            return

        # 简单规则：截取前 30 个字符作为标题
        title = user_text.strip()[:30]
        if len(user_text.strip()) > 30:
            title += "..."

        if title:
            try:
                self.db.set_topic_title(self._topic_id, title)
                _log.debug(f"话题 #{self._topic_id} 标题自动生成: {title}")
                if self.on_topic_changed:
                    self.on_topic_changed(self._topic_id)
            except Exception as e:
                _log.warning(f"自动标题生成失败: {e}")

    # ══════════════════════════════════════════════════
    # 消息裁剪
    # ══════════════════════════════════════════════════

    def _trim_messages(self):
        """裁剪消息历史：保留系统消息 + 最近 MAX_MESSAGES 条"""
        with self._lock:
            if len(self.messages) <= self.MAX_MESSAGES:
                return
            system_msgs = [m for m in self.messages if m.get("role") == "system"]
            non_system = [m for m in self.messages if m.get("role") != "system"]
            keep_count = max(self.MAX_MESSAGES - len(system_msgs), 20)
            if len(non_system) > keep_count:
                trimmed = len(non_system) - keep_count
                non_system = non_system[-keep_count:]
                self.messages = system_msgs + non_system
                _log.info(f"消息裁剪: 移除了 {trimmed} 条旧消息, 当前 {len(self.messages)} 条")

    # ══════════════════════════════════════════════════
    # 历史与上下文
    # ══════════════════════════════════════════════════

    def load_history(self):
        """★ 1.9.3: 载入**共享工作台**的对话窗口（所有形象共用一段连续上下文）"""
        try:
            # ★ 1.9.3: 共享工作台 —— 不再按形象名找回自己的窗口；
            #   配置里读不到形象名也能照常载入共享话题（load_character_window 允许空名）。
            self.load_character_window(self._config_character() or self._character)
        except Exception as e:
            _log.warning(f"加载历史失败: {e}")

    def add_context(self, content: str):
        with self._lock:
            self.messages.append({"role": "system", "content": f"[系统上下文] {content}"})

    # ══════════════════════════════════════════════════
    # ★ 1.6.0: 多模态图片直传
    # ══════════════════════════════════════════════════

    def set_multimodal(self, enabled: bool):
        """启用/关闭「图片作为 image_url 直接传给对话模型」"""
        self._multimodal = bool(enabled)
        _log.info(f"多模态直传: {'开启' if self._multimodal else '关闭'}")

    def attach_image(self, path: str):
        """登记一张待随下一条用户消息一起直传的图片（本地路径）"""
        if not path:
            return
        try:
            p = Path(path)
            if not p.is_absolute():
                p = (_BASE_DIR / p).resolve()
        except Exception:
            p = Path(path)
        if not p.exists():
            _log.debug(f"待传图片不存在，忽略: {path}")
            return
        if str(p) not in self._pending_images:
            self._pending_images.append(str(p))
        _log.info(f"待直传图片已登记: {p.name} (共 {len(self._pending_images)} 张)")

    @staticmethod
    def _image_to_data_uri(path: str, max_side: int = 1024, quality: int = 85) -> str:
        """把本地图片压成 JPEG 并转成 data:image/jpeg;base64,...（控制请求体积）"""
        import base64
        import io
        from PIL import Image
        with Image.open(path) as im:
            # ★★ 1.9.3 第 22 轮：**透明图先压白底再转 RGB**。
            #   直接 convert("RGB") 会把透明区（其 RGB 分量是垃圾值）压成**黑色**，
            #   于是用户发来的透明 PNG（立绘/贴纸/logo）在模型眼里是"黑底图"，
            #   描述和判断都会跟着错。JPEG 没有 alpha，所以必须在这里拍平。
            if im.mode in ("RGBA", "LA", "PA") or (im.mode == "P" and "transparency" in im.info):
                _rgba = im.convert("RGBA")
                _base = Image.new("RGB", _rgba.size, (255, 255, 255))
                _base.paste(_rgba.convert("RGB"), (0, 0), _rgba.split()[3])
                im = _base
            else:
                im = im.convert("RGB")
            if max(im.size) > max_side:
                ratio = max_side / max(im.size)
                im = im.resize((max(1, int(im.width * ratio)),
                                max(1, int(im.height * ratio))), Image.LANCZOS)
            buf = io.BytesIO()
            im.save(buf, format="JPEG", quality=quality, optimize=True)
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        if len(b64) > 6_000_000:      # 约 4.5MB 原图，避免请求体过大
            raise ValueError("图片过大，无法直传")
        return f"data:image/jpeg;base64,{b64}"

    def _strip_images_from_messages(self) -> bool:
        """★ 1.6.0: 把消息里的 image_url 块降级为纯文字（模型不支持多模态时的兜底）。

        返回 True 表示确实剥掉了图片（调用方可据此重试一次）。
        """
        stripped = False
        with self._lock:
            for m in self.messages:
                content = m.get("content")
                if isinstance(content, list):
                    texts = [b.get("text", "") for b in content
                             if isinstance(b, dict) and b.get("type") == "text"]
                    note = "\n[图片未能直传给模型，请以文字描述为准]"
                    m["content"] = ("\n".join(t for t in texts if t) + note).strip()
                    stripped = True
        if stripped:
            _log.warning("已将历史消息中的图片块降级为纯文字")
        return stripped

    def _build_user_content(self, user_text: str):
        """★ 1.6.0: 有待传图片且已开启多模态 → 组装 content 数组，否则返回纯文本"""
        if not self._multimodal or not self._pending_images:
            return user_text
        blocks = [{"type": "text", "text": user_text}]
        sent = 0
        for p in self._pending_images:
            try:
                blocks.append({"type": "image_url",
                               "image_url": {"url": self._image_to_data_uri(p)}})
                sent += 1
            except Exception as e:
                _log.warning(f"图片直传失败({Path(p).name}): {e}")
                blocks[0]["text"] += f"\n[图片 {Path(p).name} 直传失败，已忽略]"
        self._pending_images.clear()
        if not sent:
            return user_text
        _log.info(f"本轮随消息直传 {sent} 张图片（多模态）")
        return blocks

    # ══════════════════════════════════════════════════
    # 发送与循环
    # ══════════════════════════════════════════════════

    def send_message(self, user_text: str):
        """用户发送消息"""
        # 确保有话题（★ 1.6.0: 归属当前角色）
        if self._topic_id is None:
            if not self._character:
                self._character = self._config_character()
            self.new_topic()

        # ★ 1.6.0: 上下文满 → 自动总结旧话题并延续（不再阻断用户输入）
        if self._input_blocked:
            self._input_blocked = False
            if self.on_input_blocked:
                try:
                    self.on_input_blocked(False)
                except Exception:
                    pass
        # ★ 1.6.0: 总结进行中 → 暂存这条消息，总结完成后自动发出（避免丢消息）
        if self._summarizing:
            self._pending_user_text = user_text
            _log.info("总结进行中，本条消息暂存，待新话题开启后自动发送")
            if self.on_system_msg:
                self.on_system_msg("[正在总结旧话题，稍后自动发送这条消息...]")
            return
        try:
            if self._topic_id and self.db.is_topic_full(self._topic_id):
                _log.warning("发送前检测到上下文已满 → 自动总结旧话题")
                self._pending_user_text = user_text
                self.summarize_current_topic(then_new_topic=True, reason="满上下文")
                return
        except Exception:
            pass

        # 防止重入
        if self._streaming:
            _log.warning("已有进行中的对话，中止后重新发送")
            self._abort_chat = True
            for _ in range(20):
                if not self._streaming:
                    break
                time.sleep(0.1)

        # 自动生成标题
        self._auto_generate_title(user_text)

        self._abort_chat = False
        self._abort_tool = False
        self._crawler_attempts = 0  # ★ 每次新对话重置爬虫计数
        self._consecutive_searches = 0  # ★ 每次新对话重置搜索计数
        self._streaming = True

        with self._lock:
            # ★ 1.6.0: 开启多模态时，把待传图片作为 image_url 一起发给对话模型
            content = self._build_user_content(user_text)
            self.messages.append({"role": "user", "content": content})
        try:
            # 历史库里仍存纯文本（图片不进 db，避免库膨胀；下次加载时不会重复发图）
            self.db.add_chat_history("user", user_text, topic_id=self._topic_id)
        except Exception:
            pass

        # ★ 1.9.1: 不再截断到 80 字（你要求尽可能记录所有数据，全文更好调试）
        _log.info(f"用户消息 (topic={self._topic_id}, {len(user_text)} 字): {user_text}")
        thread = threading.Thread(target=self._run_loop, daemon=True)
        thread.start()

    def abort_chat(self):
        _log.info("中止对话")
        self._abort_chat = True
        self._streaming = False
        if self.on_abort:
            self.on_abort()

    def abort_tool(self):
        _log.info("中止工具调用")
        self._abort_tool = True
        if self.on_system_msg:
            self.on_system_msg("[已中止工具调用]")

    def send_auto_tool_trigger(self, prompt: str, tool_name: str = "idle_talk") -> bool:
        """★ 1.9.0（你拍板"做"）：主动说话 = **真正的工具调用条目**。

        你原话："作为自动工具，在上下文中的角色和其他工具一样，
        「自动工具触发」返回'……'。"

        做法：往会话里插一对
            assistant(tool_calls=[idle_talk]) + tool(结果=[自动工具触发] …)
        然后照常启动对话循环 —— 对 LLM 来说它就是一次普通的工具调用结果，
        不再是"用户发了一大段"；聊天里也按工具条目的样式显示。
        idle_talk **不进** LLM 的工具列表（它是自动工具，LLM 不会主动调它）。
        """
        if self._streaming:
            _log.warning("主动说话：已有进行中的对话，本次跳过")
            return False
        if self._summarizing:
            _log.warning("主动说话：正在总结话题，本次跳过")
            return False
        try:
            if self._topic_id and self.db.is_topic_full(self._topic_id):
                _log.warning("主动说话：上下文已满，跳过（等用户下次发消息时自动总结）")
                return False
        except Exception:
            pass

        self._abort_chat = False
        self._abort_tool = False
        self._streaming = True
        _tc_id = f"auto_{tool_name}_{int(time.time())}"
        with self._lock:
            self.messages.append({
                "role": "assistant", "content": None,
                "tool_calls": [{
                    "id": _tc_id, "type": "function",
                    "function": {
                        "name": tool_name,
                        "arguments": json.dumps({"trigger": "auto"},
                                                ensure_ascii=False),
                    },
                }],
            })
            self.messages.append({
                "role": "tool", "tool_call_id": _tc_id,
                "content": prompt,
            })
        try:
            self.db.add_chat_history("tool", prompt[:500], tool_name,
                                     tool_call_id=_tc_id, topic_id=self._topic_id)
        except Exception:
            pass
        # 界面上按工具条目样式显示（和其他工具一致）
        if self.on_tool_call:
            try:
                self.on_tool_call(tool_name, {"trigger": "auto"})
            except Exception:
                pass
        if self.on_tool_result:
            try:
                self.on_tool_result(tool_name, prompt)
            except Exception:
                pass
        _log.info(f"[自动工具] {tool_name} 触发（工具条目样式注入会话）")
        threading.Thread(target=self._run_loop, daemon=True).start()
        return True

    def resume_after_subagent(self, job_label: str = "") -> bool:
        """★ 1.9.3 第 60 轮：子代理交付了 → **主 LLM 立刻续一轮**（不许"还在等"）。

        用户原话："主LLM能够感知到子代理的行为就行。不要出现子代理已经交付文件了
        主LLM还说再等。"

        做法（选的是 b：**把结果当工具结果的返回，让工具调用循环自然延续**）：
          交付物早由 `main_window._flush_pending_subagent_results()` 以
          `assistant(tool_calls=[subagent]) + tool(交付物)` 一对补进 `self.messages`，
          这里**只负责把这一轮跑起来** —— 对模型来说，它就是"我刚调的那个工具返回了"，
          于是它会直接据此汇报，而不是继续等一个"还没回来"的子代理。

        ★ 为什么不再自己注入一条 system 提示（不选 c）：
          那一对 tool 结果本身就是模型必须回应的东西，再塞一条系统提示纯属重复烧
          token；而且 `_prepare_messages` 已保证这对消息合法（补 reasoning_content、
          按块配对），不需要额外兜底。
        ★ 为什么**只在这个时机**被调用：调用方（主窗口）已经确认"子代理完成了、
          而且当前没有别的一轮在跑"。主 LLM 正忙时**不许**走这里 ——
          那种情况由主窗口把交付物排队，等本轮结束统一补进上下文再触发一次
          （见 `_on_session_done_safe`），避免把正在跑的轮次打断/叠轮。
        """
        if self._streaming:
            _log.warning("子代理交付续轮：已有进行中的对话，本次跳过（交付物已在上下文里）")
            return False
        if self._summarizing:
            _log.warning("子代理交付续轮：正在总结话题，本次跳过")
            return False
        try:
            if self._topic_id and self.db.is_topic_full(self._topic_id):
                _log.warning("子代理交付续轮：上下文已满，跳过（等下次发言时自动总结）")
                return False
        except Exception:
            pass

        self._abort_chat = False
        self._abort_tool = False
        self._streaming = True
        _log.info(f"[子代理] 交付物已在上下文里 → 主动续一轮让主 LLM 汇报"
                  f"{('（' + job_label + '）') if job_label else ''}")
        threading.Thread(target=self._run_loop, daemon=True,
                         name="subagent-followup").start()
        return True

    # ★ 1.4.0+: 爬虫拦截守卫
    def _guard_tool_call(self, tool_name: str, tool_args: dict) -> Optional[str]:
        """检查工具调用是否为爬虫行为。返回 None=放行, 返回 str=拦截消息"""
        if tool_name == "shell_exec":
            command = tool_args.get("command", "")
            if not command:
                return None
            for pattern, label in _CRAWLER_SHELL_PATTERNS:
                if pattern.search(command):
                    self._crawler_attempts += 1
                    _log.warning(f"🛡 拦截爬虫命令({label}): {command[:100]}")
                    msg = (
                        f"[🛡 系统拦截] 检测到爬虫行为({label})。请改用 web_search 获取网页内容，"
                        f"用 office 工具创建文档表格。不要写脚本爬数据，web_search 已经能获取你需要的信息。"
                    )
                    # 连续2次拦截 → 注入更强引导
                    if self._crawler_attempts >= 2:
                        msg += (
                            f"\n\n⚠️ 这已经是第{self._crawler_attempts}次被拦截了！"
                            f"你必须 100% 放弃爬虫方式，直接调用 web_search 搜索数据，"
                            f"然后用 office 工具的 create→add→set 三步创建文档。这是唯一正确的路径。"
                        )
                    return msg
            return None

        if tool_name == "file_ops":
            action = tool_args.get("action", "")
            path = tool_args.get("path", "")
            content = tool_args.get("content", "")
            # 只拦截写入 .py/.ps1/.js 文件且内容含爬虫库
            if action == "write" and path and content:
                ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
                if ext in ("py", "ps1", "js"):
                    for pattern, label in _CRAWLER_FILE_PATTERNS:
                        if pattern.search(content):
                            self._crawler_attempts += 1
                            _log.warning(f"🛡 拦截爬虫脚本({label}): {path} ({len(content)}字节)")
                            msg = (
                                f"[🛡 系统拦截] 禁止写入爬虫脚本({label})。"
                                f"请直接用 web_search 搜索数据，不要写脚本爬网页。"
                            )
                            if self._crawler_attempts >= 2:
                                msg += (
                                    f"\n\n⚠️ 第{self._crawler_attempts}次拦截！放弃爬虫，"
                                    f"用 web_search + office 是最快完成任务的唯一路径。"
                                )
                            return msg
            return None

        return None

    # ─── ★ 1.9.1: 工具 → GIF 的那条路整个删掉了 ───
    #   你原话："决定返回之前的方式，每一次思考的最后给 json"。
    #   所以 TOOL_GIF_HINTS（按工具名的默认标签表）和 _tool_gif_hint() 都不要了；
    #   模型想换动作就在思考末尾吐 {"gif": "名字"}，由形象管理统一处理。

    def _prepare_messages(self, messages: list[dict]) -> list[dict]:
        """★ 1.6.0: 发送前的消息体检 —— 修掉会让 API 直接 400 的三种情况：

        1. deepseek 思考模式要求**每条带 tool_calls 的 assistant 消息**都带
           reasoning_content，否则报 "The reasoning_content in the thinking mode must
           be passed back to the API"（实测出现过多次，整轮回复被丢掉）。
        2. 历史里可能出现连续 tool_calls 但 tool 结果缺失的链（老库/裁剪导致），
           OpenAI 兼容接口会 400，这里把没有对应 tool 结果的 tool_calls 链丢弃。
        3. ★ 1.9.0: 光"丢弃残缺链"还不够 —— 老实现**只丢 assistant 那条、把它的
           tool 结果留在列表里**，于是 tool 结果变成孤儿，下一轮照样 400：
               Messages with role 'tool' must be a response to a preceding
               message with 'tool_calls'
           （真机实测 21:17:35，整轮对话直接失败、用户什么都收不到。）
           另外 OpenAI 兼容接口要求 tool 结果**紧跟在** assistant.tool_calls
           之后；链路中间被塞进别的消息（附件卡片、系统提示等）同样 400。
           所以这里改成"按块处理"：一个 tool_calls 块必须连它的结果一起成立，
           中间夹着的其它消息挪到块后面，残缺的块**整块丢弃**。
        """
        out: list[dict] = []
        blk: Optional[dict] = None      # {"a": assistant消息, "ids": 未配对的id,
                                        #  "tools": 已配对的结果, "extra": 夹在中间的消息}

        def _flush():
            nonlocal blk
            if blk is None:
                return
            if blk["ids"]:
                # 残缺链：assistant **连同它的 tool 结果**一起丢，绝不留孤儿
                _log.debug(f"消息体检：丢弃残缺工具链 {blk['ids']}"
                           f"（连 {len(blk['tools'])} 条结果一起）")
            else:
                out.append(blk["a"])
                out.extend(blk["tools"])
            out.extend(blk["extra"])    # 夹在中间的消息不丢，只是挪到块后面
            blk = None

        for m in messages:
            # ★ 深拷贝一层，避免体检时把调用方（self.messages）写脏
            m = dict(m)
            role = m.get("role")
            if role == "assistant" and m.get("tool_calls"):
                _flush()
                ids = [tc.get("id") for tc in (m.get("tool_calls") or [])
                       if isinstance(tc, dict) and tc.get("id")]
                blk = {"a": m, "ids": list(ids), "tools": [], "extra": []}
                continue
            if blk is not None:
                tid = m.get("tool_call_id")
                if role == "tool" and tid in blk["ids"]:
                    blk["ids"].remove(tid)
                    blk["tools"].append(m)
                else:
                    blk["extra"].append(m)
                continue
            if role == "tool":
                # 找不到对应请求的 tool 结果 → 丢弃（否则 400）
                continue
            out.append(m)
        _flush()

        # 补齐 reasoning_content（思考模式必需字段）
        # ★ 1.8.3: 不止「带 tool_calls 的 assistant 消息」——deepseek-flash 思考模式
        #   要求历史里**每一条** assistant 消息都带 reasoning_content。
        #   实测踩坑（logs/prts_20260915_203645.log 第 203 行）：
        #   某一轮模型返回空 thinking（thinking=0字符），第 1095 行 `if thinking_buffer:`
        #   为假 → 落进 self.messages 的 assistant 消息没有 reasoning_content →
        #   紧接着的边界轮把整个历史重发 → 400
        #   "The reasoning_content in the thinking mode must be passed back to the API"。
        #   判定"这个模型用不用思考模式"：历史里已经出现过 reasoning_content 才算，
        #   这样换到不支持该字段的普通模型时不会平白多塞一个字段进去。
        #
        # ★★ 接管修正（2026-09-17 22:05）——**这个判定条件不够，主动说话必炸就是这个**：
        #   实测（用真实历史 100 条 + 14 个工具直连接口复现，见日志本轮记录）：
        #     · **请求里只要带 tools**，思考模式就要求「每一条 assistant」都带回
        #       reasoning_content —— 不带 tools 时接口反而宽容。
        #     · 而历史一旦从库里重载（`_load_topic_messages` 只存 role/content，
        #       **不存 reasoning_content、也不存 tool_calls、tool 消息直接跳过**），
        #       `history_uses_reasoning` 立刻变 False → 纯文本 assistant 全都不补
        #       → 下一次请求 400。
        #     · 主动说话正好踩这个：它注入那一刻（messages=102 = 重载 100 + 注入 2）
        #       历史就是刚重载出来的，于是每次都报
        #       "The `reasoning_content` in the thinking mode must be passed back"。
        #   修法：带工具（或历史用过思考）就**一律补**。占位值用 " " 足够 ——
        #   实测接口既接受空格也接受真实思考文本，**只有缺字段才 400**。
        _client = getattr(self, "client", None)
        _model = str(getattr(_client, "model", "") or "").lower()
        _has_tools = bool(getattr(_client, "tools", None))
        _deepseek = "deepseek" in _model
        history_uses_reasoning = any(
            m.get("reasoning_content") for m in out if m.get("role") == "assistant")
        _need_all = history_uses_reasoning or (_deepseek and _has_tools)
        for m in out:
            if m.get("role") != "assistant":
                continue
            if m.get("reasoning_content") is None and (
                    m.get("tool_calls") or _need_all):
                m["reasoning_content"] = m.get("reasoning_content") or " "
        return out

    def _side_progress_line(self, tool_calls: list) -> str:
        """★ 1.9.3 第 9 轮（用户选 B）：**旁路小请求**拿一句进度。

        为什么单独发一次请求：真机实测（10-03）模型主动调 progress_report = **0 次**，
        光靠提示词哄不出来。用户拍板把汇报做进循环结构 → 每轮工具跑完后，
        由程序发一次**极小的独立请求**，让模型用一句话说"我在做什么"。

        三条硬要求：
          · **不碰 self.messages**（不占主对话轮次、不破坏 tool_calls↔tool 配对 → 否则 400）；
          · 旁路请求**临时关掉工具**（`set_tools([])`），免它又想调工具；
          · 任何异常都吞掉并返回空串（调用方退回代码兜底），绝不影响主流程。
        """
        try:
            _acts = []
            for tc in (tool_calls or []):
                try:
                    _acts.append(f"- {tc['function']['name']} "
                                 f"{str(tc['function']['arguments'])[:100]}")
                except Exception:
                    pass
            _last_user = ""
            with self._lock:
                for m in reversed(self.messages):
                    if m.get("role") == "user":
                        _last_user = str(m.get("content") or "")[:200]
                        break
            _msgs = [{"role": "user", "content": (
                f"【内部：只是让你给用户同步一句进度，不是新任务】\n"
                f"用户的要求：{_last_user}\n"
                f"我刚做完的一步：\n" + ("\n".join(_acts) or "（无）") + "\n\n"
                "请用**一句话**告诉用户你现在在做什么、到哪一步了："
                "说人话、像随口同步、不超过 30 字，别客套、别罗列步骤、"
                "别提工具名或第几轮。只输出这一句话，不要做别的事。")}]
            _saved = list(getattr(self.client, "tools", None) or [])
            try:
                self.client.set_tools([])
                _resp = self.client.chat(_msgs)
            finally:
                try:
                    self.client.set_tools(_saved)
                except Exception:
                    pass
            def _pick(r):
                if not r or not getattr(r, "choices", None):
                    return ""
                _c = (getattr(r.choices[0].message, "content", "") or "").strip()
                if not _c or '"name"' in _c or _c.startswith("{"):
                    return ""        # 防呆：吐了 JSON / 工具片段就不要这句
                return _c.splitlines()[0].strip()[:120]

            _line = _pick(_resp)
            if not _line:
                # ★ 1.9.3 第 10 轮：真机 7 次旁路里有 3 次是空的（thinking 模式下
                #   正文可能为空）。**再用更严的提示重试一次**，还空才交给兜底。
                _log.debug("[进度汇报·模型] 第一次为空 → 用更严提示重试一次")
                _msgs2 = [{"role": "user", "content":
                           "只输出一句话（20 字以内）告诉用户你正在做什么，"
                           "不要推理、不要解释、不要输出别的任何内容。"}]
                try:
                    self.client.set_tools([])
                    _line = _pick(self.client.chat(_msgs2))
                finally:
                    pass
            return _line
        except Exception as e:
            _log.debug(f"[进度汇报·模型] 旁路请求失败: {e}")
            return ""

    def _run_loop(self):
        tools = self.tool_manager.get_openai_tools()
        self.client.set_tools(tools)
        _log.info(f"对话循环启动: model={self.client.model}, 工具数={len(tools)}, messages={len(self.messages)}")

        round_count = 0
        consecutive_tool_failures = 0  # ★ 1.4.0: 跟踪连续工具失败次数
        tool_just_executed = False     # ★ 1.4.0+: 边界轮工具已执行标记

        _finished_ok = False      # ★ 1.9.3 第 11 轮：是否"正常收尾"（不是被预算/中断截断）
        while round_count < self.MAX_ROUNDS and not self._abort_chat:
            round_count += 1
            _log.debug(f"第 {round_count} 轮对话")
            # ★★ 1.9.3 第 11 轮（用户要求：全局预算感知）：
            #   走到 75% 就先提醒模型"该收尾了" —— 让它自己规划，而不是闷头跑到撞墙。
            if round_count == max(1, int(self.MAX_ROUNDS * 0.75)):
                try:
                    with self._lock:
                        self.messages.append({
                            "role": "system",
                            "content": (f"（预算提醒）你这一轮的轮次预算已用 "
                                        f"{round_count}/{self.MAX_ROUNDS}。请优先把**当前成果"
                                        f"交付/收尾**；如果确实还差很多步，先用一句话告诉用户"
                                        f"进展和大概还差多少，再继续。"),
                        })
                    _log.info(f"[预算] 已在 {round_count}/{self.MAX_ROUNDS} 处提醒模型收尾")
                except Exception as _e_b:
                    _log.debug(f"[预算] 提醒插入失败: {_e_b}")
            _last_failed_tool = ""
            _last_failed_err = ""
            # ★★ 1.9.3（第 3 轮批注 2）：**每轮工具调用结束后，由代码兜底发一句进度**。
            #   为什么不由模型发：实测（09-22 三次运行）模型主动调 `progress_report`
            #   的次数是 0/0/0，有一次整整 2 分钟没吐一个字（用户还发了个"？"）
            #   → 按批注 2 改成代码兜底，不依赖模型自觉。
            #   位置说明：这里"新一轮开始"= 上一轮的工具调用已经跑完，正是批注说的时机。
            #   限流 20 秒一条，避免长任务里把聊天窗口刷屏。
            # ★ 1.9.3 第 9 轮：原来的「上下文提醒」已删 —— 汇报改成**结构性旁路请求**，见下面 tool_just_executed 处的调用。

            try:
                with self._lock:
                    # ★ 第 51 轮：这里**已持 self._lock**，必须把快照传进去，
                    #   不能再让 _build_llm_messages 自己加锁（不可重入 → 死锁）。
                    messages_snapshot = self._prepare_messages(
                        self._build_llm_messages([dict(m) for m in self.messages]))
                _log.debug(f"第{round_count}轮: 发起流式请求 ({len(messages_snapshot)}条消息)...")
                stream = self.client.chat_stream(messages_snapshot)
                content_buffer = ""
                thinking_buffer = ""
                tool_calls_buffer = []

                has_tool_call = False
                tool_json_leak = False
                text_sent_to_ui = False
                # ★ 1.9.0: 正文里的协议 JSON 过滤器（见 reply_filter 的说明）：
                #   模型有时把 {"gif":…,"meme":…} 吐在正文里，必须边流边挡，
                #   否则等流结束再删就已经显示给用户了（真机实测就是这么翻车的）。
                proto_filter = ProtocolTailFilter()
                # ★ 1.9.3 第 22 轮：再挡一道"工具调用标记的开头"。
                #   为什么需要：泄漏判定要"看见完整标记"才成立，而标记最前面那个 `<`
                #   是单独一个流包 —— 判定还没成立时它已经被显示出去了，
                #   于是用户每条进度汇报后面都看到一个孤零零的 `<`（真机截图）。
                marker_filter = ToolMarkerTailFilter()

                for chunk in stream:
                    if self._abort_chat:
                        # ★ 1.9.0（你的 bug 报告"终止对话没生效"）：
                        #   光 break 只是不再读，底下的 HTTP 流还开着 ——
                        #   显式 close 让请求真正断掉。
                        try:
                            stream.close()
                        except Exception:
                            pass
                        break
                    delta = chunk.choices[0].delta if chunk.choices else None
                    if delta is None:
                        continue

                    if delta.tool_calls:
                        has_tool_call = True

                    # ★ 1.5.0: 思考流（reasoning_content）— 实时推送给形象线程
                    reasoning = getattr(delta, "reasoning_content", None)
                    if reasoning:
                        thinking_buffer += reasoning
                        if self.on_thinking:
                            self.on_thinking(reasoning)

                    if delta.content:
                        content_buffer += delta.content
                        # ★★ 1.9.3 第 15 轮（用户报"把思考内容发到了聊天界面"）：
                        #   真机日志 `LLM回复: <｜｜DSML｜｜ calls>` —— 模型把**工具调用
                        #   以 DSML 文本**吐在正文里（不是标准 tool_calls 字段）。
                        #   以前只认 `"name":`/`"arguments":` → DSML 被当正文显示到聊天窗口。
                        #   现在把 DSML / 全角竖线标记也纳入"工具调用泄漏"判定：
                        #   置 `tool_json_leak=True` → 后续不再 on_text（不显示）。
                        # 1.9.3 第 17 轮修正：按累积正文判断，不能按单个 chunk
                        #（DSML 标记是跨 chunk 拼出来的，上一轮单 chunk 判断没生效）
                        _blob = content_buffer
                        if not has_tool_call and (
                                '"name":' in _blob or '"arguments":' in _blob
                                or 'DSML' in _blob or '<｜' in _blob
                                or '<|tool_calls|>' in _blob or 'invoke name=' in _blob):
                            tool_json_leak = True
                        # ★ 1.9.0: 先过协议过滤器 —— 它会把 {"gif":…,"meme":…} 拦下来，
                        #   只把"该显示的正文"转发给聊天窗口。
                        safe_content = proto_filter.feed(delta.content)
                        # ★ 1.9.3 第 22 轮：再过一道"标记开头"过滤器（挡掉那个孤立的 `<`）
                        safe_content = marker_filter.feed(safe_content)
                        if not tool_json_leak and safe_content and self.on_text:
                            self.on_text(safe_content)
                            text_sent_to_ui = True

                    if delta.tool_calls:
                        for tc in delta.tool_calls:
                            if tc.index is None or tc.function is None:
                                continue
                            idx = tc.index
                            while len(tool_calls_buffer) <= idx:
                                tool_calls_buffer.append(None)
                            if tool_calls_buffer[idx] is None:
                                tool_calls_buffer[idx] = {
                                    "id": tc.id or "",
                                    "function": {
                                        "name": tc.function.name or "",
                                        "arguments": tc.function.arguments or "",
                                    }
                                }
                            elif tc.function and tc.function.arguments:
                                tool_calls_buffer[idx]["function"]["arguments"] += tc.function.arguments or ""

                valid_tool_calls_count = len([tc for tc in tool_calls_buffer if tc is not None])
                _log.debug(f"第{round_count}轮: 流式完成, content={len(content_buffer)}字符, "
                            f"thinking={len(thinking_buffer)}字符, tool_calls={valid_tool_calls_count}")

                # ★ 1.9.0: 流结束 → ① 处理过滤器里没闭合的尾巴 ② 把正文里的协议
                #   JSON 剥掉（存进历史的那份必须干净）③ 把协议交给形象线程。
                #   （只在过滤器由于异常没抓住时，下面的 split_protocol 才兜底。）
                _tail = proto_filter.flush()
                if _tail:
                    _tail = marker_filter.feed(_tail)
                if _tail and not tool_json_leak and self.on_text:
                    self.on_text(_tail)
                    text_sent_to_ui = True
                # ★ 1.9.3 第 22 轮：marker 过滤器也收尾 —— 它 hold 的那点若是普通文本
                #   （比如正文里一个孤立的 `<`），这里必须补发，**不能吃掉用户的字**。
                #   但若本轮已判定为工具调用泄漏，就永远不要吐出来。
                _mtail = marker_filter.flush()
                if _mtail and not tool_json_leak and self.on_text:
                    self.on_text(_mtail)
                    text_sent_to_ui = True
                content_buffer, _proto_split = split_protocol(content_buffer)
                _proto = proto_filter.payload or _proto_split
                if _proto and not valid_tool_calls_count:
                    _log.info(f"[协议] 从正文里取到动作/表情包: {_proto}")
                    if self.on_protocol:
                        try:
                            self.on_protocol(_proto)
                        except Exception as _e:
                            _log.debug(f"协议回调失败: {_e}")

                # ★ 1.5.0: 思考结束 → 形象线程立即解析并切换 GIF（赶在回复输出前）
                if thinking_buffer and self.on_thinking_done:
                    try:
                        self.on_thinking_done()
                    except Exception:
                        pass

                if self._abort_chat:
                    break

                # 工具调用泄漏恢复（★ 1.9.3 第 21 轮：从"只认 JSON 形状"扩成
                #   先认 DSML 再认 JSON —— 真机第 11 轮就是因为不认 DSML，
                #   把 `file_ops send` 整个丢掉，PPT 根本没发出去）
                if tool_json_leak and not has_tool_call and content_buffer.strip():
                    _recovered = recover_leaked_tool_calls(content_buffer)
                    if _recovered:
                        tool_calls_buffer = _recovered
                        content_buffer = ""

                valid_tool_calls = [tc for tc in tool_calls_buffer if tc is not None]
                text_only_no_tool = bool(content_buffer.strip()) and not valid_tool_calls

                if valid_tool_calls and not self._abort_tool:
                    with self._lock:
                        # ★ 1.6.0: 思考模式（deepseek-flash thinking）要求**每一条**
                        #   assistant 消息都带回 reasoning_content，否则下一次请求直接 400
                        #   （"The reasoning_content in the thinking mode must be passed
                        #   back to the API"）。这里把本轮思考一并写回。
                        _tc_msg = {
                            "role": "assistant", "content": None,
                            "tool_calls": [{
                                "id": tc["id"], "type": "function",
                                "function": tc["function"],
                            } for tc in valid_tool_calls]
                        }
                        if thinking_buffer:
                            _tc_msg["reasoning_content"] = thinking_buffer
                            # ★ 1.9.1: 调工具那一轮的思考也要落日志（否则"为什么调这个工具"
                            #   就永远查不到）。同一套 `[思考]` 标签。
                            _log.info(f"[思考] 调工具前的思考 ({len(thinking_buffer)} 字)：\n"
                                      f"{thinking_buffer}")
                        self.messages.append(_tc_msg)

                    # ★ 1.4.0+: 延迟注入标记（避免在 tool_calls→tool 链中插入 system 消息破坏 API 格式）
                    search_warn_needed = 0       # 0=不需要, >0=需要(值为连续次数)
                    failure_warn_needed = 0      # 0=不需要, >0=需要(值为连续失败次数)

                    for tc in valid_tool_calls:
                        if self._abort_chat or self._abort_tool:
                            break
                        func_name = tc["function"]["name"]
                        # ★★ 1.9.3 第 18 轮：模型有**已知 bug**（长提示词下 40~60% 的工具
                        #   调用参数是非法 JSON，见 issue #1541 / vLLM #47449、#48343）。
                        #   以前直接空参 → 工具报「需要指定 action」→ 白跑一轮。
                        #   现在先**尽力修复**（补引号/去尾逗号/补右括号），修不了才空参。
                        _raw_args = tc["function"].get("arguments") or "{}"
                        try:
                            func_args = json.loads(_raw_args)
                        except Exception:
                            func_args = _repair_json_args(_raw_args)
                            if func_args is None:
                                _log.warning(f"[工具参数] JSON 非法且修不好，按空参处理: {_raw_args[:120]}")
                                func_args = {}
                            else:
                                _log.info(f"[工具参数] JSON 非法但已修复: {_raw_args[:60]}")

                        _log.info(f"LLM请求工具: {func_name}({func_args})")
                        if self.on_tool_call:
                            self.on_tool_call(func_name, func_args)

                        # ★ 1.4.0+: 爬虫拦截守卫
                        guard_msg = self._guard_tool_call(func_name, func_args)
                        if guard_msg is not None:
                            result = guard_msg
                        else:
                            result = self.tool_manager.execute_tool(func_name, **func_args)

                        # ★ 1.4.0: 检测工具执行是否失败
                        is_failure = _is_tool_failure(result)
                        if is_failure:
                            consecutive_tool_failures += 1
                            # ★ 1.9.3：记下最近一次失败的**工具名 + 原因**，
                            #   供"放弃"时如实告诉用户卡在哪一步（见下面空回复兜底）。
                            _last_failed_tool = func_name
                            _last_failed_err = str(result)[:120]
                            _log.warning(f"工具 {func_name} 执行失败(连续{consecutive_tool_failures}次): {result[:1000]}")
                        else:
                            consecutive_tool_failures = 0

                        # ★ 1.4.0+: 搜索循环检测 — 仅计数，不在此处注入 system 消息
                        if func_name == "web_search" and guard_msg is None:
                            self._consecutive_searches += 1
                            _log.debug(f"连续搜索: {self._consecutive_searches}次")
                            if self._consecutive_searches >= 5 and search_warn_needed == 0:
                                search_warn_needed = self._consecutive_searches
                        elif func_name != "web_search":
                            if guard_msg is not None:
                                # 被拦截的爬虫命令视为变相搜索，不重置计数
                                self._consecutive_searches += 1
                                _log.debug(f"连续搜索(含拦截): {self._consecutive_searches}次")
                                if self._consecutive_searches >= 5 and search_warn_needed == 0:
                                    search_warn_needed = self._consecutive_searches
                            else:
                                self._consecutive_searches = 0  # 其他正常工具调用才重置

                        # ★ 1.9.1: 放宽到 2000 字（工具结果常常是关键线索，截 80 字等于没有）
                        _log.info(f"工具返回: {func_name} -> {result[:2000]}"
                                  + (f" …(共 {len(result)} 字，已截断)"
                                     if len(result) > 2000 else ""))
                        if self.on_tool_result:
                            self.on_tool_result(func_name, result)
                        # ★ 1.9.1（你的重新设计）：工具返回**不再**触发形象切换。
                        #   你原话："决定返回之前的方式，每一次思考的最后给 json"。
                        #   所以这里原来那段"取工具返回的 gif / 按工具名查默认标签"
                        #   整个删掉了 —— 形象切换只剩两个来源（待机计时器、
                        #   思考 JSON 拦截器），都走 pet_manager.apply_switch()。

                        with self._lock:
                            self.messages.append({
                                "role": "tool", "tool_call_id": tc["id"], "content": result})

                        # ★ 1.4.0: 连续失败延迟注入（标记，不在 tool 结果流中插入）
                        if consecutive_tool_failures >= 3 and failure_warn_needed == 0:
                            failure_warn_needed = consecutive_tool_failures

                        try:
                            self.db.add_chat_history("tool", result[:500], func_name,
                                                     tool_call_id=tc["id"], topic_id=self._topic_id)
                        except Exception:
                            pass

                    # ★ 1.4.0+: 所有 tool 结果已追加完毕，此处安全注入 system 消息
                    if search_warn_needed > 0:
                        _log.warning("搜索循环触发: 已连续 {} 次".format(search_warn_needed))
                        with self._lock:
                            self.messages.append({
                                "role": "system",
                                "content": (
                                    "⚠️ 你已经连续用 web_search 搜索了 {n} 次还没产出任何结果！"
                                    "web_search 每次只返回摘要片段，永远不可能通过反复搜索获取完整数据。"
                                    "请立即停止搜索，用已有结果直接创建文档：office create 创建文件 → office add 填表格 → office set 设样式。"
                                    "不完整的表格远好过永远在搜索。现在就创建！"
                                ).format(n=search_warn_needed)
                            })
                        self._consecutive_searches = 0

                    if failure_warn_needed > 0:
                        _log.warning("连续工具失败: {} 次".format(failure_warn_needed))
                        with self._lock:
                            self.messages.append({
                                "role": "system",
                                "content": (
                                    "已连续 {n} 次工具调用失败。请检查参数是否正确，"
                                    "或换用其他工具/方式完成任务。可以先用 shell_exec 检查环境状态。"
                                ).format(n=failure_warn_needed)
                            })
                        consecutive_tool_failures = 0

                    # ★★ 1.9.3 第 9 轮（用户选 B：汇报做进循环结构）——
                    #   **每轮工具调用结束后**（用户模型的第 7 步），用一次旁路小请求
                    #   让模型说一句进展，直接显示给用户。不碰主对话、不占轮次。
                    #   旁路失败 → 退回代码兜底那句（20 秒限流、本轮报过就不发）。
                    try:
                        import time as _t_r
                        from src.core import progress_bus as _pb_r
                        _rep_line = ""
                        if _t_r.time() - float(getattr(self, "_last_side_report", 0.0)) >= 3.0:
                            self._last_side_report = _t_r.time()
                            _t0_r = _t_r.time()
                            _rep_line = self._side_progress_line(valid_tool_calls)
                            _log.info(f"[进度汇报·模型] 旁路耗时 {_t_r.time() - _t0_r:.1f}s"
                                      f" → {_rep_line[:60] or '(空)'}")
                        if _rep_line:
                            _pb_r.report(_rep_line)
                        elif int(_pb_r.count_this_turn() or 0) == 0 and \
                                _t_r.time() - float(getattr(self, "_last_progress_at", 0.0)) >= 20.0:
                            self._last_progress_at = _t_r.time()
                            _pb_r.report("还在弄，我继续～")
                            _log.info("[进度兜底] 已发兜底汇报")
                    except Exception as _e_r:
                        _log.debug(f"[进度汇报] 失败: {_e_r}")
                    self._empty_rounds = 0   # ★ 1.9.3 第 10 轮：有过推进就不算空
                    tool_just_executed = True  # ★ 1.4.0+: 标记工具已执行
                    # ★ 1.9.0（你的 bug 报告"终止工具没生效"）：
                    #   以前点了「终止工具」，剩下的工具确实不跑了，但这里
                    #   `continue` 回到外层 while —— 而外层只看 `_abort_chat`，
                    #   于是**又发起了一轮新的 LLM 请求**，看起来就是"没生效"。
                    #   现在：终止工具 = 剩下的工具不跑 + 不再发起下一轮请求。
                    if self._abort_tool:
                        _log.info("终止工具：跳过后续 LLM 轮次")
                        tool_just_executed = False
                        break
                    continue

                # 纯文本回复
                if text_only_no_tool:
                    promise_words = ["正在帮你查", "马上查", "这就查", "这就帮你", "正在获取", "正在查询", "正在搜索"]
                    said_will_do = any(w in content_buffer for w in promise_words)
                    is_suggestive = ("如果要" in content_buffer or "可以试试" in content_buffer or "暂时没有" in content_buffer)

                    if said_will_do and not is_suggestive and round_count == 1 and not self._abort_tool:
                        _log.info(f"检测到口嗨回复，强制重试: {content_buffer[:50]}")
                        if self.on_system_msg:
                            self.on_system_msg("[检测到需要工具，重新请求...]")
                        with self._lock:
                            self.messages.append({"role": "system", "content": "请直接用工具获取结果，不要再输出预告文字。调用工具后根据结果回答。"})
                        continue

                    # ★ 1.9.1（你 01:4x 的批注）：把**思考内容**也写进日志。
                    #   你原话："思考内容不可见，希望log能够尽可能的记录所有数据，
                    #   这样才好调试"。以前 reasoning_content 只喂给形象线程
                    #   （用来切 GIF），**从没落过日志**，所以出问题时看不到模型在想什么。
                    #   标签统一用 `[思考]`，你可以直接搜这个字眼；
                    #   也为下面的 `grep "[思考]"` 回归断言留了锚点。
                    if thinking_buffer:
                        _log.info(f"[思考] 本轮思考内容 ({len(thinking_buffer)} 字)：\n"
                                  f"{thinking_buffer}")
                    _log.debug(f"LLM回复: {content_buffer}")
                    with self._lock:
                        msg_dict = {"role": "assistant", "content": content_buffer,
                                    "speaker": self._character}
                        # ★ 1.5.0: 思考模型需要把 reasoning_content 回传给 API
                        if thinking_buffer:
                            msg_dict["reasoning_content"] = thinking_buffer
                        self.messages.append(msg_dict)
                    try:
                        self.db.add_chat_history("assistant", content_buffer,
                                                 topic_id=self._topic_id,
                                                 speaker=self._character)
                    except Exception:
                        pass

                # 空回复兜底
                if not content_buffer.strip() and not valid_tool_calls:
                    # ★ 1.9.3 第 12 轮：把 thinking 长度也打出来 ——
                    #   真机 10-04 00:15 的"连续空回复"就是**思考吃光了 max_tokens**
                    #   （thinking 5527 字 → content=0）。日志里有这个数字就能一眼判定。
                    _log.warning(f"第{round_count}轮空回复"
                                 f"（thinking={len(thinking_buffer)} 字，"
                                 f"max_tokens={getattr(self.client, 'max_tokens', '?')}）")
                    if round_count == 1:
                        self.client.set_tools([])
                        if self.on_system_msg:
                            self.on_system_msg("[模型无响应，无工具重试...]")
                        continue
                    else:
                        # ★★ 1.9.3 第 10 轮：**别一次空回复就整轮放弃**。
                        #   真机 10-03 23:58：做 PPT 走到第 8 轮（上限 20）时空回复 →
                        #   直接"放弃"→ 长任务中道崩殂，前面 17 次工具调用全白费。
                        #   改成：先插一句提醒**再给一次机会**，连续两次空回复才放弃。
                        _empt = int(getattr(self, "_empty_rounds", 0)) + 1
                        self._empty_rounds = _empt
                        if _empt <= 2:
                            _log.warning(f"第{round_count}轮空回复（第 {_empt} 次，再试一次）")
                            if self.on_system_msg:
                                self.on_system_msg("[模型这一轮没输出，已提醒它继续...]")
                            # ★★ 1.9.3 第 28 轮：**提醒要按"为什么空"来说**。
                            #   真机 `logs/prts_20261004_040213.log`：
                            #     第14轮空回复（thinking=**13584** 字，max_tokens=8192）
                            #     第15轮空回复（thinking=**13372** 字，max_tokens=8192）
                            #   —— 一万三千多字的思考把输出预算吃光了，正文一个字都挤不出来。
                            #   老提醒只说"继续"，它下一轮**照样继续长篇推理**，于是又空。
                            #   现在：思考异常长时，直接把病因说出来 + 明确禁止再推理。
                            _think_len = len(thinking_buffer)
                            _mt = getattr(self.client, "max_tokens", 0) or 0
                            if _think_len and _mt and _think_len > int(_mt * 0.8):
                                _nudge = (
                                    f"（系统提醒）刚才**没有收到正文**，原因是你的思考太长了"
                                    f"（{_think_len} 字，已经把输出预算吃光）。"
                                    f"**不要再长篇推理**：直接用一句话说明当前状态，"
                                    f"然后**立刻**给出下一步的工具调用（或最终答复）。"
                                    f"想清楚「做什么」就够了，不要继续展开推导。")
                            else:
                                _nudge = ("（系统提醒）刚才没有收到你的输出。请继续："
                                          "如果任务已经做完，就给出最终答复；"
                                          "还没做完，就接着做下一步。")
                            with self._lock:
                                self.messages.append({"role": "system", "content": _nudge})
                            continue
                        _log.warning("多轮空回复，放弃")
                        # ★★ 1.9.3（用户第 2 条反馈）：**失败要说清楚卡在哪**。
                        #   旧文案"（唔...让我想想怎么说）"既没提失败、也没提下一步，
                        #   真机上用户只看到"任务没完成"、完全不知道为什么
                        #   （那次是下载被目标站 HTTP 567 拦、重试 3 次全败）。
                        try:
                            _lt = locals().get("_last_failed_tool")
                            _le = str(locals().get("_last_failed_err") or "").strip()
                            if _lt:
                                _msg = (f"抱歉，这次没弄完 —— 卡在 `{_lt}` 那一步"
                                        f"（{_le[:60] or '工具报错'}）。"
                                        f"要我换个法子再试一次吗？")
                            elif _last_failed_tool:
                                _msg = (f"抱歉，这次没弄完 —— 卡在 `{_last_failed_tool}` 那一步。"
                                        f"要我换个法子再试一次吗？")
                            else:
                                _msg = "抱歉，我这边没组织出回复，这次没弄完。要我再试一次吗？"
                        except Exception:
                            _msg = "抱歉，这次没弄完。要我再试一次吗？"
                        if self.on_text:
                            self.on_text(_msg)
                        if self.on_system_msg:
                            self.on_system_msg("[模型返回为空，请检查API配置]")
                        break

                # ★ 1.8.3: 已经给出文字回复 = 上一轮的工具结果已经消化完，
                #   不需要再走「边界轮」补一次 API 调用
                #   （不置回 False 的话，每次用过工具都会多打一次请求，
                #    而那一次正好是会踩 reasoning_content 400 的那次）。
                tool_just_executed = False
                _finished_ok = True      # ★ 1.9.3 第 11 轮：这是"正常收尾"（有正文回复）
                break

            except Exception as e:
                err_name = type(e).__name__
                if "timeout" in err_name.lower() or "timeout" in str(e).lower():
                    _log.error(f"对话超时(第{round_count}轮): {e}")
                    if self.on_system_msg:
                        self.on_system_msg("[请求超时] LLM 服务响应过慢或未响应，请检查网络或 API 配置")
                    if self.on_text:
                        self.on_text("（唔…我好像卡住了，稍后再试试吧）")
                else:
                    _log.error(f"对话循环错误(第{round_count}轮): {e}", exc_info=True)
                    # ★ 1.6.0: 若本轮带了图片直传而模型不支持 → 自动降级为纯文字重试一次
                    if self._multimodal and self._strip_images_from_messages():
                        if self.on_system_msg:
                            self.on_system_msg("[当前模型不支持图片直传，已降级为文字重试]")
                        self._multimodal = False   # 本次会话先关掉，避免每次都失败
                        round_count -= 1
                        continue
                    if self.on_system_msg:
                        # ★★ 1.9.4 真机补：连接失败要说清"为什么"，别只甩一句
                        #   Connection error —— 用户实测撞到的是"Windows 系统代理开着
                        #   但代理客户端没开"，他以为是"没给桌宠联网权限"，
                        #   对着 Connection error 完全无处下手。
                        _diag = ""
                        try:
                            from src.core.net_proxy import diagnose_conn_error
                            _diag = diagnose_conn_error(e)
                        except Exception:
                            pass
                        self.on_system_msg(
                            f"[错误] {e}" + (f"\n\n{_diag}" if _diag else ""))
                break

        # ★ 1.4.0+: 边界轮补偿 — 最后一轮执行了工具但 LLM 没机会处理结果
        # 最多追加 2 轮（工具执行 + 结果处理），避免 LLM 死循环
        # ★★ 1.9.3 第 11 轮（用户要求）：**预算用满时的收尾**（全局，任何任务都适用）。
        #   不静默结束：先拿一句"我正在做什么"，再明确告诉用户"预算用完了、要不要继续"。
        #   ★ 用户回一句就自动开新一轮 —— round_count 每条用户消息本来就从 1 起（天然重置）。
        if (not _finished_ok) and round_count >= self.MAX_ROUNDS and not self._abort_chat:
            _log.warning(f"[预算] 轮次预算用完（{self.MAX_ROUNDS} 轮）→ 汇报并询问是否继续")
            try:
                _cur = ""
                try:
                    _cur = self._side_progress_line([])
                except Exception:
                    _cur = ""
                _bmsg = (f"这一轮的「轮次预算」用完啦（我跑了 {self.MAX_ROUNDS} 轮工具调用）。"
                         + (f"刚在做：{_cur}。" if _cur else "")
                         + "要我接着往下做吗？回一句「继续」我就接着干～")
                if self.on_text:
                    self.on_text(_bmsg)
                try:
                    from src.core import progress_bus as _pb_b
                    if int(_pb_b.count_this_turn() or 0) == 0:
                        _pb_b.report(_bmsg)
                except Exception:
                    pass
            except Exception as _e_b2:
                _log.warning(f"[预算] 收尾汇报失败: {_e_b2}")
            tool_just_executed = False   # 预算已用完 → **不再走边界轮**
        boundary_rounds = 0
        # ★ 1.9.0：边界轮也要认「终止工具」—— 否则终止后还会补一次 LLM 调用
        while tool_just_executed and not self._abort_chat \
                and not self._abort_tool and boundary_rounds < 2:
            boundary_rounds += 1
            _log.info(f"边界轮 #{boundary_rounds}: 追加处理...")
            try:
                with self._lock:
                    # ★ 第 51 轮：这里**已持 self._lock**，必须把快照传进去，
                    #   不能再让 _build_llm_messages 自己加锁（不可重入 → 死锁）。
                    messages_snapshot = self._prepare_messages(
                        self._build_llm_messages([dict(m) for m in self.messages]))
                response = self.client.chat(messages_snapshot)
                if not response or not response.choices:
                    _log.debug(f"边界轮 #{boundary_rounds}: LLM 无有效响应")
                    break
                msg = response.choices[0].message
                content = msg.content or ""
                tool_calls = getattr(msg, "tool_calls", None)
                has_tool = tool_calls and len(tool_calls) > 0

                if has_tool:
                    # 边界轮 LLM 还想调工具 → 执行所有工具，再追加一轮
                    _log.info(f"边界轮 #{boundary_rounds}: LLM 请求 {len(tool_calls)} 个工具，执行后追加轮...")
                    # 先添加 assistant 消息（含所有 tool_calls）
                    openai_tool_calls = [
                        {"id": tc.id, "type": "function",
                         "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                        for tc in tool_calls
                    ]
                    with self._lock:
                        # ★ 1.6.0: 思考模型（deepseek-flash thinking 模式）要求
                        #   reasoning_content 必须回传，否则下一轮 API 直接 400
                        _bc_msg = {"role": "assistant", "content": None,
                                   "tool_calls": openai_tool_calls}
                        _bc_think = getattr(msg, "reasoning_content", None)
                        if _bc_think:
                            _bc_msg["reasoning_content"] = _bc_think
                        self.messages.append(_bc_msg)
                    for tc in tool_calls:
                        # ★ 1.9.0：边界轮里点了终止 → 剩下的工具不跑
                        if self._abort_chat or self._abort_tool:
                            break
                        tool_name = tc.function.name
                        tool_args_str = tc.function.arguments
                        if isinstance(tool_args_str, str):
                            try:
                                tool_args = json.loads(tool_args_str)
                            except json.JSONDecodeError:
                                # ★ 1.4.0+: 尝试修复常见 JSON 问题
                                repaired = _repair_json(tool_args_str)
                                try:
                                    tool_args = json.loads(repaired)
                                    _log.debug(f"边界轮 JSON 修复成功: {tool_name}")
                                except json.JSONDecodeError:
                                    _log.warning(f"边界轮 JSON 解析失败({tool_name}): {tool_args_str[:200]}")
                                    tool_args = {"action": "help"}  # 兜底action
                        else:
                            tool_args = tool_args_str
                        _log.info(f"边界轮 #{boundary_rounds}: 执行工具 {tool_name}")
                        if self.on_tool_call:
                            self.on_tool_call(tool_name, tool_args)
                        # ★ 爬虫拦截守卫（边界轮同样生效）
                        guard_msg = self._guard_tool_call(tool_name, tool_args)
                        if guard_msg is not None:
                            tool_result = guard_msg
                        else:
                            tool_result = self.tool_manager.execute_tool(tool_name, **tool_args)
                        with self._lock:
                            self.messages.append({"role": "tool", "tool_call_id": tc.id,
                                                  "content": str(tool_result)[:4000]})
                        if self.on_tool_result:
                            self.on_tool_result(tool_name, str(tool_result)[:500])
                    _log.debug(f"边界轮 #{boundary_rounds}: 执行完成")
                    tool_just_executed = True  # 继续下一轮
                elif content.strip():
                    with self._lock:
                        _bt_msg = {"role": "assistant", "content": content,
                                   "speaker": self._character}
                        # ★ 1.6.0: 同样回传 reasoning_content（thinking 模式必需）
                        _bt_think = getattr(msg, "reasoning_content", None)
                        if _bt_think:
                            _bt_msg["reasoning_content"] = _bt_think
                        self.messages.append(_bt_msg)
                    try:
                        self.db.add_chat_history("assistant", content,
                                                 topic_id=self._topic_id,
                                                 speaker=self._character)
                    except Exception:
                        pass
                    # ★ 修复"回复重复"：流式阶段已通过 on_text(delta.content) 增量推送，
                    #   这里仅在「本轮从未增量推送」（如被 tool_json_leak 吞掉）时才补发整段，
                    #   避免整段 content 再追加一次导致重复显示。
                    if not text_sent_to_ui and self.on_text:
                        self.on_text(content)
                    _log.debug(f"边界轮 #{boundary_rounds}: LLM 处理完成, content={len(content)}字符, "
                               f"text_sent_to_ui={text_sent_to_ui}")
                    tool_just_executed = False  # 结束
                else:
                    _log.debug(f"边界轮 #{boundary_rounds}: LLM 返回空内容（无文本也无工具调用）")
                    tool_just_executed = False  # 结束
            except Exception as e:
                _log.warning(f"边界轮 #{boundary_rounds} 失败: {e}")
                tool_just_executed = False

        self._streaming = False
        _log.debug(f"对话结束 (共{round_count}轮)")

        self._trim_messages()
        self._check_token_limit()

        if self.on_done:
            self.on_done()


_session: Optional[ChatSession] = None


def get_session() -> ChatSession:
    global _session
    if _session is None:
        _session = ChatSession()
    return _session


