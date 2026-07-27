"""Account deletion: the self-serve /settings flow, the delete_user CLI, and
the guarantee that a deleted account leaves no per-user rows behind."""
import io
import sqlite3
import sys

import pytest

from app import db, events
from app.config import get_settings
from app.connections import ConnectionStore
from app.routes.settings import WRONG_PASSWORD_ERROR
from app.store import ConversionStore
from app.users import UserStore
from tests.test_app_flow import EMAIL, PASSWORD, connect_ynab, get_csrf, signup

OTHER_EMAIL = "other@example.com"
INTACT = {"users": 1, "ynab_connections": 1, "conversions": 1}
GONE = {"users": 0, "ynab_connections": 0, "conversions": 0}


def _conv(account_id="acct-1"):
    return {
        "budget_id": "b1",
        "budget_name": "Plan",
        "account_id": account_id,
        "account_name": "Chequing",
        "from_currency": "JPY",
        "to_currency": "CAD",
        "start_date": "2024-01-01",
    }


def _seed_account(client, email=EMAIL, account_id="acct-1"):
    """Sign up, connect YNAB, and store one conversion. Returns (data_dir, user)."""
    token = signup(client, email=email)
    connect_ynab(client, email=email)
    data_dir = get_settings().data_dir
    user = UserStore(data_dir).get_by_email(email)
    ConversionStore(data_dir).add(user.id, _conv(account_id))
    return data_dir, user, token


def _delete_account(client, token, password=PASSWORD):
    return client.post(
        "/settings/delete-account",
        data={"password": password, "csrf_token": token},
        follow_redirects=False,
    )


def _event_types(data_dir, user_id):
    conn = db.connect(data_dir)
    try:
        rows = conn.execute(
            "SELECT event_type FROM events WHERE user_id = ?", (user_id,)
        ).fetchall()
    finally:
        conn.close()
    return {row["event_type"] for row in rows}


def _row_counts(data_dir, user_id):
    conn = db.connect(data_dir)
    try:
        return {
            table: conn.execute(
                f"SELECT COUNT(*) AS n FROM {table} WHERE {column} = ?", (user_id,)
            ).fetchone()["n"]
            for table, column in (
                ("users", "id"),
                ("ynab_connections", "user_id"),
                ("conversions", "user_id"),
            )
        }
    finally:
        conn.close()


# --- store layer -----------------------------------------------------------


def test_delete_removes_user_connection_and_conversions(app_client):
    data_dir, user, _ = _seed_account(app_client)
    assert _row_counts(data_dir, user.id) == INTACT

    assert UserStore(data_dir).delete(user.id) is True

    assert _row_counts(data_dir, user.id) == GONE
    assert UserStore(data_dir).get_by_email(EMAIL) is None


def test_delete_is_scoped_to_one_user(app_client, app_client_factory):
    data_dir, user, _ = _seed_account(app_client)
    with app_client_factory() as other:
        _, other_user, _ = _seed_account(other, email=OTHER_EMAIL, account_id="acct-2")

        UserStore(data_dir).delete(user.id)

        assert _row_counts(data_dir, other_user.id) == INTACT
        # ...and the survivor is still logged in and working.
        assert other.get("/conversions").status_code == 200


def test_delete_unknown_user_returns_false(app_client):
    assert UserStore(get_settings().data_dir).delete("no-such-id") is False


def test_delete_rolls_back_if_a_statement_fails(app_client, monkeypatch):
    """The three DELETEs share one transaction: a failure part-way through must
    leave everything, so a half-done deletion can't strand a YNAB token."""
    data_dir, user, _ = _seed_account(app_client)
    real_connect = db.connect

    class _FailsOnUsers:
        """Proxies a real connection but blows up on the final DELETE. `with`
        looks dunders up on the type, so __enter__/__exit__ are explicit."""

        def __init__(self, conn):
            self._conn = conn

        def __getattr__(self, name):
            return getattr(self._conn, name)

        def __enter__(self):
            return self._conn.__enter__()

        def __exit__(self, *exc_info):
            return self._conn.__exit__(*exc_info)

        def execute(self, sql, *args):
            if sql.startswith("DELETE FROM users"):
                raise sqlite3.OperationalError("boom")
            return self._conn.execute(sql, *args)

    monkeypatch.setattr(db, "connect", lambda data_dir: _FailsOnUsers(real_connect(data_dir)))
    with pytest.raises(sqlite3.OperationalError):
        UserStore(data_dir).delete(user.id)
    monkeypatch.undo()

    assert _row_counts(data_dir, user.id) == INTACT


def test_delete_removes_the_users_activity_log(app_client):
    """`events` goes with the account: nothing can read a deleted user's rows
    (aggregate_by_user selects FROM users) and `detail` holds real YNAB account
    ids, so keeping them would be privacy surface for no reader."""
    data_dir, user, _ = _seed_account(app_client)
    events.record_event(data_dir, user.id, events.APPLY, count=3)
    events.record_event(data_dir, user.id, events.CONVERSION_CREATED, detail="acct-1")

    UserStore(data_dir).delete(user.id)

    assert _event_types(data_dir, user.id) == set()


def test_deleted_account_leaves_no_ynab_identifiers_on_disk(app_client):
    """The end-to-end promise: nothing recoverable, not even in free pages."""
    data_dir, user, token = _seed_account(app_client, account_id="acct-sentinel")
    events.record_event(data_dir, user.id, events.CONVERSION_CREATED, detail="acct-sentinel")

    _delete_account(app_client, token)

    # Read the write-ahead log too: a checkpointed page can still sit there.
    raw = b"".join(
        path.read_bytes()
        for path in (db.db_path(data_dir), db.db_path(data_dir).with_name("app.db-wal"))
        if path.exists()
    )
    for sentinel in (EMAIL.encode(), b"acct-sentinel", b"test-token"):
        assert sentinel not in raw, f"{sentinel!r} survived the delete"


# --- self-serve HTTP flow --------------------------------------------------


def test_delete_account_requires_the_password(app_client):
    data_dir, user, token = _seed_account(app_client)

    response = _delete_account(app_client, token, password="not-the-password")

    assert response.status_code == 403
    assert WRONG_PASSWORD_ERROR.split(" — ")[0] in response.text
    # Nothing deleted at all, still logged in.
    assert _row_counts(data_dir, user.id) == INTACT
    assert app_client.get("/settings").status_code == 200


@pytest.mark.parametrize("extra", [{}, {"password": ""}])
def test_delete_account_rejects_an_empty_or_missing_password(app_client, extra):
    """`required` lives only in the HTML; a hand-rolled POST can omit the field
    entirely, and Form(default="") must not fall through to a delete."""
    data_dir, user, token = _seed_account(app_client)

    response = app_client.post(
        "/settings/delete-account",
        data={"csrf_token": token, **extra},
        follow_redirects=False,
    )

    assert response.status_code == 403
    assert _row_counts(data_dir, user.id) == INTACT


def test_delete_account_attempts_are_throttled(app_client):
    """Otherwise this form is an unthrottled oracle for guessing the password
    of a session left open in someone else's browser."""
    from app.auth import LOCKOUT_THRESHOLD

    data_dir, user, token = _seed_account(app_client)

    for _ in range(LOCKOUT_THRESHOLD):
        assert _delete_account(app_client, token, password="wrong").status_code == 403

    response = _delete_account(app_client, token, password="wrong")
    assert response.status_code == 429
    assert "Too many failed attempts" in response.text
    # The lockout holds even for the *correct* password, and nothing is deleted.
    assert _delete_account(app_client, token).status_code == 429
    assert _row_counts(data_dir, user.id) == INTACT


def test_delete_account_failures_count_against_login(app_client):
    """One counter per email across every route that checks a password —
    a second, separate allowance would just be a way around the first."""
    from app.auth import LOCKOUT_THRESHOLD

    _, _, token = _seed_account(app_client)
    for _ in range(LOCKOUT_THRESHOLD):
        _delete_account(app_client, token, password="wrong")

    response = app_client.post(
        "/login",
        data={"email": EMAIL, "password": PASSWORD, "csrf_token": get_csrf(app_client)},
        follow_redirects=False,
    )
    assert response.status_code == 429


def test_delete_account_requires_csrf(app_client):
    data_dir, user, _ = _seed_account(app_client)

    response = app_client.post(
        "/settings/delete-account", data={"password": PASSWORD}, follow_redirects=False
    )

    assert response.status_code == 403
    assert _row_counts(data_dir, user.id) == INTACT


def test_delete_account_requires_login(app_client):
    # A valid CSRF token from an anonymous session, so this exercises the login
    # gate rather than tripping the app-level CSRF check first.
    token = get_csrf(app_client)
    response = app_client.post(
        "/settings/delete-account",
        data={"password": PASSWORD, "csrf_token": token},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_delete_account_deletes_everything_and_logs_out(app_client):
    data_dir, user, token = _seed_account(app_client)

    response = _delete_account(app_client, token)

    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert _row_counts(data_dir, user.id) == GONE
    # Session cleared: the landing page renders (no redirect to /conversions)
    # and shows the confirmation once.
    landing = app_client.get("/")
    assert landing.status_code == 200
    assert "account has been deleted" in landing.text
    # One-shot: a reload doesn't repeat it, and no URL can conjure it.
    assert "account has been deleted" not in app_client.get("/").text
    assert app_client.get("/conversions", follow_redirects=False).status_code == 303


def test_deleted_account_cannot_log_back_in(app_client):
    _, _, token = _seed_account(app_client)
    _delete_account(app_client, token)

    token = get_csrf(app_client)
    response = app_client.post(
        "/login",
        data={"email": EMAIL, "password": PASSWORD, "csrf_token": token},
        follow_redirects=False,
    )
    assert response.status_code == 401
    assert "Incorrect email or password" in response.text


def test_delete_account_records_an_event(app_client):
    data_dir, user, token = _seed_account(app_client)

    _delete_account(app_client, token)

    conn = db.connect(data_dir)
    try:
        row = conn.execute(
            "SELECT detail FROM events WHERE user_id = ? AND event_type = ?",
            (user.id, events.ACCOUNT_DELETED),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None and row["detail"] == "self"
    # ...and it is the ONLY row left for that id.
    assert _event_types(data_dir, user.id) == {events.ACCOUNT_DELETED}


def test_settings_page_offers_deletion(app_client):
    signup(app_client)
    body = app_client.get("/settings").text
    assert "/settings/delete-account" in body
    assert "Delete my account" in body


def test_signup_works_again_with_the_same_email(app_client):
    """The unique-email index must not keep a deleted address reserved."""
    _, _, token = _seed_account(app_client)
    _delete_account(app_client, token)

    token = get_csrf(app_client)
    response = app_client.post(
        "/signup",
        data={
            "email": EMAIL,
            "password": PASSWORD,
            "password_confirm": PASSWORD,
            "csrf_token": token,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303


# --- CLI -------------------------------------------------------------------


def test_cli_deletes_by_email(app_client):
    from app.delete_user import delete_user

    data_dir, user, _ = _seed_account(app_client)

    message = delete_user(EMAIL.upper())  # emails are normalized

    assert EMAIL in message
    assert _row_counts(data_dir, user.id) == GONE
    assert ConnectionStore(data_dir).get(user.id) is None


def test_cli_records_an_admin_deletion_event(app_client):
    from app.delete_user import delete_user

    data_dir, user, _ = _seed_account(app_client)

    delete_user(EMAIL)

    conn = db.connect(data_dir)
    try:
        row = conn.execute(
            "SELECT detail FROM events WHERE user_id = ? AND event_type = ?",
            (user.id, events.ACCOUNT_DELETED),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None and row["detail"] == "admin"


def test_cli_fails_loudly_on_unknown_email(app_client):
    from app.delete_user import delete_user

    signup(app_client)
    with pytest.raises(SystemExit, match="No user with email"):
        delete_user("nobody@example.com")


# The confirmation prompt is the only thing standing between a typo and an
# irreversible delete, so each of its branches gets a test.


class _Tty(io.StringIO):
    def isatty(self):
        return True


def test_confirm_skips_the_prompt_with_yes(monkeypatch):
    from app import delete_user as cli

    monkeypatch.setattr("builtins.input", lambda *a: pytest.fail("prompted despite --yes"))
    assert cli._confirm("them@example.com", assume_yes=True) is None


def test_confirm_requires_a_terminal_without_yes(monkeypatch):
    from app import delete_user as cli

    monkeypatch.setattr(sys, "stdin", io.StringIO(""))  # isatty() is False
    with pytest.raises(SystemExit, match="Not a terminal"):
        cli._confirm("them@example.com", assume_yes=False)


@pytest.mark.parametrize("answer", ["n", "", "nope", "Y E S"])
def test_confirm_aborts_on_anything_but_yes(monkeypatch, answer):
    from app import delete_user as cli

    monkeypatch.setattr(sys, "stdin", _Tty(""))
    monkeypatch.setattr("builtins.input", lambda *a: answer)
    with pytest.raises(SystemExit, match="Aborted"):
        cli._confirm("them@example.com", assume_yes=False)


@pytest.mark.parametrize("answer", ["y", "YES", " yes "])
def test_confirm_accepts_yes(monkeypatch, answer):
    from app import delete_user as cli

    monkeypatch.setattr(sys, "stdin", _Tty(""))
    monkeypatch.setattr("builtins.input", lambda *a: answer)
    assert cli._confirm("them@example.com", assume_yes=False) is None
