"""Per-user YNAB OAuth credentials (access token + refresh token)."""
import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from . import db

logger = logging.getLogger("ynabfx")


class ConnectionGoneError(Exception):
    """Storing a token failed because the user no longer exists — the account
    was deleted while this request was refreshing. The caller must abort
    rather than carry on with a token it could not persist (see
    oauth.get_access_token); continuing would let a request keep reading and
    writing the YNAB budget of an account already reported as deleted."""


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
        except sqlite3.IntegrityError as exc:
            # The only constraint reachable here is the FK to users: the account
            # was deleted while this request was refreshing its token. Storing
            # the token is exactly what must NOT happen — but neither may we
            # return normally, because the caller would then go on using a live
            # access token for a deleted account. Signal it so the request
            # stops; the raw IntegrityError would surface as a 500.
            logger.info("Dropped a YNAB token for a user deleted mid-request")
            raise ConnectionGoneError(user_id) from exc
        finally:
            conn.close()
