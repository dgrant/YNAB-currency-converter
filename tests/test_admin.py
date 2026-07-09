"""Admin dashboard: access control, per-user aggregates, event recording,
and the set_admin CLI."""
import re
import sqlite3

import pytest

from app import db, events
from app.store import ConversionStore
from app.users import UserStore

CSRF_RE = re.compile(r'name="csrf_token" value="([^"]+)"')
EMAIL = "admin@example.com"
PASSWORD = "test-password"


def _get_csrf(client):
    return CSRF_RE.search(client.get("/login").text).group(1)


def _signup(client, email=EMAIL, password=PASSWORD):
    token = _get_csrf(client)
    resp = client.post(
        "/signup",
        data={
            "email": email,
            "password": password,
            "password_confirm": password,
            "csrf_token": token,
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303


def _make_admin(data_dir, email=EMAIL):
    assert UserStore(data_dir).set_admin_by_email(email, True) is True


def _conv(account_id, name="Acct"):
    return {
        "budget_id": "b1",
        "budget_name": "Plan",
        "account_id": account_id,
        "account_name": name,
        "from_currency": "JPY",
        "to_currency": "USD",
        "start_date": "2024-01-01",
    }


# --- HTTP access control ---------------------------------------------------


def test_admin_page_requires_login(app_client):
    resp = app_client.get("/admin", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


def test_admin_page_404_for_non_admin(app_client_factory, tmp_path):
    with app_client_factory() as client:
        _signup(client)  # a normal, non-admin user
        resp = client.get("/admin")
        assert resp.status_code == 404


def test_admin_page_200_for_admin(app_client_factory, tmp_path):
    from app.config import get_settings

    with app_client_factory() as client:
        _signup(client)
        _make_admin(get_settings().data_dir)
        resp = client.get("/admin")
        assert resp.status_code == 200
        assert EMAIL in resp.text


def test_admin_page_is_get_only(app_client_factory):
    from app.config import get_settings

    with app_client_factory() as client:
        _signup(client)
        _make_admin(get_settings().data_dir)
        # No POST route exists -> 405, so /admin adds no new POST/CSRF surface.
        resp = client.post("/admin")
        assert resp.status_code == 405


# --- Aggregates ------------------------------------------------------------


def test_aggregate_no_cartesian_fanout(tmp_path):
    """Two conversions + two apply events must not multiply: a naive
    LEFT JOIN + GROUP BY would report 4 conversions and 14 txns."""
    db.init(tmp_path)
    user = UserStore(tmp_path).create(EMAIL, PASSWORD)
    store = ConversionStore(tmp_path)
    store.add(user.id, _conv("a1"))
    store.add(user.id, _conv("a2"))
    events.record_event(tmp_path, user.id, events.APPLY, count=3)
    events.record_event(tmp_path, user.id, events.APPLY, count=4)

    row = {r["id"]: r for r in events.aggregate_by_user(tmp_path)}[user.id]
    assert row["conversions_count"] == 2
    assert row["transactions_converted"] == 7  # 3 + 4, not inflated by conversions


def test_aggregate_handles_users_with_only_one_side(tmp_path):
    db.init(tmp_path)
    store = ConversionStore(tmp_path)
    users = UserStore(tmp_path)
    # user A: conversions, no events
    a = users.create("a@example.com", PASSWORD)
    store.add(a.id, _conv("a1"))
    # user B: events, no conversions
    b = users.create("b@example.com", PASSWORD)
    events.record_event(tmp_path, b.id, events.APPLY, count=5)

    rows = {r["id"]: r for r in events.aggregate_by_user(tmp_path)}
    assert rows[a.id]["conversions_count"] == 1
    assert rows[a.id]["transactions_converted"] == 0  # COALESCE, not NULL
    assert rows[b.id]["conversions_count"] == 0
    assert rows[b.id]["transactions_converted"] == 5


def test_non_apply_events_do_not_count_as_transactions(tmp_path):
    """Only apply events carry a count; a delete must not inflate the metric."""
    db.init(tmp_path)
    user = UserStore(tmp_path).create(EMAIL, PASSWORD)
    events.record_event(tmp_path, user.id, events.CONVERSION_DELETED, detail="bulk:9")
    row = {r["id"]: r for r in events.aggregate_by_user(tmp_path)}[user.id]
    assert row["transactions_converted"] == 0


def test_last_activity_three_way_max_across_formats(tmp_path):
    """last_activity = MAX(event created_at, user created_at, conversion
    last_synced) across the two on-disk timestamp formats. A future-dated
    last_synced must win over a just-now event — which only holds because
    events.created_at is the space-separated datetime('now') format, not a
    'T'-separated Python isoformat (which would sort after everything)."""
    db.init(tmp_path)
    user = UserStore(tmp_path).create(EMAIL, PASSWORD)
    store = ConversionStore(tmp_path)
    conv = store.add(user.id, _conv("a1"))
    store.mark_synced(user.id, conv["id"], "2099-01-01")  # far future, date-only
    events.record_event(tmp_path, user.id, events.LOGIN)  # created_at = now

    row = {r["id"]: r for r in events.aggregate_by_user(tmp_path)}[user.id]
    assert row["last_activity"] == "2099-01-01"


def test_last_activity_falls_back_when_no_events(tmp_path):
    """A pre-existing user who never triggered an event is never blank."""
    db.init(tmp_path)
    user = UserStore(tmp_path).create(EMAIL, PASSWORD)
    row = {r["id"]: r for r in events.aggregate_by_user(tmp_path)}[user.id]
    assert row["last_activity"]  # falls back to users.created_at


# --- record_event isolation & privacy --------------------------------------


def test_record_event_swallows_db_error(tmp_path, monkeypatch):
    """A locked/broken DB must not turn a successful action into a 500."""
    db.init(tmp_path)

    def boom(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(events.db, "connect", boom)
    # Must not raise.
    events.record_event(tmp_path, "u1", events.APPLY, count=5)


def test_events_store_only_metadata(tmp_path):
    """The events row holds type + count + a small detail — never a token,
    amount, or memo."""
    db.init(tmp_path)
    user = UserStore(tmp_path).create(EMAIL, PASSWORD)
    events.record_event(tmp_path, user.id, events.APPLY, count=3, detail="conv123")
    conn = db.connect(tmp_path)
    try:
        row = conn.execute("SELECT * FROM events").fetchone()
    finally:
        conn.close()
    assert row["event_type"] == events.APPLY
    assert row["count"] == 3
    assert row["detail"] == "conv123"
    assert set(row.keys()) == {"id", "user_id", "event_type", "count", "detail", "created_at"}


# --- is_admin round-trip & CLI ---------------------------------------------


def test_is_admin_round_trips(tmp_path):
    db.init(tmp_path)
    store = UserStore(tmp_path)
    user = store.create(EMAIL, PASSWORD)
    assert store.get(user.id).is_admin is False
    assert store.set_admin_by_email(EMAIL, True) is True
    assert store.get(user.id).is_admin is True
    assert store.set_admin_by_email(EMAIL, False) is True
    assert store.get(user.id).is_admin is False


def test_set_admin_by_email_unknown_returns_false(tmp_path):
    db.init(tmp_path)
    assert UserStore(tmp_path).set_admin_by_email("nobody@example.com", True) is False


def test_set_admin_cli_hard_fails_on_unknown_email(tmp_path, monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "x")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import app.config as config

    monkeypatch.setattr(config, "_settings", None)
    from app.set_admin import set_admin

    with pytest.raises(SystemExit):
        set_admin("nobody@example.com", is_admin=True)


def test_set_admin_cli_succeeds_for_real_user(tmp_path, monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "x")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import app.config as config

    monkeypatch.setattr(config, "_settings", None)
    db.init(tmp_path)
    UserStore(tmp_path).create(EMAIL, PASSWORD)
    from app.set_admin import set_admin

    msg = set_admin(EMAIL, is_admin=True)
    assert "admin" in msg
    assert UserStore(tmp_path).get_by_email(EMAIL).is_admin is True
