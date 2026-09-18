# -*- coding: utf-8 -*-
"""收尾自检：config/.env/角色资源/数据库 是否完好。只读。"""
import json
import sqlite3
from pathlib import Path

print("=== config.json ===")
d = json.loads(Path("config.json").read_text(encoding="utf-8"))
print("  合法 JSON: True")
print(f"  角色={d['pet']['character']} ui_bg={d['ui']['ui_bg_color']} "
      f"width={d['ui']['chat_window_width']}")
# ★ 1.9.1: sidebar_visible 已从 UIConfig 删除（聊天窗口左侧栏整个移除），
#   这里不再打印它。旧 config.json 里如果还留着这个键，只是没人读，无害。
print("  探针残留:", "PROBE" in json.dumps(d, ensure_ascii=False))
print("  enabled_tools:", len(d["enabled_tools"]), "个")
print("  pipeline:", d.get("pipeline"))

print("\n=== .env ===")
e = Path(".env").read_text(encoding="utf-8")
print("  含 probe:", "probe" in e.lower())
print("  键:", [ln.split("=")[0] for ln in e.splitlines() if "=" in ln and not ln.startswith("#")])

print("\n=== 角色资源 ===")
c = Path("data/桌宠形象")
print("  文件数:", len(list(c.rglob("*.*"))), "(应为 27)")

print("\n=== 数据库 ===")
for f in sorted(Path("data/DBs").glob("*.db")):
    con = sqlite3.connect(f"file:{f}?mode=ro", uri=True)
    tabs = [r[0] for r in con.execute(
        "select name from sqlite_master where type='table'")]
    counts = {}
    for t in tabs:
        if t.startswith("fts") or t.startswith("sqlite"):
            continue
        counts[t] = con.execute(f"select count(*) from {t}").fetchone()[0]
    con.close()
    print(f"  {f.name}: {counts}")
