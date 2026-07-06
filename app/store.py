"""Per-user conversion configs, persisted in SQLite (see db.py)."""
import uuid
from pathlib import Path

from . import db

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


def _row_to_dict(row) -> dict:
    return {**{key: row[key] for key in ("id", *_FIELDS)}, "last_synced": row["last_synced"]}


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
        conversion = {"id": uuid.uuid4().hex[:8], **conversion}
        conn = db.connect(self.data_dir)
        try:
            conn.execute(
                f"INSERT INTO conversions (id, user_id, {', '.join(_FIELDS)}) "
                f"VALUES (?, ?, {', '.join('?' * len(_FIELDS))})",
                (conversion["id"], user_id, *(conversion[f] for f in _FIELDS)),
            )
            conn.commit()
        finally:
            conn.close()
        return conversion

    def update(self, user_id: str, conversion_id: str, fields: dict) -> dict | None:
        """Merge fields into an existing conversion; None if it doesn't exist."""
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
