"""Delete a user account and all its data, out-of-band.

Users can delete their own account from /settings; this CLI is for the case
where someone emails and asks *you* to do it (a data-deletion request), or for
cleaning up a test account.

IMPORTANT — run against the LIVE database, which lives in the container's
bind-mounted volume. Run it INSIDE the container:

    docker compose exec app python -m app.delete_user them@example.com

It prints what will be deleted and asks for confirmation. Add --yes to skip the
prompt (required when stdin isn't a terminal, e.g. `docker compose exec -T`).

Running `python -m app.delete_user` on the host opens a *different*, likely
empty `data/app.db` and would silently delete nothing. This CLI exits non-zero
and prints an error if the email doesn't exist, so a typo fails loudly instead
of appearing to succeed.

What it deletes: the user row (email + password hash), their YNAB OAuth tokens,
and their conversion configs. What it keeps: the anonymous `events` rows (an
opaque user id, event type, timestamp, count — no personal data, and no way
back to the person once the user row is gone). What it can't touch: anything in
YNAB itself — already-converted transactions keep their amounts and memos, and
the OAuth grant should be revoked by the user from YNAB's security settings.
"""
import sys

from . import db, events
from .config import get_settings
from .users import UserStore


def delete_user(email: str) -> str:
    settings = get_settings()
    db.init(settings.data_dir)
    store = UserStore(settings.data_dir)
    user = store.get_by_email(email)
    if user is None:
        raise SystemExit(f"No user with email {email!r} — nothing deleted.")
    store.delete(user.id)
    events.record_event(settings.data_dir, user.id, events.ACCOUNT_DELETED, detail="admin")
    return f"Deleted {user.email} and all associated data."


def _confirm(email: str, assume_yes: bool) -> None:
    if assume_yes:
        return
    if not sys.stdin.isatty():
        raise SystemExit("Not a terminal — re-run with --yes to confirm.")
    answer = input(f"Permanently delete {email} and all their data? [y/N] ")
    if answer.strip().lower() not in ("y", "yes"):
        raise SystemExit("Aborted — nothing deleted.")


if __name__ == "__main__":
    args = sys.argv[1:]
    assume_yes = "--yes" in args
    args = [a for a in args if a != "--yes"]
    if len(args) != 1:
        raise SystemExit("usage: python -m app.delete_user <email> [--yes]")
    _confirm(args[0], assume_yes)
    print(delete_user(args[0]))
