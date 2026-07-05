from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from . import auth
from .config import get_settings
from .rates import RatesError
from .routes import conversions
from .templates import templates
from .ynab import YNABError


def _error_page(request: Request, title: str, message: str, hint: str, status_code: int) -> Response:
    return templates.TemplateResponse(
        request,
        "error.html",
        {"title": title, "message": message, "hint": hint},
        status_code=status_code,
    )


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="YNAB Currency Converter")
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.secret_key,
        https_only=False,
        same_site="lax",
    )

    @app.exception_handler(YNABError)
    async def ynab_error(request: Request, exc: YNABError) -> Response:
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
    app.include_router(conversions.router)
    return app


app = create_app()
