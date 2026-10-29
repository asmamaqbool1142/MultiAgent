"""SQLite persistence for local chat threads and their messages."""

import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DB_PATH = Path(os.getenv("CHAT_DB_PATH", "data/chat_history.sqlite3"))
MODEL_CONTEXT_MESSAGES = 16
MODEL_CONTEXT_CHARS = 16000


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH, timeout=15)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 15000")
    return connection


def initialize_conversations() -> None:
    """Create the thread store on first startup; data survives app restarts."""
    with _connect() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS conversations (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
                content TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS messages_by_conversation
                ON messages(conversation_id, id);
            CREATE TABLE IF NOT EXISTS integration_permissions (
                service TEXT PRIMARY KEY,
                enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
                updated_at TEXT NOT NULL
            );
            """
        )


def get_integration_permission(service: str, default: bool = True) -> bool:
    with _connect() as connection:
        row = connection.execute(
            "SELECT enabled FROM integration_permissions WHERE service = ?", (service,)
        ).fetchone()
    return default if row is None else bool(row["enabled"])


def set_integration_permission(service: str, enabled: bool) -> None:
    with _connect() as connection:
        connection.execute(
            "INSERT INTO integration_permissions(service, enabled, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(service) DO UPDATE SET enabled = excluded.enabled, updated_at = excluded.updated_at",
            (service, int(enabled), _now()),
        )


def create_conversation(title: str = "New chat") -> dict[str, str]:
    conversation_id = str(uuid.uuid4())
    now = _now()
    with _connect() as connection:
        connection.execute(
            "INSERT INTO conversations(id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (conversation_id, title[:80] or "New chat", now, now),
        )
    return {"id": conversation_id, "title": title[:80] or "New chat", "created_at": now, "updated_at": now}


def list_conversations() -> list[dict[str, str]]:
    with _connect() as connection:
        rows = connection.execute(
            "SELECT id, title, created_at, updated_at FROM conversations ORDER BY updated_at DESC"
        ).fetchall()
    return [dict(row) for row in rows]


def get_conversation(conversation_id: str) -> dict[str, Any] | None:
    with _connect() as connection:
        conversation = connection.execute(
            "SELECT id, title, created_at, updated_at FROM conversations WHERE id = ?",
            (conversation_id,),
        ).fetchone()
        if conversation is None:
            return None
        messages = connection.execute(
            "SELECT role, content, created_at FROM messages WHERE conversation_id = ? ORDER BY id",
            (conversation_id,),
        ).fetchall()
    return {**dict(conversation), "messages": [dict(message) for message in messages]}


def get_model_history(conversation_id: str) -> list[dict[str, str]]:
    """Return recent turns in chronological order for the model context window."""
    with _connect() as connection:
        rows = connection.execute(
            "SELECT role, content FROM messages WHERE conversation_id = ? ORDER BY id DESC LIMIT ?",
            (conversation_id, MODEL_CONTEXT_MESSAGES),
        ).fetchall()
    history: list[dict[str, str]] = []
    remaining = MODEL_CONTEXT_CHARS
    for row in rows:
        if remaining <= 0:
            break
        content = row["content"]
        if len(content) > remaining:
            content = content[-remaining:]
        history.append({"role": row["role"], "content": content})
        remaining -= len(content)
    return list(reversed(history))


def save_turn(conversation_id: str, user_message: str, assistant_message: str) -> None:
    now = _now()
    title = " ".join(user_message.split())[:80] or "New chat"
    with _connect() as connection:
        exists = connection.execute(
            "SELECT 1 FROM conversations WHERE id = ?", (conversation_id,)
        ).fetchone()
        if exists is None:
            raise KeyError("Conversation not found.")
        count = connection.execute(
            "SELECT COUNT(*) FROM messages WHERE conversation_id = ?", (conversation_id,)
        ).fetchone()[0]
        connection.executemany(
            "INSERT INTO messages(conversation_id, role, content, created_at) VALUES (?, ?, ?, ?)",
            [
                (conversation_id, "user", user_message, now),
                (conversation_id, "assistant", assistant_message, now),
            ],
        )
        if count == 0:
            connection.execute(
                "UPDATE conversations SET title = ?, updated_at = ? WHERE id = ?",
                (title, now, conversation_id),
            )
        else:
            connection.execute(
                "UPDATE conversations SET updated_at = ? WHERE id = ?",
                (now, conversation_id),
            )


def delete_conversation(conversation_id: str) -> bool:
    with _connect() as connection:
        cursor = connection.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))
    return cursor.rowcount > 0
