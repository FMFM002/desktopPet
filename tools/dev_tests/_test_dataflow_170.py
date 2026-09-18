"""1.7.0：验证数据流改动 —
1. monitoring_events → 长期记忆 断链已接通（memory_ref 标记）
2. LLM 失败 → memory_ref=-1（数据回退，不误标已处理）
3. LTM 工具禁用 → 直接标记 1（不堆积）
4. chat_records 的 get_unprocessed / mark_processed 链路（供 main_window 投喂用）
不依赖真实 LLM（stub 掉 LongTermMemoryTool）。
"""
import os
import sys
import shutil
from pathlib import Path

BASE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE))
os.environ.setdefault("DEEPSEEK_API_KEY", "test")

import src.core.database as _dbmod
_TMP = BASE / "_test_tmp_dbs"
if _TMP.exists():
    shutil.rmtree(_TMP)
_TMP.mkdir(exist_ok=True)
_dbmod.DB_DIR = _TMP

ok = True


def check(name, cond, extra=""):
    global ok
    print(("  [PASS] " if cond else "  [FAIL] ") + name + (f"  {extra}" if extra else ""))
    if not cond:
        ok = False


from src.core.database import get_db

db = get_db()

# ── stub LTM 工具 ──
import tools.long_term_memory_tool as ltm_mod


class StubLTM:
    def __init__(self):
        self.disabled = False
        self.fed = []
        self.result = "长期记忆更新完成：新增 0 条。"

    def is_disabled(self):
        return self.disabled

    def execute_async(self, text, callback=None):
        self.fed.append(text)
        if callback:
            callback(self.result)


stub = StubLTM()
ltm_mod._tool_instance = stub  # get_long_term_memory_tool 单例替换

from src.core.data_pipeline import get_pipeline

pipeline = get_pipeline()

# ── 1. monitoring_events → LTM：成功路径 ──
print("== 1. 监控事件 → 长期记忆（成功） ==")
e1 = db.raw.add_event(time_start="2026-09-15 10:00:00", event="浏览网页",
                      detail="用户在浏览器查看了 Python 文档", confidence=90)
e2 = db.raw.add_event(time_start="2026-09-15 10:05:00", event="写代码",
                      detail="用户在 VSCode 中编写 Python 脚本", confidence=90)
pipeline._feed_long_term_memory(db)
check("投喂了 1 批文本", len(stub.fed) == 1)
check("文本包含两条事件", "浏览网页" in stub.fed[0] and "写代码" in stub.fed[0])
rows = db.raw.get_unprocessed_events()
check("memory_ref 全部标记为 1", not rows, f"剩余 {len(rows)} 条")

# ── 2. LLM 失败 → memory_ref=-1 ──
print("== 2. 监控事件 → 长期记忆（LLM 失败回退） ==")
stub.fed.clear()
stub.result = "[长期记忆] LLM 不可用，放弃本批次。（数据已回退，不污染记忆库）"
e3 = db.raw.add_event(event="测试失败路径", detail="这条事件的记忆提取会失败", confidence=50)
pipeline._feed_long_term_memory(db)
check("投喂发生", len(stub.fed) == 1)
row = db.ltm  # noqa 占位
cur = db.raw._conn.execute(
    "SELECT memory_ref FROM monitoring_events WHERE id=?", (e3,)).fetchone()
check("memory_ref 标记为 -1", cur and cur[0] == -1)

# ── 3. 工具禁用 → 直接标记 1，不投喂 ──
print("== 3. 工具禁用：跳过投喂，直接标记 ==")
stub.fed.clear()
stub.disabled = True
e4 = db.raw.add_event(event="禁用期间事件", detail="工具禁用时的监控事件不应投喂", confidence=50)
pipeline._feed_long_term_memory(db)
check("未投喂", len(stub.fed) == 0)
cur = db.raw._conn.execute(
    "SELECT memory_ref FROM monitoring_events WHERE id=?", (e4,)).fetchone()
check("memory_ref 标记为 1", cur and cur[0] == 1)
stub.disabled = False

# ── 4. chat_records 投喂链路 API ──
print("== 4. 聊天记录投喂链路 ==")
t = db.chat.create_topic("", "测试角色_数据流")
c1 = db.chat.add("user", "我喜欢蓝色，最近在学 Rust", topic_id=t)
c2 = db.chat.add("assistant", "记住了，蓝色很衬你", topic_id=t)
c3 = db.chat.add("tool", '{"result": "ok"}', tool_name="web_search", topic_id=t)
unprocessed = db.chat.get_unprocessed()
check("3 条未处理", len(unprocessed) == 3)
tool_rows = [r for r in unprocessed if r["role"] not in ("user", "assistant")]
db.chat.mark_processed([r["id"] for r in tool_rows], 1)
# 模拟 main_window._feed_ltm_from_chat 的核心逻辑
rows = [r for r in db.chat.get_unprocessed()
        if r["role"] in ("user", "assistant")]
text = "\n".join(f"{r['role']}: {r['content']}" for r in rows)
ids = [r["id"] for r in rows]
stub.result = "长期记忆更新完成：新增 1 条。"
stub.execute_async(text, callback=lambda res: db.chat.mark_processed(ids, 1))
check("投喂文本包含用户消息", "喜欢蓝色" in stub.fed[-1])
remaining = db.chat.get_unprocessed()
check("chat 全部标记已处理", not remaining, f"剩余 {len(remaining)} 条")

# ── 5. 在途去重（_pending_memory_ids）──
print("== 5. 在途去重 ==")
pipeline._pending_memory_ids = {999}
e5 = db.raw.add_event(event="在途测试", detail="该事件 id 不在途，应正常投喂", confidence=50)
stub.fed.clear()
stub.result = "长期记忆更新完成：新增 0 条。"
pipeline._feed_long_term_memory(db)
check("在途 id 被跳过、新事件正常投喂",
      len(stub.fed) == 1 and "在途测试" in stub.fed[0])

# ── 清理 ──
db.close()
shutil.rmtree(_TMP, ignore_errors=True)

print()
print("RESULT:", "ALL PASS" if ok else "HAS FAILURES")
sys.exit(0 if ok else 1)
