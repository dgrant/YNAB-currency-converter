"""User accounts: scrypt password hashing (stdlib) and the user store."""
import hashlib
import hmac
import secrets
import uuid
from dataclasses import dataclass
from pathlib import Path

from . import db

# Interactive-login scrypt parameters (libsodium's "interactive" tier).
_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2**14, 8, 1


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P
    )
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt_hex, digest_hex = stored.split("$")
        if scheme != "scrypt":
            return False
        digest = hashlib.scrypt(
            password.encode(), salt=bytes.fromhex(salt_hex), n=int(n), r=int(r), p=int(p)
        )
        return hmac.compare_digest(digest, bytes.fromhex(digest_hex))
    except (ValueError, TypeError):
        return False


def normalize_email(email: str) -> str:
    return email.strip().lower()


@dataclass(frozen=True)
class User:
    id: str
    email: str
    password_hash: str
    refresh_on_load: bool = False
    is_admin: bool = False


def _row_to_user(row) -> User:
    # refresh_on_load / is_admin are stored 0/1; some code paths (tests, rows
    # read before the migration ALTERs the column in) may not carry them, so
    # default to off. The is_admin guard is load-bearing: drop it and every
    # user reads is_admin=False, so require_admin 404s everyone, David included.
    keys = row.keys() if hasattr(row, "keys") else []
    return User(
        id=row["id"],
        email=row["email"],
        password_hash=row["password_hash"],
        refresh_on_load=bool(row["refresh_on_load"]) if "refresh_on_load" in keys else False,
        is_admin=bool(row["is_admin"]) if "is_admin" in keys else False,
    )


class UserStore:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir

    def create(self, email: str, password: str) -> User:
        """Insert a new user; raises sqlite3.IntegrityError if the email exists."""
        user = User(
            id=uuid.uuid4().hex, email=normalize_email(email), password_hash=hash_password(password)
        )
        conn = db.connect(self.data_dir)
        try:
            conn.execute(
                "INSERT INTO users (id, email, password_hash) VALUES (?, ?, ?)",
                (user.id, user.email, user.password_hash),
            )
            conn.commit()
        finally:
            conn.close()
        return user

    def get(self, user_id: str) -> User | None:
        conn = db.connect(self.data_dir)
        try:
            row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        finally:
            conn.close()
        return _row_to_user(row) if row else None

    def get_by_email(self, email: str) -> User | None:
        conn = db.connect(self.data_dir)
        try:
            row = conn.execute(
                "SELECT * FROM users WHERE email = ?", (normalize_email(email),)
            ).fetchone()
        finally:
            conn.close()
        return _row_to_user(row) if row else None

    def delete(self, user_id: str) -> bool:
        """Delete a user and every row belonging to them. Returns True if the
        user existed, False if there was nothing to delete (so the CLI can fail
        loudly on a typo, same as set_admin_by_email).

        Every per-user table is listed here explicitly rather than left to
        ON DELETE CASCADE, and all of it runs in ONE transaction so a failure
        can't leave an orphaned YNAB token behind: the cascade only fires while
        `PRAGMA foreign_keys = ON` is set (db.connect sets it, but that is a
        per-connection pragma, not a property of the schema), and a deletion
        that half-happens is exactly the failure mode a data-deletion request
        must not have. **Any NEW per-user table must be added to this method**,
        or a deleted account will leave data behind.

        That includes `events`. An earlier design kept those rows as an
        "anonymous" activity log, but nothing can read them once the user row
        is gone (`events.aggregate_by_user` selects FROM users), and
        `events.detail` holds real YNAB account ids — so retaining them cost
        privacy surface and bought nothing. The caller writes a single
        account_deleted event *after* this returns; that dangling uuid plus a
        date is all that survives, recording that a deletion happened without
        recording whose.

        VACUUM runs after the commit, outside the transaction: `secure_delete`
        zeroes pages freed from here on, but the live DB predates that pragma,
        so rebuilding the file is what actually purges bytes freed earlier. The
        WAL checkpoint then truncates the write-ahead log, which would
        otherwise still hold the pre-delete copy of those pages.
        """
        conn = db.connect(self.data_dir)
        try:
            with conn:  # commits on success, rolls back on any exception
                conn.execute("DELETE FROM conversions WHERE user_id = ?", (user_id,))
                conn.execute("DELETE FROM ynab_connections WHERE user_id = ?", (user_id,))
                conn.execute("DELETE FROM events WHERE user_id = ?", (user_id,))
                cur = conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
            deleted = cur.rowcount > 0
            if deleted:
                conn.execute("VACUUM")
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            return deleted
        finally:
            conn.close()

    def set_refresh_on_load(self, user_id: str, enabled: bool) -> None:
        """Toggle the per-user 'refresh pending counts on page load' opt-in."""
        conn = db.connect(self.data_dir)
        try:
            conn.execute(
                "UPDATE users SET refresh_on_load = ? WHERE id = ?",
                (1 if enabled else 0, user_id),
            )
            conn.commit()
        finally:
            conn.close()

    def set_admin_by_email(self, email: str, is_admin: bool) -> bool:
        """Flip the admin flag for the user with this email. Returns True if a
        row was updated, False if no such user exists (so the CLI can fail
        loudly on a typo instead of silently no-opping). Set out-of-band, not
        via any web route — there is deliberately no self-serve admin grant."""
        conn = db.connect(self.data_dir)
        try:
            cur = conn.execute(
                "UPDATE users SET is_admin = ? WHERE email = ?",
                (1 if is_admin else 0, normalize_email(email)),
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()
