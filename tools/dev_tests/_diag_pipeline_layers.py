# -*- coding: utf-8 -*-
"""只读诊断：拿真实 raw_events 跑一遍 Pipeline 的「取数 → 机械清洗」，
看看每一层实际过滤掉多少、剩下的长什么样、以及会不会卡住。

用法: python tools/dev_tests/_diag_pipeline_layers.py
"""
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from src.core.data_pipeline import DataPipeline  # noqa: E402
from src.core.database import get_db  # noqa: E402

db = get_db()
p = DataPipeline()

total_rows = db.raw.get_count()
uncleaned_all = db.raw.get_uncleaned(100000)
uncleaned = len(uncleaned_all)
print(f"raw_events 总数: {total_rows}")
print(f"其中 cleaned=0 : {uncleaned}")
print(f"cleaned!=0     : {total_rows - uncleaned}")

print("\n===== 第 1 层: get_uncleaned(100) 取最旧 100 条 =====")
raw = db.raw.get_uncleaned(100)
print(f"取出 {len(raw)} 条  id 范围 {min(r['id'] for r in raw)}..{max(r['id'] for r in raw)}")

print("\n===== 第 2 层: _fetch_uncleaned 的 时间/ID 过滤 =====")
fetched = p._fetch_uncleaned(db)
kept_ids = {r["id"] for r in fetched}
dropped = [r for r in raw if r["id"] not in kept_ids]
print(f"100 条 → 保留 {len(fetched)} 条，过滤掉 {len(dropped)} 条")
if dropped:
    print("  !! 被过滤掉的（这批不会被清洗、也不会被标记 cleaned → 会一直卡在最前面）:")
    for r in dropped[:5]:
        print(f"     id={r['id']} created_at={r['created_at']} src={r['source']}")
    max_id = max(r["id"] for r in raw)
    for r in dropped[:3]:
        print(f"     id_diff={max_id - r['id']} (阈值<=100)")
else:
    print("  本批没被过滤（id 连续、时间也接近）")

print("\n===== 第 3 层: _mechanical_clean 四条规则逐条统计 =====")
browser_noise = ("crash", "gpu-process", "utility", "renderer",
                 "network-service", "storage", "audio", "video-capture")
rule_hits = Counter()
for evt in fetched:
    source = evt.get("source", "")
    content = evt.get("content", "") or ""
    if source in ("process", "system") and content:
        if any(n in content.lower() for n in browser_noise):
            rule_hits["规则1 浏览器子进程噪声"] += 1
            continue
    if not content or len(content.strip()) < 3:
        rule_hits["规则2 空/过短内容"] += 1
        continue
    cs = content.strip()
    if re.match(r'^[A-Za-z]:\\[^\s]*$', cs) or re.match(r'^/[^\s]*$', cs):
        if len(cs) < 200:
            rule_hits["规则3 短纯路径"] += 1
            continue
    if source == "clipboard" and len(content) > 2000:
        rule_hits["规则4 剪贴板截断(不算丢弃)"] += 1

cleaned = p._mechanical_clean(fetched)
print(f"机械清洗: {len(fetched)} → {len(cleaned)} 条")
if rule_hits:
    for k, v in rule_hits.most_common():
        print(f"  {k}: {v} 条")
else:
    print("  ★ 四条规则一条都没命中 —— 机械清洗目前是「原样放行」")

print("\n===== 真实数据长什么样 =====")
print("source 分布:", dict(Counter(r["source"] for r in fetched).most_common()))
print("\n前 12 条内容样本（截断 90 字）:")
for r in fetched[:12]:
    c = (r.get("content") or "").replace("\n", " ")[:90]
    print(f"  [{r['source']:>16}] id={r['id']:<5} {c!r}")

print("\n===== 关键结论 =====")
print(f"  送进 LLM 的条数 = {len(cleaned)}  (机械清洗前 {len(fetched)})")
print(f"  积压 {uncleaned} 条 × 每批 100 条 = 约 {-(-uncleaned // 100)} 批才能清完")

print("\n===== 机械清洗「本可以」滤掉多少？ =====")
# ① 连续完全重复
consec_dup = 0
prev = None
for r in fetched:
    key = (r.get("source"), (r.get("content") or "").strip())
    if key == prev:
        consec_dup += 1
    prev = key
print(f"  连续完全重复（同 source + 同内容）: {consec_dup}/{len(fetched)} 条")

# ② 归一化后重复（去掉「持续 32s」这类会变的部分）
def norm(s):
    s = (s or "").strip()
    s = re.sub(r"持续\s*\d+s", "持续Ns", s)
    s = re.sub(r"\d+", "N", s)
    return s

norm_dup = 0
seen = set()
for r in fetched:
    key = (r.get("source"), norm(r.get("content")))
    if key in seen:
        norm_dup += 1
    else:
        seen.add(key)
print(f"  归一化后重复（去掉数字差异）: {norm_dup}/{len(fetched)} 条")

# ③ 按 source 统计「同内容重复率」
by_src = Counter()
for r in fetched:
    by_src[(r["source"], norm(r.get("content")))] += 1
print("\n  重复最多的几类:")
for (src, content), n in by_src.most_common(6):
    if n > 1:
        print(f"    ×{n:<3} [{src}] {content[:70]!r}")

# ④ 规则1 想滤的东西，实际 source 名对不上
print("\n  规则1 的 source 白名单（修复前）= ('process', 'system')")
print("  实际出现的 source   =", dict(Counter(r['source'] for r in fetched).most_common()))

print("\n===== 修复后的真实效果 =====")
fetched2 = p._fetch_uncleaned(db)
mech2 = p._mechanical_clean([dict(r) for r in fetched2])
merged2 = p._dedup_events([dict(r) for r in mech2])
print(f"  取数         : {len(fetched2)} 条")
print(f"  机械清洗后   : {len(mech2)} 条（规则滤掉 {len(fetched2) - len(mech2)} 条）")
print(f"  重复合并后   : {len(merged2)} 条（合并掉 {len(mech2) - len(merged2)} 条）")
if len(fetched2):
    print(f"  → 送进 LLM 的数据量减少 "
          f"{100 * (1 - len(merged2) / len(fetched2)):.0f}%")
print("\n  合并后的样本（前 10 条）:")
for e in merged2[:10]:
    c = (e.get("content") or "")[:92]
    print(f"    [{e.get('source', ''):>16}] {c}")

print("\n===== 全库口径 =====")
print(f"  积压 {uncleaned} 条 → 每批取 100 原始条，合并后约 {len(merged2)} 条送 LLM")
print(f"  约需 {-(-uncleaned // 100)} 批清完")

db.close()
