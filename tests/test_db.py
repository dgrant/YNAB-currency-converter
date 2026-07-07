import sqlite3

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
