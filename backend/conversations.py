"""Persistence for chat threads, their messages, and per-user integration switches."""

import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend import db
from backend.auth import current_user_id

DB_PATH = Path(os.getenv("CHAT_DB_PATH", "data/chat_history.sqlite3"))
MODEL_CONTEXT_MESSAGES = 16
MODEL_CONTEXT_CHARS = 16000


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _uid() -> str:
    """The signed-in user for this request; every query below is scoped to it."""
    user_id = current_user_id.get()
    if not user_id:
        raise PermissionError("Sign in required.")
    return user_id


def _connect() -> db.Database:
    return db.connect(DB_PATH)


def initialize_conversations() -> None:
    """Create the thread store on first startup; data survives app restarts."""
    with _connect() as connection:
        message_id = "BIGSERIAL PRIMARY KEY" if connection.postgres else "INTEGER PRIMARY KEY AUTOINCREMENT"
        for statement in (
            """CREATE TABLE IF NOT EXISTS conversations (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL DEFAULT '',
                title TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )""",
            f"""CREATE TABLE IF NOT EXISTS messages (
                id {message_id},
                conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
                content TEXT NOT NULL,
                created_at TEXT NOT NULL
            )""",
            "CREATE INDEX IF NOT EXISTS messages_by_conversation ON messages(conversation_id, id)",
            """CREATE TABLE IF NOT EXISTS user_permissions (
                user_id TEXT NOT NULL,
                service TEXT NOT NULL,
                enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
                updated_at TEXT NOT NULL,
                PRIMARY KEY (user_id, service)
            )""",
        ):
            connection.execute(statement)
        if not connection.postgres:
            # SQLite files from before accounts existed have no owner column; '' marks
            # those rows so the first account created can claim them.
            columns = [row["name"] for row in connection.execute("PRAGMA table_info(conversations)")]
            if "user_id" not in columns:
                connection.execute("ALTER TABLE conversations ADD COLUMN user_id TEXT NOT NULL DEFAULT ''")
        connection.execute(
            "CREATE INDEX IF NOT EXISTS conversations_by_user ON conversations(user_id, updated_at)"
        )


def claim_legacy_conversations(user_id: str) -> None:
    """Give chats saved before accounts existed to the first user."""
    with _connect() as connection:
        connection.execute("UPDATE conversations SET user_id = ? WHERE user_id = ''", (user_id,))


def get_integration_permission(service: str, default: bool = True) -> bool:
    with _connect() as connection:
        row = connection.execute(
            "SELECT enabled FROM user_permissions WHERE user_id = ? AND service = ?", (_uid(), service)
        ).fetchone()
    return default if row is None else bool(row["enabled"])


def set_integration_permission(service: str, enabled: bool) -> None:
    with _connect() as connection:
        connection.execute(
            "INSERT INTO user_permissions(user_id, service, enabled, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(user_id, service) DO UPDATE SET enabled = excluded.enabled, updated_at = excluded.updated_at",
            (_uid(), service, int(enabled), _now()),
        )


def create_conversation(title: str = "New chat") -> dict[str, str]:
    conversation_id = str(uuid.uuid4())
    now = _now()
    with _connect() as connection:
        connection.execute(
            "INSERT INTO conversations(id, user_id, title, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (conversation_id, _uid(), title[:80] or "New chat", now, now),
        )
    return {"id": conversation_id, "title": title[:80] or "New chat", "created_at": now, "updated_at": now}


def list_conversations() -> list[dict[str, str]]:
    with _connect() as connection:
        rows = connection.execute(
            "SELECT id, title, created_at, updated_at FROM conversations WHERE user_id = ? ORDER BY updated_at DESC",
            (_uid(),),
        ).fetchall()
    return [dict(row) for row in rows]


def get_conversation(conversation_id: str) -> dict[str, Any] | None:
    with _connect() as connection:
        conversation = connection.execute(
            "SELECT id, title, created_at, updated_at FROM conversations WHERE id = ? AND user_id = ?",
            (conversation_id, _uid()),
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
            "SELECT m.role, m.content FROM messages m JOIN conversations c ON c.id = m.conversation_id "
            "WHERE m.conversation_id = ? AND c.user_id = ? ORDER BY m.id DESC LIMIT ?",
            (conversation_id, _uid(), MODEL_CONTEXT_MESSAGES),
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
            "SELECT 1 FROM conversations WHERE id = ? AND user_id = ?", (conversation_id, _uid())
        ).fetchone()
        if exists is None:
            raise KeyError("Conversation not found.")
        count = connection.execute(
            "SELECT COUNT(*) AS n FROM messages WHERE conversation_id = ?", (conversation_id,)
        ).fetchone()["n"]
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
        cursor = connection.execute(
            "DELETE FROM conversations WHERE id = ? AND user_id = ?", (conversation_id, _uid())
        )
    return cursor.rowcount > 0
