"""
长期记忆定期总结器 v1.0（1.7.3 新增）
================================================================================
职责：
  1. 按 long_term_memories.db 文件大小（KB）/ 条目数定量触发总结
  2. 衰减清理：对 last_accessed 超过 N 天的条目按比例扣 confidence，
     ≤ 阈值时删除（防止长期记忆无限堆积）
  3. 聚类合并：用 difflib 找相似变量名（≥0.75），对每个 cluster 调 LLM
     合并为单条，去重冗余
  4. 仿 DataPipeline 模式：daemon thread + Event.wait + 手动 trigger_now()

触发条件（任一满足即触发，距上次 summarize ≥ cooldown）：
  - DB 文件大小 ≥ config.ltm.file_size_threshold_kb
  - 条目数 ≥ config.ltm.count_threshold

所有 LLM 调用经 LongTermMemoryTool._chat() 统一入口，兼容 deepseek 推理模型。

严格规则：
  - summarize 失败不得阻塞主流程（catch 吞掉异常、记 log）
  - 不删除访问活跃的条目（last_accessed 在 decay_idle_days 内）
  - 聚类合并：cluster size ≥2 才触发 LLM，单条跳过
================================================================================
"""
import logging
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from concurrent.futures import ThreadPoolExecutor

_log = logging.getLogger("LTMSummarizer")


# ------------------------------------------------------------------------
# DB 文件位置（与 src/core/database.py 同源）
# ------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent.parent
LTM_DB_PATH = BASE_DIR / "data" / "DBs" / "long_term_memories.db"


class LTMSummarizer:
    """长期记忆定期总结器"""

    def __init__(self):
        # 延迟读 config（避免循环 import）
        from src.utils.config import get_config
        cfg = get_config().ltm

        self._enabled = cfg.enabled
        self._size_threshold_kb = cfg.file_size_threshold_kb
        self._count_threshold = cfg.count_threshold
        self._cooldown_seconds = cfg.cooldown_minutes * 60
        self._decay_idle_days = cfg.decay_idle_days
        self._decay_rate = cfg.decay_rate_per_period
        self._delete_threshold = cfg.delete_threshold
        self._cluster_similarity = cfg.cluster_similarity
        self._run_on_start = cfg.run_on_start

        self._last_summarize_time: float = 0.0
        self._running: bool = False
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._executor: Optional[ThreadPoolExecutor] = None
        self._executor_lock = threading.Lock()

        _log.info(
            f"[LTMSummarizer] 初始化: enabled={self._enabled}, "
            f"size≥{self._size_threshold_kb}KB | count≥{self._count_threshold} | "
            f"cooldown={cfg.cooldown_minutes}min | 衰减={self._decay_idle_days}天扣{self._decay_rate} | "
            f"删≤{self._delete_threshold} | 聚类≥{self._cluster_similarity}")

    # -------- 生命周期 --------

    def start(self):
        if self._running or not self._enabled:
            return
        self._running = True
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True,
                                        name="ltm-summarizer")
        self._thread.start()
        _log.info("[LTMSummarizer] 已启动")

        # ★ 启动时跑一次，把上次遗留的脏数据处理掉
        if self._run_on_start:
            try:
                self.trigger_now()
            except Exception as e:
                _log.debug(f"[LTMSummarizer] 启动时 summarize 跳过: {e}")

    def stop(self):
        if not self._running:
            return
        self._running = False
        self._stop_event.set()
        if self._executor:
            with self._executor_lock:
                self._executor.shutdown(wait=False)
                self._executor = None
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3)
        _log.info("[LTMSummarizer] 已停止")

    def is_enabled(self) -> bool:
        return self._enabled

    def _get_executor(self) -> ThreadPoolExecutor:
        with self._executor_lock:
            if self._executor is None:
                self._executor = ThreadPoolExecutor(
                    max_workers=1, thread_name_prefix="ltm-summarizer")
            return self._executor

    # -------- 触发入口 --------

    def maybe_trigger(self):
        """★ 写后 hook：LTM 工具 execute() 内调用。

        检查 DB 大小/条目数是否达到阈值；达到且冷却期过 → 异步提交 summarize。
        不阻塞调用线程、不抛异常。
        """
        if not self._enabled or not self._running:
            return

        # 冷却期
        elapsed = time.time() - self._last_summarize_time
        if elapsed < self._cooldown_seconds:
            return

        # 定量检查
        triggered, reason = self._check_thresholds()
        if not triggered:
            return

        _log.info(f"[LTMSummarizer] 触发 summarize (原因: {reason})")
        try:
            executor = self._get_executor()
            executor.submit(self._run_summarize, reason)
        except Exception as e:
            _log.debug(f"[LTMSummarizer] 提交失败: {e}")

    def trigger_now(self) -> str:
        """手动触发一次 summarize（同步），返回结果摘要。"""
        if not self._enabled:
            return "[LTMSummarizer] 已禁用"
        # 强制覆盖冷却期
        self._last_summarize_time = 0.0
        reason = "manual"
        result = self._run_summarize(reason)
        return result or ""

    # -------- 主循环（监听 stop）--------

    def _run_loop(self):
        """空跑守护线程：仅负责 stop 信号响应。
        主要工作由 maybe_trigger / trigger_now 提交到 executor。
        每 60s 检查一次数据库大小（兜底，防止写后 hook 漏触发）。
        """
        _log.info("[LTMSummarizer] 守护线程启动")
        while self._running:
            triggered = self._stop_event.wait(timeout=60)
            if triggered:
                break
            # 兜底：定时巡检
            try:
                self.maybe_trigger()
            except Exception as e:
                _log.debug(f"[LTMSummarizer] 巡检异常: {e}")

    # -------- 检查阈值 --------

    def _check_thresholds(self) -> tuple[bool, str]:
        """返回 (是否触发, 原因)。"""
        try:
            size_bytes = self._db_size_bytes()
            size_kb = size_bytes / 1024.0
            count = self._count()
        except Exception:
            return False, "error"

        if size_kb >= self._size_threshold_kb:
            return True, f"db {size_kb:.1f}KB ≥ {self._size_threshold_kb}KB"
        if count >= self._count_threshold:
            return True, f"count {count} ≥ {self._count_threshold}"
        return False, "below"

    @staticmethod
    def _db_size_bytes() -> int:
        if LTM_DB_PATH.exists():
            return LTM_DB_PATH.stat().st_size
        return 0

    @staticmethod
    def _count() -> int:
        from src.core.database import get_db
        return get_db().ltm.count()

    # -------- 主流程 --------

    def _run_summarize(self, reason: str = "auto") -> str:
        """summarize 完整流程：衰减 + 清理 + 聚类 + 合并。
        返回结果摘要字符串。
        """
        started = time.time()
        self._last_summarize_time = started
        _log.info(f"[LTMSummarizer] ========== 开始 summarize ({reason}) ==========")

        # 1) 衰减 + 清理
        decay = self._decay_step()

        # 2) 聚类 + 合并（需要 LLM）
        cluster = self._cluster_step()

        elapsed = time.time() - started
        summary = (f"summarize 完成 ({reason}, {elapsed:.2f}s) "
                   f"衰减扣分 {decay.get('decayed', 0)} / 删 {decay.get('deleted', 0)} | "
                   f"聚类 {cluster.get('clusters', 0)} 组 / 合并 {cluster.get('merged', 0)} 条")
        _log.info(f"[LTMSummarizer] {summary}")
        return summary

    # -------- 衰减步 --------

    def _decay_step(self) -> dict:
        """对 last_accessed ≥ decay_idle_days 的条目按比率扣 confidence，
        扣到 ≤ delete_threshold 的条目删除。
        """
        from src.core.database import get_db
        db = get_db()

        all_mems = db.ltm.get_all_for_decay()
        if not all_mems:
            return {"decayed": 0, "deleted": 0}

        now = datetime.now()
        decayed = 0
        to_delete_names: list[str] = []

        for mem in all_mems:
            # last_accessed 为空时用 updated_at
            ts_str = mem.get("last_accessed") or mem.get("updated_at") or mem.get("created_at")
            try:
                ts = datetime.strptime(str(ts_str)[:19], "%Y-%m-%d %H:%M:%S")
            except Exception:
                continue
            days_idle = (now - ts).days
            if days_idle < self._decay_idle_days:
                continue

            periods = days_idle / max(1, self._decay_idle_days)
            old_conf = float(mem.get("confidence") or 0.5)
            new_conf = max(0.0, old_conf - periods * self._decay_rate)

            if new_conf <= self._delete_threshold:
                to_delete_names.append(mem["variable_name"])
            elif new_conf < old_conf - 0.001:
                try:
                    db.ltm.update_confidence(mem["variable_name"], new_conf)
                    decayed += 1
                except Exception as e:
                    _log.warning(f"[LTMSummarizer] 扣分失败 {mem['variable_name']}: {e}")

        deleted = 0
        if to_delete_names:
            # 逐条删除（每条都同步 FTS）
            for vn in to_delete_names:
                try:
                    db.ltm.delete(vn)
                    deleted += 1
                except Exception as e:
                    _log.warning(f"[LTMSummarizer] 删除失败 {vn}: {e}")

        if decayed or deleted:
            _log.info(f"[LTMSummarizer] 衰减: 扣分 {decayed} 条, 删除 {deleted} 条 "
                      f"(阈值 {self._decay_idle_days} 天 / rate {self._decay_rate})")
        return {"decayed": decayed, "deleted": deleted}

    # -------- 聚类合并步 --------

    def _cluster_step(self) -> dict:
        """对所有 LTM 条目做 difflib 聚类；对 cluster≥2 调 LLM 合并。"""
        from src.core.database import get_db
        db = get_db()

        all_mems = db.ltm.get_all_for_decay()
        if len(all_mems) < 2:
            return {"clusters": 0, "merged": 0}

        # 1) 用并查集聚类（O(N²) 但 N 通常 < 50，无所谓）
        clusters = self._build_clusters(all_mems)

        # 2) 过滤掉 size=1 的 cluster，只对 ≥2 的做合并
        multi_clusters = [c for c in clusters if len(c) >= 2]
        if not multi_clusters:
            return {"clusters": 0, "merged": 0}

        # 3) 对每个 cluster 调 LLM
        merged_total = 0
        for cluster in multi_clusters:
            try:
                kept = self._merge_one_cluster(db, cluster)
                if kept:
                    merged_total += 1
            except Exception as e:
                _log.warning(f"[LTMSummarizer] cluster 合并失败 (size={len(cluster)}): {e}")

        return {"clusters": len(multi_clusters), "merged": merged_total}

    def _build_clusters(self, mems: list[dict]) -> list[list[dict]]:
        """用 difflib 对 variable_name 做单链聚类 (similarity ≥ threshold)。"""
        import difflib

        parent = list(range(len(mems)))

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(x, y):
            px, py = find(x), find(y)
            if px != py:
                parent[px] = py

        names = [str(m["variable_name"]) for m in mems]
        for i in range(len(mems)):
            for j in range(i + 1, len(mems)):
                ratio = difflib.SequenceMatcher(
                    None, names[i].lower(), names[j].lower()).ratio()
                if ratio >= self._cluster_similarity:
                    union(i, j)

        groups: dict[int, list[int]] = {}
        for i in range(len(mems)):
            r = find(i)
            groups.setdefault(r, []).append(i)

        return [[mems[i] for i in idxs] for idxs in groups.values()]

    def _merge_one_cluster(self, db, cluster: list[dict]) -> Optional[str]:
        """调 LLM 把 cluster 内多条合并为 1 条；upsert 入库 + 删除源条目。
        复用 LongTermMemoryTool._chat() 入口（兼容 deepseek thinking 禁用）。
        """
        from tools.long_term_memory_tool import get_long_term_memory_tool
        ltm_tool = get_long_term_memory_tool()

        if not ltm_tool._get_llm():
            _log.debug("[LTMSummarizer] LLM 不可用，跳过 cluster 合并")
            return None

        # 构造 prompt
        lines = []
        for i, m in enumerate(cluster, 1):
            lines.append(
                f"{i}. variable_name={m['variable_name']!r}, "
                f"value={m['value']!r}, "
                f"category={m.get('category', 'other')}, "
                f"confidence={float(m.get('confidence', 0.5)):.2f}")

        cluster_text = "\n".join(lines)
        user_msg = (
            "你是长期记忆管理助手。以下多条记忆表达含义相近或相互冗余。"
            "请合并为 1 条更精炼的版本，保留核心信息，confidence 取平均值并适当上浮（高质记忆加分）。\n"
            "严格规则：不要思考、不要解释、不要 markdown。只返回一个 JSON 对象，格式：\n"
            '{"variable_name": "...", "value": "...", "category": "...", "confidence": 0.0-1.0}\n\n'
            f"待合并记忆：\n{cluster_text}"
        )
        messages = [
            {"role": "system", "content": "你是桌宠长期记忆的合并助理。"},
            {"role": "user", "content": user_msg},
        ]

        try:
            response = ltm_tool._chat(
                messages=messages, max_tokens=500, temperature=0.2)
            from tools.long_term_memory_tool import LongTermMemoryTool
            content = LongTermMemoryTool._extract_text(response)
        except Exception as e:
            _log.warning(f"[LTMSummarizer] cluster LLM 调用失败: {e}")
            return None

        # 解析 JSON
        from src.core.prompts import parse_json_response
        data = parse_json_response(content)
        if not isinstance(data, dict):
            _log.debug(f"[LTMSummarizer] cluster JSON 解析失败: {content[:120]!r}")
            return None

        # 字段校验
        new_vn = str(data.get("variable_name") or "").strip()
        new_val = str(data.get("value") or "").strip()
        new_cat = str(data.get("category") or "other").strip()
        try:
            new_conf = float(data.get("confidence", 0.7))
        except Exception:
            new_conf = 0.7
        if not new_vn or not new_val:
            return None

        # upsert + 删源
        try:
            db.ltm.upsert(
                variable_name=new_vn, value=new_val,
                category=new_cat, confidence=new_conf, source="summarize")
        except Exception as e:
            _log.warning(f"[LTMSummarizer] cluster 写入失败: {e}")
            return None

        for m in cluster:
            if m["variable_name"] != new_vn:
                try:
                    db.ltm.delete(m["variable_name"])
                except Exception:
                    pass

        _log.info(f"[LTMSummarizer] cluster 合并: {len(cluster)} → 1 "
                  f"({new_vn}, conf={new_conf:.2f})")
        return new_vn


# ============================================================================
# 全局单例
# ============================================================================
_summarizer_instance: Optional[LTMSummarizer] = None
_summarizer_lock = threading.Lock()


def get_ltm_summarizer() -> LTMSummarizer:
    global _summarizer_instance
    with _summarizer_lock:
        if _summarizer_instance is None:
            _summarizer_instance = LTMSummarizer()
        return _summarizer_instance
