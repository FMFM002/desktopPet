# -*- coding: utf-8 -*-
"""★ 1.9.3（用户 2026-10-04 定稿）：**共享工作台**回归。

把"每个形象一套上下文"改成"所有形象共用同一个工作台"之后，这套件钉住四件事：
  1. `chat_records.speaker` 列的迁移**幂等**（每次启动都跑，重复执行不许报错）；
  2. 所有形象共用**同一个**话题（两个不同形象名拿到同一个 topic_id）；
  3. 喂给 LLM 的上下文装配规则：
       自己的发言 = assistant 无前缀；别人的发言 = user + `[名字]：`；
       孤儿 tool 跳过（配对的本轮工具结果必须留）；超过 N 条时更早的折成 system 摘要；
  4. 系统提示词里确实带着"共享工作台 / 别把别人的话当成自己说的"这一段，
     并且这一段进了缓存键（改了它必须重建提示词缓存）。

★ 不碰真实 `data/DBs`：全过程在自己的一次性临时目录里开库。
★ 不调真实 LLM：只测装配函数与提示词字符串。
"""
import os
import sys
import shutil
import sqlite3
from pathlib import Path

BASE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE))
os.environ.setdefault("DEEPSEEK_API_KEY", "test")

import src.core.database as _dbmod  # noqa: E402

# ★ 先指到本套件**专用**的一次性目录，再碰任何 DB / 会话
#   （get_db 是单例，必须抢先；不能和 `_test_tmp_dbs` 里的其它套件混用）
_TMP = BASE / "_test_tmp_dbs_shared193"
shutil.rmtree(_TMP, ignore_errors=True)      # 上一轮若跑挂留下的残留，先清掉
_TMP.mkdir(parents=True, exist_ok=True)

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'[PASS]' if cond else '[FAIL]'} {name}" + (f"  {extra}" if extra else ""))


# ── 1. speaker 列迁移：旧库补列 + 幂等 ──
print("== 1. speaker 列迁移（幂等）==")
from src.core.database import ChatRecordsDB, SHARED_CHARACTER  # noqa: E402

# 造一个"改造之前"的库：chat_records 没有 speaker 列（列结构与线上 1.9.3 一致）
_LEGACY_DDL = """
CREATE TABLE chat_records (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    role            TEXT    NOT NULL,
    content         TEXT    NOT NULL,
    tool_name       TEXT    DEFAULT NULL,
    memory_processed INTEGER DEFAULT 0,
    created_at      TEXT    DEFAULT (datetime('now', 'localtime')),
    tool_call_id    TEXT    DEFAULT NULL,
    topic_id        INTEGER DEFAULT NULL,
    token_count     INTEGER DEFAULT 0
);
"""
_legacy_dir = _TMP / "legacy"
_legacy_dir.mkdir()
_con = sqlite3.connect(str(_legacy_dir / "chat_records.db"))
_con.executescript(_LEGACY_DDL)
_con.execute("INSERT INTO chat_records (role, content) VALUES ('assistant', '改造前的老消息')")
_con.commit()
_con.close()

_dbmod.DB_DIR = _legacy_dir
_old = ChatRecordsDB()
_cols = {r["name"] for r in _old._conn.execute("PRAGMA table_info(chat_records)")}
check("★旧库启动后自动补出 speaker 列", "speaker" in _cols, f"列={sorted(_cols)}")
_row = _old._conn.execute(
    "SELECT speaker FROM chat_records WHERE content='改造前的老消息'").fetchone()
check("★老数据 speaker 为 NULL（按'自己的发言'处理）", _row["speaker"] is None)

# 幂等：重复跑迁移 / 重复实例化（= 反复启动）都不许抛
try:
    _old._init_tables()
    _old._init_tables()
    _again = ChatRecordsDB()
    _cols2 = {r["name"] for r in _again._conn.execute("PRAGMA table_info(chat_records)")}
    _idem = "speaker" in _cols2
except Exception as e:                       # noqa: BLE001
    _idem = False
    print("    迁移重复执行抛异常:", e)
check("★★speaker 迁移幂等：重复执行/重复启动不报错", _idem)
_again.close()
_old.close()

# 全新库：建表就该带 speaker
_fresh_dir = _TMP / "fresh"
_fresh_dir.mkdir()
_dbmod.DB_DIR = _fresh_dir
_fresh = ChatRecordsDB()
_fcols = {r["name"] for r in _fresh._conn.execute("PRAGMA table_info(chat_records)")}
check("★全新库的 chat_records 自带 speaker 列", "speaker" in _fcols)

_tid = _fresh.create_topic("", SHARED_CHARACTER)
_fresh.add("assistant", "我说的话", topic_id=_tid, speaker="甲")
_fresh.add("user", "主人说的话", topic_id=_tid)
_rows = _fresh.get_topic_messages(_tid)
check("★add(speaker=...) 真的落库",
      _rows[0]["speaker"] == "甲" and _rows[1]["speaker"] is None,
      f"{[(r['role'], r['speaker']) for r in _rows]}")
_fresh.close()

# ── 2. 共享话题：两个形象名 = 同一个 topic_id ──
print("\n== 2. 所有形象共用同一个话题 ==")
_dbmod.DB_DIR = _TMP / "shared"
from src.core.session import ChatSession, build_shared_context, RECENT_CONTEXT_LIMIT  # noqa: E402

_s = ChatSession()
_s._config_character = lambda: "甲"
_s.set_character("甲")
_tid_a = _s.topic_id
check("set_character 建立共享话题", _tid_a is not None)
_s.set_character("乙")
_tid_b = _s.topic_id
check("★★甲乙两个形象拿到**同一个** topic_id", _tid_a == _tid_b and _tid_a is not None,
      f"甲={_tid_a} 乙={_tid_b}")
check("话题挂在共享标记下",
      _s.db.get_active_topic(SHARED_CHARACTER)["id"] == _tid_a)
check("★不再按形象名建话题",
      _s.db.list_character_topics("甲") == [] and _s.db.list_character_topics("乙") == [])

# 切换形象不许把正在进行的对话冲掉
_s.messages.append({"role": "user", "content": "共享工作台的第一句"})
_s.set_character("甲")
check("★★切换形象不清空/不重载共享上下文",
      any("共享工作台的第一句" in (m.get("content") or "") for m in _s.messages)
      and _s.topic_id == _tid_a)
check("形象名仍然跟着切（persona / 谁在回复要用）", _s.character == "甲")

# ── 3. 上下文装配规则 ──
print("\n== 3. 上下文装配（最近的原文 + 更早的摘要）==")
_msgs = [
    {"role": "system", "content": "[系统上下文] 现在是下午"},
    {"role": "user", "content": "主人问了一句"},
    {"role": "assistant", "content": "这是甲说的", "speaker": "甲"},
    {"role": "assistant", "content": "这是乙说的", "speaker": "乙"},
    {"role": "assistant", "content": "改造前没有 speaker 的老发言"},
    {"role": "tool", "tool_call_id": "orphan-1", "content": "孤儿工具结果"},
]
_out = build_shared_context(_msgs, "甲")
_pairs = [(m["role"], m["content"]) for m in _out]
check("★自己的发言 = assistant 且**无前缀**",
      ("assistant", "这是甲说的") in _pairs)
check("★★别人的发言 = user 且带 [名字]： 前缀",
      ("user", "[乙]：这是乙说的") in _pairs)
check("★speaker 为空（历史遗留）= 当作自己说的，assistant 无前缀",
      ("assistant", "改造前没有 speaker 的老发言") in _pairs)
check("user 消息原样保留", ("user", "主人问了一句") in _pairs)
check("★孤儿 tool 消息被跳过",
      all(m.get("role") != "tool" for m in _out))
check("system 消息始终保留", ("system", "[系统上下文] 现在是下午") in _pairs)
check("system 消息排在对话前面", _out[0]["role"] == "system")

# 本轮真实工具调用链（assistant.tool_calls + 配对的 tool 结果）必须整条留下，否则 400
_msgs_tc = [
    {"role": "assistant", "content": None,
     "tool_calls": [{"id": "t1", "type": "function",
                     "function": {"name": "web_search", "arguments": "{}"}}]},
    {"role": "tool", "tool_call_id": "t1", "content": "搜索结果"},
]
_out_tc = build_shared_context(_msgs_tc, "甲")
check("★★配对的 tool 结果不被跳过（否则工具链断掉 → API 400）",
      [m["role"] for m in _out_tc] == ["assistant", "tool"])

# 超过 N 条 → 更早的折成一条 system 摘要
check("N = 20（用户定稿）", RECENT_CONTEXT_LIMIT == 20, str(RECENT_CONTEXT_LIMIT))
_many = [{"role": "system", "content": "[系统上下文] 背景"}]
for _i in range(25):
    _many.append({"role": "user", "content": f"第{_i}问"})
    _many.append({"role": "assistant", "content": f"第{_i}答", "speaker": "甲"})
_deep = build_shared_context(_many, "甲", summary="前面聊了天气和 PPT")
_deep_convo = [m for m in _deep if m["role"] != "system"]
_deep_sys = [m for m in _deep if m["role"] == "system"]
check("★超过 N 条时只留最近 N 条原文", len(_deep_convo) == RECENT_CONTEXT_LIMIT,
      f"留下 {len(_deep_convo)} 条")
check("★更早的折成**一条** system 摘要",
      len(_deep_sys) == 2 and "[更早的对话摘要，供你了解来龙去脉，不用复述]" in _deep_sys[-1]["content"]
      and "前面聊了天气和 PPT" in _deep_sys[-1]["content"],
      f"system 条数={len(_deep_sys)}")
_expect_tail = [m for m in _many if m["role"] != "system"][-RECENT_CONTEXT_LIMIT:]
check("★留下的确实是**最近**那几条",
      [m["content"] for m in _deep_convo] == [m["content"] for m in _expect_tail],
      f"{_deep_convo[0]['content']} … {_deep_convo[-1]['content']}")
_deep2 = build_shared_context(_many, "甲")
_sys2 = [m for m in _deep2 if m["role"] == "system"]
check("没有现成摘要时只注明'已省略'（不新造会调 LLM 的同步轮子）",
      "已省略" in _sys2[-1]["content"], _sys2[-1]["content"])

# 端到端：落库(speaker) → 重载 → 换形象 → 拼上下文
_topic = _s.topic_id
_s.db.add_chat_history("assistant", "甲留下的发言", topic_id=_topic, speaker="甲")
_s._load_topic_messages(_topic)
_s.set_character("乙")
_llm_msgs = _s._build_llm_messages()
check("★端到端：库里甲的发言，切到乙之后被拼成 user + [甲]：",
      any(m["role"] == "user" and m["content"] == "[甲]：甲留下的发言" for m in _llm_msgs),
      str([(m["role"], (m.get("content") or "")[:14]) for m in _llm_msgs]))

# ── 4. 系统提示词里必须带"共享工作台"那一段 ──
print("\n== 4. 系统提示词的共享工作台标注 ==")
from src.core.llm_client import ChatClient  # noqa: E402
from src.utils.config import get_config  # noqa: E402


def _prompt_for(personality: str) -> str:
    """按既有的"绕开 __init__"套路真跑一遍 _build_system_prompt"""
    cfg = get_config()
    cfg.pet.personality_prompt = personality
    c = ChatClient.__new__(ChatClient)
    c.tools = []
    c._cached_prompt = None
    c._cached_prompt_hash = None
    return ChatClient._build_system_prompt(c)


_cfg = get_config()
_bak = (_cfg.pet.personality_prompt, _cfg.pet.reply_style, _cfg.pet.emoji_level)
try:
    _p_custom = _prompt_for("你是测试角色。")      # 自定义人设分支
    _p_default = _prompt_for("")                   # 默认模板分支
finally:
    (_cfg.pet.personality_prompt, _cfg.pet.reply_style, _cfg.pet.emoji_level) = _bak

for _tag, _p in (("自定义人设", _p_custom), ("默认模板", _p_default)):
    check(f"★{_tag}分支的系统提示词含共享工作台标注",
          "多人共享的工作台" in _p
          and "不是你" in _p
          and "不要给自己加 [名字]： 前缀" in _p)

# 缓存键：改了标注版本号 → 必须重建提示词
_cc = ChatClient.__new__(ChatClient)
_cc.tools = []
_cc._cached_prompt = None
_cc._cached_prompt_hash = None
_p1 = ChatClient._build_system_prompt(_cc)
_p1_again = ChatClient._build_system_prompt(_cc)
_tag_old = ChatClient._SHARED_CONTEXT_TAG
try:
    ChatClient._SHARED_CONTEXT_TAG = _tag_old + "_test"
    _p2 = ChatClient._build_system_prompt(_cc)
finally:
    ChatClient._SHARED_CONTEXT_TAG = _tag_old
check("★标注版本在 cache_key 里（改了它 → 提示词重建）",
      _p1 is _p1_again and _p1 is not _p2 and "多人共享的工作台" in _p2)

# 收尾
try:
    _s.db.close()
except Exception:
    pass
shutil.rmtree(_TMP, ignore_errors=True)

print(f"\n==== 结果: {len(PASS)} PASS / {len(FAIL)} FAIL ====")
for _f in FAIL:
    print("  FAIL:", _f)
sys.exit(1 if FAIL else 0)
