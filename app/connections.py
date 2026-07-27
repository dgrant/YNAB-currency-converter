"""Per-user YNAB OAuth credentials (access token + refresh token)."""
import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from . import db

logger = logging.getLogger("ynabfx")


@dataclass(frozen=True)
class YNABConnection:
    user_id: str
    kind: str  # 'oauth' (the only kind created; legacy 'pat' rows are re-prompted)
    access_token: str
    refresh_token: str | None
    expires_at: float | None  # unix time


class ConnectionStore:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir

    def get(self, user_id: str) -> YNABConnection | None:
        conn = db.connect(self.data_dir)
        try:
            row = conn.execute(
                "SELECT * FROM ynab_connections WHERE user_id = ?", (user_id,)
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        return YNABConnection(
            user_id=row["user_id"],
            kind=row["kind"],
            access_token=row["access_token"],
            refresh_token=row["refresh_token"],
            expires_at=row["expires_at"],
        )

    def set_oauth(
        self, user_id: str, access_token: str, refresh_token: str, expires_at: float
    ) -> None:
        self._upsert(user_id, "oauth", access_token, refresh_token, expires_at)

    def delete(self, user_id: str) -> None:
        conn = db.connect(self.data_dir)
        try:
            conn.execute("DELETE FROM ynab_connections WHERE user_id = ?", (user_id,))
            conn.commit()
        finally:
            conn.close()

    def _upsert(
        self,
        user_id: str,
        kind: str,
        access_token: str,
        refresh_token: str | None,
        expires_at: float | None,
    ) -> None:
        conn = db.connect(self.data_dir)
        try:
            conn.execute(
                "INSERT INTO ynab_connections "
                "(user_id, kind, access_token, refresh_token, expires_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT (user_id) DO UPDATE SET kind = excluded.kind, "
                "access_token = excluded.access_token, "
                "refresh_token = excluded.refresh_token, "
                "expires_at = excluded.expires_at",
                (user_id, kind, access_token, refresh_token, expires_at),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            # The only constraint reachable here is the FK to users: the account
            # was deleted while this request was refreshing its token. Storing
            # the new token is exactly what must NOT happen, so treat it as a
            # no-op rather than letting it escape as a 500 — the next request
            # from that (now dead) session gets bounced to /login anyway.
            logger.info("Dropped a YNAB token for a user deleted mid-request")
        finally:
            conn.close()
