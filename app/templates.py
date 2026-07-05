import secrets
from pathlib import Path

from fastapi.templating import Jinja2Templates
from markupsafe import Markup
from starlette.requests import Request

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


def csrf_input(request: Request) -> Markup:
    """Hidden CSRF field for POST forms; the token lives in the session."""
    token = request.session.get("csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["csrf"] = token
    return Markup(f'<input type="hidden" name="csrf_token" value="{token}">')


templates.env.globals["csrf_input"] = csrf_input
