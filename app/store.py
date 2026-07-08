"""Per-user conversion configs, persisted in SQLite (see db.py)."""
import sqlite3
import uuid
from pathlib import Path

from . import db


class DuplicateAccountError(Exception):
    """Raised when an insert/update would violate the (user_id, account_id)
    uniqueness constraint (see db._dedupe_and_index_conversions) — the DB-level
    backstop behind the application-level duplicate check in
    routes/conversions.py, closing the check-then-insert race a plain
    pre-check can't."""

    def __init__(self, account_id: str) -> None:
        super().__init__(f"Account {account_id} already has a conversion")
        self.account_id = account_id

# User-editable fields, written by add()/update(). last_synced is managed
# separately (mark_synced) since it's set by preview/apply, not the form.
_FIELDS = (
    "budget_id",
    "budget_name",
    "account_id",
    "account_name",
    "from_currency",
    "to_currency",
    "start_date",
)


# Store-managed (non-form) columns, set by mark_synced / set_pending, not by
# add()/update(). Kept out of _FIELDS so a form write can't clobber them.
_MANAGED = ("last_synced", "pending_count", "pending_checked_at")


def _row_to_dict(row) -> dict:
    return {
        **{key: row[key] for key in ("id", *_FIELDS)},
        **{key: row[key] for key in _MANAGED},
    }


class ConversionStore:
    """CRUD for one user's conversions; every method is scoped by user_id."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir

    def load(self, user_id: str) -> list[dict]:
        conn = db.connect(self.data_dir)
        try:
            rows = conn.execute(
                "SELECT * FROM conversions WHERE user_id = ? ORDER BY rowid", (user_id,)
            ).fetchall()
        finally:
            conn.close()
        return [_row_to_dict(row) for row in rows]

    def get(self, user_id: str, conversion_id: str) -> dict | None:
        conn = db.connect(self.data_dir)
        try:
            row = conn.execute(
                "SELECT * FROM conversions WHERE user_id = ? AND id = ?",
                (user_id, conversion_id),
            ).fetchone()
        finally:
            conn.close()
        return _row_to_dict(row) if row else None

    def add(self, user_id: str, conversion: dict) -> dict:
        """Raises DuplicateAccountError if (user_id, account_id) already
        exists — the DB-level backstop behind the caller's own pre-check."""
        conversion = {"id": uuid.uuid4().hex[:8], **conversion}
        conn = db.connect(self.data_dir)
        try:
            conn.execute(
                f"INSERT INTO conversions (id, user_id, {', '.join(_FIELDS)}) "
                f"VALUES (?, ?, {', '.join('?' * len(_FIELDS))})",
                (conversion["id"], user_id, *(conversion[f] for f in _FIELDS)),
            )
            conn.commit()
        except sqlite3.IntegrityError as exc:
            raise DuplicateAccountError(conversion["account_id"]) from exc
        finally:
            conn.close()
        return conversion

    def add_many(self, user_id: str, conversions: list[dict]) -> list[dict]:
        """Insert several conversions in one connection/transaction (see
        delete_many — same reasoning: batch-create shouldn't open/commit/close
        a separate connection per row). A no-op for an empty list.

        Falls back to inserting one at a time — skipping any that collide
        with the (user_id, account_id) uniqueness constraint — if the fast
        batched insert hits a collision. That's a rare race (another
        concurrent request inserted one of these accounts between the
        caller's own duplicate check and this call), not the common case, so
        the fallback trades a little speed for not losing the rest of a
        large batch over one collision."""
        if not conversions:
            return []
        prepared = [{"id": uuid.uuid4().hex[:8], **c} for c in conversions]
        conn = db.connect(self.data_dir)
        try:
            conn.executemany(
                f"INSERT INTO conversions (id, user_id, {', '.join(_FIELDS)}) "
                f"VALUES (?, ?, {', '.join('?' * len(_FIELDS))})",
                [(c["id"], user_id, *(c[f] for f in _FIELDS)) for c in prepared],
            )
            conn.commit()
            return prepared
        except sqlite3.IntegrityError:
            conn.rollback()
        finally:
            conn.close()

        inserted = []
        for c in conversions:
            try:
                inserted.append(self.add(user_id, c))
            except DuplicateAccountError:
                continue
        return inserted

    def update(self, user_id: str, conversion_id: str, fields: dict) -> dict | None:
        """Merge fields into an existing conversion; None if it doesn't exist.
        Raises DuplicateAccountError if the merged account_id collides with
        another of this user's conversions (the DB-level backstop, same as
        add())."""
        existing = self.get(user_id, conversion_id)
        if existing is None:
            return None
        updated = {**existing, **fields, "id": conversion_id}
        conn = db.connect(self.data_dir)
        try:
            conn.execute(
                f"UPDATE conversions SET {', '.join(f'{f} = ?' for f in _FIELDS)} "
                "WHERE user_id = ? AND id = ?",
                (*(updated[f] for f in _FIELDS), user_id, conversion_id),
            )
            conn.commit()
        except sqlite3.IntegrityError as exc:
            raise DuplicateAccountError(updated["account_id"]) from exc
        finally:
            conn.close()
        return updated

    def mark_synced(self, user_id: str, conversion_id: str, when: str) -> None:
        """Record that this conversion was just previewed/applied against YNAB."""
        conn = db.connect(self.data_dir)
        try:
            conn.execute(
                "UPDATE conversions SET last_synced = ? WHERE user_id = ? AND id = ?",
                (when, user_id, conversion_id),
            )
            conn.commit()
        finally:
            conn.close()

    def set_pending(
        self, user_id: str, conversion_id: str, count: int, checked_at: str
    ) -> None:
        """Cache the pending-transaction count for the index badges. Written
        by any path that just fetched this conversion's transactions (preview,
        apply, on-load refresh) — a focused write so it can't clobber a
        concurrent edit of the config fields."""
        conn = db.connect(self.data_dir)
        try:
            conn.execute(
                "UPDATE conversions SET pending_count = ?, pending_checked_at = ? "
                "WHERE user_id = ? AND id = ?",
                (count, checked_at, user_id, conversion_id),
            )
            conn.commit()
        finally:
            conn.close()

    def set_start_date(self, user_id: str, conversion_id: str, start_date: str) -> None:
        """Advance the fetch floor after an apply (see routes/conversions.apply).
        A focused write, not update(), so it can't clobber a concurrent edit of
        the other fields."""
        conn = db.connect(self.data_dir)
        try:
            conn.execute(
                "UPDATE conversions SET start_date = ? WHERE user_id = ? AND id = ?",
                (start_date, user_id, conversion_id),
            )
            conn.commit()
        finally:
            conn.close()

    def delete_many(self, user_id: str, conversion_ids: list[str]) -> None:
        """Delete several conversions in one connection/transaction. Each id is
        still scoped by user_id, so ids the caller doesn't own are silently
        skipped, same as delete(). A no-op for an empty list."""
        if not conversion_ids:
            return
        conn = db.connect(self.data_dir)
        try:
            placeholders = ", ".join("?" * len(conversion_ids))
            conn.execute(
                f"DELETE FROM conversions WHERE user_id = ? AND id IN ({placeholders})",
                (user_id, *conversion_ids),
            )
            conn.commit()
        finally:
            conn.close()

    def delete(self, user_id: str, conversion_id: str) -> None:
        conn = db.connect(self.data_dir)
        try:
            conn.execute(
                "DELETE FROM conversions WHERE user_id = ? AND id = ?",
                (user_id, conversion_id),
            )
            conn.commit()
        finally:
            conn.close()
