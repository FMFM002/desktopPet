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
from dataclasses import dataclass

from src.core.reply_filter import ProtocolTailFilter, split_protocol

_log = logging.getLogger("Session")

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


@dataclass
class SessionMessage:
    role: str
    content: str
    tool_name: Optional[str] = None
    tool_call_id: Optional[str] = None


class ChatSession:
    """聊天会话 - 多轮循环 + 话题管理"""

    MAX_ROUNDS = 20  # ★ 1.4.0+: 提升至20轮（支持网络爬取+Excel生成等多阶段任务）
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
        """切换到某角色的对话窗口（每个角色独立的连续上下文）

        切换时先保存当前窗口状态，再为该角色恢复/创建活跃话题并载入消息。
        """
        name = (name or "").strip()
        if not name:
            return
        if name == self._character:
            if load:
                self.load_character_window(name)
            return
        # ★ 若正在流式输出，先中止，避免回复写进新角色的窗口
        if self._streaming:
            _log.info("切换角色前中止进行中的对话")
            self._abort_chat = True
            for _ in range(20):
                if not self._streaming:
                    break
                time.sleep(0.1)
        self._character = name
        _log.info(f"切换角色窗口: {name}")
        if load:
            self.load_character_window(name)
        if self.on_character_changed:
            try:
                self.on_character_changed(name)
            except Exception:
                pass

    def load_character_window(self, name: str = ""):
        """载入（或首次创建）某角色的活跃话题及其消息"""
        name = (name or self._character or self._config_character()).strip()
        if not name:
            return
        self._character = name
        topic = self.db.get_active_topic(name)
        if topic is None:
            # 首次进入该角色的窗口：若无任何历史则直接建新话题；
            # 若有历史（旧版本遗留话题）则继承最近话题的总结作为上下文续接
            self._topic_id = self.db.create_topic("", name)
            self._first_user_message = None
            summary = self.db.get_last_archived_summary(name)
            with self._lock:
                self.messages.clear()
                if summary:
                    self.messages.append({
                        "role": "system",
                        "content": f"[上一段对话的总结，用于延续上下文]\n{summary}"
                    })
            _log.info(f"为角色「{name}」创建新窗口话题 #{self._topic_id}")
        else:
            self._topic_id = topic["id"]
            self._first_user_message = None
            self._load_topic_messages(self._topic_id)
            _log.info(f"恢复角色「{name}」窗口 话题 #{self._topic_id} "
                      f"({len(self.messages)} 条)")
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
        """
        topic_id = self._topic_id
        character = self._character
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
            # 回到主流程：创建新话题并把总结作为起始上下文
            self._start_new_topic_internal(character, summary, reason)

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

    def _start_new_topic_internal(self, character: str, summary: str, reason: str):
        """内部：归档后创建新话题，把总结写入新话题上下文"""
        # 若总结期间角色已切换，则写回该角色自己的窗口
        current = self._character
        topic_id = self.db.create_topic("", character or current)
        if character != current:
            _log.info(f"总结期间角色已切换({character}→{current})，总结仅入库不注入")
            return
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
        _log.info(f"新话题 #{topic_id} 已创建（角色={character}, 原因={reason}）")
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
            topic_id = self.db.create_topic("", self._character)
            self._topic_id = topic_id
            self._first_user_message = None
            self._input_blocked = False
            with self._lock:
                self.messages.clear()
            _log.info(f"用户开启新话题 #{topic_id}（角色={self._character}, 不总结）")
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
        """创建新话题，返回 topic_id（★ 1.6.0: 归属当前角色）"""
        self._save_current_state()
        topic_id = self.db.create_topic("", self._character)
        self._topic_id = topic_id
        self._first_user_message = None
        self._input_blocked = False
        with self._lock:
            self.messages.clear()
        _log.info(f"新话题创建: id={topic_id} (角色={self._character})")
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
        """从数据库加载话题消息到内存"""
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
                    self.messages.append(msg)
            _log.debug(f"加载话题 #{topic_id} 消息: {len(self.messages)} 条")
        except Exception as e:
            _log.warning(f"加载话题消息失败: {e}")

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

    def estimate_total_tokens(self) -> int:
        """估算当前消息列表的总 token 数"""
        total = 0
        with self._lock:
            for msg in self.messages:
                content = msg.get("content", "")
                if isinstance(content, str):
                    total += self.db.chat._estimate_tokens(content)
        return total

    def is_input_blocked(self) -> bool:
        return self._input_blocked

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
        """★ 1.6.0: 载入当前角色的对话窗口（一个角色一个连续窗口）"""
        try:
            name = self._config_character()
            if not name:
                # 兜底：无可识别角色时退回最近话题
                topics = self.db.list_topics()
                if topics:
                    self._topic_id = topics[0]["id"]
                    self._character = topics[0].get("character", "") or ""
                    self._load_topic_messages(self._topic_id)
                    if self.on_topic_changed:
                        self.on_topic_changed(self._topic_id)
                else:
                    self.new_topic()
                return
            self.load_character_window(name)
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

    def clear_pending_images(self):
        self._pending_images.clear()

    @staticmethod
    def _image_to_data_uri(path: str, max_side: int = 1024, quality: int = 85) -> str:
        """把本地图片压成 JPEG 并转成 data:image/jpeg;base64,...（控制请求体积）"""
        import base64
        import io
        from PIL import Image
        with Image.open(path) as im:
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

    def _run_loop(self):
        tools = self.tool_manager.get_openai_tools()
        self.client.set_tools(tools)
        _log.info(f"对话循环启动: model={self.client.model}, 工具数={len(tools)}, messages={len(self.messages)}")

        round_count = 0
        consecutive_tool_failures = 0  # ★ 1.4.0: 跟踪连续工具失败次数
        tool_just_executed = False     # ★ 1.4.0+: 边界轮工具已执行标记

        while round_count < self.MAX_ROUNDS and not self._abort_chat:
            round_count += 1
            _log.debug(f"第 {round_count} 轮对话")
            _last_failed_tool = ""
            _last_failed_err = ""
            # ★★ 1.9.3（第 3 轮批注 2）：**每轮工具调用结束后，由代码兜底发一句进度**。
            #   为什么不由模型发：实测（09-22 三次运行）模型主动调 `progress_report`
            #   的次数是 0/0/0，有一次整整 2 分钟没吐一个字（用户还发了个"？"）
            #   → 按批注 2 改成代码兜底，不依赖模型自觉。
            #   位置说明：这里"新一轮开始"= 上一轮的工具调用已经跑完，正是批注说的时机。
            #   限流 20 秒一条，避免长任务里把聊天窗口刷屏。
            if round_count == 1:
                self._progress_nudged = False      # 每条用户消息：提醒只插一次
            if round_count > 1:
                try:
                    import time as _t_prog
                    from src.core import progress_bus as _pb_prog
                    _reported = int(_pb_prog.count_this_turn() or 0)
                    # ① 主路径：在**上下文里**提醒它自己汇报（一轮一次）。
                    #   真机证据：只在系统提示词顶部写一句时，模型主动调用次数是 0；
                    #   把提醒放到"刚跑完工具、正要决定下一步"这个位置才有效。
                    if not getattr(self, "_progress_nudged", False) and _reported == 0:
                        self._progress_nudged = True
                        with self._lock:
                            self.messages.append({
                                "role": "system",
                                "content": ("（系统提醒）你正在做多步任务。请**先调用 "
                                            "progress_report** 用一句话告诉用户你当前在做什么，"
                                            "然后继续下一步；不要输出正文、不要结束任务。"),
                            })
                        _log.info("[进度提醒] 已在上下文里插入提醒（让模型自己汇报）")
                    # ② 保底：模型始终不报时才由程序发（20 秒限流；报过就不发）。
                    elif _reported == 0:
                        _now_prog = _t_prog.time()
                        if _now_prog - float(getattr(self, "_last_progress_at", 0.0)) >= 20.0:
                            self._last_progress_at = _now_prog
                            _pb_prog.report("还在弄，我继续～")
                            _log.info(f"[进度兜底] 已发第 {round_count} 轮后的进度汇报")
                except Exception as _e_prog:
                    _log.debug(f"[进度提醒/兜底] 失败: {_e_prog}")

            try:
                with self._lock:
                    messages_snapshot = self._prepare_messages(list(self.messages))
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
                        if not has_tool_call and ('"name":' in delta.content or '"arguments":' in delta.content):
                            tool_json_leak = True
                        # ★ 1.9.0: 先过协议过滤器 —— 它会把 {"gif":…,"meme":…} 拦下来，
                        #   只把"该显示的正文"转发给聊天窗口。
                        safe_content = proto_filter.feed(delta.content)
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
                if _tail and not tool_json_leak and self.on_text:
                    self.on_text(_tail)
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

                # JSON 泄漏恢复
                if tool_json_leak and not has_tool_call and content_buffer.strip():
                    try:
                        leaked = content_buffer.strip()
                        if '{' in leaked and '}' in leaked:
                            start = leaked.index('{')
                            end = leaked.rindex('}') + 1
                            json_str = leaked[start:end]
                            data = json.loads(json_str)
                            if "name" in data and "arguments" in data:
                                func_args = data["arguments"]
                                if isinstance(func_args, str):
                                    func_args = json.loads(func_args) if func_args.strip() else {}
                                valid_tool_calls = [{
                                    "id": "leak_001",
                                    "function": {
                                        "name": data["name"],
                                        "arguments": json.dumps(func_args, ensure_ascii=False),
                                    }
                                }]
                                _log.info(f"从 content 中恢复工具调用: {data['name']}")
                                content_buffer = ""
                                tool_calls_buffer = valid_tool_calls
                    except Exception:
                        _log.debug("JSON 泄漏恢复失败，按普通文本处理")

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
                        try:
                            func_args = json.loads(tc["function"]["arguments"])
                        except json.JSONDecodeError:
                            func_args = {}

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
                        msg_dict = {"role": "assistant", "content": content_buffer}
                        # ★ 1.5.0: 思考模型需要把 reasoning_content 回传给 API
                        if thinking_buffer:
                            msg_dict["reasoning_content"] = thinking_buffer
                        self.messages.append(msg_dict)
                    try:
                        self.db.add_chat_history("assistant", content_buffer, topic_id=self._topic_id)
                    except Exception:
                        pass

                # 空回复兜底
                if not content_buffer.strip() and not valid_tool_calls:
                    _log.warning(f"第{round_count}轮空回复")
                    if round_count == 1:
                        self.client.set_tools([])
                        if self.on_system_msg:
                            self.on_system_msg("[模型无响应，无工具重试...]")
                        continue
                    else:
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
                        self.on_system_msg(f"[错误] {e}")
                break

        # ★ 1.4.0+: 边界轮补偿 — 最后一轮执行了工具但 LLM 没机会处理结果
        # 最多追加 2 轮（工具执行 + 结果处理），避免 LLM 死循环
        boundary_rounds = 0
        # ★ 1.9.0：边界轮也要认「终止工具」—— 否则终止后还会补一次 LLM 调用
        while tool_just_executed and not self._abort_chat \
                and not self._abort_tool and boundary_rounds < 2:
            boundary_rounds += 1
            _log.info(f"边界轮 #{boundary_rounds}: 追加处理...")
            try:
                with self._lock:
                    messages_snapshot = self._prepare_messages(list(self.messages))
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
                        _bt_msg = {"role": "assistant", "content": content}
                        # ★ 1.6.0: 同样回传 reasoning_content（thinking 模式必需）
                        _bt_think = getattr(msg, "reasoning_content", None)
                        if _bt_think:
                            _bt_msg["reasoning_content"] = _bt_think
                        self.messages.append(_bt_msg)
                    try:
                        self.db.add_chat_history("assistant", content, topic_id=self._topic_id)
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

    def is_streaming(self) -> bool:
        return self._streaming


_session: Optional[ChatSession] = None


def get_session() -> ChatSession:
    global _session
    if _session is None:
        _session = ChatSession()
    return _session


def reset_session():
    global _session
    _session = ChatSession()
