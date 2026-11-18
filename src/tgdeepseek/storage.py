from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ChatSetting:
    chat_id: int
    enabled: bool
    mode: str
    updated_at: int


class Storage:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock, self._connection:
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS chat_settings (
                    chat_id INTEGER PRIMARY KEY,
                    enabled INTEGER NOT NULL,
                    mode TEXT NOT NULL,
                    updated_at INTEGER NOT NULL
                )
                """
            )
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS reply_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    created_at INTEGER NOT NULL
                )
                """
            )
            self._connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_reply_log_created_at ON reply_log(created_at)"
            )
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS web_chat_state (
                    chat_key TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    last_mid TEXT NOT NULL,
                    updated_at INTEGER NOT NULL
                )
                """
            )
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS web_reply_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_key TEXT NOT NULL,
                    created_at INTEGER NOT NULL
                )
                """
            )
            self._connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_web_reply_log_created_at ON web_reply_log(created_at)"
            )
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS web_sender_state (
                    chat_key TEXT NOT NULL,
                    sender TEXT NOT NULL,
                    last_reply_at INTEGER NOT NULL,
                    PRIMARY KEY (chat_key, sender)
                )
                """
            )
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS web_proactive_reply_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_key TEXT NOT NULL,
                    created_at INTEGER NOT NULL
                )
                """
            )
            self._connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_web_proactive_reply_created_at "
                "ON web_proactive_reply_log(created_at)"
            )

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def get_chat(self, chat_id: int) -> ChatSetting | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT chat_id, enabled, mode, updated_at FROM chat_settings WHERE chat_id = ?",
                (chat_id,),
            ).fetchone()
        if row is None:
            return None
        return ChatSetting(int(row["chat_id"]), bool(row["enabled"]), str(row["mode"]), int(row["updated_at"]))

    def set_chat(self, chat_id: int, enabled: bool, mode: str) -> None:
        if mode not in {"mention", "free", "off"}:
            raise ValueError("mode must be mention, free, or off")
        now = int(time.time())
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO chat_settings(chat_id, enabled, mode, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                  enabled = excluded.enabled,
                  mode = excluded.mode,
                  updated_at = excluded.updated_at
                """,
                (chat_id, int(enabled), mode, now),
            )

    def list_chats(self) -> list[ChatSetting]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT chat_id, enabled, mode, updated_at FROM chat_settings ORDER BY updated_at DESC"
            ).fetchall()
        return [
            ChatSetting(int(row["chat_id"]), bool(row["enabled"]), str(row["mode"]), int(row["updated_at"]))
            for row in rows
        ]

    def record_reply(self, chat_id: int) -> None:
        now = int(time.time())
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO reply_log(chat_id, created_at) VALUES (?, ?)", (chat_id, now)
            )
            self._connection.execute("DELETE FROM reply_log WHERE created_at < ?", (now - 86400,))

    def last_reply_at(self, chat_id: int) -> int | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT MAX(created_at) AS last_at FROM reply_log WHERE chat_id = ?", (chat_id,)
            ).fetchone()
        return int(row["last_at"]) if row and row["last_at"] is not None else None

    def replies_since(self, since: int) -> int:
        with self._lock:
            row = self._connection.execute(
                "SELECT COUNT(*) AS total FROM reply_log WHERE created_at >= ?", (since,)
            ).fetchone()
        return int(row["total"])

    def get_web_last_mid(self, chat_key: str) -> str | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT last_mid FROM web_chat_state WHERE chat_key = ?", (chat_key,)
            ).fetchone()
        return str(row["last_mid"]) if row else None

    def set_web_last_mid(self, chat_key: str, title: str, last_mid: str) -> None:
        now = int(time.time())
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO web_chat_state(chat_key, title, last_mid, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(chat_key) DO UPDATE SET
                  title = excluded.title,
                  last_mid = excluded.last_mid,
                  updated_at = excluded.updated_at
                """,
                (chat_key, title, last_mid, now),
            )

    def get_web_chat_title(self, chat_key: str) -> str | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT title FROM web_chat_state WHERE chat_key = ?", (chat_key,)
            ).fetchone()
        return str(row["title"]) if row and row["title"] else None

    def record_web_reply(self, chat_key: str, sender: str | None = None) -> None:
        now = int(time.time())
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO web_reply_log(chat_key, created_at) VALUES (?, ?)", (chat_key, now)
            )
            if sender:
                self._connection.execute(
                    """
                    INSERT INTO web_sender_state(chat_key, sender, last_reply_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(chat_key, sender) DO UPDATE SET
                      last_reply_at = excluded.last_reply_at
                    """,
                    (chat_key, sender.casefold(), now),
                )
            self._connection.execute("DELETE FROM web_reply_log WHERE created_at < ?", (now - 86400,))

    def web_last_sender_reply_at(self, chat_key: str, sender: str) -> int | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT last_reply_at FROM web_sender_state WHERE chat_key = ? AND sender = ?",
                (chat_key, sender.casefold()),
            ).fetchone()
        return int(row["last_reply_at"]) if row else None

    def record_web_proactive_reply(self, chat_key: str) -> None:
        now = int(time.time())
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO web_proactive_reply_log(chat_key, created_at) VALUES (?, ?)",
                (chat_key, now),
            )
            self._connection.execute(
                "DELETE FROM web_proactive_reply_log WHERE created_at < ?", (now - 86400,)
            )

    def web_last_proactive_reply_at(self, chat_key: str) -> int | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT MAX(created_at) AS last_at FROM web_proactive_reply_log WHERE chat_key = ?",
                (chat_key,),
            ).fetchone()
        return int(row["last_at"]) if row and row["last_at"] is not None else None

    def web_proactive_replies_since(self, since: int) -> int:
        with self._lock:
            row = self._connection.execute(
                "SELECT COUNT(*) AS total FROM web_proactive_reply_log WHERE created_at >= ?",
                (since,),
            ).fetchone()
        return int(row["total"])

    def web_proactive_replies_for_chat_since(self, chat_key: str, since: int) -> int:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT COUNT(*) AS total FROM web_proactive_reply_log
                WHERE chat_key = ? AND created_at >= ?
                """,
                (chat_key, since),
            ).fetchone()
        return int(row["total"])

    def web_last_reply_at(self, chat_key: str) -> int | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT MAX(created_at) AS last_at FROM web_reply_log WHERE chat_key = ?", (chat_key,)
            ).fetchone()
        return int(row["last_at"]) if row and row["last_at"] is not None else None

    def web_replies_since(self, since: int) -> int:
        with self._lock:
            row = self._connection.execute(
                "SELECT COUNT(*) AS total FROM web_reply_log WHERE created_at >= ?", (since,)
            ).fetchone()
        return int(row["total"])

    def web_replies_for_chat_since(self, chat_key: str, since: int) -> int:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT COUNT(*) AS total FROM web_reply_log
                WHERE chat_key = ? AND created_at >= ?
                """,
                (chat_key, since),
            ).fetchone()
        return int(row["total"])

