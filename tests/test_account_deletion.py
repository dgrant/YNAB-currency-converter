"""Account deletion: the self-serve /settings flow, the delete_user CLI, and
the guarantee that a deleted account leaves no per-user rows behind."""
import pytest

from app import db, events
from app.config import get_settings
from app.connections import ConnectionStore
from app.store import ConversionStore
from app.users import UserStore
from tests.test_app_flow import EMAIL, PASSWORD, connect_ynab, get_csrf, signup

OTHER_EMAIL = "other@example.com"


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
    assert _row_counts(data_dir, user.id) == {
        "users": 1,
        "ynab_connections": 1,
        "conversions": 1,
    }

    assert UserStore(data_dir).delete(user.id) is True

    assert _row_counts(data_dir, user.id) == {
        "users": 0,
        "ynab_connections": 0,
        "conversions": 0,
    }
    assert UserStore(data_dir).get_by_email(EMAIL) is None


def test_delete_is_scoped_to_one_user(app_client, app_client_factory):
    data_dir, user, _ = _seed_account(app_client)
    with app_client_factory() as other:
        _, other_user, _ = _seed_account(other, email=OTHER_EMAIL, account_id="acct-2")

        UserStore(data_dir).delete(user.id)

        assert _row_counts(data_dir, other_user.id) == {
            "users": 1,
            "ynab_connections": 1,
            "conversions": 1,
        }
        # ...and the survivor is still logged in and working.
        assert other.get("/conversions").status_code == 200


def test_delete_unknown_user_returns_false(app_client):
    assert UserStore(get_settings().data_dir).delete("no-such-id") is False


def test_delete_leaves_the_events_audit_trail(app_client):
    """events rows deliberately have no FK to users (db.SCHEMA) — they must
    survive so the activity log isn't rewritten by a deletion."""
    data_dir, user, _ = _seed_account(app_client)
    events.record_event(data_dir, user.id, events.APPLY, count=3)

    UserStore(data_dir).delete(user.id)

    conn = db.connect(data_dir)
    try:
        rows = conn.execute(
            "SELECT event_type FROM events WHERE user_id = ?", (user.id,)
        ).fetchall()
    finally:
        conn.close()
    assert {row["event_type"] for row in rows} >= {events.SIGNUP, events.APPLY}


# --- self-serve HTTP flow --------------------------------------------------


def test_delete_account_requires_the_password(app_client):
    data_dir, user, token = _seed_account(app_client)

    response = app_client.post(
        "/settings/delete-account",
        data={"password": "not-the-password", "csrf_token": token},
        follow_redirects=False,
    )

    assert response.status_code == 403
    assert "password is incorrect" in response.text
    # Nothing deleted, still logged in.
    assert _row_counts(data_dir, user.id)["users"] == 1
    assert app_client.get("/settings").status_code == 200


def test_delete_account_requires_csrf(app_client):
    data_dir, user, _ = _seed_account(app_client)

    response = app_client.post(
        "/settings/delete-account", data={"password": PASSWORD}, follow_redirects=False
    )

    assert response.status_code == 403
    assert _row_counts(data_dir, user.id)["users"] == 1


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

    response = app_client.post(
        "/settings/delete-account",
        data={"password": PASSWORD, "csrf_token": token},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/?deleted=1"
    assert _row_counts(data_dir, user.id) == {
        "users": 0,
        "ynab_connections": 0,
        "conversions": 0,
    }
    # Session cleared: the landing page renders (no redirect to /conversions)
    # and shows the confirmation.
    landing = app_client.get("/?deleted=1")
    assert landing.status_code == 200
    assert "account has been deleted" in landing.text
    assert app_client.get("/conversions", follow_redirects=False).status_code == 303


def test_deleted_account_cannot_log_back_in(app_client):
    _, _, token = _seed_account(app_client)
    app_client.post(
        "/settings/delete-account",
        data={"password": PASSWORD, "csrf_token": token},
        follow_redirects=False,
    )

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

    app_client.post(
        "/settings/delete-account",
        data={"password": PASSWORD, "csrf_token": token},
        follow_redirects=False,
    )

    conn = db.connect(data_dir)
    try:
        row = conn.execute(
            "SELECT detail FROM events WHERE user_id = ? AND event_type = ?",
            (user.id, events.ACCOUNT_DELETED),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None and row["detail"] == "self"


def test_settings_page_offers_deletion(app_client):
    signup(app_client)
    body = app_client.get("/settings").text
    assert "/settings/delete-account" in body
    assert "Delete my account" in body


def test_signup_works_again_with_the_same_email(app_client):
    """The unique-email index must not keep a deleted address reserved."""
    _, _, token = _seed_account(app_client)
    app_client.post(
        "/settings/delete-account",
        data={"password": PASSWORD, "csrf_token": token},
        follow_redirects=False,
    )

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
    assert _row_counts(data_dir, user.id) == {
        "users": 0,
        "ynab_connections": 0,
        "conversions": 0,
    }
    assert ConnectionStore(data_dir).get(user.id) is None


def test_cli_fails_loudly_on_unknown_email(app_client):
    from app.delete_user import delete_user

    signup(app_client)
    with pytest.raises(SystemExit, match="No user with email"):
        delete_user("nobody@example.com")
