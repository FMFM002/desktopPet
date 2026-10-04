"""
用户画像系统 — 长期记忆与个性化（重构 v2.0）
- 数据存储：SQLite (long_term_memories.db)
- 废弃：JSON 文件持久化（user_profile.json / long_term_memory.json）
- 保留：画像提取器 ProfileExtractor（LLM 逻辑不变，存储改 SQLite）
- TODO: 长期记忆 tool 待下一轮讨论完成后接入（当前暂用现有 set_trait 逻辑）
"""
import queue
import threading
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

_log = logging.getLogger("UserProfile")

BASE_DIR = Path(__file__).resolve().parent.parent.parent
DATA_DIR = BASE_DIR / "data"


class UserProfile:
    """用户画像 — 基于 SQLite long_term_memories 的 K-V 存储"""

    CATEGORIES = [
        "identity",        # 身份: 名字、年龄、职业
        "preferences",     # 偏好: 颜色、音乐、食物
        "habits",          # 习惯: 作息、工作习惯
        "skills",          # 技能: 编程语言、工具
        "interests",       # 兴趣: 游戏、动漫、运动
        "projects",        # 项目: 工作项目、学习项目
        "relationships",   # 关系: 家人、朋友、同事
        "goals",           # 目标: 短期/长期目标
        "environment",     # 环境: 操作系统、常用软件
        "other",
    ]

    def __init__(self, storage_path: Optional[Path] = None):
        """storage_path 保留兼容，已废弃（使用 SQLite）"""
        from src.core.database import get_db
        self._db = get_db().ltm
        self._lock = threading.Lock()
        _log.info("用户画像已连接 SQLite (long_term_memories)")

    def set_trait(self, key: str, value: str, confidence: float = 0.5,
                  source: str = "conversation", category: str = "other"):
        """设置或更新用户特征 → SQLite upsert"""
        with self._lock:
            self._db.upsert(
                variable_name=key,
                value=value,
                category=category,
                confidence=confidence,
                source=source,
            )

    def get_all(self) -> dict:
        items = self._db.get_all()
        return {item["variable_name"]: item for item in items}

    def get_summary(self, max_items: int = 20) -> str:
        return self._db.get_summary(max_items)

    def get_context_prompt(self, max_tokens: int = 1000) -> str:
        """获取注入到 LLM 上下文中的画像片段"""
        summary = self.get_summary(15)
        return f"""## 用户画像（长期记忆）
{summary}

请根据以上用户画像，个性化你的回答。如果用户提到相关信息，自然地引用已知的用户特征。
"""


class LongTermMemory:
    """长期记忆 — 基于 SQLite 存储（兼容旧接口）"""

    def __init__(self, storage_path: Optional[Path] = None, max_memories: int = 500):
        """storage_path 保留兼容，已废弃（使用 SQLite）"""
        from src.core.database import get_db
        self._db = get_db().ltm
        self._lock = threading.Lock()
        _log.info("长期记忆已连接 SQLite (long_term_memories)")

    def add(self, content: str, category: str = "general", importance: int = 1,
            source: str = "conversation", tags: list = None):
        """
        TODO-NEXT-ROUND: 长期记忆 tool 接入后，这里会改为 LLM 判断 + 结构化提取。
        当前版本：简单地把 content 当作 variable_name=content[:50] 存入 SQLite。
        重要性映射到 confidence。
        """
        with self._lock:
            # 临时方案：用内容的前 50 字符做 key
            key = f"memory_{datetime.now().strftime('%Y%m%d%H%M%S')}_{content[:30]}"
            self._db.upsert(
                variable_name=key,
                value=content,
                category=category,
                confidence=min(importance / 5.0, 1.0),
                source=source,
            )

    def search(self, keyword: str, limit: int = 10) -> list[dict]:
        """关键词搜索记忆"""
        return self._db.search(keyword, limit)

    def get_recent(self, limit: int = 20, hours: int = 24) -> list[dict]:
        """获取最近的记忆"""
        all_items = self._db.get_all()
        cutoff = (datetime.now() - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
        recent = [m for m in all_items if m.get("updated_at", "") > cutoff]
        recent.sort(key=lambda x: x.get("updated_at", ""), reverse=True)
        return recent[:limit]

    def get_by_category(self, category: str, limit: int = 20) -> list[dict]:
        items = self._db.get_by_category(category)
        return items[:limit]

    def summarize_recent(self, hours: int = 24) -> str:
        """总结最近记忆"""
        memories = self.get_recent(20, hours)
        if not memories:
            return "（最近无长期记忆）"
        lines = [f"## 长期记忆（最近{hours}小时）"]
        for m in memories:
            lines.append(f"- [{m.get('category', '')}] {m.get('value', m.get('variable_name', ''))[:100]}")
        return "\n".join(lines)


# ============================================================================
# 画像提取器 — 使用 LLM 从文本中提取用户特征
# ============================================================================

class ProfileExtractor:
    """画像提取器 — 使用 LLM1 从对话/监控中提取用户特征"""

    EXTRACTION_PROMPT = """你是一个用户画像分析器。请从以下文本中提取用户特征，输出 JSON 格式。

规则：
1. 只提取明确的信息，不要推测
2. 每条特征的 confidence 为 0.0-1.0，越明确越高
3. category 必须是以下之一: identity, preferences, habits, skills, interests, projects, relationships, goals, environment, other
4. 最多提取 5 条特征

输入文本:
{text}

请输出 JSON 数组:
[
  {{"key": "特征名(简短英文)", "value": "特征值", "confidence": 0.8, "category": "interests"}}
]
只输出 JSON，不要其他内容。"""

    def __init__(self):
        from src.core.llm_client import SummarizerClient
        self._llm = SummarizerClient()
        self._profile: Optional[UserProfile] = None
        self._long_term: Optional[LongTermMemory] = None
        self._lock = threading.Lock()

    def set_stores(self, profile: UserProfile, long_term: LongTermMemory):
        self._profile = profile
        self._long_term = long_term

    @staticmethod
    def _parse_items(raw: str) -> list:
        """从 LLM 回复里抠出 JSON 数组（容忍 ``` 围栏、前后废话）"""
        import json as _json
        s = (raw or "").strip()
        if not s:
            return []
        if s.startswith("```"):
            s = s.strip("`")
            if "\n" in s:
                s = s.split("\n", 1)[1]
        try:
            data = _json.loads(s)
        except Exception:
            i, j = s.find("["), s.rfind("]")
            if i < 0 or j <= i:
                return []
            try:
                data = _json.loads(s[i:j + 1])
            except Exception:
                return []
        return data if isinstance(data, list) else []

    def extract_from_text(self, text: str):
        """从文本中提取用户画像"""
        try:
            prompt = self.EXTRACTION_PROMPT.format(text=text[:3000])
            # ★ 1.8.10: 必须拿【原始 JSON 文本】。
            #   以前这里写的是 self._llm.summarize(prompt) —— 那个方法返回的是
            #   已经解析好的 list[str]（"- xxx" 记忆条目），于是
            #   `items = result` 拿到的是字符串列表，下面
            #   `isinstance(item, dict) and "key" in item` 永远为假 →
            #   一条画像都写不进去，却照样打印"提取了 N 条画像特征"
            #   （N 其实是记忆条目数，日志里两个数字永远相等，就是铁证）。
            raw = self._llm.complete_raw(prompt, max_tokens=800, temperature=0.2)
            if not raw:
                return

            items = self._parse_items(raw)
            if not items:
                _log.debug("画像提取：LLM 未返回可解析的 JSON 数组")
                return

            saved = 0
            for item in items:
                if isinstance(item, dict) and "key" in item and "value" in item:
                    if self._profile:
                        self._profile.set_trait(
                            key=item["key"],
                            value=item["value"],
                            confidence=item.get("confidence", 0.5),
                            source="llm_extraction",
                            category=item.get("category", "other"),
                        )
                    if self._long_term and item.get("confidence", 0) > 0.7:
                        self._long_term.add(
                            content=f"{item['key']}: {item['value']}",
                            category=item.get("category", "other"),
                            importance=4,
                            source="profile_extraction",
                        )
                    saved += 1
            # 只统计真正写进去的条数，别再报"收到了几条"来假装成功
            if saved:
                _log.info(f"从文本中提取了 {saved} 条画像特征")
            else:
                _log.debug(f"画像提取：{len(items)} 个条目均不符合 key/value 结构")
        except Exception as e:
            _log.debug(f"画像提取失败: {e}")

    # ★ 1.9.2: 单 worker 队列（见 extract_async 的说明）
    _extract_queue = None

    def extract_async(self, text: str):
        """异步提取（后台单 worker 队列）

        ★ 1.9.2（内存调研结论）：原来是**每次调用起一条新线程**、无限流 ——
        调用点有 main_window 的 3 处（粘贴图片 / 上传文件 / 定时总结），
        短时间内连着来就会堆线程。改成"一条常驻 daemon 线程 + 有界队列"：
          · 线程数恒为 1，不再随调用次数增长；
          · 队列满（默认 32）时**丢最旧的**，因为画像提取只是"锦上添花"，
            不该为了它把内存/线程堆起来。
        """
        try:
            if self._extract_queue is None:
                self._extract_queue = queue.Queue(maxsize=32)
                threading.Thread(target=self._extract_loop, daemon=True,
                                 name="profile-extract").start()
            try:
                self._extract_queue.put_nowait(text)
            except queue.Full:
                try:
                    self._extract_queue.get_nowait()      # 丢最旧的
                    self._extract_queue.put_nowait(text)
                    _log.debug("画像提取队列满 → 丢掉最旧的一条")
                except Exception:
                    pass
        except Exception as e:
            _log.debug(f"画像提取入队失败（退回同步调用）: {e}")
            try:
                self.extract_from_text(text)
            except Exception:
                pass

    def _extract_loop(self):
        """唯一的提取工作线程：串行消费队列。"""
        while True:
            try:
                item = self._extract_queue.get()
                if item is None:
                    return
                self.extract_from_text(item)
            except Exception as e:
                _log.debug(f"画像提取 worker 出错（继续跑）: {e}")


# ============================================================================
# 全局单例
# ============================================================================
_profile_instance: Optional[UserProfile] = None
_long_term_instance: Optional[LongTermMemory] = None
_extractor_instance: Optional[ProfileExtractor] = None


def get_profile() -> UserProfile:
    global _profile_instance
    if _profile_instance is None:
        _profile_instance = UserProfile()
    return _profile_instance


def get_long_term_memory() -> LongTermMemory:
    global _long_term_instance
    if _long_term_instance is None:
        _long_term_instance = LongTermMemory()
    return _long_term_instance


def get_profile_extractor() -> ProfileExtractor:
    global _extractor_instance
    if _extractor_instance is None:
        _extractor_instance = ProfileExtractor()
        _extractor_instance.set_stores(get_profile(), get_long_term_memory())
    return _extractor_instance
