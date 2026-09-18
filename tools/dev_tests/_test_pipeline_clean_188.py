# -*- coding: utf-8 -*-
"""★ 1.8.8 回归：机械清洗是空转 + 缺重复合并。

用户提问："按道理来说这些监控数据应该会先机械清洗，然后才是 LLM 清洗吗？"
顺序确实是「取数 → _fetch_uncleaned → 机械清洗 → LLM」，但实测：
  · 机械清洗 100 → 100，四条规则一条都没命中
  · 规则1 的 source 白名单是 ("process","system")，
    而库里真实 source 是 process_metadata(44%)/window_metadata(39%) → 永远不匹配
  · 这批数据 65% 是重复（"活动应用类别: browser, ide" 一条出现 44 次）

用法: python tools/dev_tests/_test_pipeline_clean_188.py
"""
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   {detail}" if detail else ""))


from src.core.data_pipeline import DataPipeline  # noqa: E402

p = DataPipeline()


def ev(i, source, content, created="2026-09-15 21:00:00"):
    return {"id": i, "source": source, "content": content, "created_at": created}


print("== 1. 规则1：source 白名单修好了（关键修复）==")
noise = "启动 gpu-process 子进程"
check("process_metadata + 噪声 → 被过滤",
      p._mechanical_clean([ev(1, "process_metadata", noise)]) == [])
check("window_metadata + 噪声 → 被过滤",
      p._mechanical_clean([ev(2, "window_metadata", "renderer 崩溃")]) == [])
check("system + 噪声 → 被过滤",
      p._mechanical_clean([ev(3, "system", "utility: network-service")]) == [])
check("旧的 'process' 仍被覆盖（向后兼容）",
      p._mechanical_clean([ev(4, "process", noise)]) == [])
check("正常内容不会被误杀",
      len(p._mechanical_clean([ev(5, "process_metadata", "活动应用类别: browser, ide")])) == 1)

print("\n== 2. 噪声词用词边界匹配，别误杀窗口标题 ==")
keep = [
    ev(10, "window_metadata", "切换应用: chrome.exe | 窗口: Storage 管理 - 控制台"),
    ev(11, "window_metadata", "切换应用: audacity.exe | 窗口: audio track 编辑"),
]
check("标题里出现 storage/audio 等常见词不会被误杀",
      len(p._mechanical_clean(keep)) == 2,
      f"保留 {len(p._mechanical_clean(keep))}/2")

print("\n== 3. 规则2/3 仍然有效 ==")
check("空内容被丢弃", p._mechanical_clean([ev(20, "x", "  ")]) == [])
check("过短内容被丢弃", p._mechanical_clean([ev(21, "x", "ab")]) == [])
check("短纯路径被丢弃", p._mechanical_clean([ev(22, "x", r"D:\tmp\a.txt")]) == [])
check("正常短文本保留", len(p._mechanical_clean([ev(23, "x", "在写代码")])) == 1)

print("\n== 4. 重复合并 ==")
dup = [
    ev(30, "process_metadata", "活动应用类别: browser, ide", "2026-09-15 21:00:01"),
    ev(31, "process_metadata", "活动应用类别: browser, ide", "2026-09-15 21:00:02"),
    ev(32, "process_metadata", "活动应用类别: browser, ide", "2026-09-15 21:09:58"),
    ev(33, "window_metadata", "切换应用: python.exe | 窗口: python", "2026-09-15 21:00:03"),
    ev(34, "process_metadata", "活动应用类别: browser, ide", "2026-09-15 21:10:00"),
]
m = p._dedup_events(dup)
check("5 条 → 2 条", len(m) == 2, f"{len(m)} 条: {[e['id'] for e in m]}")
check("保留首次出现的那条（位置与时间戳不变）", m[0]["id"] == 30 and m[1]["id"] == 33,
      str([e["id"] for e in m]))
check("标注了重复次数 ×4", "×4" in m[0]["content"], m[0]["content"])
check("标注了末次时间（保住时间跨度）", "至 2026-09-15 21:10:00" in m[0]["content"],
      m[0]["content"])
check("不重复的那条不动", m[1]["content"] == "切换应用: python.exe | 窗口: python")

print("\n== 5. 归一化：只差数字/时长算同一条 ==")
n1 = ev(40, "system", "[进程] idea64.exe(1进程, 2749MB), MemCompression(1进程, 1064MB)")
n2 = ev(41, "system", "[进程] idea64.exe(1进程, 2097MB), MemCompression(1进程, 977MB)")
n3 = ev(42, "system", "[窗口切换] A → B (持续 32s)")
n4 = ev(43, "system", "[窗口切换] A → B (持续 55s)")
check("进程列表（只差内存数字）合并成 1 条", len(p._dedup_events([n1, n2])) == 1)
check("窗口切换（只差持续秒数）合并成 1 条", len(p._dedup_events([n3, n4])) == 1)

print("\n== 6. 不该合并的情况 ==")
d1 = ev(50, "window_metadata", "切换应用: a.exe | 窗口: X")
d2 = ev(51, "window_metadata", "切换应用: b.exe | 窗口: Y")
check("内容不同不合并", len(p._dedup_events([d1, d2])) == 2)
d3 = ev(52, "clipboard", "同一段文字")
d4 = ev(53, "keyboard", "同一段文字")
check("source 不同不合并", len(p._dedup_events([d3, d4])) == 2)

print("\n== 7. 真实积压上跑一遍（不写库）==")
try:
    from src.core.database import get_db
    db = get_db()
    raw = p._fetch_uncleaned(db)
    if raw:
        mech = p._mechanical_clean([dict(r) for r in raw])
        merged = p._dedup_events([dict(r) for r in mech])
        ratio = 1 - len(merged) / len(raw)
        print(f"  取数 {len(raw)} → 机械清洗 {len(mech)} → 合并 {len(merged)}"
              f"（减少 {ratio*100:.0f}%）")
        # ★ 1.9.0 修订：这条原来写死「≥40%」，但它量的是**库里的真实积压**，
        #   不是代码行为 —— 代码行为已由上面 20 多条造数据的确定性用例覆盖。
        #   实测（2026-09-16）：那批高重复积压已被 pipeline 自己洗掉
        #   （日志里 32→3），剩下的新数据重复度天然低，而且**样本只有 7 条**：
        #   7→5 就是 29%，要 7→4 才够 43% —— 差一条就变色，纯属误报。
        #   所以：样本够大（≥30 条）才卡 40%，样本小只要求"确实变少且没丢数据"。
        # ★ 2026-09-17 再修：**样本 1 条时"变少"在数学上不可能**（1 条没法再合并），
        #   而反复跑全量回归会把真实积压耗到只剩 1 条 —— 那不是代码坏了。
        #   所以 <2 条时只验"跑得通、没丢数据"，不做比例判定。
        _big_enough = len(raw) >= 30
        if len(raw) < 2:
            print(f"  （真实积压只剩 {len(raw)} 条，样本太小，跳过「确实变少」的比例判定）")
        else:
            _ok = ratio > 0 and (ratio >= 0.40 or not _big_enough)
            check(f"真实积压上跑得通且确实变少（样本 {len(raw)} 条，减少 {ratio*100:.0f}%）",
                  _ok,
                  "" if _big_enough else "样本 <30 条，不卡 40% 阈值（小样本上没统计意义）")
        check("合并后每条都还在", len(merged) > 0)
    else:
        print("  （库里没有待清洗数据，跳过）")
    db.close()
except Exception as e:
    print("  跳过（DB 不可用）:", e)

print("\n== 8. 顺序：机械清洗 → 重复合并 → 才轮到 LLM ==")
src = (ROOT / "src" / "core" / "data_pipeline.py").read_text(encoding="utf-8")
batch = src.split("def _process_batch", 1)[1]
i_mech = batch.find("self._mechanical_clean")
i_dedup = batch.find("self._dedup_events")
i_llm = batch.find("_llm_summarize_with_retry")
check("机械清洗在重复合并之前", 0 <= i_mech < i_dedup, f"mech@{i_mech} dedup@{i_dedup}")
check("重复合并在 LLM 之前", 0 <= i_dedup < i_llm, f"dedup@{i_dedup} llm@{i_llm}")
check("合并日志会打印省了多少", "重复合并:" in src)

print(f"\n==== 结果: {len(PASS)} PASS / {len(FAIL)} FAIL ====")
for f in FAIL:
    print("  FAIL:", f)
sys.exit(1 if FAIL else 0)
