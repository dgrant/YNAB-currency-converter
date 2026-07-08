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
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    refresh_on_load INTEGER NOT NULL DEFAULT 0
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
    start_date    TEXT NOT NULL,
    last_synced   TEXT,
    pending_count      INTEGER,
    pending_checked_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_conversions_user ON conversions(user_id);
"""

# Columns added after the table first shipped. CREATE TABLE IF NOT EXISTS won't
# touch an existing table, so bring older DBs up to date with idempotent
# ALTERs. Each entry is (table, column, definition).
_MIGRATIONS = (
    ("conversions", "last_synced", "TEXT"),
    # Cached pending-transaction count + when it was last computed, so the
    # index can show per-account "N pending" badges without a YNAB fetch on
    # page load. Written by preview/apply and the opt-in on-load refresh; see
    # store.set_pending and routes/conversions.py.
    ("conversions", "pending_count", "INTEGER"),
    ("conversions", "pending_checked_at", "TEXT"),
    # Per-user opt-in: refresh stale pending counts on GET /conversions.
    # Default 0 (off) — behavior is unchanged until a user turns it on.
    ("users", "refresh_on_load", "INTEGER NOT NULL DEFAULT 0"),
)


def _apply_migrations(conn: sqlite3.Connection) -> None:
    for table, column, definition in _MIGRATIONS:
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _dedupe_and_index_conversions(conn: sqlite3.Connection) -> None:
    """Enforce "one conversion per account" at the DB level, closing a
    check-then-insert race the application-level check alone couldn't (two
    concurrent requests both passing the "not already used" check before
    either commits — see TODOS.md). Must run the cleanup BEFORE creating the
    unique index: creating a UNIQUE index over rows that already violate it
    would fail outright, and this runs on every init() including against the
    live production DB, which predates this constraint.

    The cleanup keeps the oldest (lowest rowid) row per (user_id, account_id)
    pair — the one most likely to already have synced/applied history against
    it — and deletes any newer duplicates. This never touches YNAB itself,
    only this app's own conversion config rows. A no-op on a DB with no
    duplicates (the overwhelming common case, and true for every DB from here
    on since the index then prevents new ones), so safe to run on every
    init()."""
    conn.execute(
        "DELETE FROM conversions WHERE rowid NOT IN "
        "(SELECT MIN(rowid) FROM conversions GROUP BY user_id, account_id)"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_conversions_user_account "
        "ON conversions(user_id, account_id)"
    )


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
        _apply_migrations(conn)
        _dedupe_and_index_conversions(conn)
        conn.commit()
    finally:
        conn.close()
