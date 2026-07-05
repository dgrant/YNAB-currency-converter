"""One-shot migration from the single-user v1 layout to the multi-user DB.

Usage (inside the container or venv, with the old .env still loaded):

    python -m app.import_legacy you@example.com

Creates a user with that email whose password is the old APP_PASSWORD,
stores the old YNAB_TOKEN as the user's personal-access-token connection,
and imports data/conversions.json (preserving conversion ids so existing
URLs keep working). The JSON file is renamed to conversions.json.imported
afterwards. Safe to re-run: it refuses to touch an email that already exists.
"""
import json
import sys

from . import db
from .config import get_settings
from .connections import ConnectionStore
from .store import _FIELDS
from .users import UserStore


def import_legacy(email: str) -> str:
    settings = get_settings()
    if not settings.app_password:
        raise SystemExit("APP_PASSWORD is not set — it becomes the imported user's password")
    db.init(settings.data_dir)

    users = UserStore(settings.data_dir)
    if users.get_by_email(email) is not None:
        raise SystemExit(f"A user with email {email!r} already exists — nothing imported")
    user = users.create(email, settings.app_password)

    if settings.ynab_token:
        ConnectionStore(settings.data_dir).set_pat(user.id, settings.ynab_token)

    json_path = settings.data_dir / "conversions.json"
    imported = 0
    if json_path.exists():
        with open(json_path) as f:
            conversions = json.load(f)
        conn = db.connect(settings.data_dir)
        try:
            for conversion in conversions:
                conn.execute(
                    f"INSERT INTO conversions (id, user_id, {', '.join(_FIELDS)}) "
                    f"VALUES (?, ?, {', '.join('?' * len(_FIELDS))})",
                    (conversion["id"], user.id, *(conversion[f] for f in _FIELDS)),
                )
            conn.commit()
        finally:
            conn.close()
        imported = len(conversions)
        json_path.rename(json_path.with_suffix(".json.imported"))

    return (
        f"Created user {user.email} (password = old APP_PASSWORD), "
        f"{'stored YNAB_TOKEN as their connection, ' if settings.ynab_token else ''}"
        f"imported {imported} conversion(s)."
    )


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: python -m app.import_legacy <email>")
    print(import_legacy(sys.argv[1]))
