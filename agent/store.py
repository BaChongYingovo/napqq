"""会话存储：SQLite。保存每个会话的消息历史、滚动摘要、运行时状态。"""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_type   TEXT NOT NULL,
    chat_id     INTEGER NOT NULL,
    title       TEXT DEFAULT '',
    created_at  INTEGER NOT NULL,
    updated_at  INTEGER NOT NULL,
    UNIQUE(chat_type, chat_id)
);

CREATE TABLE IF NOT EXISTS messages (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    conv_id      INTEGER NOT NULL REFERENCES conversations(id),
    role         TEXT NOT NULL,          -- user / assistant / system
    sender_id    INTEGER DEFAULT 0,
    sender_name  TEXT DEFAULT '',
    content      TEXT NOT NULL,
    raw          TEXT DEFAULT '',
    created_at   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conv_id, id);

CREATE TABLE IF NOT EXISTS summaries (
    conv_id      INTEGER PRIMARY KEY REFERENCES conversations(id),
    content      TEXT NOT NULL,
    upto_msg_id  INTEGER NOT NULL,
    updated_at   INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS kv (
    key          TEXT PRIMARY KEY,
    value        TEXT NOT NULL,
    updated_at   INTEGER NOT NULL
);
"""


class Store:
    def __init__(self, db_path: str | Path):
        p = Path(db_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(p), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._lock = threading.RLock()
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---------- 会话 ----------

    def get_or_create_conversation(self, chat_type: str, chat_id: int, title: str = "") -> int:
        import time
        ts = int(time.time())
        with self._lock:
            cur = self._conn.execute(
                "SELECT id FROM conversations WHERE chat_type=? AND chat_id=?", (chat_type, chat_id)
            )
            row = cur.fetchone()
            if row:
                if title:
                    self._conn.execute(
                        "UPDATE conversations SET title=?, updated_at=? WHERE id=?", (title, ts, row["id"])
                    )
                    self._conn.commit()
                return int(row["id"])
            cur = self._conn.execute(
                "INSERT INTO conversations(chat_type, chat_id, title, created_at, updated_at) VALUES (?,?,?,?,?)",
                (chat_type, chat_id, title, ts, ts),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def list_conversations(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT c.*, (SELECT COUNT(*) FROM messages m WHERE m.conv_id=c.id) AS msg_count "
                "FROM conversations c ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    # ---------- 消息 ----------

    def add_message(
        self,
        conv_id: int,
        role: str,
        content: str,
        sender_id: int = 0,
        sender_name: str = "",
        raw: str = "",
    ) -> int:
        import time
        ts = int(time.time())
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO messages(conv_id, role, sender_id, sender_name, content, raw, created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (conv_id, role, sender_id, sender_name, content, raw, ts),
            )
            self._conn.execute("UPDATE conversations SET updated_at=? WHERE id=?", (ts, conv_id))
            self._conn.commit()
            return int(cur.lastrowid)

    def recent_messages(self, conv_id: int, limit: int = 24) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM (SELECT * FROM messages WHERE conv_id=? ORDER BY id DESC LIMIT ?) ORDER BY id ASC",
                (conv_id, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def messages_after(self, conv_id: int, after_id: int, limit: int = 500) -> list[dict[str, Any]]:
        """id 大于 after_id 的消息，升序。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM messages WHERE conv_id=? AND id>? ORDER BY id ASC LIMIT ?",
                (conv_id, int(after_id), limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def recent_messages_after(self, conv_id: int, after_id: int, limit: int = 24) -> list[dict[str, Any]]:
        """id 大于 after_id 的最近 limit 条，升序。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM (SELECT * FROM messages WHERE conv_id=? AND id>? ORDER BY id DESC LIMIT ?) ORDER BY id ASC",
                (conv_id, int(after_id), limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def clear_conversation(self, conv_id: int) -> int:
        with self._lock:
            cur = self._conn.execute("DELETE FROM messages WHERE conv_id=?", (conv_id,))
            self._conn.execute("DELETE FROM summaries WHERE conv_id=?", (conv_id,))
            self._conn.commit()
            return cur.rowcount

    def message_count(self, conv_id: int) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) AS n FROM messages WHERE conv_id=?", (conv_id,)).fetchone()
        return int(row["n"]) if row else 0

    # ---------- 摘要 ----------

    def get_summary(self, conv_id: int) -> tuple[str, int]:
        with self._lock:
            row = self._conn.execute("SELECT content, upto_msg_id FROM summaries WHERE conv_id=?", (conv_id,)).fetchone()
        return (str(row["content"]), int(row["upto_msg_id"])) if row else ("", 0)

    def set_summary(self, conv_id: int, content: str, upto_msg_id: int) -> None:
        import time
        with self._lock:
            self._conn.execute(
                "INSERT INTO summaries(conv_id, content, upto_msg_id, updated_at) VALUES (?,?,?,?) "
                "ON CONFLICT(conv_id) DO UPDATE SET content=excluded.content, upto_msg_id=excluded.upto_msg_id, "
                "updated_at=excluded.updated_at",
                (conv_id, content, upto_msg_id, int(time.time())),
            )
            self._conn.commit()

    # ---------- 运行时状态 ----------

    def kv_get(self, key: str, default: str = "") -> str:
        with self._lock:
            row = self._conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return str(row["value"]) if row else default

    def kv_set(self, key: str, value: str) -> None:
        import time
        with self._lock:
            self._conn.execute(
                "INSERT INTO kv(key, value, updated_at) VALUES (?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (key, value, int(time.time())),
            )
            self._conn.commit()

    def stats(self) -> dict[str, Any]:
        with self._lock:
            convs = self._conn.execute("SELECT COUNT(*) AS n FROM conversations").fetchone()["n"]
            msgs = self._conn.execute("SELECT COUNT(*) AS n FROM messages").fetchone()["n"]
        return {"conversations": int(convs), "messages": int(msgs)}
