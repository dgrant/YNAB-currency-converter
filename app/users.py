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


def _row_to_user(row) -> User:
    # refresh_on_load is stored 0/1; some code paths (tests, older rows) may
    # not carry it, so default to off.
    keys = row.keys() if hasattr(row, "keys") else []
    return User(
        id=row["id"],
        email=row["email"],
        password_hash=row["password_hash"],
        refresh_on_load=bool(row["refresh_on_load"]) if "refresh_on_load" in keys else False,
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
