"""
长期记忆 Tool v2.0（完整实现）
================================================================================
职责：
  1. 输入：纯文本（聊天文本 / monitoring_events 详情）
  2. LLM 判断：是否包含值得长期记忆的信息？（YES/NO 二分类）
  3. 如果 YES → LLM 结构化提取 → 去重检查 → 冲突打包 → LLM 合并 → 入库
  4. LLM 不可用 → 放弃入库 → 标记回退 → 不污染 DB
  5. 一个 variable_name 仅一条数据（K-V 模型）

新特性 v2.0:
  - ★ 线程池：最多 2 个并行线程处理记忆写入（解决 chat + 系统同时调用）
  - ★ SQLite 写入排队：数据库写入串行化，避免并发冲突
  - ★ 30s 空闲自动卸载（由 ToolManager 策略控制）
  - ★ 禁用时优雅静默：不报错，不处理，不阻塞

严格规则：
  - LLM 不可用 → 零写入长期记忆 DB
  - 不降级、不 last-write-wins、不猜测
  - 3 次重试失败 → 标记 processed=-1
================================================================================
"""
import difflib
import json
import logging
import re
import time
import threading
import queue as queue_module
from typing import Optional
from concurrent.futures import ThreadPoolExecutor, Future

_log = logging.getLogger("LongTermMemoryTool")

# 并行线程数上限（写 DB 排队，读可并行）
MAX_PARALLEL_WORKERS = 2


class LongTermMemoryTool:
    """长期记忆工具 v2.0"""

    TOOL_NAME = "long_term_memory"
    TOOL_DESCRIPTION = (
        "管理长期记忆 — 记住用户偏好、习惯、身份、项目等信息。"
        "输入要分析的文字，输出记忆存储结果。"
    )

    def __init__(self):
        self._llm_client = None
        self._llm_model = None
        self._executor: Optional[ThreadPoolExecutor] = None
        self._executor_lock = threading.Lock()
        self._db_mutex = threading.Lock()  # SQLite 写入互斥锁
        self._last_active = time.time()     # 最后活跃时间（用于空闲卸载判断）
        self._disabled = False              # 是否被禁用
        self._futures: list[Future] = []    # 跟踪提交的任务
        self._disable_thinking = False      # ★ 1.7.1: deepseek 推理模型禁用 thinking
        _log.info("[LongTermMemoryTool v2.0] 初始化完成 (workers=%d)", MAX_PARALLEL_WORKERS)

    # ---- 线程池管理 ----

    def _get_executor(self) -> ThreadPoolExecutor:
        """获取或创建线程池（按需）"""
        with self._executor_lock:
            if self._executor is None:
                self._executor = ThreadPoolExecutor(
                    max_workers=MAX_PARALLEL_WORKERS,
                    thread_name_prefix="ltm-worker")
                _log.debug("[LTM] 线程池已创建 (workers=%d)", MAX_PARALLEL_WORKERS)
            return self._executor

    def _touch(self):
        """更新最后活跃时间"""
        self._last_active = time.time()

    def idle_seconds(self) -> float:
        """空闲秒数（用于 ToolManager 判断是否卸载）"""
        return time.time() - self._last_active

    def shutdown(self):
        """关闭线程池"""
        with self._executor_lock:
            if self._executor:
                self._executor.shutdown(wait=False)
                self._executor = None
            self._llm_client = None
            self._llm_model = None
            _log.debug("[LTM] 已关闭")

    # ---- LLM 客户端 ----

    def _get_llm(self):
        """延迟初始化 LLM 客户端"""
        if self._llm_client is None:
            try:
                from src.core.llm_client import _get_llm_config
                from openai import OpenAI
                kc = _get_llm_config()
                self._llm_client = OpenAI(
                    api_key=kc["api_key"], base_url=kc["host"],
                    timeout=30.0, max_retries=1)
                self._llm_model = kc["model"]
                # ★ 1.7.1: 检测是否为 deepseek 推理模型；若是则禁用 thinking
                # 否则 reasoning_content 会吞掉 token、还把规则文本里的 YES/NO
                # 混进判断，导致 _detect_memorable 永远判 None。
                self._disable_thinking = "deepseek" in (kc["host"] or "").lower() and \
                    ("flash" in (kc["model"] or "").lower() or
                     "reasoner" in (kc["model"] or "").lower() or
                     "r1" in (kc["model"] or "").lower().split("-"))
                _log.debug("[LTM] LLM 客户端已连接: %s model=%s disable_thinking=%s",
                           kc["host"], kc["model"], self._disable_thinking)
            except Exception as e:
                _log.error(f"[LTM] LLM 客户端初始化失败: {e}")
                return None
        return self._llm_client

    # ---- 公共接口 ----

    def get_name(self) -> str:
        return self.TOOL_NAME

    def get_description(self) -> str:
        return self.TOOL_DESCRIPTION

    def set_disabled(self, disabled: bool):
        """设置禁用状态（由 ToolManager 策略变更触发）"""
        was = self._disabled
        self._disabled = disabled
        if disabled and not was:
            _log.info("[LTM] 已禁用")
            self.shutdown()
        elif not disabled and was:
            _log.info("[LTM] 已启用")

    def is_disabled(self) -> bool:
        return self._disabled

    def get_function_def(self) -> dict:
        """OpenAI function calling 定义"""
        return {
            "type": "function",
            "function": {
                "name": self.TOOL_NAME,
                "description": self.TOOL_DESCRIPTION,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "text": {
                            "type": "string",
                            "description": "需要分析并提取长期记忆的文本内容",
                        },
                    },
                    "required": ["text"],
                },
            }
        }

    def execute(self, text: str = "", **kwargs) -> str:
        """
        执行长期记忆写入流程。
        返回：操作结果描述。
        - 禁用时返回静默结果，不报错
        """
        if self._disabled:
            _log.debug("[LTM] 跳过（已禁用）")
            return "[长期记忆] 功能已禁用。"

        if not text or len(text.strip()) < 10:
            return "文本过短，未提取长期记忆。"

        self._touch()
        _log.info(f"[LTM] 收到分析请求: {text[:80]}...")

        # Step 1: LLM YES/NO 判断
        is_memorable = self._detect_memorable(text)
        if is_memorable is None:
            return "[长期记忆] LLM 不可用，放弃本批次。（数据已回退，不污染记忆库）"
        if not is_memorable:
            _log.debug("[LTM] LLM 判断: 无长期记忆价值")
            return "分析完成：当前文本无需要长期记忆的信息。"

        # Step 2: LLM 结构化提取
        extracted = self._extract_memories(text)
        if extracted is None:
            return "[长期记忆] LLM 提取失败，放弃本批次。（数据已回退）"
        if not extracted:
            return "分析完成：未提取到结构化记忆。"

        _log.info(f"[LTM] 提取到 {len(extracted)} 条记忆")

        # Step 3: 去重检查 + 冲突打包（读 DB，可并行）
        try:
            from src.core.database import get_db
            db = get_db()
        except Exception as e:
            _log.error(f"[LTM] 数据库不可用: {e}")
            return f"[长期记忆] 数据库不可用: {e}"

        no_conflict = []     # 无冲突 → 直接 INSERT
        conflicts = []       # 有冲突 → LLM 合并包

        for mem in extracted:
            vn = mem["variable_name"]

            # 精确匹配（读操作，不加锁）
            existing = db.ltm.get(vn)
            if existing:
                conflicts.append({
                    "variable_name": vn,
                    "old_value": existing["value"],
                    "new_value": mem["value"],
                    "old_confidence": existing.get("confidence", 0.5),
                    "new_confidence": mem["confidence"],
                    "old_category": existing.get("category", "other"),
                    "new_category": mem["category"],
                })
                continue

            # 弱匹配（difflib 相似度 > 0.7）
            fuzzy_matches = db.ltm.search_fuzzy(vn, threshold=0.7)
            if fuzzy_matches:
                best = fuzzy_matches[0]
                conflicts.append({
                    "variable_name": best["variable_name"],
                    "old_value": best["value"],
                    "new_value": mem["value"],
                    "old_confidence": best.get("confidence", 0.5),
                    "new_confidence": mem["confidence"],
                    "old_category": best.get("category", "other"),
                    "new_category": mem["category"],
                    "_new_vn": vn,  # 新提议的变量名
                })
                continue

            # 无冲突
            no_conflict.append(mem)

        # Step 4: 无冲突 → 直接入库（写操作，加锁）
        written = 0
        with self._db_mutex:
            for mem in no_conflict:
                try:
                    db.ltm.upsert(
                        variable_name=mem["variable_name"],
                        value=mem["value"],
                        category=mem["category"],
                        confidence=mem["confidence"],
                        source="chat")
                    written += 1
                    _log.debug(f"[LTM] 新记忆: {mem['variable_name']}={mem['value'][:50]}")
                except Exception as e:
                    _log.error(f"[LTM] 写入失败 {mem['variable_name']}: {e}")

        # Step 5: 冲突 → LLM 合并决策
        if conflicts:
            merged = self._merge_conflicts(conflicts)
            if merged is None:
                _log.warning(f"[LTM] 冲突合并 LLM 不可用，放弃 {len(conflicts)} 条冲突")
            else:
                with self._db_mutex:
                    for decision in merged:
                        try:
                            vn = decision["variable_name"]
                            action = decision["action"]
                            if action == "keep_old":
                                _log.debug(f"[LTM] 冲突 {vn}: 保留旧值")
                                continue
                            if action == "keep_new":
                                # ★ 1.8.10: keep_new 的语义是"采用新值"，
                                #   必须真的写回去。原来这一支只把 vn 换成
                                #   _new_vn 就结束 —— 因为 elif 链一旦命中就不会
                                #   再往下走，upsert 根本没执行，written 也不 +1，
                                #   这条记忆被静默丢弃（LLM 明明给了决策）。
                                vn = decision.get("_new_vn") or vn
                            # replace / merge / keep_new 都走写回
                            db.ltm.upsert(
                                variable_name=vn,
                                value=decision["value"],
                                category=decision.get("category", "other"),
                                confidence=decision.get("confidence", 0.5),
                                source="chat")
                            written += 1
                            _log.debug(f"[LTM] 冲突合并 {vn}: {action}")
                        except Exception as e:
                            _log.error(f"[LTM] 冲突写入失败 {vn}: {e}")

        result = f"长期记忆更新完成：新增 {written} 条。"
        if no_conflict:
            result += f" 无冲突直写 {len(no_conflict)} 条。"
        if conflicts:
            result += f" 合并冲突 {len(conflicts)} 条。"
        _log.info(f"[LTM] {result}")

        # ★ 1.7.3: 写后检查是否触发定期总结（按文件大小/条目数定量）
        try:
            self._maybe_summarize()
        except Exception as e:
            _log.debug(f"[LTM] maybe_summarize 跳过: {e}")
        return result

    def execute_async(self, text: str, callback=None):
        """异步执行（在 ThreadPool 中运行），不阻塞调用线程。
        适合监控系统等后台调用场景。
        
        Args:
            text: 要分析的文本
            callback: 可选回调(result: str)
        """
        if self._disabled or not text or len(text.strip()) < 10:
            if callback:
                callback("跳过（禁用或文本过短）")
            return

        self._touch()

        def _run():
            try:
                result = self.execute(text=text)
                if callback:
                    callback(result)
            except Exception as e:
                _log.error(f"[LTM] 异步执行异常: {e}")
                if callback:
                    callback(f"[LTM] 异常: {e}")

        try:
            executor = self._get_executor()
            future = executor.submit(_run)
            self._futures.append(future)
            # 清理已完成的任务
            self._futures = [f for f in self._futures if not f.done()]
        except Exception as e:
            _log.error(f"[LTM] 提交异步任务失败: {e}")

    # ---- 内部方法 ----

    def _maybe_summarize(self):
        """★ 1.7.3: 写后 hook，检查是否触发定期总结。
        - 调用不阻塞本批次
        - 调用不抛异常（内部 try/except）
        - 由工具持有摘要器单例的引用，避免循环 import
        """
        try:
            from tools.ltm_summarizer import get_ltm_summarizer
            sumzr = get_ltm_summarizer()
            sumzr.maybe_trigger()
        except Exception as e:
            _log.debug(f"[LTM] maybe_summarize 内部异常: {e}")

    def _chat(self, messages: list, max_tokens: int, temperature: float):
        """★ 1.7.1: 统一 LLM 调用入口。
        - 对 deepseek 推理模型默认传 thinking=disabled，
          避免 reasoning_content 吞 token、混入判断
        - 其他模型不带 extra_body（兼容 OpenAI 兼容协议）
        """
        kwargs = dict(
            model=self._llm_model,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        if self._disable_thinking:
            kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
        return self._llm_client.chat.completions.create(**kwargs)

    @staticmethod
    def _extract_text(response) -> str:
        """★ 1.7.0: 安全提取 LLM 输出文本。
        兼容推理模型（content 可能为空/被截断，答案在 reasoning_content）。
        1.7.1 改进：thinking 已禁用，正常模型不会写 reasoning_content。
        """
        try:
            msg = response.choices[0].message
        except (AttributeError, IndexError):
            return ""
        content = getattr(msg, "content", None) or ""
        reasoning = getattr(msg, "reasoning_content", None) or ""
        return (content + ("\n" + reasoning if reasoning else "")).strip()

    def _detect_memorable(self, text: str, max_retries: int = 2) -> Optional[bool]:
        """LLM YES/NO 判断文本是否包含值得记忆的信息。返回 True/False/None(失败)"""
        from src.core.prompts import build_ltm_detect_messages

        client = self._get_llm()
        if client is None:
            return None

        messages = build_ltm_detect_messages(text)

        for attempt in range(1, max_retries + 1):
            try:
                response = self._chat(
                    messages=messages,
                    max_tokens=512,  # ★ 1.7.1: 推理模型禁用 thinking 后 512 足够
                    temperature=0.1,
                )
                raw = self._extract_text(response).strip().upper()
                # ★ 1.7.1: 找所有独立 YES/NO 出现位置，取最后一个
                # (?<![A-Z]) 兼容中文/标点紧贴场景（如"结论YES"、"最后：NO"）
                matches = list(re.finditer(r"(?<![A-Z])(YES|NO)(?![A-Z])", raw))
                if matches:
                    return matches[-1].group(1) == "YES"
                _log.warning(f"[LTM] 检测返回异常 (第{attempt}次): {raw[:80]!r}")
                if attempt < max_retries:
                    messages.append({"role": "user",
                                     "content": "不要思考。在最后一行只输出 YES 或 NO。"})
            except Exception as e:
                _log.error(f"[LTM] 检测 LLM 调用失败 (第{attempt}次): {e}")
                if attempt < max_retries:
                    time.sleep(1 * attempt)

        return None

    def _extract_memories(self, text: str, max_retries: int = 2) -> Optional[list[dict]]:
        """LLM 结构化提取长期记忆。返回 list[dict] 或 None(失败)"""
        from src.core.prompts import build_ltm_extract_messages, parse_json_response, validate_ltm_extract

        client = self._get_llm()
        if client is None:
            return None

        messages = build_ltm_extract_messages(text)

        for attempt in range(1, max_retries + 1):
            try:
                response = self._chat(
                    messages=messages,
                    max_tokens=2000,
                    temperature=0.2,
                )
                content = self._extract_text(response)
                data = parse_json_response(content)
                if data is None:
                    _log.warning(f"[LTM] 提取 JSON 解析失败 (第{attempt}次) content[:120]={content[:120]!r}")
                    if attempt < max_retries:
                        messages.append({"role": "user",
                                         "content": "格式错误，请只返回 JSON 数组。"})
                    continue

                validated = validate_ltm_extract(data)
                return validated  # 可能为空列表 []

            except Exception as e:
                _log.error(f"[LTM] 提取 LLM 调用失败 (第{attempt}次): {e}")
                if attempt < max_retries:
                    time.sleep(1 * attempt)

        return None

    def _merge_conflicts(self, conflicts: list[dict], max_retries: int = 2) -> Optional[list[dict]]:
        """LLM 合并冲突决策。返回 list[dict] 或 None(失败)"""
        from src.core.prompts import build_ltm_merge_messages, parse_json_response, validate_ltm_merge

        client = self._get_llm()
        if client is None:
            return None

        messages = build_ltm_merge_messages(conflicts)

        for attempt in range(1, max_retries + 1):
            try:
                response = self._chat(
                    messages=messages,
                    max_tokens=1500,
                    temperature=0.2,
                )
                content = self._extract_text(response)
                data = parse_json_response(content)
                if data is None:
                    _log.warning(f"[LTM] 合并 JSON 解析失败 (第{attempt}次) content[:120]={content[:120]!r}")
                    if attempt < max_retries:
                        messages.append({"role": "user",
                                         "content": "格式错误，请只返回 JSON 数组。"})
                    continue

                validated = validate_ltm_merge(data)
                # 将 _new_vn 信息回填
                for decision in validated:
                    for orig_conflict in conflicts:
                        if orig_conflict.get("_new_vn") and \
                           decision["variable_name"] == orig_conflict["variable_name"]:
                            decision["_new_vn"] = orig_conflict["_new_vn"]
                            break
                return validated

            except Exception as e:
                _log.error(f"[LTM] 合并 LLM 调用失败 (第{attempt}次): {e}")
                if attempt < max_retries:
                    time.sleep(1 * attempt)

        return None


# ============================================================================
# 全局单例
# ============================================================================
_tool_instance: Optional[LongTermMemoryTool] = None
_tool_lock = threading.Lock()


def get_long_term_memory_tool() -> LongTermMemoryTool:
    global _tool_instance
    with _tool_lock:
        if _tool_instance is None:
            _tool_instance = LongTermMemoryTool()
        return _tool_instance


# ============================================================================
# 模块级接口（ToolManager 自动扫描用）
# ============================================================================
# 注意：ToolManager 扫描时会导入此模块，这里不做重量初始化
TOOL_NAME = "long_term_memory"
TOOL_DESCRIPTION = (
    "管理长期记忆 — 记住用户偏好、习惯、身份、项目等信息。"
    "输入要分析的文字，输出记忆存储结果。"
)
TOOL_PARAMETERS = {
    "type": "object",
    "properties": {
        "text": {
            "type": "string",
            "description": "需要分析并提取长期记忆的文本内容（聊天对话、监控事件等）",
        },
    },
    "required": ["text"],
}


def execute(text: str = "", **_) -> str:
    """模块级 execute，供 ToolManager 调用"""
    ltm = get_long_term_memory_tool()
    if ltm.is_disabled():
        return "[长期记忆] 功能已禁用。"
    return ltm.execute(text=text)
