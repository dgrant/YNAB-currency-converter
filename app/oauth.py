"""YNAB OAuth (authorization-code flow) and per-user access-token resolution.

Available only when the deployment registers an OAuth application with YNAB
(YNAB_CLIENT_ID / YNAB_CLIENT_SECRET). OAuth is the only way users connect.
"""
import threading
import time
import urllib.parse

import httpx

from .config import Settings
from .connections import ConnectionStore, YNABConnection
from .ynab import YNABError

# Refresh this long before the token actually expires, so a token that is
# valid now can't expire mid-request.
REFRESH_MARGIN_SECONDS = 60

# Serializes refreshes per user (routes are sync, so requests interleave
# across FastAPI's threadpool). Keyed by user_id and never evicted — bounded
# by the real user count, not by attacker-controlled input like the login
# throttle is.
_refresh_locks: dict[str, threading.Lock] = {}
_refresh_locks_guard = threading.Lock()


def _refresh_lock(user_id: str) -> threading.Lock:
    with _refresh_locks_guard:
        return _refresh_locks.setdefault(user_id, threading.Lock())


def _is_fresh(connection: YNABConnection) -> bool:
    return (
        connection.expires_at is not None
        and time.time() < connection.expires_at - REFRESH_MARGIN_SECONDS
    )


def is_configured(settings: Settings) -> bool:
    return bool(settings.ynab_client_id and settings.ynab_client_secret)


def authorize_url(settings: Settings, redirect_uri: str, state: str) -> str:
    query = urllib.parse.urlencode(
        {
            "client_id": settings.ynab_client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "state": state,
        }
    )
    return f"{settings.ynab_oauth_base}/oauth/authorize?{query}"


def _token_request(settings: Settings, data: dict) -> dict:
    """POST to the token endpoint; returns the token payload.

    Raises YNABError for transport failures and 5xx (transient — the existing
    handler renders a friendly 502) and OAuthGrantError for 4xx (the grant is
    dead: denied, expired, or revoked).
    """
    try:
        response = httpx.post(f"{settings.ynab_oauth_base}/oauth/token", data=data, timeout=30)
    except httpx.TransportError as exc:
        raise YNABError(f"Could not reach YNAB: {exc}") from exc
    if response.is_success:
        payload = response.json()
        # A 200 that omits the tokens (unexpected from YNAB) is treated as a
        # transient error, not a dead grant — don't destroy a live connection
        # over a malformed refresh response; surface a friendly 502 instead.
        if not payload.get("access_token") or not payload.get("refresh_token"):
            raise YNABError("YNAB returned an unexpected OAuth token response")
        return payload
    if 400 <= response.status_code < 500:
        raise OAuthGrantError(f"YNAB rejected the authorization ({response.status_code})")
    raise YNABError(
        f"YNAB OAuth error {response.status_code}: {response.text[:200]}",
        status_code=response.status_code,
    )


class OAuthGrantError(Exception):
    """The OAuth grant is invalid (denied, expired, or revoked by the user)."""


def exchange_code(settings: Settings, code: str, redirect_uri: str) -> dict:
    return _token_request(
        settings,
        {
            "client_id": settings.ynab_client_id,
            "client_secret": settings.ynab_client_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
            "code": code,
        },
    )


def refresh_tokens(settings: Settings, refresh_token: str) -> dict:
    return _token_request(
        settings,
        {
            "client_id": settings.ynab_client_id,
            "client_secret": settings.ynab_client_secret,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        },
    )


def save_token_response(store: ConnectionStore, user_id: str, tokens: dict) -> None:
    store.set_oauth(
        user_id,
        access_token=tokens["access_token"],
        refresh_token=tokens["refresh_token"],
        expires_at=time.time() + float(tokens.get("expires_in", 7200)),
    )


def get_access_token(settings: Settings, store: ConnectionStore, user_id: str) -> str | None:
    """A currently-valid YNAB access token for the user, or None if not connected.

    OAuth tokens are refreshed when (nearly) expired. A refresh the token
    endpoint *rejects* means the user revoked access — the dead connection is
    deleted so the UI returns to the "connect" state. Transient failures
    bubble up as YNABError (friendly 502) without touching the stored tokens.

    Refreshing is serialized per user: YNAB rotates the refresh token on use,
    so two concurrent requests racing to refresh the same stale token would
    otherwise have the loser's refresh rejected and delete the connection the
    winner just replaced it with.
    """
    connection = store.get(user_id)
    if connection is None:
        return None
    if _is_fresh(connection):
        return connection.access_token

    with _refresh_lock(user_id):
        # Re-read: another thread may have refreshed while we waited for the lock.
        connection = store.get(user_id)
        if connection is None:
            return None
        if _is_fresh(connection):
            return connection.access_token
        if not connection.refresh_token:
            store.delete(user_id)
            return None
        stale_refresh_token = connection.refresh_token
        try:
            tokens = refresh_tokens(settings, stale_refresh_token)
        except OAuthGrantError:
            # A rejected refresh only means the grant is dead if OUR token was
            # still the current one. If it no longer matches, someone else
            # already refreshed (rotating it) and this connection is fine.
            current = store.get(user_id)
            if current is not None and current.refresh_token != stale_refresh_token:
                return current.access_token
            store.delete(user_id)
            return None
        save_token_response(store, user_id, tokens)
        return str(tokens["access_token"])
