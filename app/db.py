"""SQLite persistence. One file (data/app.db), stdlib sqlite3, no ORM.

Connections are opened per operation (cheap for SQLite, and safe with
FastAPI's threadpool for sync routes). WAL mode keeps concurrent
readers/writers from blocking each other.
"""
import logging
import sqlite3
import time
from pathlib import Path

logger = logging.getLogger("ynabfx")

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
    pending_checked_at TEXT,
    default_category_id   TEXT,
    default_category_name TEXT,
    approve_on_apply      INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_conversions_user ON conversions(user_id);

CREATE TABLE IF NOT EXISTS events (
    id          TEXT PRIMARY KEY,
    -- Nullable on purpose: a failed-login event (added later) has no user.
    -- There is deliberately no "REFERENCES users(id) ON DELETE CASCADE" — not
    -- so rows outlive their user (users.py: UserStore.delete removes them
    -- explicitly), but because the cascade only fires while the per-connection
    -- foreign_keys pragma is on, which is too fragile a thing to hang deletion
    -- correctness on. The ONE row that outlives a user is the account_deleted
    -- marker, written after the delete: a dangling uuid and a date, recording
    -- that a deletion happened without recording whose.
    user_id     TEXT,
    event_type  TEXT NOT NULL,
    -- The one summable quantity (e.g. transactions converted on an apply), in
    -- its own column so the per-user metric is SUM(count), never json_extract
    -- over `detail`. NULL for events that have no count.
    count       INTEGER,
    -- Display-only extras (e.g. account_id). Never summed; never holds a token,
    -- password, or transaction amount/memo — see the memo marker rules. It DOES
    -- hold real YNAB account ids, which is why UserStore.delete drops these
    -- rows rather than keeping them as an "anonymous" activity log.
    detail      TEXT,
    -- Defaulted in-DB so it matches users.created_at's datetime('now') format
    -- exactly (space-separated, no 'T'); record_event never passes a Python
    -- isoformat string, which would sort wrong in the last-activity MAX().
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_events_user_created ON events(user_id, created_at);
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
    # Admin flag for the /admin dashboard. Default 0 (off); flipped out-of-band
    # by `python -m app.set_admin <email>`. On an existing DB this ALTERs in;
    # a fresh DB gets it via CREATE TABLE. The matching read is _row_to_user in
    # users.py — miss that and require_admin 404s everyone, David included.
    ("users", "is_admin", "INTEGER NOT NULL DEFAULT 0"),
    # Per-account default YNAB category applied to converted transactions, and
    # whether an apply also flips YNAB's `approved` flag. The name is stored
    # alongside the id (denormalized, like budget_name/account_name) so the
    # preview banner and detail page show it without a categories fetch. Both
    # written by the conversion form; read by store._CONFIG. approve_on_apply
    # defaults to 0 (opt-in) so shipping this never silently auto-approves an
    # existing user's next pile — see the design doc's gate decision.
    ("conversions", "default_category_id", "TEXT"),
    ("conversions", "default_category_name", "TEXT"),
    ("conversions", "approve_on_apply", "INTEGER NOT NULL DEFAULT 0"),
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


# How hard to try to truncate the WAL before giving up and logging.
_CHECKPOINT_ATTEMPTS = 3
_CHECKPOINT_RETRY_SECONDS = 0.1
# A TRUNCATE checkpoint invokes the busy handler, so it inherits `busy_timeout`
# (5s) and each attempt can block for all of it — 15s per delete, on the request
# path, with new writers stalled behind the pending checkpoint. That is the same
# single-writer monopoly that got VACUUM moved off this path, so the checkpoint
# gets its own short timeout: fail fast, log, let a later checkpoint finish it.
_CHECKPOINT_BUSY_TIMEOUT_MS = 250


def checkpoint_wal(conn: sqlite3.Connection) -> bool:
    """Fold the write-ahead log into the database and truncate it. Returns
    whether it actually completed.

    Called after deleting an account. `secure_delete` zeroes the freed pages,
    but in WAL mode that zeroing is written to the -wal file while the
    PRE-DELETE copy of the page stays in `app.db` until a checkpoint folds the
    new version over it. So until this succeeds, the email, password hash and
    YNAB tokens of a long-lived row are still readable **in the main database
    file** — the one any `cp`/`docker cp`/volume snapshot backup picks up.
    (Only on a young row that was never checkpointed does the residue sit in
    the -wal instead, which is what a fresh test database produces. Both are
    verified; production is the first case.)

    `PRAGMA wal_checkpoint(TRUNCATE)` does NOT raise when it cannot finish —
    it returns `(busy, log_frames, checkpointed)`, and `busy = 1` means a
    reader on an older snapshot blocked it and the old pages are still on
    disk. Treating that as success is how a deletion gets reported as
    permanent while the data is still readable, so retry briefly and log
    loudly if it never clears. Never raises: the rows are already deleted and
    the account is gone either way, so this must not turn a completed deletion
    into a 500.
    """
    try:
        previous_timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
        conn.execute(f"PRAGMA busy_timeout = {_CHECKPOINT_BUSY_TIMEOUT_MS}")
    except sqlite3.Error:
        logger.exception("WAL checkpoint could not set its busy timeout")
        return False
    try:
        for attempt in range(_CHECKPOINT_ATTEMPTS):
            try:
                row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            except sqlite3.Error:
                logger.exception("WAL checkpoint failed outright")
                return False
            # row is None on a non-WAL database (nothing to checkpoint).
            if row is None or not row[0]:
                return True
            if attempt + 1 < _CHECKPOINT_ATTEMPTS:
                time.sleep(_CHECKPOINT_RETRY_SECONDS)
        logger.error(
            "WAL checkpoint still busy after %d attempts — the pre-delete copy of "
            "the deleted rows may remain readable in the main database file until "
            "a later checkpoint succeeds",
            _CHECKPOINT_ATTEMPTS,
        )
        return False
    finally:
        try:
            conn.execute(f"PRAGMA busy_timeout = {previous_timeout}")
        except sqlite3.Error:
            logger.exception("WAL checkpoint could not restore the busy timeout")


def vacuum(data_dir: Path) -> None:
    """Rebuild the database file, reclaiming pages freed before
    `PRAGMA secure_delete` was turned on (older deletions left their bytes
    readable in the free list).

    Deliberately NOT called from any request path: VACUUM rewrites the whole
    file under an exclusive lock, which on a single worker is a denial of
    service waiting to happen. Maintenance only — the delete_user CLI and the
    manual step in DEPLOY.md.
    """
    conn = connect(data_dir)
    try:
        conn.execute("VACUUM")
        checkpoint_wal(conn)
    finally:
        conn.close()


def connect(data_dir: Path) -> sqlite3.Connection:
    """Open a connection with the pragmas the app relies on. Caller closes."""
    data_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path(data_dir))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    # Overwrite deleted content with zeros instead of leaving it readable in
    # free pages. Without this, "delete my account" leaves the email, password
    # hash and both YNAB tokens recoverable verbatim in app.db (and in every
    # backup taken afterwards) until those pages happen to be reused — which
    # would make the privacy policy's deletion promise untrue. Negligible cost
    # at this DB's size. Applies to future deletes only; `db.vacuum` (run by
    # the delete_user CLI, and documented in DEPLOY.md) reclaims pages freed
    # before this was turned on.
    conn.execute("PRAGMA secure_delete = ON")
    # Wait up to 5s for a competing writer instead of raising SQLITE_BUSY
    # immediately. WAL allows concurrent readers but still a single writer, and
    # sync routes run in a threadpool (plus recording an event now adds a write
    # to every action), so brief write contention is expected. Benefits every
    # writer, not just events.
    conn.execute("PRAGMA busy_timeout = 5000")
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
