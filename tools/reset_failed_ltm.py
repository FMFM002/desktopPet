"""
1.7.0 LTM 失败记录回退工具

历史背景：
  1.7.0 之前，deepseek-flash 等推理模型开了 thinking 模式，
  LTM 工具读 response.choices[0].message.content 经常拿到空串，
  导致每次都判 "LLM 不可用"，并把 chat_records / monitoring_events
  的 memory_processed / memory_ref 标成 -1。
  这批 -1 记录会永久卡死，永不再被 _feed_ltm_from_chat 拾取。

用途：
  在 1.7.0 修复 _extract_text() 后，把这批 -1 记录重置为 0，
  让它们重新进入 LTM 异步投喂链路。

用法：
  python tools/reset_failed_ltm.py            # 重置所有 -1
  python tools/reset_failed_ltm.py --dry-run  # 只统计、不修改
  python tools/reset_failed_ltm.py --chat 50  # 只重置最近 50 条聊天
"""
import argparse, sqlite3, sys

DB_DIR = "data/DBs"


def count(db_path, table, col):
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    n = con.execute(f"SELECT COUNT(*) FROM {table} WHERE {col}=-1").fetchone()[0]
    total = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    con.close()
    return n, total


def reset(db_path, table, col, limit=None, dry_run=False):
    con = sqlite3.connect(db_path)
    cur = con.cursor()
    n = cur.execute(f"SELECT COUNT(*) FROM {table} WHERE {col}=-1").fetchone()[0]
    print(f"  [{table}] 待回退 -1: {n} 条")
    if n == 0 or dry_run:
        con.close()
        return 0
    if limit:
        sql = (f"UPDATE {table} SET {col}=0 WHERE id IN "
               f"(SELECT id FROM {table} WHERE {col}=-1 ORDER BY id DESC LIMIT ?)")
        cur.execute(sql, (limit,))
    else:
        cur.execute(f"UPDATE {table} SET {col}=0 WHERE {col}=-1")
    con.commit()
    affected = cur.rowcount
    con.close()
    print(f"  [{table}] 已重置 {affected} 条")
    return affected


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--chat", type=int, metavar="N", help="只重置最近 N 条 chat_records")
    ap.add_argument("--events", type=int, metavar="N", help="只重置最近 N 条 monitoring_events")
    args = ap.parse_args()

    print(f"=== 扫描 {DB_DIR} 下的 LTM 失败残留 ===\n")

    targets = [
        ("chat_records.db",   "chat_records",      "memory_processed", args.chat),
        ("raw_monitor_data.db", "monitoring_events", "memory_ref",      args.events),
    ]

    total_reset = 0
    for db, tbl, col, limit in targets:
        path = f"{DB_DIR}/{db}"
        try:
            n_bad, n_total = count(path, tbl, col)
            print(f"[{db}] {tbl}.{col} 总 {n_total} 条 / 失败(-1) {n_bad} 条")
            if n_bad:
                total_reset += reset(path, tbl, col, limit, args.dry_run)
        except FileNotFoundError:
            print(f"  跳过：{path} 不存在")
        except Exception as e:
            print(f"  [错误] {tbl}: {e}")

    print(f"\n{'只统计' if args.dry_run else '已处理'} {total_reset} 条")
    if not args.dry_run and total_reset > 0:
        print("提示：重启桌宠后这些记录会被 _feed_ltm_from_chat() 自动拾取。")


if __name__ == "__main__":
    main()