# -*- coding: utf-8 -*-
"""★ 1.8.7 回归：Pipeline 看起来"没在工作" —— 触发条件不可见 + 积压永远等不到触发。

背景（用户实测："又重启了一遍，没看见 pipeline 工作啊"）：
  Pipeline 线程其实活着（日志有"主循环启动/启动/已启动"），
  但触发条件只有两个：每 600s 一次，或内存计数器新增 ≥100 条。
  问题在于 write_counter 是**内存计数器、每次重启归零**，
  而库里躺着 1058 条 cleaned=0 的历史积压 ——
  既不会被"新增 100 条"认到，也只能干等 10 分钟定时，
  表现就是"Pipeline 像死了一样"。

用法: python tools/dev_tests/_test_pipeline_trigger_187.py
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

_ORIG_GET_DB = dbmod.get_db


class _Raw:
    def __init__(self, backlog):
        self.write_counter = 0
        self._backlog = backlog

    def get_uncleaned(self, limit: int = 100):
        return [{"id": i} for i in range(min(self._backlog, limit))]

    def count_uncleaned(self):
        # ★ 1.8.9: 主循环改用真实总数判定积压
        return self._backlog


class _DB:
    def __init__(self, backlog):
        self.raw = _Raw(backlog)


class _FastEvent:
    """wait() 立即返回 False —— 让主循环快速转动，便于断言"""

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


def _make(backlog: int, iterations: int = 3):
    p = DataPipeline()
    p._feed_long_term_memory = lambda db: None
    p._should_trigger_by_time = lambda: False       # 关掉定时触发，只测定量/积压
    ev = _FastEvent(iterations)
    ev._p = p
    p._process_event = ev
    calls = []
    # ★ 1.8.9: _process_batch 现在带 batch_size 参数
    p._process_batch = lambda batch_size=None: calls.append(batch_size)
    p._running = True
    return p, calls


try:
    print("== 1. 有积压（>=阈值）→ 应该触发清洗 ==")
    dbmod.get_db = lambda: _DB(1058)
    p, calls = _make(1058, iterations=2)
    p._run_loop()
    check("积压 1058 条时确实触发了批次", len(calls) >= 1, f"触发 {len(calls)} 次")

    print("\n== 2. 既没积压、也没新增、定时未到 → 不该触发 ==")
    dbmod.get_db = lambda: _DB(3)
    p2, calls2 = _make(3, iterations=3)
    p2._run_loop()
    check("无积压无新增时保持安静", len(calls2) == 0, f"触发 {len(calls2)} 次")

    print("\n== 3. 内存计数器达到阈值同样触发 ==")
    class _RawHot(_Raw):
        def __init__(self):
            super().__init__(0)
            self.write_counter = 100

    class _DBHot:
        raw = _RawHot()

    dbmod.get_db = lambda: _DBHot()
    p3, calls3 = _make(0, iterations=2)
    p3._run_loop()
    check("新增计数 100 时触发", len(calls3) >= 1, f"触发 {len(calls3)} 次")

    print("\n== 4. 触发后会清零计数器（避免连刷）==")
    hot = _DBHot()
    dbmod.get_db = lambda: hot
    p4, calls4 = _make(0, iterations=2)
    p4._run_loop()
    check("write_counter 已归零", hot.raw.write_counter == 0, str(hot.raw.write_counter))

    print("\n== 5. 可观测性：启动就说清触发条件 + 每轮心跳 ==")
    src = (ROOT / "src" / "core" / "data_pipeline.py").read_text(encoding="utf-8")
    check("启动日志写明触发条件", "触发条件: 每 " in src and "当前待清洗积压" in src)
    check("每轮有心跳日志", "[DataPipeline] 心跳:" in src)
    check("心跳里能看到新增/积压/定时三项",
          all(k in src for k in ("新增=", "积压=", "定时")))
    check("触发时打印触发原因",
          ("触发清洗（外部=" in src) or ("大力清洗" in src))
    check("积压判定用真实总数 count_uncleaned",
          "db.raw.count_uncleaned()" in src)
finally:
    dbmod.get_db = _ORIG_GET_DB

print(f"\n==== 结果: {len(PASS)} PASS / {len(FAIL)} FAIL ====")
for f in FAIL:
    print("  FAIL:", f)
sys.exit(1 if FAIL else 0)
