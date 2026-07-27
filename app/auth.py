import secrets
import sqlite3
import time

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from . import events
from .config import get_settings
from .templates import templates
from .users import User, UserStore, hash_password, normalize_email, verify_password

MIN_PASSWORD_LENGTH = 8

# Brute-force throttles for password checks (in-memory, single process): after
# LOCKOUT_THRESHOLD consecutive failures for a key, each further failure doubles
# the wait before the next attempt is accepted.
#
# TWO stores, deliberately not one. `_login_throttle` is keyed by email and any
# anonymous visitor can create entries in it just by POSTing /login, which never
# validates that the value looks like an email. `_reauth_throttle` is keyed by
# user id and only an authenticated request can touch it. Namespacing both into
# one dict (an earlier attempt did: "reauth:<user_id>") puts the re-auth counter
# back within reach of a stranger, who could POST /login with that literal
# string as the "email" and lock the owner out of deleting their own account —
# exactly the denial the per-user key exists to prevent. Separate dicts also
# stop an attacker flooding /login with junk emails to evict a victim's re-auth
# entry through the eviction sweep and reset their failure count.
LOCKOUT_THRESHOLD = 5
LOCKOUT_MAX_SECONDS = 300.0
_MAX_TRACKED_KEYS = 1000
_login_throttle: dict[str, dict] = {}
_reauth_throttle: dict[str, dict] = {}

# Verified against when the email doesn't exist, so unknown-email and
# wrong-password attempts take the same time (no account-probing oracle).
_DUMMY_HASH = hash_password("dummy-password")


def _reset_throttle() -> None:
    _login_throttle.clear()
    _reauth_throttle.clear()


def _throttle_entry(store: dict[str, dict], key: str) -> dict:
    if key not in store and len(store) >= _MAX_TRACKED_KEYS:
        # Drop expired entries rather than grow without bound.
        now = time.monotonic()
        for expired in [k for k, v in store.items() if v["locked_until"] < now]:
            del store[expired]
        # Expiry alone is not a bound: an attacker who keeps every tracked key
        # actively locked leaves nothing expired to sweep, and the dict grows
        # past the cap regardless (measured: 2500 entries against a cap of
        # 1000). Evict the entry closest to expiring so the cap is real. That
        # entry is the one whose lockout was about to lapse anyway, so this
        # costs an attacker nothing they weren't already getting.
        if len(store) >= _MAX_TRACKED_KEYS:
            del store[min(store, key=lambda k: store[k]["locked_until"])]
    return store.setdefault(key, {"failures": 0, "locked_until": 0.0})


def _lockout_remaining(store: dict[str, dict], key: str) -> int:
    entry = store.get(key)
    if entry is None:
        return 0
    return max(0, int(entry["locked_until"] - time.monotonic()) + 1)


def _record_failure(store: dict[str, dict], key: str) -> None:
    entry = _throttle_entry(store, key)
    entry["failures"] += 1
    if entry["failures"] >= LOCKOUT_THRESHOLD:
        # Clamp the exponent (not just the result): failures grows without
        # bound, and 2.0 ** ~1024 would raise OverflowError before min() ran.
        exponent = min(entry["failures"] - LOCKOUT_THRESHOLD + 1, 16)
        entry["locked_until"] = time.monotonic() + min(2.0**exponent, LOCKOUT_MAX_SECONDS)


def _is_locked(store: dict[str, dict], key: str) -> bool:
    entry = store.get(key)
    return entry is not None and time.monotonic() < entry["locked_until"]


# Public face of the re-auth throttle, for routes outside this module that ask
# a logged-in user to re-enter their password (delete-account). Keyed by user
# id and held in its own store — see the comment on `_reauth_throttle`.


def password_lockout_seconds(user_id: str) -> int:
    """Seconds until another re-auth attempt for this user is accepted, or 0
    if one is allowed right now."""
    if not _is_locked(_reauth_throttle, user_id):
        # Guard, not a ternary: _lockout_remaining rounds up, so it returns 1
        # for an entry that has already expired. Callers treat any non-zero
        # value as "still locked out", so that 1 would be a phantom lockout.
        return 0
    return _lockout_remaining(_reauth_throttle, user_id)


def record_password_failure(user_id: str) -> None:
    _record_failure(_reauth_throttle, user_id)


def clear_password_failures(user_id: str) -> None:
    _reauth_throttle.pop(user_id, None)


def get_user_store() -> UserStore:
    return UserStore(get_settings().data_dir)


def _login_session(request: Request, user: User) -> None:
    request.session["user_id"] = user.id
    request.session["email"] = user.email


def require_login(request: Request) -> User:
    """Dependency: the logged-in User, or a 303 to /login."""
    user_id = request.session.get("user_id")
    user = get_user_store().get(user_id) if user_id else None
    if user is None:
        request.session.pop("user_id", None)
        request.session.pop("email", None)
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    return user


def require_admin(request: Request) -> User:
    """Dependency: the logged-in User iff they're an admin. A logged-in
    non-admin gets a 404 (not 403) so /admin's existence isn't disclosed;
    an anonymous visitor still gets require_login's 303 to /login."""
    user = require_login(request)
    if not user.is_admin:
        raise HTTPException(status_code=404, detail="Not found")
    return user


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
    # compare bytes: compare_digest raises TypeError on non-ASCII *strings*,
    # which would turn a garbage token into a 500 instead of a 403
    if not token or not secrets.compare_digest(submitted.encode(), token.encode()):
        raise HTTPException(
            403, "Invalid or missing CSRF token — go back, reload the page, and retry"
        )


# CSRF is enforced app-wide (see create_app); routers don't opt in individually.
router = APIRouter()


@router.get("/healthz")
def healthz():
    """Unauthenticated liveness check; also answers 'what SHA is live?'."""
    return {"status": "ok", "version": get_settings().app_version}


@router.get("/")
def home(request: Request):
    """Public landing page; logged-in users go straight to their conversions."""
    if request.session.get("user_id"):
        return RedirectResponse("/conversions", status_code=303)
    # delete-account lands here; it sets a one-shot session flag rather than a
    # ?deleted=1 query param, so the confirmation can only appear for someone
    # who actually just deleted an account. A crafted link must not be able to
    # tell a stranger their account was deleted — that is a phishing opener.
    return templates.TemplateResponse(
        request, "landing.html", {"deleted": request.session.pop("account_deleted", False)}
    )


@router.get("/privacy")
def privacy(request: Request):
    """Public privacy policy; required for the YNAB OAuth App Review."""
    return templates.TemplateResponse(request, "privacy.html", {})


@router.get("/signup")
def signup_form(request: Request):
    if request.session.get("user_id"):
        return RedirectResponse("/conversions", status_code=303)
    return templates.TemplateResponse(request, "signup.html", {"error": None, "email": ""})


@router.post("/signup")
def signup(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    password_confirm: str = Form(...),
):
    email = normalize_email(email)

    def error(message: str, status_code: int):
        return templates.TemplateResponse(
            request, "signup.html", {"error": message, "email": email}, status_code=status_code
        )

    if "@" not in email or len(email) < 3 or len(email) > 254:
        return error("Enter a valid email address.", 400)
    if len(password) < MIN_PASSWORD_LENGTH:
        return error(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.", 400)
    if password_confirm != password:
        return error("Passwords do not match.", 400)
    try:
        user = get_user_store().create(email, password)
    except sqlite3.IntegrityError:
        return error("That email is already registered — log in instead.", 409)
    events.record_event(get_settings().data_dir, user.id, events.SIGNUP)
    _login_session(request, user)
    return RedirectResponse("/conversions", status_code=303)


@router.get("/login")
def login_form(request: Request):
    if request.session.get("user_id"):
        return RedirectResponse("/conversions", status_code=303)
    return templates.TemplateResponse(request, "login.html", {"error": None, "email": ""})


@router.post("/login")
def login(request: Request, email: str = Form(...), password: str = Form(...)):
    email = normalize_email(email)
    if _is_locked(_login_throttle, email):
        return templates.TemplateResponse(
            request,
            "login.html",
            {
                "error": "Too many failed attempts — "
                f"try again in {_lockout_remaining(_login_throttle, email)}s.",
                "email": email,
            },
            status_code=429,
        )
    user = get_user_store().get_by_email(email)
    # Always verify against *some* hash so unknown emails take as long as
    # wrong passwords.
    if verify_password(password, user.password_hash if user else _DUMMY_HASH) and user:
        _login_throttle.pop(email, None)
        events.record_event(get_settings().data_dir, user.id, events.LOGIN)
        _login_session(request, user)
        return RedirectResponse("/conversions", status_code=303)
    _record_failure(_login_throttle, email)
    return templates.TemplateResponse(
        request,
        "login.html",
        {"error": "Incorrect email or password.", "email": email},
        status_code=401,
    )


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)
