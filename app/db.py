"""SQLite persistence. One file (data/app.db), stdlib sqlite3, no ORM.

Connections are opened per operation (cheap for SQLite, and safe with
FastAPI's threadpool for sync routes). WAL mode keeps concurrent
readers/writers from blocking each other.
"""
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            TEXT PRIMARY KEY,
    email         TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS ynab_connections (
    user_id       TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    kind          TEXT NOT NULL CHECK (kind IN ('pat', 'oauth')),
    access_token  TEXT NOT NULL,
    refresh_token TEXT,
    expires_at    REAL,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS conversions (
    id            TEXT PRIMARY KEY,
    user_id       TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    budget_id     TEXT NOT NULL,
    budget_name   TEXT NOT NULL,
    account_id    TEXT NOT NULL,
    account_name  TEXT NOT NULL,
    from_currency TEXT NOT NULL,
    to_currency   TEXT NOT NULL,
    start_date    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_conversions_user ON conversions(user_id);
"""


def db_path(data_dir: Path) -> Path:
    return data_dir / "app.db"


def connect(data_dir: Path) -> sqlite3.Connection:
    """Open a connection with the pragmas the app relies on. Caller closes."""
    data_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path(data_dir))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init(data_dir: Path) -> None:
    """Create tables if missing. Called at app startup and by CLI tools."""
    conn = connect(data_dir)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()
