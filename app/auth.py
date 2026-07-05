import secrets
import sqlite3
import time

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from .config import get_settings
from .templates import templates
from .users import User, UserStore, hash_password, normalize_email, verify_password

MIN_PASSWORD_LENGTH = 8

# Brute-force throttle for /login, per email (in-memory, single process):
# after LOCKOUT_THRESHOLD consecutive failures for an email, each further
# failure doubles the wait before the next attempt is accepted.
LOCKOUT_THRESHOLD = 5
LOCKOUT_MAX_SECONDS = 300.0
_MAX_TRACKED_EMAILS = 1000
_throttle: dict[str, dict] = {}

# Verified against when the email doesn't exist, so unknown-email and
# wrong-password attempts take the same time (no account-probing oracle).
_DUMMY_HASH = hash_password("dummy-password")


def _reset_throttle() -> None:
    _throttle.clear()


def _throttle_entry(email: str) -> dict:
    if email not in _throttle and len(_throttle) >= _MAX_TRACKED_EMAILS:
        # Drop expired entries rather than grow without bound.
        now = time.monotonic()
        for key in [k for k, v in _throttle.items() if v["locked_until"] < now]:
            del _throttle[key]
    return _throttle.setdefault(email, {"failures": 0, "locked_until": 0.0})


def _lockout_remaining(email: str) -> int:
    entry = _throttle.get(email)
    if entry is None:
        return 0
    return max(0, int(entry["locked_until"] - time.monotonic()) + 1)


def _record_login_failure(email: str) -> None:
    entry = _throttle_entry(email)
    entry["failures"] += 1
    if entry["failures"] >= LOCKOUT_THRESHOLD:
        # Clamp the exponent (not just the result): failures grows without
        # bound, and 2.0 ** ~1024 would raise OverflowError before min() ran.
        exponent = min(entry["failures"] - LOCKOUT_THRESHOLD + 1, 16)
        entry["locked_until"] = time.monotonic() + min(2.0**exponent, LOCKOUT_MAX_SECONDS)


def _is_locked(email: str) -> bool:
    entry = _throttle.get(email)
    return entry is not None and time.monotonic() < entry["locked_until"]


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
    return templates.TemplateResponse(request, "landing.html", {})


@router.get("/signup")
def signup_form(request: Request):
    if request.session.get("user_id"):
        return RedirectResponse("/conversions", status_code=303)
    return templates.TemplateResponse(request, "signup.html", {"error": None, "email": ""})


@router.post("/signup")
def signup(request: Request, email: str = Form(...), password: str = Form(...)):
    email = normalize_email(email)

    def error(message: str, status_code: int):
        return templates.TemplateResponse(
            request, "signup.html", {"error": message, "email": email}, status_code=status_code
        )

    if "@" not in email or len(email) < 3 or len(email) > 254:
        return error("Enter a valid email address.", 400)
    if len(password) < MIN_PASSWORD_LENGTH:
        return error(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.", 400)
    try:
        user = get_user_store().create(email, password)
    except sqlite3.IntegrityError:
        return error("That email is already registered — log in instead.", 409)
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
    if _is_locked(email):
        return templates.TemplateResponse(
            request,
            "login.html",
            {
                "error": "Too many failed attempts — "
                f"try again in {_lockout_remaining(email)}s.",
                "email": email,
            },
            status_code=429,
        )
    user = get_user_store().get_by_email(email)
    # Always verify against *some* hash so unknown emails take as long as
    # wrong passwords.
    if verify_password(password, user.password_hash if user else _DUMMY_HASH) and user:
        _throttle.pop(email, None)
        _login_session(request, user)
        return RedirectResponse("/conversions", status_code=303)
    _record_login_failure(email)
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
