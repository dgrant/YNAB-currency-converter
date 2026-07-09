"""Append-only activity/audit log (the `events` table) + the per-user
aggregates the /admin dashboard reads.

This is a best-effort activity log, NOT a tamper-proof forensic record: an
event insert is separate from the external YNAB write it accompanies (no shared
transaction), so a failed insert is swallowed-and-logged rather than allowed to
break a user's apply. It records metadata only — event type, user id, timestamp,
an optional integer `count`, and a small display-only `detail` — never a token,
password, or transaction amount/memo.
"""
import logging
import sqlite3
import uuid
from pathlib import Path

from . import db

logger = logging.getLogger("ynabfx")

# Event types. Kept as constants so the write sites and any future reader agree
# on the exact strings.
LOGIN = "login"
SIGNUP = "signup"
APPLY = "apply"  # carries count = transactions converted
CONVERSION_CREATED = "conversion_created"
CONVERSION_UPDATED = "conversion_updated"
CONVERSION_DELETED = "conversion_deleted"
YNAB_CONNECTED = "ynab_connected"
YNAB_DISCONNECTED = "ynab_disconnected"


def record_event(
    data_dir: Path,
    user_id: str | None,
    event_type: str,
    *,
    count: int | None = None,
    detail: str | None = None,
) -> None:
    """Append one event. Best-effort: any DB error (a locked writer raises
    sqlite3.OperationalError, an FK/constraint issue raises IntegrityError — we
    catch the broad sqlite3.Error) is logged loudly to stderr and swallowed, so
    recording an event can never turn a successful user action into a 500.
    `created_at` is defaulted in-DB (see db.SCHEMA) — never pass a Python
    isoformat string, which would sort wrong against users.created_at."""
    try:
        conn = db.connect(data_dir)
        try:
            conn.execute(
                "INSERT INTO events (id, user_id, event_type, count, detail) "
                "VALUES (?, ?, ?, ?, ?)",
                (uuid.uuid4().hex, user_id, event_type, count, detail),
            )
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error:
        # Loud on purpose: a persistent write failure should be visible in the
        # logs, not silent — but it must not propagate to the caller.
        logger.exception(
            "record_event failed (type=%s user=%s) — event dropped", event_type, user_id
        )


def aggregate_by_user(data_dir: Path) -> list[dict]:
    """Per-user rows for the admin dashboard: identity plus three aggregates.

    Each aggregate is a correlated subquery, NOT a LEFT JOIN — joining `users`
    to both `conversions` and `events` and grouping would multiply the rows
    (cartesian product) and inflate both counts. `transactions_converted` is
    SUM(events.count) (only apply events carry a count), COALESCEd to 0.
    `last_activity` is the MAX across the newest event, the user's signup, and
    the newest conversion sync — MAX ignores NULLs and users.created_at is
    never NULL, so it is always populated. Metrics count post-launch activity
    only (events starts empty at deploy); `last_activity` still falls back to
    signup/last_synced so a pre-existing user is never blank.
    """
    conn = db.connect(data_dir)
    try:
        rows = conn.execute(
            """
            SELECT
                u.id, u.email, u.created_at, u.is_admin,
                (SELECT COUNT(*) FROM conversions c WHERE c.user_id = u.id)
                    AS conversions_count,
                (SELECT COALESCE(SUM(e.count), 0) FROM events e WHERE e.user_id = u.id)
                    AS transactions_converted,
                (SELECT MAX(x) FROM (
                    SELECT (SELECT MAX(e.created_at) FROM events e
                            WHERE e.user_id = u.id) AS x
                    UNION ALL SELECT u.created_at
                    UNION ALL SELECT (SELECT MAX(c.last_synced) FROM conversions c
                                      WHERE c.user_id = u.id)
                )) AS last_activity
            FROM users u
            ORDER BY u.created_at, u.id
            """
        ).fetchall()
    finally:
        conn.close()
    return [
        {
            "id": row["id"],
            "email": row["email"],
            "created_at": row["created_at"],
            "is_admin": bool(row["is_admin"]),
            "conversions_count": row["conversions_count"],
            "transactions_converted": row["transactions_converted"],
            "last_activity": row["last_activity"],
        }
        for row in rows
    ]
