"""Account settings: connect/disconnect the user's YNAB credentials."""
import secrets

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse

from .. import oauth
from ..auth import require_login
from ..config import get_settings
from ..connections import ConnectionStore
from ..templates import templates
from ..users import User

router = APIRouter(dependencies=[Depends(require_login)])

_FLASHES = {
    "connected": "YNAB connected.",
    "disconnected": "YNAB disconnected. You can revoke the grant any time from "
    "YNAB's security settings.",
}
_ERRORS = {
    "denied": "YNAB authorization was cancelled or denied — nothing was connected.",
    "reauth": "Your YNAB connection predates OAuth-only support and had to be "
    "cleared — please reconnect.",
}


def get_connection_store() -> ConnectionStore:
    return ConnectionStore(get_settings().data_dir)


def _redirect_uri(request: Request) -> str:
    base = get_settings().public_base_url
    if base:
        return f"{base}/oauth/ynab/callback"
    return str(request.url_for("oauth_callback"))


@router.get("/settings")
def settings_page(request: Request, user: User = Depends(require_login)):
    connection = get_connection_store().get(user.id)
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "user": user,
            "connection": connection,
            "oauth_configured": oauth.is_configured(get_settings()),
            "flash": _FLASHES.get(str(request.query_params.get("ok"))),
            "error": _ERRORS.get(str(request.query_params.get("error"))),
        },
    )


@router.post("/settings/ynab/disconnect")
def disconnect(user: User = Depends(require_login)):
    get_connection_store().delete(user.id)
    return RedirectResponse("/settings?ok=disconnected", status_code=303)


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
    return RedirectResponse("/settings?ok=connected", status_code=303)
