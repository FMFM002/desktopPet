"""
一键手动触发长期记忆 summarize（1.7.3 新增）
================================================================================
用法:
    cd d:/Dtopdesk/普瑞赛斯在哪/倒腾/桌宠/PTRTS1.7.0_DSH&Hy3
    python tools/run_ltm_summarize.py                # 立即跑一次
    python tools/run_ltm_summarize.py --dry-run      # 只看现状
    python tools/run_ltm_summarize.py --decay-only   # 只跑衰减步（不调 LLM）
    python tools/run_ltm_summarize.py --cluster-only # 只跑聚类步（需要 LLM）
================================================================================
"""
import argparse
import sys
import json
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))


def main():
    parser = argparse.ArgumentParser(description="长期记忆定期总结（一键触发）")
    parser.add_argument("--dry-run", action="store_true",
                        help="只检查当前 DB 状态，不实际跑")
    parser.add_argument("--decay-only", action="store_true",
                        help="只跑衰减+清理（不调 LLM）")
    parser.add_argument("--cluster-only", action="store_true",
                        help="只跑聚类+LLM 合并（要 LLM）")
    args = parser.parse_args()

    # 1) 现状报告
    from src.core.database import get_db
    from tools.ltm_summarizer import LTM_DB_PATH
    db = get_db()
    count = db.ltm.count()
    size_kb = LTM_DB_PATH.stat().st_size / 1024.0 if LTM_DB_PATH.exists() else 0

    print("=" * 60)
    print(" 长期记忆定期总结（1.7.3）")
    print("=" * 60)
    print(f"DB 路径: {LTM_DB_PATH}")
    print(f"文件大小: {size_kb:.1f} KB")
    print(f"条目数: {count}")
    print()

    # 拉 5 条样本
    sample = db.ltm.get_all()[:5]
    if sample:
        print("样本（前 5 条）:")
        for m in sample:
            print(f"  - {m['variable_name']}: {m['value'][:40]} "
                  f"(conf={m['confidence']:.2f}, cat={m.get('category', 'other')})")
    print()

    if args.dry_run:
        from src.utils.config import get_config
        cfg = get_config().ltm
        print(f"[阈值参考] size≥{cfg.file_size_threshold_kb}KB | "
              f"count≥{cfg.count_threshold} | "
              f"衰减={cfg.decay_idle_days}天/{cfg.decay_rate_per_period}/期 | "
              f"删≤{cfg.delete_threshold}")
        print("[dry-run] 已退出，未做修改")
        return

    # 2) 触发 summarize
    from tools.ltm_summarizer import get_ltm_summarizer
    sumzr = get_ltm_summarizer()

    if args.decay_only:
        result = sumzr._decay_step()
    elif args.cluster_only:
        result = sumzr._cluster_step()
    else:
        result = sumzr.trigger_now()  # 全套

    print("执行结果:")
    print(f"  {result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)}")
    print()

    # 3) 之后再报告一次
    new_count = db.ltm.count()
    new_size_kb = LTM_DB_PATH.stat().st_size / 1024.0 if LTM_DB_PATH.exists() else 0
    print(f"执行后: {new_size_kb:.1f} KB / {new_count} 条 "
          f"(Δ count={new_count - count:+d})")


if __name__ == "__main__":
    main()
