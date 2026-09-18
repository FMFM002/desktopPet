# -*- coding: utf-8 -*-
import sqlite3
from pathlib import Path

for d in sorted(Path("data/DBs").glob("*")):
    if d.suffix.lower() not in (".db", ".sqlite", ".sqlite3"):
        continue
    print(f"\n=== {d.name} ({d.stat().st_size/1024:.0f} KB) ===")
    try:
        c = sqlite3.connect(f"file:{d}?mode=ro", uri=True)
        tables = [r[0] for r in c.execute(
            "select name from sqlite_master where type='table'")]
        print("  表:", tables)
        for t in ("raw_events", "monitoring_events"):
            if t not in tables:
                continue
            n = c.execute(f"select count(*) from {t}").fetchone()[0]
            print(f"  {t}: {n} 行")
            cols = [r[1] for r in c.execute(f"pragma table_info({t})")]
            if "cleaned" in cols:
                for row in c.execute(
                        f"select cleaned, count(*) from {t} group by cleaned"):
                    print(f"    cleaned={row[0]}: {row[1]}")
            if "memory_ref" in cols:
                for row in c.execute(
                        f"select memory_ref, count(*) from {t} group by memory_ref"):
                    print(f"    memory_ref={row[0]}: {row[1]}")
            if "time_start" in cols:
                r = c.execute(f"select min(time_start), max(time_start) from {t}").fetchone()
                print(f"    时间范围: {r[0]} .. {r[1]}")
        c.close()
    except Exception as e:
        print("  读取失败:", e)
