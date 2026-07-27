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

What it deletes: everything belonging to that account — the user row (email +
password hash), their YNAB OAuth tokens, their conversion configs, and their
activity-log rows. `PRAGMA secure_delete` zeroes those bytes as they are
freed; the CLI then VACUUMs to reclaim pages freed by *older* deletions, from
before that pragma existed. What it keeps: one `account_deleted` row carrying a dangling uuid and a
date, so the log still shows a deletion happened. What it can't touch: anything
in YNAB itself — already-converted transactions keep their amounts and memos,
and the OAuth grant should be revoked by the user from YNAB's security
settings.
"""
import sys

from . import auth, db, events
from .config import get_settings
from .users import UserStore


def delete_user(email: str) -> str:
    settings = get_settings()
    db.init(settings.data_dir)
    store = UserStore(settings.data_dir)
    user = store.get_by_email(email)
    if user is None:
        raise SystemExit(f"No user with email {email!r} — nothing deleted.")
    if not store.delete(user.id):
        # Only reachable if the row vanished between the lookup and the delete
        # (a concurrent self-serve deletion). Report it rather than printing a
        # success line for work that didn't happen.
        raise SystemExit(f"{user.email} disappeared mid-delete — nothing to do.")
    events.record_event(settings.data_dir, user.id, events.ACCOUNT_DELETED, detail="admin")
    auth.clear_login_failures(user.email)  # the email is a live throttle key
    # Compaction lives here, not in UserStore.delete: VACUUM rewrites the whole
    # file under an exclusive lock, which is fine for an operator running one
    # command and unacceptable on a request path. It reclaims pages freed
    # before secure_delete was enabled; the delete itself is already zeroed.
    db.vacuum(settings.data_dir)
    return f"Deleted {user.email} and all associated data."


def _confirm(email: str, assume_yes: bool) -> None:
    if assume_yes:
        return
    if not sys.stdin.isatty():
        raise SystemExit("Not a terminal — re-run with --yes to confirm.")
    answer = input(f"Permanently delete {email} and all their data? [y/N] ")
    if answer.strip().lower() not in ("y", "yes"):
        raise SystemExit("Aborted — nothing deleted.")


def main(argv: list[str]) -> str:
    """Parse args, confirm, delete. Split out from the __main__ block below so
    it can be tested: this is the whole front door to an irreversible
    operation, and "python -m app.delete_user --yes" with no email must fail
    loudly rather than do something surprising."""
    assume_yes = "--yes" in argv
    args = [a for a in argv if a != "--yes"]
    if len(args) != 1:
        raise SystemExit("usage: python -m app.delete_user <email> [--yes]")
    _confirm(args[0], assume_yes)
    return delete_user(args[0])


if __name__ == "__main__":
    print(main(sys.argv[1:]))
