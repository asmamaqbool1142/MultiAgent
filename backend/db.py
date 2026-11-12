"""One small database layer: SQLite on a laptop, Postgres (DATABASE_URL, e.g. Neon) when deployed."""

import os
import sqlite3
from pathlib import Path

try:
    import psycopg
    from psycopg.rows import dict_row

    INTEGRITY_ERRORS: tuple[type[Exception], ...] = (sqlite3.IntegrityError, psycopg.IntegrityError)
except ImportError:  # psycopg is only needed when DATABASE_URL is set
    psycopg = None
    INTEGRITY_ERRORS = (sqlite3.IntegrityError,)


def using_postgres() -> bool:
    return bool(os.getenv("DATABASE_URL"))


class Database:
    """Context manager that commits on success, rolls back on error, and always closes.

    Queries are written with SQLite-style `?` placeholders; they're translated for Postgres.
    """

    def __init__(self, connection, postgres: bool) -> None:
        self.connection = connection
        self.postgres = postgres

    def execute(self, sql: str, params: tuple = ()):
        return self.connection.execute(sql.replace("?", "%s") if self.postgres else sql, params)

    def executemany(self, sql: str, rows) -> None:
        for row in rows:
            self.execute(sql, row)

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        try:
            if exc_type is None:
                self.connection.commit()
            else:
                self.connection.rollback()
        finally:
            self.connection.close()


def connect(sqlite_path: Path) -> Database:
    if using_postgres():
        if psycopg is None:
            raise RuntimeError("DATABASE_URL is set but psycopg is not installed.")
        return Database(psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row, connect_timeout=15), True)
    sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(sqlite_path, timeout=15)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 15000")
    return Database(connection, False)
