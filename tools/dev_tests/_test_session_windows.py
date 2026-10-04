"""1.6.0 回归自测：DB 角色窗口 + 会话窗口切换 + 话题总结链路（不启动 Qt/LLM）"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ.setdefault("DEEPSEEK_API_KEY", "test")

# ★ 用隔离目录保存测试数据库，避免污染 data/DBs
import src.core.database as _dbmod
_TMP = Path(__file__).resolve().parents[2] / "_test_tmp_dbs"
_TMP.mkdir(exist_ok=True)
_dbmod.DB_DIR = _TMP

ok = True


def check(name, cond, extra=""):
    global ok
    print(("  [PASS] " if cond else "  [FAIL] ") + name + (f"  {extra}" if extra else ""))
    if not cond:
        ok = False


# ── 1. DB 层：角色窗口 ──
print("== 1. DB 角色窗口 ==")
from src.core.database import ChatRecordsDB

db_chat = ChatRecordsDB()

t_a = db_chat.create_topic("", "甲")
check("create_topic 带角色", t_a > 0)
active = db_chat.get_active_topic("甲")
check("get_active_topic 命中", active is not None and active["id"] == t_a)

t_b = db_chat.create_topic("", "乙")
check("两个角色各自活跃", db_chat.get_active_topic("甲")["id"] == t_a
      and db_chat.get_active_topic("乙")["id"] == t_b)

# 同角色再开一个 → 旧的归档
db_chat.archive_topic(t_a, "甲的旧话题总结")
t_a2 = db_chat.create_topic("", "甲")
check("归档后可取回总结", db_chat.get_last_archived_summary("甲", t_a2) == "甲的旧话题总结")
check("归档话题不在活跃列表", db_chat.get_active_topic("甲")["id"] == t_a2)
check("list_character_topics 按角色隔离",
      all(r["character"] == "甲" for r in db_chat.list_character_topics("甲")))

db_chat.add("user", "你好呀", topic_id=t_a2)
db_chat.add("assistant", "你好，主人", topic_id=t_a2)
check("count_character_messages 统计", db_chat.count_character_messages("甲") == 2)
check("get_topic_messages 正序", [m["role"] for m in db_chat.get_topic_messages(t_a2)]
      == ["user", "assistant"])

# 清理测试数据
for tid in (t_a, t_b, t_a2):
    db_chat.delete_topic(tid)
check("清理完成", db_chat.count_character_messages("甲") == 0)

# ── 2. 会话层：★ 1.9.3 共享工作台（所有形象共用同一个话题）──
print("== 2. 会话共享工作台 ==")
from src.core.session import ChatSession
from src.core.database import SHARED_CHARACTER
s = ChatSession()
s._config_character = lambda: "甲"      # 绕开真实 config
s.set_character("甲")
check("set_character 建立共享话题", s.character == "甲" and s.topic_id is not None)
tid_a = s.topic_id
s.messages.append({"role": "user", "content": "甲的消息"})
s.set_character("乙")
# ★ 1.9.3 新设计：切形象**不换话题、不清空上下文**（原断言是"窗口独立"，
#   那是"一形象一话题"时代的规则，已被用户 2026-10-04 的定稿取代）
check("★切到乙：共用同一个话题且上下文不被冲掉",
      s.character == "乙" and s.topic_id == tid_a
      and any("甲的消息" in (m.get("content") or "") for m in s.messages))
check("★话题挂在共享标记下（不再按形象名建话题）",
      s.db.get_active_topic(SHARED_CHARACTER)["id"] == tid_a
      and s.db.list_character_topics("甲") == [] and s.db.list_character_topics("乙") == [])
s.set_character("甲")
check("切回甲：还是同一个话题", s.topic_id == tid_a)
s.set_character("乙")
s.start_new_topic(summarize=False)
check("开启新话题清空上下文", len(s.messages) == 0 and s.topic_id is not None
      and s.topic_id != tid_a)

# 总结链路（打桩 SummarizerClient，避免真实 API）
print("== 3. 总结 → 新话题 链路 ==")
import src.core.llm_client as llm

class _FakeSum:
    def summarize_conversation(self, text):
        return "用户聊了测试话题；未完成：无。"

llm.get_summarizer_client = lambda: _FakeSum()

s2 = ChatSession()
s2._config_character = lambda: "丙"
s2.set_character("丙")
s2.messages.append({"role": "user", "content": "记住我喜欢蓝色"})
s2.messages.append({"role": "assistant", "content": "好的，记住了"})
old_topic = s2.topic_id
done = []
s2.on_system_msg = lambda m: done.append(m)
s2.summarize_current_topic(then_new_topic=True, reason="旧")
import time
for _ in range(50):
    if s2.topic_id != old_topic:
        break
    time.sleep(0.1)
check("总结后开启新话题", s2.topic_id != old_topic)
check("新话题继承总结", any("上一段对话的总结" in (m.get("content") or "")
                          for m in s2.messages))
check("旧话题已归档",
      db_chat.get_topic_summary(old_topic) != "" or
      s2.db.get_topic_summary(old_topic) != "")
check("旧话题状态=archived",
      s2.db.get_active_topic(SHARED_CHARACTER)["id"] != old_topic)

# 清理
for tid in (tid_a, s.topic_id, old_topic, s2.topic_id):
    try:
        s2.db.delete_topic(tid)
    except Exception:
        pass

print("\n结果:", "ALL PASS" if ok else "FAILED")
sys.exit(0 if ok else 1)
