# -*- coding: utf-8 -*-
"""★ 1.9.3（用户 2026-10-04 定稿）：一次性清空聊天历史 —— 共享工作台改造的上线动作。

用户原话："现有聊天历史直接清空（就当清理缓存）"，但**必须先备份**。

本脚本严格按定稿做三件事：
  1. 先把 `data/DBs/chat_records.db` **整库备份**成
     `data/DBs/chat_records_backup_<YYYYmmdd_HHMMSS>.db`；
  2. 打印清空前的行数（chat_records / topics）；
  3. 只 `DELETE FROM chat_records` + `DELETE FROM topics`。
     ★ `long_term_memories`（LTM）与 `raw_monitor_data`（监控数据）**一个字节都不动** ——
       它们本来就是独立 DB 文件，本脚本连打开都不打开。

备份用的是 SQLite 官方的 `Connection.backup()`（而不是裸文件拷贝）：
裸拷 `.db` 可能漏掉还压在 WAL 里、已提交但未 checkpoint 的数据；
backup API 拿到的是**一致快照**。

用法：
  python tools/clear_chat_history_193.py            # 备份 + 清空
  python tools/clear_chat_history_193.py --dry-run  # 只打印行数，不备份、不清空
"""
import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "data" / "DBs" / "chat_records.db"


def _counts(con) -> tuple:
    """当前 chat_records / topics 行数"""
    chat = con.execute("SELECT COUNT(*) FROM chat_records").fetchone()[0]
    topics = con.execute("SELECT COUNT(*) FROM topics").fetchone()[0]
    return chat, topics


def main() -> int:
    ap = argparse.ArgumentParser(description="清空聊天历史（先备份，只清 chat_records + topics）")
    ap.add_argument("--dry-run", action="store_true",
                    help="只打印会清掉的行数，不做备份、不修改数据库")
    args = ap.parse_args()

    if not DB_PATH.exists():
        print(f"★ 找不到数据库：{DB_PATH}")
        return 1

    con = sqlite3.connect(str(DB_PATH))
    try:
        before_chat, before_topics = _counts(con)
        print(f"数据库      : {DB_PATH}")
        print(f"清空前行数  : chat_records = {before_chat} 条, topics = {before_topics} 条")
        if args.dry_run:
            print("--dry-run    : 不备份、不清空。")
            return 0

        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = DB_PATH.with_name(f"chat_records_backup_{stamp}.db")
        dst = sqlite3.connect(str(backup_path))
        try:
            con.backup(dst)          # 一致快照（含 WAL 中已提交的数据）
        finally:
            dst.close()
        size_mb = backup_path.stat().st_size / 1024 / 1024
        print(f"已备份      : {backup_path}  ({size_mb:.2f} MB)")

        con.execute("DELETE FROM chat_records")
        con.execute("DELETE FROM topics")
        con.commit()

        after_chat, after_topics = _counts(con)
        print(f"清空后行数  : chat_records = {after_chat} 条, topics = {after_topics} 条")
        print("★ 只清了 chat_records / topics；LTM 与监控数据未触碰。")
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main())
