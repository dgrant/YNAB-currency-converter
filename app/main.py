import logging
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

from . import auth, db
from .config import get_settings
from .rates import RatesError
from .routes import conversions
from .routes import settings as settings_routes
from .templates import templates
from .ynab import YNABError

logger = logging.getLogger("ynabfx")

_ERROR_TITLES = {
    400: "Bad request",
    403: "Forbidden",
    404: "Not found",
    409: "Conflict",
    429: "Too many requests",
}


def _error_page(
    request: Request, title: str, message: str, hint: str, status_code: int
) -> Response:
    return templates.TemplateResponse(
        request,
        "error.html",
        {"title": title, "message": message, "hint": hint},
        status_code=status_code,
    )


def create_app() -> FastAPI:
    settings = get_settings()
    db.init(settings.data_dir)
    # CSRF verification is app-level so every router — present and future —
    # is covered without opting in (it no-ops on non-POST requests).
    app = FastAPI(
        title="Currency Converter for YNAB",
        dependencies=[Depends(auth.verify_csrf)],
    )
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.secret_key,
        https_only=settings.session_https_only,
        same_site="lax",
    )

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        try:
            response = await call_next(request)
        except Exception:
            # Catch here (not in ServerErrorMiddleware) so 500s still get the
            # security headers below and a friendly page instead of raw text.
            logger.exception("Unhandled error for %s %s", request.method, request.url.path)
            response = _error_page(
                request,
                "Something went wrong",
                "An unexpected error occurred.",
                "Try again; if it keeps happening, check the server logs.",
                status_code=500,
            )
        headers = response.headers
        headers.setdefault("X-Frame-Options", "DENY")
        headers.setdefault("X-Content-Type-Options", "nosniff")
        headers.setdefault("Referrer-Policy", "same-origin")
        # Only on the HTTPS deployment (same flag that makes the session cookie
        # Secure); sending HSTS over plain http dev would be wrong.
        if settings.session_https_only:
            headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
        # 'unsafe-inline' script-src: the templates use small inline scripts
        # (form wiring, confirm dialogs) and no external resources at all.
        headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self' 'unsafe-inline'; "
            "style-src 'self'; img-src 'self' data:; frame-ancestors 'none'; "
            "form-action 'self'; base-uri 'self'",
        )
        return response

    @app.exception_handler(StarletteHTTPException)
    async def friendly_http_error(request: Request, exc: StarletteHTTPException) -> Response:
        # Redirect-style HTTPExceptions (the 303 used by require_login) keep
        # the framework handler, which preserves their headers.
        if exc.status_code < 400:
            return await http_exception_handler(request, exc)
        return _error_page(
            request,
            _ERROR_TITLES.get(exc.status_code, f"Error {exc.status_code}"),
            str(exc.detail),
            "Go back and try again.",
            status_code=exc.status_code,
        )

    @app.exception_handler(YNABError)
    async def ynab_error(request: Request, exc: YNABError) -> Response:
        if exc.status_code == 401:
            # A documented YNAB 401 means the access token is invalid, expired,
            # or revoked (https://api.ynab.com/#errors). get_access_token
            # refreshes proactively before expiry, so a 401 on a data call
            # almost always means the user revoked the grant in YNAB — a generic
            # "try again shortly" is wrong here. Send them to /settings to
            # reconnect. Nothing was written (the 401 aborts the request), and
            # reconnecting via OAuth upserts a fresh token over the dead one.
            return RedirectResponse("/settings?error=revoked", status_code=303)
        if exc.status_code == 429:
            return _error_page(
                request,
                "YNAB rate limit reached",
                str(exc),
                "Wait a few minutes and try again — nothing was lost.",
                status_code=429,
            )
        return _error_page(
            request,
            "YNAB error",
            str(exc),
            "YNAB may be down, or the access token may be invalid or revoked. "
            "Try again shortly.",
            status_code=502,
        )

    @app.exception_handler(RatesError)
    async def rates_error(request: Request, exc: RatesError) -> Response:
        return _error_page(
            request,
            "Exchange-rate error",
            str(exc),
            "The exchange-rate service (Frankfurter) may be down. Nothing was "
            "written to YNAB. Try again shortly.",
            status_code=502,
        )
    app.mount(
        "/static",
        StaticFiles(directory=Path(__file__).parent / "static"),
        name="static",
    )
    app.include_router(auth.router)
    app.include_router(settings_routes.router)
    app.include_router(conversions.router)
    return app


app = create_app()
