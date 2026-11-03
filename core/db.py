"""SQLite 数据层。

设计要点（照搬自原版 companion.db，去掉了多群分库）：
  * memories 带版本链：root_id / previous_id / version / status / superseded_at
    → 记忆可以被"修正"而不是直接覆盖
  * self_facts 三态：candidate / confirmed / conflict / superseded
    → 只有 confirmed 才允许进入人格提示词
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

-- 画像层：人工编辑的稳定事实
CREATE TABLE IF NOT EXISTS profile (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  REAL NOT NULL
);

-- 自我事实（角色对自己的认知），三态确认制
CREATE TABLE IF NOT EXISTS self_facts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    char_id       TEXT NOT NULL,
    key           TEXT NOT NULL,
    value         TEXT NOT NULL,
    value_hash    TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'candidate'
                  CHECK(status IN ('candidate','confirmed','conflict','superseded')),
    origin        TEXT NOT NULL DEFAULT 'analysis',   -- analysis | manual
    source_turn   INTEGER,
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL,
    confirmed_at  REAL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_self_fact_kv
    ON self_facts(char_id, key, value_hash);

-- 情节层：带向量与版本链
CREATE TABLE IF NOT EXISTS memories (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    char_id       TEXT NOT NULL,
    ts            REAL NOT NULL,
    kind          TEXT NOT NULL DEFAULT 'episodic',   -- episodic | emotion | fact
    text          TEXT NOT NULL,
    vec           TEXT NOT NULL,          -- 空格分隔的 256 维浮点（L2 归一化）
    emotion       TEXT,
    weight        REAL NOT NULL DEFAULT 1.0,
    text_hash     TEXT,
    source_turn   INTEGER,
    root_id       INTEGER,                -- 同一条记忆的版本链根
    previous_id   INTEGER,                -- 上一版
    version       INTEGER NOT NULL DEFAULT 1,
    status        TEXT NOT NULL DEFAULT 'active'
                  CHECK(status IN ('active','superseded')),
    superseded_at REAL,
    updated_at    REAL
);
CREATE INDEX IF NOT EXISTS idx_mem_hash ON memories(char_id, text_hash, status);
CREATE INDEX IF NOT EXISTS idx_mem_root ON memories(root_id, version);
CREATE INDEX IF NOT EXISTS idx_mem_ts   ON memories(char_id, ts DESC);

CREATE TABLE IF NOT EXISTS memory_meta (
    memory_id   INTEGER PRIMARY KEY,
    pinned      INTEGER NOT NULL DEFAULT 0,
    category    TEXT NOT NULL DEFAULT 'auto',
    corrected_at REAL
);

-- 会话与消息
CREATE TABLE IF NOT EXISTS sessions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    char_id       TEXT NOT NULL,
    topic         TEXT NOT NULL DEFAULT '',
    started_at    REAL NOT NULL,
    last_activity REAL NOT NULL,
    summary       TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS messages (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    char_id       TEXT NOT NULL,
    session_id    INTEGER,
    ts            REAL NOT NULL,
    role          TEXT NOT NULL,          -- user | char
    text          TEXT NOT NULL,
    meta          TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_msg_ts ON messages(char_id, ts DESC);

-- 事件流（含 flagged，用于安全信号留痕）
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    char_id     TEXT NOT NULL,
    ts          REAL NOT NULL,
    kind        TEXT NOT NULL,
    text        TEXT NOT NULL,
    emotion     TEXT,
    importance  REAL DEFAULT 0.5,
    flagged     INTEGER DEFAULT 0
);

-- 角色运行时状态：与角色卡（声明）解耦
CREATE TABLE IF NOT EXISTS char_state (
    char_id       TEXT PRIMARY KEY,
    enthusiasm    INTEGER NOT NULL DEFAULT 60,
    affection     INTEGER NOT NULL DEFAULT 50,
    initiative    REAL NOT NULL DEFAULT 1.0,
    energy        INTEGER NOT NULL DEFAULT 80,
    mood          TEXT NOT NULL DEFAULT '平静',
    quiet_until   REAL NOT NULL DEFAULT 0,
    last_spoke_at REAL NOT NULL DEFAULT 0,
    last_user_at  REAL NOT NULL DEFAULT 0,
    cold_streak   INTEGER NOT NULL DEFAULT 0,
    daily_proactive INTEGER NOT NULL DEFAULT 0,
    daily_reset_day TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS agreements (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    char_id     TEXT NOT NULL,
    title       TEXT NOT NULL,
    details     TEXT NOT NULL DEFAULT '',
    due_at      REAL NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending',
    created_at  REAL NOT NULL,
    followed_up_at REAL
);
"""


class Store:
    def __init__(self, db_path: str):
        self.path = db_path
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self._local = threading.local()
        with self.conn() as c:
            c.executescript(SCHEMA)

    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
            c.row_factory = sqlite3.Row
            self._local.conn = c
        return c

    # ---- 通用 ----
    def q(self, sql: str, args=()):
        return self.conn().execute(sql, args).fetchall()

    def q1(self, sql: str, args=()):
        return self.conn().execute(sql, args).fetchone()

    def x(self, sql: str, args=()):
        c = self.conn()
        cur = c.execute(sql, args)
        c.commit()
        return cur

    # ---- 状态 ----
    def state(self, char_id: str) -> dict:
        row = self.q1("SELECT * FROM char_state WHERE char_id=?", (char_id,))
        if row:
            return dict(row)
        self.x(
            "INSERT INTO char_state(char_id) VALUES(?)", (char_id,)
        )
        return dict(self.q1("SELECT * FROM char_state WHERE char_id=?", (char_id,)))

    def update_state(self, char_id: str, **kw):
        if not kw:
            return
        sets = ", ".join(f"{k}=?" for k in kw)
        self.x(f"UPDATE char_state SET {sets} WHERE char_id=?", (*kw.values(), char_id))

    # ---- 消息 ----
    def add_message(self, char_id, session_id, role, text, meta="{}"):
        ts = time.time()
        cur = self.x(
            "INSERT INTO messages(char_id,session_id,ts,role,text,meta) VALUES(?,?,?,?,?,?)",
            (char_id, session_id, ts, role, text, meta),
        )
        self.x("UPDATE sessions SET last_activity=? WHERE id=?", (ts, session_id))
        return cur.lastrowid, ts

    def recent_messages(self, char_id: str, limit: int = 20):
        return self.q(
            "SELECT * FROM messages WHERE char_id=? ORDER BY id DESC LIMIT ?",
            (char_id, limit),
        )[::-1]

    def session(self, char_id: str) -> int:
        row = self.q1(
            "SELECT id FROM sessions WHERE char_id=? ORDER BY last_activity DESC LIMIT 1",
            (char_id,),
        )
        if row:
            return row["id"]
        ts = time.time()
        cur = self.x(
            "INSERT INTO sessions(char_id,started_at,last_activity) VALUES(?,?,?)",
            (char_id, ts, ts),
        )
        return cur.lastrowid
