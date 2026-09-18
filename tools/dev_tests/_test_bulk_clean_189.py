# -*- coding: utf-8 -*-
"""★ 1.8.9 回归：大力清洗（积压 > 500 → 单批 500）。

背景：用户实测"感觉永远都总结完"。
  真实日志 prts_20260915_213404.log：21:35→21:48 连续 13 个批次，
  每分钟一个、每批只吃 100 条，全程 13 分钟不停调 LLM；
  而且心跳里的"积压=100"是被 get_uncleaned(limit=100) 截断的假象
  （实际还有 900+），看着永远清不完。

本次改动：
  · 积压 > bulk_threshold 时单批取 bulk_batch_size 条（默认 500/500）
  · 心跳/启动日志用 count_uncleaned() 显示**真实总数**
  · _fetch_uncleaned 的窗口随 limit 缩放，并加「保底推进」不许丢行
    （老逻辑 id_diff<=100 在 500 条时会把最旧的一大半剔掉且不标记 cleaned
      → 永久卡死）
  · LLM 输出的 max_tokens 跟着输入条数放大，避免 JSON 被截断

用法: python tools/dev_tests/_test_bulk_clean_189.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   {detail}" if detail else ""))


import src.core.database as dbmod  # noqa: E402
from src.core.data_pipeline import DataPipeline  # noqa: E402


class _Raw:
    """假 raw 表：可控积压总数 + 可控 id 跨度（模拟 cap 裁剪出的空洞）"""

    def __init__(self, backlog, id_span=None):
        self.write_counter = 0
        self._backlog = backlog
        self._span = id_span if id_span is not None else backlog

    def count_uncleaned(self):
        return self._backlog

    def get_uncleaned(self, limit=100):
        n = min(self._backlog, limit)
        # id 从 10000 往下排，跨度可控
        step = max(1, self._span // max(1, n))
        return [{"id": 10000 - i * step,
                 "source": "process_metadata",
                 "content": f"活动应用类别: browser, ide #{i}",
                 "created_at": "2026-09-15 21:00:00"} for i in range(n)]


class _DB:
    def __init__(self, backlog, id_span=None):
        self.raw = _Raw(backlog, id_span)


class _FastEvent:
    def __init__(self, max_iter):
        self.calls = 0
        self.max_iter = max_iter
        self._p = None

    def wait(self, timeout=60):
        self.calls += 1
        if self.calls > self.max_iter and self._p is not None:
            self._p._running = False
        return False

    def set(self):
        pass

    def clear(self):
        pass


def _run_one(backlog, id_span=None):
    """跑主循环若干轮，返回 (进程内看到的 batch_size 列表, pipeline)"""
    dbmod.get_db = lambda: _DB(backlog, id_span)
    p = DataPipeline()
    p._feed_long_term_memory = lambda db: None
    p._should_trigger_by_time = lambda: False
    ev = _FastEvent(2)
    ev._p = p
    p._process_event = ev
    seen = []
    p._process_batch = lambda batch_size=None: seen.append(batch_size)
    p._running = True
    p._run_loop()
    return seen, p


_ORIG = dbmod.get_db
try:
    print("== 1. 配置项就位 ==")
    from src.utils.config import PipelineConfig  # noqa: E402
    c = PipelineConfig()
    check("bulk_threshold 默认 500", c.bulk_threshold == 500, str(c.bulk_threshold))
    check("bulk_batch_size 默认 500", c.bulk_batch_size == 500, str(c.bulk_batch_size))
    check("bulk_max_tokens 默认 4000", c.bulk_max_tokens == 4000, str(c.bulk_max_tokens))

    src = (ROOT / "src" / "core" / "data_pipeline.py").read_text(encoding="utf-8")
    check("count_uncleaned 已接入主循环", "db.raw.count_uncleaned()" in src)
    check("心跳里显示真实积压 + 单批大小",
          "积压={backlog}, 单批={batch_size}" in src)
    check("大力清洗有独立日志", "→ 大力清洗" in src)

    print("\n== 2. 积压 > 500 → 单批 500（大力清洗）==")
    seen, p = _run_one(1058)
    print(f"  积压 1058 → 进程内 process_batch 收到的 batch_size = {seen}")
    check("确实走了大力清洗（batch_size=500）", 500 in seen, str(seen))
    check("不是默认的 100", seen and seen[0] == 500, str(seen))

    print("\n== 3. 积压 100~500 → 仍是常规单批 100 ==")
    seen2, _ = _run_one(300)
    print(f"  积压 300 → batch_size = {seen2}")
    check("常规模式仍用 100", seen2 and seen2[0] == 100, str(seen2))

    print("\n== 4. 积压 < 100 且无其他触发 → 不动 ==")
    seen3, _ = _run_one(55)
    check("积压 55 条时不触发", seen3 == [], str(seen3))

    print("\n== 5. 保底推进：id 跨度大时不许丢行（老逻辑会卡死）==")
    # 500 条但 id 跨度 5000 → 老逻辑 id_diff<=100 会把绝大多数剔掉
    p5 = DataPipeline()
    db5 = _DB(500, id_span=5000)
    got = p5._fetch_uncleaned(db5, limit=500)
    check("limit=500、id 跨度 5000 时仍返回 500 条", len(got) == 500,
          f"返回 {len(got)} 条（老逻辑会大量丢弃 → 永久卡住）")
    got100 = p5._fetch_uncleaned(db5, limit=100)
    check("limit=100 时返回 100 条", len(got100) == 100, f"{len(got100)} 条")

    print("\n== 6. 窗口随 limit 缩放 ==")
    check("id_window = max(100, limit)", "id_window = max(100, limit)" in src)
    check("time_window 随 limit 放大", "time_window = max(60, 60 * (limit // 100 or 1))" in src)

    print("\n== 7. _process_batch 把 batch_size 传给取数 ==")
    batch = src.split("def _process_batch", 1)[1].split("def _enrich_file_paths", 1)[0]
    check("_fetch_uncleaned(limit=batch_size)",
          "_fetch_uncleaned(db, limit=batch_size)" in batch)
    check("批次日志写明单批条数", "单批 {batch_size} 条" in batch)

    print("\n== 8. max_tokens 跟着输入放大（避免 JSON 被截断）==")
    check("_llm_summarize_with_retry 接受 max_tokens 参数",
          "max_tokens: int = 2000" in src)
    check("调用处按输入条数计算上限",
          "max_out = max(2000, min(self._bulk_max_tokens" in batch)
    check("不再硬编码 max_tokens=2000", "max_tokens=2000,\n" not in src)

    print("\n== 9. 顺序不变：机械清洗 → 重复合并 → LLM ==")
    i_mech = batch.find("self._mechanical_clean")
    i_dedup = batch.find("self._dedup_events")
    i_llm = batch.find("_llm_summarize_with_retry")
    check("机械清洗 < 重复合并 < LLM", 0 <= i_mech < i_dedup < i_llm,
          f"{i_mech} < {i_dedup} < {i_llm}")

    print("\n== 10. 真实库上跑一遍取数（只读）==")
    dbmod.get_db = _ORIG
    try:
        from src.core.database import get_db
        db = get_db()
        real = db.raw.count_uncleaned()
        print(f"  真实积压 = {real} 条")
        p10 = DataPipeline()
        for lim in (100, 500):
            got = p10._fetch_uncleaned(db, limit=lim)
            print(f"  _fetch_uncleaned(limit={lim}) -> {len(got)} 条")
        check("真实库取数不丢行（返回 = min(积压, limit)）",
              len(p10._fetch_uncleaned(db, limit=500)) == min(real, 500),
              f"积压 {real}")
        db.close()
    except Exception as e:
        print("  跳过（DB 不可用）:", e)
finally:
    dbmod.get_db = _ORIG

print(f"\n==== 结果: {len(PASS)} PASS / {len(FAIL)} FAIL ====")
for f in FAIL:
    print("  FAIL:", f)
sys.exit(1 if FAIL else 0)
