"""Account settings: connect/disconnect the user's YNAB credentials."""
import secrets

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from .. import auth, events, oauth
from ..auth import get_user_store, require_login
from ..config import get_settings
from ..connections import ConnectionStore
from ..templates import templates
from ..users import User, verify_password

router = APIRouter(dependencies=[Depends(require_login)])

_FLASHES = {
    "connected": "YNAB connected.",
    "disconnected": "YNAB disconnected. You can revoke the grant any time from "
    "YNAB's security settings.",
    "refresh_on": "Pending counts will now refresh automatically when you open "
    "the conversions page.",
    "refresh_off": "Automatic refresh turned off — pending counts update when "
    "you preview.",
}
_ERRORS = {
    "denied": "YNAB authorization was cancelled or denied — nothing was connected.",
    "reauth": "Your YNAB connection predates OAuth-only support and had to be "
    "cleared — please reconnect.",
    "revoked": "YNAB rejected the connection as unauthorized (the access was "
    "likely revoked from YNAB's settings, or the token expired). Please reconnect.",
}

WRONG_PASSWORD_ERROR = (
    "That password is incorrect — your account was not deleted. Enter your "
    "current password to confirm."
)


def get_connection_store() -> ConnectionStore:
    return ConnectionStore(get_settings().data_dir)


def _redirect_uri(request: Request) -> str:
    base = get_settings().public_base_url
    if base:
        return f"{base}/oauth/ynab/callback"
    return str(request.url_for("oauth_callback"))


def _settings_response(
    request: Request, user: User, *, error: str | None = None, status_code: int = 200
):
    """Render /settings. `error` overrides the ?error= flash so a failed POST
    (e.g. the wrong password on delete-account) can re-render in place instead
    of redirecting the message through the URL."""
    connection = get_connection_store().get(user.id)
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "user": user,
            "connection": connection,
            "oauth_configured": oauth.is_configured(get_settings()),
            "flash": None if error else _FLASHES.get(str(request.query_params.get("ok"))),
            "error": error or _ERRORS.get(str(request.query_params.get("error"))),
        },
        status_code=status_code,
    )


@router.get("/settings")
def settings_page(request: Request, user: User = Depends(require_login)):
    return _settings_response(request, user)


@router.post("/settings/ynab/disconnect")
def disconnect(user: User = Depends(require_login)):
    get_connection_store().delete(user.id)
    events.record_event(get_settings().data_dir, user.id, events.YNAB_DISCONNECTED)
    return RedirectResponse("/settings?ok=disconnected", status_code=303)


@router.post("/settings/refresh-on-load")
def set_refresh_on_load(
    user: User = Depends(require_login),
    enabled: str = Form(default=""),
):
    """Toggle the opt-in 'refresh pending counts on page load'. A checkbox that
    posts `enabled=on` when ticked, nothing when not."""
    on = enabled == "on"
    get_user_store().set_refresh_on_load(user.id, on)
    return RedirectResponse(
        f"/settings?ok={'refresh_on' if on else 'refresh_off'}", status_code=303
    )


@router.post("/settings/delete-account")
def delete_account(
    request: Request,
    user: User = Depends(require_login),
    password: str = Form(default=""),
):
    """Permanently delete the logged-in user's account and all their data.

    Re-authenticates with the current password first: the session cookie alone
    shouldn't be enough to destroy an account (a borrowed/unlocked browser
    otherwise suffices), and unlike disconnect this is not undoable. A wrong
    password re-renders /settings with an error and changes nothing.

    Deleting the stored OAuth tokens does NOT revoke the grant on YNAB's side —
    YNAB has no token-revocation endpoint — so the confirmation points the user
    at YNAB's own security settings, same as Disconnect does.

    Attempts are throttled, or this form is an unthrottled oracle for guessing
    the password of a session someone else's browser left logged in. The
    counter is keyed per user (auth.reauth_key), NOT per email: sharing
    /login's email counter would let any anonymous visitor lock the owner out
    of deleting their own account just by failing logins for that address.
    """
    throttle_key = auth.reauth_key(user.id)
    locked_for = auth.password_lockout_seconds(throttle_key)
    if locked_for:
        return _settings_response(
            request,
            user,
            error=f"Too many failed attempts — try again in {locked_for}s.",
            status_code=429,
        )
    if not verify_password(password, user.password_hash):
        auth.record_password_failure(throttle_key)
        return _settings_response(request, user, error=WRONG_PASSWORD_ERROR, status_code=403)
    auth.clear_password_failures(throttle_key)
    get_user_store().delete(user.id)
    # After the delete, so a failure there leaves no "deleted" row for a live
    # account. This is the one row that outlives the user: a dangling uuid and
    # a date, recording that a deletion happened, not whose.
    events.record_event(
        get_settings().data_dir, user.id, events.ACCOUNT_DELETED, detail="self"
    )
    # clear() first, then the flag: the confirmation is a one-shot session
    # value, so a crafted URL can't show a stranger "your account was deleted".
    request.session.clear()
    request.session["account_deleted"] = True
    return RedirectResponse("/", status_code=303)


@router.get("/oauth/ynab/start")
def oauth_start(request: Request):
    settings = get_settings()
    if not oauth.is_configured(settings):
        raise HTTPException(404, "YNAB OAuth is not configured on this server")
    state = secrets.token_urlsafe(16)
    request.session["ynab_oauth_state"] = state
    return RedirectResponse(
        oauth.authorize_url(settings, _redirect_uri(request), state), status_code=303
    )


@router.get("/oauth/ynab/callback", name="oauth_callback")
def oauth_callback(
    request: Request,
    user: User = Depends(require_login),
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
):
    settings = get_settings()
    if not oauth.is_configured(settings):
        raise HTTPException(404, "YNAB OAuth is not configured on this server")
    expected_state = request.session.pop("ynab_oauth_state", None)
    if not expected_state or state != expected_state:
        raise HTTPException(403, "OAuth state mismatch — start the connection again")
    if error or not code:
        return RedirectResponse("/settings?error=denied", status_code=303)
    try:
        tokens = oauth.exchange_code(settings, code, _redirect_uri(request))
    except oauth.OAuthGrantError:
        return RedirectResponse("/settings?error=denied", status_code=303)
    oauth.save_token_response(get_connection_store(), user.id, tokens)
    events.record_event(get_settings().data_dir, user.id, events.YNAB_CONNECTED)
    return RedirectResponse("/settings?ok=connected", status_code=303)
