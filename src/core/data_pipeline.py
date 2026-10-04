"""
数据清洗 Pipeline（完整实现）
================================================================================
职责：
  1. 双触发：定时（Event.wait）+ 定量（write_counter >= trigger_count）
  2. 机械清洗规则：过滤无意义数据
  3. SQL 时间过滤：60s + 100id 差异，一步查询
  4. LLM 总结：打包 → JSON 结构化事件 → 写入 monitoring_events
  5. 重试 3 次 + 失败回退 cleaned=0
  6. 不降级、不纯机械模式

触发条件：
  - 定时：每 N 分钟（config.monitor.summarize_interval_minutes）
  - 定量：raw_events 写入计数器达到 N 条（config.monitor.summarize_trigger_count）
================================================================================
"""
import logging
import re
import threading
import time
from datetime import datetime
from typing import Optional

_log = logging.getLogger("DataPipeline")


class DataPipeline:
    """数据清洗流水线"""

    def __init__(self):
        import sys
        from pathlib import Path
        BASE_DIR = Path(__file__).resolve().parent.parent.parent
        sys.path.insert(0, str(BASE_DIR))

        from src.utils.config import get_config
        cfg = get_config().monitor
        self._interval_seconds = cfg.summarize_interval_minutes * 60
        self._trigger_count = cfg.summarize_trigger_count

        # ★ 1.8.9: 大力清洗参数（积压 > 阈值时一次多吃点，少跑几次 LLM）
        pcfg = get_config().pipeline
        self._bulk_threshold = int(getattr(pcfg, "bulk_threshold", 500) or 500)
        self._bulk_batch_size = int(getattr(pcfg, "bulk_batch_size", 500) or 500)
        self._bulk_max_tokens = int(getattr(pcfg, "bulk_max_tokens", 4000) or 4000)

        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._process_event = threading.Event()  # 外部触发立即清洗
        # ★ 1.7.0: 正在喂给长期记忆的 monitoring_events id（防止异步在途任务被重复投喂）
        self._pending_memory_ids: set = set()

        _log.info(f"[DataPipeline] 初始化: 定时={cfg.summarize_interval_minutes}min, 定量={self._trigger_count}条")

    def start(self):
        if self._running:
            return
        self._running = True
        self._stop_event.clear()
        self._process_event.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="pipeline")
        self._thread.start()
        _log.info("[DataPipeline] 启动")

    def stop(self):
        self._running = False
        self._stop_event.set()
        self._process_event.set()  # 唤醒等待中的线程
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        _log.info("[DataPipeline] 已停止")

    def trigger_now(self):
        """外部调用，立即触发一次清洗"""
        _log.debug("[DataPipeline] 收到外部触发")
        self._process_event.set()

    # ---- 主循环 ----

    def _run_loop(self):
        """双触发主循环：Event.wait(timeout) 或被 trigger_now() / write_counter 触发"""
        _log.info(f"[DataPipeline] 主循环启动: 定时={self._interval_seconds}s, 定量={self._trigger_count}条")
        # ★ 1.8.7: 把"什么时候才会干活"直接写进日志，省得以为它没在工作。
        # ★ 1.8.9: 用 count_uncleaned() 取真实总数（以前被 limit 截断，永远显示 100）。
        try:
            from src.core.database import get_db
            _bk = get_db().raw.count_uncleaned()
            _log.info(f"[DataPipeline] 触发条件: 每 {self._interval_seconds//60} 分钟一次；"
                      f"积压 ≥ {self._trigger_count} 条；"
                      f"积压 > {self._bulk_threshold} 条时大力清洗"
                      f"（单批 {self._bulk_batch_size} 条）"
                      f"；当前待清洗积压 {_bk} 条")
        except Exception as e:
            _log.debug(f"[DataPipeline] 积压统计失败: {e}")
        # ★ 1.7.0: 启动时先消化上次运行遗留的待记忆事件（memory_ref=0）
        try:
            from src.core.database import get_db
            self._feed_long_term_memory(get_db())
        except Exception as e:
            _log.debug(f"[DataPipeline] 启动遗留记忆消化失败: {e}")
        while self._running:
            try:
                # 等待：超时 OR 外部触发
                triggered = self._process_event.wait(timeout=60)  # 每 60s 检查一次

                # ★ 1.8.6: 被 stop() 唤醒时 _running 已经为 False ——
                #   必须在这里就退出，否则会「退出过程中反而开一个新批次」。
                #   实测日志（logs/prts_20260915_211608.log）：
                #     21:18:41 正在退出...
                #     21:18:46 [DataPipeline] === 开始清洗批次 ===   ← 退出中还在跑
                #     21:18:46 数据库已关闭
                #   根因：stop() 用 _process_event.set() 唤醒线程，
                #   而 while 判断在 wait 之前就已经通过，醒来后照样往下走。
                if not self._running:
                    break

                # 检查定量触发
                count_triggered = False
                backlog = 0
                new_count = 0
                db_ok = False
                bulk = False
                batch_size = self._trigger_count
                try:
                    from src.core.database import get_db
                    db = get_db()
                    new_count = db.raw.write_counter
                    if new_count >= self._trigger_count:
                        count_triggered = True
                        db.raw.write_counter = 0  # 归零
                    # ★ 1.8.9: 用**真实总数**，不再用 len(get_uncleaned(100))
                    #   —— 那个永远显示 100，看着像"怎么清都清不完"。
                    backlog = db.raw.count_uncleaned()

                    # ★ 1.8.9: 大力清洗 —— 积压超过阈值就一次多吃点。
                    if backlog > self._bulk_threshold:
                        bulk = True
                        batch_size = self._bulk_batch_size
                        count_triggered = True
                    elif backlog >= self._trigger_count:
                        # ★ 1.8.7: 常规积压触发
                        count_triggered = True
                    db_ok = True
                except Exception as e:
                    _log.debug(f"[DataPipeline] 触发条件检查失败: {e}")

                should_time = self._should_trigger_by_time()
                # ★ 1.8.7: 心跳 —— 否则启动后到第一次触发之间日志一片空白，
                #   根本看不出 Pipeline 是活着还是死了。
                _log.debug(
                    f"[DataPipeline] 心跳: 新增={new_count}/{self._trigger_count}, "
                    f"积压={backlog}, 单批={batch_size}, "
                    f"定时{'已到' if should_time else '未到'}, "
                    f"db={'OK' if db_ok else '不可用'}")

                if triggered or count_triggered or should_time:
                    # 重置事件
                    self._process_event.clear()
                    # 执行清洗
                    if bulk:
                        _log.info(f"[DataPipeline] 积压 {backlog} 条 > "
                                  f"{self._bulk_threshold} → 大力清洗"
                                  f"（单批 {batch_size} 条）")
                    else:
                        _log.info(f"[DataPipeline] 触发清洗（外部={bool(triggered)}, "
                                  f"积压/新增达标={count_triggered}, 定时={should_time}, "
                                  f"当前积压={backlog}）")
                    self._process_batch(batch_size)

            except Exception as e:
                _log.error(f"[DataPipeline] 主循环异常: {e}", exc_info=True)
                time.sleep(30)

    def _should_trigger_by_time(self) -> bool:
        """检查是否到了定时触发时间"""
        if not hasattr(self, '_last_run'):
            self._last_run = time.time()
            return False  # 刚启动不立即触发

        elapsed = time.time() - self._last_run
        return elapsed >= self._interval_seconds

    # ---- 核心处理 ----

    def _process_batch(self, batch_size: int = None):
        """执行一批数据的清洗流水线

        ★ 1.8.9: batch_size 由调用方决定 —— 积压超过 bulk_threshold 时
        走"大力清洗"，一次取 bulk_batch_size 条，少跑几次 LLM。
        """
        # ★ 1.8.6: 退出中就别开新批次了（避免 DB 已关还在写 / 白调一次 LLM）
        if not self._running:
            _log.info("[DataPipeline] 正在退出，跳过本次批次")
            return
        batch_size = int(batch_size or self._trigger_count)
        self._last_run = time.time()
        _log.info(f"[DataPipeline] === 开始清洗批次（单批 {batch_size} 条）===")

        try:
            from src.core.database import get_db
            db = get_db()
        except Exception as e:
            _log.error(f"[DataPipeline] 数据库不可用: {e}")
            return

        # Step 1: 获取未清洗数据（SQL 时间+ID 一步过滤）
        raw_events = self._fetch_uncleaned(db, limit=batch_size)
        if not raw_events:
            _log.debug("[DataPipeline] 无待清洗数据")
            return

        _log.info(f"[DataPipeline] 获取 {len(raw_events)} 条原始数据")

        # Step 2: 机械清洗
        cleaned = self._mechanical_clean(raw_events)
        if not cleaned:
            _log.info("[DataPipeline] 机械清洗后无有效数据")
            # 标记所有为已清洗（无价值数据）
            db.raw.mark_cleaned([r["id"] for r in raw_events], 1)
            return

        _log.info(f"[DataPipeline] 机械清洗后保留 {len(cleaned)} 条")

        # ★ 1.8.8: 重复合并（在送 LLM 之前，能省多少 token 一目了然）
        before_dedup = len(cleaned)
        cleaned = self._dedup_events(cleaned)
        if len(cleaned) < before_dedup:
            _log.info(f"[DataPipeline] 重复合并: {before_dedup}→{len(cleaned)} 条 "
                      f"(合并掉 {before_dedup - len(cleaned)} 条重复)")

        # Step 3: 处理截图/音频关联（检查 file_path，标记待视觉/STT 处理）
        cleaned = self._enrich_file_paths(cleaned)

        # Step 4: LLM 总结（带重试）
        # ★ 1.8.6: 退出中不调 LLM（一次总结可能几十秒，会把退出卡住）
        if not self._running:
            _log.info("[DataPipeline] 正在退出，跳过 LLM 总结")
            return
        # ★ 1.8.9: 输出上限跟着输入条数走 —— 大力清洗时 500 条原始数据
        #   合并后约 175 条进 prompt，输出事件数会明显变多，
        #   固定 max_tokens=2000 有被截断（JSON 解析失败）的风险。
        max_out = max(2000, min(self._bulk_max_tokens,
                                int(1200 + len(cleaned) * 12)))
        events = self._llm_summarize_with_retry(cleaned, db, max_tokens=max_out)

        # ★ 1.8.6: LLM 期间可能已经点了退出 → 别再写库了（此时 DB 可能已关）
        if not self._running:
            _log.info("[DataPipeline] 正在退出，跳过结果写入")
            return

        # Step 5: 写入结果 + 标记
        if events:
            written = 0
            for evt in events:
                try:
                    db.raw.add_event(
                        time_start=evt.get("time_start"),
                        time_end=evt.get("time_end"),
                        event=evt.get("event", ""),
                        detail=evt.get("detail", ""),
                        confidence=evt.get("confidence", 100),
                        source="pipeline")
                    written += 1
                except Exception as e:
                    _log.error(f"[DataPipeline] 写入 monitoring_event 失败: {e}")
            _log.info(f"[DataPipeline] 写入 {written}/{len(events)} 条事件")

        # Step 6: 标记 raw_events 为已清洗
        db.raw.mark_cleaned([r["id"] for r in raw_events], 1)
        _log.info(f"[DataPipeline] === 批次完成: {len(raw_events)}→{len(events) if events else 0} ===")

        # Step 7: ★ 1.7.0 monitoring_events → 长期记忆（异步，不阻塞本批次）
        self._feed_long_term_memory(db)

    # ---- ★ 1.7.0: 监控事件 → 长期记忆 ----

    def _feed_long_term_memory(self, db):
        """把 memory_ref=0 的 monitoring_events 喂给长期记忆工具（异步）。

        - LLM 判断/提取/合并全部在 LongTermMemoryTool 内完成
        - 完成回调里标记 memory_ref：1=已处理, -1=失败（数据保留待查）
        - 工具禁用时直接标记 1（明确不处理，避免堆积）
        - 在途 id 用 _pending_memory_ids 去重
        """
        try:
            events = db.raw.get_unprocessed_events(limit=20)
        except Exception as e:
            _log.debug(f"[DataPipeline] 取待记忆事件失败: {e}")
            return

        events = [e for e in events if e["id"] not in self._pending_memory_ids]
        if not events:
            return

        try:
            from tools.long_term_memory_tool import get_long_term_memory_tool
            ltm = get_long_term_memory_tool()
        except Exception as e:
            _log.debug(f"[DataPipeline] LTM 工具不可用: {e}")
            return

        ids = [e["id"] for e in events]

        if ltm.is_disabled():
            db.raw.mark_memory_processed(ids, 1)
            return

        text = "\n".join(
            f"[{e.get('time_start') or str(e.get('created_at', ''))[:19]}] "
            f"{e.get('event', '')}: {e.get('detail', '')}"
            for e in events)

        if len(text.strip()) < 10:
            db.raw.mark_memory_processed(ids, 1)
            return

        self._pending_memory_ids.update(ids)

        def _done(result: str):
            self._pending_memory_ids.difference_update(ids)
            try:
                bad = ("不可用" in result) or ("异常" in result) or ("失败" in result)
                db.raw.mark_memory_processed(ids, -1 if bad else 1)
            except Exception:
                pass

        _log.info(f"[DataPipeline] 喂给长期记忆 {len(ids)} 条事件")
        try:
            ltm.execute_async(text, callback=_done)
        except Exception as e:
            _log.error(f"[DataPipeline] LTM 异步投喂失败: {e}")
            self._pending_memory_ids.difference_update(ids)

    def _fetch_uncleaned(self, db, limit: int = 100) -> list[dict]:
        """取最旧的 limit 条未清洗数据，并做「时间/ID 窗口」筛选。

        ★ 1.8.9: 窗口必须跟着 limit 缩放，否则大力清洗（500 条）时，
        老逻辑的 id_diff<=100 会把最旧的一大半直接剔掉 ——
        而这些被剔掉的行**不会被标记 cleaned**，下一批又排在最前面被剔，
        结果就是"永远清不完"。这正是本次要根治的问题。

        窗口放大到与 limit 同量级后，本批取到的行基本都能留下；
        并且额外加了一道「保底推进」：万一真的还有行落在窗口外，
        也不许把它们丢掉（丢掉就会卡死），只记一条 warning。
        """
        try:
            raw = db.raw.get_uncleaned(limit)
            if len(raw) <= 2:
                return raw

            max_row = max(raw, key=lambda r: r["id"])
            max_id = max_row["id"]
            max_time = max_row["created_at"]

            # 窗口随 limit 缩放：100 条 ≈ 100 id ≈ 60s；500 条 ≈ 500 id ≈ 300s
            id_window = max(100, limit)
            time_window = max(60, 60 * (limit // 100 or 1))

            filtered = []
            for r in raw:
                id_diff = max_id - r["id"]
                ok = (id_diff <= id_window)
                try:
                    t1 = datetime.strptime(str(max_time)[:19], "%Y-%m-%d %H:%M:%S")
                    t2 = datetime.strptime(str(r["created_at"])[:19], "%Y-%m-%d %H:%M:%S")
                    ok = ok or (abs((t1 - t2).total_seconds()) <= time_window)
                except Exception:
                    ok = True  # 解析失败不过滤

                if ok:
                    filtered.append(r)

            # ★ 保底推进：窗口外的行如果被丢掉，就永远不会被清洗（死锁）。
            #   宁可多送几条给 LLM，也不能让流程卡死。
            if len(filtered) < len(raw):
                missing = [r for r in raw if r["id"] not in {f["id"] for f in filtered}]
                _log.warning(
                    f"[DataPipeline] 窗口过滤会丢掉 {len(missing)} 条"
                    f"（id_window={id_window}, time_window={time_window}s）"
                    f"—— 为保证推进，已全部保留")
                filtered = raw

            return filtered

        except Exception as e:
            _log.error(f"[DataPipeline] 获取未清洗数据失败: {e}")
            return []

    def _mechanical_clean(self, events: list[dict]) -> list[dict]:
        """机械清洗规则（★ 1.8.8 修好了规则1 的 source 白名单）

        实测（tools/dev_tests/_diag_pipeline_layers.py 跑真实库）：
          规则1 原白名单是 ("process", "system")，而库里真实的 source 是
          "process_metadata"(44%) / "window_metadata"(39%) / "system"(14%)，
          'process_metadata' 根本不匹配 → 规则1 对绝大多数数据形同虚设，
          四条规则一条都不命中，100 条原样送进 LLM。
        """
        import re
        cleaned = []
        for evt in events:
            source = evt.get("source", "")
            content = evt.get("content", "")

            # 规则1: 浏览器/Electron 子进程噪声
            # ★ 1.8.8: 用前缀匹配覆盖 process / process_metadata /
            #   window_metadata / system 这些真实 source 名；
            #   噪声词改成「词边界」匹配，避免窗口标题里出现
            #   audio/storage 这种常见词就被误杀。
            if any(source.startswith(s) for s in self._NOISE_SOURCES) and content:
                if self._BROWSER_NOISE_RE.search(content):
                    _log.debug(f"[DataPipeline] 过滤浏览器子进程: {content[:50]}")
                    continue

            # 规则2: 空内容
            if not content or len(content.strip()) < 3:
                continue

            # 规则3: 纯文件路径（无其他内容）
            content_stripped = content.strip()
            if re.match(r'^[A-Za-z]:\\[^\s]*$', content_stripped) or \
               re.match(r'^/[^\s]*$', content_stripped):
                if len(content_stripped) < 200:
                    continue  # 短纯路径 → 跳过

            # 规则4: 剪贴板 2000 字截断
            if source == "clipboard" and len(content) > 2000:
                evt["content"] = content[:2000]

            cleaned.append(evt)

        return cleaned

    # ─── ★ 1.8.8: 重复合并 ───
    # 真实 source 名（老白名单只有 "process"/"system"，漏掉了 *_metadata）
    _NOISE_SOURCES = ("process", "system", "window")
    # 只认「独立的」噪声词，别把窗口标题里的 audio/storage 误杀
    _BROWSER_NOISE_RE = re.compile(
        r"(?i)(?:^|[\s,(\[（])(?:crash|gpu-process|utility|renderer|"
        r"network-service|video-capture)(?:$|[\s,)\]）:：])")

    @staticmethod
    def _normalize_content(content: str) -> str:
        """把只差数字/时长的事件归一化成同一个 key，用于判定"同一种状态"。

        例："[进程] idea64.exe(1进程, 2749MB)…" 与
            "[进程] idea64.exe(1进程, 2097MB)…" → 同一个 key。
        """
        s = (content or "").strip()
        s = re.sub(r"持续\s*\d+\s*s", "持续Ns", s)
        s = re.sub(r"\d+", "N", s)
        s = re.sub(r"\s+", " ", s)
        s = re.sub(r"N+", "N", s)
        return s

    def _dedup_events(self, events: list[dict]) -> list[dict]:
        """★ 1.8.8: 同一批里「同 source + 同归一化内容」的合并成一条。

        实测这批监控数据 100 条里 65 条是重复的
        （如 "活动应用类别: browser, ide" 一条就出现 44 次）。
        合并策略刻意做成**保留首次出现的位置与时间戳**，
        把末次时间与次数写进正文 —— 这样 LLM 既能知道"这段时间一直是这个状态"，
        又不会丢掉时间跨度信息，比直接丢弃更有用。
        """
        merged: list[dict] = []
        index: dict = {}
        for evt in events:
            key = (evt.get("source", ""),
                   self._normalize_content(evt.get("content", "") or ""))
            if key in index:
                first = merged[index[key]]
                first["_dup_n"] = first.get("_dup_n", 1) + 1
                last = evt.get("created_at") or evt.get("time_start") or ""
                if last:
                    first["_dup_last"] = last
                continue
            index[key] = len(merged)
            merged.append(dict(evt))

        for e in merged:
            n = e.pop("_dup_n", 1)
            last = e.pop("_dup_last", None)
            if n > 1:
                tail = f"，至 {str(last)[:19]}" if last else ""
                e["content"] = f"{e.get('content', '')}（同批 ×{n}{tail}）"
        return merged

    def _enrich_file_paths(self, events: list[dict]) -> list[dict]:
        """为包含 file_path 的事件补充视觉/STT 分析结果（异步队列占位）"""
        # TODO: 下一轮集成视觉/STT queue
        # 目前：标记截图/音频，但不阻塞
        for evt in events:
            fp = evt.get("file_path", "")
            if fp and fp.endswith((".jpg", ".png", ".jpeg")):
                evt["content"] = evt.get("content", "") + " [待视觉分析]"
            elif fp and fp.endswith((".wav", ".mp3")):
                evt["content"] = evt.get("content", "") + " [待STT转写]"
        return events

    def _llm_summarize_with_retry(self, events: list[dict], db,
                                  max_retries: int = 3,
                                  max_tokens: int = 2000) -> list[dict]:
        """LLM 总结 + JSON 验证 + 重试"""
        if not events:
            return []

        from src.core.prompts import build_pipeline_messages, parse_json_response, validate_pipeline_events
        messages = build_pipeline_messages(events)

        # 获取 LLM 客户端（使用全局单例，避免每次批次重复创建）
        try:
            from src.core.llm_client import get_summarizer_client
            client = get_summarizer_client()
        except Exception as e:
            _log.error(f"[DataPipeline] LLM 客户端初始化失败: {e}")
            return []

        for attempt in range(1, max_retries + 1):
            # ★ 1.8.6: 退出中不要继续重试（每次都是几十秒的网络请求）
            if not self._running:
                _log.info("[DataPipeline] 正在退出，中止 LLM 重试")
                break
            try:
                _log.info(f"[DataPipeline] LLM 总结 第{attempt}/{max_retries}次"
                          f"（输入 {len(events)} 条, max_tokens={max_tokens}）")
                response = client.client.chat.completions.create(
                    model=client.model,
                    messages=messages,
                    max_tokens=max_tokens,
                    temperature=0.3,
                )
                content = response.choices[0].message.content

                if not content:
                    _log.error(f"[DataPipeline] LLM 返回空内容 (第{attempt}次)")
                    if attempt < max_retries:
                        continue
                    else:
                        break

                # 解析 JSON
                data = parse_json_response(content)
                if data is None:
                    _log.warning(f"[DataPipeline] JSON 解析失败 (第{attempt}次)，原始输出: {content[:200]}")
                    if attempt < max_retries:
                        # 追加格式纠正指令重试
                        messages.append({
                            "role": "assistant",
                            "content": content[:500],
                        })
                        messages.append({
                            "role": "user",
                            "content": "你的输出格式不正确。请只返回一个 JSON 数组，不要 markdown 代码块，不要解释文字。",
                        })
                        continue
                    else:
                        break

                # 验证事件
                validated = validate_pipeline_events(data)
                if validated:
                    _log.info(f"[DataPipeline] LLM 总结成功: {len(validated)} 条事件")
                    return validated
                else:
                    _log.warning(f"[DataPipeline] 验证后无有效事件 (第{attempt}次)")
                    if attempt < max_retries:
                        messages.append({
                            "role": "assistant",
                            "content": content[:500],
                        })
                        messages.append({
                            "role": "user",
                            "content": "解析结果为空。请确保 JSON 数组中每个元素都包含 event 和 detail 字段。",
                        })
                        continue

            except Exception as e:
                _log.error(f"[DataPipeline] LLM 调用失败 (第{attempt}次): {e}")
                if attempt < max_retries:
                    time.sleep(2 * attempt)  # 退避重试
                continue

        # 全部重试失败 → 标记 cleaned=-1，回退数据
        _log.error(f"[DataPipeline] {max_retries}次重试全部失败，标记 cleaned=-1")
        try:
            db.raw.mark_cleaned([e["id"] for e in events], -1)
        except Exception:
            pass
        return []


# ============================================================================
# 全局单例
# ============================================================================
_pipeline_instance: Optional[DataPipeline] = None


def get_pipeline() -> DataPipeline:
    global _pipeline_instance
    if _pipeline_instance is None:
        _pipeline_instance = DataPipeline()
    return _pipeline_instance
