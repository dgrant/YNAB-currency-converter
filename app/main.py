from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from . import auth
from .config import get_settings
from .routes import conversions


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="YNAB Currency Converter")
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.secret_key,
        https_only=False,
        same_site="lax",
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
