"""
数据库模块（重构 v2.0）
- 纯 SQLite，去 MySQL 依赖
- 四个独立 DB 文件：raw_monitor_data / chat_records / long_term_memories
- WAL 模式 + 每模块独立连接
- FTS5 全文搜索内建在业务 DB 中
"""
import sqlite3
import threading
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

_log = logging.getLogger("Database")

BASE_DIR = Path(__file__).resolve().parent.parent.parent
DB_DIR = BASE_DIR / "data" / "DBs"


def _ensure_db_dir():
    DB_DIR.mkdir(parents=True, exist_ok=True)
    (DB_DIR / "screenphotos").mkdir(exist_ok=True)
    (DB_DIR / "audio").mkdir(exist_ok=True)


# ====================================================================
# 连接工厂：每个模块拿独立连接，WAL 模式
# ====================================================================

def _connect(db_name: str) -> sqlite3.Connection:
    """创建 SQLite 连接，WAL 模式，check_same_thread=False"""
    _ensure_db_dir()
    conn = sqlite3.connect(str(DB_DIR / db_name), check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA cache_size=-8000")  # 8MB 缓存
    conn.row_factory = sqlite3.Row
    return conn


# ====================================================================
# 原始监控数据 DB（raw_monitor_data.db）
#   表1: raw_events          — 监控直接采集的原始数据
#   表2: monitoring_events   — 清洗/LLM 总结后的结构化事件
#   表3: fts_raw_events      — FTS5 全文搜索
# ====================================================================

_RAW_DDL = """
CREATE TABLE IF NOT EXISTS raw_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source      TEXT    NOT NULL,    -- keyboard/clipboard/screen/audio/window/process/system
    type        TEXT    DEFAULT '',  -- 子类型：move/click/scroll/start/stop 等
    content     TEXT    DEFAULT '',  -- 核心内容（截断后 ≤2000 字符）
    file_path   TEXT    DEFAULT NULL,-- 关联文件路径（截图/音频文件路径）
    metadata    TEXT    DEFAULT '{}',-- JSON 扩展字段
    cleaned     INTEGER DEFAULT 0,  -- 0=未清洗, 1=已清洗, -1=清洗失败
    created_at  TEXT    DEFAULT (datetime('now', 'localtime'))
);

CREATE INDEX IF NOT EXISTS idx_raw_source   ON raw_events(source);
CREATE INDEX IF NOT EXISTS idx_raw_cleaned  ON raw_events(cleaned);
CREATE INDEX IF NOT EXISTS idx_raw_created  ON raw_events(created_at);

-- FTS5 全文搜索（原始事件）
CREATE VIRTUAL TABLE IF NOT EXISTS fts_raw_events
USING fts5(source, type, content, tokenize='unicode61 remove_diacritics 2');

CREATE TABLE IF NOT EXISTS monitoring_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    time_start  TEXT    DEFAULT NULL,    -- 事件开始时间
    time_end    TEXT    DEFAULT NULL,    -- 事件结束时间（可为空）
    event       TEXT    NOT NULL,        -- 事件类型标签（LLM 生成的简短标签）
    detail      TEXT    DEFAULT '',      -- 事件详细描述
    confidence  INTEGER DEFAULT 100,     -- 推测度 0-100
    source      TEXT    DEFAULT 'pipeline', -- 来源: pipeline/chat/manual
    memory_ref  INTEGER DEFAULT 0,       -- 是否已写入长期记忆 (0=未处理, 1=已处理, -1=失败)
    created_at  TEXT    DEFAULT (datetime('now', 'localtime'))
);

CREATE INDEX IF NOT EXISTS idx_me_memref    ON monitoring_events(memory_ref);
CREATE INDEX IF NOT EXISTS idx_me_created   ON monitoring_events(created_at);

-- FTS5 全文搜索（结构化事件）
CREATE VIRTUAL TABLE IF NOT EXISTS fts_monitoring_events
USING fts5(event, detail, tokenize='unicode61 remove_diacritics 2');
"""

_RAW_TRIGGERS = """
-- 同步 raw_events → FTS（仅 INSERT/DELETE，UPDATE 不触发 FTS——cleaned 字段变化无需索引）
CREATE TRIGGER IF NOT EXISTS tr_raw_events_ai AFTER INSERT ON raw_events BEGIN
    INSERT INTO fts_raw_events(rowid, source, type, content)
    VALUES (NEW.id, NEW.source, NEW.type, NEW.content);
END;

-- ★ 删除直接用 DELETE：本机 SQLite 对 FTS5 的 'delete' 特殊命令会报 "SQL logic error"
CREATE TRIGGER IF NOT EXISTS tr_raw_events_ad AFTER DELETE ON raw_events BEGIN
    DELETE FROM fts_raw_events WHERE rowid = OLD.id;
END;

-- 同步 monitoring_events → FTS（仅 INSERT/DELETE）
CREATE TRIGGER IF NOT EXISTS tr_me_ai AFTER INSERT ON monitoring_events BEGIN
    INSERT INTO fts_monitoring_events(rowid, event, detail)
    VALUES (NEW.id, NEW.event, NEW.detail);
END;

CREATE TRIGGER IF NOT EXISTS tr_me_ad AFTER DELETE ON monitoring_events BEGIN
    DELETE FROM fts_monitoring_events WHERE rowid = OLD.id;
END;
"""


class RawMonitorDB:
    """原始监控数据 + 结构化事件"""

    def __init__(self):
        self._conn = _connect("raw_monitor_data.db")
        self._lock = threading.Lock()
        self._init_tables()
        # 写入计数器（供 summary 触发用）
        self.write_counter = 0

    def _init_tables(self):
        with self._lock:
            self._conn.executescript(_RAW_DDL)
            # 删除旧触发器（防止旧版本残留）
            self._conn.executescript("""
                DROP TRIGGER IF EXISTS tr_raw_events_ai;
                DROP TRIGGER IF EXISTS tr_raw_events_ad;
                DROP TRIGGER IF EXISTS tr_raw_events_au;
                DROP TRIGGER IF EXISTS tr_me_ai;
                DROP TRIGGER IF EXISTS tr_me_ad;
                DROP TRIGGER IF EXISTS tr_me_au;
            """)
            try:
                self._conn.executescript(_RAW_TRIGGERS)
            except sqlite3.OperationalError:
                pass
            self._conn.commit()

    # ---- raw_events ----

    def add_raw_event(self, source: str, type_: str = "",
                      content: str = "", file_path: str = None,
                      metadata: dict = None) -> int:
        """写入原始事件，返回 id"""
        import json as _json
        # 内容截断
        if len(content) > 2000:
            content = content[:2000]
        with self._lock:
            cur = self._conn.execute(
                """INSERT INTO raw_events (source, type, content, file_path, metadata)
                   VALUES (?, ?, ?, ?, ?)""",
                (source, type_, content, file_path,
                 _json.dumps(metadata or {}, ensure_ascii=False)))
            self._conn.commit()
            self.write_counter += 1
            return cur.lastrowid

    def get_uncleaned(self, limit: int = 100) -> list[dict]:
        """获取未清洗的原始事件"""
        with self._lock:
            rows = self._conn.execute(
                """SELECT * FROM raw_events WHERE cleaned=0
                   ORDER BY created_at ASC LIMIT ?""",
                (limit,)).fetchall()
        return [dict(r) for r in rows]

    def count_uncleaned(self) -> int:
        """★ 1.8.9: 待清洗**总数**（不受 limit 截断）。

        以前心跳用 len(get_uncleaned(100)) 当积压，永远显示 100，
        看上去像"怎么清都清不完"。
        """
        with self._lock:
            return self._conn.execute(
                "SELECT COUNT(*) FROM raw_events WHERE cleaned=0").fetchone()[0]

    def mark_cleaned(self, ids: list[int], status: int = 1):
        """批量标记清洗状态"""
        if not ids:
            return
        with self._lock:
            placeholders = ",".join("?" * len(ids))
            self._conn.execute(
                f"UPDATE raw_events SET cleaned=? WHERE id IN ({placeholders})",
                [status] + ids)
            self._conn.commit()

    def get_count(self) -> int:
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) FROM raw_events").fetchone()[0]

    def delete_oldest(self, count: int):
        """删除最旧的 N 条记录"""
        with self._lock:
            self._conn.execute(
                "DELETE FROM raw_events WHERE id IN "
                "(SELECT id FROM raw_events ORDER BY id ASC LIMIT ?)",
                (count,))
            self._conn.commit()

    def enforce_cap(self, cap: int = 1000):
        """强制上限：超过 cap 时删除最旧的"""
        current = self.get_count()
        if current > cap:
            to_delete = current - cap
            self.delete_oldest(to_delete)
            _log.info(f"[RawMonitorDB] 上限清理: 删除 {to_delete} 条, 剩余 {cap} 条")

    # ---- monitoring_events ----

    def add_event(self, time_start: str = None, time_end: str = None,
                  event: str = "", detail: str = "",
                  confidence: int = 100, source: str = "pipeline") -> int:
        """写入结构化事件"""
        with self._lock:
            cur = self._conn.execute(
                """INSERT INTO monitoring_events (time_start, time_end, event, detail,
                   confidence, source) VALUES (?, ?, ?, ?, ?, ?)""",
                (time_start, time_end, event, detail, confidence, source))
            self._conn.commit()
            return cur.lastrowid

    def get_unprocessed_events(self, limit: int = 10) -> list[dict]:
        """获取未写入长期记忆的事件"""
        with self._lock:
            rows = self._conn.execute(
                """SELECT * FROM monitoring_events WHERE memory_ref=0
                   ORDER BY id ASC LIMIT ?""",
                (limit,)).fetchall()
        return [dict(r) for r in rows]

    def mark_memory_processed(self, ids: list[int], status: int = 1):
        if not ids:
            return
        with self._lock:
            placeholders = ",".join("?" * len(ids))
            self._conn.execute(
                f"UPDATE monitoring_events SET memory_ref=? WHERE id IN ({placeholders})",
                [status] + ids)
            self._conn.commit()

    # ---- FTS 搜索 ----

    def close(self):
        if self._conn:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None


# ====================================================================
# 聊天记录 DB（chat_records.db）
# ====================================================================

# ★ 1.9.3（用户 2026-10-04 拍板）：**共享工作台**。
#   所有形象共用**同一个**话题/上下文，切换形象只是换 persona 与"谁在回复"；
#   聊天窗口像群聊一样保留全部历史。
#   话题表里的 `character` 列从此固定写这个标识（不再写形象名），
#   消息表用 `speaker` 列记录"这句话是谁说的"，供拼上下文时加 [名字]： 前缀。
SHARED_CHARACTER = "__shared__"

_CHAT_DDL = """
CREATE TABLE IF NOT EXISTS topics (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    character       TEXT    DEFAULT '',       -- ★ 1.6.0: 所属角色；★ 1.9.3: 共享工作台固定为 SHARED_CHARACTER
    title           TEXT    DEFAULT '',       -- 话题标题（由第一轮对话自动生成）
    title_set       INTEGER DEFAULT 0,       -- 0=标题未设置, 1=已设置
    message_count   INTEGER DEFAULT 0,       -- 消息数量
    total_tokens    INTEGER DEFAULT 0,       -- 估算总 token 数
    max_tokens      INTEGER DEFAULT 168000,  -- 上下文窗口上限 (deepseek-chat: 128k, 其他: 168k)
    status          TEXT    DEFAULT 'active', -- ★ 1.6.0: active(当前) / archived(旧话题)
    summary         TEXT    DEFAULT '',       -- ★ 1.6.0: 该话题结束时的总结（续接用）
    created_at      TEXT    DEFAULT (datetime('now', 'localtime')),
    updated_at      TEXT    DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS chat_records (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    topic_id        INTEGER DEFAULT NULL,    -- ★ 所属话题 ID
    role            TEXT    NOT NULL,         -- user / assistant / system / tool
    content         TEXT    NOT NULL,
    token_count     INTEGER DEFAULT 0,       -- 估算 token 数
    tool_name       TEXT    DEFAULT NULL,
    tool_call_id    TEXT    DEFAULT NULL,     -- OpenAI tool_call_id（用于重建 tool_calls 链路）
    memory_processed INTEGER DEFAULT 0,      -- 0=未判断, 1=已处理, -1=失败
    speaker         TEXT    DEFAULT NULL,     -- ★ 1.9.3: 谁说的（assistant 存形象名；user/tool 留空）
    ctx_exclude     INTEGER DEFAULT 0,        -- ★ 1.9.3 第 60 轮: 1=只在窗口显示，**不进主 LLM 上下文**
    created_at      TEXT    DEFAULT (datetime('now', 'localtime'))
);

CREATE INDEX IF NOT EXISTS idx_chat_created ON chat_records(created_at);
CREATE INDEX IF NOT EXISTS idx_chat_memproc ON chat_records(memory_processed);
CREATE INDEX IF NOT EXISTS idx_chat_topic   ON chat_records(topic_id);
CREATE INDEX IF NOT EXISTS idx_topics_updated ON topics(updated_at);

CREATE VIRTUAL TABLE IF NOT EXISTS fts_chat_records
USING fts5(role, content, tool_name, tokenize='unicode61 remove_diacritics 2');
"""

_CHAT_TRIGGERS = """
-- 仅 INSERT/DELETE 同步 FTS（memory_processed 更新无需触发）
CREATE TRIGGER IF NOT EXISTS tr_chat_ai AFTER INSERT ON chat_records BEGIN
    INSERT INTO fts_chat_records(rowid, role, content, tool_name)
    VALUES (NEW.id, NEW.role, NEW.content, NEW.tool_name);
END;

-- ★ 删除直接用 DELETE：本机 SQLite 对 FTS5 的 'delete' 特殊命令会报 "SQL logic error"
CREATE TRIGGER IF NOT EXISTS tr_chat_ad AFTER DELETE ON chat_records BEGIN
    DELETE FROM fts_chat_records WHERE rowid = OLD.id;
END;
"""


class ChatRecordsDB:
    """聊天记录 + 话题管理"""

    def __init__(self):
        self._conn = _connect("chat_records.db")
        self._lock = threading.Lock()
        self._init_tables()

    def _init_tables(self):
        with self._lock:
            # ★ 先迁移旧表（兼容旧版本 DB 文件），再执行 DDL
            #    否则 DDL 中的 CREATE INDEX ON topic_id 会在旧表上失败
            for col, col_type in [("topic_id", "INTEGER DEFAULT NULL"),
                                  ("token_count", "INTEGER DEFAULT 0")]:
                try:
                    self._conn.execute(f"ALTER TABLE chat_records ADD COLUMN {col} {col_type}")
                except sqlite3.OperationalError:
                    pass  # 列已存在
            # ★ 1.9.3: 共享工作台 —— chat_records 增加 speaker 列（这句话是谁说的）。
            #   这里**先 PRAGMA table_info 判定再 ALTER**：本函数每次启动都会跑，
            #   必须幂等 —— 列已存在就跳过，重复执行绝不报错。
            try:
                _cols = {r["name"] for r in
                         self._conn.execute("PRAGMA table_info(chat_records)").fetchall()}
            except sqlite3.OperationalError:
                _cols = set()   # 表还不存在（全新库）：交给下面的 DDL 建带 speaker 的表
            if "speaker" not in _cols:
                try:
                    self._conn.execute(
                        "ALTER TABLE chat_records ADD COLUMN speaker TEXT DEFAULT NULL")
                except sqlite3.OperationalError:
                    pass  # 并发/重复迁移：列已存在
            # ★ 1.9.3 第 60 轮：**子代理中间发言**要落库给"人"看（重启后窗口里还在），
            #   但**绝不能**跟着 speaker 一起被 `_load_topic_messages` 读回、再由
            #   `build_shared_context` 以 `[名字]：…` 灌进主 LLM 上下文
            #   （那等于把子代理整段会话喂给主模型，违反"只回传交付物 / token 有界"）。
            #   所以加这一列当"**不进上下文**"的标记：
            #     · 1 = 只给人看（窗口渲染照旧，拼上下文时跳过）；
            #     · 0 = 正常（默认，老数据全是 0，行为一字不变）。
            #   迁移口径与 speaker 完全一致：先 PRAGMA 判定再 ALTER，幂等、重复不报错。
            if "ctx_exclude" not in _cols:
                try:
                    self._conn.execute(
                        "ALTER TABLE chat_records ADD COLUMN ctx_exclude INTEGER DEFAULT 0")
                except sqlite3.OperationalError:
                    pass  # 并发/重复迁移：列已存在
            # ★ 1.6.0: topics 表迁移（角色 / 状态 / 总结）
            for col, col_type in [("character", "TEXT DEFAULT ''"),
                                  ("status", "TEXT DEFAULT 'active'"),
                                  ("summary", "TEXT DEFAULT ''")]:
                try:
                    self._conn.execute(f"ALTER TABLE topics ADD COLUMN {col} {col_type}")
                except sqlite3.OperationalError:
                    pass  # 列已存在
            self._conn.executescript(_CHAT_DDL)
            self._conn.executescript("""
                DROP TRIGGER IF EXISTS tr_chat_ai;
                DROP TRIGGER IF EXISTS tr_chat_ad;
                DROP TRIGGER IF EXISTS tr_chat_au;
            """)
            try:
                self._conn.executescript(_CHAT_TRIGGERS)
            except sqlite3.OperationalError:
                pass
            self._conn.commit()

    # ── 话题管理 ──

    def create_topic(self, title: str = "", character: str = "") -> int:
        """创建新话题，返回 topic_id"""
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO topics (title, title_set, character) VALUES (?, ?, ?)",
                (title, 1 if title else 0, character or ""))
            self._conn.commit()
            return cur.lastrowid

    # ── ★ 1.6.0: 一个角色一个窗口（连续上下文）──

    def get_active_topic(self, character: str) -> Optional[dict]:
        """获取某角色当前活跃话题（无则 None）"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM topics WHERE character=? AND status='active' "
                "ORDER BY id DESC LIMIT 5",
                (character or "",)).fetchall()
            if not rows:
                return None
            if len(rows) > 1:
                # 容错：同一角色出现多个 active（历史遗留）→ 只保留最新的，其余归档
                for r in rows[1:]:
                    try:
                        self._conn.execute(
                            "UPDATE topics SET status='archived', "
                            "updated_at=datetime('now','localtime') WHERE id=?", (r["id"],))
                    except Exception:
                        pass
                self._conn.commit()
            return dict(rows[0])

    def list_character_topics(self, character: str, limit: int = 50) -> list[dict]:
        """列出某角色的所有话题（新→旧）"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM topics WHERE character=? ORDER BY id DESC LIMIT ?",
                (character or "", limit)).fetchall()
        return [dict(r) for r in rows]

    def archive_topic(self, topic_id: int, summary: str = ""):
        """归档话题并写入总结（旧话题归为新话题上下文）"""
        with self._lock:
            self._conn.execute(
                "UPDATE topics SET status='archived', summary=?, "
                "updated_at=datetime('now','localtime') WHERE id=?",
                (summary or "", topic_id))
            self._conn.commit()

    def set_topic_summary(self, topic_id: int, summary: str):
        """写入/更新话题总结"""
        with self._lock:
            self._conn.execute(
                "UPDATE topics SET summary=?, updated_at=datetime('now','localtime') WHERE id=?",
                (summary or "", topic_id))
            self._conn.commit()

    def get_topic_summary(self, topic_id: int) -> str:
        with self._lock:
            row = self._conn.execute(
                "SELECT summary FROM topics WHERE id=?", (topic_id,)).fetchone()
        return (row["summary"] or "") if row else ""

    def get_last_archived_summary(self, character: str, before_topic_id: int = 0) -> str:
        """取该角色最近一次归档话题的总结（用于新话题续接上下文）"""
        with self._lock:
            row = self._conn.execute(
                "SELECT summary FROM topics WHERE character=? AND status='archived' "
                "AND summary != '' AND id < ? ORDER BY id DESC LIMIT 1",
                (character or "", before_topic_id or 999999999)).fetchone()
        return (row["summary"] or "") if row else ""

    def count_character_messages(self, character: str) -> int:
        """统计某角色的历史消息总数"""
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM chat_records c JOIN topics t ON c.topic_id=t.id "
                "WHERE t.character=?", (character or "",)).fetchone()
        return int(row[0]) if row else 0

    def get_character_history_messages(self, character: str,
                                       exclude_topic: int = 0,
                                       limit: int = 200) -> list[dict]:
        """★ 1.6.0: 取某角色「除当前话题以外」的历史消息（旧话题，按时间正序）。

        用于「开启新话题后聊天界面保留旧话题」——新话题为空，旧内容仍在界面上。
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT c.* FROM chat_records c JOIN topics t ON c.topic_id = t.id "
                "WHERE t.character=? AND c.topic_id != ? AND c.role IN ('user','assistant') "
                "ORDER BY c.id DESC LIMIT ?",
                (character or "", exclude_topic or -1, max(1, limit))).fetchall()
        return [dict(r) for r in reversed(rows)]

    def set_topic_title(self, topic_id: int, title: str):
        """设置话题标题"""
        with self._lock:
            self._conn.execute(
                "UPDATE topics SET title=?, title_set=1, updated_at=datetime('now','localtime') WHERE id=?",
                (title, topic_id))
            self._conn.commit()

    def get_topic_title(self, topic_id: int) -> str:
        """获取话题标题"""
        with self._lock:
            row = self._conn.execute(
                "SELECT title FROM topics WHERE id=?", (topic_id,)).fetchone()
            return row["title"] if row else ""

    def list_topics(self) -> list[dict]:
        """列出所有话题"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM topics ORDER BY updated_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def delete_topic(self, topic_id: int):
        """删除话题及其所有聊天记录（FTS 由 tr_chat_ad 触发器自动同步）"""
        with self._lock:
            self._conn.execute("DELETE FROM chat_records WHERE topic_id=?", (topic_id,))
            self._conn.execute("DELETE FROM topics WHERE id=?", (topic_id,))
            self._conn.commit()

    def get_topic_token_count(self, topic_id: int) -> int:
        """获取话题当前估算 token 总数"""
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(SUM(token_count), 0) FROM chat_records WHERE topic_id=?",
                (topic_id,)).fetchone()
            total = row[0] if row else 0
            # 更新 topics 表
            self._conn.execute(
                "UPDATE topics SET total_tokens=?, updated_at=datetime('now','localtime') WHERE id=?",
                (total, topic_id))
            self._conn.commit()
            return total

    def is_topic_full(self, topic_id: int) -> bool:
        """检查话题是否超出上下文窗口上限"""
        with self._lock:
            row = self._conn.execute(
                "SELECT total_tokens, max_tokens FROM topics WHERE id=?", (topic_id,)).fetchone()
            if not row:
                return False
            return row["total_tokens"] >= row["max_tokens"]

    # ── 聊天记录（扩展支持 topic_id）──

    def add(self, role: str, content: str, tool_name: str = None,
            tool_call_id: str = None, topic_id: int = None,
            speaker: str = None, ctx_exclude: int = 0) -> int:
        """添加聊天记录，返回 id。自动估算 token 数。

        ★ 1.9.3: speaker = 这句话是谁说的。assistant 消息存当前形象名；
        user / tool 消息留空（None）。拼上下文时据此决定要不要加 [名字]： 前缀。
        ★ 1.9.3 第 60 轮: ctx_exclude = 1 → **只在窗口里显示，不进主 LLM 上下文**
        （子代理的中间发言走这条；交付物仍然照常进上下文）。
        """
        token_count = ChatRecordsDB._estimate_tokens(content)
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO chat_records (topic_id, role, content, token_count, tool_name, tool_call_id, speaker, ctx_exclude) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (topic_id, role, content, token_count, tool_name, tool_call_id,
                 (speaker or None), 1 if ctx_exclude else 0))
            # 更新话题计数
            if topic_id:
                self._conn.execute(
                    "UPDATE topics SET message_count=message_count+1, total_tokens=total_tokens+?, updated_at=datetime('now','localtime') WHERE id=?",
                    (token_count, topic_id))
            self._conn.commit()
            return cur.lastrowid

    def get_unprocessed(self) -> list[dict]:
        """获取未判断是否写入长期记忆的聊天记录"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM chat_records WHERE memory_processed=0 ORDER BY id ASC"
            ).fetchall()
        return [dict(r) for r in rows]

    def mark_processed(self, ids: list[int], status: int = 1):
        if not ids:
            return
        with self._lock:
            placeholders = ",".join("?" * len(ids))
            self._conn.execute(
                f"UPDATE chat_records SET memory_processed=? WHERE id IN ({placeholders})",
                [status] + ids)
            self._conn.commit()

    def get_recent(self, limit: int = 50, topic_id: int = None) -> list[dict]:
        """获取最近的聊天记录，可按话题过滤"""
        with self._lock:
            if topic_id is not None:
                rows = self._conn.execute(
                    "SELECT * FROM chat_records WHERE topic_id=? ORDER BY id DESC LIMIT ?",
                    (topic_id, limit)).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM chat_records ORDER BY id DESC LIMIT ?",
                    (limit,)).fetchall()
        return list(reversed([dict(r) for r in rows]))

    def get_topic_messages(self, topic_id: int) -> list[dict]:
        """获取话题的所有消息（正序）"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM chat_records WHERE topic_id=? ORDER BY id ASC",
                (topic_id,)).fetchall()
        return [dict(r) for r in rows]

    def search(self, keyword: str, limit: int = 20) -> list[dict]:
        with self._lock:
            try:
                rows = self._conn.execute(
                    """SELECT c.* FROM fts_chat_records f
                       JOIN chat_records c ON f.rowid = c.id
                       WHERE fts_chat_records MATCH ? ORDER BY rank LIMIT ?""",
                    (keyword, limit)).fetchall()
                return [dict(r) for r in rows]
            except Exception:
                return []

    @staticmethod
    def _estimate_tokens(text: str) -> int:
        """简单 token 估算：中文约 1 字符=1 token，英文约 1 词=1 token"""
        if not text:
            return 0
        try:
            # 中文字符
            chinese = sum(1 for c in text if '\u4e00' <= c <= '\u9fff' or '\u3000' <= c <= '\u303f')
            # 英文单词
            english_text = ''.join(c for c in text if c.isascii())
            english_words = len(english_text.split()) if english_text else 0
            # 其他字符（标点、数字等）
            other = len(text) - chinese - len([c for c in text if c.isascii() and c != ' '])
            return chinese + english_words + max(0, other // 2)
        except Exception:
            return len(text) // 2

    def close(self):
        if self._conn:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None


# ====================================================================
# 长期记忆 DB（long_term_memories.db）
# ====================================================================

_LTM_DDL = """
CREATE TABLE IF NOT EXISTS long_term_memories (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    variable_name   TEXT    NOT NULL UNIQUE, -- K-V 模型的 key（如 user_name, preferred_language）
    value           TEXT    NOT NULL,        -- 变量的值
    category        TEXT    DEFAULT 'other',
    confidence      REAL    DEFAULT 0.5,     -- 0.0-1.0
    source          TEXT    DEFAULT 'chat',  -- chat / monitoring / manual
    created_at      TEXT    DEFAULT (datetime('now', 'localtime')),
    updated_at      TEXT    DEFAULT (datetime('now', 'localtime')),
    update_count    INTEGER DEFAULT 1,
    access_count    INTEGER DEFAULT 0,       -- ★ 1.7.3: 被注入上下文的次数（衰减依据）
    last_accessed   TEXT    DEFAULT NULL,    -- ★ 1.7.3: 最近一次注入上下文时间
    decay_score     REAL    DEFAULT 1.0       -- ★ 1.7.3: 动态衰减分（summarize 时重算）
);

CREATE INDEX IF NOT EXISTS idx_ltm_category ON long_term_memories(category);
CREATE INDEX IF NOT EXISTS idx_ltm_source   ON long_term_memories(source);
CREATE INDEX IF NOT EXISTS idx_ltm_updated  ON long_term_memories(updated_at);

CREATE VIRTUAL TABLE IF NOT EXISTS fts_long_term_memories
USING fts5(variable_name, value, tokenize='unicode61 remove_diacritics 2');
"""

_LTM_TRIGGERS = """
-- FTS 同步全部在应用层手动处理，不依赖触发器（避免 FTS5 trigger 兼容问题）
"""


class LongTermMemoriesDB:
    """长期记忆 — K-V 模型，一个 variable_name 唯一"""

    def __init__(self):
        self._conn = _connect("long_term_memories.db")
        self._lock = threading.Lock()
        self._init_tables()

    def _init_tables(self):
        with self._lock:
            # ★ 1.7.3: 迁移旧表（兼容旧 DB 文件），加 access_count/last_accessed/decay_score
            for col, col_type in [("access_count", "INTEGER DEFAULT 0"),
                                  ("last_accessed", "TEXT DEFAULT NULL"),
                                  ("decay_score", "REAL DEFAULT 1.0")]:
                try:
                    self._conn.execute(
                        f"ALTER TABLE long_term_memories ADD COLUMN {col} {col_type}")
                except sqlite3.OperationalError:
                    pass  # 列已存在
            self._conn.executescript(_LTM_DDL)
            self._conn.executescript("""
                DROP TRIGGER IF EXISTS tr_ltm_ai;
                DROP TRIGGER IF EXISTS tr_ltm_ad;
                DROP TRIGGER IF EXISTS tr_ltm_au;
            """)
            try:
                self._conn.executescript(_LTM_TRIGGERS)
            except sqlite3.OperationalError:
                pass
            self._conn.commit()

    def upsert(self, variable_name: str, value: str, category: str = "other",
               confidence: float = 0.5, source: str = "chat") -> int:
        """
        K-V 模型：同一 variable_name 原地更新，不新增行。
        返回 id。
        """
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._lock:
            # 先尝试获取已有 id
            row = self._conn.execute(
                "SELECT id, update_count FROM long_term_memories WHERE variable_name=?",
                (variable_name,)).fetchone()
            if row:
                new_count = row["update_count"] + 1
                rid = row["id"]
                self._conn.execute(
                    """UPDATE long_term_memories
                       SET value=?, category=?, confidence=?, source=?,
                           updated_at=?, update_count=?
                       WHERE id=?""",
                    (value, category, confidence, source, now, new_count, rid))
                # 手动同步 FTS5（删除旧 + 插入新）
                try:
                    self._conn.execute(
                        "DELETE FROM fts_long_term_memories WHERE rowid=?", (rid,))
                    self._conn.execute(
                        "INSERT INTO fts_long_term_memories(rowid, variable_name, value) "
                        "VALUES(?, ?, ?)",
                        (rid, variable_name, value))
                except sqlite3.OperationalError as e:
                    _log.warning(f"[LTM] FTS 同步失败: {e}")
                self._conn.commit()
                return rid
            else:
                cur = self._conn.execute(
                    """INSERT INTO long_term_memories
                       (variable_name, value, category, confidence, source, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (variable_name, value, category, confidence, source, now))
                rid = cur.lastrowid
                # 手动同步 FTS
                try:
                    self._conn.execute(
                        "INSERT INTO fts_long_term_memories(rowid, variable_name, value) "
                        "VALUES(?, ?, ?)",
                        (rid, variable_name, value))
                except sqlite3.OperationalError:
                    pass
                self._conn.commit()
                return rid

    def get(self, variable_name: str) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM long_term_memories WHERE variable_name=?",
                (variable_name,)).fetchone()
        return dict(row) if row else None

    def get_all(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM long_term_memories ORDER BY updated_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def get_by_category(self, category: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM long_term_memories WHERE category=? ORDER BY updated_at DESC",
                (category,)).fetchall()
        return [dict(r) for r in rows]

    def get_summary(self, max_items: int = 20) -> str:
        """生成长期记忆摘要文本（注入 LLM context 用）。
        ★ 1.7.3: 同步累计访问计数 + last_accessed（衰减反哺）。
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT variable_name, value, category, confidence "
                "FROM long_term_memories ORDER BY confidence DESC, updated_at DESC "
                "LIMIT ?", (max_items,)).fetchall()

        if not rows:
            return "（暂无长期记忆）"

        # ★ 1.7.3: 累计本次注入涉及的条目（衰减依据）
        try:
            self.touch_access([r["variable_name"] for r in rows])
        except Exception:
            pass  # 不影响主流程

        by_cat = {}
        for r in rows:
            d = dict(r)
            cat = d.get("category", "other")
            by_cat.setdefault(cat, []).append(d)

        lines = ["[长期记忆]"]
        for cat, items in by_cat.items():
            lines.append(f"\n## {cat}")
            for item in items:
                lines.append(f"  - {item['variable_name']}: {item['value']} "
                             f"(置信度:{item['confidence']:.0%})")
        return "\n".join(lines)

    def search(self, keyword: str, limit: int = 20) -> list[dict]:
        with self._lock:
            try:
                rows = self._conn.execute(
                    """SELECT m.* FROM fts_long_term_memories f
                       JOIN long_term_memories m ON f.rowid = m.id
                       WHERE fts_long_term_memories MATCH ? ORDER BY rank LIMIT ?""",
                    (keyword, limit)).fetchall()
                return [dict(r) for r in rows]
            except Exception:
                return []

    def search_fuzzy(self, variable_name: str, threshold: float = 0.7) -> list[dict]:
        """弱匹配搜索变量名（用 difflib 本地匹配，不调 LLM）"""
        import difflib
        with self._lock:
            all_names = self._conn.execute(
                "SELECT variable_name FROM long_term_memories"
            ).fetchall()
        results = []
        for row in all_names:
            name = row["variable_name"]
            ratio = difflib.SequenceMatcher(None, variable_name.lower(), name.lower()).ratio()
            if ratio >= threshold:
                mem = self.get(name)
                if mem:
                    mem["_match_ratio"] = round(ratio, 3)
                    results.append(mem)
        results.sort(key=lambda x: x.get("_match_ratio", 0), reverse=True)
        return results

    def delete(self, variable_name: str):
        with self._lock:
            # 先查 id，FTS delete 需要 rowid
            row = self._conn.execute(
                "SELECT id FROM long_term_memories WHERE variable_name=?",
                (variable_name,)).fetchone()
            if row:
                rid = row["id"]
                # 手动同步 FTS 删除
                try:
                    self._conn.execute(
                        "DELETE FROM fts_long_term_memories WHERE rowid=?", (rid,))
                except sqlite3.OperationalError as e:
                    _log.warning(f"[LTM] FTS 删除失败: {e}")
            self._conn.execute(
                "DELETE FROM long_term_memories WHERE variable_name=?",
                (variable_name,))
            self._conn.commit()

    # ---- ★ 1.7.3: 衰减 / 访问追踪专用接口 ----

    def touch_access(self, variable_names: list[str]):
        """★ 1.7.3: 批量 +1 access_count，更新 last_accessed。
        由 get_summary() 在注入 LLM context 时调用，提供衰减反哺数据。
        """
        if not variable_names:
            return
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._lock:
            placeholders = ",".join("?" * len(variable_names))
            try:
                self._conn.execute(
                    f"UPDATE long_term_memories "
                    f"SET access_count = access_count + 1, last_accessed = ? "
                    f"WHERE variable_name IN ({placeholders})",
                    [now] + list(variable_names))
                self._conn.commit()
            except Exception as e:
                _log.warning(f"[LTM] touch_access 失败: {e}")

    def update_confidence(self, variable_name: str, new_confidence: float,
                          new_value: str = None):
        """★ 1.7.3: 只更新 confidence（衰减时用），可选同步 value（合并时用）。"""
        with self._lock:
            if new_value is not None:
                self._conn.execute(
                    "UPDATE long_term_memories "
                    "SET confidence=?, value=?, updated_at=datetime('now','localtime') "
                    "WHERE variable_name=?",
                    (float(new_confidence), new_value, variable_name))
            else:
                self._conn.execute(
                    "UPDATE long_term_memories SET confidence=? "
                    "WHERE variable_name=?",
                    (float(new_confidence), variable_name))
            self._conn.commit()
    def get_all_for_decay(self) -> list[dict]:
        """★ 1.7.3: 拉所有条目供衰减计算用（全量列）。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM long_term_memories"
            ).fetchall()
        return [dict(r) for r in rows]

    def count(self) -> int:
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) FROM long_term_memories").fetchone()[0]

    def close(self):
        if self._conn:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None


# ====================================================================
# 统一 Database 接口（兼容旧调用 + 新功能）
# ====================================================================

class Database:
    """统一数据库接口"""

    def __init__(self):
        self.raw = RawMonitorDB()
        self.chat = ChatRecordsDB()
        self.ltm = LongTermMemoriesDB()
        _log.info(f"SQLite 数据库已就绪: {DB_DIR}")

    # ---- 原始事件（兼容旧 add_memory 调用）----

    def add_memory(self, content: str, category: str = "general",
                   source: str = "screen", importance: int = 1):
        """兼容旧接口：存入 raw_events"""
        import json as _json
        self.raw.add_raw_event(
            source=source, type_=category,
            content=content,
            metadata={"importance": importance, "legacy": True})

    def add_memories_batch(self, memories: list[dict]):
        for m in memories:
            self.add_memory(
                content=m.get("content", ""),
                category=m.get("category", "general"),
                source=m.get("source", "screen"),
                importance=m.get("importance", 1))
    # ---- 聊天记录（兼容旧接口）----

    def add_chat_history(self, role: str, content: str, tool_name: str = None,
                         tool_call_id: str = None, topic_id: int = None,
                         speaker: str = None, ctx_exclude: int = 0):
        """★ 1.9.3: speaker = 说话者（assistant 消息传当前形象名，其余留空）
        ★ 1.9.3 第 60 轮: ctx_exclude = 1 → 只给人看，不进主 LLM 上下文"""
        self.chat.add(role, content, tool_name, tool_call_id, topic_id, speaker,
                      ctx_exclude)
    # ---- 话题管理 ----

    def create_topic(self, title: str = "", character: str = "") -> int:
        return self.chat.create_topic(title, character)

    # ---- ★ 1.6.0: 一个角色一个窗口 ----

    def get_active_topic(self, character: str) -> Optional[dict]:
        return self.chat.get_active_topic(character)

    def list_character_topics(self, character: str, limit: int = 50) -> list[dict]:
        return self.chat.list_character_topics(character, limit)

    def archive_topic(self, topic_id: int, summary: str = ""):
        self.chat.archive_topic(topic_id, summary)

    def set_topic_summary(self, topic_id: int, summary: str):
        self.chat.set_topic_summary(topic_id, summary)

    def get_topic_summary(self, topic_id: int) -> str:
        return self.chat.get_topic_summary(topic_id)

    def get_last_archived_summary(self, character: str, before_topic_id: int = 0) -> str:
        return self.chat.get_last_archived_summary(character, before_topic_id)

    def count_character_messages(self, character: str) -> int:
        return self.chat.count_character_messages(character)

    def get_character_history_messages(self, character: str,
                                       exclude_topic: int = 0,
                                       limit: int = 200) -> list[dict]:
        return self.chat.get_character_history_messages(character, exclude_topic, limit)

    def set_topic_title(self, topic_id: int, title: str):
        self.chat.set_topic_title(topic_id, title)

    def get_topic_title(self, topic_id: int) -> str:
        return self.chat.get_topic_title(topic_id)

    def list_topics(self) -> list[dict]:
        return self.chat.list_topics()

    def delete_topic(self, topic_id: int):
        self.chat.delete_topic(topic_id)

    def get_topic_token_count(self, topic_id: int) -> int:
        return self.chat.get_topic_token_count(topic_id)

    def is_topic_full(self, topic_id: int) -> bool:
        return self.chat.is_topic_full(topic_id)

    def get_topic_messages(self, topic_id: int) -> list[dict]:
        return self.chat.get_topic_messages(topic_id)

    def close(self):
        self.raw.close()
        self.chat.close()
        self.ltm.close()
        _log.info("数据库已关闭")


# ====================================================================
# 全局单例
# ====================================================================

_db_instance: Optional[Database] = None


def get_db() -> Database:
    global _db_instance
    if _db_instance is None:
        _db_instance = Database()
    return _db_instance
