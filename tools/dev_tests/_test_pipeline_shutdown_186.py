# -*- coding: utf-8 -*-
"""★ 1.8.6 回归：退出过程中 Pipeline 还在开批次，然后数据库被关掉。

真实证据 logs/prts_20260915_211608.log：
  21:18:41 [MainWindow] INFO: 正在退出...
  21:18:46 [InputMonitor] 增强输入监控已停止
  21:18:46 [DataPipeline] === 开始清洗批次 ===      ← 退出中反而开了新批次
  21:18:46 [DataPipeline] LLM 总结 第1/3次          ← 还在调 LLM
  21:18:46 [Database] 数据库已关闭                  ← 库关了，批次还在跑

根因：DataPipeline.stop() 用 _process_event.set() 唤醒工作线程，
  而 while self._running 的判断在 wait() 之前就已经通过了 ——
  线程醒来后照样往下走，直接 _process_batch()。

用法: python tools/dev_tests/_test_pipeline_shutdown_186.py
"""
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   {detail}" if detail else ""))


import src.core.database as dbmod  # noqa: E402
from src.core.data_pipeline import DataPipeline  # noqa: E402


class _FakeRaw:
    write_counter = 0


class _FakeDB:
    raw = _FakeRaw()


_ORIG_GET_DB = dbmod.get_db
dbmod.get_db = lambda: _FakeDB()

try:
    print("== 1. stop() 唤醒线程时不该再开批次 ==")
    p = DataPipeline()
    calls = []
    p._process_batch = lambda: calls.append(time.monotonic())
    p._feed_long_term_memory = lambda db: None      # 别碰真实长期记忆
    p._should_trigger_by_time = lambda: False       # 关掉定时触发
    p._interval_seconds = 99999

    p.start()
    time.sleep(0.4)
    check("正常运行时不会自己乱开批次", calls == [], f"calls={len(calls)}")

    t0 = time.monotonic()
    p.stop()
    dt = time.monotonic() - t0
    time.sleep(0.6)   # 给被唤醒的线程足够时间"犯错"

    check("stop() 之后没有任何批次开跑（这就是修复点）", calls == [],
          f"calls={len(calls)}（>0 说明又被唤醒出批次了）")
    check("stop() 本身很快返回（< 3s）", dt < 3.0, f"{dt:.2f}s")
    check("stop() 后 _running 为 False", p._running is False)
    check("工作线程确实已结束", p._thread is None or not p._thread.is_alive())

    print("\n== 2. 源码里的三处退出守卫 ==")
    src = (ROOT / "src" / "core" / "data_pipeline.py").read_text(encoding="utf-8")

    loop = src.split("def _run_loop", 1)[1].split("def _should_trigger_by_time", 1)[0]
    check("主循环 wait 之后有 `if not self._running: break`",
          "if not self._running:" in loop and "break" in loop)
    check("跳出判断在 wait 之后",
          loop.find("wait(timeout=60)") < loop.find("if not self._running:"))

    batch = src.split("def _process_batch", 1)[1].split("def _feed_long_term_memory", 1)[0]
    check("_process_batch 开头退出即返回", "if not self._running:" in batch[:400])
    check("调 LLM 之前再检查一次", batch.count("if not self._running:") >= 3,
          f"{batch.count('if not self._running:')} 处")
    check("LLM 之后、写库之前再检查一次",
          batch.find("if not self._running:",
                     batch.find("_llm_summarize_with_retry")) > 0,
          f"LLM 之后守卫位置={batch.find('if not self._running:', batch.find('_llm_summarize_with_retry'))}")

    retry = src.split("def _llm_summarize_with_retry", 1)[1][:1200]
    check("重试循环里也会因退出而中止",
          "if not self._running:" in retry and "中止 LLM 重试" in retry)

    print("\n== 3. _process_batch 在退出时直接返回（真跑一次）==")
    p2 = DataPipeline()
    p2._running = False
    hit = []
    p2._fetch_uncleaned = lambda db: hit.append("fetch") or []
    p2._last_run = 0
    p2._process_batch()
    check("退出中调用 _process_batch 不会去取数据", hit == [], str(hit))

    print("\n== 4. 退出顺序：Pipeline 先停、数据库最后关 ==")
    mw = (ROOT / "src" / "ui" / "main_window.py").read_text(encoding="utf-8")
    quit_body = mw.split("def _quit", 1)[1]
    svc = quit_body.split("def _stop_services", 1)[1].split("def _stop_lightweight", 1)[0]
    check("_stop_services 里 Pipeline 排在 monitor 之前",
          0 <= svc.find("self._pipeline.stop()") < svc.find("self.monitor.stop()"),
          f"pipeline@{svc.find('self._pipeline.stop()')} monitor@{svc.find('self.monitor.stop()')}")
    check("_stop_services 里 Pipeline 排在 input_monitor 之前",
          svc.find("self._pipeline.stop()") < svc.find("self.input_monitor.stop()"))
    check("db.close() 仍在两个 join 之后",
          quit_body.find("self.db.close()") > quit_body.find("t2.join(timeout=5)"),
          f"close@{quit_body.find('self.db.close()')} join2@{quit_body.find('t2.join(timeout=5)')}")
finally:
    dbmod.get_db = _ORIG_GET_DB

print(f"\n==== 结果: {len(PASS)} PASS / {len(FAIL)} FAIL ====")
for f in FAIL:
    print("  FAIL:", f)
sys.exit(1 if FAIL else 0)
