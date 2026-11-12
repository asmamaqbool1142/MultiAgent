"""Accounts and sessions: email + password, plus Google sign-in (SSO)."""

import contextvars
import hashlib
import hmac
import os
import re
import secrets
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from backend import db

AUTH_DB_PATH = Path(os.getenv("AUTH_DB_PATH", "data/users.sqlite3"))
SESSION_COOKIE = "session"
SESSION_TTL_SECONDS = 30 * 24 * 3600
MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 128
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Set per request by the auth middleware; conversations and Google tokens read it
# so each user only ever touches their own data.
current_user_id: contextvars.ContextVar[str] = contextvars.ContextVar("current_user_id", default="")


class AuthError(ValueError):
    """A problem the person can fix; the message is safe to show."""


def _connect() -> db.Database:
    return db.connect(AUTH_DB_PATH)


def initialize_auth() -> None:
    with _connect() as connection:
        for statement in (
            """CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                email TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL DEFAULT '',
                password_hash TEXT,
                google_sub TEXT UNIQUE,
                created_at TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS sessions (
                token_hash TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                expires_at DOUBLE PRECISION NOT NULL
            )""",
            # Google access for Calendar/Gmail, one row per user. Held in the database
            # (not a file) so it survives restarts on hosts with throwaway disks.
            """CREATE TABLE IF NOT EXISTS google_tokens (
                user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                token_json TEXT NOT NULL
            )""",
        ):
            connection.execute(statement)


def load_google_token(user_id: str) -> str | None:
    with _connect() as connection:
        row = connection.execute("SELECT token_json FROM google_tokens WHERE user_id = ?", (user_id,)).fetchone()
    return row["token_json"] if row else None


def save_google_token(user_id: str, token_json: str) -> None:
    with _connect() as connection:
        connection.execute(
            "INSERT INTO google_tokens(user_id, token_json) VALUES (?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET token_json = excluded.token_json",
            (user_id, token_json),
        )


# --- passwords -------------------------------------------------------------

def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str | None) -> bool:
    # Hash even when there is no stored hash so unknown emails cost the same time.
    if not stored or not stored.startswith("scrypt$"):
        hashlib.scrypt(password.encode(), salt=b"\0" * 16, n=2**14, r=8, p=1, dklen=32)
        return False
    _, salt_hex, digest_hex = stored.split("$")
    digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex), n=2**14, r=8, p=1, dklen=32)
    return hmac.compare_digest(digest.hex(), digest_hex)


# --- policy ----------------------------------------------------------------

def normalize_email(email: str) -> str:
    email = (email or "").strip().lower()
    if len(email) > 254 or not EMAIL_PATTERN.match(email):
        raise AuthError("Enter a valid email address.")
    return email


def check_email_allowed(email: str) -> None:
    """ALLOWED_EMAILS (comma separated emails or @domains) keeps a public deploy private."""
    allowed = [item.strip().lower() for item in os.getenv("ALLOWED_EMAILS", "").split(",") if item.strip()]
    if allowed and not any(email == item or (item.startswith("@") and email.endswith(item)) for item in allowed):
        raise AuthError("This email isn't on the invite list for this assistant.")


def check_password(password: str) -> None:
    if not MIN_PASSWORD_LENGTH <= len(password) <= MAX_PASSWORD_LENGTH:
        raise AuthError(f"Use a password of {MIN_PASSWORD_LENGTH} to {MAX_PASSWORD_LENGTH} characters.")


# --- users -----------------------------------------------------------------

def _public(row) -> dict[str, str]:
    return {"id": row["id"], "email": row["email"], "name": row["name"]}


def user_count() -> int:
    with _connect() as connection:
        return connection.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]


def create_user(email: str, password: str, name: str = "") -> dict[str, str]:
    email = normalize_email(email)
    check_email_allowed(email)
    check_password(password)
    user_id = str(uuid.uuid4())
    try:
        with _connect() as connection:
            connection.execute(
                "INSERT INTO users(id, email, name, password_hash, created_at) VALUES (?, ?, ?, ?, ?)",
                (user_id, email, name.strip()[:80], hash_password(password), datetime.now(timezone.utc).isoformat()),
            )
    except db.INTEGRITY_ERRORS:
        raise AuthError("An account with this email already exists. Log in instead.") from None
    return {"id": user_id, "email": email, "name": name.strip()[:80]}


def authenticate(email: str, password: str) -> dict[str, str]:
    try:
        email = normalize_email(email)
    except AuthError:
        email = ""
    with _connect() as connection:
        row = connection.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    if not verify_password(password[:MAX_PASSWORD_LENGTH], row["password_hash"] if row else None) or row is None:
        raise AuthError("Wrong email or password. If you signed up with Google, use Continue with Google.")
    return _public(row)


def sign_in_with_google(email: str, google_sub: str, name: str) -> dict[str, str]:
    """Find or create the account for a verified Google identity."""
    email = normalize_email(email)
    with _connect() as connection:
        row = connection.execute("SELECT * FROM users WHERE google_sub = ?", (google_sub,)).fetchone()
        if row is None:
            row = connection.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
            if row is None:
                check_email_allowed(email)
                user_id = str(uuid.uuid4())
                connection.execute(
                    "INSERT INTO users(id, email, name, google_sub, created_at) VALUES (?, ?, ?, ?, ?)",
                    (user_id, email, name.strip()[:80], google_sub, datetime.now(timezone.utc).isoformat()),
                )
                row = connection.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            else:
                # Emails aren't verified at password signup, so someone could have registered
                # this address first. Linking Google proves ownership: drop that password and
                # every session it created.
                connection.execute(
                    "UPDATE users SET google_sub = ?, password_hash = NULL WHERE id = ?", (google_sub, row["id"])
                )
                connection.execute("DELETE FROM sessions WHERE user_id = ?", (row["id"],))
                row = connection.execute("SELECT * FROM users WHERE id = ?", (row["id"],)).fetchone()
    return _public(row)


# --- sessions --------------------------------------------------------------

def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_session(user_id: str) -> str:
    token = secrets.token_urlsafe(32)
    with _connect() as connection:
        connection.execute("DELETE FROM sessions WHERE expires_at < ?", (time.time(),))
        connection.execute(
            "INSERT INTO sessions(token_hash, user_id, expires_at) VALUES (?, ?, ?)",
            (_token_hash(token), user_id, time.time() + SESSION_TTL_SECONDS),
        )
    return token


def user_for_session(token: str | None) -> dict[str, str] | None:
    if not token:
        return None
    with _connect() as connection:
        row = connection.execute(
            "SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id "
            "WHERE s.token_hash = ? AND s.expires_at > ?",
            (_token_hash(token), time.time()),
        ).fetchone()
    return _public(row) if row else None


def end_session(token: str | None) -> None:
    if token:
        with _connect() as connection:
            connection.execute("DELETE FROM sessions WHERE token_hash = ?", (_token_hash(token),))


# --- brute-force throttle --------------------------------------------------

_attempts: dict[str, list[float]] = {}
MAX_ATTEMPTS = 10
ATTEMPT_WINDOW_SECONDS = 15 * 60


def throttle(key: str) -> None:
    """Raise AuthError after too many tries from one client/email in the window."""
    now = time.time()
    recent = [t for t in _attempts.get(key, []) if now - t < ATTEMPT_WINDOW_SECONDS]
    if len(recent) >= MAX_ATTEMPTS:
        _attempts[key] = recent
        raise AuthError("Too many attempts. Wait a few minutes and try again.")
    recent.append(now)
    _attempts[key] = recent
    if len(_attempts) > 5000:
        for stale in [k for k, v in _attempts.items() if not v or now - v[-1] > ATTEMPT_WINDOW_SECONDS]:
            del _attempts[stale]
