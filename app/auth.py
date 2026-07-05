import secrets
import time

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from .config import get_settings
from .templates import templates

# Brute-force throttle for /login. In-memory and global (single-user app,
# single process): after LOCKOUT_THRESHOLD consecutive failures, each further
# failure doubles the wait before the next attempt is accepted.
LOCKOUT_THRESHOLD = 5
LOCKOUT_MAX_SECONDS = 300.0
_throttle = {"failures": 0, "locked_until": 0.0}


def _reset_throttle() -> None:
    _throttle["failures"] = 0
    _throttle["locked_until"] = 0.0


def _lockout_remaining() -> int:
    return max(0, int(_throttle["locked_until"] - time.monotonic()) + 1)


def _record_login_failure() -> None:
    _throttle["failures"] += 1
    if _throttle["failures"] >= LOCKOUT_THRESHOLD:
        delay = min(2.0 ** (_throttle["failures"] - LOCKOUT_THRESHOLD + 1), LOCKOUT_MAX_SECONDS)
        _throttle["locked_until"] = time.monotonic() + delay

# Single-password auth for the single-user v1. To move to Google Sign-In
# later, replace the routes below with an OIDC flow that sets the same
# session key ("authed") for allowlisted emails; require_login stays as-is.


def require_login(request: Request) -> None:
    if not request.session.get("authed"):
        raise HTTPException(status_code=303, headers={"Location": "/login"})


async def verify_csrf(request: Request) -> None:
    """Router-level dependency: POSTs must echo the session's CSRF token.

    Forms get the token via csrf_input() in templates.py. Reading the form
    here is safe — Starlette caches it, so route handlers see the same body.
    """
    if request.method != "POST":
        return
    token = request.session.get("csrf")
    form = await request.form()
    submitted = str(form.get("csrf_token", ""))
    if not token or not secrets.compare_digest(submitted, token):
        raise HTTPException(
            403, "Invalid or missing CSRF token — go back, reload the page, and retry"
        )


router = APIRouter(dependencies=[Depends(verify_csrf)])


@router.get("/login")
def login_form(request: Request):
    if request.session.get("authed"):
        return RedirectResponse("/conversions", status_code=303)
    return templates.TemplateResponse(request, "login.html", {"error": None})


@router.post("/login")
def login(request: Request, password: str = Form(...)):
    if time.monotonic() < _throttle["locked_until"]:
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": f"Too many failed attempts — try again in {_lockout_remaining()}s."},
            status_code=429,
        )
    if secrets.compare_digest(password, get_settings().app_password):
        _reset_throttle()
        request.session["authed"] = True
        return RedirectResponse("/conversions", status_code=303)
    _record_login_failure()
    return templates.TemplateResponse(
        request, "login.html", {"error": "Incorrect password."}, status_code=401
    )


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)
