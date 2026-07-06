"""YNAB OAuth: the connect flow routes and access-token refresh logic."""
import re
import threading
import time
from types import SimpleNamespace

import pytest
import respx
from httpx import Response

from app import db, oauth
from app.connections import ConnectionStore
from app.oauth import get_access_token
from app.users import UserStore
from app.ynab import YNABError
from tests.test_app_flow import signup

OAUTH = "https://app.ynab.com"

SETTINGS = SimpleNamespace(
    ynab_client_id="cid", ynab_client_secret="csec", ynab_oauth_base=OAUTH
)


@pytest.fixture
def oauth_client(app_client_factory):
    with app_client_factory(YNAB_CLIENT_ID="cid", YNAB_CLIENT_SECRET="csec") as client:
        yield client


def start_and_get_state(client):
    response = client.get("/oauth/ynab/start", follow_redirects=False)
    assert response.status_code == 303
    location = response.headers["location"]
    assert location.startswith(f"{OAUTH}/oauth/authorize?")
    return re.search(r"state=([^&]+)", location).group(1)


def test_oauth_start_redirects_to_ynab(oauth_client):
    signup(oauth_client)
    page = oauth_client.get("/settings")
    assert "/oauth/ynab/start" in page.text  # the connect button is offered

    response = oauth_client.get("/oauth/ynab/start", follow_redirects=False)
    assert response.status_code == 303
    location = response.headers["location"]
    assert "client_id=cid" in location
    assert "response_type=code" in location
    assert "redirect_uri=http%3A%2F%2Ftestserver%2Foauth%2Fynab%2Fcallback" in location
    assert "state=" in location


def test_oauth_start_404_when_not_configured(app_client):
    signup(app_client)
    assert app_client.get("/oauth/ynab/start", follow_redirects=False).status_code == 404
    assert app_client.get("/oauth/ynab/callback?code=x&state=y").status_code == 404


@respx.mock
def test_oauth_callback_exchanges_code_and_connects(oauth_client, tmp_path):
    token_route = respx.post(f"{OAUTH}/oauth/token").mock(
        return_value=Response(200, json={
            "access_token": "oauth-access", "refresh_token": "oauth-refresh",
            "expires_in": 7200, "token_type": "Bearer",
        })
    )
    signup(oauth_client)
    state = start_and_get_state(oauth_client)
    response = oauth_client.get(
        f"/oauth/ynab/callback?code=the-code&state={state}", follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/settings?ok=connected"

    body = token_route.calls[0].request.content.decode()
    assert "grant_type=authorization_code" in body
    assert "code=the-code" in body

    page = oauth_client.get("/settings?ok=connected")
    assert "YNAB connected." in page.text
    assert "via OAuth" in page.text

    user = UserStore(tmp_path).get_by_email("user@example.com")
    connection = ConnectionStore(tmp_path).get(user.id)
    assert connection.kind == "oauth"
    assert connection.access_token == "oauth-access"
    assert connection.refresh_token == "oauth-refresh"
    assert connection.expires_at > time.time()


def test_oauth_callback_state_mismatch_rejected(oauth_client):
    signup(oauth_client)
    start_and_get_state(oauth_client)
    response = oauth_client.get("/oauth/ynab/callback?code=x&state=forged")
    assert response.status_code == 403
    # without even starting, the callback is rejected too
    response = oauth_client.get("/oauth/ynab/callback?code=x&state=")
    assert response.status_code == 403


def test_oauth_callback_denied_is_flash_not_error(oauth_client):
    signup(oauth_client)
    state = start_and_get_state(oauth_client)
    response = oauth_client.get(
        f"/oauth/ynab/callback?error=access_denied&state={state}", follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/settings?error=denied"
    assert "cancelled or denied" in oauth_client.get("/settings?error=denied").text


# --- get_access_token unit tests -------------------------------------------


def make_user_and_store(tmp_path):
    db.init(tmp_path)
    user = UserStore(tmp_path).create("unit@example.com", "password123")
    return user, ConnectionStore(tmp_path)


def test_no_connection_returns_none(tmp_path):
    user, store = make_user_and_store(tmp_path)
    assert get_access_token(SETTINGS, store, user.id) is None


def test_legacy_pat_connection_is_deleted_not_returned(tmp_path):
    """A pre-OAuth-only row (no refresh_token) must be cleaned up, not treated
    as a usable, non-expiring credential — OAuth is the only connection kind
    the app creates now, so anything without a refresh token is stale."""
    user, store = make_user_and_store(tmp_path)
    store._upsert(user.id, "pat", "legacy-token", None, None)
    assert get_access_token(SETTINGS, store, user.id) is None
    assert store.get(user.id) is None


@respx.mock
def test_fresh_oauth_token_needs_no_refresh(tmp_path):
    # respx active with no routes: any HTTP call would fail the test
    user, store = make_user_and_store(tmp_path)
    store.set_oauth(user.id, "fresh", "refresh", time.time() + 3600)
    assert get_access_token(SETTINGS, store, user.id) == "fresh"


@respx.mock
def test_expired_oauth_token_is_refreshed_and_saved(tmp_path):
    respx.post(f"{OAUTH}/oauth/token").mock(
        return_value=Response(200, json={
            "access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 7200,
        })
    )
    user, store = make_user_and_store(tmp_path)
    store.set_oauth(user.id, "stale", "old-refresh", time.time() - 10)
    assert get_access_token(SETTINGS, store, user.id) == "new-access"
    connection = store.get(user.id)
    assert connection.access_token == "new-access"
    assert connection.refresh_token == "new-refresh"
    assert connection.expires_at > time.time()


@respx.mock
def test_revoked_grant_deletes_connection(tmp_path):
    respx.post(f"{OAUTH}/oauth/token").mock(
        return_value=Response(401, json={"error": "invalid_grant"})
    )
    user, store = make_user_and_store(tmp_path)
    store.set_oauth(user.id, "stale", "revoked-refresh", time.time() - 10)
    assert get_access_token(SETTINGS, store, user.id) is None
    assert store.get(user.id) is None  # UI returns to the "connect" state


@respx.mock
def test_transient_refresh_failure_keeps_connection(tmp_path):
    respx.post(f"{OAUTH}/oauth/token").mock(return_value=Response(503, text="down"))
    user, store = make_user_and_store(tmp_path)
    store.set_oauth(user.id, "stale", "refresh", time.time() - 10)
    with pytest.raises(YNABError):
        get_access_token(SETTINGS, store, user.id)
    assert store.get(user.id) is not None  # not deleted — YNAB was just down


@respx.mock
def test_concurrent_refresh_is_serialized_and_calls_ynab_once(tmp_path):
    # Several requests racing to refresh the same near-expiry token must not
    # all hit YNAB with the same (about to be rotated) refresh_token — the
    # per-user lock means only the first one through actually calls out.
    call_count = {"n": 0}

    def slow_response(request):
        call_count["n"] += 1
        time.sleep(0.05)  # hold the lock briefly so other threads pile up
        return Response(200, json={
            "access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 7200,
        })

    respx.post(f"{OAUTH}/oauth/token").mock(side_effect=slow_response)
    user, store = make_user_and_store(tmp_path)
    store.set_oauth(user.id, "stale", "shared-refresh", time.time() - 10)

    results: list[str | None] = []

    def worker() -> None:
        results.append(get_access_token(SETTINGS, store, user.id))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert call_count["n"] == 1
    assert results == ["new-access"] * 8


def test_refresh_rejected_but_already_rotated_recovers_instead_of_deleting(tmp_path, monkeypatch):
    # Simulates a race the in-process lock can't prevent (e.g. a second app
    # worker process): between our re-read and our refresh call failing,
    # something else already rotated the refresh token. The rejected 4xx for
    # OUR stale token must not delete a connection that's actually still good.
    user, store = make_user_and_store(tmp_path)
    store.set_oauth(user.id, "stale", "old-refresh", time.time() - 10)

    def fake_refresh_tokens(settings, refresh_token):
        store.set_oauth(user.id, "winner-access", "winner-refresh", time.time() + 3600)
        raise oauth.OAuthGrantError("stale refresh token rejected")

    monkeypatch.setattr(oauth, "refresh_tokens", fake_refresh_tokens)

    assert get_access_token(SETTINGS, store, user.id) == "winner-access"
    connection = store.get(user.id)
    assert connection is not None
    assert connection.refresh_token == "winner-refresh"


@respx.mock
def test_malformed_refresh_payload_keeps_connection(tmp_path):
    # A 200 without the tokens must be transient (YNABError), not a dead grant:
    # the still-valid connection must survive, not be deleted.
    respx.post(f"{OAUTH}/oauth/token").mock(
        return_value=Response(200, json={"token_type": "Bearer"})
    )
    user, store = make_user_and_store(tmp_path)
    store.set_oauth(user.id, "stale", "refresh", time.time() - 10)
    with pytest.raises(YNABError):
        get_access_token(SETTINGS, store, user.id)
    assert store.get(user.id) is not None
