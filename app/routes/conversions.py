from datetime import date

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from .. import oauth
from ..auth import require_login
from ..config import get_settings
from ..connections import ConnectionStore
from ..convert import build_preview, format_amount, format_original, is_converted, is_split
from ..rates import FrankfurterClient
from ..store import ConversionStore
from ..templates import templates
from ..users import User
from ..ynab import YNABClient

router = APIRouter(dependencies=[Depends(require_login)])

# The Frankfurter client is a process-wide singleton so connections are pooled
# across requests; YNABClient shares one pooled httpx client the same way but
# carries each user's token per request (tests reset both; see conftest.py).
_rates_client: FrankfurterClient | None = None


def get_store() -> ConversionStore:
    return ConversionStore(get_settings().data_dir)


def require_ynab(user: User = Depends(require_login)) -> YNABClient:
    """The user's YNAB client, or a 303 to /settings if not connected yet."""
    settings = get_settings()
    token = oauth.get_access_token(settings, ConnectionStore(settings.data_dir), user.id)
    if token is None:
        raise HTTPException(status_code=303, headers={"Location": "/settings"})
    return YNABClient(token, settings.ynab_api_base)


def get_rates_client() -> FrankfurterClient:
    global _rates_client
    if _rates_client is None:
        _rates_client = FrankfurterClient(get_settings().frankfurter_api_base)
    return _rates_client


@router.get("/conversions")
def index(request: Request, user: User = Depends(require_login)):
    settings = get_settings()
    has_ynab = ConnectionStore(settings.data_dir).get(user.id) is not None
    return templates.TemplateResponse(
        request,
        "index.html",
        {"conversions": get_store().load(user.id), "has_ynab": has_ynab},
    )


def _form_context(ynab: YNABClient) -> dict:
    """Budgets/accounts/currencies needed by the new & edit conversion forms."""
    budgets = []
    for budget in ynab.get_budgets():
        budgets.append(
            {
                "id": budget["id"],
                "name": budget["name"],
                "currency": (budget.get("currency_format") or {}).get("iso_code", ""),
                "accounts": [
                    {"id": a["id"], "name": a["name"]}
                    for a in ynab.get_accounts(budget["id"])
                ],
            }
        )
    currencies = get_rates_client().currencies()
    return {"budgets": budgets, "currencies": currencies, "today": date.today().isoformat()}


def _used_account_ids(user_id: str, except_conversion_id: str | None = None) -> list[str]:
    """Account ids that already have a conversion (an account gets at most one)."""
    return sorted(
        c["account_id"]
        for c in get_store().load(user_id)
        if c["id"] != except_conversion_id
    )


def _reject_duplicate_account(
    user_id: str, account_id: str, except_conversion_id: str | None = None
) -> None:
    if account_id in _used_account_ids(user_id, except_conversion_id):
        raise HTTPException(409, "That account already has a conversion configured")


def _validate_to_currency(ynab: YNABClient, budget_id: str, to_currency: str) -> None:
    """The 'to' currency must be the budget's own currency — check YNAB rather
    than trusting the form field."""
    budget = next((b for b in ynab.get_budgets() if b["id"] == budget_id), None)
    if budget is None:
        raise HTTPException(400, "Unknown budget")
    budget_currency = (budget.get("currency_format") or {}).get("iso_code", "")
    if budget_currency and to_currency != budget_currency:
        raise HTTPException(
            400,
            f"Budget '{budget['name']}' uses {budget_currency}; transactions must be "
            f"converted to {budget_currency}, not {to_currency}",
        )


@router.get("/conversions/new")
def new_form(
    request: Request,
    user: User = Depends(require_login),
    ynab: YNABClient = Depends(require_ynab),
):
    return templates.TemplateResponse(
        request,
        "conversion_form.html",
        {
            **_form_context(ynab),
            "conversion": None,
            "used_account_ids": _used_account_ids(user.id),
        },
    )


@router.post("/conversions")
def create(
    user: User = Depends(require_login),
    ynab: YNABClient = Depends(require_ynab),
    budget_id: str = Form(...),
    budget_name: str = Form(...),
    account_id: str = Form(...),
    account_name: str = Form(...),
    from_currency: str = Form(...),
    to_currency: str = Form(...),
    start_date: str = Form(...),
):
    _validate_start_date(start_date)
    _reject_duplicate_account(user.id, account_id)
    _validate_to_currency(ynab, budget_id, to_currency.upper())
    conversion = get_store().add(
        user.id,
        {
            "budget_id": budget_id,
            "budget_name": budget_name,
            "account_id": account_id,
            "account_name": account_name,
            "from_currency": from_currency.upper(),
            "to_currency": to_currency.upper(),
            "start_date": start_date,
        },
    )
    return RedirectResponse(f"/conversions/{conversion['id']}", status_code=303)


def _get_conversion_or_404(user_id: str, conversion_id: str) -> dict:
    conversion = get_store().get(user_id, conversion_id)
    if conversion is None:
        raise HTTPException(404, "Conversion not found")
    return conversion


def _validate_start_date(start_date: str) -> None:
    """A tampered/malformed start_date is a 400, not an unhandled 500."""
    try:
        date.fromisoformat(start_date)
    except ValueError as exc:
        raise HTTPException(400, "start_date must be a valid YYYY-MM-DD date") from exc


@router.get("/conversions/{conversion_id}")
def detail(
    request: Request,
    conversion_id: str,
    user: User = Depends(require_login),
    applied: int | None = None,
    skipped_splits: int = 0,
):
    conversion = _get_conversion_or_404(user.id, conversion_id)
    return templates.TemplateResponse(
        request,
        "detail.html",
        {"conversion": conversion, "applied": applied, "skipped_splits": skipped_splits},
    )


@router.get("/conversions/{conversion_id}/edit")
def edit_form(
    request: Request,
    conversion_id: str,
    user: User = Depends(require_login),
    ynab: YNABClient = Depends(require_ynab),
):
    conversion = _get_conversion_or_404(user.id, conversion_id)
    return templates.TemplateResponse(
        request,
        "conversion_form.html",
        {
            **_form_context(ynab),
            "conversion": conversion,
            "used_account_ids": _used_account_ids(user.id, except_conversion_id=conversion_id),
        },
    )


@router.post("/conversions/{conversion_id}/edit")
def edit(
    conversion_id: str,
    user: User = Depends(require_login),
    ynab: YNABClient = Depends(require_ynab),
    budget_id: str = Form(...),
    budget_name: str = Form(...),
    account_id: str = Form(...),
    account_name: str = Form(...),
    from_currency: str = Form(...),
    to_currency: str = Form(...),
    start_date: str = Form(...),
):
    _validate_start_date(start_date)
    _get_conversion_or_404(user.id, conversion_id)
    _reject_duplicate_account(user.id, account_id, except_conversion_id=conversion_id)
    _validate_to_currency(ynab, budget_id, to_currency.upper())
    get_store().update(
        user.id,
        conversion_id,
        {
            "budget_id": budget_id,
            "budget_name": budget_name,
            "account_id": account_id,
            "account_name": account_name,
            "from_currency": from_currency.upper(),
            "to_currency": to_currency.upper(),
            "start_date": start_date,
        },
    )
    return RedirectResponse(f"/conversions/{conversion_id}", status_code=303)


@router.post("/conversions/{conversion_id}/delete")
def delete(conversion_id: str, user: User = Depends(require_login)):
    get_store().delete(user.id, conversion_id)
    return RedirectResponse("/conversions", status_code=303)


@router.post("/conversions/{conversion_id}/preview")
def preview(
    request: Request,
    conversion_id: str,
    user: User = Depends(require_login),
    ynab: YNABClient = Depends(require_ynab),
):
    conversion = _get_conversion_or_404(user.id, conversion_id)
    transactions = ynab.get_transactions(
        conversion["budget_id"], conversion["account_id"], conversion["start_date"]
    )
    pending, skipped_splits = [], 0
    for txn in transactions:
        if txn["amount"] == 0 or is_converted(txn.get("memo")):
            continue
        if is_split(txn):
            skipped_splits += 1
        else:
            pending.append(txn)
    rows = []
    if pending:
        dates = [date.fromisoformat(t["date"]) for t in pending]
        rates = get_rates_client().get_rates(
            conversion["from_currency"], conversion["to_currency"], min(dates), max(dates)
        )
        rows = build_preview(
            pending, rates, conversion["from_currency"], conversion["to_currency"]
        )
    totals = None
    if rows:
        totals = {
            "original": format_original(
                sum(r["original_milliunits"] for r in rows), conversion["from_currency"]
            ),
            "converted": format_amount(
                sum(r["new_milliunits"] for r in rows), conversion["to_currency"]
            ),
        }
    return templates.TemplateResponse(
        request,
        "preview.html",
        {
            "conversion": conversion,
            "rows": rows,
            "totals": totals,
            "skipped_splits": skipped_splits,
            "total_fetched": len(transactions),
        },
    )


@router.post("/conversions/{conversion_id}/apply")
async def apply(
    request: Request,
    conversion_id: str,
    user: User = Depends(require_login),
    ynab: YNABClient = Depends(require_ynab),
):
    conversion = _get_conversion_or_404(user.id, conversion_id)
    form = await request.form()
    try:
        updates = [
            {
                "id": txn_id,
                "amount": int(str(form[f"amount_{txn_id}"])),
                "memo": str(form[f"memo_{txn_id}"])[:500],
            }
            for txn_id in form.getlist("selected")
        ]
    except (KeyError, ValueError) as exc:
        raise HTTPException(
            400, "Malformed apply form — go back and run the preview again"
        ) from exc
    updated, skipped_splits = [], 0
    if updates:
        # Re-check split status at write time: a transaction may have become a
        # split between preview render and approval, and patching a split
        # parent's amount would corrupt or be rejected by YNAB.
        current = ynab.get_transactions(
            conversion["budget_id"], conversion["account_id"], conversion["start_date"]
        )
        # Only patch transactions that still exist and are not splits. A txn
        # deleted in YNAB between preview and approval would otherwise make the
        # whole bulk PATCH fail, applying nothing.
        present_ids = {t["id"] for t in current}
        split_ids = {t["id"] for t in current if is_split(t)}
        safe = [u for u in updates if u["id"] in present_ids and u["id"] not in split_ids]
        skipped_splits = sum(1 for u in updates if u["id"] in split_ids)
        if safe:
            updated = ynab.update_transactions(conversion["budget_id"], safe)
    suffix = f"&skipped_splits={skipped_splits}" if skipped_splits else ""
    return RedirectResponse(
        f"/conversions/{conversion_id}?applied={len(updated)}{suffix}", status_code=303
    )
