"""One-shot migration from the single-user v1 layout to the multi-user DB.

Usage (inside the container or venv, with the old .env still loaded):

    python -m app.import_legacy you@example.com

Creates a user with that email whose password is the old APP_PASSWORD and
imports data/conversions.json (preserving conversion ids so existing URLs keep
working). The JSON file is renamed to conversions.json.imported afterwards.
The imported user connects their YNAB account via OAuth on first sign-in — the
old single-user YNAB_TOKEN is not migrated (the app is OAuth-only).

The user and conversions are created in a single transaction: if anything fails
partway (e.g. a malformed conversion in the JSON), nothing is committed, so a
re-run starts clean instead of getting stuck on an already-created user with no
conversions.
"""
import json
import sys
import uuid

from . import db
from .config import get_settings
from .store import _FIELDS
from .users import hash_password, normalize_email


def import_legacy(email: str) -> str:
    settings = get_settings()
    if not settings.app_password:
        raise SystemExit("APP_PASSWORD is not set — it becomes the imported user's password")
    db.init(settings.data_dir)
    email = normalize_email(email)

    json_path = settings.data_dir / "conversions.json"
    conversions = []
    if json_path.exists():
        with open(json_path) as f:
            conversions = json.load(f)

    conn = db.connect(settings.data_dir)
    try:
        if conn.execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone():
            raise SystemExit(f"A user with email {email!r} already exists — nothing imported")

        user_id = uuid.uuid4().hex
        conn.execute(
            "INSERT INTO users (id, email, password_hash) VALUES (?, ?, ?)",
            (user_id, email, hash_password(settings.app_password)),
        )
        for conversion in conversions:
            conn.execute(
                f"INSERT INTO conversions (id, user_id, {', '.join(_FIELDS)}) "
                f"VALUES (?, ?, {', '.join('?' * len(_FIELDS))})",
                (conversion["id"], user_id, *(conversion[f] for f in _FIELDS)),
            )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()

    if conversions:
        json_path.rename(json_path.with_suffix(".json.imported"))

    return (
        f"Created user {email} (password = old APP_PASSWORD), "
        f"imported {len(conversions)} conversion(s). "
        "They connect YNAB via OAuth on first sign-in."
    )


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: python -m app.import_legacy <email>")
    print(import_legacy(sys.argv[1]))
