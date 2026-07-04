import secrets

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from .config import get_settings
from .templates import templates

router = APIRouter()

# Single-password auth for the single-user v1. To move to Google Sign-In
# later, replace the routes below with an OIDC flow that sets the same
# session key ("authed") for allowlisted emails; require_login stays as-is.


def require_login(request: Request) -> None:
    if not request.session.get("authed"):
        raise HTTPException(status_code=303, headers={"Location": "/login"})


@router.get("/login")
def login_form(request: Request):
    if request.session.get("authed"):
        return RedirectResponse("/conversions", status_code=303)
    return templates.TemplateResponse(request, "login.html", {"error": None})


@router.post("/login")
def login(request: Request, password: str = Form(...)):
    if secrets.compare_digest(password, get_settings().app_password):
        request.session["authed"] = True
        return RedirectResponse("/conversions", status_code=303)
    return templates.TemplateResponse(
        request, "login.html", {"error": "Incorrect password."}, status_code=401
    )


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)
