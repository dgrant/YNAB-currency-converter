"""Grant or revoke admin on a user account, out-of-band (there is no self-serve
web route for this).

IMPORTANT — run against the LIVE database, which lives in the container's
bind-mounted volume. Run it INSIDE the container:

    docker compose exec app python -m app.set_admin you@example.com
    docker compose exec app python -m app.set_admin you@example.com --revoke

Running `python -m app.set_admin` on the host opens a *different*, likely empty
`data/app.db` and would silently change nothing. This CLI exits non-zero and
prints an error if the email doesn't exist, so a typo fails loudly instead of
appearing to succeed.
"""
import sys

from . import db
from .config import get_settings
from .users import UserStore


def set_admin(email: str, is_admin: bool) -> str:
    settings = get_settings()
    db.init(settings.data_dir)
    changed = UserStore(settings.data_dir).set_admin_by_email(email, is_admin)
    if not changed:
        raise SystemExit(f"No user with email {email!r} — nothing changed.")
    return f"{email} is now {'an admin' if is_admin else 'a regular user'}."


if __name__ == "__main__":
    args = sys.argv[1:]
    revoke = "--revoke" in args
    args = [a for a in args if a != "--revoke"]
    if len(args) != 1:
        raise SystemExit("usage: python -m app.set_admin <email> [--revoke]")
    print(set_admin(args[0], is_admin=not revoke))
