import sqlite3

import pytest

from app import db

# Schema as it existed before last_synced was added — a stand-in for a real
# pre-migration production DB file, since every other test's tmp_path DB is
# created fresh via CREATE TABLE IF NOT EXISTS, which already includes the
# column and never actually exercises the ALTER TABLE branch below.
_PRE_MIGRATION_SCHEMA = """
CREATE TABLE users (
    id TEXT PRIMARY KEY, email TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE conversions (
    id TEXT PRIMARY KEY, user_id TEXT NOT NULL, budget_id TEXT NOT NULL,
    budget_name TEXT NOT NULL, account_id TEXT NOT NULL, account_name TEXT NOT NULL,
    from_currency TEXT NOT NULL, to_currency TEXT NOT NULL, start_date TEXT NOT NULL
);
INSERT INTO conversions VALUES
    ('c1', 'u1', 'b1', 'My Budget', 'a1', 'Japan Trip', 'JPY', 'USD', '2024-01-01');
"""


def test_init_migrates_pre_existing_db_without_last_synced(tmp_path):
    conn = sqlite3.connect(db.db_path(tmp_path))
    conn.executescript(_PRE_MIGRATION_SCHEMA)
    conn.commit()
    conn.close()

    db.init(tmp_path)  # must ALTER TABLE the column in, not skip it

    conn = db.connect(tmp_path)
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(conversions)")}
    assert "last_synced" in columns
    # the pre-existing row survives, with the new column defaulting to NULL
    row = conn.execute("SELECT * FROM conversions WHERE id = 'c1'").fetchone()
    assert row["account_name"] == "Japan Trip"
    assert row["last_synced"] is None
    conn.close()

    db.init(tmp_path)  # idempotent: re-running must not error on the existing column


# A stand-in for a live production DB (predating the unique constraint) that
# already has two conversions for the same account — the exact state the
# check-then-insert race could have produced. c1 is older (lower rowid).
_SCHEMA_WITH_DUPLICATE_ACCOUNT = """
CREATE TABLE users (
    id TEXT PRIMARY KEY, email TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE conversions (
    id TEXT PRIMARY KEY, user_id TEXT NOT NULL, budget_id TEXT NOT NULL,
    budget_name TEXT NOT NULL, account_id TEXT NOT NULL, account_name TEXT NOT NULL,
    from_currency TEXT NOT NULL, to_currency TEXT NOT NULL, start_date TEXT NOT NULL,
    last_synced TEXT
);
INSERT INTO conversions VALUES
    ('c1', 'u1', 'b1', 'My Budget', 'a1', 'Japan Trip', 'JPY', 'USD', '2024-01-01', NULL),
    ('c2', 'u1', 'b1', 'My Budget', 'a1', 'Japan Trip (dup)', 'JPY', 'USD', '2024-06-01', NULL),
    ('c3', 'u1', 'b1', 'My Budget', 'a2', 'Europe Trip', 'EUR', 'USD', '2024-01-01', NULL);
"""


def test_init_dedupes_pre_existing_duplicate_accounts_then_enforces_uniqueness(tmp_path):
    conn = sqlite3.connect(db.db_path(tmp_path))
    conn.executescript(_SCHEMA_WITH_DUPLICATE_ACCOUNT)
    conn.commit()
    conn.close()

    db.init(tmp_path)  # must clean up the duplicate before creating the unique index

    conn = db.connect(tmp_path)
    rows = {row["id"] for row in conn.execute("SELECT id FROM conversions")}
    # c2 (the newer duplicate) is gone; the older c1 and the unrelated c3 survive
    assert rows == {"c1", "c3"}

    # the index now actually enforces uniqueness for future inserts
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO conversions "
            "(id, user_id, budget_id, budget_name, account_id, account_name, "
            "from_currency, to_currency, start_date) "
            "VALUES ('c4', 'u1', 'b1', 'My Budget', 'a1', 'New dup', 'JPY', 'USD', '2024-01-01')"
        )
    conn.close()

    db.init(tmp_path)  # idempotent: re-running must not error
