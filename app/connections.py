"""Per-user YNAB credentials: a personal access token or OAuth token pair."""
from dataclasses import dataclass
from pathlib import Path

from . import db


@dataclass(frozen=True)
class YNABConnection:
    user_id: str
    kind: str  # 'pat' | 'oauth'
    access_token: str
    refresh_token: str | None
    expires_at: float | None  # unix time; None for PATs


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

    def set_pat(self, user_id: str, token: str) -> None:
        self._upsert(user_id, "pat", token, None, None)

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
        finally:
            conn.close()
