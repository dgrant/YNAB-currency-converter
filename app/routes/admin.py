"""Admin-only dashboard: a read-only view of users and their activity.

Access is gated by the `require_admin` dependency (404 for a logged-in
non-admin, 303→/login for anonymous). This router is read-only (GET only), so
it adds no new POST/CSRF surface. Admin is granted out-of-band via
`python -m app.set_admin`, never a web route.
"""
from fastapi import APIRouter, Depends, Request

from .. import events
from ..auth import require_admin
from ..config import get_settings
from ..templates import templates
from ..users import User

router = APIRouter(dependencies=[Depends(require_admin)])


@router.get("/admin")
def admin_dashboard(request: Request, user: User = Depends(require_admin)):
    rows = events.aggregate_by_user(get_settings().data_dir)
    return templates.TemplateResponse(request, "admin.html", {"users": rows, "user": user})
