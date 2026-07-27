"""Account deletion: the self-serve /settings flow, the delete_user CLI, and
the guarantee that a deleted account leaves no per-user rows behind."""
import io
import sqlite3
import sys
from types import SimpleNamespace

import pytest
import respx
from httpx import Response

from app import db, events
from app.config import get_settings
from app.connections import ConnectionStore
from app.routes.settings import WRONG_PASSWORD_ERROR
from app.store import ConversionStore
from app.users import UserStore
from tests.test_app_flow import (
    EMAIL,
    PASSWORD,
    YNAB,
    connect_ynab,
    create_conversion,
    get_csrf,
    login,
    mock_budgets,
    signup,
)

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
    """Sign up, connect YNAB, store one conversion. -> (data_dir, user, csrf_token)."""
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

    # context(), not undo(): undo() unwinds the WHOLE MonkeyPatch instance,
    # including the DATA_DIR/SECRET_KEY env patches conftest set up, which
    # would point a later get_settings() at the developer's real data/app.db.
    with monkeypatch.context() as m:
        m.setattr(db, "connect", lambda data_dir: _FailsOnUsers(real_connect(data_dir)))
        with pytest.raises(sqlite3.OperationalError):
            UserStore(data_dir).delete(user.id)

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


def test_an_event_racing_the_delete_does_not_resurrect_the_account(app_client):
    """Events are recorded on their own connection AFTER the action commits, and
    `events` has no FK. Two tabs — one creating a conversion, one deleting the
    account — could land a row for a user who no longer exists, carrying a real
    YNAB account id in `detail`, which nothing ever removes. That falsifies the
    privacy policy's "the only thing kept is a dated note"."""
    data_dir, user, _ = _seed_account(app_client)

    UserStore(data_dir).delete(user.id)
    # The in-flight request finally gets around to recording its event.
    events.record_event(
        data_dir, user.id, events.CONVERSION_CREATED, detail="REAL-YNAB-ACCOUNT-ID"
    )

    assert _event_types(data_dir, user.id) == set()
    raw = db.db_path(data_dir).read_bytes()
    assert b"REAL-YNAB-ACCOUNT-ID" not in raw


def test_the_deletion_marker_itself_still_records(app_client):
    """...but account_deleted is the deliberate orphan and must still land, or
    the audit trail loses the one row it exists to keep."""
    data_dir, user, _ = _seed_account(app_client)

    UserStore(data_dir).delete(user.id)
    events.record_event(data_dir, user.id, events.ACCOUNT_DELETED, detail="self")

    assert _event_types(data_dir, user.id) == {events.ACCOUNT_DELETED}


def test_a_failed_login_event_still_records_without_a_user(app_client):
    """The other exception: user_id is None by design for anonymous events."""
    data_dir, _, _ = _seed_account(app_client)

    events.record_event(data_dir, None, events.LOGIN, detail="anonymous")

    conn = db.connect(data_dir)
    try:
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM events WHERE user_id IS NULL"
        ).fetchone()["n"]
    finally:
        conn.close()
    assert n == 1


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


def test_delete_does_not_vacuum_on_the_request_path(app_client, monkeypatch):
    """VACUUM rewrites the whole file under an exclusive lock. With open signup
    and one uvicorn worker, a signup/delete loop would monopolize SQLite's
    single writer, so compaction belongs in the CLI, not here."""
    data_dir, user, _ = _seed_account(app_client)
    statements = []
    real_connect = db.connect

    class _Recording:
        def __init__(self, conn):
            self._conn = conn

        def __getattr__(self, name):
            return getattr(self._conn, name)

        def __enter__(self):
            return self._conn.__enter__()

        def __exit__(self, *exc_info):
            return self._conn.__exit__(*exc_info)

        def execute(self, sql, *args):
            statements.append(sql)
            return self._conn.execute(sql, *args)

    with monkeypatch.context() as m:
        m.setattr(db, "connect", lambda data_dir: _Recording(real_connect(data_dir)))
        UserStore(data_dir).delete(user.id)

    assert not any("VACUUM" in s.upper() for s in statements)
    # ...but the WAL is still checkpointed, or the old rows stay on disk.
    assert any("wal_checkpoint" in s for s in statements)


def test_checkpoint_reports_a_busy_result_instead_of_claiming_success(tmp_path, caplog):
    """PRAGMA wal_checkpoint(TRUNCATE) returns (busy, ...) rather than raising
    when a reader blocks it. Treating that as success is how a deletion gets
    reported as permanent while the data is still in app.db-wal."""
    import logging

    db.init(tmp_path)
    writer = db.connect(tmp_path)
    reader = db.connect(tmp_path)
    try:
        writer.execute(
            "INSERT INTO users (id, email, password_hash) VALUES ('u1', 'a@b.c', 'h')"
        )
        writer.commit()
        # Hold an open snapshot so the checkpoint can't truncate.
        reader.execute("BEGIN")
        reader.execute("SELECT * FROM users").fetchall()
        writer.execute("DELETE FROM users WHERE id = 'u1'")
        writer.commit()

        with caplog.at_level(logging.ERROR, logger="ynabfx"):
            assert db.checkpoint_wal(writer) is False
        assert "still busy" in caplog.text
    finally:
        reader.close()
        writer.close()


class _FakeConn:
    """Minimal stand-in for the one call checkpoint_wal makes. sqlite3's real
    Connection.execute is read-only and can't be monkeypatched, and the states
    below (a raising checkpoint, a non-WAL DB, a reader that clears between
    attempts) can't be produced reliably against a real file."""

    def __init__(self, rows=None, raises=None):
        self.rows = list(rows or [])
        self.raises = raises
        self.calls = 0
        self.timeouts = []

    def execute(self, sql, *args, **kwargs):
        # checkpoint_wal reads and re-sets busy_timeout around the attempts so a
        # checkpoint can't inherit the 5s writer timeout; only the actual
        # wal_checkpoint calls consume a scripted row.
        if sql.strip() == "PRAGMA busy_timeout":
            return SimpleNamespace(fetchone=lambda: (5000,))
        if sql.strip().startswith("PRAGMA busy_timeout ="):
            self.timeouts.append(sql)
            return SimpleNamespace(fetchone=lambda: None)
        self.calls += 1
        if self.raises:
            raise self.raises
        row = self.rows.pop(0)
        return SimpleNamespace(fetchone=lambda: row)


def test_checkpoint_survives_a_sqlite_error(caplog):
    """checkpoint_wal must never raise: the rows are already deleted, so a
    checkpoint problem can't turn a completed deletion into a 500."""
    import logging

    with caplog.at_level(logging.ERROR, logger="ynabfx"):
        assert db.checkpoint_wal(_FakeConn(raises=sqlite3.OperationalError("x"))) is False
    assert "checkpoint failed outright" in caplog.text


def test_checkpoint_treats_a_missing_row_as_success():
    """Defensive branch only. A real non-WAL SQLite returns (0, -1, -1), not
    None — see the sibling test — so this guards a hypothetical driver, and
    must read as success rather than busy."""
    assert db.checkpoint_wal(_FakeConn(rows=[None])) is True


def test_checkpoint_succeeds_on_a_non_wal_database(tmp_path):
    """The real non-WAL shape, against an actual connection."""
    conn = sqlite3.connect(tmp_path / "plain.db")
    try:
        conn.execute("PRAGMA journal_mode = DELETE")
        assert db.checkpoint_wal(conn) is True
    finally:
        conn.close()


def test_checkpoint_retries_until_the_reader_clears(monkeypatch):
    """The common real case: busy once, then it goes through. The
    busy-forever test alone leaves the retry itself unexercised."""
    monkeypatch.setattr(db, "_CHECKPOINT_RETRY_SECONDS", 0)
    conn = _FakeConn(rows=[(1, 4, 0), (0, 0, 4)])  # busy, then done

    assert db.checkpoint_wal(conn) is True
    assert conn.calls == 2


def test_cli_reports_a_row_that_vanished_mid_delete(app_client, monkeypatch):
    """Concurrent self-serve deletion: the CLI must not print a success line
    for work that didn't happen."""
    from app.delete_user import delete_user

    _seed_account(app_client)
    monkeypatch.setattr(UserStore, "delete", lambda self, user_id: False)

    with pytest.raises(SystemExit, match="disappeared mid-delete"):
        delete_user(EMAIL)


@pytest.mark.parametrize("argv", [[], ["--yes"], ["a@b.com", "c@d.com"]])
def test_cli_rejects_a_bad_argument_list(argv):
    """0 or 2+ emails must fail loudly rather than delete something surprising."""
    from app.delete_user import main

    with pytest.raises(SystemExit, match="usage: python -m app.delete_user"):
        main(argv)


def test_cli_main_strips_the_yes_flag_and_deletes(app_client, monkeypatch):
    from app import delete_user as cli

    data_dir, user, _ = _seed_account(app_client)
    confirmed = []
    monkeypatch.setattr(
        cli, "_confirm", lambda email, assume_yes: confirmed.append((email, assume_yes))
    )

    message = cli.main(["--yes", EMAIL])

    # Not just "it deleted": that main() consults _confirm at all, and that
    # --yes actually reaches it. Dropping the flag would make
    # `docker compose exec -T ... --yes` die with "Not a terminal".
    assert confirmed == [(EMAIL, True)]
    assert EMAIL in message
    assert _row_counts(data_dir, user.id) == GONE


def test_cli_main_without_yes_still_confirms(app_client, monkeypatch):
    """The other half of the --yes test. Without this, hardcoding
    `assume_yes = True` — silently skipping confirmation on an irreversible
    delete — passes the whole suite."""
    from app.delete_user import main

    data_dir, user, _ = _seed_account(app_client)
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))  # isatty() is False

    with pytest.raises(SystemExit, match="Not a terminal"):
        main([EMAIL])

    assert _row_counts(data_dir, user.id) == INTACT  # nothing deleted


def test_cli_compacts_the_database(app_client, monkeypatch):
    """The CLI is the offline path, so it still VACUUMs — that's what reclaims
    pages freed before secure_delete was turned on."""
    from app.delete_user import delete_user

    _seed_account(app_client)
    vacuumed = []
    monkeypatch.setattr(db, "vacuum", lambda data_dir: vacuumed.append(data_dir))

    delete_user(EMAIL)

    assert vacuumed, "delete_user should compact the DB after deleting"


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


def test_a_stranger_cannot_lock_you_out_of_deleting(app_client, app_client_factory):
    """The re-auth throttle is keyed per user, not per email. Sharing /login's
    email counter would hand any anonymous visitor a way to block the owner
    from deleting their own account just by failing logins for that address."""
    from app.auth import LOCKOUT_THRESHOLD

    data_dir, user, token = _seed_account(app_client)

    # An anonymous attacker hammers /login with the victim's email...
    with app_client_factory() as attacker:
        for _ in range(LOCKOUT_THRESHOLD + 2):
            attacker.post(
                "/login",
                data={
                    "email": EMAIL,
                    "password": "guess",
                    "csrf_token": get_csrf(attacker),
                },
                follow_redirects=False,
            )
        assert attacker.post(
            "/login",
            data={"email": EMAIL, "password": PASSWORD, "csrf_token": get_csrf(attacker)},
            follow_redirects=False,
        ).status_code == 429  # the login lockout itself still works

    # ...and the real owner can still delete their account.
    response = _delete_account(app_client, token)
    assert response.status_code == 303
    assert _row_counts(data_dir, user.id) == GONE


def test_login_cannot_reach_the_reauth_counter(app_client, app_client_factory):
    """The two throttles live in separate dicts. When they shared one, keyed
    "reauth:<user_id>", an anonymous visitor could POST /login with that
    literal string as the email — /login never checks the value looks like an
    email — and lock the owner out of deleting their own account."""
    from app.auth import LOCKOUT_THRESHOLD

    data_dir, user, token = _seed_account(app_client)

    with app_client_factory() as attacker:
        for _ in range(LOCKOUT_THRESHOLD + 2):
            attacker.post(
                "/login",
                data={
                    "email": f"reauth:{user.id}",
                    "password": "guess",
                    "csrf_token": get_csrf(attacker),
                },
                follow_redirects=False,
            )

    assert _delete_account(app_client, token).status_code == 303
    assert _row_counts(data_dir, user.id) == GONE


def test_a_login_flood_cannot_reset_a_counter_below_the_threshold(app_client, monkeypatch):
    """The subtler half of the same bug. A counter that hasn't locked yet has
    locked_until == 0.0, which any "sweep what has expired" test reads as
    expired — so a flood wipes victims *while they are still accumulating* and
    they never reach a lockout at all."""
    import app.auth as auth

    monkeypatch.setattr(auth, "_MAX_TRACKED_KEYS", 10)
    for _ in range(auth.LOCKOUT_THRESHOLD - 1):  # 4 of 5: not locked yet
        auth._record_failure(auth._login_throttle, EMAIL)
    assert not auth._is_locked(auth._login_throttle, EMAIL)

    for i in range(40):
        for _ in range(20):
            auth._record_failure(auth._login_throttle, f"junk{i}@example.com")

    # The counter survived, so one more failure still trips the lockout.
    auth._record_failure(auth._login_throttle, EMAIL)
    assert auth._is_locked(auth._login_throttle, EMAIL), "flood reset the counter"


def test_login_rejects_an_oversized_email_before_it_becomes_a_throttle_key(app_client):
    """The throttle key is the submitted email, so an unbounded one makes the
    entry size attacker-controlled (measured: 1 MiB keys, 500 entries, 510 MB)."""
    import app.auth as auth

    huge = "a" * 5000 + "@example.com"
    response = app_client.post(
        "/login",
        data={"email": huge, "password": "x", "csrf_token": get_csrf(app_client)},
        follow_redirects=False,
    )

    assert response.status_code == 401
    assert huge not in auth._login_throttle
    assert all(len(k) <= auth.MAX_EMAIL_LENGTH for k in auth._login_throttle)


def test_a_login_flood_cannot_clear_a_lockout(app_client, monkeypatch):
    """A live lockout must survive the key-cap sweep. An eviction policy that
    drops the entry closest to expiring looks like a memory fix but hands an
    attacker a lockout bypass: flooding /login with hard-hammered junk keys
    (300s lockouts) pushes out a victim locked at the threshold (2s), resetting
    the backoff that stops password guessing."""
    import app.auth as auth

    monkeypatch.setattr(auth, "_MAX_TRACKED_KEYS", 10)
    for _ in range(auth.LOCKOUT_THRESHOLD):
        auth._record_failure(auth._login_throttle, EMAIL)
    assert auth._is_locked(auth._login_throttle, EMAIL)

    # Flood with keys whose lockouts are far longer than the victim's.
    for i in range(40):
        for _ in range(20):
            auth._record_failure(auth._login_throttle, f"junk{i}@example.com")

    assert auth._is_locked(auth._login_throttle, EMAIL), "flood cleared the lockout"


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


# --- races between a deletion and work already in flight --------------------


def test_apply_aborts_instead_of_patching_ynab_after_a_delete(app_client):
    """An apply that was queued behind its lock when the account was deleted
    must stop, not fall back to its pre-lock snapshot. Falling back would PATCH
    the user's real YNAB budget *after* they were told everything was gone.

    `ynab=None` is the assertion: the abort has to happen before any YNAB call,
    so a regression can't quietly succeed here."""
    import asyncio

    from app.routes.conversions import ConversionGoneError, _apply_updates

    data_dir, user, _ = _seed_account(app_client)
    conversion = ConversionStore(data_dir).add(user.id, _conv("acct-race"))

    UserStore(data_dir).delete(user.id)  # ...the account goes away mid-apply

    with pytest.raises(ConversionGoneError):
        asyncio.run(
            _apply_updates(
                user.id,
                None,
                conversion,
                [{"id": "t1", "amount": -1000, "memo": "x"}],
                {"t1": {"original": -1817000, "action": "convert"}},
            )
        )


def _vanish_after_first_read(monkeypatch, real_get, only_id=None):
    """Really delete the user's conversion rows straight after the route's
    first successful lookup — the shape of an account deletion landing while an
    apply is queued for its lock.

    A counter that just returns None on later calls would be simpler but is
    tied to how many times the route happens to read the store, so it can go
    vacuous the moment that changes. Deleting for real means every later read
    is genuinely empty, whatever the call order."""

    def flaky_get(self, user_id, conversion_id):
        row = real_get(self, user_id, conversion_id)
        if row is not None and only_id in (None, conversion_id):
            conn = db.connect(self.data_dir)
            try:
                conn.execute(
                    "DELETE FROM conversions WHERE user_id = ? AND id = ?",
                    (user_id, conversion_id),
                )
                conn.commit()
            finally:
                conn.close()
        return row

    monkeypatch.setattr(ConversionStore, "get", flaky_get)


@respx.mock
def test_apply_route_redirects_when_the_conversion_vanishes(app_client, monkeypatch):
    """Regression: apply() used to fall back to the pre-lock snapshot and PATCH
    anyway. It now raises ConversionGoneError — this pins the HTTP outcome the
    user actually sees, and that nothing was sent to YNAB."""
    mock_budgets()
    patched = respx.patch(f"{YNAB}/budgets/b1/transactions").mock(
        return_value=Response(200, json={"data": {"transactions": []}})
    )
    # A real unconverted transaction: with an empty list `safe` is empty and no
    # PATCH can fire, so `not patched.called` would hold even with the fix
    # reverted and prove nothing.
    respx.get(f"{YNAB}/budgets/b1/accounts/a1/transactions").mock(
        return_value=Response(200, json={"data": {"transactions": [
            {"id": "t1", "date": "2024-01-05", "amount": -1817000,
             "payee_name": "Ramen", "memo": None, "deleted": False},
        ]}})
    )
    token = login(app_client)
    conversion_id = create_conversion(app_client, token, "a1", "Japan Trip")
    _vanish_after_first_read(monkeypatch, ConversionStore.get)

    response = app_client.post(
        f"/conversions/{conversion_id}/apply",
        data={
            "selected": ["t1"], "action_t1": "convert", "original_t1": "-1817000",
            "amount_t1": "-15990", "memo_t1": "x", "csrf_token": token,
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/conversions"
    assert not patched.called, "nothing may be written to YNAB after the row is gone"


@respx.mock
def test_apply_all_skips_a_vanished_group_and_finishes_the_run(app_client, monkeypatch):
    """Regression, apply-all half: the group whose row disappeared is skipped
    like one deleted before the run, and the rest of the batch still lands."""
    mock_budgets()
    patched = respx.patch(f"{YNAB}/budgets/b1/transactions").mock(
        return_value=Response(200, json={"data": {"transactions": []}})
    )
    # A real unconverted transaction, so the reverted "fall back to the stale
    # snapshot" behavior would actually reach the PATCH — otherwise `safe` is
    # empty and `not patched.called` would hold no matter what.
    for account in ("a1", "a2"):
        respx.get(f"{YNAB}/budgets/b1/accounts/{account}/transactions").mock(
            return_value=Response(200, json={"data": {"transactions": [
                {"id": "t1", "date": "2024-01-05", "amount": -1817000,
                 "payee_name": "Ramen", "memo": None, "deleted": False},
            ]}})
        )
    token = login(app_client)
    gone = create_conversion(app_client, token, "a1", "Japan Trip")
    survivor = create_conversion(app_client, token, "a2", "Europe Trip")
    # Scoped to the one id, so the sibling group is genuinely still there and
    # "the rest of the batch still lands" is a real assertion.
    _vanish_after_first_read(monkeypatch, ConversionStore.get, only_id=gone)

    # The vanishing group must carry a real selection: an empty one short-
    # circuits _apply_updates before it ever reaches the lock, which would
    # make this test pass without exercising the fix at all.
    response = app_client.post(
        "/conversions/apply-all",
        data={
            "conversion_ids": [gone, survivor],
            f"selected_{gone}": ["t1"], f"selected_{survivor}": ["t1"],
            "action_t1": "convert", "original_t1": "-1817000",
            "amount_t1": "-15990", "memo_t1": "x", "csrf_token": token,
        },
        follow_redirects=False,
    )

    # The run completes rather than 500ing on the vanished group, and exactly
    # one PATCH goes out: the survivor's. Two would mean the deleted group was
    # written anyway; zero would mean one bad group killed the whole batch.
    assert response.status_code == 303
    assert patched.call_count == 1
    # ...and it was skipped SILENTLY, not reported as a group failure. Status +
    # PATCH count alone can't tell those apart: raising any YNABError instead
    # would also yield one PATCH, but would render an error row for the
    # vanished account in the summary.
    summary = app_client.get("/conversions").text
    assert "Europe Trip" in summary
    assert "Japan Trip" not in summary


def test_token_refresh_aborts_when_the_account_was_deleted_mid_request(app_client):
    """The other half of the same race: a request refreshing its OAuth token
    when the account disappears must not carry on with the new token. The
    refresh already rotated at YNAB, but persisting it hits the users FK — so
    the request has to stop rather than keep operating on a deleted user's
    budget."""
    from app import oauth
    from app.ynab import YNABError

    data_dir, user, _ = _seed_account(app_client)
    store = ConnectionStore(data_dir)
    store.set_oauth(user.id, "stale-access", "old-refresh", 0)  # already expired

    settings = SimpleNamespace(
        ynab_client_id="cid", ynab_client_secret="sec", ynab_oauth_base="https://x"
    )

    def refresh_then_delete(_settings, _refresh_token):
        # The account is deleted in the window between YNAB issuing the new
        # token and us storing it.
        UserStore(data_dir).delete(user.id)
        return {"access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 7200}

    original = oauth.refresh_tokens
    oauth.refresh_tokens = refresh_then_delete
    try:
        with pytest.raises(YNABError) as exc_info:
            oauth.get_access_token(settings, store, user.id)
    finally:
        oauth.refresh_tokens = original

    # 401 routes to the existing reconnect path, not a 500 error page.
    assert exc_info.value.status_code == 401
    assert store.get(user.id) is None  # the token was NOT persisted


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
